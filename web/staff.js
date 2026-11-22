// 客服与仓库工作台：读取待办、提交处理结果，界面状态以服务端重新读取的结果为准。
import { api, post, errorText } from "/static/api.js";
import { el, $ } from "/static/dom.js";
import { state, runTask } from "/static/store.js";
import { supportItems, supportFilters, renderSupportQueue, renderApprovalDetails, renderTicketDetails } from "/static/support.js";
import { renderEventQueue, renderRefundScenario, renderWarehouseCard, boardColumns } from "/static/event-controls.js";

function showError(target, failure) {
  target.textContent = errorText(failure);
  target.hidden = false;
}

const emptyDetail = text => `<p class="empty">${text}</p>`;

// —— 客服：审批与工单 ——
const supportStatus = $("#support-status");
let supportFilter = "all";
let lastSupportItems = [];

function renderFilters(items) {
  const root = $("#support-filters");
  root.replaceChildren();
  for (const [key, [label, match]] of Object.entries(supportFilters)) {
    const button = el("button", `${label} ${items.filter(match).length}`, "chip");
    button.type = "button";
    button.setAttribute("aria-pressed", String(key === supportFilter));
    button.addEventListener("click", () => {
      supportFilter = key;
      renderFilters(lastSupportItems);
      renderQueue();
    });
    root.append(button);
  }
}

function renderQueue() {
  const items = lastSupportItems.filter(supportFilters[supportFilter][1]);
  renderSupportQueue($("#support-queue"), items, state.support.selected, selection => supportTask(async () => {
    state.support.selected = selection;
    await refreshSupport();
  }));
}

export async function refreshSupport() {
  const [approvals, tickets] = await Promise.all([api("/support/approvals"), api("/support/tickets")]);
  lastSupportItems = supportItems(approvals, tickets);
  let selected = state.support.selected;
  if (selected && !lastSupportItems.some(item => item.type === selected.type && item.id === selected.id)) {
    selected = state.support.selected = null;
  }
  const data = selected
    ? await api(`/support/${selected.type === "approval" ? "approvals" : "tickets"}/${encodeURIComponent(selected.id)}`)
    : null;
  renderFilters(lastSupportItems);
  renderQueue();
  const root = $("#support-details");
  if (!data) root.innerHTML = emptyDetail("从待办列表选择申请或工单，查看证据后处理。");
  else if (selected.type === "approval") renderApprovalDetails(root, data, performSupportAction);
  else renderTicketDetails(root, data, performSupportAction);
  root.hidden = false;
  state.ready.support = true;
  const open = lastSupportItems.filter(item => item.type === "approval" || ["open", "in_progress"].includes(item.status)).length;
  supportStatus.textContent = `${open} 项需要处理`;
}

// 处理完成后自动定位到下一条仍需处理的待办（按等待时间最久优先）。
function nextPending(current) {
  const pending = item => item.type === "approval" || ["open", "in_progress"].includes(item.status);
  return lastSupportItems.find(item => pending(item) && !(item.type === current?.type && item.id === current?.id)) || null;
}

function supportTask(action) {
  if (state.user?.role !== "support") return;
  return runTask({ scope: "support", onError: failure => {
    showError($("#support-error"), failure);
    supportStatus.textContent = "操作未完成，请先刷新待办核对结果；不会自动重发。";
  } }, async () => { $("#support-error").hidden = true; await action(); });
}

function performSupportAction(path, body) {
  if (!state.ready.support) return;
  return supportTask(async () => {
    supportStatus.textContent = "正在保存处理结果…";
    const handled = state.support.selected;
    await post(path, body);
    await refreshSupport();
    const next = nextPending(handled);
    if (next) {
      state.support.selected = { type: next.type, id: next.id };
      await refreshSupport();
      supportStatus.textContent = `已保存 ${handled.id}，已切换到下一条待办。`;
    } else {
      supportStatus.textContent = `已保存 ${handled.id}，当前没有其他需要处理的待办。`;
    }
  });
}

// —— 客服：退款模拟场景（演示控制） ——
const eventStatus = $("#event-status");

export async function refreshEvents() {
  const items = (await api("/demo/refund-scenarios")).map(item => ({ ...item, type: "refund" }));
  const selected = state.events.selected;
  const request = items.find(item => item.id === selected?.id);
  if (!request) state.events.selected = null;
  renderEventQueue($("#event-queue"), items, state.events.selected, selection => eventTask(async () => {
    state.events.selected = selection;
    await refreshEvents();
  }));
  const root = $("#event-details");
  renderRefundScenario(root, request, performEventAction);
  if (!request) {
    root.hidden = false;
    root.innerHTML = emptyDetail("选择申请，查看当前状态及可设置的退款模拟场景。");
  }
  state.ready.events = true;
  eventStatus.textContent = `${items.length} 项申请`;
}

// —— 仓库：作业看板 ——
const warehouseStatus = $("#warehouse-status");

export async function refreshWarehouse() {
  const [returns, exchanges] = await Promise.all([api("/warehouse/return-requests"), api("/warehouse/exchange-requests")]);
  const items = [...returns.map(item => ({ request: item, type: "return" })), ...exchanges.map(item => ({ request: item, type: "exchange" }))];
  const board = $("#warehouse-board");
  board.replaceChildren();
  const columnStatuses = boardColumns.map(([status]) => status);
  let actionable = 0;
  for (const [status, title, hint] of boardColumns) {
    const cards = items.filter(item => item.request.status === status);
    // 换货专属列没有待办时不占位，保持看板紧凑。
    if (!cards.length && ["ready_for_exchange", "replacement_in_transit"].includes(status)) continue;
    actionable += cards.length;
    const column = el("section", undefined, "board-column");
    const head = el("header", undefined, "board-column-head");
    head.append(el("h3", title), el("span", String(cards.length), "count"));
    column.append(head, el("p", hint, "muted"));
    for (const item of cards) column.append(renderWarehouseCard(item.request, item.type, performWarehouseAction));
    if (!cards.length) column.append(el("p", "暂无", "column-empty"));
    board.append(column);
  }
  const others = items.filter(item => !columnStatuses.includes(item.request.status));
  $("#warehouse-other-count").textContent = String(others.length);
  const list = $("#warehouse-other-list");
  list.replaceChildren(...others.map(item => renderWarehouseCard(item.request, item.type, performWarehouseAction, { compact: true })));
  if (!others.length) list.append(el("p", "暂无", "column-empty"));
  state.ready.events = true;
  warehouseStatus.textContent = actionable ? `${actionable} 项待仓库处理；结果以服务端状态为准` : "当前没有待仓库处理的申请。";
}

function eventTask(action) {
  const role = state.user?.role;
  if (!["warehouse", "support"].includes(role)) return;
  const [errorTarget, statusTarget] = role === "warehouse" ? [$("#warehouse-error"), warehouseStatus] : [$("#event-error"), eventStatus];
  return runTask({ scope: "events", onError: failure => {
    showError(errorTarget, failure);
    statusTarget.textContent = "操作未确认完成，请先刷新核对结果；不会自动重发。";
  } }, async () => { errorTarget.hidden = true; await action(); });
}

function performEventAction(path, body) {
  if (!state.ready.events) return;
  return eventTask(async () => {
    eventStatus.textContent = "正在保存…";
    await post(path, body);
    await refreshEvents();
    await refreshSupport();
  });
}

function performWarehouseAction(path, body) {
  if (!state.ready.events) return;
  return eventTask(async () => {
    warehouseStatus.textContent = "正在保存演示事件…";
    await post(path, body);
    await refreshWarehouse();
  });
}

// —— 视图切换与加载 ——
export function showSupportView(view) {
  state.support.view = view;
  for (const button of document.querySelectorAll("#support-tabs button")) {
    button.setAttribute("aria-pressed", String(button.dataset.view === view));
  }
  $("#support-workspace").hidden = view !== "support";
  $("#event-workspace").hidden = view !== "events";
}

export function resetStaffView() {
  supportFilter = "all";
  lastSupportItems = [];
  for (const id of ["#support-queue", "#support-filters", "#event-queue", "#warehouse-board", "#warehouse-other-list"]) $(id).replaceChildren();
  $("#support-details").innerHTML = emptyDetail("从待办列表选择申请或工单，查看证据后处理。");
  $("#event-details").hidden = false;
  $("#event-details").innerHTML = emptyDetail("选择申请，查看当前状态及可设置的退款模拟场景。");
  for (const id of ["#support-error", "#event-error", "#warehouse-error"]) $(id).hidden = true;
  for (const id of ["#support-status", "#event-status", "#warehouse-status"]) $(id).textContent = "";
}

export async function loadStaff() {
  if (state.user.role === "warehouse") {
    try { await refreshWarehouse(); }
    catch (failure) { showError($("#warehouse-error"), failure); warehouseStatus.textContent = "看板加载失败，请刷新。"; }
    return;
  }
  try { await refreshEvents(); }
  catch (failure) { showError($("#event-error"), failure); eventStatus.textContent = "退款模拟场景加载失败，请刷新。"; }
  try { await refreshSupport(); }
  catch (failure) { showError($("#support-error"), failure); supportStatus.textContent = "客服记录加载失败，请刷新待办。"; }
}

export function initStaff() {
  $("#refresh-support").addEventListener("click", () => supportTask(refreshSupport));
  $("#refresh-events").addEventListener("click", () => eventTask(refreshEvents));
  $("#refresh-warehouse").addEventListener("click", () => eventTask(refreshWarehouse));
  for (const button of document.querySelectorAll("#support-tabs button")) {
    button.addEventListener("click", () => {
      showSupportView(button.dataset.view);
      // 切换视图时读取最新记录，避免看到切换前的旧状态。
      if (button.dataset.view === "events") eventTask(refreshEvents);
      else supportTask(refreshSupport);
    });
  }
}

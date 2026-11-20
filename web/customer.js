// 客户工作台：左侧案件列表、中间对话、右侧案件面板。所有业务状态都从服务端重新读取。
import { api, post, errorText } from "/static/api.js";
import { el, $, formatTime } from "/static/dom.js";
import { statusPill, statusTone } from "/static/labels.js";
import { state, runTask } from "/static/store.js";
import { renderCaseDetails } from "/static/case-details.js";
import { renderTrace, streamMessage, stepLabel } from "/static/trace.js";
import { renderTicketDetails } from "/static/support.js";
import { renderWarehouseDetails } from "/static/event-controls.js";
import { renderCard } from "/static/cards.js";

// 示例诉求只填入输入框，不自动发送。
const prompts = [
  ["退 1 根数据线", "订单ORD-DEMO-002退1根数据线，买错了，未拆封。"],
  ["质量问题退耳机", "订单ORD-DEMO-004耳机无法开机，质量原因退1件，已经拆封。"],
  ["咨询退货期限", "订单ORD-DEMO-001普通退货几天内能退？"],
  ["查询物流", "查询物流 ORD-DEMO-008"],
  ["同款换货", "订单ORD-DEMO-002的ITEM-002键盘换1件，目标规格黑色，原因是想换颜色。"],
  ["转人工", "转人工"],
];

const layout = $("#customer-workspace");
const messageList = $("#message-list");
const input = $("#message-input");
const statusLine = $("#conversation-status");
const errorLine = $("#conversation-error");
const narrow = window.matchMedia("(max-width: 1180px)");

function fail(text) {
  return failure => {
    errorLine.textContent = errorText(failure);
    errorLine.hidden = false;
    statusLine.textContent = text;
  };
}

function customerTask(action, failText = "未能完成页面更新，请先刷新记录核对结果；不会自动重发。") {
  if (state.user?.role !== "customer") return;
  return runTask({ scope: "customer", onError: fail(failText) }, async () => {
    errorLine.hidden = true;
    await action();
  });
}

// —— 抽屉与面板 ——
function syncScrim() {
  const open = layout.classList.contains("sidebar-open") || (narrow.matches && layout.classList.contains("panel-open"));
  $("#scrim").hidden = !open;
}
function openPanel(tab = "case") {
  selectTab(tab);
  if (narrow.matches) layout.classList.add("panel-open");
  syncScrim();
}
function closeDrawers() {
  layout.classList.remove("panel-open", "sidebar-open");
  syncScrim();
}
function selectTab(tab) {
  for (const name of ["case", "trace"]) {
    $(`#tab-${name}`).setAttribute("aria-selected", String(name === tab));
    $(`#panel-${name}`).hidden = name !== tab;
  }
}

// —— 消息 ——
function messageNode(message, extraClass = "", cardContext = null) {
  const user = message.role === "user";
  const article = el("article", undefined, `message ${user ? "user" : "assistant"} ${extraClass}`.trim());
  const meta = el("div", undefined, "meta");
  meta.append(el("span", user ? "你" : "售后助手", "speaker"));
  const time = formatTime(message.created_at);
  if (time) meta.append(el("time", time));
  const bubble = el("div", undefined, "bubble");
  bubble.append(el("p", message.content));
  article.append(meta, bubble);
  const card = cardContext && renderCard(message.card, cardContext);
  if (card) article.append(card);
  return article;
}

// 卡片按钮只调用已有业务接口或结构化指令，完成后统一重新读取服务端记录。
const cardActions = {
  confirmReturn: card => performCaseAction(`/proposals/${encodeURIComponent(card.id)}/confirm`, { version: card.version }),
  cancelReturn: card => performCaseAction(`/proposals/${encodeURIComponent(card.id)}/cancel`, { version: card.version }),
  submitReturn: confirmationId => performCaseAction("/return-requests", { confirmation_id: confirmationId }),
  modify: () => onModify(),
  confirmExchange: card => customerTask(async () => {
    statusLine.textContent = "正在确认换货并核对库存…";
    const result = await post(`/exchange-proposals/${encodeURIComponent(card.id)}/confirm`, { version: card.version });
    state.customer = { ...state.customer, focus: "exchange", exchange: result.exchange_request?.id ?? null };
    await refreshConversation();
    statusLine.textContent = "换货申请已受理，请按右侧进度继续办理。";
    openPanel("case");
  }),
  choose: command => customerTask(() => send(command), "选择未完成处理，请先刷新记录核对结果；不会自动重发。"),
  openRecord: card => customerTask(async () => {
    if (card.kind === "exchange_request") state.customer = { ...state.customer, focus: "exchange", exchange: card.id };
    else if (card.kind === "ticket") state.customer = { ...state.customer, focus: "ticket", ticket: card.id };
    else state.customer.focus = "case";
    await refreshConversation();
    openPanel("case");
  }),
};

function renderMessages(messages, progress) {
  messageList.replaceChildren();
  const latest = [...messages].reverse().find(message => message.role === "assistant")?.id;
  // 同一方案出现在多条回复中时，只有最新一张卡片提供操作按钮。
  const newestCard = new Map();
  for (const message of messages) if (message.card?.id) newestCard.set(`${message.card.type}:${message.card.id}`, message.id);
  for (const message of messages) {
    const key = message.card?.id ? `${message.card.type}:${message.card.id}` : null;
    messageList.append(messageNode(message, "", {
      progress, isLatest: message.id === latest, isNewest: !key || newestCard.get(key) === message.id, act: cardActions }));
  }
  if (!messages.length) {
    messageList.append(el("p", "你好，我是售后助手。请描述你的售后诉求，例如订单号、商品和原因；也可以从左侧打开已有案件继续办理。", "empty"));
  }
  messageList.scrollTop = messageList.scrollHeight;
}

// —— 左侧列表 ——
function listButton(title, status, label, lines, current, onClick) {
  const button = el("button");
  button.type = "button";
  button.setAttribute("aria-current", String(current));
  const row = el("div", undefined, "item-row");
  row.append(el("strong", title), statusPill(status, label));
  button.append(row);
  for (const [text, className] of lines) if (text) button.append(el("span", text, className));
  button.addEventListener("click", onClick);
  return button;
}

function fillList(root, nodes, emptyText) {
  root.replaceChildren(...nodes);
  if (!nodes.length) root.append(el("p", emptyText));
}

function renderLists(cases, exchanges, tickets, activeCaseId) {
  fillList($("#case-list"), cases.map(item => listButton(
    item.product_name ? `${item.product_name}${item.specification ? " · " + item.specification : ""}` : "售后咨询",
    item.status, null,
    [[[item.order_id, item.quantity ? `${item.quantity} 件` : null, formatTime(item.created_at)].filter(Boolean).join(" · ")],
      [item.case_id, "item-id"]],
    state.customer.focus === "case" && item.case_id === activeCaseId,
    () => customerTask(async () => {
      statusLine.textContent = "正在恢复案件…";
      await post(`/cases/${encodeURIComponent(item.case_id)}/resume`, {});
      state.customer.focus = "case";
      input.value = "";
      await refreshConversation();
      statusLine.textContent = "案件已恢复，可继续办理。";
      closeDrawers();
      openPanel("case");
    }))), "暂无历史案件");

  fillList($("#customer-exchange-list"), exchanges.map(item => listButton(
    `${item.snapshot.product_name} 换${item.snapshot.target_specification}`, item.status, item.status_label,
    [[`${item.quantity} 件 · ${item.snapshot.order_id}`], [item.id, "item-id"]],
    state.customer.focus === "exchange" && item.id === state.customer.exchange,
    () => customerTask(async () => {
      state.customer = { ...state.customer, focus: "exchange", exchange: item.id };
      await refreshConversation();
      closeDrawers();
      openPanel("case");
    }))), "暂无换货申请");

  fillList($("#customer-ticket-list"), tickets.map(item => listButton(
    item.reason, item.status, null, [[formatTime(item.created_at)], [item.id, "item-id"]],
    state.customer.focus === "ticket" && item.id === state.customer.ticket,
    () => customerTask(async () => {
      state.customer = { ...state.customer, focus: "ticket", ticket: item.id };
      await refreshConversation();
      closeDrawers();
      openPanel("case");
    }))), "暂无人工工单");
}

// —— 对话头部与右侧面板 ——
function renderHeader(conversation, cases, progress) {
  const active = cases.find(item => item.case_id === conversation.case_id);
  $("#chat-title").textContent = active?.product_name ? `${active.product_name} · 售后办理` : "售后对话";
  $("#active-case").textContent = conversation.case_id
    ? `${active?.order_id ? active.order_id + " · " : ""}案件 ${conversation.case_id}`
    : "新对话，尚未关联案件";
  const pill = $("#chat-status-pill");
  pill.hidden = !progress;
  if (progress) {
    pill.className = `pill tone-${statusTone(progress.status)}`;
    pill.textContent = progress.status_label;
  }
}

function renderPanel(progress, exchange, ticket) {
  renderCaseDetails(progress, performCaseAction, onModify);
  renderWarehouseDetails($("#customer-exchange-details"), exchange, "exchange", (path, body) => customerTask(async () => {
    await post(path, body);
    await refreshConversation();
    statusLine.textContent = "已刷新换货结果，请核对当前状态。";
  }), true);
  renderTicketDetails($("#customer-ticket-details"), ticket);
  // 一次只展示一个焦点对象，避免案件、换货、工单详情上下堆叠。
  const focus = state.customer.focus === "exchange" && exchange ? "exchange"
    : state.customer.focus === "ticket" && ticket ? "ticket" : "case";
  $("#case-details").hidden = focus !== "case" || !progress;
  $("#customer-exchange-details").hidden = focus !== "exchange";
  $("#customer-ticket-details").hidden = focus !== "ticket";
  $("#panel-empty").hidden = focus !== "case" || Boolean(progress);
}

function onModify() {
  if (state.busy || !state.ready.customer) return;
  input.placeholder = "请说明需要修改的商品、数量或原因，例如：数量改为1件";
  statusLine.textContent = "请在对话中说明修改内容，变更后的方案需要重新确认。";
  closeDrawers();
  input.focus();
}

function performCaseAction(path, body) {
  if (!state.ready.customer) return;
  return customerTask(async () => {
    statusLine.textContent = "正在提交操作…";
    await post(path, body);
    state.customer.focus = "case";
    await refreshConversation();
    statusLine.textContent = "已刷新处理结果，请查看案件当前状态。";
  });
}

export async function refreshConversation() {
  const [conversation, cases, tickets, exchanges] = await Promise.all([
    api("/conversation"), api("/cases"), api("/tickets"), api("/exchange-requests")]);
  const progress = conversation.case_id ? await api(`/cases/${encodeURIComponent(conversation.case_id)}`) : null;
  if (!tickets.some(item => item.id === state.customer.ticket)) state.customer.ticket = null;
  const ticket = state.customer.ticket ? await api(`/tickets/${encodeURIComponent(state.customer.ticket)}`) : null;
  const exchange = exchanges.find(item => item.id === state.customer.exchange) || null;
  if (!exchange) state.customer.exchange = null;
  renderMessages(conversation.messages, progress);
  renderHeader(conversation, cases, progress);
  renderLists(cases, exchanges, tickets, conversation.case_id);
  renderPanel(progress, exchange, ticket);
  state.ready.customer = true;
  const runs = await api("/workflow-runs");
  renderTrace(runs[0] || null, progress);
}

// —— 发送消息：先显示待发送气泡和当前节点，结束后以服务端记录为准重绘 ——
let timer = null;
function showProgress(text) {
  const started = Date.now();
  $("#run-progress").hidden = false;
  $("#run-progress-text").textContent = text;
  $("#run-progress-time").textContent = "0 秒";
  clearInterval(timer);
  timer = setInterval(() => { $("#run-progress-time").textContent = `${Math.round((Date.now() - started) / 1000)} 秒`; }, 1000);
}
function hideProgress() {
  clearInterval(timer);
  $("#run-progress").hidden = true;
}

async function send(content) {
  messageList.querySelector(".empty")?.remove();
  messageList.append(messageNode({ role: "user", content }, "pending"));
  let draft = null;
  messageList.scrollTop = messageList.scrollHeight;
  input.value = "";
  showProgress("正在识别诉求…");
  try {
    await streamMessage(content, {
      onStep: event => { if (event.status === "running") $("#run-progress-text").textContent = `正在${stepLabel(event.name)}…`; },
      onDraft: text => {
        if (!draft) {
          draft = messageNode({ role: "assistant", content: "" }, "draft");
          draft.querySelector(".speaker").textContent = "政策答复草稿（待引用校验，不代表办理结果）";
          messageList.append(draft);
        }
        draft.querySelector("p").textContent += text;
        messageList.scrollTop = messageList.scrollHeight;
      },
    });
    await refreshConversation();
    statusLine.textContent = "回复已保存。";
  } catch (failure) {
    input.value = content; // 失败时保留原文，方便核对后再发。
    throw failure;
  } finally { hideProgress(); }
}

export function resetCustomerView() {
  closeDrawers();
  selectTab("case");
  messageList.replaceChildren();
  for (const id of ["#case-list", "#customer-exchange-list", "#customer-ticket-list"]) $(id).replaceChildren();
  renderCaseDetails(null);
  renderWarehouseDetails($("#customer-exchange-details"), null);
  renderTicketDetails($("#customer-ticket-details"), null);
  renderTrace(null);
  $("#panel-empty").hidden = false;
  $("#chat-status-pill").hidden = true;
  errorLine.hidden = true;
  statusLine.textContent = "";
  input.value = "";
}

export async function loadCustomer() {
  statusLine.textContent = "正在读取会话和案件…";
  try {
    await refreshConversation();
    statusLine.textContent = "";
  } catch (failure) {
    fail("记录加载失败，请点击刷新。")(failure);
  }
}

export function initCustomer() {
  $("#quick-prompts").replaceChildren(...prompts.map(([label, text]) => {
    const button = el("button", label);
    button.type = "button";
    button.title = text;
    button.addEventListener("click", () => {
      if (state.busy) return;
      input.value = text;
      input.focus();
    });
    return button;
  }));
  $("#message-form").addEventListener("submit", event => {
    event.preventDefault();
    const content = input.value.trim();
    if (!content || !state.ready.customer) return;
    customerTask(() => send(content), "消息未完成处理，请先刷新记录核对结果；不会自动重发。")
      .then(() => { if (!state.busy) input.focus(); });
  });
  input.addEventListener("keydown", event => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      $("#message-form").requestSubmit();
    }
  });
  $("#refresh-conversation").addEventListener("click", () => customerTask(async () => {
    await refreshConversation();
    statusLine.textContent = "已刷新服务端记录。";
  }));
  $("#new-conversation").addEventListener("click", () => customerTask(async () => {
    await post("/sessions", { customer_id: state.user.id });
    resetCustomerView();
    state.customer = { focus: "case", ticket: null, exchange: null };
    await refreshConversation();
    statusLine.textContent = "已开启新对话，原案件仍可从左侧恢复。";
    input.focus();
  }));
  $("#tab-case").addEventListener("click", () => selectTab("case"));
  $("#tab-trace").addEventListener("click", () => selectTab("trace"));
  $("#toggle-panel").addEventListener("click", () => {
    layout.classList.toggle("panel-open");
    syncScrim();
  });
  $("#close-panel").addEventListener("click", closeDrawers);
  $("#toggle-sidebar").addEventListener("click", () => {
    layout.classList.toggle("sidebar-open");
    syncScrim();
  });
  $("#scrim").addEventListener("click", closeDrawers);
  narrow.addEventListener("change", syncScrim);
}


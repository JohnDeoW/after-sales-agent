// 演示事件只调用已有业务写接口；状态和结果均重新从服务端读取。
import { ticketLabel } from "/static/support.js";
import { statusPill } from "/static/labels.js";
import { formatTime, since } from "/static/dom.js";
import { renderTimeline, exchangeSteps, requestSteps } from "/static/timeline.js";

function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function fact(list, label, value) {
  if (value !== undefined && value !== null) list.append(el("dt", label), el("dd", String(value)));
}
function field(fields, title, input) {
  const label = el("label", title); label.append(input); fields.append(label); return input;
}
function textInput() {
  const input = el("input"); input.required = true; input.maxLength = 100; return input;
}
function operationForm(root, title, path, onSubmit, inputs = []) {
  const form = el("form", undefined, "review-form");
  const fields = el("fieldset", undefined, "event-edit"); fields.disabled = true;
  for (const [label, input] of inputs) field(fields, label, input);
  const button = el("button", title); button.type = "submit"; fields.append(button);
  form.append(fields); root.append(form);
  const operationKey = crypto.randomUUID();
  form.addEventListener("submit", event => {
    event.preventDefault();
    const body = { operation_key: operationKey };
    for (const [, input] of inputs) {
      const value = input.value.trim();
      if (input.required && !value) return;
      body[input.name] = value;
    }
    onSubmit(path, body);
  });
}
function shippingForm(root, title, path, onSubmit) {
  const carrier = textInput(); carrier.name = "carrier";
  const tracking = textInput(); tracking.name = "tracking_number";
  operationForm(root, title, path, onSubmit, [["模拟承运商", carrier], ["模拟物流单号", tracking]]);
}

export function renderEventQueue(root, items, selected, onSelect) {
  root.replaceChildren();
  for (const item of items) {
    const button = el("button"); button.type = "button";
    const type = { return: "退货", exchange: "换货", refund: "退款场景" }[item.type];
    const row = el("div", undefined, "item-row");
    const product = item.product_name || item.snapshot?.product_name || item.details?.product_name;
    row.append(el("strong", type + (product ? " · " + product : "")),
      statusPill(item.status, item.status_label || ticketLabel(item.status)));
    button.append(row);
    if (item.type === "refund" && item.refund_amount_cents != null) {
      button.append(el("span", `${item.order_id} · 预计退款 ${(item.refund_amount_cents / 100).toFixed(2)} 元`, "muted"));
    }
    button.append(el("span", item.id, "item-id"));
    button.setAttribute("aria-current", String(selected?.id === item.id && selected?.type === item.type));
    button.addEventListener("click", () => onSelect({ type: item.type, id: item.id }));
    root.append(button);
  }
  if (!items.length) root.append(el("p", "暂无申请，请先使用客户身份办理。"));
}

export function renderWarehouseDetails(root, request, type, onSubmit, customer = false) {
  root.replaceChildren(); root.hidden = !request;
  if (!request) return;
  const exchange = type === "exchange";
  const details = exchange ? request.snapshot : request.details;
  const heading = el("div", undefined, "detail-heading");
  heading.append(el("h2", exchange ? "换货申请" : "退货事件"), statusPill(request.status, request.status_label));
  root.append(heading);
  root.append(renderTimeline(exchange ? exchangeSteps(request) : requestSteps(request), request.status,
    { viewer: customer ? "customer" : "warehouse" }));
  const facts = el("dl", undefined, "detail-facts");
  fact(facts, "申请", request.id); fact(facts, "订单", details.order_id);
  fact(facts, "商品", details.product_name); fact(facts, "商品项", request.order_item_id);
  fact(facts, "数量", request.quantity);
  fact(facts, "原规格", exchange ? details.original_specification : details.specification);
  if (exchange) fact(facts, "换新规格", details.target_specification);
  root.append(facts, el("p", request.next_step, "next-step"), el("p", "以下物流和检验均为模拟演示，请勿实际寄件。", "muted"));
  const history = eventHistory(request, exchange);
  if (history) root.append(history);
  if (request.replacement_logistics) {
    const shipment = request.replacement_logistics;
    root.append(el("p", "换新物流：" + shipment.carrier + " · " + shipment.tracking_number + " · " + (shipment.status === "delivered" ? "已模拟送达" : "运输中")));
  }
  if (customer) {
    const base = (exchange ? "/exchange-requests/" : "/return-requests/") + encodeURIComponent(request.id);
    if (request.available_actions.includes("shipment")) shippingForm(root, "提交换货寄回单号", base + "/shipment", onSubmit);
    if (request.available_actions.includes("cancel")) operationForm(root, "取消换货", base + "/cancel", onSubmit);
    return;
  }
  warehouseActions(root, request, exchange, onSubmit);
}

const eventNames = { shipment: "客户寄回", receipt: "仓库签收", inspection: "检验", dispatch: "换新发货", delivery: "换新送达", cancel: "取消" };

function eventHistory(request, exchange) {
  const events = exchange ? request.events : request.return_events;
  if (!events.length) return null;
  const history = el("ul", undefined, "event-history");
  for (const event of events) {
    const p = event.payload;
    const parts = [eventNames[event.kind], formatTime(event.created_at), p.carrier, p.tracking_number,
      p.result ? (p.result === "passed" ? "通过" : "未通过") : null, p.reason];
    history.append(el("li", parts.filter(Boolean).join(" · ")));
  }
  return history;
}

// 仓库只能看到当前状态允许的操作；提交仍由服务端校验顺序与角色。
function warehouseActions(root, request, exchange, onSubmit) {
  const base = "/warehouse" + (exchange ? "/exchange-requests/" : "/return-requests/") + encodeURIComponent(request.id);
  if (request.status === "in_transit") operationForm(root, "登记模拟仓库签收", base + "/receipt", onSubmit);
  if (request.status === "awaiting_inspection") {
    const result = el("select"); result.name = "result"; result.required = true;
    result.append(new Option("请选择检验结论", ""), new Option("通过", "passed"), new Option("未通过，转人工处理", "failed"));
    const reason = el("textarea"); reason.name = "reason"; reason.required = true; reason.maxLength = 1000; reason.rows = 2;
    reason.placeholder = "例如：模拟商品与申请一致";
    operationForm(root, "保存模拟检验结果", base + "/inspection", onSubmit, [["检验结论", result], ["检验说明", reason]]);
  }
  if (exchange && request.status === "ready_for_exchange") shippingForm(root, "登记模拟换新发货", base + "/dispatch", onSubmit);
  if (exchange && request.status === "replacement_in_transit") operationForm(root, "登记模拟换新送达", base + "/delivery", onSubmit);
}

// —— 仓库看板 ——
export const boardColumns = [
  ["in_transit", "待签收", "客户已寄回，等待仓库签收"],
  ["awaiting_inspection", "待检验", "已签收，等待检验结论"],
  ["ready_for_exchange", "待换新发货", "检验通过，使用预留库存发货"],
  ["replacement_in_transit", "换新运输中", "等待登记送达"],
];

function lastActivity(request, exchange) {
  const events = exchange ? request.events : request.return_events;
  return events.length ? events[events.length - 1].created_at : (request.created_at || request.confirmed_at);
}

export function renderWarehouseCard(request, type, onSubmit, { compact = false } = {}) {
  const exchange = type === "exchange";
  const details = exchange ? request.snapshot : request.details;
  const card = el("article", undefined, `board-card${compact ? " compact" : ""}`);
  const head = el("div", undefined, "item-row");
  // 早期种子方案缺少商品快照时，退回到申请自身的订单与商品项编号。
  head.append(el("strong", `${exchange ? "换货" : "退货"} · ${details.product_name || request.order_item_id}`), statusPill(request.status, request.status_label));
  const spec = exchange ? `${details.original_specification} → ${details.target_specification}` : details.specification;
  const line = [details.order_id || request.order_id, `${request.quantity} 件`, spec].filter(Boolean).join(" · ");
  card.append(head, el("p", line, "card-line"),
    el("p", `${request.id} · 已等待 ${since(lastActivity(request, exchange))}`, "item-id"));
  if (!compact) {
    const actions = el("div", undefined, "card-actions");
    warehouseActions(actions, request, exchange, onSubmit);
    card.append(actions);
  }
  const more = el("details", undefined, "card-more");
  more.append(el("summary", "进度与记录"));
  more.append(renderTimeline(exchange ? exchangeSteps(request) : requestSteps(request), request.status, { viewer: "warehouse" }));
  const history = eventHistory(request, exchange);
  if (history) more.append(history);
  if (request.replacement_logistics) {
    const shipment = request.replacement_logistics;
    more.append(el("p", `换新物流：${shipment.carrier} · ${shipment.tracking_number} · ${shipment.status === "delivered" ? "已模拟送达" : "运输中"}`, "muted"));
  }
  card.append(more);
  return card;
}

export function renderRefundScenario(root, request, onSubmit) {
  root.replaceChildren(); root.hidden = !request;
  if (!request) return;
  root.append(el("h2", "退款模拟场景"));
  const facts = el("dl", undefined, "detail-facts");
  fact(facts, "申请", request.id); fact(facts, "订单", request.order_id);
  fact(facts, "商品项", request.order_item_id); fact(facts, "当前状态", ticketLabel(request.status));
  fact(facts, "预计退款", (request.refund_amount_cents / 100).toFixed(2) + " 元");
  fact(facts, "当前场景", request.scenario === "success_then_timeout" ? "退款成功但响应超时" : "正常返回结果");
  fact(facts, "原退款操作", request.operation_id); root.append(facts);
  root.append(el("p", "场景选择不会执行退款或修改金额。切回客户身份，满足条件后执行模拟退款；超时后查询原操作核对结果。", "next-step"));
  if (request.operation_id) {
    root.append(el("p", "退款操作已发出，场景不可修改。客户应核对原操作，不要重复退款。", "muted")); return;
  }
  if (["cancelled", "superseded", "approval_rejected", "manual_review_required"].includes(request.status)) {
    root.append(el("p", "此申请已停止自动推进，当前无需选择退款场景。", "muted")); return;
  }
  const scenario = el("select"); scenario.name = "scenario";
  scenario.append(new Option("正常返回结果", "normal"), new Option("退款成功但响应超时", "success_then_timeout"));
  scenario.value = request.scenario;
  operationForm(root, "保存退款模拟场景", "/demo/return-requests/" + encodeURIComponent(request.id) + "/refund-scenario", onSubmit, [["退款场景", scenario]]);
}

// 对话内卡片：方案、候选商品/规格、已创建记录和待补信息。
// 卡片是当时回复的快照；按钮是否可用以当前案件状态和方案最新状态为准，提交后仍由后端核验版本。
import { el, money } from "/static/dom.js";
import { statusPill, statusLabel } from "/static/labels.js";

const fieldNames = { order_id: "订单号", item: "商品", quantity: "数量", reason: "原因", opened: "拆封情况",
  target_specification: "目标规格", sku: "SKU", ordered_on: "下单日期" };
const approvalReasons = { quality_issue: "质量原因需人工核实", high_amount: "超出自动处理金额门槛" };
const staleText = { superseded: "已被新版本替代", cancelled: "已取消", confirmed: "已确认" };

function facts(entries) {
  const list = el("dl", undefined, "card-facts");
  for (const [name, value] of entries) {
    if (value !== undefined && value !== null && value !== "") list.append(el("dt", name), el("dd", String(value)));
  }
  return list;
}

function button(text, onClick, className = "") {
  const node = el("button", text, className);
  node.type = "button";
  node.addEventListener("click", onClick);
  return node;
}

function shell(kind, title, pill) {
  const card = el("section", undefined, `chat-card card-${kind}`);
  const head = el("header", undefined, "chat-card-head");
  head.append(el("strong", title));
  if (pill) head.append(pill);
  card.append(head);
  return card;
}

// 按钮放在 fieldset 中，由页面统一在忙碌或记录未刷新时禁用。
function actionSet(...children) {
  const fields = el("fieldset", undefined, "card-actions");
  fields.disabled = true;
  fields.append(...children);
  return fields;
}

function returnProposal(card, ctx) {
  const progress = ctx.progress;
  const current = progress?.proposal?.id === card.id && ctx.isNewest;
  const status = card.live_status;
  const node = shell("proposal", `退货方案 v${card.version}`,
    status === "pending_confirmation" ? el("span", "待你确认", "pill tone-info") : statusPill(status, staleText[status] || statusLabel(status)));
  const amount = el("div", undefined, "card-amount");
  amount.append(el("small", "预计商品退款"), el("strong", money(card.refund_amount_cents)), el("small", "不含运费，预计金额不表示已退款"));
  node.append(amount, facts([
    ["商品", `${card.product_name || card.order_item_id}${card.specification ? " · " + card.specification : ""}`],
    ["订单", card.order_id], ["数量", `${card.quantity} 件`], ["原因", card.reason],
    ["拆封", card.opened === true ? "已拆封" : card.opened === false ? "未拆封" : null],
    ["人工审批", card.requires_approval ? "需要客服审批" + (card.approval_reasons.length ? `（${card.approval_reasons.map(r => approvalReasons[r] || r).join("、")}）` : "") : "无需审批"],
  ]));
  if (card.policy_refs.length) {
    node.append(el("p", "依据：" + card.policy_refs.map(p => `${p.clause_id} / ${p.version}`).join("、"), "card-note"));
  }
  if (status === "pending_confirmation" && current) {
    const ack = el("label", undefined, "acknowledgement");
    const check = el("input");
    check.type = "checkbox";
    ack.append(check, el("span", `我已核对 v${card.version} 的商品、数量、金额及退回要求`));
    const confirm = button("确认此版本", () => { if (check.checked) ctx.act.confirmReturn(card); });
    confirm.disabled = true;
    check.addEventListener("change", () => { confirm.disabled = !check.checked; });
    node.append(actionSet(ack, confirm, button("修改", () => ctx.act.modify(), "secondary"),
      button("取消办理", () => ctx.act.cancelReturn(card), "ghost")));
  } else if (status === "confirmed" && current && progress.available_actions.includes("submit_return") && progress.confirmation) {
    node.append(el("p", "方案已确认，尚未创建申请。", "card-note"),
      actionSet(button("提交退货申请", () => ctx.act.submitReturn(progress.confirmation.id)),
        button("修改", () => ctx.act.modify(), "secondary")));
  } else if (status === "pending_confirmation" && ctx.isNewest) {
    node.append(el("p", "当前案件已不是这个方案，请以最新方案为准。", "card-note"));
  }
  return node;
}

function exchangeProposal(card, ctx) {
  const status = card.live_status;
  const node = shell("proposal", `换货方案 v${card.version}`,
    status === "pending_confirmation" ? el("span", "待你确认", "pill tone-info") : statusPill(status, staleText[status] || statusLabel(status)));
  node.append(facts([
    ["商品", card.product_name], ["规格", `${card.original_specification} → ${card.target_specification}`],
    ["订单", card.order_id], ["数量", `${card.quantity} 件`], ["原因", card.reason], ["差价", "同款等价换货，无差价"],
  ]), el("p", "确认时会再次核对库存并预留，尚未创建申请。", "card-note"));
  if (status === "pending_confirmation" && ctx.isNewest) {
    node.append(actionSet(button("确认换货", () => ctx.act.confirmExchange(card)),
      button("修改", () => ctx.act.modify(), "secondary")));
  }
  return node;
}

function choice(card, ctx) {
  const items = card.type === "item_choice"
    ? card.items.map(item => ({ text: `${item.product_name} · ${item.specification}`, hint: `可选 ${item.remaining} 件 · ${item.id}`,
      command: `选择商品 ${item.id}`, disabled: item.remaining <= 0 }))
    : card.variants.map(v => ({ text: v.specification, hint: `可用 ${v.available} 件 · ${v.sku}`,
      command: `选择规格 ${v.sku}`, disabled: v.available <= 0 }));
  const node = shell("choice", card.type === "item_choice" ? `请选择要${card.purpose === "exchange" ? "换货" : "退货"}的商品` : "请选择同款等价规格");
  if (!ctx.isLatest) {
    node.append(el("p", "这是之前的选项，请以最新回复为准。", "card-note"));
    return node;
  }
  const options = items.map(item => {
    const option = button("", () => ctx.act.choose(item.command), "choice-option");
    option.append(el("span", item.text), el("small", item.hint));
    option.disabled = item.disabled;
    return option;
  });
  node.append(actionSet(...options));
  return node;
}

function record(card, ctx) {
  const titles = { exchange_request: "换货申请", return_request: "退货申请", ticket: "人工工单" };
  // 记录卡显示当前状态；与保存回复时不同则以当前状态为准。
  const status = card.live_status || card.status;
  const label = status === card.status && card.status_label ? card.status_label : statusLabel(status);
  const node = shell("record", titles[card.kind] || "记录", statusPill(status, label));
  node.append(el("p", card.id, "card-note mono"), actionSet(button("查看详情", () => ctx.act.openRecord(card), "secondary small")));
  return node;
}

function missing(card) {
  const node = el("div", undefined, "missing-tags");
  node.append(el("small", "待补充："));
  for (const field of card.fields) node.append(el("span", fieldNames[field] || field, "pill tone-wait"));
  return node;
}

export function renderCard(card, ctx) {
  if (!card) return null;
  if (card.type === "return_proposal") return returnProposal(card, ctx);
  if (card.type === "exchange_proposal") return exchangeProposal(card, ctx);
  if (card.type === "item_choice" || card.type === "spec_choice") return choice(card, ctx);
  if (card.type === "record") return record(card, ctx);
  if (card.type === "missing_fields") return missing(card);
  return null;
}

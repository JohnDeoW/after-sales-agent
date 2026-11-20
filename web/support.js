// 客服审批与工单展示；客户的工单详情也复用这里的只读部分。审批及工单操作仍由服务端接口授权。
import { el, money, formatTime, since } from "/static/dom.js";
import { statusPill, statusLabel } from "/static/labels.js";

export const ticketLabel = statusLabel;

const approvalReasons = { quality_issue: "质量原因需人工核实", high_amount: "超出自动处理金额门槛" };

function factList(entries) {
  const list = el("dl", undefined, "detail-facts");
  for (const [name, value] of entries) {
    if (value !== undefined && value !== null && value !== "") list.append(el("dt", name), el("dd", String(value)));
  }
  return list;
}

function heading(title, status, sub) {
  const row = el("div", undefined, "detail-heading");
  const wrap = el("div");
  wrap.append(el("h2", title));
  if (sub) wrap.append(el("p", sub, "muted"));
  row.append(wrap, statusPill(status, statusLabel(status)));
  return row;
}

function block(title, ...children) {
  const section = el("section", undefined, "evidence-block");
  section.append(el("h3", title), ...children);
  return section;
}

function orderEvidence(order) {
  const content = el("div");
  if (!order) { content.append(el("p", "暂无已核验订单事实。", "muted")); return content; }
  content.append(el("p", `订单 ${order.id} · 下单 ${order.ordered_on} · 签收 ${order.signed_on || "未记录"}`));
  const list = el("ul", undefined, "plain-list");
  for (const item of order.items) {
    list.append(el("li", `${item.product_name} · ${item.specification} · ${item.quantity} 件 · 实付 ${money(item.paid_amount_cents)}`));
  }
  content.append(list);
  return content;
}

function labeled(title, input) {
  const label = el("label", title);
  label.append(input);
  return label;
}

// 快捷填入只写入文本框，仍需客服核对后提交。
function presets(textarea, texts) {
  const row = el("div", undefined, "preset-row");
  for (const text of texts) {
    const button = el("button", text, "ghost small");
    button.type = "button";
    button.addEventListener("click", () => { textarea.value = text; textarea.focus(); });
    row.append(button);
  }
  return row;
}

// —— 待办列表 ——
export function supportItems(approvals, tickets) {
  const ticketRank = { open: 1, in_progress: 1, replied: 2, resolved: 3 };
  return [
    ...approvals.map(a => ({
      type: "approval", id: a.request_id, status: "pending_approval", rank: 0, created_at: a.created_at,
      title: `${a.product_name || "退货"} · ${money(a.refund_amount_cents)}`,
      summary: `${a.reason} · ${a.quantity} 件 · ${a.order_id || ""}`,
    })),
    ...tickets.map(t => ({
      type: "ticket", id: t.id, status: t.status, rank: ticketRank[t.status] ?? 1, created_at: t.created_at,
      title: t.reason, summary: t.case_id ? `关联案件 ${t.case_id}` : "未关联案件",
    })),
  ].sort((a, b) => a.rank - b.rank || String(a.created_at).localeCompare(String(b.created_at)));
}

export const supportFilters = {
  all: ["全部", () => true],
  approval: ["待审批", item => item.type === "approval"],
  ticket: ["待处理工单", item => item.type === "ticket" && ["open", "in_progress"].includes(item.status)],
  replied: ["已回复", item => item.type === "ticket" && item.status === "replied"],
  resolved: ["已解决", item => item.type === "ticket" && item.status === "resolved"],
};

export function renderSupportQueue(root, items, selected, onSelect) {
  root.replaceChildren();
  for (const item of items) {
    const button = el("button");
    button.type = "button";
    button.setAttribute("aria-current", String(selected?.id === item.id && selected?.type === item.type));
    const row = el("div", undefined, "item-row");
    row.append(el("strong", item.title), statusPill(item.status, item.type === "approval" ? "待审批" : statusLabel(item.status)));
    const waited = item.status === "resolved" ? formatTime(item.created_at) : `已等待 ${since(item.created_at)}`;
    button.append(row, el("span", `${item.type === "approval" ? "退款审批" : "人工工单"} · ${item.summary}`), el("span", `${waited} · ${item.id}`, "item-id"));
    button.addEventListener("click", () => onSelect({ type: item.type, id: item.id }));
    root.append(button);
  }
  if (!items.length) root.append(el("p", "当前筛选下没有待办。"));
}

// —— 审批详情：左侧事实与证据，右侧审批表单 ——
export function renderApprovalDetails(root, data, onSubmit) {
  root.replaceChildren();
  const { request, proposal, order, approval } = data;
  const d = proposal.details;
  const main = el("div", undefined, "work-main");
  main.append(heading(`${d.product_name} · 退款审批`, request.status, `申请 ${request.id} · 案件 ${proposal.case_id}`));

  const summary = el("div", undefined, "key-figures");
  for (const [name, value] of [["预计商品退款", money(proposal.refund_amount_cents)], ["数量", `${proposal.quantity} 件`], ["方案版本", `v${proposal.version}`]]) {
    const figure = el("div", undefined, "key-figure");
    figure.append(el("strong", value), el("small", name));
    summary.append(figure);
  }
  main.append(summary);
  const reasons = el("div", undefined, "reason-tags");
  for (const reason of d.approval_reasons) reasons.append(el("span", approvalReasons[reason] || reason, "pill tone-human"));
  main.append(reasons);
  main.append(block("申请内容", factList([
    ["订单", d.order_id], ["商品", `${d.product_name} · ${d.specification}`], ["商品项", d.order_item_id],
    ["退货原因", proposal.reason], ["拆封情况", d.opened === true ? "已拆封" : d.opened === false ? "未拆封" : null],
    ["方案", `${proposal.id} · v${proposal.version}`], ["金额说明", "不含运费；预计金额不表示已退款"],
  ])));
  main.append(block("订单事实", orderEvidence(order)));
  const policy = el("div");
  if (!data.policy_evidence_complete) policy.append(el("p", "原版本政策证据不完整，请先核对；执行时服务端会重新校验。", "error"));
  for (const item of data.policy_evidence) {
    const card = el("div", undefined, "citation");
    card.append(el("strong", `${item.clause_id || ""} / ${item.version}`), el("span", item.title), el("p", item.body));
    policy.append(card);
  }
  main.append(block("政策证据（虚构演示规则）", policy));

  const side = el("aside", undefined, "work-side");
  const panel = el("div", undefined, "action-card");
  panel.append(el("h3", "审批结论"), el("p", "批准仅推进退货流程，仍需寄回、签收和检验，不会直接退款。", "muted"));
  if (approval) {
    panel.append(el("p", `已${approval.decision === "approved" ? "批准" : "拒绝"}：${approval.reason}`, "next-step"));
  }
  if (request.status !== "pending_approval" || proposal.status !== "confirmed" || approval) {
    panel.append(el("p", "此申请当前不可再次审批。", "muted"));
  } else {
    const form = el("form", undefined, "action-form");
    const fields = el("fieldset", undefined, "support-edit");
    fields.disabled = true;
    const choice = el("div", undefined, "decision-choice");
    for (const [value, text] of [["approved", "批准"], ["rejected", "拒绝"]]) {
      const option = el("label", undefined, `decision decision-${value}`);
      const radio = el("input");
      radio.type = "radio"; radio.name = "decision"; radio.value = value; radio.required = true;
      option.append(radio, el("span", text));
      choice.append(option);
    }
    const reason = el("textarea");
    reason.name = "reason"; reason.required = true; reason.maxLength = 1000; reason.rows = 4;
    fields.append(choice, labeled("审批说明", reason),
      presets(reason, ["演示审核通过，按当前方案办理", "材料不足，请补充故障说明后申请复核"]));
    const submit = el("button", "提交审批结论"); submit.type = "submit";
    fields.append(submit);
    form.append(fields);
    panel.append(form);
    form.addEventListener("submit", event => {
      event.preventDefault();
      const decision = form.elements.decision.value;
      if (!decision || !reason.value.trim()) return;
      onSubmit(`/support/approvals/${encodeURIComponent(request.id)}`, {
        proposal_id: proposal.id, version: proposal.version, decision, reason: reason.value.trim(),
      });
    });
  }
  side.append(panel);
  const grid = el("div", undefined, "work-grid");
  grid.append(main, side);
  root.append(grid);
}

// —— 工单详情：客户只读；客服另有证据和处理表单 ——
function ticketHistory(ticket) {
  const history = el("div", undefined, "ticket-replies");
  for (const update of ticket.updates) {
    const item = el("article", undefined, "message");
    item.append(el("span", `${statusLabel(update.status)} · ${formatTime(update.created_at)}`, "speaker"));
    item.append(el("p", update.reply || "客服已开始处理此工单。"));
    history.append(item);
  }
  if (!ticket.updates.length) history.append(el("p", "暂无客服答复。", "muted"));
  return history;
}

function ticketEvidence(ticket) {
  const snapshot = ticket.snapshot;
  const evidence = snapshot.evidence || {};
  const facts = el("div");
  facts.append(orderEvidence(snapshot.order || evidence.order));
  for (const text of [evidence.customer_statement, evidence.statement, evidence.original_message, snapshot.case?.summary]) {
    if (text) facts.append(el("p", text, "quote"));
  }
  if (evidence.logistics_record) {
    const logistics = evidence.logistics_record;
    facts.append(el("p", `承运商：${logistics.carrier || "未记录"} · 单号：${logistics.tracking_number || "未记录"}`));
    const list = el("ul", undefined, "plain-list");
    for (const event of logistics.events || []) list.append(el("li", event.description));
    facts.append(list);
  }
  for (const ref of evidence.material_refs || []) facts.append(el("p", `用户材料引用：${ref}（未进行内容识别）`));
  for (const p of snapshot.proposals || []) {
    facts.append(el("p", `转交时方案 ${p.id} v${p.version} · ${p.quantity} 件 · 预计退款 ${money(p.refund_amount_cents)}`));
  }
  facts.append(el("p", "以上为转交时保存的证据，用户陈述及材料引用不等于责任认定。", "muted"));
  return facts;
}

export function renderTicketDetails(root, ticket, onSubmit = null) {
  root.replaceChildren();
  root.hidden = !ticket;
  if (!ticket) return;
  const main = el("div", undefined, onSubmit ? "work-main" : "");
  main.append(heading(onSubmit ? ticket.reason : "人工工单", ticket.status, `${ticket.id}${ticket.case_id ? " · 关联案件 " + ticket.case_id : ""}`));
  if (!onSubmit) main.append(el("p", ticket.reason));
  main.append(el("p", "工单处理状态不代表退款或其他业务操作已执行。", "muted"));
  if (ticket.status !== "resolved" && ticket.todo?.length) {
    const todo = el("ul", undefined, "plain-list");
    for (const text of ticket.todo) todo.append(el("li", text));
    main.append(onSubmit ? block("待办", todo) : todo);
  }
  if (!onSubmit) {
    main.append(ticketHistory(ticket));
    root.append(main);
    return;
  }
  main.append(block("转交时证据", ticketEvidence(ticket)));
  if (ticket.current_case) main.append(block("关联案件当前记录", el("p", ticket.current_case.case.summary || "请核对当前案件信息。")));
  main.append(block("处理记录", ticketHistory(ticket)));

  const side = el("aside", undefined, "work-side");
  const panel = el("div", undefined, "action-card");
  panel.append(el("h3", "处理工单"));
  if (ticket.status === "resolved") {
    panel.append(el("p", "工单已解决，不再接受新的处理记录。", "muted"));
  } else {
    const form = el("form", undefined, "action-form");
    const fields = el("fieldset", undefined, "support-edit");
    fields.disabled = true;
    const status = el("select"); status.name = "status";
    status.append(new Option("处理中", "in_progress"), new Option("已回复", "replied"), new Option("已解决", "resolved"));
    status.value = ticket.status === "open" ? "in_progress" : ticket.status;
    const reply = el("textarea"); reply.name = "reply"; reply.maxLength = 2000; reply.rows = 5;
    reply.required = status.value !== "in_progress";
    status.addEventListener("change", () => { reply.required = status.value !== "in_progress"; });
    const submit = el("button", "保存工单处理"); submit.type = "submit";
    fields.append(labeled("处理状态", status), labeled("答复（回复或解决时必填）", reply),
      presets(reply, ["请补充故障描述和可选材料引用，当前尚未批准或执行退款。", "已核实情况，问题已处理完毕。"]), submit);
    form.append(fields);
    panel.append(form);
    // 一次显示对应一次提交编号；刷新前重试相同内容可复用，不能覆盖原答复。
    const operationKey = crypto.randomUUID();
    form.addEventListener("submit", event => {
      event.preventDefault();
      if (reply.required && !reply.value.trim()) return;
      onSubmit(`/support/tickets/${encodeURIComponent(ticket.id)}/updates`, { operation_key: operationKey, status: status.value, reply: reply.value.trim() });
    });
  }
  side.append(panel);
  const grid = el("div", undefined, "work-grid");
  grid.append(main, side);
  root.append(grid);
}

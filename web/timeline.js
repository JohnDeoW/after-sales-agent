// 售后进度时间线：步骤状态只由服务端返回的 status、事件记录和方案字段推导，不在前端推进业务。
import { el, money, formatTime } from "/static/dom.js";

// 每个状态正在等待谁处理。
const waitingParty = {
  collecting_information: "你", awaiting_confirmation: "你", confirmed: "你", awaiting_return: "你",
  ready_for_refund: "你", refund_unknown: "你", ineligible: "你",
  pending_approval: "客服", approval_rejected: "人工客服", manual_review_required: "人工客服",
  policy_unavailable: "人工客服", refund_failed: "人工客服",
  in_transit: "仓库", awaiting_inspection: "仓库", ready_for_exchange: "仓库", replacement_in_transit: "物流",
};

// 当前步骤的样式：等待他人 / 需要处理 / 失败 / 转人工。
const currentTone = {
  pending_approval: "wait", in_transit: "wait", awaiting_inspection: "wait", ready_for_exchange: "wait",
  replacement_in_transit: "wait", refund_unknown: "wait",
  approval_rejected: "danger", refund_failed: "danger", ineligible: "danger",
  manual_review_required: "human", policy_unavailable: "human",
};

const stepStateText = { done: "完成", current: "进行中", pending: "未开始", na: "不适用", stopped: "已停止", failed: "未通过" };

function event(events, kind) {
  return (events || []).find(item => item.kind === kind) || null;
}

/**
 * steps: [{ key, title, detail?, na? }]，index: 当前步骤序号（等于 steps.length 表示全部完成）。
 * stopped: 取消或失效时，在 index 处标记停止，其后步骤保持未开始。
 */
function build(steps, index, status, { stopped = false } = {}) {
  const tone = currentTone[status] || "info";
  return steps.map((step, position) => {
    if (step.na) return { ...step, state: "na" };
    if (position < index) return { ...step, state: "done" };
    if (position === index) {
      if (stopped) return { ...step, state: "stopped" };
      return { ...step, state: tone === "danger" ? "failed" : "current", tone };
    }
    return { ...step, state: "pending" };
  });
}

// —— 退货案件：方案 → 确认 → 申请 → 审批 → 寄回 → 签收 → 检验 → 退款 ——
export function returnSteps(progress) {
  const { proposal, return_request: request, approval } = progress;
  const events = request?.return_events || [];
  const shipment = event(events, "shipment");
  const receipt = event(events, "receipt");
  const inspection = event(events, "inspection");
  const refund = progress.refund_operation;
  const confirmation = progress.confirmation_record;
  const steps = [
    { key: "proposal", title: "生成方案", detail: proposal ? `v${proposal.version} · 预计 ${money(proposal.refund_amount_cents)}` : null },
    { key: "confirm", title: "你确认方案", detail: confirmation ? formatTime(confirmation.confirmed_at) : null },
    { key: "submit", title: "提交申请", detail: request ? formatTime(request.created_at) : null },
    { key: "approval", title: "客服审批", na: Boolean(proposal && !proposal.requires_approval),
      detail: proposal && !proposal.requires_approval ? "无需审批" : approval ? (approval.decision === "approved" ? "已批准" : "已拒绝") : null },
    { key: "shipment", title: "寄回商品", detail: shipment ? `${shipment.payload.carrier} · ${shipment.payload.tracking_number}` : null },
    { key: "receipt", title: "仓库签收", detail: receipt ? formatTime(receipt.created_at) : null },
    { key: "inspection", title: "仓库检验", detail: inspection ? (inspection.payload.result === "passed" ? "通过" : "未通过") : null },
    { key: "refund", title: "模拟退款", detail: refund ? (refund.status === "succeeded" ? money(refund.amount_cents) : refund.status === "unknown" ? "结果待核对" : refund.status === "failed" ? "失败" : "处理中") : null },
  ];
  const status = progress.status;
  const byStatus = {
    collecting_information: 0, ineligible: 0, policy_unavailable: 0,
    awaiting_confirmation: 1, confirmed: progress.confirmation_valid ? 2 : 1,
    pending_approval: 3, approval_rejected: 3, awaiting_return: 4, in_transit: 5, awaiting_inspection: 6,
    ready_for_refund: 7, refund_unknown: 7, refund_failed: 7, refunded: 8,
  };
  if (status in byStatus) return build(steps, byStatus[status], status);
  // 取消、失效或转人工：停在已有记录到达的位置。
  let reached = 0;
  if (proposal) reached = 1;
  if (confirmation || request) reached = 2;
  if (request) reached = 3;
  if (request && (!proposal?.requires_approval || approval?.decision === "approved")) reached = 4;
  if (shipment) reached = 5;
  if (receipt) reached = 6;
  if (inspection?.payload.result === "passed") reached = 7;
  const steps2 = build(steps, reached, status, { stopped: ["cancelled", "superseded"].includes(status) });
  if (status === "manual_review_required") {
    const failedInspection = inspection && inspection.payload.result !== "passed";
    steps2[reached] = { ...steps2[reached], state: failedInspection ? "failed" : "current", tone: "human" };
  }
  return steps2;
}

// —— 仓库视角的退货申请：从申请开始 ——
export function requestSteps(request) {
  const progress = {
    status: request.status, proposal: { version: request.proposal_version, refund_amount_cents: request.refund_amount_cents, requires_approval: request.requires_approval },
    return_request: request, approval: request.approval, refund_operation: request.refund_operation,
    confirmation_record: { confirmed_at: null }, confirmation_valid: true,
  };
  return returnSteps(progress).slice(2);
}

// —— 换货：申请 → 寄回 → 签收 → 检验 → 换新发货 → 换新送达 ——
export function exchangeSteps(request) {
  const events = request.events || [];
  const shipment = event(events, "shipment");
  const receipt = event(events, "receipt");
  const inspection = event(events, "inspection");
  const dispatch = event(events, "dispatch");
  const delivery = event(events, "delivery");
  const steps = [
    { key: "submit", title: "受理并预留库存", detail: formatTime(request.confirmed_at) },
    { key: "shipment", title: "寄回原商品", detail: shipment ? `${shipment.payload.carrier} · ${shipment.payload.tracking_number}` : null },
    { key: "receipt", title: "仓库签收", detail: receipt ? formatTime(receipt.created_at) : null },
    { key: "inspection", title: "仓库检验", detail: inspection ? (inspection.payload.result === "passed" ? "通过" : "未通过") : null },
    { key: "dispatch", title: "换新发货", detail: dispatch ? `${dispatch.payload.carrier} · ${dispatch.payload.tracking_number}` : null },
    { key: "delivery", title: "换新送达", detail: delivery ? formatTime(delivery.created_at) : null },
  ];
  const byStatus = { awaiting_return: 1, in_transit: 2, awaiting_inspection: 3, ready_for_exchange: 4, replacement_in_transit: 5, completed: 6 };
  if (request.status in byStatus) return build(steps, byStatus[request.status], request.status);
  let reached = 1;
  if (shipment) reached = 2;
  if (receipt) reached = 3;
  if (inspection?.payload.result === "passed") reached = 4;
  const result = build(steps, reached, request.status, { stopped: request.status === "cancelled" });
  if (request.status === "manual_review_required") {
    result[reached] = { ...result[reached], state: inspection && inspection.payload.result !== "passed" ? "failed" : "current", tone: "human" };
  }
  return result;
}

export function renderTimeline(steps, status, { viewer = "customer" } = {}) {
  const wrap = el("section", undefined, "timeline");
  const head = el("div", undefined, "timeline-head");
  head.append(el("h3", "办理进度"));
  const done = steps.every(step => ["done", "na"].includes(step.state));
  let party = waitingParty[status];
  if (viewer === "warehouse") party = { 你: "客户", 仓库: "你" }[party] || party;
  if (!done && party && !["cancelled", "superseded"].includes(status)) {
    head.append(el("span", `等待：${party}`, `pill tone-${currentTone[status] || (party === "你" ? "info" : "wait")}`));
  }
  wrap.append(head);
  const list = el("ol", undefined, "timeline-steps");
  for (const step of steps) {
    const item = el("li", undefined, `step step-${step.state}`);
    if (step.tone) item.dataset.tone = step.tone;
    item.setAttribute("aria-current", step.state === "current" ? "step" : "false");
    item.append(el("span", undefined, "step-dot"));
    const body = el("div", undefined, "step-body");
    const title = el("div", undefined, "step-title");
    title.append(el("span", step.title), el("small", stepStateText[step.state]));
    body.append(title);
    if (step.detail) body.append(el("span", step.detail, "step-detail"));
    item.append(body);
    list.append(item);
  }
  wrap.append(list);
  return wrap;
}

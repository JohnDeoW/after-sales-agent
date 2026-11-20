// 页面统一使用的中文状态名和状态色；后端返回 status_label 时优先使用后端文本。
import { el } from "/static/dom.js";

const statusLabels = {
  // 案件办理阶段
  collecting_information: "待补信息", awaiting_confirmation: "待确认方案", confirmed: "已确认，待提交",
  ineligible: "暂不符合规则", policy_unavailable: "待人工核对政策",
  // 退货申请
  pending_approval: "待审批", awaiting_return: "待寄回", waiting_return: "待寄回", in_transit: "退货运输中", awaiting_inspection: "待检验",
  ready_for_refund: "待退款", refunded: "退款完成", refund_unknown: "退款待核对", refund_failed: "退款失败",
  approval_rejected: "审批拒绝", manual_review_required: "待人工处理", cancelled: "已取消", superseded: "已失效",
  // 换货申请
  ready_for_exchange: "待换新发货", replacement_in_transit: "换新运输中", completed: "换货完成",
  // 工单
  open: "待处理", in_progress: "处理中", replied: "已回复", resolved: "已解决",
};

const tones = {
  ok: ["refunded", "completed", "resolved", "replied"],
  danger: ["refund_failed", "approval_rejected", "ineligible"],
  human: ["manual_review_required", "policy_unavailable", "open", "in_progress"],
  stopped: ["cancelled", "superseded"],
  wait: ["pending_approval", "awaiting_return", "waiting_return","in_transit", "awaiting_inspection", "ready_for_exchange",
    "replacement_in_transit", "refund_unknown", "collecting_information", "awaiting_confirmation"],
};

export const statusLabel = status => statusLabels[status] || status || "未知状态";

export function statusTone(status) {
  for (const [tone, list] of Object.entries(tones)) if (list.includes(status)) return tone;
  return "info";
}

export function statusPill(status, label) {
  return el("span", label || statusLabel(status), `pill tone-${statusTone(status)}`);
}

export const roles = {
  customer: { label: "客户", list: "/demo/customers", login: "/sessions", key: "customer_id" },
  support: { label: "客服", list: "/demo/support-users", login: "/demo/support-sessions", key: "support_id" },
  warehouse: { label: "仓库", list: "/demo/warehouse-users", login: "/demo/warehouse-sessions", key: "warehouse_id" },
};

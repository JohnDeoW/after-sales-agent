// 只展示服务端案件快照；按钮携带所见方案版本，执行结果必须重新读取。
import { statusPill } from "/static/labels.js";
import { renderTimeline, returnSteps } from "/static/timeline.js";
function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function fact(list, label, value) {
  list.append(element("dt", label), element("dd", value === null || value === undefined || value === "" ? "待补充" : String(value)));
}

function button(text, action, secondary = false) {
  const node = element("button", text, secondary ? "secondary" : "");
  node.type = "button";
  node.addEventListener("click", action);
  return node;
}

const money = cents => `${(cents / 100).toFixed(2)} 元`;

export function renderCaseDetails(progress, onAction, onModify) {
  const root = document.querySelector("#case-details");
  root.replaceChildren();
  root.hidden = !progress;
  if (!progress) return;
  const proposal = progress.proposal;
  const request = progress.return_request;
  const details = proposal?.details;
  const draft = progress.state.draft;
  const actions = new Set(progress.available_actions);
  const title = element("h2", "方案与案件详情");
  title.id = "case-details-title";
  const heading = element("div", undefined, "detail-heading");
  heading.append(title, statusPill(progress.status, progress.status_label));
  root.append(heading, element("p", progress.case_id, "muted"));
  root.append(renderTimeline(returnSteps(progress), progress.status));

  const facts = element("dl", undefined, "detail-facts");
  fact(facts, "订单", details?.order_id ?? draft.order_id);
  fact(facts, "商品", details?.product_name ? `${details.product_name} · ${details.specification}` : draft.item);
  if (details) fact(facts, "商品项", details.order_item_id);
  fact(facts, "数量", proposal?.quantity ?? draft.quantity);
  fact(facts, "退货原因", proposal?.reason ?? draft.reason);
  const opened = details?.opened ?? draft.opened;
  fact(facts, "拆封情况", opened === true ? "已拆封" : opened === false ? "未拆封" : null);
  if (proposal) {
    fact(facts, "方案版本", `${proposal.id} · v${proposal.version}`);
    fact(facts, "预计商品退款", money(proposal.refund_amount_cents));
    fact(facts, "人工审批", proposal.requires_approval ? "需要客服审批" : "无需人工审批");
  }
  if (request) fact(facts, "申请编号", request.id);
  if (progress.refund_operation?.status === "succeeded") fact(facts, "已模拟退款", money(progress.refund_operation.amount_cents));
  root.append(facts);
  if (proposal) root.append(element("p", "金额不含运费；预计金额不表示已退款。", "muted"));
  root.append(element("p", progress.next_step, "next-step"));
  if (progress.approval) {
    const approved = progress.approval.decision === "approved";
    root.append(element("p", `审批结论：${approved ? "批准" : "拒绝"}${approved && !progress.approval_valid ? "（原授权已失效）" : ""}。${progress.approval.reason}`, "next-step"));
  }

  if (details) {
    const policy = element("details", undefined, "policy-details");
    policy.append(element("summary", "退回要求与政策依据（虚构演示政策）"));
    policy.append(element("p", details.return_requirements));
    const list = element("ul");
    for (const ref of details.policy_refs || []) list.append(element("li", `${ref.title} · ${ref.clause_id} / ${ref.version}`));
    policy.append(list);
    root.append(policy);
  }
  if (progress.shipment) {
    const shipping = progress.shipment.payload;
    root.append(element("p", `已登记退货：${shipping.carrier} · ${shipping.tracking_number}`, "shipping-record"));
  }

  const controls = element("fieldset", undefined, "detail-controls");
  controls.disabled = true; // 外层刷新完成后统一解除忙碌状态。
  const row = element("div", undefined, "action-row");
  if (actions.has("confirm_proposal") && proposal) {
    const acknowledgement = element("label", undefined, "acknowledgement");
    const check = element("input");
    check.type = "checkbox";
    acknowledgement.append(check, element("span", `我已核对 v${proposal.version} 的商品、数量、金额及退回要求`));
    const confirm = button("确认此版本方案", () => {
      if (check.checked) onAction(`/proposals/${encodeURIComponent(proposal.id)}/confirm`, { version: proposal.version });
    });
    confirm.disabled = true;
    check.addEventListener("change", () => { confirm.disabled = !check.checked; });
    controls.append(acknowledgement);
    row.append(confirm);
  }
  if (actions.has("submit_return") && progress.confirmation) {
    row.append(button("提交退货申请", () => onAction("/return-requests", { confirmation_id: progress.confirmation.id })));
  }
  if (actions.has("modify_proposal") || actions.has("provide_information")) {
    row.append(button(proposal ? "修改方案信息" : "补充信息", onModify, true));
  }
  if (actions.has("cancel_return")) {
    row.append(button("取消退货办理", () => proposal
      ? onAction(`/proposals/${encodeURIComponent(proposal.id)}/cancel`, { version: proposal.version })
      : onAction("/messages", { content: "取消退货" }), true));
  }
  if (request && actions.has("revalidate_request")) {
    row.append(button("重新核验申请", () => onAction(`/return-requests/${encodeURIComponent(request.id)}/revalidate`, {})));
  }
  if (request && actions.has("issue_refund")) {
    row.append(button("执行模拟退款", () => onAction(`/return-requests/${encodeURIComponent(request.id)}/refund`, { operation_key: `web-refund:${request.id}` })));
  }
  if (actions.has("check_refund_operation") && progress.refund_operation) {
    row.append(button("核对原退款结果", () => onAction(`/refund-operations/${encodeURIComponent(progress.refund_operation.id)}/check`, {})));
  }
  if (actions.has("contact_support")) row.append(button("转人工", () => onAction("/messages", { content: "转人工" }), true));
  controls.append(row);
  if (request && actions.has("submit_shipment")) {
    const form = element("form", undefined, "shipment-form");
    form.append(element("h3", "登记退货单号"), element("p", "请填写模拟物流信息，请勿实际寄件。", "muted"));
    for (const [name, labelText] of [["carrier", "承运商"], ["tracking_number", "退货单号"]]) {
      const label = element("label", labelText);
      const input = element("input");
      input.name = name;
      input.required = true;
      input.maxLength = 100;
      input.type = "text";
      label.append(input);
      form.append(label);
    }
    const submit = element("button", "提交退货单号");
    submit.type = "submit";
    form.append(submit);
    form.addEventListener("submit", event => {
      event.preventDefault();
      const carrier = form.elements.carrier.value.trim();
      const tracking_number = form.elements.tracking_number.value.trim();
      if (!carrier || !tracking_number) return;
      onAction(`/return-requests/${encodeURIComponent(request.id)}/shipment`, {
        operation_key: `web-shipment:${request.id}`, carrier, tracking_number,
      });
    });
    controls.append(form);
  }
  root.append(controls);
}

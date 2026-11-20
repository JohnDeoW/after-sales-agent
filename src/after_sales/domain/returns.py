"""虚构演示规则：退货资格与整数分退款预估，不执行退款。"""

from datetime import date

from after_sales.config import business_today
from after_sales.domain.policy_rules import PolicyUnavailable, return_policies, policy_references, references_text
from after_sales.domain.refund_amounts import calculate_refund


def evaluate_return(order: dict, item: dict, draft: dict, *, as_of: date | None = None) -> dict:
    """输入由后端校验归属的订单和商品；模型只能提供数量、原因与拆封声明。"""
    today = as_of or business_today()
    quantity = draft.get("quantity")
    remaining_quantity = item["quantity"] - item["returned_quantity"]
    assessment = {
        "decision": "ineligible", "code": "", "explanation": "",
        "order_id": order["id"], "order_item_id": item["id"],
        "product_name": item["product_name"], "specification": item["specification"],
        "quantity": quantity, "reason": draft.get("reason"),
        "refund_amount_cents": None, "currency": "CNY",
        "requires_approval": False, "approval_reasons": [],
        "remaining_quantity": remaining_quantity,
        "evaluated_on": today.isoformat(), "rule_version": None, "policy_refs": [], "policy_evidence": [],
    }

    def reject(code: str, explanation: str) -> dict:
        return {**assessment, "code": code, "explanation": explanation}

    quality = draft.get("reason_kind") == "quality"
    try:
        policies = return_policies(order, item, draft.get("reason_kind"))
    except PolicyUnavailable as exc:
        return {**assessment, "decision": "needs_policy", "code": "policy_unavailable", "explanation": str(exc)}
    window = policies["DEMO-QUALITY" if quality else "DEMO-RETURN-WINDOW"]["rules"]
    approval = policies["DEMO-APPROVAL"]["rules"]
    assessment.update(rule_version=policies["DEMO-QUALITY" if quality else "DEMO-RETURN-WINDOW"]["version"],
                      policy_refs=policy_references(policies), policy_evidence=list(policies.values()),
                      approval_threshold_cents=approval["threshold_cents"],
                      return_requirements=policies["DEMO-REFUND-CONDITIONS"]["body"])
    if order["status"] != window["required_order_status"]:
        return reject("not_delivered", "订单尚未处于已签收状态，不满足当前退货规则。")
    if not order["signed_on"]:
        return reject("missing_signed_date", "订单缺少签收日期，暂时无法核验退货期限。")
    days = (today - date.fromisoformat(order["signed_on"])).days
    if days < window["min_days"]:
        return reject("invalid_signed_date", "签收日期晚于当前业务日期，请先核对订单记录。")
    if remaining_quantity <= 0:
        return reject("no_remaining_quantity", "该商品项已无剩余可退数量。")
    if quantity is None or quantity <= 0 or quantity > remaining_quantity:
        return reject("invalid_quantity", f"退货数量必须在 1 至 {remaining_quantity} 件之间。")

    expired = days > window["max_days"] or (days == window["max_days"] and not window["max_days_inclusive"])
    if quality:
        if expired:
            return reject("quality_window_expired", f"签收已过去 {days} 天，超过适用政策的 {window['max_days']} 天质量问题审核期限。")
    else:
        if expired:
            return reject("ordinary_window_expired", f"签收已过去 {days} 天，超过适用普通退货政策的 {window['max_days']} 天期限。")
        if draft.get("opened") is not policies["opening"]["rules"]["required_opened"]:
            return reject("not_unopened", "普通退货要求商品未拆封，当前未满足这一条件。")

    # 优惠已在商品项实付中固化；仅算商品款，不把运费加入退款。
    breakdown = calculate_refund(item, quantity)
    amount = breakdown["refund_amount_cents"]
    if breakdown["available_amount_cents"] <= 0:
        return reject("no_remaining_amount", "该商品项已无剩余可退款金额，请人工核对后续退货处理。")
    approval_reasons = []
    if quality and approval["quality_requires_approval"]:
        approval_reasons.append(window["approval_reason"])
    if amount > approval["threshold_cents"]:
        approval_reasons.append("high_amount")
    return {
        **assessment,
        "decision": "manual_review" if quality else "eligible",
        "code": "quality_review_required" if quality else "ordinary_return_allowed",
        "explanation": "质量原因在审核期限内，需人工判断；尚未认定质量责任或批准退款。" if quality else
                       "订单已签收、在普通退货期限内且未拆封，数量符合要求。",
        "refund_amount_cents": amount,
        "amount_breakdown": breakdown,
        "requires_approval": bool(approval_reasons), "approval_reasons": approval_reasons,
    }


def assessment_reply(assessment: dict) -> str:
    """只把后端计算结果组织成回复，不让模型改写金额或办理状态。"""
    identity = f"订单 {assessment['order_id']}，{assessment['product_name']}（{assessment['specification']}）"
    if assessment["decision"] == "needs_policy":
        return f"{identity}：{assessment['explanation']}未生成有效方案或创建申请，请人工核对。"
    basis = "\n" + references_text(assessment["policy_refs"])
    if assessment["decision"] == "ineligible":
        return f"{identity}：暂不符合当前演示退货规则。{assessment['explanation']}尚未创建售后申请或退款。" + basis
    cents = assessment["refund_amount_cents"]
    text = (f"{identity}，退货 {assessment['quantity']} 件，原因：{assessment['reason']}。"
            f"预计商品退款 {cents // 100}.{cents % 100:02d} 元（不含运费）。{assessment['explanation']}")
    if "high_amount" in assessment["approval_reasons"]:
        threshold = assessment["approval_threshold_cents"]
        text += f"金额超过 {threshold // 100}.{threshold % 100:02d} 元，还需客服批准。"
    return text + "以上为预估，后续仍需方案确认及必要审批；当前尚未创建售后申请或退款。" + basis


def assessment_status(assessment: dict) -> str:
    return {"eligible": "eligibility_checked", "manual_review": "manual_review_required",
            "ineligible": "ineligible", "needs_policy": "policy_unavailable"}[assessment["decision"]]

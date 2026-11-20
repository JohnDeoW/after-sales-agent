"""为意图识别选择必要上下文；政策正文只在本轮解释节点提供。"""


def select(value: dict, keys: tuple) -> dict:
    return {key: value[key] for key in keys if key in value}


def build_context(context: dict, order: dict | None, proposal: dict | None) -> dict:
    state = context["state"]
    progress = context["progress"]
    compact_state = select(state, ("last_intent", "query_order_id", "logistics_order_id", "logistics_dispute", "exchange_request_id"))
    compact_state["draft"] = select(state["draft"],
                                  ("order_id", "order_item_id", "item", "quantity", "reason", "reason_kind", "opened"))
    compact_state["exchange"] = select(state.get("exchange", {}), ("order_id", "item", "quantity", "target_specification", "reason"))
    compact_state["service_request"] = select(state.get("service_request", {}), ("kind", "order_id", "item", "reason", "material_refs"))
    compact_state["cancelled"] = bool(progress and progress["status"] == "cancelled")
    if state.get("last_intent") == "policy":
        compact_state["policy"] = select(state.get("policy", {}),
                                        ("question", "order_id", "item", "sku", "ordered_on", "reason_kind"))
    current_proposal = None
    policy_refs = []
    if proposal:
        current_proposal = select(proposal, ("id", "version", "status", "order_item_id", "quantity", "reason",
                                             "refund_amount_cents", "requires_approval", "confirm_command", "cancel_command"))
        current_proposal["amount_meaning"] = "方案预估金额，不代表已退款"
        if proposal["status"] in ("pending_confirmation", "confirmed"):
            policy_refs = [select(p, ("clause_id", "version", "title"))
                           for p in proposal["details"].get("policy_refs", [])]
    current_order = None
    if order:
        current_order = select(order, ("id", "status", "ordered_on", "signed_on", "shipping_paid_cents"))
        current_order["items"] = [select(item, ("id", "sku", "product_name", "specification", "quantity",
                                               "paid_amount_cents", "returned_quantity", "refunded_amount_cents"))
                                  for item in order["items"]]
    # 摘要由当前结构化记录生成；不再次使用可能已过期的长回复和评估结果。
    compact_progress = None
    if progress:
        compact_progress = select(progress, ("status", "missing_fields", "confirmation", "return_request"))
        compact_progress["summary"] = f"案件 {context['case_id']}；当前阶段 {progress['status']}。"
        if progress["missing_fields"]:
            compact_progress["summary"] += "待补充：" + "、".join(progress["missing_fields"]) + "。"
    return {"task": {"case_id": context["case_id"], "previous_intent": state.get("last_intent", "unknown")},
            "state": compact_state, "case_progress": compact_progress,
            "order": current_order, "proposal": current_proposal, "policy_refs": policy_refs,
            "history": [{"role": m["role"], "content": m["content"][:1200]} for m in context["history"][-6:]]}

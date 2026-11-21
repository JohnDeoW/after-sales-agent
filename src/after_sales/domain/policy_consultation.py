"""咨询上下文独立于退货草稿；每轮涉及订单时重新核验归属。"""

from datetime import date

from after_sales.agent.policy import validate_answer
from after_sales.domain.intake_service import _result, _items_text


FIELD_LABELS = {"sku": "商品SKU或本人订单号", "ordered_on": "下单日期（不是签收日期）",
                "reason_kind": "普通退货还是质量问题"}


def _handoff_result(handoff: dict, evidence: dict | None = None) -> dict:
    text = handoff["message"] + "需要人工核对，未执行退款。"
    if handoff["policy_refs"]:
        text += "\n待核对条款：" + "；".join(p["chunk_id"] for p in handoff["policy_refs"])
    return _result("manual_review_required", text, handoff=handoff, policy_result=evidence)


def _handoff(context: dict, reason: str, message: str, evidence: dict | None = None) -> dict:
    evidence = evidence or {}
    clauses = evidence.get("policies", []) + [p for group in evidence.get("conflicts", []) for p in group]
    context["handoff"] = {
        "reason": reason, "message": message, "question": context["question"],
        "known_fields": {k: context[k] for k in ("order_id", "item", "sku", "ordered_on", "reason_kind") if k in context},
        "search_attempts": context.get("search_attempts", 0), "ticket_created": False,
        "evidence": evidence,
        "policy_refs": [{k: p[k] for k in ("chunk_id", "clause_id", "version", "source_sha256")} for p in clauses],
    }
    return _handoff_result(context["handoff"], evidence)


def _clarify(context: dict, text: str, fields: list[str], evidence: dict | None = None) -> dict:
    if context.get("clarification_used"):
        return _handoff(context, "insufficient_information", "补充后仍缺少确定适用政策所需的信息，已停止自动判断。", evidence)
    context["clarification_used"] = True
    return _result("clarification_needed", text + "补充后将再核查一次。", fields, policy_result=evidence)


def consult_policy(state: dict, update, content: str, get_order, search, explain) -> dict:
    context = state.setdefault("policy", {})
    question = update.policy_question or context.get("question") or content
    new_question = question != context.get("question")
    changed_fields = any(getattr(update, k) is not None and getattr(update, k) != context.get(k)
                         for k in ("order_id", "item", "sku", "ordered_on", "reason_kind")) or bool(update.clear_fields)
    if context.get("handoff") and not new_question and not changed_fields:
        return _handoff_result(context["handoff"])
    # 已结束的问题可在用户真正更换问题或补充新事实后重开；补充澄清不重置次数。
    if new_question or context.get("handoff"):
        for key in ("clarification_used", "search_attempts", "handoff"):
            context.pop(key, None)
    progress = {k: context[k] for k in ("clarification_used", "search_attempts") if k in context}
    # 提出问题的那条原话与检索问法并行检索；补充订单号等澄清消息不作为检索原话。
    question_message = content if new_question else context.get("question_message", content)
    # 新订单或独立SKU开启新的事实范围，不继承旧商品、日期及原因。
    if ((update.order_id is not None and update.order_id != context.get("order_id"))
            or (update.sku is not None and update.sku != context.get("sku"))
            or "order_id" in update.clear_fields):
        context.clear()
        context.update(progress)
    context["question"] = question
    context["question_message"] = question_message
    for field in update.clear_fields:
        context.pop(field, None)
        if field == "reason":
            context.pop("reason_kind", None)
    for field in ("order_id", "item", "sku", "ordered_on", "reason_kind"):
        value = getattr(update, field)
        if value is not None:
            context[field] = value
    if context.get("order_id"):
        lookup = get_order(context["order_id"])
        if lookup["status"] != "ok":
            result = _clarify(context, lookup["message"], ["order_id"])
            if result["status"] == "clarification_needed":
                result["status"] = "order_not_found"
            return result
        order = lookup["order"]
        items = order["items"]
        query = (context.get("item") or "").casefold().strip()
        matches = [i for i in items if query == i["id"].casefold()
                   or (query and query in (i["product_name"] + " " + i["specification"]).casefold())]
        item = matches[0] if len(matches) == 1 else (items[0] if not query and len(items) == 1 else None)
        if item is None:
            return _clarify(context, "请选择咨询的商品项：" + _items_text(items), ["item"])
        # 订单事实优先于模型/用户自述；不保存成退货草稿。
        context.update(sku=item["sku"], ordered_on=order["ordered_on"])
    if context.get("ordered_on"):
        try:
            context["ordered_on"] = date.fromisoformat(context["ordered_on"]).isoformat()
        except ValueError:
            context.pop("ordered_on")
            return _clarify(context, "请提供有效的下单日期，格式为YYYY-MM-DD。", ["ordered_on"])
    context["search_attempts"] = context.get("search_attempts", 0) + 1
    evidence = search({"question": question, "user_message": question_message,
                       **{key: context.get(key) for key in ("sku", "ordered_on", "reason_kind")}})
    if evidence["status"] == "conflict":
        return _handoff(context, "policy_conflict", "发现同等适用的条款存在冲突，不能自动选择某个版本。", evidence)
    if evidence["status"] == "clarification_needed":
        fields = evidence["missing_fields"]
        return _clarify(context, "为确定适用条款，请补充：" + "、".join(FIELD_LABELS[f] for f in fields) + "。", fields, evidence)
    if evidence["status"] != "ok":
        if evidence["status"] == "no_match" and not context.get("sku"):
            return _clarify(context, "未找到相关条款，请补充商品SKU或本人订单号。", ["sku"], evidence)
        return _handoff(context, evidence["status"], "未取得可用于回答的适用政策依据，已停止自动判断。", evidence)
    answer = validate_answer(explain(question, evidence["policies"]), evidence["policies"])
    if answer["status"] == "no_answer":
        return _handoff(context, "no_answer", "检索条款未覆盖所问事项，无法给出有依据的答案。", evidence)
    context.pop("clarification_used", None)
    context.pop("search_attempts", None)
    return _result("policy_answered", answer["text"] + "\n本次仅为咨询，未创建售后申请或执行退款。",
                   citations=answer["citations"], policy_result=evidence)

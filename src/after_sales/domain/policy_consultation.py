"""咨询上下文独立于退货草稿；每轮涉及订单时重新核验归属。"""

from datetime import date

from after_sales.agent.policy import validate_answer
from after_sales.domain.intake_service import _result, _items_text


FIELD_LABELS = {"sku": "商品SKU或本人订单号", "ordered_on": "下单日期（不是签收日期）",
                "reason_kind": "普通退货还是质量问题"}


def _handoff_result(handoff: dict, evidence: dict | None = None) -> dict:
    # 待核对的条款编号保存在工单里给客服，不放进给客户的回复。
    text = handoff["message"] + "已请人工客服核对，没有执行任何退款或办理。"
    return _result("manual_review_required", text, handoff=handoff, policy_result=evidence)


def _handoff(context: dict, reason: str, message: str, evidence: dict | None = None) -> dict:
    evidence = evidence or {}
    clauses = evidence.get("policies", []) + [p for group in evidence.get("conflicts", []) for p in group]
    context["handoff"] = {
        "reason": reason, "message": message, "question": context["question"],
        "known_fields": {k: context[k] for k in ("order_id", "item", "sku", "ordered_on", "reason_kind") if k in context},
        "search_attempts": context.get("search_attempts", 0), "requery": context.get("requery"), "ticket_created": False,
        "evidence": evidence,
        "policy_refs": [{k: p[k] for k in ("chunk_id", "clause_id", "version", "source_sha256")} for p in clauses],
    }
    return _handoff_result(context["handoff"], evidence)


def _clarify(context: dict, text: str, fields: list[str], evidence: dict | None = None) -> dict:
    if context.get("clarification_used"):
        return _handoff(context, "insufficient_information", "补充后仍不能确定适用哪条政策。", evidence)
    context["clarification_used"] = True
    return _result("clarification_needed", text + "补充后将再核查一次。", fields, policy_result=evidence)


def consult_policy(state: dict, update, content: str, get_order, search, explain, rewrite=None) -> dict:
    context = state.setdefault("policy", {})
    question = update.policy_question or context.get("question") or content
    new_question = question != context.get("question")
    changed_fields = any(getattr(update, k) is not None and getattr(update, k) != context.get(k)
                         for k in ("order_id", "item", "sku", "ordered_on", "reason_kind")) or bool(update.clear_fields)
    if context.get("handoff") and not new_question and not changed_fields:
        return _handoff_result(context["handoff"])
    # 已结束的问题可在用户真正更换问题或补充新事实后重开；补充澄清不重置次数。
    if new_question or context.get("handoff"):
        for key in ("clarification_used", "search_attempts", "handoff", "requery"):
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
    notice = ""
    if context.get("order_id"):
        lookup = get_order(context["order_id"])
        if lookup["status"] != "ok":
            # 订单号对不上时不中断咨询：说明没找到，先按通用规则回答，再请用户核对订单。
            notice = f"没有找到订单 {context.pop('order_id')}，以下先按通用规则说明。"
            context.pop("item", None)
    if context.get("order_id"):
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
    facts = {key: context.get(key) for key in ("sku", "ordered_on", "reason_kind")}
    evidence = search({"question": question, "user_message": question_message, **facts})
    # 首次没有可靠依据（重排得分低于阈值或无候选）时，由模型换一种问法再检索一次；同一问题只重查一次。
    if evidence["status"] == "no_match" and rewrite is not None and not context.get("requery"):
        requery = rewrite(question, question_message)
        context["requery"] = requery
        if requery and requery != question:
            evidence = search({"question": requery, "user_message": question_message, **facts})
    if evidence["status"] == "conflict":
        return _handoff(context, "policy_conflict", "这个问题适用的条款有不同版本，规定不一致，我不能自行选择。", evidence)
    if evidence["status"] == "clarification_needed":
        fields = evidence["missing_fields"]
        ask = "为确定适用条款，请补充：" + "、".join(FIELD_LABELS[f] for f in fields) + "。"
        general = evidence.get("general") or []
        if general and not context.get("clarification_used"):
            # 先依据通用候选条款说明一般规则，再追问具体信息；仍须引用本轮条款，不能编造。
            answer = validate_answer(explain(question, general), general)
            if answer["status"] == "answered":
                context["clarification_used"] = True
                return _result("policy_answered", notice + "一般规则：" + answer["text"] + "\n具体能否适用还要看你的订单，" + ask,
                               fields, citations=answer["citations"], policy_result=evidence)
        return _clarify(context, notice + ask, fields, evidence)
    if evidence["status"] != "ok":
        if notice and evidence["status"] == "no_match":
            return _clarify(context, notice.replace("以下先按通用规则说明。", "") + "请核对订单号，或直接告诉我是哪件商品。",
                            ["order_id"], evidence)
        if evidence["status"] == "no_match" and not context.get("sku"):
            return _clarify(context, "未找到相关条款，请补充商品SKU或本人订单号。", ["sku"], evidence)
        if evidence["status"] == "no_match":
            return _handoff(context, "no_match", "现有政策中没有找到能回答这个问题的条款"
                            + ("（已换一种问法重新检索）" if context.get("requery") else "")
                            + "，我不能凭推测回答。", evidence)
        return _handoff(context, evidence["status"], "暂时没有取得可用的政策依据，我不能凭推测回答。", evidence)
    answer = validate_answer(explain(question, evidence["policies"]), evidence["policies"])
    if answer["status"] == "no_answer":
        return _handoff(context, "no_answer", "现有政策里没有找到对这个问题的明确规定，我不能凭推测回答。", evidence)
    context.pop("clarification_used", None)
    context.pop("search_attempts", None)
    return _result("policy_answered", notice + answer["text"] + "\n本次仅为咨询，未创建售后申请或执行退款。",
                   citations=answer["citations"], policy_result=evidence)

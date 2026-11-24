"""退货信息收集、规则评估、补问与本轮保存。"""

import json

from langgraph.runtime import Runtime

from after_sales.domain.intake_service import _result, locate_items
from after_sales.domain.returns import evaluate_return, assessment_reply, assessment_status
from after_sales.storage.intake import save_turn
from after_sales.workflow.nodes.common import get_order
from after_sales.workflow.state import State, TurnContext


def locate_reply(purpose: str, description: str | None, hits: list[dict], matched: bool) -> str:
    if matched:
        return f"你的订单里有 {len(hits)} 件符合“{description}”的商品，请选择要{purpose}的是哪一件。"
    prefix = f"没有在你已签收的订单中找到“{description}”。" if description else "不记得订单号也没关系。"
    return prefix + f"请从下面选择要{purpose}的商品。"


def collect_information(s: State, runtime: Runtime[TurnContext]) -> dict:
    ctx = runtime.context
    state = s["context"]["state"]
    result, fields = ctx.deps.intake.collect_return(state["draft"], s["update"], lambda _: s["lookup"])
    if result["missing_fields"] == ["order_id"] and not state["draft"].get("order_id"):
        # 没有订单号：按描述在本人已签收订单中找商品，唯一命中直接继续，否则给出带订单的候选卡片。
        description = state["draft"].get("item")
        hits, matched = locate_items(ctx.deps.customers.list_orders(ctx.session_id), description)
        if matched and len(hits) == 1:
            located = s["update"].model_copy(update={"order_id": hits[0]["order_id"], "item": hits[0]["id"]})
            lookup = get_order(runtime, hits[0]["order_id"])
            result, fields = ctx.deps.intake.collect_return(state["draft"], located, lambda _: lookup)
        elif hits:
            result = _result("collecting_information", locate_reply("退货", description, hits, matched), ["item"], items=hits)
    state["assessment"] = None
    return {"result": result, "case_fields": fields,
            "next": "evaluate_rules" if result["status"] == "ready_for_evaluation" else "clarify"}


def evaluate_rules(s: State, runtime: Runtime[TurnContext]) -> dict:
    state = s["context"]["state"]
    assessment = evaluate_return(s["result"]["order"], s["result"]["item"], state["draft"])
    state["assessment"] = assessment
    result = _result(assessment_status(assessment), assessment_reply(assessment), assessment=assessment)
    if assessment["policy_evidence"]:
        # 资格、金额和审批要求已由规则服务确定，直接附上规则说明和所用条款，
        # 不再调用模型改写同一结论；想进一步了解条款含义时走政策咨询。
        result.update(policy_explanation=assessment["explanation"], citations=assessment["policy_evidence"])
    return {"result": result}


def clarify(s: State, runtime: Runtime[TurnContext]) -> dict:
    if s.get("result"):
        return {"result": s["result"]}
    intent, proposal = s["update"].intent, s["proposal"]
    if intent == "submit_return":
        text = "请先核对并确认方案；已确认的话点“提交退货申请”或回复“帮我提交”。"
    elif intent == "cancel_return":
        text = "如需结束本次办理，请明确回复“取消退货”。"
    elif intent == "confirm_return":
        text = ("请先在方案卡片上核对，确认的话点“确认此版本”或回复“确认”。当前尚未记录确认。"
                if proposal and proposal["status"] == "pending_confirmation" else "当前没有待确认的有效方案，请先补齐退货信息或查看最新方案状态。")
    elif intent == "issue":
        # 只描述了问题：先记下商品与问题，问用户想怎么处理，并给出处理方式选项。
        update = s["update"]
        issue = {k: getattr(update, k) for k in ("order_id", "item", "reason", "reason_kind", "opened") if getattr(update, k) is not None}
        s["context"]["state"]["issue"] = issue
        subject = "，".join(v for v in (update.item, update.reason) if v)
        text = (f"了解，{subject}。" if subject else "了解。") + "你希望怎么处理？可以退货、换货或维修，也可以转人工客服。"
        return {"result": _result("clarification_needed", text, proposal=proposal, choices=list(ISSUE_CHOICES))}
    else:
        text = ("我可以帮你办理退货、换货、维修，查询订单、物流和售后进度，也能解答售后政策。"
                "请说说遇到了什么问题，是哪件商品。")
    return {"result": _result("clarification_needed", text, proposal=proposal)}


ISSUE_CHOICES = ("退货", "换货", "维修", "转人工")


# 一件事办到这些状态即告一段落，可以提醒用户之前一并提到的其他诉求。
SETTLED = ("application_accepted", "awaiting_return", "ticket_created", "policy_answered", "logistics_found",
           "exchange_request_found", "investigation_created", "return_request_found", "case_found")


def pending_note(state: dict, status: str | None) -> str:
    """本轮首次提到的其他诉求先说明会稍后处理；当前这件告一段落时提醒一次。"""
    mentioned = state.pop("mention_pending", None)
    if mentioned:
        return "\n你还提到：" + "；".join(mentioned) + "。这件办完后告诉我，我接着帮你处理。"
    if state.get("pending_requests") and status in SETTLED and not state.get("pending_reminded"):
        state["pending_reminded"] = True
        return "\n另外，你之前还提到：" + "；".join(state["pending_requests"]) + "。需要现在办理的话直接告诉我。"
    return ""


def persist_turn(s: State, runtime: Runtime[TurnContext]) -> dict:
    """在一个业务事务中保存消息、草稿、方案与工单；补问轮次超出预算时转人工。"""
    ctx = runtime.context
    result = {**s["result"], "intent": s["update"].intent}
    state = s["context"]["state"]
    result["reply_text"] = result.get("reply_text", "") + pending_note(state, result.get("status"))
    if result["status"] in ("collecting_information", "clarification_needed"):
        identity = json.dumps({"intent": result["intent"], "missing": result["missing_fields"],
            "draft": state["draft"], "exchange": state.get("exchange"),
            "service": {k: v for k, v in state.get("service_request", {}).items() if k not in ("original_message", "statement_message")},
            "logistics_order_id": state.get("logistics_order_id")}, ensure_ascii=False, sort_keys=True)
        previous = state.get("clarification_budget", {})
        count = previous.get("count", 0) + 1 if previous.get("identity") == identity else 1
        offered = previous.get("offered", False) if previous.get("identity") == identity else False
        state["clarification_budget"] = {"identity": identity, "count": count, "offered": offered}
        last_reply = next((m["content"] for m in reversed(s["context"]["history"]) if m["role"] == "assistant"), None)
        if last_reply and result["reply_text"].strip() == last_reply.strip():
            # 同一句补问不原样重复，换个说法并说明还差什么。
            result["reply_text"] = "还差一点信息才能继续：" + result["reply_text"]
        if count >= ctx.deps.limits.max_clarification_rounds:
            if not offered:
                # 补问到上限前先给用户选择，而不是直接停止办理转人工。
                state["clarification_budget"]["offered"] = True
                result["reply_text"] += "\n如果一时说不清，也可以回复“转人工”，由客服帮你核对。"
            else:
                result["failure_handoff"] = {"reason_code": "clarification_budget", "workflow": ctx.trace.view("stopped")}
    else:
        state.pop("clarification_budget", None)
    response = save_turn(ctx.deps.customers.db_path, ctx.session_id, ctx.user["id"], ctx.display or ctx.content,
                         s["context"], result, s["case_fields"])
    return {"result": response, "response": response, "conversation": state}


def reply(s: State, runtime: Runtime[TurnContext]) -> dict:
    return {"result": s["result"], "conversation": s["context"]["state"]}

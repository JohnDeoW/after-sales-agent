"""退货信息收集、规则评估、补问与本轮保存。"""

import json

from langgraph.runtime import Runtime

from after_sales.domain.intake_service import _result
from after_sales.domain.returns import evaluate_return, assessment_reply, assessment_status
from after_sales.storage.intake import save_turn
from after_sales.workflow.state import State, TurnContext


def collect_information(s: State, runtime: Runtime[TurnContext]) -> dict:
    state = s["context"]["state"]
    result, fields = runtime.context.deps.intake.collect_return(state["draft"], s["update"], lambda _: s["lookup"])
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
        text = "请使用确认回复中的“提交申请 CONF-…”完整指令提交；尚未确认时请先核对并确认方案。"
    elif intent == "cancel_return":
        text = "如需结束本次办理，请明确回复“取消退货”。"
    elif intent == "confirm_return":
        text = (f"请核对具体方案，确认后回复“{proposal['confirm_command']}”。当前尚未记录确认。"
                if proposal and proposal["status"] == "pending_confirmation" else "当前没有待确认的有效方案，请先补齐退货信息或查看最新方案状态。")
    else:
        text = "请说明要查询订单还是办理退货；也可以直接提供订单号和售后诉求。"
    return {"result": _result("clarification_needed", text, proposal=proposal)}


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
        state["clarification_budget"] = {"identity": identity, "count": count}
        if count >= ctx.deps.limits.max_clarification_rounds:
            result["failure_handoff"] = {"reason_code": "clarification_budget", "workflow": ctx.trace.view("stopped")}
    else:
        state.pop("clarification_budget", None)
    response = save_turn(ctx.deps.customers.db_path, ctx.session_id, ctx.user["id"], ctx.content,
                         s["context"], result, s["case_fields"])
    return {"result": response, "response": response, "conversation": state}


def reply(s: State, runtime: Runtime[TurnContext]) -> dict:
    return {"result": s["result"], "conversation": s["context"]["state"]}

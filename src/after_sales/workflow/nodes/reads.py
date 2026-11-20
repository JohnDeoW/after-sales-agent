"""只读查询：选择工具 → 执行（只读工具白名单）→ 会话记账 → 生成答复。

两条路径共用同一套记账与答复：
- 确定性路径（select_read_tool / read_tool）：页面按钮的固定指令与退货办理所需的订单查询；
- Agent 路径（query_agent）：自然语言的订单、物流、申请、案件、工单查询，由 create_agent 子图自主选择工具。
choose_tool 是按意图与已知编号选择工具的规则，既用于确定性路径，也作为离线回放和测试中的替身选择模型。
"""

from langgraph.runtime import Runtime

from after_sales.domain.exchanges import exchange_text
from after_sales.domain.intake_service import _result, _items_text
from after_sales.domain.logistics import logistics_text
from after_sales.domain.return_requests import request_text
from after_sales.domain.tickets import ticket_text
from after_sales.tools.registry import READ_ONLY_TOOLS
from after_sales.workflow.nodes.common import read
from after_sales.workflow.state import State, TurnContext

QUERY_INTENTS = ("order_query", "return_query", "exchange_query", "case_query", "ticket_query", "logistics")


def query_facts(update, state: dict, case_id: str | None) -> dict:
    """给工具选择（模型或规则）的输入：本轮识别出的编号与会话中已知的编号，不含订单内容或用户隐私。"""
    extracted = {key: getattr(update, key) for key in ("order_id", "request_id", "case_id", "ticket_id") if getattr(update, key)}
    known = {"query_order_id": state.get("query_order_id"), "logistics_order_id": state.get("logistics_order_id"),
             "return_request_id": state.get("return_request_id"), "exchange_request_id": state.get("exchange_request_id"),
             "draft_order_id": state["draft"].get("order_id"), "current_case_id": case_id}
    return {"intent": update.intent, "extracted": extracted, "clear_fields": list(update.clear_fields),
            "known": {key: value for key, value in known.items() if value}}


def choose_tool(facts: dict) -> tuple[str, dict]:
    """按意图与编号选择一个只读工具；没有编号时选择对应的列表工具。"""
    intent, extracted, known = facts["intent"], facts["extracted"], facts["known"]
    cleared = "order_id" in facts["clear_fields"]
    if intent == "exchange_query":
        request_id = extracted.get("request_id") or known.get("exchange_request_id")
        return ("get_exchange_request", {"request_id": request_id}) if request_id else ("list_exchange_requests", {})
    if intent == "logistics":
        order_id = extracted.get("order_id") or (None if cleared else known.get("logistics_order_id"))
        return ("get_logistics", {"order_id": order_id}) if order_id else ("list_orders", {})
    if intent == "ticket_query":
        return ("get_ticket", {"ticket_id": extracted["ticket_id"]}) if extracted.get("ticket_id") else ("list_tickets", {})
    if intent == "case_query" or (intent == "return_query" and not extracted.get("request_id")
                                  and not known.get("return_request_id") and known.get("current_case_id")):
        case_id = extracted.get("case_id") or known.get("current_case_id")
        return ("get_case", {"case_id": case_id}) if case_id else ("list_cases", {})
    if intent == "return_query":
        request_id = extracted.get("request_id") or known.get("return_request_id")
        return ("get_return_request", {"request_id": request_id}) if request_id else ("list_return_requests", {})
    previous = known.get("draft_order_id") if intent == "return" else known.get("query_order_id")
    order_id = extracted.get("order_id") or (None if cleared else previous)
    if order_id:
        return "get_order", {"order_id": order_id}
    return ("list_orders", {}) if intent == "order_query" else ("none", {})


def apply_lookup(state: dict, update, name: str, args: dict, content: str) -> None:
    """把本次查询的对象记入会话草稿，供下一轮追问沿用。"""
    if update.intent == "logistics" and name in ("get_logistics", "list_orders"):
        order_id = args.get("order_id")
        if "order_id" in update.clear_fields or (state.get("logistics_order_id") and order_id != state["logistics_order_id"]):
            state.pop("logistics_dispute", None)
        if update.logistics_dispute:
            dispute = state.setdefault("logistics_dispute", {})
            if update.reason:
                dispute.update(statement=update.reason, original_message=content)
        state.update(logistics_order_id=order_id, last_intent="logistics")
    if update.intent == "order_query" and name in ("get_order", "list_orders"):
        state["query_order_id"] = args.get("order_id")


# ---- 答复映射表：工具名 -> 由查询结果生成答复 ----

def _ids(rows, key="id"):
    return "、".join(row[key] for row in rows)


def _logistics_result(value, state):
    if value["status"] != "ok":
        return _result("order_not_found", value["message"], ["order_id"])
    result = _result("logistics_found", logistics_text(value["logistics"]), logistics=value["logistics"])
    if "logistics_dispute" in state:
        dispute = state["logistics_dispute"]
        if value["logistics"]["status"] not in ("delivered", "lost_pending"):
            result["reply_text"] += "当前记录不属于签收争议或丢件待查，尚未创建调查工单。"
        elif not dispute.get("statement"):
            result.update(status="collecting_information", missing_fields=["reason"])
            result["reply_text"] += "请说明未收到的情况、是否联系承运商等信息，以便创建调查工单。"
        else:
            result["logistics_handoff"] = {"order_id": value["logistics"]["order_id"], **dispute}
    elif value["logistics"]["status"] == "lost_pending":
        result["reply_text"] += "如需调查，请说明未收到的情况并提出调查请求。"
    return result


def _order_result(value, state):
    if value["status"] != "ok":
        return _result("order_not_found", value["message"], ["order_id"])
    order = value["order"]
    return _result("order_found", f"订单 {order['id']}，状态 {order['status']}，签收日期 {order['signed_on']}。"
                                  f"商品：{_items_text(order['items'])}。", order=order)


RENDERERS = {
    "get_order": _order_result,
    "list_orders": lambda value, state: _result("collecting_information", "请提供要查询的订单号。本人订单：" + _ids(value),
                                                ["order_id"], orders=value),
    "get_logistics": _logistics_result,
    "get_exchange_request": lambda value, state: _result("exchange_request_found", exchange_text(value), exchange_request=value),
    "list_exchange_requests": lambda value, state: _result(
        "clarification_needed", "请指定换货申请编号：" + _ids(value) if value else "当前没有换货申请。", exchange_requests=value),
    "get_ticket": lambda value, state: _result("ticket_found", ticket_text(value), ticket=value),
    "list_tickets": lambda value, state: _result(
        "clarification_needed", "请指定工单编号：" + _ids(value) if value else "当前没有人工工单。", tickets=value),
    "get_case": lambda value, state: _result("case_found", value["summary"], case_progress=value),
    "list_cases": lambda value, state: _result(
        "clarification_needed", "请指定要查询的案件编号：" + _ids(value, "case_id") if value
        else "当前没有售后案件，可以先提供订单号和售后诉求。", cases=value),
    "get_return_request": lambda value, state: _result("return_request_found", request_text(value), return_request=value),
    "list_return_requests": lambda value, state: _result(
        "clarification_needed", "请提供要查询的申请编号。本人申请：" + _ids(value) if value
        else "当前没有已受理的退货申请；如已确认方案，请先按提示提交申请。", return_requests=value),
}


def render(lookups: list[tuple[str, dict, object]], state: dict) -> dict:
    """一个或多个查询结果合并为一轮答复：首个结果决定状态，其余结果追加文字与数据。"""
    results = [RENDERERS[name](value, state) for name, _, value in lookups]
    combined = results[0]
    for extra in results[1:]:
        combined["reply_text"] += "\n" + extra["reply_text"]
        for key, value in extra.items():
            if key not in ("status", "reply_text", "missing_fields"):
                combined.setdefault(key, value)
    return combined


# ---- 确定性路径 ----

def select_read_tool(s: State, runtime: Runtime[TurnContext]) -> dict:
    name, args = choose_tool(query_facts(s["update"], s["context"]["state"], s["context"]["case_id"]))
    apply_lookup(s["context"]["state"], s["update"], name, args, runtime.context.content)
    return {"tool_name": name, "tool_args": args}


def read_tool(s: State, runtime: Runtime[TurnContext]) -> dict:
    name = s["tool_name"]
    value = {} if name == "none" else read(name, READ_ONLY_TOOLS[name], s["tool_args"])
    if s["update"].intent == "return":
        return {"lookup": value, "next": "collect_information"}
    return {"result": render([(name, s["tool_args"], value)], s["context"]["state"]), "next": "persist_turn"}


# ---- Agent 路径 ----

NO_TOOL_REPLY = "没有找到可以直接查询的对象，请提供订单号、申请编号（RET-/EXC-）、案件编号或工单编号。"
LIMIT_REPLY = "本轮查询请求超出调用上限，已停止多余的查询，以下只列出已完成的结果。"
INVALID_REPLY = "查询参数不符合要求，已停止本次查询；只能查询本人的记录，请提供正确的订单号或编号。"


def query_agent(s: State, runtime: Runtime[TurnContext]) -> dict:
    """create_agent 子图在只读工具白名单内选择工具；结果仍由答复映射表生成，不采用模型自由文本。"""
    ctx = runtime.context
    state = s["context"]["state"]
    facts = query_facts(s["update"], state, s["context"]["case_id"])
    outcome = ctx.deps.query_agent.run(ctx.content, facts)
    lookups = outcome["lookups"]
    for name, args, _ in lookups:
        apply_lookup(state, s["update"], name, args, ctx.content)
    if not lookups:
        if outcome["invalid"]:
            text = INVALID_REPLY
        elif outcome["limited"]:
            text = "本轮查询请求超出调用上限，已停止。请分开询问或提供具体编号。"
        else:
            text = NO_TOOL_REPLY
        return {"result": _result("clarification_needed", text)}
    result = render(lookups, state)
    if outcome["limited"]:
        result["reply_text"] = LIMIT_REPLY + "\n" + result["reply_text"]
    return {"result": result}

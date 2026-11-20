"""只读查询：先按意图选择工具，再用映射表执行查询并生成答复。"""

from langgraph.runtime import Runtime

from after_sales.domain.exchanges import exchange_text
from after_sales.domain.intake_service import _result, _items_text
from after_sales.domain.logistics import logistics_text
from after_sales.domain.return_requests import request_text
from after_sales.domain.tickets import ticket_text
from after_sales.tools.logistics import create_get_logistics_tool
from after_sales.workflow.nodes.common import get_order, read, read_service
from after_sales.workflow.state import State, TurnContext


def select_read_tool(s: State, runtime: Runtime[TurnContext]) -> dict:
    update = s["update"]
    state = s["context"]["state"]
    if update.intent == "exchange_query":
        request_id = update.request_id or state.get("exchange_request_id")
        return {"tool_name": "get_exchange_request" if request_id else "list_exchange_requests",
                "tool_args": {"request_id": request_id} if request_id else {}}
    if update.intent == "logistics":
        order_id = update.order_id or (None if "order_id" in update.clear_fields else state.get("logistics_order_id"))
        if "order_id" in update.clear_fields or (state.get("logistics_order_id") and order_id != state["logistics_order_id"]):
            state.pop("logistics_dispute", None)
        if update.logistics_dispute:
            dispute = state.setdefault("logistics_dispute", {})
            if update.reason:
                dispute.update(statement=update.reason, original_message=runtime.context.content)
        state.update(logistics_order_id=order_id, last_intent="logistics")
        return {"tool_name": "get_logistics" if order_id else "list_orders",
                "tool_args": {"order_id": order_id} if order_id else {}}
    if update.intent == "ticket_query":
        return {"tool_name": "get_ticket" if update.ticket_id else "list_tickets",
                "tool_args": {"ticket_id": update.ticket_id} if update.ticket_id else {}}
    if update.intent == "case_query" or (update.intent == "return_query" and not update.request_id
                                         and not state.get("return_request_id") and s["context"]["case_id"]):
        case_id = update.case_id or s["context"]["case_id"]
        return {"tool_name": "get_case" if case_id else "list_cases",
                "tool_args": {"case_id": case_id} if case_id else {}}
    if update.intent == "return_query":
        request_id = update.request_id or state.get("return_request_id")
        return {"tool_name": "get_return_request" if request_id else "list_return_requests",
                "tool_args": {"request_id": request_id} if request_id else {}}
    previous = state["draft"].get("order_id") if update.intent == "return" else state.get("query_order_id")
    order_id = update.order_id or (None if "order_id" in update.clear_fields else previous)
    if update.intent == "order_query":
        if "order_id" in update.clear_fields:
            order_id = None
        state["query_order_id"] = order_id
    return {"tool_name": "get_order" if order_id else ("list_orders" if update.intent == "order_query" else "none"),
            "tool_args": {"order_id": order_id} if order_id else {}}


# ---- 查询映射表：工具名 -> (执行查询, 生成答复) ----

def _ids(rows, key="id"):
    return "、".join(row[key] for row in rows)


def _logistics_result(value, s, ctx):
    if value["status"] != "ok":
        return _result("order_not_found", value["message"], ["order_id"])
    result = _result("logistics_found", logistics_text(value["logistics"]), logistics=value["logistics"])
    state = s["context"]["state"]
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


def _order_result(value, s, ctx):
    if value["status"] != "ok":
        return _result("order_not_found", value["message"], ["order_id"])
    order = value["order"]
    return _result("order_found", f"订单 {order['id']}，状态 {order['status']}，签收日期 {order['signed_on']}。"
                                  f"商品：{_items_text(order['items'])}。", order=order)


READ_TOOLS = {
    "get_order": (
        lambda ctx, args, rt: get_order(rt, args["order_id"]),
        _order_result),
    "none": (lambda ctx, args, rt: {}, None),
    "list_orders": (
        lambda ctx, args, rt: read_service("list_orders", lambda: ctx.deps.customers.list_orders(ctx.session_id), args),
        lambda value, s, ctx: _result("collecting_information", "请提供要查询的订单号。本人订单：" + _ids(value),
                                      ["order_id"], orders=value)),
    "get_logistics": (
        lambda ctx, args, rt: read("get_logistics", create_get_logistics_tool(ctx.deps.customers, ctx.session_id), args),
        _logistics_result),
    "get_exchange_request": (
        lambda ctx, args, rt: read_service("get_exchange_request", lambda: ctx.deps.exchanges.read(ctx.session_id, args["request_id"]), args),
        lambda value, s, ctx: _result("exchange_request_found", exchange_text(value), exchange_request=value)),
    "list_exchange_requests": (
        lambda ctx, args, rt: read_service("list_exchange_requests", lambda: ctx.deps.exchanges.read(ctx.session_id), args),
        lambda value, s, ctx: _result("clarification_needed", "请指定换货申请编号：" + _ids(value) if value else "当前没有换货申请。",
                                      exchange_requests=value)),
    "get_ticket": (
        lambda ctx, args, rt: read_service("get_ticket", lambda: ctx.deps.tickets.read(ctx.session_id, args["ticket_id"]), args),
        lambda value, s, ctx: _result("ticket_found", ticket_text(value), ticket=value)),
    "list_tickets": (
        lambda ctx, args, rt: read_service("list_tickets", lambda: ctx.deps.tickets.read(ctx.session_id), args),
        lambda value, s, ctx: _result("clarification_needed", "请指定工单编号：" + _ids(value) if value else "当前没有人工工单。",
                                      tickets=value)),
    "get_case": (
        lambda ctx, args, rt: read_service("get_case", lambda: ctx.deps.cases.get(ctx.session_id, args["case_id"]), args),
        lambda value, s, ctx: _result("case_found", value["summary"], case_progress=value)),
    "list_cases": (
        lambda ctx, args, rt: read_service("list_cases", lambda: ctx.deps.cases.list_cases(ctx.session_id), args),
        lambda value, s, ctx: _result("clarification_needed",
                                      "请指定要查询的案件编号：" + _ids(value, "case_id") if value
                                      else "当前没有售后案件，可以先提供订单号和售后诉求。", cases=value)),
    "get_return_request": (
        lambda ctx, args, rt: read_service("get_return_request", lambda: ctx.deps.requests.get_request(ctx.session_id, args["request_id"]), args),
        lambda value, s, ctx: _result("return_request_found", request_text(value), return_request=value)),
    "list_return_requests": (
        lambda ctx, args, rt: read_service("list_return_requests", lambda: ctx.deps.requests.list_requests(ctx.session_id), args),
        lambda value, s, ctx: _result("clarification_needed",
                                      "请提供要查询的申请编号。本人申请：" + _ids(value) if value
                                      else "当前没有已受理的退货申请；如已确认方案，请先按提示提交申请。", return_requests=value)),
}


def read_tool(s: State, runtime: Runtime[TurnContext]) -> dict:
    fetch, render = READ_TOOLS[s["tool_name"]]
    value = fetch(runtime.context, s["tool_args"], runtime)
    if s["update"].intent == "return":
        return {"lookup": value, "next": "collect_information"}
    return {"result": render(value, s, runtime.context), "next": "persist_turn"}

"""每条消息运行一次图；需要用户输入时结束，由 SQLite 草稿承接下一轮。"""

import re
import json
from typing import TypedDict

from langgraph.graph import StateGraph, START, END
from langgraph.errors import GraphRecursionError
from pydantic import ValidationError

from after_sales.agent.intake import IntakeUpdate, IntakeUnavailable
from after_sales.domain.intake_service import ReturnIntake, _result, _items_text, match_order_item
from after_sales.domain.handoffs import collect_handoff
from after_sales.domain.proposals import ProposalService, ProposalNotFound
from after_sales.domain.return_requests import ReturnRequestService, request_text
from after_sales.domain.cases import CaseService
from after_sales.domain.tickets import TicketService, ticket_text
from after_sales.domain.logistics import logistics_text
from after_sales.domain.exchanges import collect_exchange, ExchangeService, exchange_text
from after_sales.tools.inventory import create_get_inventory_tool
from after_sales.tools.logistics import create_get_logistics_tool
from after_sales.domain.returns import evaluate_return, assessment_reply, assessment_status
from after_sales.agent.context import build_context
from after_sales.domain.policy_consultation import consult_policy
from after_sales.storage.intake import load_context, save_turn
from after_sales.storage.workflow import save_trace
from after_sales.tools.orders import create_get_order_tool
from after_sales.tools.policies import create_search_policy_tool
from after_sales.retrieval.index import INDEX_PATH
from after_sales.workflow.runtime import Limits, Run, WorkflowStopped, ReadQueryUnavailable, WriteOutcomeUnknown, active_run


class State(TypedDict, total=False):
    session_id: str
    content: str
    user: dict
    run: Run
    context: dict
    proposal: dict | None
    current_order: dict | None
    update: IntakeUpdate
    next: str
    command: dict
    tool_name: str
    tool_args: dict
    lookup: dict | list
    result: dict
    case_fields: dict | None


class AfterSalesWorkflow:
    def __init__(self, customers, extractor, limits: Limits | None = None, *, policy_explainer=None,
                 policy_index_path=INDEX_PATH):
        self.customers = customers
        self.extractor = extractor
        self.limits = limits or Limits()
        self.proposals = ProposalService(customers)
        self.requests = ReturnRequestService(customers)
        self.cases = CaseService(customers)
        self.tickets = TicketService(customers)
        self.exchanges = ExchangeService(customers)
        self.intake = ReturnIntake()
        self.policy_explainer = policy_explainer
        self.policy_tool = create_search_policy_tool(policy_index_path)
        self.graph = self._build()

    def _build(self):
        builder = StateGraph(State)
        nodes = {"load_context": self._load, "route_command": self._route_command,
                 "prepare_context": self._prepare_context, "recognize": self._recognize,
                 "select_read_tool": self._select_read_tool, "read_tool": self._read_tool,
                 "collect_information": self._collect, "evaluate_rules": self._evaluate,
                 "proposal": self._persist, "clarify": self._clarify, "persist_turn": self._persist,
                 "confirm": self._confirm, "cancel": self._cancel, "create_request": self._create,
                 "policy_consultation": self._policy, "exchange": self._exchange,
                 "confirm_exchange": self._confirm_exchange, "reply": self._reply}
        nodes["service_handoff"] = self._service_handoff
        for name, function in nodes.items():
            builder.add_node(name, self._node(name, function))
        builder.add_edge(START, "load_context")
        builder.add_edge("load_context", "route_command")
        builder.add_conditional_edges("route_command", lambda s: s["next"],
                                      ["prepare_context", "select_read_tool", "confirm", "cancel", "create_request", "confirm_exchange", "service_handoff", "exchange"])
        builder.add_edge("prepare_context", "recognize")
        builder.add_conditional_edges("recognize", lambda s: s["next"], ["select_read_tool", "clarify", "policy_consultation", "exchange", "service_handoff", "confirm", "cancel", "confirm_exchange"])
        builder.add_edge("service_handoff", "persist_turn")
        builder.add_edge("exchange", "persist_turn")
        builder.add_edge("policy_consultation", "persist_turn")
        builder.add_edge("select_read_tool", "read_tool")
        builder.add_conditional_edges("read_tool", lambda s: s["next"], ["collect_information", "persist_turn"])
        builder.add_conditional_edges("collect_information", lambda s: s["next"], ["evaluate_rules", "clarify"])
        builder.add_edge("evaluate_rules", "proposal")
        builder.add_edge("clarify", "persist_turn")
        for name in ("proposal", "persist_turn", "confirm", "cancel", "create_request", "confirm_exchange"):
            builder.add_edge(name, "reply")
        builder.add_edge("reply", END)
        return builder.compile()

    @staticmethod
    def _node(name, function):
        def execute(state):
            return state["run"].call("node", name, lambda: function(state))
        return execute

    def receive(self, session_id: str, content: str, *, command: dict | None = None, emit=None) -> dict:
        token = active_run.set(None)
        try:
            return self._receive(session_id, content, command=command, emit=emit)
        finally:
            active_run.reset(token)

    def _receive(self, session_id, content, *, command=None, emit=None):
        user = self.customers.current_user(session_id)
        run = Run(self.limits, emit=emit)
        active_run.set(run)
        try:
            self.graph.invoke({"session_id": session_id, "content": content, "user": user, "run": run,
                               "case_fields": None, "command": command or {}}, {"recursion_limit": self.limits.max_steps})
        except Exception as exc:
            # 只保存错误类型；业务异常由现有 HTTP 处理器返回，不吞掉未知错误。
            if isinstance(exc, GraphRecursionError):
                exc = WorkflowStopped("本轮工作流已达到步骤预算，已停止，未完成本轮办理。")
            if run.response is not None and isinstance(exc, WorkflowStopped):
                # 数据已提交时如实返回原结果，不能因回复节点耗尽预算声称未执行。
                run.response["workflow_notice"] = "业务结果已保存，后续轨迹步骤因预算停止。"
            elif isinstance(exc, (WorkflowStopped, ReadQueryUnavailable, WriteOutcomeUnknown)):
                context = load_context(self.customers.db_path, session_id)
                failure = {"reason_code": type(exc).__name__, "workflow": run.view("stopped")}
                if isinstance(exc, WriteOutcomeUnknown):
                    # 用原确认/方案编号查询已提交记录；这不是重发写操作。
                    args = exc.args_summary
                    if exc.name == "create_return_request":
                        record = next((r for r in self.requests.list_requests(session_id) if r["confirmation_id"] == args["confirmation_id"]), None)
                    elif exc.name == "create_exchange_request":
                        record = next((r for r in self.exchanges.read(session_id) if r["proposal_id"] == args["proposal_id"]), None)
                    else:
                        record = self.proposals.current(session_id)["proposal"]
                        if record and args.get("proposal_id") and (record["id"], record["version"]) != (args["proposal_id"], args["version"]):
                            record = None
                    failure["write_check"] = {"tool": exc.name, "original_args": args, "record": record}
                result = _result("workflow_handoff", "", failure_handoff=failure)
                run.response = save_turn(self.customers.db_path, session_id, user["id"], content, context, result, None)
            else:
                trace = run.view("stopped" if isinstance(exc, WorkflowStopped) else "error")
                trace["error_type"] = type(exc).__name__
                save_trace(self.customers.db_path, session_id, trace)
                exc.workflow = trace
                raise exc
        trace = run.view(run.response["status"])
        save_trace(self.customers.db_path, session_id, trace)
        return {**run.response, "workflow": trace}

    def _load(self, s):
        return {"context": load_context(self.customers.db_path, s["session_id"]),
                "proposal": self.proposals.current(s["session_id"])["proposal"]}

    def _route_command(self, s):
        # HTTP 操作路由传入结构化指令，非法编号也交给业务校验，不能退回模型猜测。
        if s["command"]:
            command = dict(s["command"])
            return {"next": command.pop("action"), "command": command}
        text = s["content"].strip()
        if text.rstrip("。！!") in ("转人工", "找人工", "人工客服", "我要人工客服"):
            return {"next": "service_handoff", "update": IntakeUpdate(intent="human")}
        exchange_confirm = re.fullmatch(r"确认换货\s+(EXPROP-[A-Za-z0-9-]+)\s+[vV](\d+)[。！!]?", text)
        if exchange_confirm:
            return {"next": "confirm_exchange", "command": {"proposal_id": exchange_confirm[1], "version": int(exchange_confirm[2])}}
        state = s["context"]["state"]
        # 页面卡片的点击选择：只沿用当前退货/换货诉求，商品项和规格仍由后续节点按订单与库存核对。
        choice = re.fullmatch(r"选择(商品|规格)\s+([A-Za-z0-9-]+)", text)
        if choice and state.get("last_intent") in ("return", "exchange"):
            intent = state["last_intent"]
            if choice[1] == "商品":
                update = IntakeUpdate(intent=intent, item=choice[2])
            elif intent == "exchange":
                update = IntakeUpdate(intent="exchange", target_specification=choice[2])
            else:
                return {"next": "prepare_context"}
            state["last_intent"] = intent
            return {"next": "exchange" if intent == "exchange" else "select_read_tool", "update": update}
        if text.rstrip("。！!") == "确认换货":
            pending = self._pending_exchange(s)
            if pending:
                return {"next": "confirm_exchange", "command": {"proposal_id": pending["id"], "version": pending["version"]}}
        exchange_query = re.fullmatch(r"查询换货\s+(EXC-[A-Za-z0-9-]+)[。！!]?", text)
        if exchange_query:
            return {"next": "select_read_tool", "update": IntakeUpdate(intent="exchange_query", request_id=exchange_query[1])}
        submit = re.fullmatch(r"提交申请\s+(CONF-[A-Za-z0-9-]+)[。！!]?", text)
        if submit:
            return {"next": "create_request", "command": {"confirmation_id": submit[1]}}
        decision = re.fullmatch(r"(确认|取消)方案\s+([A-Za-z0-9-]+)\s+[vV](\d+)[。！!]?", text)
        if decision:
            return {"next": "confirm" if decision[1] == "确认" else "cancel",
                    "command": {"proposal_id": decision[2], "version": int(decision[3])}}
        if text.rstrip("。！!") in ("取消退货", "取消办理", "不退了"):
            return {"next": "cancel", "command": {}}
        query = re.fullmatch(r"查询申请\s+(RET-[A-Za-z0-9-]+)[。！!]?", text)
        if query:
            return {"next": "select_read_tool", "update": IntakeUpdate(intent="return_query", request_id=query[1])}
        case_query = re.fullmatch(r"查询案件\s+(CASE-[A-Za-z0-9-]+)[。！!]?", text)
        if case_query:
            return {"next": "select_read_tool", "update": IntakeUpdate(intent="case_query", case_id=case_query[1])}
        ticket_query = re.fullmatch(r"查询工单\s+(TKT-[A-Za-z0-9-]+)[。！!]?", text)
        if ticket_query:
            return {"next": "select_read_tool", "update": IntakeUpdate(intent="ticket_query", ticket_id=ticket_query[1])}
        logistics_query = re.fullmatch(r"查询物流\s+(ORD-[A-Za-z0-9-]+)[。！!]?", text)
        if logistics_query:
            return {"next": "select_read_tool", "update": IntakeUpdate(intent="logistics", order_id=logistics_query[1])}
        return {"next": "prepare_context"}

    def _pending_exchange(self, s):
        proposal_id = s["context"]["state"].get("exchange_proposal_id")
        if not proposal_id:
            return None
        try:
            proposal = self.exchanges.proposal(s["session_id"], proposal_id)
        except ProposalNotFound:
            return None
        return proposal if proposal["status"] == "pending_confirmation" else None

    def _get_order(self, s, order_id):
        tool = create_get_order_tool(self.customers, s["session_id"])
        return s["run"].read("get_order", lambda: tool.invoke({"order_id": order_id}), {"order_id": order_id})

    def _prepare_context(self, s):
        state = s["context"]["state"]
        if state.get("last_intent") == "policy":
            order_id = state.get("policy", {}).get("order_id")
        elif state.get("last_intent") == "order_query":
            order_id = state.get("query_order_id")
        elif state.get("last_intent") == "logistics":
            order_id = state.get("logistics_order_id")
        elif state.get("last_intent") == "exchange":
            order_id = state.get("exchange", {}).get("order_id")
        elif state.get("last_intent") in ("repair", "quality_dispute", "human"):
            order_id = state.get("service_request", {}).get("order_id")
        else:
            order_id = state["draft"].get("order_id")
        return {"current_order": self._get_order(s, order_id).get("order") if order_id else None}

    def _recognize(self, s):
        context = s["context"]
        def extract():
            try:
                value = self.extractor(s["content"], build_context(context, s["current_order"], s["proposal"]))
                return IntakeUpdate.model_validate(value)
            except ValidationError:
                raise IntakeUnavailable("模型未返回有效的识别结果，本轮消息未保存，已有信息保留，请重试。") from None
        update = s["run"].call("model", "IntakeUpdate", extract)
        state = context["state"]
        if update.intent == "confirm_return" and state.get("last_intent") == "exchange":
            exchange = state.get("exchange", {})
            changed = bool(update.clear_fields) or any(
                getattr(update, key) is not None and getattr(update, key) != exchange.get(key)
                for key in ("order_id", "item", "quantity", "target_specification", "reason"))
            pending = self._pending_exchange(s)
            explicit = re.search(
                r"(?:^|[，,。；;！!])\s*(?:我)?(?:确认(?:换货|办理|(?:按)?(?:当前|这个|现在的)方案(?:换货|办理)?)?"
                r"|同意(?:按)?(?:当前|这个|现在的)方案(?:换货|办理)?|就按(?:当前|这个|现在的)方案(?:换货|办理)(?:吧)?)\s*[。！!]*$",
                s["content"].strip())
            if pending and explicit and not changed:
                return {"update": update, "next": "confirm_exchange",
                        "command": {"proposal_id": pending["id"], "version": pending["version"]}}
            # 修改了换货内容或只是“好的”等普通应答：继续换货流程，重新展示方案，不记录确认。
            update = update.model_copy(update={"intent": "exchange"})
        if update.intent == "confirm_return":
            draft = state["draft"]
            changed = bool(update.clear_fields) or any(
                getattr(update, key) is not None and getattr(update, key) != draft.get(key)
                for key in ("order_id", "quantity", "reason", "reason_kind", "opened"))
            if update.item is not None:
                item = match_order_item((s["current_order"] or {}).get("items", []), update.item)
                changed = changed or item is None or item["id"] != draft.get("order_item_id")
            if changed:
                # 同一句话修改了办理内容，先展示新方案，不能沿用这次确认。
                update = update.model_copy(update={"intent": "return"})
            elif s["proposal"] and s["proposal"]["status"] in ("pending_confirmation", "confirmed"):
                # 模型意图还需配合当前的明确同意表达；“已经确认”等状态声称不算授权。
                explicit = re.search(
                    r"(?:^|[，,。；;！!])\s*(?:我)?(?:确认(?:办理|退货|(?:按)?(?:当前|这个|现在的)方案(?:办理|退货)?)?"
                    r"|同意(?:按)?(?:当前|这个|现在的)方案(?:办理|退货)?|就按(?:当前|这个|现在的)方案(?:办理|退货)(?:吧)?)\s*[。！!]*$",
                    s["content"].strip())
                if explicit:
                    proposal = s["proposal"]
                    return {"update": update, "next": "confirm",
                            "command": {"proposal_id": proposal["id"], "version": proposal["version"]}}
        if update.intent == "cancel_return" and context["case_id"]:
            explicit = re.search(
                r"(?:^|[，,。；;！!])\s*(?:我)?(?:不退了|不退货了|取消(?:本次|这次)?(?:退货(?:申请)?|办理))\s*[。！!]*$",
                s["content"].strip())
            same_case = (update.case_id in (None, context["case_id"])
                         and update.order_id in (None, state["draft"].get("order_id"))
                         and update.request_id is None)
            if explicit and same_case:
                # 自然语言识别会等待模型，取消时不能误操作期间切换/更新的案件。
                return {"update": update, "next": "cancel",
                        "command": {"expected_case_id": context["loaded_case_id"],
                                    "expected_revision": context["case_revision"]}}
        if (state.get("cancelled") or state.get("completed")) and update.intent == "return":
            context["case_id"] = None
            state.clear()
            state.update(draft={}, query_order_id=None)
        state["last_intent"] = update.intent
        return {"update": update, "context": context,
                "next": "service_handoff" if update.intent in ("repair", "quality_dispute", "human") else "exchange" if update.intent == "exchange" else "policy_consultation" if update.intent == "policy" else (
                    "select_read_tool" if update.intent in ("return", "order_query", "return_query", "exchange_query", "case_query", "ticket_query", "logistics") else "clarify")}

    def _exchange(self, s):
        def inventory(order_id, item_id):
            args = {"order_id": order_id, "item_id": item_id}
            return s["run"].read("get_inventory", lambda: create_get_inventory_tool(self.customers, s["session_id"]).invoke(args), args)
        return {"result": collect_exchange(s["context"]["state"], s["update"], lambda oid: self._get_order(s, oid), inventory)}

    def _service_handoff(self, s):
        s["context"]["state"]["last_intent"] = s["update"].intent
        return {"result": collect_handoff(s["context"]["state"], s["update"], s["content"], lambda oid: self._get_order(s, oid))}

    def _policy(self, s):
        def search(args):
            return s["run"].read("search_policy", lambda: self.policy_tool.invoke(args), args)
        def explain(question, policies):
            return s["run"].call("model", "PolicyAnswer", lambda: self.policy_explainer(question, policies))
        return {"result": consult_policy(s["context"]["state"], s["update"], s["content"],
                                          lambda order_id: self._get_order(s, order_id), search, explain)}

    def _select_read_tool(self, s):
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
                    dispute.update(statement=update.reason, original_message=s["content"])
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

    def _read_tool(self, s):
        name, args, session_id = s["tool_name"], s["tool_args"], s["session_id"]
        if name == "get_order":
            value = self._get_order(s, args["order_id"])
        elif name == "none":
            value = {}
        else:
            actions = {"list_orders": lambda: self.customers.list_orders(session_id),
                       "get_exchange_request": lambda: self.exchanges.read(session_id, args["request_id"]),
                       "list_exchange_requests": lambda: self.exchanges.read(session_id),
                       "get_logistics": lambda: create_get_logistics_tool(self.customers, session_id).invoke(args),
                       "get_ticket": lambda: self.tickets.read(session_id, args["ticket_id"]),
                       "list_tickets": lambda: self.tickets.read(session_id),
                       "get_case": lambda: self.cases.get(session_id, args["case_id"]),
                       "list_cases": lambda: self.cases.list_cases(session_id),
                       "get_return_request": lambda: self.requests.get_request(session_id, args["request_id"]),
                       "list_return_requests": lambda: self.requests.list_requests(session_id)}
            value = s["run"].read(name, actions[name], args)
        if s["update"].intent == "return":
            return {"lookup": value, "next": "collect_information"}
        if name == "get_exchange_request":
            result = _result("exchange_request_found", exchange_text(value), exchange_request=value)
        elif name == "list_exchange_requests":
            result = _result("clarification_needed", "请指定换货申请编号：" + "、".join(r["id"] for r in value) if value else "当前没有换货申请。", exchange_requests=value)
        elif name == "get_logistics":
            result = (_result("logistics_found", logistics_text(value["logistics"]), logistics=value["logistics"])
                      if value["status"] == "ok" else _result("order_not_found", value["message"], ["order_id"]))
            state = s["context"]["state"]
            if value["status"] == "ok" and "logistics_dispute" in state:
                dispute = state["logistics_dispute"]
                if value["logistics"]["status"] not in ("delivered", "lost_pending"):
                    result["reply_text"] += "当前记录不属于签收争议或丢件待查，尚未创建调查工单。"
                elif not dispute.get("statement"):
                    result.update(status="collecting_information", missing_fields=["reason"])
                    result["reply_text"] += "请说明未收到的情况、是否联系承运商等信息，以便创建调查工单。"
                else:
                    result["logistics_handoff"] = {"order_id": value["logistics"]["order_id"], **dispute}
            elif value["status"] == "ok" and value["logistics"]["status"] == "lost_pending":
                result["reply_text"] += "如需调查，请说明未收到的情况并提出调查请求。"
        elif name == "get_ticket":
            result = _result("ticket_found", ticket_text(value), ticket=value)
        elif name == "list_tickets":
            text = "请指定工单编号：" + "、".join(t["id"] for t in value) if value else "当前没有人工工单。"
            result = _result("clarification_needed", text, tickets=value)
        elif name == "get_case":
            result = _result("case_found", value["summary"], case_progress=value)
        elif name == "list_cases":
            text = ("请指定要查询的案件编号：" + "、".join(c["case_id"] for c in value)
                    if value else "当前没有售后案件，可以先提供订单号和售后诉求。")
            result = _result("clarification_needed", text, cases=value)
        elif name == "get_return_request":
            result = _result("return_request_found", request_text(value), return_request=value)
        elif name == "list_return_requests":
            text = ("请提供要查询的申请编号。本人申请：" + "、".join(r["id"] for r in value)
                    if value else "当前没有已受理的退货申请；如已确认方案，请先按提示提交申请。")
            result = _result("clarification_needed", text, return_requests=value)
        elif name == "list_orders":
            result = _result("collecting_information", "请提供要查询的订单号。本人订单：" + "、".join(o["id"] for o in value), ["order_id"], orders=value)
        elif value["status"] == "ok":
            order = value["order"]
            result = _result("order_found", f"订单 {order['id']}，状态 {order['status']}，签收日期 {order['signed_on']}。商品：{_items_text(order['items'])}。", order=order)
        else:
            result = _result("order_not_found", value["message"], ["order_id"])
        return {"result": result, "next": "persist_turn"}

    def _collect(self, s):
        result, fields = self.intake.collect_return(s["context"]["state"]["draft"], s["update"], lambda _: s["lookup"])
        s["context"]["state"]["assessment"] = None
        return {"result": result, "case_fields": fields,
                "next": "evaluate_rules" if result["status"] == "ready_for_evaluation" else "clarify"}

    def _evaluate(self, s):
        draft = s["context"]["state"]["draft"]
        assessment = evaluate_return(s["result"]["order"], s["result"]["item"], draft)
        s["context"]["state"]["assessment"] = assessment
        result = _result(assessment_status(assessment), assessment_reply(assessment), assessment=assessment)
        if assessment["policy_evidence"]:
            # 资格、金额和审批要求已由规则服务确定，直接附上规则说明和所用条款，
            # 不再调用模型改写同一结论（实测这一步约占办理轮次的三分之二耗时）。
            # 用户若想进一步了解条款含义，可走政策咨询，由模型依据检索结果解释。
            result.update(policy_explanation=assessment["explanation"], citations=assessment["policy_evidence"])
        return {"result": result}

    def _clarify(self, s):
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

    def _persist(self, s):
        result = {**s["result"], "intent": s["update"].intent}
        state = s["context"]["state"]
        if result["status"] in ("collecting_information", "clarification_needed"):
            identity = json.dumps({"intent": result["intent"], "missing": result["missing_fields"],
                "draft": state["draft"], "exchange": state.get("exchange"),
                "service": {k: v for k, v in state.get("service_request", {}).items() if k not in ("original_message", "statement_message")},
                "logistics_order_id": state.get("logistics_order_id")}, ensure_ascii=False, sort_keys=True)
            previous = state.get("clarification_budget", {})
            count = previous.get("count", 0) + 1 if previous.get("identity") == identity else 1
            state["clarification_budget"] = {"identity": identity, "count": count}
            if count >= self.limits.max_clarification_rounds:
                result["failure_handoff"] = {"reason_code": "clarification_budget", "workflow": s["run"].view("stopped")}
        else:
            state.pop("clarification_budget", None)
        response = save_turn(self.customers.db_path, s["session_id"], s["user"]["id"], s["content"],
                             s["context"], result, s["case_fields"])
        s["run"].response = response
        return {"result": response}

    def _decision(self, s, action):
        def decide():
            response = self.proposals.decide(s["session_id"], action, **s["command"], content=s["content"])
            s["run"].response = response
            return response
        return {"result": s["run"].write(f"{action}_proposal", decide, s["command"])}

    def _confirm(self, s):
        return self._decision(s, "confirm")

    def _confirm_exchange(self, s):
        def create():
            response = self.exchanges.create_exchange_request(s["session_id"], **s["command"], content=s["content"])
            s["run"].response = response
            return response
        return {"result": s["run"].write("create_exchange_request", create, s["command"])}

    def _cancel(self, s):
        return self._decision(s, "cancel")

    def _create(self, s):
        def create():
            response = self.requests.create_return_request(s["session_id"], **s["command"], content=s["content"])
            s["run"].response = response
            return response
        return {"result": s["run"].write("create_return_request", create, s["command"])}

    @staticmethod
    def _reply(s):
        return {"result": s["result"]}

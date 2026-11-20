"""售后办理的 LangGraph 工作流。

- 状态：SqliteSaver 检查点按会话保存图状态（thread_id = session_id），重启后从检查点继续；
- 等待：方案确认与客服审批用 interrupt 暂停，页面按钮、消息与客服审批通过 Command(resume=...) 恢复；
- 流式：graph.stream 的 messages 模式提供答复草稿，回调处理器提供节点/模型/工具轨迹；
- 预算：步骤数用 recursion_limit，模型/工具调用次数与时间由回调处理器检查。
业务表（订单、方案、申请、退款、工单）仍是权威数据，写操作由节点经业务服务在事务内执行。
"""

import logging
import sqlite3
from collections.abc import Iterator
from copy import deepcopy

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from after_sales.domain.cases import CaseService
from after_sales.domain.exchanges import ExchangeService
from after_sales.domain.intake_service import ReturnIntake, _result
from after_sales.domain.proposals import ProposalService
from after_sales.domain.return_requests import ReturnRequestService
from after_sales.domain.tickets import TicketService
from after_sales.retrieval.index import INDEX_PATH
from after_sales.storage.database import connect_database
from after_sales.storage.intake import load_context, save_turn
from after_sales.storage.workflow import save_trace
from after_sales.tools.policies import create_search_policy_tool
from after_sales.tools.scope import tool_config
from after_sales.workflow.nodes import actions, reads, returns, routing, services, waits
from after_sales.workflow.preview import DraftPreview
from after_sales.workflow.runtime import Limits, ReadQueryUnavailable, WorkflowStopped, WriteOutcomeUnknown
from after_sales.workflow.state import State, TurnContext, turn_input
from after_sales.workflow.tracing import TraceHandler

logger = logging.getLogger("after_sales.workflow")
STOPPED = (WorkflowStopped, ReadQueryUnavailable, WriteOutcomeUnknown)
SERDE = JsonPlusSerializer(allowed_msgpack_modules=[("after_sales.agent.intake", "IntakeUpdate")])


def build_graph(checkpointer=None):
    builder = StateGraph(State, context_schema=TurnContext)
    nodes = {
        "load_context": routing.load, "route_command": routing.route_command,
        "prepare_context": routing.prepare_context, "recognize": routing.recognize,
        "select_read_tool": reads.select_read_tool, "read_tool": reads.read_tool,
        "collect_information": returns.collect_information, "evaluate_rules": returns.evaluate_rules,
        "proposal": returns.persist_turn, "clarify": returns.clarify, "persist_turn": returns.persist_turn,
        "confirm": actions.confirm, "cancel": actions.cancel, "create_request": actions.create_request,
        "confirm_exchange": actions.confirm_exchange, "policy_consultation": services.policy_consultation,
        "exchange": services.exchange, "service_handoff": services.service_handoff,
        "reply": returns.reply, "wait": waits.wait, "finish": waits.finish,
    }
    for name, function in nodes.items():
        builder.add_node(name, function)
    builder.add_node("await_confirmation", waits.await_confirmation, destinations=("load_context",))
    builder.add_node("await_approval", waits.await_approval, destinations=("load_context", "finish"))
    route = lambda s: s["next"]  # noqa: E731
    builder.add_edge(START, "load_context")
    builder.add_edge("load_context", "route_command")
    builder.add_conditional_edges("route_command", route, routing.COMMAND_NODES)
    builder.add_edge("prepare_context", "recognize")
    builder.add_conditional_edges("recognize", route, routing.RECOGNIZED_NODES)
    for name in ("service_handoff", "exchange", "policy_consultation", "clarify"):
        builder.add_edge(name, "persist_turn")
    builder.add_edge("select_read_tool", "read_tool")
    builder.add_conditional_edges("read_tool", route, ["collect_information", "persist_turn"])
    builder.add_conditional_edges("collect_information", route, ["evaluate_rules", "clarify"])
    builder.add_edge("evaluate_rules", "proposal")
    for name in ("proposal", "persist_turn", "confirm", "cancel", "create_request", "confirm_exchange"):
        builder.add_edge(name, "reply")
    builder.add_edge("reply", "wait")
    builder.add_conditional_edges("wait", route, ["await_confirmation", "await_approval", "finish"])
    builder.add_edge("finish", END)
    return builder.compile(checkpointer=checkpointer)


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
        # 检查点与业务数据在同一个本地库文件中，按会话保存图状态。
        connection = sqlite3.connect(customers.db_path, check_same_thread=False, timeout=5)
        self.checkpointer = SqliteSaver(connection, serde=SERDE)
        self.graph = build_graph(self.checkpointer)

    def close(self) -> None:
        self.checkpointer.conn.close()

    def config(self, session_id: str, trace: TraceHandler | None = None, limits: Limits | None = None) -> dict:
        config = {"configurable": {"thread_id": session_id}}
        if trace is not None:
            # 只读工具从运行配置读取会话身份（见 tools/scope.py），模型无法通过参数指定其他用户。
            config["configurable"].update(tool_config(self.customers, session_id)["configurable"])
            config.update(callbacks=[trace], recursion_limit=limits.max_steps)
        return config

    def pending(self, session_id: str) -> dict | None:
        """会话当前暂停的等待点（interrupt 的值）；没有暂停时为 None。"""
        snapshot = self.graph.get_state(self.config(session_id))
        return snapshot.interrupts[0].value if snapshot.interrupts else None

    # ---- 对外入口 ----
    def receive(self, session_id: str, content: str, *, command: dict | None = None) -> dict:
        result = None
        for event in self.stream(session_id, content, command=command):
            if event["type"] == "result":
                result = event["data"]
        return result

    def stream(self, session_id: str, content: str, *, command: dict | None = None) -> Iterator[dict]:
        """运行一轮并逐步产出页面事件：trace（轨迹快照）、reply_delta（答复草稿）、result（最终结果）。"""
        user = self.customers.current_user(session_id)
        pending = self.pending(session_id)
        if pending is None:
            graph_input = turn_input(command)
        else:
            # 暂停中的会话：按钮指令或新消息都作为恢复值，由等待节点核对版本后继续。
            graph_input = Command(resume={"action": (command or {}).get("action", "message"), **(command or {})},
                                  update=turn_input(command))
        yield from self._run(session_id, user, content, graph_input)

    def resume_approval(self, request_id: str, decision: str) -> None:
        """客服审批写入后，恢复正在等待这份申请审批的客户会话。"""
        with connect_database(self.customers.db_path) as connection:
            row = connection.execute(
                """SELECT p.case_id, p.id AS proposal_id, p.version FROM return_requests r
                   JOIN confirmations f ON f.id = r.confirmation_id JOIN proposals p ON p.id = f.proposal_id
                   WHERE r.id = ?""", (request_id,)).fetchone()
        for session_id in self.customers.sessions_for_case(row["case_id"]):
            pending = self.pending(session_id)
            if not pending or pending.get("kind") != "approval" or pending["request_id"] != request_id:
                continue
            value = {"action": "approval", "request_id": request_id, "proposal_id": row["proposal_id"],
                     "version": row["version"], "decision": decision}
            try:
                for _ in self._run(session_id, self.customers.current_user(session_id), "",
                                   Command(resume=value, update=turn_input())):
                    pass
            except Exception:
                # 审批结论已由业务服务保存；恢复失败只影响等待点，客户页面仍按业务记录显示。
                logger.warning("审批后恢复会话等待点失败：%s", session_id)

    # ---- 运行与失败处理 ----
    def _run(self, session_id, user, content, graph_input) -> Iterator[dict]:
        trace = TraceHandler(self.limits)
        config = self.config(session_id, trace, self.limits)
        start = deepcopy(self.graph.get_state(self.config(session_id)).values)
        context = TurnContext(session_id=session_id, user=user, content=content, deps=self, trace=trace)
        preview = DraftPreview()
        try:
            for mode, chunk in self.graph.stream(graph_input, config, context=context,
                                                 stream_mode=["messages", "custom", "updates"]):
                for event in trace.drain():
                    yield {"type": "trace", "run_id": trace.id, "event": event}
                if mode == "messages":
                    text = preview.feed(*chunk)
                    if text:
                        yield {"type": "reply_delta", "text": text, "provisional": True}
        except Exception as exc:
            yield from self._handle_failure(exc, session_id, user, content, trace, start)
            return
        for event in trace.drain():
            yield {"type": "trace", "run_id": trace.id, "event": event}
        values = self.graph.get_state(self.config(session_id)).values
        response = values.get("response")
        trace_view = trace.view(response["status"] if response else "resumed")
        save_trace(self.customers.db_path, session_id, trace_view)
        yield {"type": "result", "data": {**response, "workflow": trace_view} if response else {"workflow": trace_view}}

    def _reset(self, session_id, values, conversation):
        """失败后把会话检查点恢复为一轮结束的干净状态，下一轮从头开始。"""
        self.graph.update_state(self.config(session_id), {
            **turn_input(), "conversation": conversation,
            "case_id": values.get("case_id"), "case_revision": values.get("case_revision"), "pending": None,
        }, as_node="finish")

    def _handle_failure(self, exc, session_id, user, content, trace, start) -> Iterator[dict]:
        if isinstance(exc, GraphRecursionError):
            exc = WorkflowStopped("本轮工作流已达到步骤预算，已停止，未完成本轮办理。")
        values = self.graph.get_state(self.config(session_id)).values
        response = values.get("response")
        if response is not None and isinstance(exc, WorkflowStopped):
            # 数据已提交时如实返回原结果，不能因后续步骤耗尽预算声称未执行。
            self._reset(session_id, values, (values.get("context") or {}).get("state") or values.get("conversation"))
            response = {**response, "workflow_notice": "业务结果已保存，后续轨迹步骤因预算停止。"}
            trace_view = trace.view(response["status"])
            save_trace(self.customers.db_path, session_id, trace_view)
            yield {"type": "result", "data": {**response, "workflow": trace_view}}
            return
        if isinstance(exc, STOPPED):
            snapshot = {key: start.get(key) for key in ("conversation", "case_id", "case_revision")}
            context = load_context(self.customers.db_path, session_id, snapshot)
            failure = {"reason_code": type(exc).__name__, "workflow": trace.view("stopped")}
            if isinstance(exc, WriteOutcomeUnknown):
                failure["write_check"] = self._write_check(session_id, exc)
            result = _result("workflow_handoff", "", failure_handoff=failure)
            response = save_turn(self.customers.db_path, session_id, user["id"], content, context, result, None)
            self._reset(session_id, {"case_id": context["case_id"], "case_revision": context["case_revision"]},
                        context["state"])
            trace_view = trace.view(response["status"])
            save_trace(self.customers.db_path, session_id, trace_view)
            yield {"type": "result", "data": {**response, "workflow": trace_view}}
            return
        # 其余错误（模型故障、业务冲突等）本轮不保存；会话回到本轮开始前的状态。
        self._reset(session_id, start, start.get("conversation"))
        trace_view = trace.view("stopped" if isinstance(exc, WorkflowStopped) else "error")
        trace_view["error_type"] = type(exc).__name__
        save_trace(self.customers.db_path, session_id, trace_view)
        exc.workflow = trace_view
        raise exc

    def _write_check(self, session_id, exc) -> dict:
        """用原确认/方案编号查询已提交记录；这不是重发写操作。"""
        args = exc.args_summary
        if exc.name == "create_return_request":
            record = next((r for r in self.requests.list_requests(session_id) if r["confirmation_id"] == args["confirmation_id"]), None)
        elif exc.name == "create_exchange_request":
            record = next((r for r in self.exchanges.read(session_id) if r["proposal_id"] == args["proposal_id"]), None)
        else:
            record = self.proposals.current(session_id)["proposal"]
            if record and args.get("proposal_id") and (record["id"], record["version"]) != (args["proposal_id"], args["version"]):
                record = None
        return {"tool": exc.name, "original_args": args, "record": record}

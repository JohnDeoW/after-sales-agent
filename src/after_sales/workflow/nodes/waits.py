"""等待用户确认与等待客服审批：用 interrupt 暂停在检查点，由 Command(resume=...) 恢复。

每轮结束前由 wait 节点按业务记录判断是否需要等待：
- 有待确认的退货/换货方案 → await_confirmation；
- 当前退货申请待审批 → await_approval；
- 否则结束。
恢复值携带方案编号与版本；与等待中的版本不一致且等待的版本仍是当前版本时，旧指令被拒绝。
真正的授权仍由业务服务在写入事务中校验，interrupt 只负责持久化等待点与恢复。
"""

from langgraph.runtime import Runtime
from langgraph.types import Command, interrupt

from after_sales.domain.proposals import ProposalConflict, ProposalNotFound
from after_sales.workflow.nodes.routing import pending_exchange
from after_sales.workflow.state import State, TurnContext

STALE_DECISION = "方案已变更，请查看并确认最新版本。"


def wait(s: State, runtime: Runtime[TurnContext]) -> dict:
    ctx = runtime.context
    deps = ctx.deps
    state = (s.get("context") or {}).get("state") or s.get("conversation") or {}
    proposal = deps.proposals.current(ctx.session_id)["proposal"]
    if proposal and proposal["status"] == "pending_confirmation":
        return {"next": "await_confirmation", "pending": {
            "kind": "confirmation", "proposal_id": proposal["id"], "version": proposal["version"]}}
    exchange = pending_exchange(deps, ctx.session_id, state)
    if exchange:
        return {"next": "await_confirmation", "pending": {
            "kind": "exchange_confirmation", "proposal_id": exchange["id"], "version": exchange["version"]}}
    request_id = ((s.get("response") or {}).get("return_request") or {}).get("id") or state.get("return_request_id")
    if request_id:
        try:
            request = deps.requests.get_request(ctx.session_id, request_id)
        except ProposalNotFound:
            request = None
        if request and request["status"] == "pending_approval":
            return {"next": "await_approval", "pending": {
                "kind": "approval", "request_id": request["id"],
                "proposal_id": request["proposal_id"], "version": request["proposal_version"]}}
    return {"next": "finish", "pending": None}


def _still_current(deps, session_id, pending) -> bool:
    if pending["kind"] == "confirmation":
        current = deps.proposals.current(session_id)["proposal"]
        return bool(current and current["status"] == "pending_confirmation"
                    and (current["id"], current["version"]) == (pending["proposal_id"], pending["version"]))
    try:
        current = deps.exchanges.proposal(session_id, pending["proposal_id"])
    except ProposalNotFound:
        return False
    return current["status"] == "pending_confirmation" and current["version"] == pending["version"]


def await_confirmation(s: State, runtime: Runtime[TurnContext]) -> Command:
    pending = s["pending"]
    value = interrupt(pending)
    action = value.get("action")
    matching = {"confirmation": ("confirm", "cancel"), "exchange_confirmation": ("confirm_exchange",)}[pending["kind"]]
    if action in matching and value.get("proposal_id") is not None and (
            (value["proposal_id"], value.get("version")) != (pending["proposal_id"], pending["version"])
            and _still_current(runtime.context.deps, runtime.context.session_id, pending)):
        raise ProposalConflict(STALE_DECISION)
    # 恢复后重新读取上下文，再按本轮指令或消息继续；写入仍由业务服务校验。
    return Command(goto="load_context", update={"resumed": {
        "kind": pending["kind"], "action": action, "proposal_id": value.get("proposal_id"), "version": value.get("version")}})


def await_approval(s: State, runtime: Runtime[TurnContext]) -> Command:
    pending = s["pending"]
    value = interrupt(pending)
    if value.get("action") == "approval":
        if (value.get("request_id"), value.get("proposal_id"), value.get("version")) != (
                pending["request_id"], pending["proposal_id"], pending["version"]):
            raise ProposalConflict("审批对应的方案版本与等待中的申请不一致，已拒绝恢复。")
        return Command(goto="finish", update={"resumed": {"kind": "approval", "action": "approval",
                                                           "request_id": value["request_id"], "decision": value.get("decision")}})
    return Command(goto="load_context", update={"resumed": {"kind": "approval", "action": value.get("action")}})


def finish(s: State, runtime: Runtime[TurnContext]) -> dict:
    return {}

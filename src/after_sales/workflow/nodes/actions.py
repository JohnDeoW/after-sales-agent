"""写操作节点：确认/取消方案、提交退货申请、确认换货。只由确定性节点经业务服务执行。"""

from langgraph.runtime import Runtime

from after_sales.workflow.nodes.common import write
from after_sales.workflow.state import State, TurnContext


def _decision(s: State, runtime: Runtime[TurnContext], action: str) -> dict:
    ctx = runtime.context
    command = s["command"]
    response = write(f"{action}_proposal",
                     lambda: ctx.deps.proposals.decide(ctx.session_id, action, **command, content=ctx.content), command)
    return {"result": response, "response": response}


def confirm(s: State, runtime: Runtime[TurnContext]) -> dict:
    return _decision(s, runtime, "confirm")


def cancel(s: State, runtime: Runtime[TurnContext]) -> dict:
    return _decision(s, runtime, "cancel")


def create_request(s: State, runtime: Runtime[TurnContext]) -> dict:
    ctx = runtime.context
    command = s["command"]
    response = write("create_return_request",
                     lambda: ctx.deps.requests.create_return_request(ctx.session_id, **command, content=ctx.content), command)
    return {"result": response, "response": response}


def confirm_exchange(s: State, runtime: Runtime[TurnContext]) -> dict:
    ctx = runtime.context
    command = s["command"]
    response = write("create_exchange_request",
                     lambda: ctx.deps.exchanges.create_exchange_request(ctx.session_id, **command, content=ctx.content), command)
    # 换货申请编号属于会话草稿，由图状态保存；后续“查询换货”直接使用。
    state = s["context"]["state"]
    state.update(exchange_request_id=response["exchange_request"]["id"], last_intent="exchange_query")
    return {"result": response, "response": response, "conversation": state}

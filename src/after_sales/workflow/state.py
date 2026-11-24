"""图状态与每轮运行上下文。

图状态由 SqliteSaver 按会话（thread_id = session_id）保存：
- conversation / case_id / case_revision 跨轮保留，是会话的办理草稿；
- 其余为本轮数据，每轮开始时重置，暂停在 interrupt 时随检查点保存，恢复后继续使用。
运行上下文（会话、用户、本轮消息、业务服务）不进检查点。
"""

from dataclasses import dataclass
from typing import Any, TypedDict

from after_sales.agent.intake import IntakeUpdate


class State(TypedDict, total=False):
    conversation: dict
    case_id: str | None
    case_revision: int | None
    context: dict | None
    proposal: dict | None
    current_order: dict | None
    update: IntakeUpdate | None
    next: str | None
    command: dict | None
    tool_name: str | None
    tool_args: dict | None
    lookup: Any
    result: dict | None
    case_fields: dict | None
    response: dict | None
    resumed: dict | None
    pending: dict | None  # 等待点（确认或审批），暂停期间随检查点保存


TURN_KEYS = ("context", "proposal", "current_order", "update", "next", "command", "tool_name",
             "tool_args", "lookup", "result", "case_fields", "response", "resumed")


def turn_input(command: dict | None = None) -> dict:
    """新一轮的初始写入：清空上一轮的本轮数据，保留跨轮草稿。"""
    return {**{key: None for key in TURN_KEYS}, "command": command or {}}


@dataclass
class TurnContext:
    session_id: str
    user: dict
    content: str
    deps: Any  # AfterSalesWorkflow：业务服务、模型步骤与预算
    trace: Any  # 本轮的 TraceHandler
    display: str = ""  # 保存为用户消息的文字（按钮操作为按钮文字）；为空时用 content

"""节点共用的调用方式：工具与模型步骤都以 Runnable 运行，由回调生成轨迹并计入预算。"""

from langchain_core.runnables import Runnable, RunnableLambda
from langgraph.runtime import Runtime

from after_sales.tools.orders import get_order as get_order_tool
from after_sales.workflow.runtime import ReadQueryUnavailable, WriteOutcomeUnknown
from after_sales.workflow.state import TurnContext
from after_sales.workflow.tracing import MODEL_TAG, TOOL_TAG

TRANSIENT = (TimeoutError, ConnectionError)


def as_tool(name: str, function) -> Runnable:
    """把写操作包装为带 tool_call 标签的 Runnable；轨迹按工具调用记录，但不作为模型可调用的工具。"""
    return RunnableLambda(lambda args: function(), name=name).with_config(tags=[TOOL_TAG])


def read(name: str, runnable: Runnable, args: dict | None = None):
    """只读查询：临时失败（超时、连接中断）用 with_retry 重试一次，仍失败则停止自动办理。"""
    try:
        return runnable.with_retry(retry_if_exception_type=TRANSIENT, stop_after_attempt=2,
                                   wait_exponential_jitter=False).invoke(args or {})
    except TRANSIENT:
        raise ReadQueryUnavailable("只读查询重试后仍暂不可用，自动办理已停止。") from None


def write(name: str, function, args: dict | None = None):
    """写操作不自动重试；超时或连接中断时结果未知，交由失败处理按原编号查询。"""
    try:
        return as_tool(name, function).invoke(args or {})
    except TRANSIENT:
        raise WriteOutcomeUnknown(name, args or {}) from None


def model_step(name: str, function):
    """模型步骤（真实模型或测试替身）统一以带 model_step 标签的 Runnable 运行。"""
    return RunnableLambda(lambda _: function(), name=name).with_config(tags=[MODEL_TAG]).invoke(None)


def get_order(runtime: Runtime[TurnContext], order_id: str) -> dict:
    return read("get_order", get_order_tool, {"order_id": order_id})

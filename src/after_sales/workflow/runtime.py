"""工作流的预算配置、错误类型和轨迹摘要；不记录提示词、密钥或思维链。"""

from dataclasses import dataclass

import httpx
import openai


class ModelCallFailed(RuntimeError):
    """模型调用失败；detail 只含分类、状态码、请求编号和耗时，不含密钥、提示词或用户原文。"""

    def __init__(self, detail: dict):
        super().__init__(detail["category"])
        self.detail = detail


class RetryableModelError(ModelCallFailed):
    """连接失败或上游 5xx；交给 Runnable.with_retry 重试一次。"""


def classify_model_error(exc) -> dict | None:
    """区分超时、连接失败、限流和上游错误；其余异常不属于模型服务故障，原样抛出。"""
    if isinstance(exc, (openai.APITimeoutError, httpx.TimeoutException)):
        category = "timeout"
    elif isinstance(exc, (openai.APIConnectionError, httpx.TransportError)):
        category = "connection"
    elif isinstance(exc, openai.RateLimitError):
        category = "rate_limit"
    elif isinstance(exc, openai.APIStatusError):
        category = "upstream_error"
    else:
        return None
    return {"category": category, "error_class": type(exc).__name__,
            "http_status": getattr(exc, "status_code", None), "request_id": getattr(exc, "request_id", None)}


class WorkflowStopped(RuntimeError):
    def __init__(self, message: str, status_code: int = 429):
        super().__init__(message)
        self.status_code = status_code


class ReadQueryUnavailable(RuntimeError):
    """只读工具的两次临时失败，不包含底层异常文本。"""


class WriteOutcomeUnknown(RuntimeError):
    def __init__(self, name, args):
        super().__init__("写入结果待核对，不自动重复执行。")
        self.name, self.args_summary = name, args


@dataclass(frozen=True)
class Limits:
    # max_steps 作为 LangGraph recursion_limit；含等待节点在内的最长路径约 12 步。
    max_steps: int = 16
    max_model_calls: int = 3  # 识别 + 政策解释；政策首次无依据时另有一次改写重查
    max_tool_calls: int = 4
    turn_seconds: float = 255
    max_clarification_rounds: int = 3


TRACE_ARGS = ("order_id", "item_id", "request_id", "case_id", "ticket_id", "proposal_id",
              "confirmation_id", "version", "sku", "ordered_on", "reason_kind")


def summary(value) -> dict:
    if hasattr(value, "goto") and hasattr(value, "resume"):  # 节点返回的 langgraph Command
        value = {**(value.update or {}), "next": value.goto}
    if isinstance(value, list):
        return {"count": len(value)}
    if not isinstance(value, dict):
        return {"intent": value.intent} if hasattr(value, "intent") else {}
    if "result" in value and isinstance(value["result"], dict):
        return summary(value["result"])
    if "update" in value and value["update"] is not None:
        return summary(value["update"])
    # 不保存完整订单、用户原话、业务原因或模型原始响应。
    result = {key: value[key] for key in ("status", "intent", "missing_fields", "reused", "next", "tool_name") if key in value}
    for key in ("proposal", "return_request"):
        if value.get(key):
            result[key] = {k: value[key][k] for k in ("id", "version", "status") if k in value[key]}
    if value.get("assessment"):
        result["rule_code"] = value["assessment"]["code"]
    if value.get("order"):
        result["order_id"] = value["order"]["id"]
    if value.get("policies"):
        result["policy_refs"] = [p["chunk_id"] for p in value["policies"]]
    evidence = value.get("citations") or (value.get("assessment") or {}).get("policy_evidence")
    if evidence:
        result["citations"] = [{k: p[k] for k in ("chunk_id", "clause_id", "version", "title", "body") if k in p} for p in evidence]
    if value.get("ticket"):
        result["ticket"] = {k: value["ticket"][k] for k in ("id", "status", "category") if k in value["ticket"]}
    for key in ("interrupt", "resumed", "pending", "selected_tools", "retrieval"):
        if value.get(key):
            result[key] = value[key]
    return result

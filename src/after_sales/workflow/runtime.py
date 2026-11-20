"""每轮独立的调用预算和精简轨迹，不记录提示词、密钥或思维链。"""

from dataclasses import asdict, dataclass, field
from contextvars import ContextVar
from copy import deepcopy
from time import perf_counter
import logging
import secrets

import httpx
import openai

active_run = ContextVar("after_sales_run", default=None)
logger = logging.getLogger("after_sales.model")
SLOW_MODEL_MS = 30_000


class ModelCallFailed(RuntimeError):
    """模型调用失败；detail 只含分类、状态码、请求编号和耗时，不含密钥、提示词或用户原文。"""

    def __init__(self, detail: dict):
        super().__init__(detail["category"])
        self.detail = detail


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


def _call(model, messages, run, preview, on_chunk):
    if run is None or run.emit is None:
        return model.invoke(messages)
    reply = None
    previous = ""
    for chunk in model.stream(messages):
        on_chunk()
        reply = chunk if reply is None else reply + chunk
        if preview and reply.tool_calls:
            args = reply.tool_calls[0].get("args", {})
            current = args.get("text", "") if isinstance(args, dict) else ""
            if isinstance(current, str) and current.startswith(previous) and current != previous:
                run.emit({"type": "reply_delta", "text": current[len(previous):], "provisional": True})
                previous = current
    if reply is None:
        raise ValueError("empty model stream")
    return reply


def invoke_model(model, messages, *, preview=False):
    """结构化流只公开答复草稿；不传递工具 JSON 或推理块。

    连接失败或上游 5xx 且尚未收到任何数据时重试一次；超时不重试，避免把等待时间翻倍。
    """
    run = active_run.get()
    for attempt in (1, 2):
        started = perf_counter()
        first = []
        mark = lambda: first or first.append(perf_counter())  # noqa: E731
        try:
            reply = _call(model, messages, run, preview, mark)
            break
        except Exception as exc:
            detail = classify_model_error(exc)
            if detail is None:
                raise
            detail.update(attempt=attempt, elapsed_ms=round((perf_counter() - started) * 1000),
                          first_chunk_ms=round((first[0] - started) * 1000) if first else None)
            retryable = detail["category"] == "connection" or (
                detail["category"] == "upstream_error" and (detail["http_status"] or 0) >= 500)
            if attempt == 1 and retryable and not first:
                logger.warning("模型调用失败，重试一次：%s", detail)
                continue
            logger.warning("模型调用失败：%s", detail)
            raise ModelCallFailed(detail) from None
    elapsed = round((perf_counter() - started) * 1000)
    metrics = {"elapsed_ms": elapsed, "first_chunk_ms": round((first[0] - started) * 1000) if first else None,
               "attempts": attempt}
    if elapsed >= SLOW_MODEL_MS:
        logger.warning("模型响应较慢：%s", metrics)
    if run is not None:
        run.model_metrics.append(metrics)
        if reply.usage_metadata:
            for key in ("input_tokens", "output_tokens", "total_tokens"):
                run.tokens[key] = run.tokens.get(key, 0) + reply.usage_metadata.get(key, 0)
    return reply


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
    max_steps: int = 12
    max_model_calls: int = 2
    max_tool_calls: int = 4
    turn_seconds: float = 255
    max_clarification_rounds: int = 3


def summary(value) -> dict:
    if isinstance(value, list):
        return {"count": len(value)}
    if not isinstance(value, dict):
        return {"intent": value.intent} if hasattr(value, "intent") else {}
    if "result" in value:
        return summary(value["result"])
    if "update" in value:
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
    return result


@dataclass
class Run:
    limits: Limits
    emit: object = None
    tokens: dict = field(default_factory=dict)
    model_metrics: list = field(default_factory=list)
    id: str = field(default_factory=lambda: "RUN-" + secrets.token_hex(8))
    started: float = field(default_factory=perf_counter)
    events: list = field(default_factory=list)
    steps: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    response: dict | None = None

    def check_time(self):
        if perf_counter() - self.started >= self.limits.turn_seconds:
            raise WorkflowStopped("本轮已达到时间预算，已停止，未完成本轮办理。请稍后重试。", 504)

    def read(self, name, function, args=None):
        for attempt in range(2):
            try:
                return self.call("tool", name, function, args)
            except (TimeoutError, ConnectionError):
                if attempt == 1:
                    raise ReadQueryUnavailable("只读查询重试后仍暂不可用，自动办理已停止。") from None

    def write(self, name, function, args=None):
        try:
            return self.call("tool", name, function, args)
        except (TimeoutError, ConnectionError):
            raise WriteOutcomeUnknown(name, args or {}) from None

    def call(self, kind: str, name: str, function, args: dict | None = None):
        event = {"sequence": len(self.events) + 1, "kind": kind, "name": name,
                 "args": {k: v for k, v in (args or {}).items() if k in (
                     "order_id", "item_id", "request_id", "case_id", "ticket_id", "proposal_id",
                     "confirmation_id", "version", "sku", "ordered_on", "reason_kind")}, "status": "running"}
        self.events.append(event)
        if self.emit:
            self.emit({"type": "trace", "run_id": self.id, "event": deepcopy(event)})
        started = perf_counter()
        try:
            self.check_time()
            counter, maximum = {"node": ("steps", self.limits.max_steps),
                                "model": ("model_calls", self.limits.max_model_calls),
                                "tool": ("tool_calls", self.limits.max_tool_calls)}[kind]
            if getattr(self, counter) >= maximum:
                raise WorkflowStopped(f"本轮已达到 {counter} 调用预算，已停止，未完成本轮办理。")
            setattr(self, counter, getattr(self, counter) + 1)
            value = function()
            if kind == "model" and self.model_metrics:
                event["model_metrics"] = self.model_metrics.pop()
            # 写操作的结果一旦提交就保留，不因后续预算检查改报未完成。
            if self.response is None:
                self.check_time()
            event.update(status="ok", result=summary(value))
            return value
        except Exception as exc:
            event.update(status="error", error_type=type(exc).__name__)
            if kind == "model" and isinstance(getattr(exc, "detail", None), dict):
                # 模型故障分类：超时、连接、限流、上游错误或输出格式错误。
                event["error_detail"] = exc.detail
            raise
        finally:
            event["duration_ms"] = round((perf_counter() - started) * 1000, 2)
            if self.emit:
                self.emit({"type": "trace", "run_id": self.id, "event": deepcopy(event)})

    def view(self, status: str) -> dict:
        return {"id": self.id, "status": status, "duration_ms": round((perf_counter() - self.started) * 1000, 2),
                "limits": asdict(self.limits), "usage": {"steps": self.steps, "model_calls": self.model_calls,
                                                       "tool_calls": self.tool_calls, "tokens": self.tokens or None}, "events": self.events}

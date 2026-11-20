"""由 LangChain 回调生成页面运行轨迹，并执行每轮的调用次数与时间预算。

节点、模型步骤（带 model_step 标签的 Runnable）和工具调用的开始/结束回调都会到这里：
- 轨迹只保留节点名、工具参数白名单、结果摘要、耗时、token 与故障分类，不记录提示词或模型原文；
- 预算在开始回调中检查，超出时抛出 WorkflowStopped（raise_error=True 使其中止本轮）；
- 流式接口从 drain() 取出新增的轨迹快照推送给页面。
"""

from copy import deepcopy
from dataclasses import asdict
from threading import Lock
from time import perf_counter
import logging
import secrets

from langchain_core.callbacks import BaseCallbackHandler
from langgraph.errors import GraphInterrupt

from after_sales.workflow.runtime import Limits, WorkflowStopped, summary, TRACE_ARGS

logger = logging.getLogger("after_sales.model")
SLOW_MODEL_MS = 30_000
MODEL_TAG = "model_step"
TOOL_TAG = "tool_call"


class TraceHandler(BaseCallbackHandler):
    raise_error = True

    def __init__(self, limits: Limits):
        self.limits = limits
        self.id = "RUN-" + secrets.token_hex(8)
        self.started = perf_counter()
        self.events: list[dict] = []
        self.tokens: dict = {}
        self.counts = {"node": 0, "model": 0, "tool": 0}
        self._root = None
        self._parents: dict = {}
        self._open: dict = {}  # run_id -> (event, started)
        self._lock = Lock()
        self._published: list[dict] = []
        self._drained = 0

    # ---- 事件与预算 ----
    def _owner(self, run_id, kind):
        """沿父运行查找已登记的同类事件（模型内部的重试、工具内部的子运行都归到外层事件）。"""
        while run_id is not None:
            entry = self._open.get(run_id)
            if entry and entry[0]["kind"] == kind:
                return entry[0]
            run_id = self._parents.get(run_id)
        return None

    def _start(self, run_id, kind, name, args=None):
        event = {"sequence": len(self.events) + 1, "kind": kind, "name": name,
                 "args": {k: v for k, v in (args or {}).items() if k in TRACE_ARGS}, "status": "running"}
        with self._lock:
            self.events.append(event)
            self._open[run_id] = (event, perf_counter())
            self._publish(event)
        try:
            if perf_counter() - self.started >= self.limits.turn_seconds:
                raise WorkflowStopped("本轮已达到时间预算，已停止，未完成本轮办理。请稍后重试。", 504)
            counter, maximum = {"node": ("steps", self.limits.max_steps),
                                "model": ("model_calls", self.limits.max_model_calls),
                                "tool": ("tool_calls", self.limits.max_tool_calls)}[kind]
            if self.counts[kind] >= maximum:
                raise WorkflowStopped(f"本轮已达到 {counter} 调用预算，已停止，未完成本轮办理。")
            self.counts[kind] += 1
        except WorkflowStopped as exc:
            self._finish(run_id, error=exc)
            raise

    def _finish(self, run_id, *, output=None, error=None):
        entry = self._open.pop(run_id, None)
        if entry is None:
            return
        event, started = entry
        event["duration_ms"] = round((perf_counter() - started) * 1000, 2)
        if isinstance(error, GraphInterrupt):
            # interrupt 以异常形式暂停图；记录暂停点而不是失败。
            event.update(status="interrupted", result={"interrupt": [i.value for i in error.args[0]]})
        elif error is not None:
            event.update(status="error", error_type=type(error).__name__)
            if event["kind"] == "model" and isinstance(getattr(error, "detail", None), dict):
                event["error_detail"] = error.detail
        else:
            event.update(status="ok", result=summary(output))
        if event["kind"] == "model":
            metrics = event.setdefault("model_metrics", {"attempts": 1, "first_chunk_ms": None})
            metrics["elapsed_ms"] = round(event["duration_ms"])
            if metrics["elapsed_ms"] >= SLOW_MODEL_MS:
                logger.warning("模型响应较慢：%s", metrics)
            if error is not None and isinstance(getattr(error, "detail", None), dict):
                logger.warning("模型调用失败：%s", error.detail)
        with self._lock:
            self._publish(event)

    def _publish(self, event):
        self._published.append(deepcopy(event))

    def drain(self) -> list[dict]:
        """返回自上次取出后新增的轨迹快照，供流式接口推送。"""
        with self._lock:
            items = self._published[self._drained:]
            self._drained = len(self._published)
        return items

    # ---- 回调 ----
    def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None, tags=None, metadata=None, name=None, **kwargs):
        self._parents[run_id] = parent_run_id
        tags = tags or []
        if parent_run_id is None:
            self._root = run_id
        elif parent_run_id == self._root and name and not name.startswith("__"):
            self._start(run_id, "node", name)
        elif TOOL_TAG in tags:
            # 业务服务查询/写入以带 tool_call 标签的 Runnable 运行，按工具调用记录（重试的每次尝试各记一次）。
            if self._owner(parent_run_id, "tool") is None:
                self._start(run_id, "tool", name, inputs if isinstance(inputs, dict) else None)
        elif MODEL_TAG in tags:
            owner = self._owner(parent_run_id, "model")
            if owner is None:
                self._start(run_id, "model", name)
            else:
                attempt = next((int(t.rsplit(":", 1)[1]) for t in tags if t.startswith("retry:attempt:")), None)
                if attempt:
                    owner.setdefault("model_metrics", {"attempts": 1, "first_chunk_ms": None})["attempts"] = attempt

    def on_chain_end(self, outputs, *, run_id, **kwargs):
        self._finish(run_id, output=outputs)

    def on_chain_error(self, error, *, run_id, **kwargs):
        self._finish(run_id, error=error)

    def on_tool_start(self, serialized, input_str, *, run_id, parent_run_id=None, inputs=None, **kwargs):
        self._parents[run_id] = parent_run_id
        if self._owner(parent_run_id, "tool") is None:
            self._start(run_id, "tool", (serialized or {}).get("name") or kwargs.get("name"), inputs)

    def on_tool_end(self, output, *, run_id, **kwargs):
        self._finish(run_id, output=output)

    def on_tool_error(self, error, *, run_id, **kwargs):
        self._finish(run_id, error=error)

    def on_chat_model_start(self, serialized, messages, *, run_id, parent_run_id=None, **kwargs):
        self._parents[run_id] = parent_run_id

    def on_llm_new_token(self, token, *, run_id, **kwargs):
        owner = self._owner(run_id, "model")
        if owner is not None:
            metrics = owner.setdefault("model_metrics", {"attempts": 1, "first_chunk_ms": None})
            if metrics.get("first_chunk_ms") is None:
                metrics["first_chunk_ms"] = round((perf_counter() - self._open[self._find_open(owner)][1]) * 1000)

    def _find_open(self, event):
        return next(key for key, (item, _) in self._open.items() if item is event)

    def on_llm_end(self, response, *, run_id, **kwargs):
        generation = response.generations[0][0] if response.generations and response.generations[0] else None
        usage = getattr(getattr(generation, "message", None), "usage_metadata", None)
        if usage:
            for key in ("input_tokens", "output_tokens", "total_tokens"):
                self.tokens[key] = self.tokens.get(key, 0) + usage.get(key, 0)

    # ---- 结果 ----
    def usage(self) -> dict:
        return {"steps": self.counts["node"], "model_calls": self.counts["model"],
                "tool_calls": self.counts["tool"], "tokens": self.tokens or None}

    def view(self, status: str) -> dict:
        # 中途停止时仍在运行的事件按停止处理，避免页面一直显示“进行中”。
        for event in self.events:
            if event["status"] == "running":
                event["status"] = "stopped"
        return {"id": self.id, "status": status, "duration_ms": round((perf_counter() - self.started) * 1000, 2),
                "limits": asdict(self.limits), "usage": self.usage(), "events": self.events}

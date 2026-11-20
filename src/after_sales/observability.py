"""本地 Arize Phoenix 追踪：OpenTelemetry 导出 + OpenInference 对 LangChain/LangGraph 自动埋点。

- 由 .env 的 PHOENIX_ENABLED 控制，默认关闭；关闭时不导入 Phoenix，也不建立任何连接。
- 自动埋点挂在 LangChain 回调上，图节点、模型调用（输入输出与 token）、工具调用都会形成完整调用链。
- PHOENIX_HIDE_IO=true 时隐藏模型消息、输入输出与检索文档原文，只保留结构、耗时与 token。
- 页面轨迹面向用户（脱敏摘要）；Phoenix 面向开发调试与评测（完整调用链），两者并存。
"""

import logging

from after_sales.config import ConfigurationError, Settings

logger = logging.getLogger("after_sales.observability")
_state: dict = {}


def trace_config(hide_io: bool):
    from openinference.instrumentation import TraceConfig
    return TraceConfig(hide_inputs=hide_io, hide_outputs=hide_io, hide_input_messages=hide_io,
                       hide_output_messages=hide_io, hide_prompts=hide_io, hide_choices=hide_io,
                       hide_input_text=hide_io, hide_output_text=hide_io, hide_retrieval_documents=hide_io,
                       hide_llm_tools=hide_io)


def instrument(tracer_provider, *, hide_io: bool) -> None:
    """为当前进程的 LangChain 回调挂上 OpenInference 追踪；测试可传入内存导出的 TracerProvider。"""
    from openinference.instrumentation.langchain import LangChainInstrumentor
    LangChainInstrumentor().instrument(tracer_provider=tracer_provider, config=trace_config(hide_io))


def uninstrument() -> None:
    from openinference.instrumentation.langchain import LangChainInstrumentor
    LangChainInstrumentor().uninstrument()
    _state.clear()


def phoenix_url(settings: Settings) -> str:
    return settings.phoenix_endpoint.removesuffix("/v1/traces").rstrip("/") + "/projects"


def setup_tracing(settings: Settings) -> str | None:
    """按配置开启 Phoenix 追踪，返回 Phoenix 页面地址；未开启时返回 None。每个进程只初始化一次。"""
    if not settings.phoenix_enabled:
        return None
    if "provider" not in _state:
        try:
            from phoenix.otel import register
        except ImportError:
            raise ConfigurationError("已开启 PHOENIX_ENABLED，但未安装可观测性依赖，请运行 uv sync --extra observability。") from None
        provider = register(endpoint=settings.phoenix_endpoint, project_name=settings.phoenix_project_name,
                            batch=True, set_global_tracer_provider=False, verbose=False)
        instrument(provider, hide_io=settings.phoenix_hide_io)
        _state["provider"] = provider
        logger.info("Phoenix 追踪已开启：%s（隐藏输入输出：%s）", settings.phoenix_endpoint, settings.phoenix_hide_io)
    return phoenix_url(settings)


def flush() -> None:
    """服务关闭时把尚未发送的 span 推送出去。"""
    provider = _state.get("provider")
    if provider is not None:
        provider.force_flush()

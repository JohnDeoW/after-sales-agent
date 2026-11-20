"""通过项目配置连接中转站的 Responses 接口，并统一模型调用的重试与故障分类。"""

from time import perf_counter

from langchain_core.runnables import RunnableConfig, RunnableLambda
from langchain_openai import ChatOpenAI

from after_sales.config import Settings, load_settings
from after_sales.workflow.runtime import ModelCallFailed, RetryableModelError, classify_model_error


def create_model(settings: Settings | None = None) -> ChatOpenAI:
    settings = settings or load_settings()
    return ChatOpenAI(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        model=settings.llm_model_id,
        use_responses_api=True,
        output_version="responses/v1",
        # 历史由应用传递，不依赖中转站保存上一轮 response。
        store=False,
        include=["reasoning.encrypted_content"],
        reasoning_effort="low",
        timeout=settings.llm_timeout_seconds,
        # 重试由下方 with_retry 控制，SDK 自身不重试。
        max_retries=0,
    )


def invoke_model(model, messages):
    """调用模型；连接失败或上游 5xx 用 Runnable.with_retry 重试一次，超时与限流不重试。

    返回完整 AIMessage。所有模型服务故障转换为 ModelCallFailed，detail 含分类、尝试次数与耗时。
    在 LangGraph 中以 messages 模式流式运行时，模型会自动逐段输出，供页面预览答复草稿。
    """
    def attempt(value, config: RunnableConfig):
        number = next((int(t.rsplit(":", 1)[1]) for t in config.get("tags", []) if t.startswith("retry:attempt:")), 1)
        started = perf_counter()
        try:
            return model.invoke(value)
        except Exception as exc:
            detail = classify_model_error(exc)
            if detail is None:
                raise
            detail.update(attempt=number, elapsed_ms=round((perf_counter() - started) * 1000))
            retryable = detail["category"] == "connection" or (
                detail["category"] == "upstream_error" and (detail["http_status"] or 0) >= 500)
            raise (RetryableModelError if retryable else ModelCallFailed)(detail) from None

    runnable = RunnableLambda(attempt, name="model_attempt").with_retry(
        retry_if_exception_type=(RetryableModelError,), stop_after_attempt=2, wait_exponential_jitter=False)
    return runnable.invoke(messages)

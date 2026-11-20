"""通过项目配置连接中转站的 Responses 接口。"""

from langchain_openai import ChatOpenAI

from after_sales.config import Settings, load_settings


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
        max_retries=0,
    )

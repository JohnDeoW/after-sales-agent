"""读取本项目的配置，不搜索工作目录或其他项目的 .env。"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dotenv import dotenv_values


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"
BUSINESS_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Hong_Kong")


def business_today() -> date:
    """演示业务统一按香港时区的自然日计算。"""
    return datetime.now(BUSINESS_TIMEZONE).date()


class ConfigurationError(ValueError):
    """必要的项目配置缺失。"""


@dataclass(frozen=True)
class Settings:
    llm_api_key: str = field(repr=False)
    llm_base_url: str = "https://rightapi.ai/v1"
    llm_model_id: str = "gpt-6.2-sol"
    llm_timeout_seconds: int = 120
    confirmation_ttl_seconds: int = 86400
    approval_ttl_seconds: int = 604800
    # 本地 Phoenix 追踪：默认关闭；隐藏输入输出时 span 中不记录消息与工具参数原文。
    phoenix_enabled: bool = False
    phoenix_endpoint: str = "http://localhost:6006/v1/traces"
    phoenix_project_name: str = "after-sales-agent"
    phoenix_hide_io: bool = False


def load_settings() -> Settings:
    # 不修改进程环境，也不通过变量插值继承其他项目的密钥。
    values = dotenv_values(ENV_FILE, encoding="utf-8-sig", interpolate=False)
    api_key = (values.get("LLM_API_KEY") or "").strip()
    if not api_key:
        raise ConfigurationError(
            f"请在 {ENV_FILE} 中填写 LLM_API_KEY；当前项目尚未配置 API 密钥。"
        )

    def duration(name: str, default: int) -> int:
        try:
            value = int(values.get(name) or default)
            if value <= 0:
                raise ValueError
            return value
        except ValueError:
            raise ConfigurationError(f"{name} 必须是正整数秒。") from None

    def flag(name: str) -> bool:
        value = (values.get(name) or "false").strip().lower()
        if value not in ("true", "false", "1", "0", "yes", "no"):
            raise ConfigurationError(f"{name} 只能是 true 或 false。")
        return value in ("true", "1", "yes")

    return Settings(
        llm_api_key=api_key,
        llm_base_url=(values.get("LLM_BASE_URL") or "https://rightapi.ai/v1").strip(),
        llm_model_id=(values.get("LLM_MODEL_ID") or "gpt-6.2-sol").strip(),
        llm_timeout_seconds=duration("LLM_TIMEOUT_SECONDS", 120),
        confirmation_ttl_seconds=duration("CONFIRMATION_TTL_SECONDS", 86400),
        approval_ttl_seconds=duration("APPROVAL_TTL_SECONDS", 604800),
        phoenix_enabled=flag("PHOENIX_ENABLED"),
        phoenix_endpoint=(values.get("PHOENIX_COLLECTOR_ENDPOINT") or "http://localhost:6006/v1/traces").strip(),
        phoenix_project_name=(values.get("PHOENIX_PROJECT_NAME") or "after-sales-agent").strip(),
        phoenix_hide_io=flag("PHOENIX_HIDE_IO"),
    )

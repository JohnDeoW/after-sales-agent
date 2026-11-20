"""只读工具的会话身份：由运行配置注入，不是模型可填写的参数。

工具函数声明 `config: RunnableConfig` 参数，LangChain 调用时自动注入且不出现在工具参数 schema 中。
工作流在每轮运行配置的 configurable 中放入 session_id 与业务服务；以 "__" 开头的键不会写入检查点元数据。
"""

from langchain_core.runnables import RunnableConfig

SERVICES_KEY = "__after_sales_customers"


class MissingSession(PermissionError):
    """没有服务端会话身份时拒绝查询。"""


def tool_config(customers, session_id: str) -> RunnableConfig:
    """在工作流之外（测试、MCP 服务等）调用工具时使用的运行配置。"""
    return {"configurable": {"session_id": session_id, SERVICES_KEY: customers}}


def scope(config: RunnableConfig | None):
    configurable = (config or {}).get("configurable") or {}
    customers, session_id = configurable.get(SERVICES_KEY), configurable.get("session_id")
    if customers is None or not session_id:
        raise MissingSession("缺少服务端会话身份，拒绝查询。")
    return customers, session_id

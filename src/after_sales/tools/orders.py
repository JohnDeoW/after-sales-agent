"""订单只读工具；身份来自运行配置中的服务端会话，模型不能提供 user_id 或 session_id。"""

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict, Field

from after_sales.tools.scope import scope


class OrderQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: str = Field(min_length=1, description="需要查询的订单编号")


class NoArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


@tool("get_order", args_schema=OrderQuery)
def get_order(order_id: str, config: RunnableConfig) -> dict:
    """查询当前客户的订单和商品项；未找到时请用户核对订单号。"""
    customers, session_id = scope(config)
    return customers.get_order(session_id, order_id)


@tool("list_orders", args_schema=NoArguments)
def list_orders(config: RunnableConfig) -> list[dict]:
    """列出当前客户的订单编号、状态与商品摘要，用于让用户选择订单。"""
    customers, session_id = scope(config)
    return customers.list_orders(session_id)

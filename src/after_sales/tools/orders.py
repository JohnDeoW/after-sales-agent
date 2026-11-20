"""订单工具的身份由后端绑定，不允许模型提供 user_id 或 session_id。"""

from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, ConfigDict, Field

from after_sales.domain.customer_service import CustomerService


class OrderQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: str = Field(min_length=1, description="需要查询的订单编号")


def create_get_order_tool(service: CustomerService, session_id: str) -> BaseTool:
    @tool("get_order", args_schema=OrderQuery)
    def get_order(order_id: str) -> dict:
        """查询当前客户的订单和商品项；未找到时请用户核对订单号。"""
        return service.get_order(session_id, order_id)

    return get_order

"""物流只读工具；客户身份固定绑定到服务端会话。"""

from langchain_core.tools import BaseTool, tool

from after_sales.domain.logistics import LogisticsService
from after_sales.tools.orders import OrderQuery


def create_get_logistics_tool(customers, session_id: str) -> BaseTool:
    @tool("get_logistics", args_schema=OrderQuery)
    def get_logistics(order_id: str) -> dict:
        """查询当前客户指定订单的模拟物流状态与轨迹，不承诺记录之外的送达时间。"""
        return LogisticsService(customers).get_logistics(session_id, order_id)

    return get_logistics

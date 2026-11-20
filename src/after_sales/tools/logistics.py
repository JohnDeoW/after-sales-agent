"""物流只读工具；客户身份来自运行配置中的服务端会话。"""

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from after_sales.domain.logistics import LogisticsService
from after_sales.tools.orders import OrderQuery
from after_sales.tools.scope import scope


@tool("get_logistics", args_schema=OrderQuery)
def get_logistics(order_id: str, config: RunnableConfig) -> dict:
    """查询当前客户指定订单的模拟物流状态与轨迹，不承诺记录之外的送达时间。"""
    customers, session_id = scope(config)
    return LogisticsService(customers).get_logistics(session_id, order_id)

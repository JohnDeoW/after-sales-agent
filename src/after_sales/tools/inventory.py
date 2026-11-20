"""库存只读工具，同款等价候选由业务数据确定。"""

from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict, Field

from after_sales.domain.exchanges import ExchangeService


class InventoryQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: str = Field(min_length=1)
    item_id: str = Field(min_length=1)


def create_get_inventory_tool(customers, session_id):
    @tool("get_inventory", args_schema=InventoryQuery)
    def get_inventory(order_id: str, item_id: str) -> dict:
        """查询本人的原订单项可换的同款等价规格及模拟可用库存；不预留。"""
        return ExchangeService(customers).get_inventory(session_id, order_id, item_id)
    return get_inventory

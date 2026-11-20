"""只读工具白名单：工作流、后续的 Agent 子图与 MCP 服务共用同一组工具。政策检索按索引路径单独创建。"""

from after_sales.tools.inventory import get_inventory
from after_sales.tools.logistics import get_logistics
from after_sales.tools.orders import get_order, list_orders
from after_sales.tools.records import (get_case, get_exchange_request, get_return_request, get_ticket, list_cases,
                                       list_exchange_requests, list_return_requests, list_tickets)

READ_ONLY_TOOLS = {tool.name: tool for tool in (
    get_order, list_orders, get_logistics, get_inventory, get_case, list_cases, get_return_request,
    list_return_requests, get_exchange_request, list_exchange_requests, get_ticket, list_tickets)}

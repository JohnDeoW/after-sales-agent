"""案件、退货申请、换货申请与工单的只读工具；只返回当前会话用户本人的记录。"""

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict, Field

from after_sales.domain.cases import CaseService
from after_sales.domain.exchanges import ExchangeService
from after_sales.domain.return_requests import ReturnRequestService
from after_sales.domain.tickets import TicketService
from after_sales.tools.orders import NoArguments
from after_sales.tools.scope import scope


class CaseQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str = Field(min_length=1, description="案件编号 CASE-…")


class RequestQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=1, description="申请编号：退货 RET-…，换货 EXC-…")


class TicketQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticket_id: str = Field(min_length=1, description="工单编号 TKT-…")


@tool("get_case", args_schema=CaseQuery)
def get_case(case_id: str, config: RunnableConfig) -> dict:
    """查询本人案件的办理进度、当前状态、等待对象与下一步。"""
    customers, session_id = scope(config)
    return CaseService(customers).get(session_id, case_id)


@tool("list_cases", args_schema=NoArguments)
def list_cases(config: RunnableConfig) -> list[dict]:
    """列出本人的售后案件编号与状态。"""
    customers, session_id = scope(config)
    return CaseService(customers).list_cases(session_id)


@tool("get_return_request", args_schema=RequestQuery)
def get_return_request(request_id: str, config: RunnableConfig) -> dict:
    """查询本人退货申请的状态、审批、退回事件与退款操作。"""
    customers, session_id = scope(config)
    return ReturnRequestService(customers).get_request(session_id, request_id)


@tool("list_return_requests", args_schema=NoArguments)
def list_return_requests(config: RunnableConfig) -> list[dict]:
    """列出本人的退货申请。"""
    customers, session_id = scope(config)
    return ReturnRequestService(customers).list_requests(session_id)


@tool("get_exchange_request", args_schema=RequestQuery)
def get_exchange_request(request_id: str, config: RunnableConfig) -> dict:
    """查询本人换货申请的状态、库存预留与换新物流。"""
    customers, session_id = scope(config)
    return ExchangeService(customers).read(session_id, request_id)


@tool("list_exchange_requests", args_schema=NoArguments)
def list_exchange_requests(config: RunnableConfig) -> list[dict]:
    """列出本人的换货申请。"""
    customers, session_id = scope(config)
    return ExchangeService(customers).read(session_id)


@tool("get_ticket", args_schema=TicketQuery)
def get_ticket(ticket_id: str, config: RunnableConfig) -> dict:
    """查询本人人工工单的状态与客服答复。"""
    customers, session_id = scope(config)
    return TicketService(customers).read(session_id, ticket_id)


@tool("list_tickets", args_schema=NoArguments)
def list_tickets(config: RunnableConfig) -> list[dict]:
    """列出本人的人工工单。"""
    customers, session_id = scope(config)
    return TicketService(customers).read(session_id)

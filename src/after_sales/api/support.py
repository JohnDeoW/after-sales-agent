"""本地客服演示身份和审批入口；每次读取与写入都检查服务端角色。"""

from typing import Annotated, Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from after_sales.api.customer import Service, Session, set_session_cookie
from after_sales.domain.approvals import ApprovalService
from after_sales.domain.refunds import RefundService
from after_sales.domain.tickets import TicketService


router = APIRouter()


class SupportSession(BaseModel):
    model_config = ConfigDict(extra="forbid")
    support_id: str


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    proposal_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    decision: Literal["approved", "rejected"]
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]


class SimulatedRefundResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["succeeded", "failed"]
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]


class RefundScenario(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_key: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    scenario: Literal["normal", "success_then_timeout"]


@router.get("/demo/refund-scenarios")
def refund_scenarios(service: Service, session_id: Session) -> list[dict]:
    return RefundService(service).list_scenarios(session_id)


@router.post("/demo/return-requests/{request_id}/refund-scenario")
def select_refund_scenario(request_id: str, body: RefundScenario, service: Service, session_id: Session) -> dict:
    return RefundService(service).select_scenario(session_id, request_id, **body.model_dump())


class TicketReply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_key: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    reply: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


class TicketUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_key: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    status: Literal["in_progress", "replied", "resolved"]
    reply: Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)] = ""


@router.post("/support/tickets/{ticket_id}/updates")
def update_ticket(ticket_id: str, body: TicketUpdate, service: Service, session_id: Session) -> dict:
    return TicketService(service).update(session_id, ticket_id, **body.model_dump())


@router.post("/support/tickets/{ticket_id}/replies")
def reply_ticket(ticket_id: str, body: TicketReply, service: Service, session_id: Session) -> dict:
    return TicketService(service).reply(session_id, ticket_id, **body.model_dump())


@router.get("/demo/support-users")
def support_users(service: Service) -> list[dict]:
    return ApprovalService(service).list_staff()


@router.post("/demo/support-sessions", status_code=201)
def support_session(body: SupportSession, response: Response, service: Service) -> dict:
    session_id, user = ApprovalService(service).create_session(body.support_id)
    set_session_cookie(response, session_id, "support")
    return {"user": user}


@router.get("/support/approvals")
def pending_approvals(service: Service, session_id: Session) -> list[dict]:
    return ApprovalService(service).pending(session_id)


@router.get("/support/tickets")
def support_tickets(service: Service, session_id: Session) -> list[dict]:
    return TicketService(service).read(session_id, support=True)


@router.get("/support/tickets/{ticket_id}")
def support_ticket(ticket_id: str, service: Service, session_id: Session) -> dict:
    return TicketService(service).read(session_id, ticket_id, support=True)


@router.get("/support/approvals/{request_id}")
def approval_details(request_id: str, service: Service, session_id: Session) -> dict:
    return ApprovalService(service).get(session_id, request_id)


@router.post("/support/approvals/{request_id}")
def decide_approval(request_id: str, body: ApprovalDecision, request: Request, service: Service, session_id: Session) -> dict:
    result = ApprovalService(service).decide(session_id, request_id, **body.model_dump())
    # 审批结论已在业务事务中保存，再恢复正在等待这份审批的客户会话（interrupt）。
    if result.get("approval") and not result.get("reused"):
        request.app.state.workflow.resume_approval(request_id, body.decision)
    return result


@router.post("/demo/refund-operations/{operation_id}/result")
def simulated_refund_result(operation_id: str, body: SimulatedRefundResult, service: Service, session_id: Session) -> dict:
    return RefundService(service).set_simulated_result(session_id, operation_id, body.status, body.reason)

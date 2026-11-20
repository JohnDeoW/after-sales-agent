"""本地模拟客户入口、会话、消息与订单查询。"""

from typing import Annotated
import json
from queue import Queue, Empty
from threading import Thread

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, StringConstraints, Field

from after_sales.domain.customer_service import CustomerService, InvalidSession
from after_sales.domain.proposals import ProposalService
from after_sales.domain.return_requests import ReturnRequestService
from after_sales.domain.cases import CaseService
from after_sales.domain.refunds import RefundService
from after_sales.domain.tickets import TicketService
from after_sales.domain.logistics import LogisticsService
from after_sales.domain.exchanges import ExchangeService
from after_sales.storage.workflow import get_traces


router = APIRouter()
SESSION_COOKIE = "after_sales_session"


class CreateSession(BaseModel):
    model_config = ConfigDict(extra="forbid")
    customer_id: str


class SendMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]


class ProposalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)


class CreateReturnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmation_id: str = Field(min_length=1)


class IssueRefund(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_key: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


def get_service(request: Request) -> CustomerService:
    return request.app.state.customer_service


DEMO_ROLES = ("customer", "support", "warehouse")


def set_session_cookie(response: Response, session_id: str, role: str) -> None:
    """同时写入通用会话和该角色的会话；不同标签页按角色选择各自的会话，互不登出。"""
    response.set_cookie(SESSION_COOKIE, session_id, httponly=True, samesite="strict")
    response.set_cookie(f"{SESSION_COOKIE}_{role}", session_id, httponly=True, samesite="strict")


def get_session_id(request: Request) -> str:
    # 请求头只决定读取浏览器里哪一个已有会话；会话对应的用户与权限仍由服务端校验。
    role = request.headers.get("X-Demo-Role")
    session_id = request.cookies.get(f"{SESSION_COOKIE}_{role}") if role in DEMO_ROLES else None
    session_id = session_id or (None if role in DEMO_ROLES else request.cookies.get(SESSION_COOKIE))
    if not session_id:
        raise InvalidSession("请先选择模拟客户创建会话。")
    return session_id


Service = Annotated[CustomerService, Depends(get_service)]
Session = Annotated[str, Depends(get_session_id)]


@router.get("/demo/customers")
def customers(service: Service) -> list[dict]:
    """仅供本地演示选择模拟客户。"""
    return service.list_customers()


@router.post("/sessions", status_code=201)
def create_session(body: CreateSession, response: Response, service: Service) -> dict:
    session_id = service.create_session(body.customer_id)
    set_session_cookie(response, session_id, "customer")
    return {"user": service.current_user(session_id)}


@router.get("/sessions/current")
def current_session(service: Service, session_id: Session) -> dict:
    return {"user": service.current_user(session_id)}


@router.post("/messages", status_code=201)
def send_message(body: SendMessage, request: Request, session_id: Session) -> dict:
    """识别诉求、补全退货信息，并保存本轮用户消息和助手回复。"""
    return request.app.state.workflow.receive(session_id, body.content)


@router.post("/messages/stream")
def stream_message(body: SendMessage, request: Request, service: Service, session_id: Session):
    service.current_user(session_id)  # 发送响应头前验证身份。
    queue = Queue()

    def execute():
        # 在独立线程中消费 LangGraph 流：浏览器断开时这一轮仍会跑完，不会中断已开始的写入。
        try:
            for event in request.app.state.workflow.stream(session_id, body.content):
                queue.put(event)
        except Exception as exc:
            # 项目自身的固定提示可以展示（如模型超时、预算停止）；未知异常文本可能含上游数据，只给通用提示。
            from after_sales.agent.intake import IntakeUnavailable
            from after_sales.domain.proposals import ProposalConflict, ProposalNotFound
            from after_sales.workflow.runtime import WorkflowStopped
            from after_sales.agent.mcp_tools import MCPUnavailable
            known = isinstance(exc, (IntakeUnavailable, WorkflowStopped, ProposalConflict, ProposalNotFound, MCPUnavailable))
            queue.put({"type": "error", "detail": str(exc) if known else "本轮处理未完成，请刷新记录核对结果。",
                       "workflow": getattr(exc, "workflow", None)})
        finally:
            queue.put(None)

    def events():
        # 浏览器断开后允许正在执行的业务完成，不重发或中断已提交写入。
        Thread(target=execute, daemon=True).start()
        while True:
            try:
                item = queue.get(timeout=10)
            except Empty:
                yield ": keepalive\n\n"
                continue
            if item is None:
                break
            yield "data: " + json.dumps(item, ensure_ascii=False) + "\n\n"

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/messages")
def messages(service: Service, session_id: Session) -> list[dict]:
    return service.list_messages(session_id)


@router.get("/conversation")
def conversation(service: Service, session_id: Session) -> dict:
    return service.conversation(session_id)


@router.get("/orders")
def orders(service: Service, session_id: Session) -> list[dict]:
    return service.list_orders(session_id)


@router.get("/orders/{order_id}")
def order(order_id: str, service: Service, session_id: Session) -> Response:
    result = service.get_order(session_id, order_id)
    return JSONResponse(result, status_code=200 if result["status"] == "ok" else 404)


@router.get("/proposals/current")
def current_proposal(service: Service, session_id: Session) -> dict:
    return ProposalService(service).current(session_id)


@router.get("/orders/{order_id}/items/{item_id}/inventory")
def inventory(order_id: str, item_id: str, service: Service, session_id: Session) -> dict:
    return ExchangeService(service).get_inventory(session_id, order_id, item_id)


@router.get("/exchange-proposals/{proposal_id}")
def exchange_proposal(proposal_id: str, service: Service, session_id: Session) -> dict:
    return ExchangeService(service).proposal(session_id, proposal_id)


@router.get("/orders/{order_id}/logistics")
def logistics(order_id: str, service: Service, session_id: Session) -> Response:
    result = LogisticsService(service).get_logistics(session_id, order_id)
    return JSONResponse(result, status_code=200 if result["status"] == "ok" else 404)


@router.get("/cases")
def cases(service: Service, session_id: Session) -> list[dict]:
    return CaseService(service).list_cases(session_id)


@router.get("/tickets")
def tickets(service: Service, session_id: Session) -> list[dict]:
    return TicketService(service).read(session_id)


@router.get("/tickets/{ticket_id}")
def ticket(ticket_id: str, service: Service, session_id: Session) -> dict:
    return TicketService(service).read(session_id, ticket_id)


@router.get("/cases/{case_id}")
def case_progress(case_id: str, service: Service, session_id: Session) -> dict:
    return CaseService(service).get(session_id, case_id)


@router.post("/cases/{case_id}/resume")
def resume_case(case_id: str, service: Service, session_id: Session) -> dict:
    return CaseService(service).resume(session_id, case_id)


@router.post("/proposals/{proposal_id}/confirm")
def confirm_proposal(proposal_id: str, body: ProposalDecision, request: Request, session_id: Session) -> dict:
    return request.app.state.workflow.receive(session_id, f"确认方案 {proposal_id} v{body.version}",
                                               command={"action": "confirm", "proposal_id": proposal_id, "version": body.version})


@router.post("/proposals/{proposal_id}/cancel")
def cancel_proposal(proposal_id: str, body: ProposalDecision, request: Request, session_id: Session) -> dict:
    return request.app.state.workflow.receive(session_id, f"取消方案 {proposal_id} v{body.version}",
                                               command={"action": "cancel", "proposal_id": proposal_id, "version": body.version})


@router.post("/return-requests")
def create_return_request(body: CreateReturnRequest, request: Request, session_id: Session) -> dict:
    return request.app.state.workflow.receive(session_id, f"提交申请 {body.confirmation_id}",
                                               command={"action": "create_request", "confirmation_id": body.confirmation_id})


@router.get("/return-requests")
def return_requests(service: Service, session_id: Session) -> list[dict]:
    return ReturnRequestService(service).list_requests(session_id)


@router.get("/return-requests/{request_id}")
def return_request(request_id: str, service: Service, session_id: Session) -> dict:
    return ReturnRequestService(service).get_request(session_id, request_id)


@router.post("/return-requests/{request_id}/revalidate")
def revalidate_return(request_id: str, service: Service, session_id: Session) -> dict:
    return ReturnRequestService(service).revalidate(session_id, request_id)


@router.post("/return-requests/{request_id}/refund")
def issue_refund(request_id: str, body: IssueRefund, service: Service, session_id: Session) -> dict:
    return RefundService(service).issue_refund(session_id, request_id, body.operation_key)


@router.get("/refund-operations/{operation_id}")
def refund_operation(operation_id: str, service: Service, session_id: Session) -> dict:
    return RefundService(service).get_operation_status(session_id, operation_id)


@router.post("/refund-operations/{operation_id}/check")
def check_refund_operation(operation_id: str, service: Service, session_id: Session) -> dict:
    return RefundService(service).get_operation_status(session_id, operation_id, refresh=True)


@router.get("/workflow-runs")
def workflow_runs(service: Service, session_id: Session) -> list[dict]:
    """当前会话最近 20 轮轨迹，包含失败和预算停止记录。"""
    return get_traces(service.db_path, session_id)


@router.get("/workflow-runs/{run_id}")
def workflow_run(run_id: str, service: Service, session_id: Session) -> dict:
    return get_traces(service.db_path, session_id, run_id)

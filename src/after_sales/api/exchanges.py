"""客户换货与仓库模拟事件入口，复用既有角色及事件输入模型。"""

from fastapi import APIRouter, Request

from after_sales.api.customer import Service, Session, ProposalDecision
from after_sales.api.return_events import EventOperation, Shipment, Inspection
from after_sales.domain.exchanges import ExchangeService


router = APIRouter()


@router.post("/exchange-proposals/{proposal_id}/confirm")
def confirm_exchange(proposal_id: str, body: ProposalDecision, request: Request, session_id: Session):
    return request.app.state.workflow.receive(session_id, f"确认换货 {proposal_id} v{body.version}",
        command={"action": "confirm_exchange", "proposal_id": proposal_id, "version": body.version})


@router.get("/exchange-requests")
def exchange_requests(service: Service, session_id: Session):
    return ExchangeService(service).read(session_id)


@router.get("/exchange-requests/{request_id}")
def exchange_request(request_id: str, service: Service, session_id: Session):
    return ExchangeService(service).read(session_id, request_id)


@router.post("/exchange-requests/{request_id}/shipment")
def shipment(request_id: str, body: Shipment, service: Service, session_id: Session):
    return ExchangeService(service).record(session_id, request_id, "shipment", body.operation_key, body.model_dump(exclude={"operation_key"}))


@router.post("/exchange-requests/{request_id}/cancel")
def cancel(request_id: str, body: EventOperation, service: Service, session_id: Session):
    return ExchangeService(service).record(session_id, request_id, "cancel", body.operation_key, {})


@router.get("/warehouse/exchange-requests")
def warehouse_requests(service: Service, session_id: Session):
    return ExchangeService(service).read(session_id, warehouse=True)


@router.get("/warehouse/exchange-requests/{request_id}")
def warehouse_request(request_id: str, service: Service, session_id: Session):
    return ExchangeService(service).read(session_id, request_id, warehouse=True)


@router.post("/warehouse/exchange-requests/{request_id}/receipt")
def receipt(request_id: str, body: EventOperation, service: Service, session_id: Session):
    return ExchangeService(service).record(session_id, request_id, "receipt", body.operation_key, {})


@router.post("/warehouse/exchange-requests/{request_id}/inspection")
def inspection(request_id: str, body: Inspection, service: Service, session_id: Session):
    return ExchangeService(service).record(session_id, request_id, "inspection", body.operation_key, body.model_dump(exclude={"operation_key"}))


@router.post("/warehouse/exchange-requests/{request_id}/dispatch")
def dispatch(request_id: str, body: Shipment, service: Service, session_id: Session):
    return ExchangeService(service).record(session_id, request_id, "dispatch", body.operation_key, body.model_dump(exclude={"operation_key"}))


@router.post("/warehouse/exchange-requests/{request_id}/delivery")
def delivery(request_id: str, body: EventOperation, service: Service, session_id: Session):
    return ExchangeService(service).record(session_id, request_id, "delivery", body.operation_key, {})

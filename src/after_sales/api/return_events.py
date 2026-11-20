"""模拟寄件、仓库签收与检验接口。"""

from typing import Annotated, Literal

from fastapi import APIRouter, Response
from pydantic import BaseModel, ConfigDict, StringConstraints

from after_sales.api.customer import Service, Session, set_session_cookie
from after_sales.domain.return_events import ReturnEventService


router = APIRouter()
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class WarehouseSession(BaseModel):
    model_config = ConfigDict(extra="forbid")
    warehouse_id: str


class EventOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_key: ShortText


class Shipment(EventOperation):
    carrier: ShortText
    tracking_number: ShortText


class Inspection(EventOperation):
    result: Literal["passed", "failed"]
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]


@router.get("/demo/warehouse-users")
def warehouse_users(service: Service) -> list[dict]:
    return ReturnEventService(service).list_warehouses()


@router.post("/demo/warehouse-sessions", status_code=201)
def warehouse_session(body: WarehouseSession, response: Response, service: Service) -> dict:
    session_id, user = ReturnEventService(service).create_session(body.warehouse_id)
    set_session_cookie(response, session_id, "warehouse")
    return {"user": user}


@router.post("/return-requests/{request_id}/shipment")
def shipment(request_id: str, body: Shipment, service: Service, session_id: Session) -> dict:
    return ReturnEventService(service).record(session_id, request_id, "shipment", body.operation_key,
                                             body.model_dump(exclude={"operation_key"}))


@router.get("/warehouse/return-requests")
def warehouse_requests(service: Service, session_id: Session) -> list[dict]:
    return ReturnEventService(service).list_requests(session_id)


@router.get("/warehouse/return-requests/{request_id}")
def warehouse_request(request_id: str, service: Service, session_id: Session) -> dict:
    return ReturnEventService(service).get(session_id, request_id)


@router.post("/warehouse/return-requests/{request_id}/receipt")
def receipt(request_id: str, body: EventOperation, service: Service, session_id: Session) -> dict:
    return ReturnEventService(service).record(session_id, request_id, "receipt", body.operation_key, {})


@router.post("/warehouse/return-requests/{request_id}/inspection")
def inspection(request_id: str, body: Inspection, service: Service, session_id: Session) -> dict:
    return ReturnEventService(service).record(session_id, request_id, "inspection", body.operation_key,
                                             body.model_dump(exclude={"operation_key"}))

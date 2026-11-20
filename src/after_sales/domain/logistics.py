"""只读取当前客户订单的物流，未知进度不推算送达日期。"""

import json
import hashlib

from after_sales.domain.customer_service import _session_user
from after_sales.storage.database import connect_database
from after_sales.storage.database import read_order
from after_sales.storage.tickets import create_ticket
from after_sales.domain.proposals import ProposalNotFound, ProposalConflict


def read_logistics(connection, order_id):
    row = connection.execute("SELECT * FROM logistics WHERE order_id = ?", (order_id,)).fetchone()
    if row:
        shipment = dict(row)
        shipment["events"] = sorted(json.loads(shipment.pop("events_json")), key=lambda e: e["occurred_at"])
    else:
        shipment = {"order_id": order_id, "status": "unknown", "carrier": None, "tracking_number": None,
                    "estimated_delivery_at": None, "events": []}
    shipment.update(status_label=LABELS[shipment["status"]], simulated=True)
    return shipment


def create_investigation(connection, user_id, order_id, statement, original_message):
    order = read_order(connection, order_id)
    if not order or order["user_id"] != user_id:
        raise ProposalNotFound("未找到当前用户的订单，请核对订单号。")
    shipment = read_logistics(connection, order_id)
    if shipment["status"] not in ("delivered", "lost_pending"):
        raise ProposalConflict("当前物流不是签收争议或丢件待查状态，请重新查询物流或联系人工。")
    fingerprint = hashlib.sha256(statement.strip().encode()).hexdigest()
    return create_ticket(connection, user_id=user_id, source="logistics_dispute",
                         operation_key=f"logistics:{user_id}:{order_id}:{fingerprint}",
                         reason="物流争议待调查，尚未认定责任",
                         evidence={"order": order, "logistics_record": shipment,
                                   "customer_statement": statement, "original_message": original_message,
                                   "responsibility": "undetermined"},
                         todo=["核对承运商轨迹、签收凭证与客户陈述", "联系相关方调查并答复结果，不自动认定丢件或责任"])


LABELS = {"in_transit": "正常运输中", "delayed": "运输延迟", "lost_pending": "丢件待查",
          "delivered": "物流显示已签收", "unknown": "物流状态未知"}


class LogisticsService:
    def __init__(self, customers):
        self.db_path = customers.db_path

    def get_logistics(self, session_id, order_id):
        with connect_database(self.db_path) as connection:
            connection.execute("BEGIN")
            user = _session_user(connection, session_id)
            order = connection.execute("SELECT id FROM orders WHERE id = ? AND user_id = ?", (order_id, user["id"])).fetchone()
            if order is None:
                return {"status": "not_found", "message": "未找到当前用户的订单，请核对订单号。"}
            return {"status": "ok", "logistics": read_logistics(connection, order_id)}


def logistics_text(shipment):
    text = f"订单 {shipment['order_id']} 的模拟物流：{shipment['status_label']}。"
    if shipment["tracking_number"]:
        text += f"承运商：{shipment['carrier']}，运单号：{shipment['tracking_number']}。"
    events = shipment["events"][-3:]
    text += "最近轨迹：" + "；".join(f"{e['occurred_at']} {e['description']}" for e in events) + "。" if events else "暂无可查询的物流轨迹。"
    if shipment["status"] == "delivered":
        text += "这表示承运商记录已签收，不代表已核实本人收货。"
    elif shipment["estimated_delivery_at"]:
        text += f"物流记录中的预计送达时间：{shipment['estimated_delivery_at']}（仅为预计）。"
    else:
        text += "暂无已确认的预计送达时间。"
    if shipment["status"] == "lost_pending":
        text += "仍待核查，不能据此认定包裹已丢失。"
    return text

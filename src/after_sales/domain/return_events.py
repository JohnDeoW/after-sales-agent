"""客户寄件与仓库事实事件；状态推进在事务中完成，不操作退款账本。"""

import json
import secrets

from after_sales.domain.customer_service import InvalidCustomer, InvalidSession, _session_user
from after_sales.domain.proposals import ProposalConflict, ProposalNotFound
from after_sales.domain.return_requests import REQUEST_SELECT, request_view, revalidate_request
from after_sales.storage.database import connect_database
from after_sales.storage.return_events import event_view
from after_sales.storage.tickets import create_request_ticket


class WarehouseRequired(ValueError):
    pass


def warehouse_user(connection, session_id):
    user = connection.execute(
        "SELECT u.id, u.display_name, u.role FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.id = ?",
        (session_id,),
    ).fetchone()
    if user is None:
        raise InvalidSession("请先选择模拟仓库身份。")
    if user["role"] != "warehouse":
        raise WarehouseRequired("只有仓库会话可以签收和登记检验结果。")
    return dict(user)


class ReturnEventService:
    def __init__(self, customers):
        self.db_path = customers.db_path

    def list_warehouses(self):
        with connect_database(self.db_path) as connection:
            return [dict(row) for row in connection.execute("SELECT id, display_name FROM users WHERE role = 'warehouse' ORDER BY id")]

    def create_session(self, warehouse_id):
        """本地演示身份选择，不代表生产认证。"""
        with connect_database(self.db_path) as connection:
            if connection.execute("SELECT id FROM users WHERE id = ? AND role = 'warehouse'", (warehouse_id,)).fetchone() is None:
                raise InvalidCustomer("请选择有效的模拟仓库身份。")
            session_id = secrets.token_urlsafe(32)
            connection.execute("INSERT INTO sessions (id, user_id) VALUES (?, ?)", (session_id, warehouse_id))
            return session_id, warehouse_user(connection, session_id)

    def list_requests(self, session_id):
        with connect_database(self.db_path) as connection:
            connection.execute("BEGIN")
            warehouse_user(connection, session_id)
            return [self._request(connection, row["id"]) for row in connection.execute(
                "SELECT id FROM return_requests ORDER BY rowid DESC")]

    def get(self, session_id, request_id):
        with connect_database(self.db_path) as connection:
            warehouse_user(connection, session_id)
            return self._request(connection, request_id)

    @staticmethod
    def _request(connection, request_id, user_id=None):
        owned = connection.execute(
            """SELECT c.user_id FROM return_requests r JOIN confirmations f ON f.id = r.confirmation_id
               JOIN proposals p ON p.id = f.proposal_id JOIN cases c ON c.id = p.case_id
               WHERE r.id = ? AND c.user_id = f.user_id""", (request_id,),
        ).fetchone()
        if owned is None or (user_id is not None and user_id != owned["user_id"]):
            raise ProposalNotFound("未找到可操作的退货申请。")
        row = connection.execute(REQUEST_SELECT + " AND r.id = ?", (owned["user_id"], owned["user_id"], request_id)).fetchone()
        return request_view(row, connection)

    def record(self, session_id, request_id, kind, operation_key, payload):
        with connect_database(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            user = _session_user(connection, session_id) if kind == "shipment" else warehouse_user(connection, session_id)
            request = self._request(connection, request_id, user["id"] if kind == "shipment" else None)
            prior = connection.execute("SELECT * FROM return_events WHERE operation_key = ?", (operation_key,)).fetchone()
            if prior is None:
                prior = connection.execute("SELECT * FROM return_events WHERE request_id = ? AND kind = ?", (request_id, kind)).fetchone()
            if prior:
                if (prior["request_id"] != request_id or prior["kind"] != kind or prior["actor_id"] != user["id"]
                        or json.loads(prior["payload_json"]) != payload):
                    raise ProposalConflict("操作编号已使用或此阶段已有不同记录，不能覆盖原事件。")
                return {"event": event_view(prior), "return_request": request, "reused": True}
            expected = {"shipment": "awaiting_return", "receipt": "in_transit", "inspection": "awaiting_inspection"}[kind]
            if request["status"] != expected:
                raise ProposalConflict(f"当前状态 {request['status_label']} 不允许此操作，请按寄件、签收、检验顺序办理。")
            if kind == "shipment":
                checked = revalidate_request(connection, request_id)
                if not checked["authorization_valid"]:
                    return checked
                from after_sales.domain.item_capacity import ensure_item_capacity
                ensure_item_capacity(connection, request["order_item_id"], request["quantity"], return_id=request_id,
                                     amount_cents=request["refund_amount_cents"])
            status = {"shipment": "in_transit", "receipt": "awaiting_inspection",
                      "inspection": "ready_for_refund" if payload.get("result") == "passed" else "manual_review_required"}[kind]
            event_id = "EVT-" + secrets.token_hex(8)
            connection.execute(
                "INSERT INTO return_events (id, request_id, kind, actor_id, operation_key, payload_json) VALUES (?, ?, ?, ?, ?, ?)",
                (event_id, request_id, kind, user["id"], operation_key, json.dumps(payload, ensure_ascii=False)),
            )
            connection.execute("UPDATE return_requests SET status = ? WHERE id = ?", (status, request_id))
            result = self._request(connection, request_id)
            summary = f"申请 {request_id}：{result['status_label']}。{result['next_step']}"
            connection.execute("UPDATE cases SET status = ?, summary = ? WHERE id = ?", (status, summary, request["case_id"]))
            connection.execute("UPDATE case_progress SET revision = revision + 1 WHERE case_id = ?", (request["case_id"],))
            if kind == "inspection" and payload["result"] == "failed":
                create_request_ticket(connection, request_id, "inspection_failed", payload["reason"],
                                      ["核对寄件、签收和检验记录", "联系客户核实争议并决定实物处理方式；不得自动退款"])
                result = self._request(connection, request_id)
            event = connection.execute("SELECT * FROM return_events WHERE id = ?", (event_id,)).fetchone()
            return {"event": event_view(event), "return_request": result, "reused": False}

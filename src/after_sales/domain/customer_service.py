"""客户会话与订单访问；身份来自服务端会话记录。"""

import secrets
import sqlite3
from pathlib import Path

from after_sales.storage.database import DEMO_DB_PATH, connect_database, read_order, message_view
from after_sales.domain.message_cards import live_status


class InvalidSession(ValueError):
    """会话不存在，需重新选择演示身份。"""


class InvalidCustomer(ValueError):
    """只能以现有演示客户建立客户会话。"""


def _session_user(connection: sqlite3.Connection, session_id: str) -> dict:
    row = connection.execute(
        """SELECT u.id, u.display_name, u.role FROM sessions s
           JOIN users u ON u.id = s.user_id
           WHERE s.id = ? AND u.role = 'customer'""", (session_id,),
    ).fetchone()
    if row is None:
        raise InvalidSession("会话无效，请重新选择模拟客户创建会话。")
    return dict(row)


class CustomerService:
    def __init__(self, db_path: Path = DEMO_DB_PATH) -> None:
        self.db_path = db_path
        self.confirmation_ttl_seconds = 86400
        self.approval_ttl_seconds = 604800

    def list_customers(self) -> list[dict]:
        """本地演示入口允许选择客户，不代表生产环境登录认证。"""
        with connect_database(self.db_path) as connection:
            rows = connection.execute(
                "SELECT id, display_name FROM users WHERE role = 'customer' ORDER BY id"
            ).fetchall()
            return [dict(row) for row in rows]

    def create_session(self, customer_id: str) -> str:
        with connect_database(self.db_path) as connection:
            customer = connection.execute(
                "SELECT id FROM users WHERE id = ? AND role = 'customer'", (customer_id,),
            ).fetchone()
            if customer is None:
                raise InvalidCustomer("请选择有效的模拟客户。")
            session_id = secrets.token_urlsafe(32)
            connection.execute(
                "INSERT INTO sessions (id, user_id) VALUES (?, ?)", (session_id, customer_id),
            )
            return session_id

    def current_user(self, session_id: str) -> dict:
        with connect_database(self.db_path) as connection:
            return _session_user(connection, session_id)

    def list_orders(self, session_id: str) -> list[dict]:
        with connect_database(self.db_path) as connection:
            user = _session_user(connection, session_id)
            rows = connection.execute(
                "SELECT * FROM orders WHERE user_id = ? ORDER BY id", (user["id"],),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_order(self, session_id: str, order_id: str) -> dict:
        with connect_database(self.db_path) as connection:
            user = _session_user(connection, session_id)
            owned = connection.execute(
                "SELECT id FROM orders WHERE id = ? AND user_id = ?", (order_id, user["id"]),
            ).fetchone()
            # 他人订单与不存在订单返回相同内容，不暴露归属或订单是否存在。
            if owned is None:
                return {"status": "not_found", "message": "未找到当前用户的订单，请核对订单号。"}
            return {"status": "ok", "order": read_order(connection, order_id)}

    def send_message(self, session_id: str, content: str) -> dict:
        with connect_database(self.db_path) as connection:
            _session_user(connection, session_id)
            cursor = connection.execute(
                "INSERT INTO messages (session_id, role, content) VALUES (?, 'user', ?)",
                (session_id, content),
            )
            row = connection.execute(
                "SELECT id, role, content, created_at FROM messages WHERE id = ?", (cursor.lastrowid,),
            ).fetchone()
            return dict(row)

    def list_messages(self, session_id: str) -> list[dict]:
        return self.conversation(session_id)["messages"]

    def sessions_for_case(self, case_id: str) -> list[str]:
        """当前打开该案件的会话（内部使用，用于恢复等待审批的会话）。"""
        with connect_database(self.db_path) as connection:
            rows = connection.execute("SELECT session_id FROM conversation_states WHERE case_id = ?", (case_id,))
            return [row["session_id"] for row in rows]

    def conversation(self, session_id: str) -> dict:
        """页面恢复用的会话快照；消息和当前案件来自同一次只读事务。"""
        with connect_database(self.db_path) as connection:
            connection.execute("BEGIN")
            user = _session_user(connection, session_id)
            active = connection.execute(
                """SELECT c.id FROM conversation_states s JOIN cases c ON c.id = s.case_id
                   WHERE s.session_id = ? AND c.user_id = ?""", (session_id, user["id"]),
            ).fetchone()
            rows = connection.execute(
                "SELECT id, role, content, created_at, card_json FROM messages WHERE session_id = ? ORDER BY id",
                (session_id,),
            ).fetchall()
            messages = [message_view(row) for row in rows]
            for message in messages:
                if message["card"] and message["card"]["type"] in ("return_proposal", "exchange_proposal", "record"):
                    # 卡片是当时的快照；附上方案当前状态，失效卡片由页面停用按钮，后端仍会拒绝旧版本。
                    message["card"]["live_status"] = live_status(connection, message["card"])
            return {"case_id": active["id"] if active else None, "messages": messages}

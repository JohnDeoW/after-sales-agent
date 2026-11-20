"""数据库路径固定在本项目；正常连接不会隐式创建空数据库。"""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from after_sales.config import PROJECT_ROOT


DEMO_DB_PATH = PROJECT_ROOT / "data" / "demo.sqlite3"
SCHEMA_PATH = Path(__file__).with_name("schema.sql")
SESSION_SCHEMA_PATH = Path(__file__).with_name("session_schema.sql")


def initialize_session_storage(path: Path = DEMO_DB_PATH) -> None:
    """为已有演示库补充会话表，保留原订单、案件和办理记录。"""
    if not path.is_file():
        raise RuntimeError("演示数据库不存在，请先运行 uv run after-sales-init-db。")
    with connect_database(path) as connection:
        connection.executescript(SESSION_SCHEMA_PATH.read_text(encoding="utf-8"))
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(proposals)")}
        if "details_json" not in columns:
            connection.execute("ALTER TABLE proposals ADD COLUMN details_json TEXT NOT NULL DEFAULT '{}'")
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(messages)")}
        if "card_json" not in columns:
            connection.execute("ALTER TABLE messages ADD COLUMN card_json TEXT")
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(conversation_states)")}
        if "history_start_id" not in columns:
            connection.execute("ALTER TABLE conversation_states ADD COLUMN history_start_id INTEGER NOT NULL DEFAULT 0")
        for table in ("confirmations", "approvals"):
            columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
            for name, definition in (("expires_at", "TEXT"), ("invalidated_at", "TEXT"),
                                     ("invalidation_reason", "TEXT"), ("snapshot_json", "TEXT NOT NULL DEFAULT '{}'")):
                if name not in columns:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
        from after_sales.storage.case_progress import backfill_progress
        backfill_progress(connection)
        from after_sales.storage.logistics import initialize_logistics
        initialize_logistics(connection)
        from after_sales.storage.inventory import initialize_inventory
        initialize_inventory(connection)


@contextmanager
def connect_database(path: Path = DEMO_DB_PATH) -> Iterator[sqlite3.Connection]:
    # 本地业务工具等待数据库锁最多 5 秒，不无限阻塞办理流程。
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def read_order(connection: sqlite3.Connection, order_id: str) -> dict | None:
    """内部存储查询；用户身份校验和对外工具由后续任务实现。"""
    order = connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if order is None:
        return None
    items = connection.execute(
        "SELECT * FROM order_items WHERE order_id = ? ORDER BY id", (order_id,)
    ).fetchall()
    return {**dict(order), "items": [dict(item) for item in items]}


def append_messages(connection: sqlite3.Connection, session_id: str, content: str, reply: str,
                    card: dict | None = None) -> dict:
    """保存用户消息与助手回复；card 是助手回复附带的结构化卡片快照。"""
    saved = []
    for role, text, card_json in (("user", content, None),
                                  ("assistant", reply, json.dumps(card, ensure_ascii=False) if card else None)):
        cursor = connection.execute(
            "INSERT INTO messages (session_id, role, content, card_json) VALUES (?, ?, ?, ?)",
            (session_id, role, text, card_json),
        )
        saved.append(message_view(connection.execute(
            "SELECT id, role, content, created_at, card_json FROM messages WHERE id = ?", (cursor.lastrowid,),
        ).fetchone()))
    return {"message": saved[0], "reply": saved[1]}


def message_view(row) -> dict:
    message = dict(row)
    raw = message.pop("card_json", None)
    message["card"] = json.loads(raw) if raw else None
    return message

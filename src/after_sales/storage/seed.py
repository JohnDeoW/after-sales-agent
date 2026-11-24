"""创建合成演示数据；不调用模型，也不连接任何真实商家系统。"""

import argparse
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from after_sales.config import business_today

from after_sales.storage.database import DEMO_DB_PATH, SCHEMA_PATH, connect_database


def seed_demo(connection: sqlite3.Connection, as_of: date) -> None:
    """以指定业务日期生成固定编号的演示场景，日期存 ISO 格式。"""
    connection.executemany(
        "INSERT INTO users (id, display_name, role) VALUES (?, ?, ?)",
        [
            ("customer-001", "演示客户小林", "customer"),
            ("customer-002", "演示客户小陈", "customer"),
            ("staff-001", "演示客服", "support"),
            ("warehouse-001", "演示仓库操作员", "warehouse"),
        ],
    )
    # 编号依次对应普通、多商品、超期、质量原因、他人订单、已有申请。
    orders = [
        ("ORD-DEMO-001", "customer-001", 2),
        ("ORD-DEMO-002", "customer-001", 3),
        ("ORD-DEMO-003", "customer-001", 10),
        ("ORD-DEMO-004", "customer-001", 20),
        ("ORD-DEMO-005", "customer-002", 2),
        ("ORD-DEMO-006", "customer-001", 2),
    ]
    connection.executemany(
        "INSERT INTO orders (id, user_id, status, ordered_on, signed_on) VALUES (?, ?, ?, ?, ?)",
        [
            (order_id, user_id, "delivered",
             (as_of - timedelta(days=days + 3)).isoformat(),
             (as_of - timedelta(days=days)).isoformat())
            for order_id, user_id, days in orders
        ],
    )
    # 演示：部分订单下单时购买了退货运费险，政策咨询可据此回答寄回运费。
    connection.executemany("UPDATE orders SET shipping_insurance = 1 WHERE id = ?", [("ORD-DEMO-001",), ("ORD-DEMO-004",)])
    connection.executemany(
        """INSERT INTO order_items
           (id, order_id, sku, product_name, specification, quantity, paid_amount_cents)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        [
            ("ITEM-001", "ORD-DEMO-001", "MOUSE-BLACK", "无线鼠标", "黑色", 1, 19900),
            ("ITEM-002", "ORD-DEMO-002", "KEYBOARD-WHITE", "键盘", "白色", 1, 29900),
            ("ITEM-003", "ORD-DEMO-002", "CABLE-1M", "数据线", "1 米", 3, 10001),
            ("ITEM-004", "ORD-DEMO-003", "STAND-SILVER", "电脑支架", "银色", 1, 12900),
            ("ITEM-005", "ORD-DEMO-004", "HEADPHONE-BLACK", "耳机", "黑色", 1, 69900),
            ("ITEM-006", "ORD-DEMO-005", "MOUSE-BLACK", "无线鼠标", "黑色", 1, 19900),
            ("ITEM-007", "ORD-DEMO-006", "PAD-GREY", "鼠标垫", "灰色", 1, 3900),
        ],
    )
    # 质量问题只是用户报告，尚未由客服确认责任或批准退款。
    connection.executemany(
        """INSERT INTO cases
           (id, user_id, order_id, order_item_id, intent, status, quantity, reason, opened, summary)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            ("CASE-DEMO-001", "customer-001", "ORD-DEMO-004", "ITEM-005", "return",
             "collecting_information", 1, "用户报告耳机无法开机", 1, "质量原因演示，等待补充故障描述"),
            ("CASE-DEMO-002", "customer-001", "ORD-DEMO-006", "ITEM-007", "return",
             "waiting_return", 1, "不再需要", 0, "已确认退货方案并受理申请，尚未退款"),
        ],
    )
    connection.execute(
        """INSERT INTO proposals
           (id, case_id, version, order_item_id, quantity, reason, refund_amount_cents, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        ("PROP-DEMO-001", "CASE-DEMO-002", 1, "ITEM-007", 1, "不再需要", 3900, "confirmed"),
    )
    connection.execute(
        "INSERT INTO confirmations (id, proposal_id, user_id) VALUES (?, ?, ?)",
        ("CONF-DEMO-001", "PROP-DEMO-001", "customer-001"),
    )
    connection.execute(
        "INSERT INTO return_requests (id, confirmation_id, operation_key, status) VALUES (?, ?, ?, ?)",
        ("RET-DEMO-001", "CONF-DEMO-001", "demo-return-001", "awaiting_return"),
    )


def initialize_demo(as_of: date, path: Path | None = None, *, reset: bool = False) -> bool:
    """初始化指定演示库（默认固定文件）；存在时原样保留，reset=True 时删除后按 as_of 重建。"""
    path = path or DEMO_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    if reset:
        # 只删除这一个库及其 SQLite 附属文件，其他库文件不受影响。
        for target in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm"),
                       path.with_name(path.name + "-journal")):
            target.unlink(missing_ok=True)
    try:
        # 独占创建，防止重复执行时覆盖已存在的数据库。
        with path.open("xb"):
            pass
    except FileExistsError:
        return False
    try:
        with connect_database(path) as connection:
            connection.executescript("BEGIN;\n" + SCHEMA_PATH.read_text(encoding="utf-8"))
            seed_demo(connection, as_of)
    except Exception:
        # 仅删除本次新建且初始化失败的文件，便于修复后重新执行。
        path.unlink()
        raise
    return True


def reset_demo_in_place(path: Path, as_of: date) -> None:
    """服务运行中重置演示库：在同一文件内删表重建（Windows 下运行中的库文件不能删除），
    清空案件、退款、会话、检查点等全部记录后按 as_of 重新生成演示数据。"""
    from after_sales.storage.database import initialize_session_storage
    with connect_database(path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        tables = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
        for table in tables:
            connection.execute(f'DROP TABLE "{table}"')
    with connect_database(path) as connection:
        connection.executescript("BEGIN;\n" + SCHEMA_PATH.read_text(encoding="utf-8"))
        seed_demo(connection, as_of)
    initialize_session_storage(path)


def run() -> None:
    parser = argparse.ArgumentParser(description="初始化演示库（默认 data/demo.sqlite3）；已存在时保留原数据，--reset 时重建。")
    parser.add_argument(
        "--as-of", type=date.fromisoformat,
        default=business_today(),
        help="演示基准日期 YYYY-MM-DD；默认 Asia/Hong_Kong 当天，不加 --reset 时重复执行不刷新日期。",
    )
    parser.add_argument("--db", type=Path, default=DEMO_DB_PATH,
                        help="演示库文件路径；默认本项目 data/demo.sqlite3。")
    parser.add_argument("--reset", action="store_true",
                        help="删除指定库并按基准日期重建；会清空该库的全部案件、退款和会话记录。")
    args = parser.parse_args()
    path = args.db.resolve()
    if initialize_demo(args.as_of, path, reset=args.reset):
        print(f"已{'重置' if args.reset else '初始化'}：{path}；演示基准日期：{args.as_of}")
        print("已创建 4 个模拟用户、6 个订单，以及质量问题案件和已有退货申请样例。金额单位：分。")
    else:
        print(f"数据库已存在，保留原数据：{path}（需要全新数据时加 --reset）")


if __name__ == "__main__":
    run()

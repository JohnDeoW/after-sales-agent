"""本地模拟物流记录；首次初始化补充场景，重启不重置轨迹。"""

import json
from datetime import date, timedelta


def initialize_logistics(connection):
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'logistics'").fetchone():
        return
    connection.execute("""CREATE TABLE logistics (
        order_id TEXT PRIMARY KEY REFERENCES orders(id),
        status TEXT NOT NULL CHECK (status IN ('in_transit', 'delayed', 'lost_pending', 'delivered', 'unknown')),
        carrier TEXT NOT NULL,
        tracking_number TEXT NOT NULL,
        estimated_delivery_at TEXT,
        events_json TEXT NOT NULL
    )""")
    anchor = connection.execute("SELECT ordered_on FROM orders WHERE id = 'ORD-DEMO-001'").fetchone()
    if anchor is None:
        return
    as_of = date.fromisoformat(anchor["ordered_on"]) + timedelta(days=5)
    for number, status, description in (
        (7, "in_transit", "包裹已到达演示转运中心，正常运输中"),
        (8, "delayed", "转运延迟，新的送达时间尚未确认"),
        (9, "lost_pending", "轨迹中断，疑似丢件待查，尚未确认丢失"),
    ):
        order_id = f"ORD-DEMO-{number:03d}"
        ordered_on = (as_of - timedelta(days=3)).isoformat()
        connection.execute("INSERT INTO orders (id, user_id, status, ordered_on) VALUES (?, 'customer-001', 'shipped', ?)",
                           (order_id, ordered_on))
        connection.execute("""INSERT INTO order_items
            (id, order_id, sku, product_name, specification, quantity, paid_amount_cents)
            VALUES (?, ?, 'MOUSE-BLACK', '无线鼠标', '黑色', 1, 19900)""", (f"ITEM-{number + 1:03d}", order_id))
        events = [{"occurred_at": ordered_on + "T16:00:00+08:00", "description": "演示承运商已揽收"},
                  {"occurred_at": as_of.isoformat() + "T09:00:00+08:00", "description": description}]
        connection.execute("""INSERT INTO logistics (order_id, status, carrier, tracking_number, events_json)
                              VALUES (?, ?, '演示物流', ?, ?)""",
                           (order_id, status, f"DEMO-TRACK-{number:03d}", json.dumps(events, ensure_ascii=False)))
    for row in connection.execute("""SELECT id, ordered_on, signed_on FROM orders
            WHERE id IN ('ORD-DEMO-001', 'ORD-DEMO-002', 'ORD-DEMO-003', 'ORD-DEMO-004', 'ORD-DEMO-005', 'ORD-DEMO-006')
            AND status = 'delivered' AND signed_on IS NOT NULL"""):
        events = [{"occurred_at": row["ordered_on"] + "T16:00:00+08:00", "description": "演示承运商已揽收"},
                  {"occurred_at": row["signed_on"] + "T14:00:00+08:00", "description": "承运商记录显示已签收"}]
        connection.execute("""INSERT INTO logistics (order_id, status, carrier, tracking_number, events_json)
                              VALUES (?, 'delivered', '演示物流', ?, ?)""",
                           (row["id"], "DEMO-TRACK-" + row["id"], json.dumps(events, ensure_ascii=False)))

"""同款等价规格的演示库存；首次初始化后不重置可用量。"""


def initialize_inventory(connection):
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'inventory'").fetchone():
        return
    connection.execute("""CREATE TABLE inventory (
        sku TEXT PRIMARY KEY, product_name TEXT NOT NULL, specification TEXT NOT NULL,
        product_group TEXT NOT NULL, price_cents INTEGER NOT NULL CHECK (price_cents >= 0),
        available INTEGER NOT NULL CHECK (available >= 0),
        reserved INTEGER NOT NULL DEFAULT 0 CHECK (reserved >= 0)
    )""")
    connection.executemany("INSERT INTO inventory (sku, product_name, specification, product_group, price_cents, available) VALUES (?, ?, ?, ?, ?, ?)", [
        ("MOUSE-BLACK", "无线鼠标", "黑色", "mouse", 19900, 4),
        ("MOUSE-WHITE", "无线鼠标", "白色", "mouse", 19900, 3),
        ("MOUSE-RED", "无线鼠标", "红色", "mouse", 19900, 0),
        ("MOUSE-PRO", "无线鼠标升级款", "专业版", "mouse", 29900, 2),
        ("KEYBOARD-WHITE", "键盘", "白色", "keyboard", 29900, 3),
        ("KEYBOARD-BLACK", "键盘", "黑色", "keyboard", 29900, 2),
        ("CABLE-1M", "数据线", "1 米", "cable", 3990, 6),
        ("STAND-SILVER", "电脑支架", "银色", "stand", 12900, 2),
        ("HEADPHONE-BLACK", "耳机", "黑色", "headphone", 69900, 2),
        ("PAD-GREY", "鼠标垫", "灰色", "pad", 3900, 3),
    ])

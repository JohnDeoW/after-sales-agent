CREATE TABLE users (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL
);

CREATE TABLE orders (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    status TEXT NOT NULL,
    ordered_on TEXT NOT NULL,
    signed_on TEXT,
    shipping_paid_cents INTEGER NOT NULL DEFAULT 0 CHECK (shipping_paid_cents >= 0)
);

CREATE TABLE order_items (
    id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES orders(id),
    sku TEXT NOT NULL,
    product_name TEXT NOT NULL,
    specification TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    -- 整个商品项分摊优惠后的实付，单位为分，不是单价。
    paid_amount_cents INTEGER NOT NULL CHECK (paid_amount_cents >= 0),
    returned_quantity INTEGER NOT NULL DEFAULT 0
        CHECK (returned_quantity BETWEEN 0 AND quantity),
    refunded_amount_cents INTEGER NOT NULL DEFAULT 0
        CHECK (refunded_amount_cents BETWEEN 0 AND paid_amount_cents)
);
CREATE INDEX idx_orders_user ON orders(user_id);
CREATE INDEX idx_order_items_order ON order_items(order_id);

CREATE TABLE cases (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    order_id TEXT REFERENCES orders(id),
    order_item_id TEXT REFERENCES order_items(id),
    intent TEXT NOT NULL,
    status TEXT NOT NULL,
    quantity INTEGER CHECK (quantity > 0),
    reason TEXT,
    opened INTEGER CHECK (opened IN (0, 1)),
    summary TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE proposals (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id),
    version INTEGER NOT NULL CHECK (version > 0),
    order_item_id TEXT NOT NULL REFERENCES order_items(id),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    reason TEXT NOT NULL,
    refund_amount_cents INTEGER NOT NULL CHECK (refund_amount_cents >= 0),
    requires_approval INTEGER NOT NULL DEFAULT 0 CHECK (requires_approval IN (0, 1)),
    details_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (case_id, version)
);

CREATE TABLE confirmations (
    id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL UNIQUE REFERENCES proposals(id),
    user_id TEXT NOT NULL REFERENCES users(id),
    confirmed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE return_requests (
    id TEXT PRIMARY KEY,
    confirmation_id TEXT NOT NULL UNIQUE REFERENCES confirmations(id),
    operation_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

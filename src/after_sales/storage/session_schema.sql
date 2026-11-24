CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    card_json TEXT,
    case_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);

CREATE TABLE IF NOT EXISTS conversation_states (
    session_id TEXT PRIMARY KEY REFERENCES sessions(id),
    case_id TEXT REFERENCES cases(id),
    state_json TEXT NOT NULL,
    history_start_id INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS case_progress (
    case_id TEXT PRIMARY KEY REFERENCES cases(id),
    state_json TEXT NOT NULL,
    missing_fields_json TEXT NOT NULL DEFAULT '[]',
    revision INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS workflow_runs (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    trace_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_session ON workflow_runs(session_id, created_at);

CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE REFERENCES return_requests(id),
    proposal_id TEXT NOT NULL REFERENCES proposals(id),
    proposal_version INTEGER NOT NULL,
    reviewer_id TEXT NOT NULL REFERENCES users(id),
    decision TEXT NOT NULL CHECK (decision IN ('approved', 'rejected')),
    reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    snapshot_json TEXT NOT NULL,
    operation_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS return_events (
    id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL REFERENCES return_requests(id),
    kind TEXT NOT NULL CHECK (kind IN ('shipment', 'receipt', 'inspection')),
    actor_id TEXT NOT NULL REFERENCES users(id),
    operation_key TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (request_id, kind)
);

CREATE TABLE IF NOT EXISTS refund_operations (
    id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE REFERENCES return_requests(id),
    operation_key TEXT NOT NULL UNIQUE,
    user_id TEXT NOT NULL REFERENCES users(id),
    order_item_id TEXT NOT NULL REFERENCES order_items(id),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
    status TEXT NOT NULL CHECK (status IN ('pending', 'succeeded', 'failed', 'unknown')),
    snapshot_json TEXT NOT NULL,
    result_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS simulated_refund_results (
    operation_id TEXT PRIMARY KEY REFERENCES refund_operations(id),
    result_json TEXT NOT NULL,
    updated_by TEXT REFERENCES users(id),
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS refund_demo_scenarios (
    operation_key TEXT PRIMARY KEY,
    request_id TEXT NOT NULL REFERENCES return_requests(id),
    scenario TEXT NOT NULL CHECK (scenario IN ('normal', 'success_then_timeout')),
    selected_by TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tickets (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    case_id TEXT REFERENCES cases(id),
    request_id TEXT REFERENCES return_requests(id),
    source TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    operation_key TEXT NOT NULL UNIQUE,
    snapshot_json TEXT NOT NULL,
    todo_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_tickets_user ON tickets(user_id, created_at);

CREATE TABLE IF NOT EXISTS ticket_updates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id TEXT NOT NULL REFERENCES tickets(id),
    actor_id TEXT NOT NULL REFERENCES users(id),
    operation_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    reply TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS exchange_proposals (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    session_id TEXT NOT NULL REFERENCES sessions(id),
    version INTEGER NOT NULL,
    order_item_id TEXT NOT NULL REFERENCES order_items(id),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    target_sku TEXT NOT NULL,
    reason TEXT NOT NULL,
    details_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending_confirmation',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (session_id, version)
);

CREATE TABLE IF NOT EXISTS exchange_requests (
    id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL UNIQUE REFERENCES exchange_proposals(id),
    user_id TEXT NOT NULL REFERENCES users(id),
    operation_key TEXT NOT NULL UNIQUE,
    order_item_id TEXT NOT NULL REFERENCES order_items(id),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    target_sku TEXT NOT NULL,
    status TEXT NOT NULL,
    inventory_held INTEGER NOT NULL DEFAULT 1 CHECK (inventory_held IN (0, 1)),
    snapshot_json TEXT NOT NULL,
    confirmed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS exchange_events (
    id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL REFERENCES exchange_requests(id),
    kind TEXT NOT NULL CHECK (kind IN ('shipment', 'receipt', 'inspection', 'dispatch', 'delivery', 'cancel')),
    actor_id TEXT NOT NULL REFERENCES users(id),
    operation_key TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (request_id, kind)
);

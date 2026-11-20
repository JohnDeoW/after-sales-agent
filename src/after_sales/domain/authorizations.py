"""确认和审批的快照、期限与失效记录；不依赖对话历史授权。"""

import json
from datetime import datetime, timedelta, timezone


def utc_now():
    return datetime.now(timezone.utc)


def expires_at(seconds):
    return (utc_now() + timedelta(seconds=seconds)).isoformat()


def snapshot(proposal):
    data = dict(proposal)
    details = data.get("details") or json.loads(data.get("details_json", "{}"))
    return {**{k: data[k] for k in ("case_id", "order_item_id", "quantity", "reason", "refund_amount_cents")},
            "proposal_id": data["id"], "proposal_version": data["version"],
            "order_id": details.get("order_id"), "details": details}


def valid(record, proposal):
    return bool(record and not record["invalidated_at"] and record["expires_at"]
                and datetime.fromisoformat(record["expires_at"]) > utc_now()
                and json.loads(record["snapshot_json"]) == snapshot(proposal))


def invalidate(connection, proposal_id, reason, *, cancelled=False):
    timestamp = utc_now().isoformat()
    for table in ("confirmations", "approvals"):
        connection.execute(
            f"UPDATE {table} SET invalidated_at = ?, invalidation_reason = ? WHERE proposal_id = ? AND invalidated_at IS NULL",
            (timestamp, reason, proposal_id),
        )
    connection.execute("UPDATE proposals SET status = ? WHERE id = ?",
                       ("cancelled" if cancelled else "superseded", proposal_id))
    connection.execute(
        """UPDATE return_requests SET status = ? WHERE confirmation_id IN
           (SELECT id FROM confirmations WHERE proposal_id = ?)
           AND status IN ('pending_approval', 'awaiting_return', 'approval_rejected',
                          'in_transit', 'awaiting_inspection', 'ready_for_refund', 'manual_review_required')""",
        ("cancelled" if cancelled else "superseded", proposal_id),
    )

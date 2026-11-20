"""工单与触发业务共用事务；保存转交时的事实快照，不代替当前业务记录。"""

import json
import secrets

from after_sales.storage.database import read_order
from after_sales.retrieval.policies import load_policy_chunks


def ticket_view(row):
    ticket = dict(row)
    ticket["snapshot"] = json.loads(ticket.pop("snapshot_json"))
    ticket["todo"] = json.loads(ticket.pop("todo_json"))
    return ticket


def ticket_summaries(connection, *, case_id=None, request_id=None):
    field, value = ("request_id", request_id) if request_id else ("case_id", case_id)
    return [dict(r) for r in connection.execute(
        f"SELECT id, source, status, reason, created_at FROM tickets WHERE {field} = ? ORDER BY rowid", (value,))]


def case_snapshot(connection, case_id, user_id):
    case = connection.execute("SELECT * FROM cases WHERE id = ? AND user_id = ?", (case_id, user_id)).fetchone()
    if case is None:
        raise ValueError("工单关联案件归属不匹配")
    checkpoint = connection.execute("SELECT * FROM case_progress WHERE case_id = ?", (case_id,)).fetchone()
    proposals = [dict(r) for r in connection.execute("SELECT * FROM proposals WHERE case_id = ? ORDER BY version", (case_id,))]
    for proposal in proposals:
        proposal["details"] = json.loads(proposal.pop("details_json"))
    confirmations, requests, approvals, events, refunds = [], [], [], [], []
    for proposal in proposals:
        confirmation = connection.execute("SELECT * FROM confirmations WHERE proposal_id = ?", (proposal["id"],)).fetchone()
        if not confirmation:
            continue
        confirmations.append(dict(confirmation))
        request = connection.execute("SELECT * FROM return_requests WHERE confirmation_id = ?", (confirmation["id"],)).fetchone()
        if not request:
            continue
        requests.append(dict(request))
        approvals.extend(dict(r) for r in connection.execute("SELECT * FROM approvals WHERE request_id = ?", (request["id"],)))
        events.extend(dict(r) for r in connection.execute("SELECT * FROM return_events WHERE request_id = ? ORDER BY rowid", (request["id"],)))
        refunds.extend(dict(r) for r in connection.execute("SELECT * FROM refund_operations WHERE request_id = ?", (request["id"],)))
    for rows in (confirmations, approvals, events, refunds):
        for row in rows:
            for key in list(row):
                if key.endswith("_json"):
                    row[key[:-5]] = json.loads(row.pop(key))
    order = read_order(connection, case["order_id"]) if case["order_id"] else None
    refs = {p["chunk_id"]: p for proposal in proposals for p in proposal["details"].get("policy_refs", [])}
    policy_evidence = [p for p in load_policy_chunks() if p["chunk_id"] in refs
                       and p["source_sha256"] == refs[p["chunk_id"]]["source_sha256"]]
    return {"case": dict(case), "order": order if order and order["user_id"] == user_id else None,
            "state": json.loads(checkpoint["state_json"]) if checkpoint else {},
            "missing_fields": json.loads(checkpoint["missing_fields_json"]) if checkpoint else [],
            "proposals": proposals, "confirmations": confirmations, "requests": requests,
            "approvals": approvals, "return_events": events, "refund_operations": refunds,
            "policy_evidence": policy_evidence}


def create_ticket(connection, *, user_id, source, operation_key, reason, todo, case_id=None,
                  request_id=None, evidence=None):
    """调用者已检查角色，并开启写事务；同一来源操作始终返回同一工单。"""
    existing = connection.execute("SELECT * FROM tickets WHERE operation_key = ?", (operation_key,)).fetchone()
    if existing:
        if (existing["user_id"], existing["case_id"], existing["request_id"], existing["source"]) != (user_id, case_id, request_id, source):
            raise ValueError("工单操作编号与来源不匹配")
        return {"ticket": ticket_view(existing), "reused": True}
    facts = case_snapshot(connection, case_id, user_id) if case_id else {}
    if request_id and not any(r["id"] == request_id for r in facts["requests"]):
        raise ValueError("工单申请不属于关联案件")
    facts["evidence"] = evidence or {}
    ticket_id = "TKT-" + secrets.token_hex(8)
    connection.execute(
        """INSERT INTO tickets (id, user_id, case_id, request_id, source, reason, operation_key, snapshot_json, todo_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (ticket_id, user_id, case_id, request_id, source, reason, operation_key,
         json.dumps(facts, ensure_ascii=False), json.dumps(todo, ensure_ascii=False)),
    )
    return {"ticket": ticket_view(connection.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()), "reused": False}


def create_request_ticket(connection, request_id, source, reason, todo):
    owner = connection.execute(
        """SELECT c.id, c.user_id FROM return_requests r JOIN confirmations f ON f.id = r.confirmation_id
           JOIN proposals p ON p.id = f.proposal_id JOIN cases c ON c.id = p.case_id
           WHERE r.id = ? AND f.user_id = c.user_id""", (request_id,)).fetchone()
    if owner is None:
        raise ValueError("工单申请归属不匹配")
    return create_ticket(connection, user_id=owner["user_id"], case_id=owner["id"], request_id=request_id,
                         source=source, operation_key=f"{source}:{request_id}", reason=reason, todo=todo)

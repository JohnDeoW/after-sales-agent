"""将一轮对话、退货草稿和案件更新放在同一个数据库事务中。"""

import json
import secrets
from copy import deepcopy
import hashlib
from after_sales.domain.message_cards import build_card
from pathlib import Path

from after_sales.storage.database import connect_database, append_messages, read_order
from after_sales.domain.proposals import sync_proposal, ProposalConflict
from after_sales.storage.case_progress import save_state
from after_sales.domain.customer_service import _session_user
from after_sales.storage.refunds import read_refund
from after_sales.storage.tickets import create_ticket


DEFAULT_STATE = {"draft": {}, "last_intent": "unknown", "query_order_id": None}


def load_context(path: Path, session_id: str, snapshot: dict | None = None) -> dict:
    """读取本轮上下文。snapshot 是检查点中的会话图状态（conversation、case_id、case_revision）。

    会话状态以检查点为准；会话所在案件被其他会话或业务操作更新过（revision 变化）或切换了案件时，
    改用案件进度中的最新草稿。没有检查点的旧会话从原对话状态表迁移。
    """
    with connect_database(path) as connection:
        connection.execute("BEGIN")
        user = _session_user(connection, session_id)
        row = connection.execute(
            "SELECT case_id, state_json, history_start_id FROM conversation_states WHERE session_id = ?", (session_id,),
        ).fetchone()
        case_id = row["case_id"] if row else None
        case = connection.execute("SELECT state_json, revision FROM case_progress WHERE case_id = ?", (case_id,)).fetchone()
        if snapshot and snapshot.get("conversation") is not None and snapshot.get("case_id") == case_id and (
                case_id is None or (case and snapshot.get("case_revision") == case["revision"])):
            state = deepcopy(snapshot["conversation"])
        elif case:
            state = json.loads(case["state_json"])
        elif snapshot and snapshot.get("conversation") is not None and case_id is None:
            state = deepcopy(snapshot["conversation"])
        elif row and row["state_json"] not in (None, "", "{}"):
            state = json.loads(row["state_json"])  # 检查点上线前保存的旧会话
        else:
            state = deepcopy(DEFAULT_STATE)
        history = connection.execute(
            "SELECT id, role, content FROM messages WHERE session_id = ? AND id >= ? ORDER BY id DESC LIMIT 6",
            (session_id, row["history_start_id"] if row else 0),
        ).fetchall()
        progress = connection.execute(
            """SELECT p.revision, p.missing_fields_json, c.status, c.summary FROM case_progress p
               JOIN cases c ON c.id = p.case_id WHERE p.case_id = ? AND c.user_id = ?""",
            (row["case_id"] if row else None, user["id"]),
        ).fetchone()
        facts = None
        if progress:
            confirmation = connection.execute(
                """SELECT f.id, f.proposal_id, p.version AS proposal_version, f.confirmed_at, f.expires_at
                   FROM confirmations f JOIN proposals p ON p.id = f.proposal_id
                   WHERE p.case_id = ? AND p.id = ? AND p.status = 'confirmed' AND f.user_id = ?
                   AND f.invalidated_at IS NULL AND julianday(f.expires_at) > julianday('now')""",
                (row["case_id"], state.get("proposal_id"), user["id"]),
            ).fetchone()
            request = connection.execute(
                """SELECT r.id, r.status, r.operation_key, a.decision AS approval_decision, a.reason AS approval_reason FROM return_requests r
                   JOIN confirmations f ON f.id = r.confirmation_id JOIN proposals p ON p.id = f.proposal_id
                   LEFT JOIN approvals a ON a.request_id = r.id
                   WHERE p.case_id = ? AND f.user_id = ? AND p.id = ?""", (row["case_id"], user["id"], state.get("proposal_id")),
            ).fetchone()
            facts = {"status": request["status"] if request else progress["status"],
                     "summary": progress["summary"], "missing_fields": json.loads(progress["missing_fields_json"]),
                     "confirmation": dict(confirmation) if confirmation else None,
                     "return_request": dict(request) if request else None}
            if request:
                # 恢复案件后可以直接查询这份申请。
                state.setdefault("return_request_id", request["id"])
                refund = read_refund(connection, request["id"])
                facts["return_request"]["refund_operation"] = ({key: refund[key] for key in ("id", "status", "amount_cents")} if refund else None)
        last_id = connection.execute("SELECT MAX(id) FROM messages WHERE session_id = ?", (session_id,)).fetchone()[0]
        return {"state": state, "case_id": row["case_id"] if row else None,
                "loaded_case_id": row["case_id"] if row else None,
                "case_revision": progress["revision"] if progress else None,
                "progress": facts,
                "last_message_id": last_id,
                "history": [dict(message) for message in reversed(history)]}


def save_turn(path: Path, session_id: str, user_id: str, content: str,
              context: dict, result: dict, case_fields: dict | None) -> dict:
    with connect_database(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        last_id = connection.execute("SELECT MAX(id) FROM messages WHERE session_id = ?", (session_id,)).fetchone()[0]
        if last_id != context["last_message_id"]:
            raise ProposalConflict("本次识别期间会话已更新，请查看最新方案后重试；本轮消息未保存。")
        active = connection.execute("SELECT case_id FROM conversation_states WHERE session_id = ?", (session_id,)).fetchone()
        loaded_case_id = context["loaded_case_id"]
        progress = connection.execute("SELECT revision FROM case_progress WHERE case_id = ?", (loaded_case_id,)).fetchone()
        if ((active["case_id"] if active else None) != loaded_case_id
                or (progress["revision"] if progress else None) != context["case_revision"]):
            raise ProposalConflict("案件已切换或被其他会话更新，请查看最新进度后重试；本轮消息未保存。")
        case_id = context["case_id"]
        if result.get("intent") in ("exchange", "return"):
            from after_sales.domain.exchanges import save_exchange_proposal
            save_exchange_proposal(connection, session_id, user_id, context["state"], result)
            if result["intent"] == "return":
                context["state"].pop("exchange", None)
        if case_fields is not None:
            if case_id is None:
                case_id = "CASE-" + secrets.token_hex(8)
                connection.execute(
                    "INSERT INTO cases (id, user_id, intent, status) VALUES (?, ?, 'return', 'collecting_information')",
                    (case_id, user_id),
                )
            sync_proposal(connection, case_id, context["state"], result)
            connection.execute(
                """UPDATE cases SET order_id = ?, order_item_id = ?, quantity = ?, reason = ?,
                   opened = ?, status = ?, summary = ? WHERE id = ? AND user_id = ?""",
                (case_fields.get("order_id"), case_fields.get("order_item_id"),
                 case_fields.get("quantity"), case_fields.get("reason"), case_fields.get("opened"),
                 result["status"], result["reply_text"], case_id, user_id),
            )
        save_state(connection, session_id, case_id, context["state"],
                   result.get("missing_fields", []) if case_fields is not None else None)
        if result.get("logistics_handoff"):
            from after_sales.domain.logistics import create_investigation
            created = create_investigation(connection, user_id, **result.pop("logistics_handoff"))
            ticket = created["ticket"]
            result.update(status="investigation_created", ticket=ticket, ticket_reused=created["reused"])
            result["reply_text"] += f"调查工单 {ticket['id']} 已保存用户说明和物流记录，等待客服核实，尚未认定责任。"
            context["state"].pop("logistics_dispute", None)
            save_state(connection, session_id, case_id, context["state"])
        handoff = result.get("handoff")
        if result.get("failure_handoff"):
            from after_sales.domain.tickets import ticket_text
            failure = result.pop("failure_handoff")
            state = context["state"]
            known = {key: state.get(key) for key in ("draft", "exchange", "service_request", "logistics_order_id", "exchange_request_id")}
            if known.get("service_request"):
                known["service_request"] = {k: v for k, v in known["service_request"].items() if k not in ("original_message", "statement_message")}
            fingerprint = hashlib.sha256(json.dumps({"known": known, "reason": failure["reason_code"]}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            evidence = {**failure, "known_information": known, "current_message": content,
                        "write_policy": "查询原操作，不自动重复写入"}
            if state.get("exchange_request_id"):
                from after_sales.domain.exchanges import read_exchange
                evidence["exchange_request"] = read_exchange(connection, state["exchange_request_id"], user_id)
            created = create_ticket(connection, user_id=user_id, case_id=case_id, source="workflow_failure",
                                    operation_key=f"workflow:{user_id}:{case_id or session_id}:{fingerprint}",
                                    reason="查询持续失败或自动办理达到预算，需人工接管", evidence=evidence,
                                    todo=["查看已有信息和失败轨迹，补充缺失信息或恢复查询", "先查询原申请、退款操作和实际状态，不重复执行结果未知的写操作"])
            ticket = created["ticket"]
            ticket["updates"] = [dict(r) for r in connection.execute("SELECT * FROM ticket_updates WHERE ticket_id = ? ORDER BY id", (ticket["id"],))]
            result.update(status="workflow_handoff", ticket=ticket, ticket_reused=created["reused"])
            result["reply_text"] = "自动办理已停止，已有记录保留并转交人工。" + ticket_text(ticket)
            if failure.get("write_check"):
                checked = failure["write_check"]
                result["write_check"] = checked
                record = checked["record"]
                result["reply_text"] += (f"已按原编号查询，现有记录 {record['id']} 的实际状态为 {record['status']}，未重复执行写操作。"
                                        if record else "原编号暂未查询到可确认记录，结果仍待人工核对；未重复执行写操作。")
        if result.get("service_handoff"):
            from after_sales.domain.handoffs import create_service_ticket
            from after_sales.domain.tickets import ticket_text
            created = create_service_ticket(connection, user_id, case_id, result.pop("service_handoff"))
            ticket = created["ticket"]
            ticket["updates"] = [dict(r) for r in connection.execute("SELECT * FROM ticket_updates WHERE ticket_id = ? ORDER BY id", (ticket["id"],))]
            result.update(status="ticket_created", ticket=ticket, ticket_reused=created["reused"])
            result["reply_text"] = ticket_text(ticket) + "材料仅作为引用保存，系统未进行图片识别或责任鉴定。"
        if handoff or result["status"] == "policy_unavailable":
            if handoff:
                identity = {key: handoff[key] for key in ("reason", "question", "known_fields", "policy_refs")}
                fingerprint = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
                # 咨询可独立存在；只有明确同一订单时才挂到当前办理案件。
                case = connection.execute("SELECT order_id FROM cases WHERE id = ?", (case_id,)).fetchone()
                linked_case = case_id if case and case["order_id"] and case["order_id"] == handoff["known_fields"].get("order_id") else None
                evidence = {**identity, "search_attempts": handoff["search_attempts"], "search_result": handoff.get("evidence", {})}
                order = read_order(connection, handoff["known_fields"].get("order_id"))
                evidence["order"] = order if order and order["user_id"] == user_id else None
                reason, operation_key = handoff["message"], f"policy:{user_id}:{linked_case}:{fingerprint}"
            else:
                linked_case, evidence = case_id, result["assessment"]
                fingerprint = hashlib.sha256(json.dumps({"assessment": evidence, "draft": context["state"]["draft"]}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
                reason, operation_key = evidence["explanation"], f"policy-assessment:{case_id}:{fingerprint}"
            created = create_ticket(connection, user_id=user_id, case_id=linked_case, source="policy_review",
                                    operation_key=operation_key, reason=reason, evidence=evidence,
                                    todo=["核对用户问题、已知商品信息和检索证据", "解决政策冲突或补齐适用依据后答复客户；不得编造结论"])
            ticket = created["ticket"]
            result.update(ticket=ticket, ticket_reused=created["reused"])
            if handoff:
                handoff.update(ticket_created=True, ticket_id=ticket["id"])
                save_state(connection, session_id, case_id, context["state"])
            result["reply_text"] += f"已转交人工工单 {ticket['id']}，等待客服核对；可查询工单进度。"
        messages = append_messages(connection, session_id, content, result.pop("reply_text"), build_card(result))
        return {**result, **messages, "case_id": case_id,
                "draft": context["state"]["draft"]}

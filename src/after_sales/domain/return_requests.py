"""确认后受控创建退货申请；受理不代表审批通过或退款完成。"""

import json
import secrets

from after_sales.domain.message_cards import build_card
from after_sales.domain.customer_service import CustomerService, _session_user
from after_sales.domain.proposals import ProposalConflict, ProposalNotFound, sync_proposal, assessment_details
from after_sales.domain.returns import evaluate_return, assessment_reply, assessment_status
from after_sales.storage.database import connect_database, read_order, append_messages
from after_sales.storage.case_progress import save_state, save_case_state
from after_sales.domain.authorizations import valid, invalidate
from after_sales.storage.return_events import read_events, return_conditions_met
from after_sales.storage.refunds import read_refund
from after_sales.storage.tickets import create_request_ticket, ticket_summaries


REQUEST_SELECT = """
SELECT r.*, p.id AS proposal_id, p.version AS proposal_version, p.case_id,
       p.order_item_id, p.quantity, p.reason, p.refund_amount_cents,
       p.requires_approval, p.details_json, c.order_id,
       a.id AS approval_id, a.decision AS approval_decision, a.reason AS approval_reason,
       a.expires_at AS approval_expires_at, a.invalidated_at AS approval_invalidated_at
FROM return_requests r
JOIN confirmations f ON f.id = r.confirmation_id
JOIN proposals p ON p.id = f.proposal_id
JOIN cases c ON c.id = p.case_id
LEFT JOIN approvals a ON a.request_id = r.id
WHERE c.user_id = ? AND f.user_id = ?
"""


def request_view(row, connection) -> dict:
    result = dict(row)
    result["details"] = json.loads(result.pop("details_json"))
    result["requires_approval"] = bool(result["requires_approval"])
    approval = {key: result.pop("approval_" + key) for key in ("id", "decision", "reason", "expires_at", "invalidated_at")}
    result["approval"] = approval if approval["id"] else None
    result["return_events"] = read_events(connection, result["id"])
    result["refund_operation"] = read_refund(connection, result["id"])
    result["tickets"] = ticket_summaries(connection, request_id=result["id"])
    result["return_conditions_met"] = result["status"] == "ready_for_refund" and return_conditions_met(result["return_events"])
    result["status_label"], result["next_step"] = {
        "pending_approval": ("待审批", "等待客服审核，尚未批准，不能退款，请勿寄回。"),
        "awaiting_return": ("待退货", "可提交演示承运商和退货单号，请勿实际寄件。后续需仓库签收、检验通过后才能退款。"),
        "in_transit": ("退货运输中", "已登记退货单号，等待仓库签收，尚未满足退款条件。"),
        "awaiting_inspection": ("待检验", "仓库已签收，等待检验结果，尚未满足退款条件。"),
        "ready_for_refund": ("检验通过，待退款", "退回收货与检验条件已满足；退款前仍需核验授权和金额，尚未退款。"),
        "refunded": ("模拟退款完成", "本申请已完成模拟退款，无需重复执行；未向真实支付系统付款。"),
        "refund_failed": ("模拟退款失败", "模拟服务明确返回失败，本次未记账。请联系人工客服核对原操作。"),
        "refund_unknown": ("退款待核对", "模拟服务未给出可确认结果；请核对原操作，不能另建退款绕开核对。"),
        "manual_review_required": ("待人工处理", "退回过程需要人工核对，停止自动退款。请联系人工客服处理；当前尚未创建工单。"),
        "approval_rejected": ("审批拒绝", "本申请已停止自动推进，不能退款。请联系人工客服补充材料并申请复核；当前尚未自动创建复核工单。"),
        "superseded": ("原方案已失效", "请查看案件的最新方案，重新确认并按需审批；旧申请不能继续执行。"),
        "cancelled": ("已取消", "用户已撤回本申请及相关授权，不再继续办理。"),
    }[result["status"]]
    if result["approval"]:
        result["next_step"] = "审批说明：" + approval["reason"] + "。" + result["next_step"]
    inspection = next((e for e in result["return_events"] if e["kind"] == "inspection"), None)
    if inspection:
        result["next_step"] = "检验说明：" + inspection["payload"]["reason"] + "。" + result["next_step"]
    if result["tickets"]:
        result["next_step"] = result["next_step"].replace("当前尚未创建工单。", "").replace("当前尚未自动创建复核工单。", "")
        result["next_step"] += "已转交人工工单 " + "、".join(t["id"] for t in result["tickets"]) + "，等待客服处理。"
    return result


def request_text(request: dict) -> str:
    amount = request["refund_amount_cents"]
    if request["status"] == "refunded":
        operation = request["refund_operation"]
        return (f"申请 {request['id']} 已完成模拟退款，商品款 {operation['amount_cents'] // 100}.{operation['amount_cents'] % 100:02d} 元，"
                f"退款操作编号 {operation['id']}。结果来自模拟记录，未发生真实付款。")
    if request["status"] == "refund_unknown":
        return f"申请 {request['id']} 的退款操作 {request['refund_operation']['id']} 结果待核对，尚不能确认成功或失败，请核对原操作。"
    prefix = "原申请" if request["status"] in ("cancelled", "superseded") else "已受理申请"
    return (f"{prefix} {request['id']}，当前状态：{request['status_label']}。"
            f"订单 {request['order_id']}，商品项 {request['order_item_id']}，{request['quantity']} 件，"
            f"预计商品退款 {amount // 100}.{amount % 100:02d} 元。"
            f"{request['next_step']}尚未退款。可回复“查询申请 {request['id']}”查询进度。")


def revalidate_request(connection, request_id, *, require_approval=True):
    """必须与后续业务写操作共用事务；有效授权仍不等于已满足退货/检验条件。"""
    row = connection.execute(
        """SELECT p.*, r.status AS request_status, c.user_id, c.order_id FROM return_requests r
           JOIN confirmations f ON f.id = r.confirmation_id JOIN proposals p ON p.id = f.proposal_id
           JOIN cases c ON c.id = p.case_id WHERE r.id = ? AND f.user_id = c.user_id""", (request_id,),
    ).fetchone()
    if row is None:
        raise ProposalNotFound("未找到有效申请。")
    if row["request_status"] == "refunded":
        return {"authorization_valid": False, "status": "refunded", "reply_text": "模拟退款已完成，无需重复授权或执行。"}
    if row["request_status"] in ("cancelled", "superseded", "approval_rejected", "manual_review_required", "refund_failed", "refund_unknown"):
        return {"authorization_valid": False, "status": row["request_status"], "reply_text": "申请已停止推进，旧授权不可执行。"}
    confirmation = connection.execute("SELECT * FROM confirmations WHERE proposal_id = ?", (row["id"],)).fetchone()
    approval = connection.execute("SELECT * FROM approvals WHERE request_id = ?", (request_id,)).fetchone()
    context = connection.execute("SELECT state_json FROM case_progress WHERE case_id = ?", (row["case_id"],)).fetchone()
    state = json.loads(context["state_json"])
    order = read_order(connection, row["order_id"])
    item = next((i for i in order["items"] if i["id"] == row["order_item_id"]), None) if order else None
    if not order or order["user_id"] != row["user_id"] or item is None:
        invalidate(connection, row["id"], "订单归属或商品项已变化", cancelled=True)
        state.update(cancelled=True, draft={}, assessment=None)
        result = {"authorization_valid": False, "status": "cancelled", "reply_text": "订单归属或商品项已变化，已取消旧授权，请人工核对。"}
    else:
        assessment = evaluate_return(order, item, state["draft"])
        eligible = assessment["decision"] in ("eligible", "manual_review")
        unchanged = eligible and assessment_details(assessment, state["draft"]) == json.loads(row["details_json"])
        latest = connection.execute("SELECT id FROM proposals WHERE case_id = ? ORDER BY version DESC LIMIT 1", (row["case_id"],)).fetchone()
        if row["status"] != "confirmed" or latest["id"] != row["id"]:
            return {"authorization_valid": False, "status": "superseded", "reply_text": "原方案不是当前有效版本，不能继续执行。"}
        reason = ("订单条件、金额或适用政策已变化" if not unchanged else
                  "用户确认过期或快照不匹配" if not valid(confirmation, row) else
                  "审批过期或快照不匹配" if approval and not valid(approval, row) else None)
        if reason is None:
            allowed = not require_approval or not row["requires_approval"] or bool(approval and approval["decision"] == "approved")
            return {"authorization_valid": allowed, "status": row["request_status"],
                    "reply_text": "授权当前有效，后续仍须满足退货、检验等条件。" if allowed else "尚未审批通过，不能执行退款。"}
        if row["request_status"] in ("in_transit", "awaiting_inspection", "ready_for_refund"):
            # 已寄回的实物记录不能自动迁到另一张申请、或要求用户再寄一次。
            invalidate(connection, row["id"], reason)
            connection.execute("UPDATE return_requests SET status = 'manual_review_required' WHERE id = ?", (request_id,))
            message = reason + "。已有退回记录保留在原申请，停止自动推进，请人工核对授权与实物处理。"
            connection.execute("UPDATE cases SET status = 'manual_review_required', summary = ? WHERE id = ?", (message, row["case_id"]))
            save_case_state(connection, row["case_id"], state, [])
            ticket = create_request_ticket(connection, request_id, "authorization_review", message,
                                           ["核对失效的确认或审批与当前订单条件", "保留原退回记录，人工决定后续授权与实物处理"])["ticket"]
            return {"authorization_valid": False, "status": "manual_review_required", "ticket": ticket,
                    "reply_text": message + f"已转交人工工单 {ticket['id']}，等待客服处理。"}
        invalidate(connection, row["id"], reason)
        state.pop("return_request_id", None)
        state["assessment"] = assessment
        result = {"authorization_valid": False, "requires_reconfirmation": True, "assessment": assessment,
                  "status": assessment_status(assessment), "reply_text": assessment_reply(assessment)}
        sync_proposal(connection, row["case_id"], state, result)
        result["reply_text"] = reason + "，原申请和授权已停止推进。" + result["reply_text"]
    connection.execute("UPDATE cases SET status = ?, summary = ? WHERE id = ?",
                       (result["status"], result["reply_text"], row["case_id"]))
    save_case_state(connection, row["case_id"], state, [])
    return {**result, "case_id": row["case_id"]}


class ReturnRequestService:
    def __init__(self, customers: CustomerService) -> None:
        self.customers = customers

    def revalidate(self, session_id, request_id):
        with connect_database(self.customers.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            user = _session_user(connection, session_id)
            if connection.execute(REQUEST_SELECT + " AND r.id = ?", (user["id"], user["id"], request_id)).fetchone() is None:
                raise ProposalNotFound("未找到当前用户的退货申请。")
            return revalidate_request(connection, request_id)

    def list_requests(self, session_id: str) -> list[dict]:
        with connect_database(self.customers.db_path) as connection:
            user = _session_user(connection, session_id)
            rows = connection.execute(REQUEST_SELECT + " ORDER BY r.created_at DESC, r.id", (user["id"], user["id"]))
            return [request_view(row, connection) for row in rows]

    def get_request(self, session_id: str, request_id: str) -> dict:
        with connect_database(self.customers.db_path) as connection:
            user = _session_user(connection, session_id)
            row = connection.execute(REQUEST_SELECT + " AND r.id = ?", (user["id"], user["id"], request_id)).fetchone()
            if row is None:
                raise ProposalNotFound("未找到当前用户的退货申请，请核对申请编号。")
            return request_view(row, connection)

    def create_return_request(self, session_id: str, confirmation_id: str, *, content: str) -> dict:
        with connect_database(self.customers.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            user = _session_user(connection, session_id)
            existing = connection.execute(REQUEST_SELECT + " AND r.confirmation_id = ?",
                                          (user["id"], user["id"], confirmation_id)).fetchone()
            if existing is not None:
                checked = revalidate_request(connection, existing["id"])
                if checked.get("requires_reconfirmation"):
                    messages = append_messages(connection, session_id, content, checked.pop("reply_text"))
                    return {**checked, **messages}
                request = request_view(connection.execute(REQUEST_SELECT + " AND r.id = ?",
                                                          (user["id"], user["id"], existing["id"])).fetchone(), connection)
                messages = append_messages(connection, session_id, content, request_text(request))
                status = request["status"] if request["status"] in ("cancelled", "superseded", "approval_rejected") else "application_accepted"
                return {"status": status, "return_request": request, "reused": True, **messages}

            proposal = connection.execute(
                """SELECT p.* FROM confirmations f JOIN proposals p ON p.id = f.proposal_id
                   JOIN cases c ON c.id = p.case_id WHERE f.id = ? AND f.user_id = ? AND c.user_id = ?""",
                (confirmation_id, user["id"], user["id"]),
            ).fetchone()
            if proposal is None:
                raise ProposalNotFound("未找到当前用户的确认记录，请先明确确认方案。")
            context = connection.execute("SELECT * FROM conversation_states WHERE session_id = ?", (session_id,)).fetchone()
            state = json.loads(context["state_json"]) if context else {}
            if (context is None or context["case_id"] != proposal["case_id"]
                    or state.get("proposal_id") != proposal["id"] or proposal["status"] != "confirmed"):
                raise ProposalConflict("原确认已失效或不属于当前会话，请查看并确认最新方案后提交。")

            # 在写入事务内重查订单；确认后的业务变化不能沿用旧金额或资格。
            draft = state["draft"]
            order = read_order(connection, draft["order_id"])
            if order is None or order["user_id"] != user["id"]:
                raise ProposalNotFound("未找到当前用户的订单。")
            item = next(i for i in order["items"] if i["id"] == draft["order_item_id"])
            assessment = evaluate_return(order, item, draft)
            state["assessment"] = assessment
            result = {"status": assessment_status(assessment), "assessment": assessment, "reply_text": assessment_reply(assessment)}
            confirmation = connection.execute("SELECT * FROM confirmations WHERE id = ?", (confirmation_id,)).fetchone()
            if not valid(confirmation, proposal):
                invalidate(connection, proposal["id"], "用户确认过期或快照不匹配")
            sync_proposal(connection, proposal["case_id"], state, result)
            if result.get("proposal") is None or result["proposal"]["id"] != proposal["id"]:
                result["reply_text"] = "订单事实或规则已变化，未创建申请，请重新核对。" + result["reply_text"]
            else:
                from after_sales.domain.item_capacity import ensure_item_capacity
                ensure_item_capacity(connection, proposal["order_item_id"], proposal["quantity"], amount_cents=proposal["refund_amount_cents"])
                request_id = "RET-" + secrets.token_hex(8)
                status = "pending_approval" if proposal["requires_approval"] else "awaiting_return"
                connection.execute(
                    "INSERT INTO return_requests (id, confirmation_id, operation_key, status) VALUES (?, ?, ?, ?)",
                    (request_id, confirmation_id, f"create-return:{confirmation_id}", status),
                )
                request = request_view(connection.execute(REQUEST_SELECT + " AND r.id = ?",
                                                          (user["id"], user["id"], request_id)).fetchone(), connection)
                state["return_request_id"] = request_id
                result.update(status="application_accepted", return_request=request, reused=False,
                              reply_text=request_text(request))
            connection.execute("UPDATE cases SET status = ?, summary = ? WHERE id = ?",
                               (result.get("return_request", {}).get("status", result["status"]),
                                result["reply_text"], proposal["case_id"]))
            save_state(connection, session_id, proposal["case_id"], state, [])
            messages = append_messages(connection, session_id, content, result.pop("reply_text"), build_card(result))
            return {**result, **messages, "case_id": proposal["case_id"], "draft": draft}

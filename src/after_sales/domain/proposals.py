"""方案版本及用户决定；此模块不创建售后申请或执行退款。"""

import json
import secrets
import sqlite3

from after_sales.domain.message_cards import build_card
from after_sales.domain.customer_service import CustomerService, _session_user
from after_sales.domain.returns import evaluate_return, assessment_reply, assessment_status
from after_sales.storage.database import connect_database, read_order, append_messages
from after_sales.storage.case_progress import save_state, case_state
from after_sales.domain.authorizations import expires_at, snapshot, valid, invalidate


class ProposalNotFound(ValueError):
    pass


class ProposalConflict(ValueError):
    pass


SNAPSHOT_FIELDS = ("decision", "order_id", "order_item_id", "product_name", "specification", "quantity",
                   "reason", "refund_amount_cents", "requires_approval", "approval_reasons", "rule_version",
                   "policy_refs", "return_requirements", "explanation", "amount_breakdown")


def proposal_view(row: sqlite3.Row | dict) -> dict:
    proposal = dict(row)
    proposal["details"] = json.loads(proposal.pop("details_json"))
    proposal["requires_approval"] = bool(proposal["requires_approval"])
    proposal["confirm_command"] = f"确认方案 {proposal['id']} v{proposal['version']}"
    proposal["cancel_command"] = f"取消方案 {proposal['id']} v{proposal['version']}"
    return proposal


def proposal_text(proposal: dict, explanation: str | None = None) -> str:
    """方案回复只说清商品、数量、金额、审批与下一步；条款原文、编号与退回要求放在方案卡片和案件详情中。"""
    details = proposal["details"]
    amount = proposal["refund_amount_cents"]
    subject = (f"订单 {details['order_id']} 的{details['product_name']}（{details['specification']}）{proposal['quantity']} 件，"
               f"原因：{proposal['reason']}，预计退款 {amount // 100}.{amount % 100:02d} 元（不含运费）")
    notes = ""
    if "quality_issue" in details["approval_reasons"]:
        notes += "质量原因需要人工核实，尚未认定责任。"
    if "high_amount" in details["approval_reasons"]:
        notes += "金额超过自动处理门槛，提交后需客服审批。"
    if not details["approval_reasons"]:
        notes += "无需审批。"
    if explanation:
        notes += explanation
    if proposal["status"] == "confirmed":
        return (f"你已确认退货方案：{subject}。{notes}\n"
                "点“提交退货申请”或回复“帮我提交”即可提交；提交前不会退款。")
    return (f"已为你生成退货方案（第 {proposal['version']} 版）：{subject}。{notes}\n"
            "请在下方方案卡片核对后点“确认此版本”，或直接回复“确认”；要改数量、原因等直接告诉我。确认前不会创建申请或退款。")


def assessment_details(assessment, draft):
    return {**{key: assessment[key] for key in SNAPSHOT_FIELDS},
            "opened": draft.get("opened"), "reason_kind": draft.get("reason_kind")}


def sync_proposal(connection: sqlite3.Connection, case_id: str, state: dict, result: dict) -> None:
    """在本轮事务内复用相同方案，或使旧方案失效并创建下一版。"""
    accepted = connection.execute(
        """SELECT r.id, r.status FROM return_requests r JOIN confirmations f ON f.id = r.confirmation_id
           JOIN proposals p ON p.id = f.proposal_id WHERE p.case_id = ? AND r.status NOT IN ('superseded', 'cancelled')""", (case_id,),
    ).fetchone()
    latest = connection.execute(
        "SELECT * FROM proposals WHERE case_id = ? ORDER BY version DESC LIMIT 1", (case_id,),
    ).fetchone()
    assessment = result.get("assessment")
    valid = assessment is not None and assessment["decision"] in ("eligible", "manual_review")
    details = None
    if valid:
        details = assessment_details(assessment, state["draft"])
    reusable = (latest is not None and latest["status"] in ("pending_confirmation", "confirmed")
                and json.loads(latest["details_json"]) == details)
    if accepted and (reusable or accepted["status"] not in ("pending_approval", "awaiting_return", "approval_rejected")):
        raise ProposalConflict(f"申请 {accepted['id']} 已受理，请查询进度；当前状态不能重复提交或直接修改。")
    if latest is not None and not reusable and latest["status"] in ("pending_confirmation", "confirmed"):
        invalidate(connection, latest["id"], "方案内容或业务条件已变化")
        state.pop("return_request_id", None)
    if not valid:
        state["proposal_id"] = None
        result["proposal"] = None
        return
    if not reusable:
        proposal_id = "PROP-" + secrets.token_hex(8)
        version = latest["version"] + 1 if latest else 1
        connection.execute(
            """INSERT INTO proposals (id, case_id, version, order_item_id, quantity, reason,
               refund_amount_cents, requires_approval, details_json, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending_confirmation')""",
            (proposal_id, case_id, version, assessment["order_item_id"], assessment["quantity"],
             assessment["reason"], assessment["refund_amount_cents"], assessment["requires_approval"],
             json.dumps(details, ensure_ascii=False)),
        )
        latest = connection.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,)).fetchone()
    proposal = proposal_view(latest)
    state["proposal_id"] = proposal["id"]
    result.update(proposal=proposal, status="confirmed" if proposal["status"] == "confirmed" else "awaiting_confirmation",
                  reply_text=proposal_text(proposal, result.get("policy_explanation")))


class ProposalService:
    def __init__(self, customers: CustomerService) -> None:
        self.customers = customers

    def current(self, session_id: str) -> dict:
        with connect_database(self.customers.db_path) as connection:
            _session_user(connection, session_id)
            case_id, state = case_state(connection, session_id)
            if case_id is None:
                return {"proposal": None}
            row = connection.execute("SELECT * FROM proposals WHERE id = ? AND case_id = ?",
                                     (state.get("proposal_id"), case_id)).fetchone()
            return {"proposal": proposal_view(row) if row else None}

    def decide(self, session_id: str, action: str, proposal_id: str | None = None,
               version: int | None = None, *, content: str,
               expected_case_id: str | None = None, expected_revision: int | None = None) -> dict:
        with connect_database(self.customers.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            user = _session_user(connection, session_id)
            case_id, state = case_state(connection, session_id)
            if case_id is None:
                raise ProposalNotFound("当前会话没有待办理的退货案件。")
            if expected_case_id is not None:
                progress = connection.execute("SELECT revision FROM case_progress WHERE case_id=?", (case_id,)).fetchone()
                if case_id != expected_case_id or progress is None or progress["revision"] != expected_revision:
                    raise ProposalConflict("识别期间案件已切换或更新，请查看最新进度后重新决定。")
            current = connection.execute("SELECT * FROM proposals WHERE id = ? AND case_id = ?",
                                         (state.get("proposal_id"), case_id)).fetchone()
            if proposal_id is not None:
                owned = connection.execute("SELECT * FROM proposals WHERE id = ? AND case_id = ?",
                                           (proposal_id, case_id)).fetchone()
                if owned is None:
                    raise ProposalNotFound("未找到当前会话的方案。")
                if current is None or current["id"] != proposal_id or owned["version"] != version:
                    raise ProposalConflict("方案已变更，请查看并确认最新版本。")
            accepted = connection.execute(
                """SELECT r.id, r.status FROM return_requests r JOIN confirmations f ON f.id = r.confirmation_id
                   JOIN proposals p ON p.id = f.proposal_id WHERE p.case_id = ? AND r.status NOT IN ('superseded', 'cancelled')""", (case_id,),
            ).fetchone()
            if accepted and (action != "cancel" or accepted["status"] not in (
                    "pending_approval", "awaiting_return", "approval_rejected", "in_transit", "awaiting_inspection",
                    "ready_for_refund", "manual_review_required")):
                raise ProposalConflict(f"申请 {accepted['id']} 已受理，请查询申请状态；当前操作不可执行。")
            if action == "confirm":
                if proposal_id is None or current is None or current["status"] not in ("pending_confirmation", "confirmed"):
                    raise ProposalConflict("当前方案不可确认，请先查看有效方案及版本。")
                draft = state["draft"]
                order = read_order(connection, draft["order_id"])
                if order is None or order["user_id"] != user["id"]:
                    raise ProposalNotFound("未找到当前用户的订单。")
                item = next(i for i in order["items"] if i["id"] == draft["order_item_id"])
                assessment = evaluate_return(order, item, draft)
                state["assessment"] = assessment
                result = {"assessment": assessment, "status": assessment_status(assessment), "reply_text": assessment_reply(assessment)}
                old_confirmation = connection.execute("SELECT * FROM confirmations WHERE proposal_id = ?", (proposal_id,)).fetchone()
                if old_confirmation and not valid(old_confirmation, current):
                    invalidate(connection, proposal_id, "原确认过期或已失效，需要重新展示并确认方案")
                sync_proposal(connection, case_id, state, result)
                if result.get("proposal") is None or result["proposal"]["id"] != proposal_id:
                    result["reply_text"] = "订单事实或规则校验结果已变化，本次未确认。" + result["reply_text"]
                else:
                    connection.execute(
                        """INSERT INTO confirmations (id, proposal_id, user_id, expires_at, snapshot_json)
                           VALUES (?, ?, ?, ?, ?) ON CONFLICT(proposal_id) DO NOTHING""",
                        ("CONF-" + secrets.token_hex(8), proposal_id, user["id"],
                         expires_at(self.customers.confirmation_ttl_seconds), json.dumps(snapshot(current), ensure_ascii=False)),
                    )
                    connection.execute("UPDATE proposals SET status = 'confirmed' WHERE id = ?", (proposal_id,))
                    result["proposal"]["status"] = "confirmed"
                    result.update(status="confirmed", reply_text=proposal_text(result["proposal"]))
                    result["confirmation_id"] = connection.execute(
                        "SELECT id FROM confirmations WHERE proposal_id = ?", (proposal_id,),
                    ).fetchone()["id"]
                    result["submit_command"] = f"提交申请 {result['confirmation_id']}"
            elif action == "cancel":
                if current is not None:
                    invalidate(connection, current["id"], "用户撤回办理与授权", cancelled=True)
                state.update(draft={}, assessment=None, cancelled=True)
                proposal = proposal_view(current) if current else None
                if proposal:
                    proposal["status"] = "cancelled"
                result = {"status": "cancelled", "proposal": proposal,
                          "reply_text": "已取消本次退货办理，原方案、确认及审批不再有效。已受理的申请也已停止推进，尚未执行退款。"}
                if accepted and accepted["status"] in ("in_transit", "awaiting_inspection", "ready_for_refund", "manual_review_required"):
                    result["reply_text"] += "退回记录已保留，请联系人工客服处理已寄出的实物。"
            else:
                raise ValueError("Unknown proposal action")
            connection.execute("UPDATE cases SET status = ?, summary = ? WHERE id = ? AND user_id = ?",
                               (result["status"], result["reply_text"], case_id, user["id"]))
            save_state(connection, session_id, case_id, state, [])
            messages = append_messages(connection, session_id, content, result.pop("reply_text"), build_card(result))
            return {**result, **messages, "case_id": case_id, "draft": state["draft"]}

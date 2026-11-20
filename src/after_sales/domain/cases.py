"""按客户归属读取案件检查点，并将原案件绑定到当前会话。"""

import json

from after_sales.domain.customer_service import CustomerService, _session_user
from after_sales.domain.proposals import ProposalNotFound, proposal_view, assessment_details
from after_sales.domain.authorizations import valid
from after_sales.domain.approvals import approval_view
from after_sales.domain.return_requests import REQUEST_SELECT, request_view, request_text
from after_sales.domain.returns import evaluate_return
from after_sales.storage.database import connect_database, append_messages, read_order
from after_sales.storage.tickets import ticket_summaries
from after_sales.storage.case_progress import save_pointer


def read_progress(connection, user_id: str, case_id: str) -> dict:
    row = connection.execute(
        """SELECT c.*, p.state_json, p.missing_fields_json, p.revision FROM cases c
           JOIN case_progress p ON p.case_id = c.id WHERE c.id = ? AND c.user_id = ?""",
        (case_id, user_id),
    ).fetchone()
    if row is None:
        raise ProposalNotFound("未找到当前用户的案件，请核对案件编号。")
    state = json.loads(row["state_json"])
    proposal = connection.execute(
        "SELECT * FROM proposals WHERE case_id = ? ORDER BY version DESC LIMIT 1", (case_id,),
    ).fetchone()
    confirmation = connection.execute(
        "SELECT * FROM confirmations WHERE proposal_id = ? AND user_id = ?",
        (proposal["id"], user_id),
    ).fetchone() if proposal else None
    request_row = connection.execute(
        REQUEST_SELECT + " AND p.case_id = ? AND p.id = ?",
        (user_id, user_id, case_id, state.get("proposal_id")),
    ).fetchone()
    request = request_view(request_row, connection) if request_row else None
    approval = connection.execute("SELECT * FROM approvals WHERE request_id = ?", (request["id"],)).fetchone() if request else None
    confirmation_valid = bool(proposal and proposal["status"] == "confirmed" and valid(confirmation, proposal))
    progress = {"case_id": case_id, "status": request["status"] if request else row["status"],
            "revision": row["revision"],
            "missing_fields": json.loads(row["missing_fields_json"]), "state": state,
            "proposal": proposal_view(proposal) if proposal else None,
            "confirmation": dict(confirmation) if confirmation_valid else None,
            "confirmation_record": dict(confirmation) if confirmation else None,
            "approval": approval_view(approval), "return_request": request,
            "confirmation_valid": confirmation_valid,
            "approval_valid": bool(proposal and proposal["status"] == "confirmed" and approval
                                   and approval["decision"] == "approved" and valid(approval, proposal))}
    if request:
        # 保留恢复接口已有的简要审批字段。
        request.update(approval_id=approval["id"] if approval else None,
                       approval_decision=approval["decision"] if approval else None,
                       approval_reason=approval["reason"] if approval else None)
    events = {e["kind"]: e for e in request["return_events"]} if request else {}
    progress.update(shipment=events.get("shipment"), receipt=events.get("receipt"),
                    inspection=events.get("inspection"), refund_operation=request["refund_operation"] if request else None)
    describe_progress(progress)
    progress["tickets"] = ticket_summaries(connection, case_id=case_id)
    if progress["tickets"] and not request:
        progress["next_step"] = progress["next_step"].replace("尚未创建人工工单。", "")
        progress["next_step"] += "已转交人工工单 " + "、".join(t["id"] for t in progress["tickets"]) + "，等待客服处理。"
    # 可执行动作还需符合最新订单和规则；这里只读评估，不使旧授权自动失效。
    if request and progress["status"] in ("pending_approval", "awaiting_return", "in_transit", "awaiting_inspection", "ready_for_refund"):
        order = read_order(connection, row["order_id"])
        item = next((i for i in order["items"] if i["id"] == proposal["order_item_id"]), None) if order and order["user_id"] == user_id else None
        assessment = evaluate_return(order, item, state["draft"]) if item else None
        unchanged = bool(assessment and assessment["decision"] in ("eligible", "manual_review")
                         and assessment_details(assessment, state["draft"]) == json.loads(proposal["details_json"]))
        approval_missing = proposal["requires_approval"] and progress["status"] != "pending_approval" and not progress["approval_valid"]
        if not unchanged or not confirmation_valid or (approval and not progress["approval_valid"]) or approval_missing:
            progress["available_actions"] = ["revalidate_request", "cancel_return"]
            progress["next_step"] = "订单条件、政策或授权已变化或过期，请重新核验原申请；核验完成前不能寄回或退款。"
    if "issue_refund" in progress["available_actions"]:
        unresolved = connection.execute(
            "SELECT id FROM refund_operations WHERE order_item_id = ? AND status IN ('pending', 'unknown')",
            (proposal["order_item_id"],),
        ).fetchone()
        if unresolved:
            progress["available_actions"] = ["contact_support", "cancel_return"]
            progress["next_step"] = "该商品存在未核清的退款操作，暂不能发起新退款；请联系人工核对原操作。"
    progress["summary"] = progress_text(progress)
    return progress


def describe_progress(progress):
    """当前客户可发起的操作；仓库、客服操作只作为等待原因展示。"""
    status, request, proposal = progress["status"], progress["return_request"], progress["proposal"]
    field_labels = {"order_id": "订单号", "item": "退货商品", "quantity": "退货数量",
                    "reason": "退货原因", "opened": "是否拆封"}
    if request:
        progress.update(status_label=request["status_label"], next_step=request["next_step"])
        actions = {
            "pending_approval": ["cancel_return"], "awaiting_return": ["submit_shipment", "cancel_return"],
            "in_transit": ["cancel_return"], "awaiting_inspection": ["cancel_return"],
            "ready_for_refund": ["issue_refund", "cancel_return"] if request["return_conditions_met"] else ["revalidate_request", "cancel_return"],
            "refunded": [], "refund_unknown": ["check_refund_operation"], "refund_failed": ["contact_support"],
            "approval_rejected": ["contact_support", "cancel_return"],
            "manual_review_required": ["contact_support", "cancel_return"], "cancelled": [], "superseded": [],
        }[status]
        if status == "cancelled" and progress["shipment"]:
            actions = ["contact_support"]
            progress["next_step"] += "已寄出的实物仍需人工处理，退回记录已保留。"
    else:
        label, message, actions = {
            "collecting_information": ("待补信息", "请补充：" + "、".join(field_labels.get(field, field) for field in progress["missing_fields"]), ["provide_information", "cancel_return"]),
            "awaiting_confirmation": ("待确认方案", "请核对方案后明确确认；尚未创建申请。", ["confirm_proposal", "modify_proposal", "cancel_return"]),
            "confirmed": ("方案已确认，待提交", "可提交已确认方案，尚未创建申请。", ["submit_return", "modify_proposal", "cancel_return"]),
            "cancelled": ("已取消", "本次办理已取消，原授权不可继续执行。", []),
            "ineligible": ("暂不符合退货规则", "请核对退货信息或联系人工客服。", ["provide_information", "contact_support", "cancel_return"]),
            "policy_unavailable": ("待人工核对政策", "缺少可靠政策依据，自动办理已停止，尚未创建人工工单。", ["contact_support", "cancel_return"]),
            "manual_review_required": ("待人工处理", "自动办理已停止，请联系人工客服核对，尚未创建人工工单。", ["contact_support", "cancel_return"]),
        }.get(status, (status, "请核对案件信息后继续办理。", ["provide_information"]))
        progress.update(status_label=label, next_step=message)
        if status == "confirmed" and not progress["confirmation_valid"]:
            actions = ["confirm_proposal", "modify_proposal", "cancel_return"]
            progress["next_step"] = "原确认已过期或失效，请重新核对方案并确认，尚未创建申请。"
        if proposal and "confirm_proposal" in actions:
            progress["next_step"] += f"可回复“{proposal['confirm_command']}”。"
        if "submit_return" in actions:
            progress["next_step"] += f"可回复“提交申请 {progress['confirmation']['id']}”。"
    progress["available_actions"] = actions


def progress_text(progress):
    text = f"案件 {progress['case_id']}，当前状态：{progress['status_label']}。"
    request = progress["return_request"]
    if request and progress["status"] in ("refunded", "refund_unknown"):
        text += request_text(request)
    if progress["approval"] and progress["approval"]["reason"] not in progress["next_step"]:
        text += "审批说明：" + progress["approval"]["reason"] + "。"
    if progress["inspection"] and progress["inspection"]["payload"]["reason"] not in progress["next_step"]:
        text += "检验说明：" + progress["inspection"]["payload"]["reason"] + "。"
    return text + progress["next_step"]


class CaseService:
    def __init__(self, customers: CustomerService) -> None:
        self.customers = customers

    def list_cases(self, session_id: str) -> list[dict]:
        with connect_database(self.customers.db_path) as connection:
            user = _session_user(connection, session_id)
            # 商品名称与订单号供页面列表展示，不需要客户阅读内部摘要里的操作编号。
            return [dict(row) for row in connection.execute(
                """SELECT c.id AS case_id, c.status, c.summary, c.order_id, c.quantity, c.created_at,
                          i.product_name, i.specification
                   FROM cases c LEFT JOIN order_items i ON i.id = c.order_item_id
                   WHERE c.user_id = ? ORDER BY c.created_at DESC, c.id""",
                (user["id"],),
            )]

    def get(self, session_id: str, case_id: str) -> dict:
        with connect_database(self.customers.db_path) as connection:
            connection.execute("BEGIN")
            user = _session_user(connection, session_id)
            return read_progress(connection, user["id"], case_id)

    def resume(self, session_id: str, case_id: str) -> dict:
        with connect_database(self.customers.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            user = _session_user(connection, session_id)
            progress = read_progress(connection, user["id"], case_id)
            # 只切换会话指针；下一轮工作流发现案件变化后从案件进度读取草稿。
            save_pointer(connection, session_id, case_id)
            # 给切换案件后的下一轮留下明确的会话边界，不重新执行图或业务写操作。
            append_messages(connection, session_id, f"恢复案件 {case_id}",
                            "已恢复。" + progress["summary"])
            return progress

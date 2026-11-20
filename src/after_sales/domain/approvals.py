"""客服审批已确认的申请；只推进退货阶段，不执行退款。"""

import json
import secrets

from after_sales.domain.customer_service import InvalidCustomer, InvalidSession
from after_sales.domain.proposals import ProposalConflict, ProposalNotFound, proposal_view
from after_sales.retrieval.policies import load_policy_chunks
from after_sales.storage.database import connect_database, read_order
from after_sales.domain.authorizations import expires_at, snapshot
from after_sales.domain.return_requests import revalidate_request
from after_sales.storage.tickets import create_request_ticket, ticket_summaries


class SupportRequired(ValueError):
    pass


def support_user(connection, session_id):
    row = connection.execute(
        "SELECT u.id, u.display_name, u.role FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.id = ?",
        (session_id,),
    ).fetchone()
    if row is None:
        raise InvalidSession("会话无效，请先选择演示客服身份。")
    if row["role"] != "support":
        raise SupportRequired("只有客服会话可以查看或处理审批。")
    return dict(row)


def approval_view(row):
    if row is None:
        return None
    result = dict(row)
    result["snapshot"] = json.loads(result.pop("snapshot_json"))
    return result


def review_data(connection, request_id):
    request = connection.execute(
        """SELECT r.*, p.id AS proposal_id, p.case_id, c.user_id, c.order_id FROM return_requests r
           JOIN confirmations f ON f.id = r.confirmation_id JOIN proposals p ON p.id = f.proposal_id
           JOIN cases c ON c.id = p.case_id WHERE r.id = ? AND f.user_id = c.user_id""", (request_id,),
    ).fetchone()
    if request is None:
        raise ProposalNotFound("未找到退货申请。")
    proposal = proposal_view(connection.execute("SELECT * FROM proposals WHERE id = ?", (request["proposal_id"],)).fetchone())
    order = read_order(connection, request["order_id"])
    return {"request": dict(request), "proposal": proposal, "order": order,
            "approval": approval_view(connection.execute("SELECT * FROM approvals WHERE request_id = ?", (request_id,)).fetchone())}


class ApprovalService:
    def __init__(self, customers):
        self.db_path = customers.db_path
        self.approval_ttl_seconds = customers.approval_ttl_seconds

    def list_staff(self):
        with connect_database(self.db_path) as connection:
            return [dict(row) for row in connection.execute("SELECT id, display_name FROM users WHERE role = 'support' ORDER BY id")]

    def create_session(self, support_id):
        """仅供本地模拟身份选择；与客户入口相同，不是生产登录认证。"""
        with connect_database(self.db_path) as connection:
            if not connection.execute("SELECT id FROM users WHERE id = ? AND role = 'support'", (support_id,)).fetchone():
                raise InvalidCustomer("请选择有效的模拟客服。")
            session_id = secrets.token_urlsafe(32)
            connection.execute("INSERT INTO sessions (id, user_id) VALUES (?, ?)", (session_id, support_id))
            return session_id, support_user(connection, session_id)

    def pending(self, session_id):
        with connect_database(self.db_path) as connection:
            support_user(connection, session_id)
            return [dict(row) for row in connection.execute(
                # 商品名、订单号与提交时间只用于客服列表展示和等待时长。
                """SELECT r.id AS request_id, p.case_id, p.id AS proposal_id, p.version, p.refund_amount_cents,
                          p.reason, p.order_item_id, p.quantity, r.created_at, i.product_name, i.order_id
                   FROM return_requests r
                   JOIN confirmations f ON f.id = r.confirmation_id JOIN proposals p ON p.id = f.proposal_id
                   JOIN order_items i ON i.id = p.order_item_id
                   WHERE r.status = 'pending_approval' AND p.requires_approval = 1 ORDER BY r.created_at, r.id"""
            )]

    def get(self, session_id, request_id):
        with connect_database(self.db_path) as connection:
            support_user(connection, session_id)
            result = review_data(connection, request_id)
        refs = result["proposal"]["details"].get("policy_refs", [])
        chunks = {p["chunk_id"]: p for p in load_policy_chunks()}
        # 展示引用的原版本；源文件变更后不能把新正文冒充旧证据。
        evidence = [chunks[p["chunk_id"]] for p in refs if p["chunk_id"] in chunks
                    and chunks[p["chunk_id"]]["source_sha256"] == p["source_sha256"]]
        result.update(policy_evidence=evidence, policy_evidence_complete=bool(refs) and len(evidence) == len(refs))
        return result

    def decide(self, session_id, request_id, *, proposal_id, version, decision, reason):
        with connect_database(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            reviewer = support_user(connection, session_id)
            data = review_data(connection, request_id)
            proposal, request, existing = data["proposal"], data["request"], data["approval"]
            if proposal["id"] != proposal_id or proposal["version"] != version:
                raise ProposalConflict("方案编号或版本不匹配，请重新查看待审批方案。")
            if proposal["status"] != "confirmed":
                raise ProposalConflict("原方案已失效，请查看并审批最新方案。")
            checked = revalidate_request(connection, request_id, require_approval=False)
            if checked.get("requires_reconfirmation") or checked["status"] == "cancelled":
                return checked
            if existing:
                if (existing["decision"], existing["reason"], existing["reviewer_id"]) != (decision, reason, reviewer["id"]):
                    raise ProposalConflict("此申请已有审批结论，不能覆盖；请查看已有记录。")
                return {"approval": existing, "status": request["status"], "reused": True,
                        "tickets": ticket_summaries(connection, request_id=request_id)}
            latest = connection.execute("SELECT id FROM proposals WHERE case_id = ? ORDER BY version DESC LIMIT 1", (proposal["case_id"],)).fetchone()
            if (request["status"] != "pending_approval" or not proposal["requires_approval"]
                    or proposal["status"] != "confirmed" or latest["id"] != proposal_id):
                raise ProposalConflict("申请不是当前已确认的待审批方案，不能审批。")
            approval_id = "APRV-" + secrets.token_hex(8)
            connection.execute(
                """INSERT INTO approvals (id, request_id, proposal_id, proposal_version, reviewer_id,
                   decision, reason, snapshot_json, operation_key, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (approval_id, request_id, proposal_id, version, reviewer["id"], decision, reason,
                 json.dumps(snapshot(proposal), ensure_ascii=False),
                 f"approve-return:{request_id}:{proposal_id}:v{version}", expires_at(self.approval_ttl_seconds)),
            )
            status = "awaiting_return" if decision == "approved" else "approval_rejected"
            summary = (f"申请 {request_id} 审批通过，等待退货、签收和检验，尚未退款。" if decision == "approved" else
                       f"申请 {request_id} 审批拒绝：{reason}。请联系人工客服补充材料并申请复核；本申请停止自动推进，尚未退款。")
            connection.execute("UPDATE return_requests SET status = ? WHERE id = ?", (status, request_id))
            connection.execute("UPDATE cases SET status = ?, summary = ? WHERE id = ?", (status, summary, proposal["case_id"]))
            connection.execute("UPDATE case_progress SET revision = revision + 1 WHERE case_id = ?", (proposal["case_id"],))
            if decision == "rejected":
                create_request_ticket(connection, request_id, "approval_rejected", reason,
                                      ["核对原方案和拒绝原因", "联系客户补充所缺材料并人工复核；原拒绝不能作为退款授权"])
            row = connection.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
            return {"approval": approval_view(row), "status": status, "reused": False,
                    "tickets": ticket_summaries(connection, request_id=request_id)}

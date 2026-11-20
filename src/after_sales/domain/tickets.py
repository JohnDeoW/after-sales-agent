"""客户只能查询本人工单，客服可读取转交证据和案件当前状态。"""

from after_sales.domain.customer_service import _session_user
from after_sales.domain.proposals import ProposalNotFound, ProposalConflict
from after_sales.storage.database import connect_database
from after_sales.storage.tickets import ticket_view, case_snapshot


class TicketService:
    def __init__(self, customers):
        self.db_path = customers.db_path

    def read(self, session_id, ticket_id=None, *, support=False):
        with connect_database(self.db_path) as connection:
            connection.execute("BEGIN")
            if support:
                from after_sales.domain.approvals import support_user
                support_user(connection, session_id)
                where, args = "1 = 1", []
            else:
                user = _session_user(connection, session_id)
                where, args = "user_id = ?", [user["id"]]
            if ticket_id:
                row = connection.execute(f"SELECT * FROM tickets WHERE {where} AND id = ?", (*args, ticket_id)).fetchone()
                if row is None:
                    raise ProposalNotFound("未找到可查询的工单。")
                ticket = ticket_view(row)
                ticket["updates"] = [dict(r) for r in connection.execute("SELECT * FROM ticket_updates WHERE ticket_id = ? ORDER BY id", (ticket_id,))]
                ticket["current_case"] = case_snapshot(connection, row["case_id"], row["user_id"]) if row["case_id"] else None
                return ticket
            return [dict(r) for r in connection.execute(
                f"SELECT id, case_id, request_id, source, reason, status, created_at FROM tickets WHERE {where} ORDER BY rowid DESC", args)]

    def reply(self, session_id, ticket_id, operation_key, reply):
        return self.update(session_id, ticket_id, operation_key, "replied", reply)

    def update(self, session_id, ticket_id, operation_key, status, reply=""):
        from after_sales.domain.approvals import support_user
        with connect_database(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            staff = support_user(connection, session_id)
            ticket = connection.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
            if ticket is None:
                raise ProposalNotFound("未找到可查询的工单。")
            if status not in ("in_progress", "replied", "resolved") or (status in ("replied", "resolved") and not reply.strip()):
                raise ProposalConflict("请选择有效处理状态；回复或解决工单时须填写答复。")
            previous = connection.execute("SELECT * FROM ticket_updates WHERE operation_key = ?", (operation_key,)).fetchone()
            if previous:
                if (previous["ticket_id"], previous["actor_id"], previous["reply"], previous["status"]) != (ticket_id, staff["id"], reply, status):
                    raise ProposalConflict("该操作编号已用于不同的工单答复。")
            else:
                if ticket["status"] == "resolved":
                    raise ProposalConflict("工单已解决，不能覆盖原处理结果。")
                connection.execute("INSERT INTO ticket_updates (ticket_id, actor_id, operation_key, status, reply) VALUES (?, ?, ?, ?, ?)",
                                   (ticket_id, staff["id"], operation_key, status, reply))
                connection.execute("UPDATE tickets SET status = ? WHERE id = ?", (status, ticket_id))
        return self.read(session_id, ticket_id, support=True)


def ticket_text(ticket):
    label = {"open": "待客服处理", "in_progress": "客服处理中", "replied": "客服已回复", "resolved": "已解决"}[ticket["status"]]
    text = f"人工工单 {ticket['id']}，当前状态：{label}。转交原因：{ticket['reason']}。"
    replies = [u["reply"] for u in ticket.get("updates", []) if u["reply"]]
    if replies:
        text += "客服答复：" + replies[-1] + "。"
    elif ticket["status"] == "open":
        text += "工单已保存，尚不代表客服已处理。"
    if ticket["status"] != "resolved":
        text += "待办：" + "；".join(ticket["todo"]) + "。"
    return text + "工单状态不代表退款或其他业务操作已执行。"

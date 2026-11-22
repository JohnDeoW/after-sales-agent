"""受控模拟退款：校验、操作记录和账本更新在同一个 SQLite 事务中。"""

import json
import secrets

from after_sales.domain.customer_service import _session_user
from after_sales.domain.proposals import ProposalConflict, ProposalNotFound
from after_sales.domain.refund_amounts import calculate_refund
from after_sales.domain.return_requests import REQUEST_SELECT, revalidate_request, request_view
from after_sales.storage.case_progress import save_case_state
from after_sales.storage.database import connect_database
from after_sales.storage.refunds import read_refund, operation_view, save_simulated_result, query_simulated_result


def simulate_refund(operation_id, amount_cents):
    """本地演示回执，无网络请求、真实付款或独立支付系统副作用。"""
    return {"status": "succeeded", "provider_reference": "SIM-" + operation_id,
            "amount_cents": amount_cents, "currency": "CNY", "simulated": True}


def submit_simulated_refund(connection, operation_id, amount_cents, scenario):
    """先保存模拟服务端结果，再模拟客户端收不到成功响应；查询始终用原编号。"""
    result = simulate_refund(operation_id, amount_cents)
    save_simulated_result(connection, operation_id, result)
    if scenario == "success_then_timeout" and result["status"] == "succeeded":
        raise TimeoutError("模拟退款成功，但响应超时")
    return result


def finish_refund(connection, operation, result):
    """保存模拟回执；只有明确成功且金额匹配才记账。与调用方共用事务。"""
    # 重读原操作，防止调用方持有旧快照时重复记账。
    operation = read_refund(connection, operation["request_id"])
    if operation["status"] in ("succeeded", "failed"):
        return
    status = result.get("status", "unknown")
    if status not in ("succeeded", "failed", "unknown"):
        status = "unknown"
    if status == "succeeded" and result.get("amount_cents") != operation["amount_cents"]:
        status = "unknown"
    if status == "succeeded":
        updated = connection.execute(
            """UPDATE order_items SET refunded_amount_cents = refunded_amount_cents + ?,
               returned_quantity = returned_quantity + ? WHERE id = ?
               AND refunded_amount_cents + ? <= paid_amount_cents AND returned_quantity + ? <= quantity""",
            (operation["amount_cents"], operation["quantity"], operation["order_item_id"],
             operation["amount_cents"], operation["quantity"]),
        )
        if updated.rowcount != 1:
            raise ProposalConflict("原操作回执已成功，但本地额度记录不一致，需人工核对；未重复发起退款。")
    connection.execute("UPDATE refund_operations SET status = ?, result_json = ? WHERE id = ?",
                       (status, json.dumps(result, ensure_ascii=False), operation["id"]))
    request_status = {"succeeded": "refunded", "failed": "refund_failed", "unknown": "refund_unknown"}[status]
    connection.execute("UPDATE return_requests SET status = ? WHERE id = ?", (request_status, operation["request_id"]))
    summary = (f"退款操作 {operation['id']}，状态 {request_status}，"
               f"金额 {operation['amount_cents'] // 100}.{operation['amount_cents'] % 100:02d} 元（模拟）。")
    case_id = operation["snapshot"]["case_id"]
    connection.execute("UPDATE cases SET status = ?, summary = ? WHERE id = ?", (request_status, summary, case_id))
    state = json.loads(connection.execute("SELECT state_json FROM case_progress WHERE case_id = ?", (case_id,)).fetchone()[0])
    state.update(completed=status == "succeeded", refund_operation_id=operation["id"])
    save_case_state(connection, case_id, state, [])


class RefundService:
    def __init__(self, customers):
        self.db_path = customers.db_path

    def list_scenarios(self, session_id):
        from after_sales.domain.approvals import support_user
        with connect_database(self.db_path) as connection:
            support_user(connection, session_id)
            return [dict(row) for row in connection.execute("""
                SELECT r.id, r.status, c.order_id, p.order_item_id, p.refund_amount_cents, i.product_name,
                       o.id AS operation_id, o.status AS operation_status,
                       COALESCE((SELECT s.scenario FROM refund_demo_scenarios s
                                 WHERE s.request_id = r.id ORDER BY s.rowid DESC LIMIT 1), 'normal') AS scenario
                FROM return_requests r
                JOIN confirmations f ON f.id = r.confirmation_id
                JOIN proposals p ON p.id = f.proposal_id
                JOIN cases c ON c.id = p.case_id
                JOIN order_items i ON i.id = p.order_item_id
                LEFT JOIN refund_operations o ON o.request_id = r.id
                ORDER BY r.rowid DESC
            """)]

    def select_scenario(self, session_id, request_id, operation_key, scenario):
        from after_sales.domain.approvals import support_user
        with connect_database(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            staff = support_user(connection, session_id)
            if not connection.execute("SELECT id FROM return_requests WHERE id = ?", (request_id,)).fetchone():
                raise ProposalNotFound("未找到退货申请。")
            if scenario not in ("normal", "success_then_timeout"):
                raise ProposalConflict("请选择支持的退款演示场景。")
            previous = connection.execute("SELECT * FROM refund_demo_scenarios WHERE operation_key = ?", (operation_key,)).fetchone()
            if previous:
                if (previous["request_id"], previous["scenario"], previous["selected_by"]) != (request_id, scenario, staff["id"]):
                    raise ProposalConflict("该操作编号已经用于不同的演示设置。")
                return {"selection": dict(previous), "reused": True}
            if read_refund(connection, request_id):
                raise ProposalConflict("退款操作已发出，不能修改演示场景；请查询原操作。")
            connection.execute("INSERT INTO refund_demo_scenarios (operation_key, request_id, scenario, selected_by) VALUES (?, ?, ?, ?)",
                               (operation_key, request_id, scenario, staff["id"]))
            return {"selection": dict(connection.execute("SELECT * FROM refund_demo_scenarios WHERE operation_key = ?", (operation_key,)).fetchone()), "reused": False}

    def get_operation_status(self, session_id, operation_id, *, refresh=False):
        with connect_database(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE" if refresh else "BEGIN")
            user = _session_user(connection, session_id)
            row = connection.execute(
                """SELECT o.* FROM refund_operations o JOIN return_requests r ON r.id = o.request_id
                   JOIN confirmations f ON f.id = r.confirmation_id JOIN proposals p ON p.id = f.proposal_id
                   JOIN cases c ON c.id = p.case_id WHERE o.id = ? AND o.user_id = ? AND c.user_id = ?""",
                (operation_id, user["id"], user["id"]),
            ).fetchone()
            if row is None:
                raise ProposalNotFound("未找到当前用户的退款操作，请核对操作编号。")
            operation = operation_view(row)
            reconciled = refresh and operation["status"] in ("pending", "unknown")
            if reconciled:
                # 查询是核对已发出的操作；即使确认已过期，也不能重发退款或丢弃已成功回执。
                result = query_simulated_result(connection, operation_id)
                finish_refund(connection, operation, result)
            request = connection.execute(REQUEST_SELECT + " AND r.id = ?",
                                         (user["id"], user["id"], operation["request_id"])).fetchone()
            operation = read_refund(connection, operation["request_id"])
            message = {"succeeded": "原操作已确认模拟退款成功，无需再次执行。",
                       "failed": "原操作已确认模拟退款失败，本次未记账，请联系人工处理。",
                       "pending": "原操作尚未取得最终结果，请核对原编号，不要重复退款。",
                       "unknown": "原操作结果仍待核对，尚不能确认成功或失败，不要重复退款。"}[operation["status"]]
            return {"operation": operation, "return_request": request_view(request, connection),
                    "reconciled": reconciled, "message": message}

    def set_simulated_result(self, session_id, operation_id, status, reason):
        """客服演示控制：只改变模拟服务回执；业务状态由原操作核对入口更新。"""
        from after_sales.domain.approvals import support_user

        with connect_database(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            reviewer = support_user(connection, session_id)
            row = connection.execute("SELECT * FROM refund_operations WHERE id = ?", (operation_id,)).fetchone()
            if row is None:
                raise ProposalNotFound("未找到退款操作。")
            result = {"status": status, "amount_cents": row["amount_cents"], "reason": reason,
                      "provider_reference": "SIM-" + operation_id, "currency": "CNY", "simulated": True}
            existing = query_simulated_result(connection, operation_id)
            if existing == result:
                return {"operation_id": operation_id, "result": existing, "reused": True}
            if row["status"] not in ("pending", "unknown") or existing.get("status") in ("succeeded", "failed"):
                raise ProposalConflict("原操作已有明确结果，不能用演示入口覆盖。")
            save_simulated_result(connection, operation_id, result, reviewer["id"])
            return {"operation_id": operation_id, "result": result, "reused": False}

    def issue_refund(self, session_id, request_id, operation_key):
        with connect_database(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            user = _session_user(connection, session_id)
            row = connection.execute(REQUEST_SELECT + " AND r.id = ?", (user["id"], user["id"], request_id)).fetchone()
            if row is None:
                raise ProposalNotFound("未找到当前用户的退货申请。")
            used_key = connection.execute("SELECT request_id FROM refund_operations WHERE operation_key = ?", (operation_key,)).fetchone()
            if used_key and used_key["request_id"] != request_id:
                raise ProposalConflict("该操作编号已用于其他申请，请核对操作编号。")
            existing = read_refund(connection, request_id)
            if existing:
                return {"operation": existing, "return_request": request_view(row, connection), "reused": True}
            request = request_view(row, connection)
            if request["status"] != "ready_for_refund" or not request["return_conditions_met"]:
                raise ProposalConflict("尚未满足退货签收、检验通过等条件，不能执行退款。")
            if connection.execute(
                "SELECT id FROM refund_operations WHERE order_item_id = ? AND status IN ('pending', 'unknown')",
                (request["order_item_id"],),
            ).fetchone():
                raise ProposalConflict("该商品已有结果待核对的退款操作，须先核对原操作。")
            checked = revalidate_request(connection, request_id)
            if not checked["authorization_valid"]:
                return checked
            item = dict(connection.execute("SELECT * FROM order_items WHERE id = ?", (request["order_item_id"],)).fetchone())
            from after_sales.domain.item_capacity import ensure_item_capacity
            ensure_item_capacity(connection, request["order_item_id"], request["quantity"], return_id=request_id,
                                 amount_cents=request["refund_amount_cents"])
            breakdown = calculate_refund(item, request["quantity"])
            amount = breakdown["refund_amount_cents"]
            if amount != request["refund_amount_cents"]:
                raise ProposalConflict("当前可退款金额与确认方案不一致，请重新核对方案。")
            operation_id = "REF-" + secrets.token_hex(8)
            selected = connection.execute("SELECT scenario FROM refund_demo_scenarios WHERE request_id = ? ORDER BY rowid DESC LIMIT 1", (request_id,)).fetchone()
            scenario = selected["scenario"] if selected else "normal"
            snapshot = {"case_id": request["case_id"], "order_id": request["order_id"],
                        "demo_scenario": scenario,
                        "proposal_id": request["proposal_id"], "proposal_version": request["proposal_version"],
                        "confirmation_id": request["confirmation_id"],
                        "approval_id": request["approval"]["id"] if request["approval"] else None,
                        "amount_breakdown": breakdown, "policy_refs": request["details"]["policy_refs"]}
            connection.execute(
                """INSERT INTO refund_operations (id, request_id, operation_key, user_id, order_item_id,
                   quantity, amount_cents, status, snapshot_json) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
                (operation_id, request_id, operation_key, user["id"], item["id"], request["quantity"], amount,
                 json.dumps(snapshot, ensure_ascii=False)),
            )
            try:
                result = submit_simulated_refund(connection, operation_id, amount, scenario)
            except TimeoutError:
                # 超时不能当作明确失败，也不能回滚掉供后续核对的原编号。
                result = {"status": "unknown", "reason": "模拟服务响应超时，需核对原操作", "simulated": True}
            finish_refund(connection, read_refund(connection, request_id), result)
            row = connection.execute(REQUEST_SELECT + " AND r.id = ?", (user["id"], user["id"], request_id)).fetchone()
            return {"operation": read_refund(connection, request_id), "return_request": request_view(row, connection), "reused": False}

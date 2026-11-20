"""退货与换货共用原商品数量；调用者在同一写事务中校验并写入申请。"""

from after_sales.domain.proposals import ProposalConflict


def ensure_item_capacity(connection, item_id, quantity, *, return_id=None, exchange_id=None, amount_cents=None):
    item = connection.execute("SELECT * FROM order_items WHERE id = ?", (item_id,)).fetchone()
    # 未核清的支付结果必须先查询，即使另一个申请只想占用剩余件数。
    unknown = connection.execute("SELECT id FROM refund_operations WHERE order_item_id = ? AND status IN ('pending', 'unknown') AND request_id != ?",
                                 (item_id, return_id or "")).fetchone()
    if unknown:
        raise ProposalConflict("该商品已有退款结果待核对，请先查询原退款操作，不能新建申请绕过核对。")
    returns = connection.execute("""SELECT COALESCE(SUM(p.quantity), 0), COALESCE(SUM(p.refund_amount_cents), 0)
        FROM return_requests r JOIN confirmations f ON f.id = r.confirmation_id
        JOIN proposals p ON p.id = f.proposal_id LEFT JOIN refund_operations o ON o.request_id = r.id
        WHERE p.order_item_id = ? AND r.id != ? AND r.status != 'refunded' AND COALESCE(o.status, '') != 'succeeded'
        AND (r.status NOT IN ('cancelled', 'superseded', 'approval_rejected')
             OR EXISTS (SELECT 1 FROM return_events e WHERE e.request_id = r.id AND e.kind = 'shipment'))""",
        (item_id, return_id or "")).fetchone()
    exchanges = connection.execute("""SELECT COALESCE(SUM(quantity), 0) FROM exchange_requests r
        WHERE order_item_id = ? AND id != ? AND (status != 'cancelled'
        OR EXISTS (SELECT 1 FROM exchange_events e WHERE e.request_id = r.id AND e.kind = 'shipment'))""",
        (item_id, exchange_id or "")).fetchone()[0]
    available = item["quantity"] - item["returned_quantity"] - returns[0] - exchanges
    if quantity > available:
        raise ProposalConflict(f"该商品已有售后占用或剩余数量不足，当前最多可申请 {max(0, available)} 件；请查询原申请，已寄回实物需人工核对。")
    if amount_cents is not None and amount_cents > item["paid_amount_cents"] - item["refunded_amount_cents"] - returns[1]:
        raise ProposalConflict("该商品的退款额度已被其他有效申请占用，请重新核对方案。")

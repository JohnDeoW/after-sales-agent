"""基于订单固化实付和已退款账本计算商品退款；金额全部为整数分。"""


def calculate_refund(item: dict, quantity: int) -> dict:
    remaining_quantity = item["quantity"] - item["returned_quantity"]
    if not 0 < quantity <= remaining_quantity:
        raise ValueError("退货数量超出剩余可退数量。")
    paid = item["paid_amount_cents"]
    unit, remainder = divmod(paid, item["quantity"])
    remainder_for_return = remainder if quantity == remaining_quantity else 0
    allocated = unit * quantity + remainder_for_return
    available = paid - item["refunded_amount_cents"]
    return {
        "item_paid_amount_cents": paid,
        "purchased_quantity": item["quantity"],
        "returned_quantity": item["returned_quantity"],
        "refunded_amount_cents": item["refunded_amount_cents"],
        "remaining_quantity": remaining_quantity,
        "return_quantity": quantity,
        "unit_amount_cents": unit,
        "remainder_amount_cents": remainder_for_return,
        "allocated_amount_cents": allocated,
        "available_amount_cents": available,
        "refund_amount_cents": min(allocated, available),
        # 优惠已经计入 item_paid_amount_cents；不重新估算折扣或附加费用。
        "shipping_refund_cents": 0,
        "compensation_cents": 0,
    }

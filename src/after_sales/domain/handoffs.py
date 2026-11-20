"""收集维修/质量争议并转交人工；材料引用不是识别或鉴定结果。"""

import hashlib
import json

from after_sales.domain.intake_service import _result, _items_text
from after_sales.domain.proposals import ProposalNotFound
from after_sales.storage.database import read_order
from after_sales.storage.tickets import create_ticket


def collect_handoff(state, update, content, get_order):
    draft = state.setdefault("service_request", {})
    if draft.get("kind") != update.intent and update.intent != "human":
        draft.clear()
    draft["kind"] = update.intent
    if "order_id" in update.clear_fields or (update.order_id and update.order_id != draft.get("order_id")):
        draft.clear()
        draft["kind"] = update.intent
    if "item" in update.clear_fields or (update.item and update.item != draft.get("item")):
        for key in ("item", "order_item_id", "reason", "material_refs", "statement_message"):
            draft.pop(key, None)
    for key in ("order_id", "item", "reason"):
        if key in update.clear_fields:
            draft.pop(key, None)
        if getattr(update, key) is not None:
            draft[key] = getattr(update, key)
    if update.material_refs:
        draft["material_refs"] = update.material_refs
    if update.reason is not None:
        draft["statement_message"] = content
    draft["original_message"] = content
    # 明确转人工不依赖外部只读工具可用；保存时仍从本地数据核对归属。
    if update.intent == "human":
        return _result("handoff_requested", "", service_handoff=dict(draft))
    order = get_order(draft["order_id"]).get("order") if draft.get("order_id") else None
    item = None
    if order:
        query = draft.get("item", "").strip().casefold()
        matches = [i for i in order["items"] if query and (query == i["id"].casefold() or query in (i["product_name"] + " " + i["specification"]).casefold())]
        item = matches[0] if len(matches) == 1 else (order["items"][0] if not query and len(order["items"]) == 1 else None)
    draft["order_item_id"] = item["id"] if item else None
    if update.intent != "human":
        if not order:
            return _result("collecting_information", "请提供本人的订单号，以便核对需要处理的商品。", ["order_id"])
        if item is None:
            return _result("collecting_information", "请选择商品项：" + _items_text(order["items"]), ["item"], items=order["items"])
        if not draft.get("reason", "").strip():
            return _result("collecting_information", "请描述故障或质量争议；材料引用可选，系统不会自动鉴定责任。", ["reason"])
    return _result("handoff_requested", "", service_handoff=dict(draft))


def create_service_ticket(connection, user_id, case_id, handoff):
    kind = handoff["kind"]
    order = read_order(connection, handoff.get("order_id"))
    if order and order["user_id"] != user_id:
        order = None
    item = next((i for i in order["items"] if i["id"] == handoff.get("order_item_id")), None) if order else None
    if kind != "human" and (order is None or item is None):
        raise ProposalNotFound("订单或商品项已变化，请重新核对。")
    case = connection.execute("SELECT * FROM cases WHERE id = ? AND user_id = ?", (case_id, user_id)).fetchone()
    linked_case = case_id if case and (case["order_id"] == (order["id"] if order else None)
                                     or (kind == "human" and not handoff.get("order_id"))) else None
    statement = handoff.get("reason") or "用户要求人工接入，请客服继续了解诉求"
    identity = {"kind": kind, "order_id": handoff.get("order_id"), "item_id": item["id"] if item else None,
                "statement": statement, "material_refs": handoff.get("material_refs", [])}
    fingerprint = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    evidence = {**identity, "order": order, "item": item, "original_message": handoff["original_message"],
                "original_statement": handoff.get("statement_message"),
                "material_review": "not_performed", "responsibility": "undetermined"}
    return create_ticket(connection, user_id=user_id, case_id=linked_case, source=kind,
                         operation_key=f"service:{user_id}:{linked_case}:{fingerprint}",
                         reason={"repair": "维修请求待人工评估", "quality_dispute": "质量争议待人工核实", "human": "用户主动请求人工处理"}[kind],
                         evidence=evidence, todo=["查看用户说明、商品信息和已有操作，缺少信息由客服补充", "按需核对材料并答复；材料未经自动识别，尚未认定责任"])

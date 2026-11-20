"""从本轮结构化结果生成助手消息附带的卡片数据，页面据此渲染方案、候选和记录。

卡片只是当时结果的快照：能否操作以最新业务状态为准，读取会话时再附上当前状态。
"""

import json


def _proposal_card(proposal: dict) -> dict:
    d = proposal["details"]
    return {"type": "return_proposal", "id": proposal["id"], "version": proposal["version"],
            "order_id": d.get("order_id"), "order_item_id": proposal["order_item_id"],
            "product_name": d.get("product_name"), "specification": d.get("specification"),
            "quantity": proposal["quantity"], "reason": proposal["reason"], "opened": d.get("opened"),
            "refund_amount_cents": proposal["refund_amount_cents"], "requires_approval": bool(proposal["requires_approval"]),
            "approval_reasons": d.get("approval_reasons", []),
            "policy_refs": [{k: p.get(k) for k in ("clause_id", "version", "title")} for p in d.get("policy_refs", [])]}


def _record(kind: str, record: dict) -> dict:
    return {"type": "record", "kind": kind, "id": record["id"], "status": record.get("status"),
            "status_label": record.get("status_label")}


def build_card(result: dict) -> dict | None:
    missing = result.get("missing_fields") or []
    if result.get("exchange_proposal"):
        proposal = result["exchange_proposal"]
        return {"type": "exchange_proposal", "id": proposal["id"], "version": proposal["version"], **{
            key: proposal["details"].get(key) for key in (
                "order_id", "order_item_id", "product_name", "original_specification", "target_specification",
                "quantity", "reason", "price_difference_cents")}}
    # 已创建的申请优先展示为记录卡；提交申请的结果里也带有已确认方案。
    for key in ("exchange_request", "return_request"):
        if isinstance(result.get(key), dict) and result[key].get("id"):
            return _record(key, result[key])
    if result.get("proposal") and result["proposal"]["status"] in ("pending_confirmation", "confirmed"):
        return _proposal_card(result["proposal"])
    if "item" in missing and result.get("items"):
        return {"type": "item_choice", "purpose": result.get("intent"), "items": [
            {"id": item["id"], "product_name": item["product_name"], "specification": item["specification"],
             "remaining": item["quantity"] - item["returned_quantity"]} for item in result["items"]]}
    if "target_specification" in missing and result.get("inventory"):
        return {"type": "spec_choice", "variants": [
            {"sku": v["sku"], "specification": v["specification"], "available": v["available"]}
            for v in result["inventory"]["variants"]]}
    if isinstance(result.get("ticket"), dict) and result["ticket"].get("id"):
        return _record("ticket", result["ticket"])
    if missing:
        return {"type": "missing_fields", "fields": missing}
    return None


def live_status(connection, card: dict) -> str | None:
    """读取卡片对应对象的当前状态，供页面判断按钮是否仍然有效。"""
    table = {"return_proposal": "proposals", "exchange_proposal": "exchange_proposals"}.get(card["type"])
    if card["type"] == "record":
        table = {"return_request": "return_requests", "exchange_request": "exchange_requests", "ticket": "tickets"}.get(card["kind"])
    if table is None:
        return None
    row = connection.execute(f"SELECT status FROM {table} WHERE id = ?", (card["id"],)).fetchone()
    return row["status"] if row else None


def dump(card: dict | None) -> str | None:
    return json.dumps(card, ensure_ascii=False) if card else None

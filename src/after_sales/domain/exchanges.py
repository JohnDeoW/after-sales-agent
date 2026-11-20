"""换货信息与方案；等价关系、库存和商品归属来自后端记录。"""

import json
import secrets

from after_sales.domain.message_cards import build_card
from after_sales.domain.customer_service import _session_user
from after_sales.domain.proposals import ProposalNotFound, ProposalConflict
from after_sales.domain.intake_service import _result, _items_text, match_order_item
from after_sales.storage.database import connect_database, read_order, append_messages
from after_sales.storage.case_progress import save_state
from after_sales.storage.tickets import create_ticket


def inventory_for_item(connection, user_id, order_id, item_id):
    order = read_order(connection, order_id)
    if not order or order["user_id"] != user_id:
        raise ProposalNotFound("未找到当前用户的订单，请核对订单号。")
    item = next((i for i in order["items"] if i["id"] == item_id), None)
    if item is None:
        raise ProposalNotFound("未找到当前订单的商品项。")
    original = connection.execute("SELECT * FROM inventory WHERE sku = ?", (item["sku"],)).fetchone()
    variants = [] if original is None else [dict(r) for r in connection.execute(
        "SELECT * FROM inventory WHERE product_group = ? AND price_cents = ? ORDER BY sku",
        (original["product_group"], original["price_cents"]))]
    return {"order": order, "item": item, "variants": variants, "simulated": True}


class ExchangeService:
    def __init__(self, customers):
        self.db_path = customers.db_path

    def get_inventory(self, session_id, order_id, item_id):
        with connect_database(self.db_path) as connection:
            connection.execute("BEGIN")
            user = _session_user(connection, session_id)
            return inventory_for_item(connection, user["id"], order_id, item_id)

    def proposal(self, session_id, proposal_id):
        with connect_database(self.db_path) as connection:
            user = _session_user(connection, session_id)
            row = connection.execute("SELECT * FROM exchange_proposals WHERE id = ? AND user_id = ?", (proposal_id, user["id"])).fetchone()
            if row is None:
                raise ProposalNotFound("未找到可查询的换货方案。")
            return proposal_view(row)

    def read(self, session_id, request_id=None, *, warehouse=False):
        from after_sales.domain.return_events import warehouse_user
        with connect_database(self.db_path) as connection:
            connection.execute("BEGIN")
            user = warehouse_user(connection, session_id) if warehouse else _session_user(connection, session_id)
            if request_id:
                return read_exchange(connection, request_id, None if warehouse else user["id"])
            if warehouse:
                return [read_exchange(connection, r["id"]) for r in connection.execute(
                    "SELECT id FROM exchange_requests ORDER BY rowid DESC")]
            return [read_exchange(connection, r["id"], user["id"]) for r in connection.execute(
                "SELECT id FROM exchange_requests WHERE user_id = ? ORDER BY rowid DESC", (user["id"],))]

    def create_exchange_request(self, session_id, proposal_id, version, *, content=None):
        with connect_database(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            user = _session_user(connection, session_id)
            proposal = connection.execute("SELECT * FROM exchange_proposals WHERE id = ? AND user_id = ?", (proposal_id, user["id"])).fetchone()
            if proposal is None:
                raise ProposalNotFound("未找到可确认的换货方案。")
            if proposal["version"] != version:
                raise ProposalConflict("换货方案版本不匹配，请核对最新方案。")
            previous = connection.execute("SELECT id FROM exchange_requests WHERE proposal_id = ?", (proposal_id,)).fetchone()
            reused = previous is not None
            if previous:
                result = read_exchange(connection, previous["id"], user["id"])
            else:
                if proposal["status"] != "pending_confirmation":
                    raise ProposalConflict("该换货方案已失效，请查看并确认最新方案。")
                d = json.loads(proposal["details_json"])
                stock = inventory_for_item(connection, user["id"], d["order_id"], proposal["order_item_id"])
                target = next((v for v in stock["variants"] if v["sku"] == proposal["target_sku"]), None)
                if (stock["order"]["status"] != "delivered" or stock["item"]["sku"] != d["original_sku"] or not target
                        or target["specification"] != d["target_specification"]):
                    raise ProposalConflict("订单或等价规格已变化，请重新选择换货方案。")
                from after_sales.domain.item_capacity import ensure_item_capacity
                ensure_item_capacity(connection, proposal["order_item_id"], proposal["quantity"])
                updated = connection.execute("UPDATE inventory SET available = available - ?, reserved = reserved + ? WHERE sku = ? AND available >= ?",
                    (proposal["quantity"], proposal["quantity"], proposal["target_sku"], proposal["quantity"]))
                if updated.rowcount != 1:
                    raise ProposalConflict("目标规格库存不足，尚未创建换货申请；可重新选规格、退款或转人工。")
                request_id = "EXC-" + secrets.token_hex(8)
                connection.execute("""INSERT INTO exchange_requests
                    (id, proposal_id, user_id, operation_key, order_item_id, quantity, target_sku, status, snapshot_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'awaiting_return', ?)""",
                    (request_id, proposal_id, user["id"], f"exchange:{proposal_id}:v{version}", proposal["order_item_id"],
                     proposal["quantity"], proposal["target_sku"], json.dumps({**d, "proposal_id": proposal_id, "version": version}, ensure_ascii=False)))
                connection.execute("UPDATE exchange_proposals SET status = 'confirmed' WHERE id = ?", (proposal_id,))
                result = read_exchange(connection, request_id, user["id"])
            saved = connection.execute("SELECT * FROM conversation_states WHERE session_id = ?", (session_id,)).fetchone()
            state = json.loads(saved["state_json"]) if saved else {"draft": {}}
            state.update(exchange_request_id=result["id"], last_intent="exchange_query")
            save_state(connection, session_id, saved["case_id"] if saved else None, state)
            messages = append_messages(connection, session_id, content or f"确认换货 {proposal_id} v{version}", exchange_text(result),
                                       build_card({"exchange_request": result}))
            return {"status": result["status"], "exchange_request": result, "reused": reused, **messages}

    def record(self, session_id, request_id, kind, operation_key, payload):
        from after_sales.domain.return_events import warehouse_user
        with connect_database(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            customer_action = kind in ("shipment", "cancel")
            user = _session_user(connection, session_id) if customer_action else warehouse_user(connection, session_id)
            request = read_exchange(connection, request_id, user["id"] if customer_action else None)
            previous = connection.execute("SELECT * FROM exchange_events WHERE operation_key = ?", (operation_key,)).fetchone()
            if previous is None:
                previous = connection.execute("SELECT * FROM exchange_events WHERE request_id = ? AND kind = ?", (request_id, kind)).fetchone()
            if previous:
                if (previous["request_id"], previous["kind"], previous["actor_id"], json.loads(previous["payload_json"])) != (request_id, kind, user["id"], payload):
                    raise ProposalConflict("操作编号或该换货阶段已有不同记录，不能覆盖。")
                return {"exchange_request": request, "event": exchange_event_view(previous), "reused": True}
            expected = {"shipment": "awaiting_return", "receipt": "in_transit", "inspection": "awaiting_inspection",
                        "dispatch": "ready_for_exchange", "delivery": "replacement_in_transit"}
            if kind == "cancel":
                if request["status"] in ("replacement_in_transit", "completed", "cancelled"):
                    raise ProposalConflict("换新商品已经发出或申请已经结束，不能直接取消；请联系人工。")
                status = "cancelled"
            else:
                if request["status"] != expected[kind]:
                    raise ProposalConflict("当前换货状态不允许此操作，请按寄回、签收、检验、换新发货、送达顺序办理。")
                if kind in ("shipment", "dispatch"):
                    from after_sales.domain.item_capacity import ensure_item_capacity
                    ensure_item_capacity(connection, request["order_item_id"], request["quantity"], exchange_id=request_id)
                status = {"shipment": "in_transit", "receipt": "awaiting_inspection",
                          "inspection": "ready_for_exchange" if payload.get("result") == "passed" else "manual_review_required",
                          "dispatch": "replacement_in_transit", "delivery": "completed"}[kind]
            if kind in ("cancel", "dispatch") or (kind == "inspection" and payload["result"] == "failed"):
                if request["inventory_held"]:
                    release = 0 if kind == "dispatch" else request["quantity"]
                    changed = connection.execute("UPDATE inventory SET available = available + ?, reserved = reserved - ? WHERE sku = ? AND reserved >= ?",
                        (release, request["quantity"], request["target_sku"], request["quantity"]))
                    if changed.rowcount != 1:
                        raise ProposalConflict("库存预留记录不一致，停止执行，请人工核对。")
                    connection.execute("UPDATE exchange_requests SET inventory_held = 0 WHERE id = ?", (request_id,))
                elif kind == "dispatch":
                    raise ProposalConflict("没有有效库存预留，不能换新发货。")
            event_id = "EXEVT-" + secrets.token_hex(8)
            connection.execute("INSERT INTO exchange_events (id, request_id, kind, actor_id, operation_key, payload_json) VALUES (?, ?, ?, ?, ?, ?)",
                (event_id, request_id, kind, user["id"], operation_key, json.dumps(payload, ensure_ascii=False)))
            connection.execute("UPDATE exchange_requests SET status = ? WHERE id = ?", (status, request_id))
            if kind == "cancel":
                connection.execute("UPDATE exchange_proposals SET status = 'cancelled' WHERE id = ?", (request["proposal_id"],))
            if status == "manual_review_required" or (kind == "cancel" and request["status"] != "awaiting_return"):
                create_ticket(connection, user_id=request["user_id"], source="exchange_review", operation_key=f"exchange-review:{request_id}:{kind}",
                              reason=payload.get("reason", "换货停止，退回实物需要人工处理"),
                              evidence={"exchange_request": read_exchange(connection, request_id), "responsibility": "undetermined"},
                              todo=["核对退回实物及检验记录，联系客户安排处理", "原换货不再自动发货，库存预留已释放"])
            result = read_exchange(connection, request_id)
            event = connection.execute("SELECT * FROM exchange_events WHERE id = ?", (event_id,)).fetchone()
            return {"exchange_request": result, "event": exchange_event_view(event), "reused": False}


def exchange_event_view(row):
    event = dict(row)
    event["payload"] = json.loads(event.pop("payload_json"))
    return event


def read_exchange(connection, request_id, user_id=None):
    row = connection.execute("SELECT * FROM exchange_requests WHERE id = ?", (request_id,)).fetchone()
    if row is None or (user_id is not None and row["user_id"] != user_id):
        raise ProposalNotFound("未找到可查询的换货申请。")
    result = dict(row)
    result["snapshot"] = json.loads(result.pop("snapshot_json"))
    result["events"] = [exchange_event_view(r) for r in connection.execute("SELECT * FROM exchange_events WHERE request_id = ? ORDER BY rowid", (request_id,))]
    label, next_step, actions = {
        "awaiting_return": ("已受理，待寄回", "库存已预留，请提交模拟退回单号；尚未换新。", ["shipment", "cancel"]),
        "in_transit": ("退回运输中", "等待仓库签收。", ["cancel"]),
        "awaiting_inspection": ("待检验", "等待仓库检验，尚未换新。", ["cancel"]),
        "ready_for_exchange": ("待换新发货", "检验通过，等待仓库使用预留库存发货。", ["cancel"]),
        "manual_review_required": ("待人工处理", "已停止换新并释放库存，请查询人工工单。", []),
        "replacement_in_transit": ("换新运输中", "换新商品已模拟发出，等待送达。", []),
        "completed": ("模拟换货完成", "换新商品已有模拟送达记录。", []),
        "cancelled": ("已取消", "库存预留已释放；已寄回的实物由人工处理。", []),
    }[result["status"]]
    result.update(status_label=label, next_step=next_step, available_actions=actions)
    dispatch = next((e for e in result["events"] if e["kind"] == "dispatch"), None)
    result["replacement_logistics"] = ({**dispatch["payload"], "status": "delivered" if result["status"] == "completed" else "in_transit",
                                       "simulated": True, "events": [e for e in result["events"] if e["kind"] in ("dispatch", "delivery")]} if dispatch else None)
    result["tickets"] = [dict(t) for t in connection.execute(
        "SELECT id, status, reason FROM tickets WHERE source = 'exchange_review' AND operation_key IN (?, ?)",
        (f"exchange-review:{request_id}:inspection", f"exchange-review:{request_id}:cancel"))]
    return result


def exchange_text(request):
    text = f"换货申请 {request['id']}：{request['status_label']}。{request['next_step']}"
    if request["replacement_logistics"]:
        shipment = request["replacement_logistics"]
        text += f"换新物流：{shipment['carrier']}，单号 {shipment['tracking_number']}。"
    if request["tickets"]:
        text += "人工工单：" + "、".join(t["id"] for t in request["tickets"]) + "。"
    return text


def collect_exchange(state, update, get_order, get_inventory):
    draft = state.setdefault("exchange", {})
    if "order_id" in update.clear_fields or (update.order_id and draft.get("order_id") != update.order_id):
        draft.clear()
    # 只有已核实的商品被换成另一件时才清空其余字段；首次选定商品沿用已给出的数量和目标规格。
    if "item" in update.clear_fields or (update.item and draft.get("order_item_id") and draft.get("item") != update.item):
        for key in ("item", "order_item_id", "quantity", "target_specification", "reason"):
            draft.pop(key, None)
    for key in ("order_id", "item", "quantity", "target_specification", "reason"):
        if key in update.clear_fields:
            draft.pop(key, None)
        value = getattr(update, key)
        if value is not None:
            draft[key] = value
    if not draft.get("order_id"):
        return _result("collecting_information", "请提供需要换货的订单号。", ["order_id"])
    order = get_order(draft["order_id"]).get("order")
    if not order:
        return _result("order_not_found", "未找到当前用户的订单，请核对订单号。", ["order_id"])
    candidates = order["items"]
    item = match_order_item(candidates, draft.get("item"))
    if item is None:
        draft.pop("order_item_id", None)
        return _result("collecting_information", "请选择要换货的商品项：" + _items_text(candidates), ["item"], items=candidates)
    draft["order_item_id"] = item["id"]
    if order["status"] != "delivered":
        return _result("exchange_unavailable", "订单尚未签收，不能创建换货方案。可查询物流或联系人工。")
    quantity = draft.get("quantity")
    remaining = item["quantity"] - item["returned_quantity"]
    if quantity is None or quantity <= 0 or quantity > remaining:
        draft.pop("quantity", None)
        return _result("collecting_information", f"请提供换货数量，当前剩余可选数量为 {remaining} 件。", ["quantity"])
    inventory = get_inventory(order["id"], item["id"])
    variants = inventory["variants"]
    if not variants:
        return _result("exchange_unavailable", "暂无可核对的同款等价规格，请联系人工。", alternatives=["human"])
    target_query = draft.get("target_specification", "").strip().casefold()
    target = next((v for v in variants if target_query in (v["sku"].casefold(), v["specification"].casefold())), None)
    if target is None:
        return _result("collecting_information", "请选择同款等价规格（不支持补差价）：" + "；".join(
            f"{v['sku']} {v['specification']}，可用 {v['available']} 件" for v in variants),
            ["target_specification"], inventory=inventory)
    if not draft.get("reason", "").strip():
        return _result("collecting_information", "请说明换货原因。", ["reason"], inventory=inventory)
    if target["available"] < quantity:
        return _result("exchange_out_of_stock", f"{target['specification']} 当前库存不足，尚未预留库存。可以重新选择规格、改为退货退款或联系人工；新方案需要重新确认。",
                       alternatives=["exchange", "return", "human"], inventory=inventory)
    details = {"order_id": order["id"], "order_item_id": item["id"], "original_sku": item["sku"],
               "product_name": item["product_name"], "original_specification": item["specification"],
               "target_sku": target["sku"], "target_specification": target["specification"],
               "quantity": quantity, "reason": draft["reason"], "price_difference_cents": 0}
    return _result("exchange_proposed", "", exchange_details=details, inventory=inventory)


def proposal_view(row):
    proposal = dict(row)
    proposal["details"] = json.loads(proposal.pop("details_json"))
    proposal["confirm_command"] = f"确认换货 {proposal['id']} v{proposal['version']}"
    return proposal


def save_exchange_proposal(connection, session_id, user_id, state, result):
    previous = connection.execute("SELECT * FROM exchange_proposals WHERE id = ? AND user_id = ?",
                                  (state.get("exchange_proposal_id"), user_id)).fetchone()
    if previous is None:
        previous = connection.execute("SELECT * FROM exchange_proposals WHERE session_id = ? AND user_id = ? AND status = 'pending_confirmation' ORDER BY version DESC LIMIT 1",
                                      (session_id, user_id)).fetchone()
    details = result.pop("exchange_details", None)
    if previous and previous["status"] == "confirmed":
        request = connection.execute("SELECT id, status FROM exchange_requests WHERE proposal_id = ?", (previous["id"],)).fetchone()
        if request and request["status"] not in ("completed", "cancelled"):
            if details != json.loads(previous["details_json"]):
                raise ProposalConflict("换货申请已受理，请先取消原申请并释放库存，再修改或切换方案。")
            result.update(status="exchange_request_found", exchange_request=read_exchange(connection, request["id"]))
            result["reply_text"] = exchange_text(result["exchange_request"])
            return
    if previous and previous["status"] == "pending_confirmation" and details == json.loads(previous["details_json"]):
        proposal = proposal_view(previous)
    else:
        if previous and previous["status"] == "pending_confirmation":
            connection.execute("UPDATE exchange_proposals SET status = 'superseded' WHERE id = ?", (previous["id"],))
        state.pop("exchange_proposal_id", None)
        if details is None:
            return
        version = connection.execute("SELECT COALESCE(MAX(version), 0) + 1 FROM exchange_proposals WHERE session_id = ?", (session_id,)).fetchone()[0]
        proposal_id = "EXPROP-" + secrets.token_hex(8)
        connection.execute("""INSERT INTO exchange_proposals
            (id, user_id, session_id, version, order_item_id, quantity, target_sku, reason, details_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", (proposal_id, user_id, session_id, version,
            details["order_item_id"], details["quantity"], details["target_sku"], details["reason"], json.dumps(details, ensure_ascii=False)))
        proposal = proposal_view(connection.execute("SELECT * FROM exchange_proposals WHERE id = ?", (proposal_id,)).fetchone())
    state["exchange_proposal_id"] = proposal["id"]
    result["exchange_proposal"] = proposal
    d = proposal["details"]
    result["reply_text"] = (f"换货方案 {proposal['id']} v{proposal['version']}：订单 {d['order_id']}，"
                            f"{d['product_name']} {d['original_specification']} → {d['target_specification']}，{d['quantity']} 件，"
                            f"原因：{d['reason']}；同款等价换货，无差价。"
                            f"请核对后回复“{proposal['confirm_command']}”。当前尚未创建申请或预留库存，执行时会再次核对。")

"""合并退货字段、匹配商品和找出缺失信息；流程由 LangGraph 编排。"""

import re

from after_sales.agent.intake import IntakeUpdate


DETAIL_FIELDS = ("quantity", "reason", "reason_kind", "opened")


def _result(status: str, text: str, missing: list[str] | None = None, **data) -> dict:
    return {"status": status, "reply_text": text, "missing_fields": missing or [], **data}


def _items_text(items: list[dict]) -> str:
    return "；".join(f"{item['id']}：{item['product_name']}（{item['specification']}，购买 {item['quantity']} 件）"
                    for item in items)


def match_order_item(items: list[dict], text: str | None, loose: bool = True) -> dict | None:
    """仅在当前订单中唯一命中时选择商品，允许规格与名称换序。
    loose：订单只有一件商品且描述与它有共同字（如错别字“鼠表”）时直接选中，方案仍需用户核对确认。"""
    query = "".join((text or "").split()).casefold()
    if not query:
        return items[0] if len(items) == 1 else None
    matches = []
    for item in items:
        name = "".join(item["product_name"].split()).casefold()
        spec = "".join(item["specification"].split()).casefold()
        # 例如“黑色鼠标”去掉已核实的规格后，“鼠标”能命中“无线鼠标”。
        remaining = query.replace(spec, "", 1) if spec and spec in query else ""
        if (query == item["id"].casefold() or query in name + spec
                or (remaining and remaining in name)):
            matches.append(item)
    if not matches and loose and len(items) == 1:
        only = items[0]
        spec = only["specification"].casefold()
        # 只容忍错别字：带否定，或提到与该商品不同的颜色/规格（如只有白色却说“黑色键盘”）时不选中。
        conflicting = any(color not in spec for color in re.findall(r"[^\W\d_]色", query))
        if (not re.search(r"不|非|别", query) and not conflicting
                and set(query) & set(only["product_name"].casefold())):
            return only
    return matches[0] if len(matches) == 1 else None


def resolve_order_ref(ref: str | None, order_ids: list[str]) -> str | None:
    """把“004”“4号订单”这类简写对应到本人订单号：结尾数字唯一相同时采用，否则原样交给后续核对。"""
    if not ref or ref in order_ids:
        return ref
    digits = re.findall(r"\d+", ref)
    if not digits:
        return ref
    hits = [i for i in order_ids if re.search(r"\d+$", i) and int(re.search(r"\d+$", i)[0]) == int(digits[-1])]
    return hits[0] if len(hits) == 1 else ref


def locate_items(orders: list[dict], text: str | None, busy: set[str] = frozenset()) -> tuple[list[dict], bool]:
    """在本人已签收订单中按描述找商品：返回（候选商品，是否按描述命中）；没有命中时返回全部可选商品。
    busy 为已有进行中退货/换货申请的商品项，不作为候选。"""
    pool = [{**item, "order_id": order["id"], "ordered_on": order["ordered_on"]}
            for order in orders if order["status"] == "delivered" for item in order["items"]
            if item["quantity"] > item["returned_quantity"] and item["id"] not in busy]
    if text:
        hits = [item for item in pool if match_order_item([item], text, loose=False)]
        if hits:
            return hits, True
    return pool, False


def orders_summary(orders: list[dict]) -> list[dict]:
    """给识别模型的本人订单摘要：编号、日期、状态与商品名称规格，不含金额等其他信息。"""
    return [{"id": o["id"], "ordered_on": o["ordered_on"], "status": o["status"],
             "items": [{"id": i["id"], "name": f"{i['product_name']} {i['specification']}"} for i in o["items"]]}
            for o in orders]


def order_choice_text(orders: list[dict]) -> str:
    return "、".join(f"{o['id']}（{o['ordered_on']} 下单，" + "、".join(f"{i['product_name']} {i['specification']}" for i in o["items"]) + "）"
                    for o in orders)


class ReturnIntake:
    def collect_return(self, draft: dict, update: IntakeUpdate, get_order) -> tuple[dict, dict]:
        previous_item_id = draft.get("order_item_id")
        if "order_id" in update.clear_fields or (
            update.order_id is not None and draft.get("order_id") is not None and update.order_id != draft["order_id"]
        ):
            draft.clear()
            previous_item_id = None
        if update.order_id is not None:
            draft["order_id"] = update.order_id
        if "item" in update.clear_fields:
            for field in ("item", "order_item_id", *DETAIL_FIELDS):
                draft.pop(field, None)
        if update.item is not None:
            draft["item"] = update.item

        order = None
        if draft.get("order_id"):
            order = get_order(draft["order_id"]).get("order")
        item = None
        if order:
            item = match_order_item(order["items"], draft.get("item"))
        item_id = item["id"] if item else None
        if previous_item_id is not None and previous_item_id != item_id:
            for field in DETAIL_FIELDS:
                draft.pop(field, None)
        draft["order_item_id"] = item_id
        for field in update.clear_fields:
            if field in DETAIL_FIELDS:
                draft.pop(field, None)
            if field == "reason":
                draft.pop("reason_kind", None)
        if update.reason is not None:
            draft.pop("reason_kind", None)
        for field in DETAIL_FIELDS:
            value = getattr(update, field)
            if value is not None:
                draft[field] = value

        # 案件外键只保存已核实的订单和商品，用户输入的未核实编号仅留在草稿中。
        case_fields = {**{k: draft.get(k) for k in ("quantity", "reason", "opened")},
                       "order_id": order["id"] if order else None, "order_item_id": item_id}
        if draft.get("quantity") is not None and (draft["quantity"] <= 0 or (
            item and draft["quantity"] > item["quantity"] - item["returned_quantity"]
        )):
            draft.pop("quantity")
            case_fields["quantity"] = None
            quantity_error = True
        else:
            quantity_error = False
        if not order:
            text = "请提供需要退货的订单号。" if not draft.get("order_id") else "未找到当前用户的订单，请核对订单号。"
            return _result("collecting_information", text, ["order_id"]), case_fields
        if not item:
            return _result("collecting_information", "请明确选择一个要退货的商品项：" + _items_text(order["items"]), ["item"], items=order["items"]), case_fields
        if item["quantity"] == item["returned_quantity"]:
            return _result("ready_for_evaluation", "", order=order, item=item), case_fields
        if draft.get("quantity") is None and not quantity_error and item["quantity"] - item["returned_quantity"] == 1:
            # 只剩 1 件可退时不再追问数量，方案中写明 1 件由用户核对。
            draft["quantity"] = case_fields["quantity"] = 1
        if draft.get("quantity") is None:
            prefix = "退货数量无效。" if quantity_error else ""
            return _result("collecting_information", f"{prefix}请问 {item['product_name']} 要退几件？当前剩余可选数量为 {item['quantity'] - item['returned_quantity']}。", ["quantity"]), case_fields
        if not draft.get("reason"):
            return _result("collecting_information", "请说明退货原因。", ["reason"]), case_fields
        if draft.get("reason_kind") != "quality" and draft.get("opened") is None:
            return _result("collecting_information", "商品是否已拆封？", ["opened"]), case_fields
        return _result("ready_for_evaluation", "", order=order, item=item), case_fields


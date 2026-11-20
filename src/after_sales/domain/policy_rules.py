"""为退货选择完整规则集，并核对索引正文与当前结构化源数据属于同一版本。"""

from after_sales.retrieval.index import INDEX_PATH, read_index_chunks
from after_sales.retrieval.policies import POLICY_PATH, load_policy_chunks
from after_sales.retrieval.search import applicability


class PolicyUnavailable(ValueError):
    pass


def return_policies(order: dict, item: dict, reason_kind: str | None) -> dict[str, dict]:
    if not item.get("sku") or not order.get("ordered_on") or not reason_kind:
        raise PolicyUnavailable("缺少商品SKU、下单日期或退货原因类型，无法确定适用政策。")
    try:
        source = load_policy_chunks(POLICY_PATH)
        indexed = {c["chunk_id"]: c for c in read_index_chunks(INDEX_PATH)}
    except (FileNotFoundError, ValueError, KeyError):
        raise PolicyUnavailable("政策源或索引不可用，请核对政策并重建索引后继续。") from None
    required = ["DEMO-QUALITY" if reason_kind == "quality" else "DEMO-RETURN-WINDOW",
                "DEMO-APPROVAL", "DEMO-REFUND-CONDITIONS", "DEMO-REFUND-AMOUNT", "DEMO-SHIPPING"]
    selected = {}
    for name in required + (["opening"] if reason_kind != "quality" else []):
        matches = []
        for clause in source:
            if not (clause["clause_id"] == name or (name == "opening" and clause["clause_id"].startswith("DEMO-OPENED-"))):
                continue
            applies, missing = applicability(clause, sku=item["sku"], ordered_on=order["ordered_on"], reason_kind=reason_kind)
            if applies and not missing:
                matches.append(clause)
        if len(matches) != 1:
            raise PolicyUnavailable(f"条款{name}缺失或存在同等适用冲突，需人工核对。")
        clause = matches[0]
        # 比较完整快照，包含编号、版本、正文、规则、适用条件及源内容摘要。
        if indexed.get(clause["chunk_id"]) != clause:
            raise PolicyUnavailable("政策索引正文与当前规则源的版本或内容不一致，停止自动办理；请核对并重建索引。")
        selected[name] = clause
    # 金额算法目前只实现下列策略；政策要求其他算法时不能继续套用旧计算。
    supported = {
        "DEMO-REFUND-AMOUNT": {"amount_unit": "cent", "amount_field": "paid_amount_cents",
                               "unit_allocation": "integer_division", "remainder_allocation": "last_returnable_unit",
                               "deduct_refunded_amount": True, "max_total": "item_paid_amount",
                               "max_quantity": "purchased_minus_returned"},
        "DEMO-APPROVAL": {"comparison": "gt", "currency": "CNY", "amount_unit": "cent"},
        "DEMO-SHIPPING": {"include_original_shipping_in_refund": False, "auto_reimburse_return_shipping": False},
    }
    for name, fields in supported.items():
        if any(selected[name]["rules"].get(key) != expected for key, expected in fields.items()):
            raise PolicyUnavailable(f"条款{name}要求当前未支持的计算方式，需人工核对。")
    return selected


def policy_references(policies: dict[str, dict]) -> list[dict]:
    return [{key: clause[key] for key in ("clause_id", "version", "chunk_id", "title",
                                         "source_path", "source_pointer", "source_sha256")}
            for clause in policies.values()]


def references_text(references: list[dict]) -> str:
    return "依据：" + "；".join(f"{p['clause_id']} / {p['version']}" for p in references) if references else ""

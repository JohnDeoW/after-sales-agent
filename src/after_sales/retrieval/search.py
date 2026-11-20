"""从有限文本候选中筛选商品、下单时间及原因适用的政策。"""

from datetime import date
from pathlib import Path

from after_sales.retrieval.index import INDEX_PATH, search_chunks, read_index_chunks


def policy_family(clause: dict) -> str:
    # 不同商品的拆封条款属于同一个规则类别；其他条款由稳定编号关联版本。
    name = clause["clause_id"]
    return "DEMO-OPENED" if name.startswith("DEMO-OPENED-") else name


def find_conflicts(clauses: list[dict]) -> list[list[dict]]:
    groups = {}
    for clause in clauses:
        groups.setdefault(policy_family(clause), []).append(clause)
    # 同等适用却有不同规则/正文时不能按检索分数或版本号擅自裁决。
    return [group for group in groups.values() if len(group) > 1 and any(
        (p["rules"], p["body"]) != (group[0]["rules"], group[0]["body"]) for p in group[1:])]


def applicability(clause: dict, *, sku: str | None, ordered_on: str | None,
                  reason_kind: str | None) -> tuple[bool, set[str]]:
    """先排除已知不适用条件，再返回真正需要澄清的信息。"""
    scope = clause["applies_to"]
    if sku and sku not in clause["product_scope"]["skus"]:
        return False, set()
    if reason_kind and scope["reason_kind"] not in ("any", reason_kind):
        return False, set()
    start, end = scope["effective_from"], scope["effective_until"]
    if ordered_on and ((start and ordered_on < start) or (end and ordered_on >= end)):
        return False, set()
    missing = set()
    if not sku:
        missing.add("sku")
    if (start or end) and not ordered_on:
        missing.add("ordered_on")
    if scope["reason_kind"] != "any" and not reason_kind:
        missing.add("reason_kind")
    return True, missing


def search_policy(question: str, *, sku: str | None = None, ordered_on: str | None = None,
                  reason_kind: str | None = None, index_path: Path = INDEX_PATH) -> dict:
    if ordered_on:
        ordered_on = date.fromisoformat(ordered_on).isoformat()
    if reason_kind not in (None, "quality", "other"):
        raise ValueError("原因类型须为quality、other或空。")
    context = {"sku": sku, "ordered_on": ordered_on, "reason_kind": reason_kind}
    try:
        candidates = search_chunks(question, limit=20, index_path=index_path)
        # 核对命中类别的其他版本，避免冲突版本因排名靠后而被截掉。
        families = {policy_family(c) for c in candidates}
        related = [c for c in read_index_chunks(index_path) if policy_family(c) in families] if families else []
    except FileNotFoundError:
        return {"status": "unavailable", "missing_fields": [], "policies": [], "conflicts": [], "context": context}
    policies, missing = [], set()
    applicable = []
    for clause in related:
        applies, fields = applicability(clause, sku=sku, ordered_on=ordered_on, reason_kind=reason_kind)
        if not applies:
            continue
        missing.update(fields)
        if not fields:
            applicable.append(clause)
    applicable_ids = {c["chunk_id"] for c in applicable}
    policies = [c for c in candidates if c["chunk_id"] in applicable_ids]
    conflicts = find_conflicts(applicable)
    # 缺少必要条件时不把某个版本作为默认办理依据返回。
    status = "conflict" if conflicts else ("clarification_needed" if missing else ("ok" if policies else "no_match"))
    return {"status": status, "missing_fields": sorted(missing),
            "policies": policies[:5] if status == "ok" else [],
            "conflicts": conflicts, "context": context}

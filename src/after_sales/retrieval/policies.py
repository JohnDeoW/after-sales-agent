"""以完整条款为片段，保留正文、规则和可追溯的来源快照。"""

import hashlib
import re
import json
from pathlib import Path


POLICY_PATH = Path(__file__).resolve().parents[1] / "domain" / "demo_rules.json"


def load_policy_chunks(source_path: Path = POLICY_PATH) -> list[dict]:
    source_path = source_path.resolve()
    source_bytes = source_path.read_bytes()
    data = json.loads(source_bytes)
    if data["schema_version"] != 1:
        raise ValueError("不支持的政策数据格式版本。")
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    # 通用条款适用于目录中的全部商品：展开为全部已登记 SKU，目录外的商品仍不适用。
    catalog = sorted({sku for scope in data["product_scopes"].values() for sku in scope.get("skus", [])})
    chunks = []
    seen = set()
    # 当前条款较短，按条款边界切分，避免把期限与例外条件拆开。
    for position, clause in enumerate(data["clauses"]):
        identity = (clause["clause_id"], clause["version"])
        if identity in seen:
            raise ValueError(f"政策编号与版本重复：{identity}")
        seen.add(identity)
        if not all(isinstance(clause[key], str) and clause[key].strip()
                   for key in ("clause_id", "version", "title", "body")):
            raise ValueError(f"政策缺少编号、版本、标题或正文：{identity}")
        scope = data["product_scopes"][clause["applies_to"]["product_scope"]]
        if scope.get("all_products"):
            scope = {**scope, "skus": catalog}
        category = clause.get("category")
        if category is not None and category not in data.get("categories", {}):
            raise ValueError(f"政策类别未定义：{identity} {category}")
        chunks.append({
            # 可执行规则只用于规则服务需要的条款；其余条款只用于咨询与检索。
            "rules": {},
            **clause,
            "category_title": data.get("categories", {}).get(category),
            "chunk_id": f"{identity[0]}@{identity[1]}",
            "product_scope": scope,
            "notice": data["notice"],
            "business_timezone": data["business_timezone"],
            "applicability_semantics": data["applicability_semantics"],
            "source_path": str(source_path),
            "source_pointer": f"/clauses/{position}",
            "source_sha256": source_hash,
        })
    return chunks


HANDBOOK_PATH = POLICY_PATH.with_name("policy_handbook.md")
FAQ_PATH = POLICY_PATH.with_name("policy_faq.json")
REFERENCE = re.compile(r"（依据：([^）]+)）\s*$")


def load_handbook_sections(path: Path = HANDBOOK_PATH, source_path: Path = POLICY_PATH) -> list[dict]:
    """长文档按段落读取：每段带所在章节标题和依据条款；依据缺失或条款不存在时报错。"""
    clause_ids = {chunk["clause_id"] for chunk in load_policy_chunks(source_path)}
    sections, headings = [], {}
    for block in re.split(r"\n\s*\n", path.read_text(encoding="utf-8").strip()):
        block = block.strip()
        if block.startswith("#"):
            level = len(block) - len(block.lstrip("#"))
            headings = {k: v for k, v in headings.items() if k < level} | {level: block.lstrip("#").strip()}
            continue
        if level_one_intro(headings):
            continue  # 文档开头的说明段落，不属于具体条款
        match = REFERENCE.search(block)
        refs = [ref.strip() for ref in match[1].split("、")] if match else []
        if not refs or not set(refs) <= clause_ids:
            raise ValueError(f"长文档段落缺少依据或引用了不存在的条款：{block[:30]}")
        sections.append({"section_id": f"HANDBOOK-{len(sections) + 1:03d}", "headings": [headings[k] for k in sorted(headings)],
                         "text": block, "clause_ids": refs, "source_path": str(path.resolve())})
    return sections


def level_one_intro(headings: dict) -> bool:
    return set(headings) == {1}


def load_faqs(path: Path = FAQ_PATH, source_path: Path = POLICY_PATH) -> list[dict]:
    """常见问答：每条带编号和依据条款；条款不存在或编号重复时报错。"""
    clause_ids = {chunk["clause_id"] for chunk in load_policy_chunks(source_path)}
    faqs = json.loads(path.read_text(encoding="utf-8"))["faqs"]
    if len({faq["faq_id"] for faq in faqs}) != len(faqs):
        raise ValueError("常见问答编号重复。")
    for faq in faqs:
        if not faq["clause_ids"] or not set(faq["clause_ids"]) <= clause_ids:
            raise ValueError(f"常见问答引用了不存在的条款：{faq['faq_id']}")
    return faqs

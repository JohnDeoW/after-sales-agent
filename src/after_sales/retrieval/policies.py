"""以完整条款为片段，保留正文、规则和可追溯的来源快照。"""

import hashlib
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
        chunks.append({
            **clause,
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

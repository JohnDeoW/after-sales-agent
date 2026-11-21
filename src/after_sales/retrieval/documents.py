"""政策语料转为 LangChain Document：完整条款作为父文档，检索用的小块作为子文档。

- 父文档：每条条款的一个版本（chunk_id = 编号@版本），检索最终返回完整父条款；
- 子文档：条款按句切成的小块、长文档按标题再按段落切分的段落、常见问答；
  每个子文档带 chunk_id 指向父条款（长文档段落与问答按“依据”关联，条款有多个版本时各关联一份），
  并带来源位置与适用条件元数据（商品范围、生效日期、原因类型），供向量检索时按元数据过滤。
"""

from datetime import date
from pathlib import Path

from langchain_core.documents import Document
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

from after_sales.retrieval.policies import FAQ_PATH, HANDBOOK_PATH, POLICY_PATH, REFERENCE, load_faqs, load_policy_chunks

ID_KEY = "chunk_id"
NO_START, NO_END = 0, 99991231
# 条款较短，按句切成小块；长文档按段落切分，段落长度上限保证期限与例外条件留在同一块。
CLAUSE_SPLITTER = RecursiveCharacterTextSplitter(chunk_size=80, chunk_overlap=20, separators=["。", "；", "，", ""], keep_separator="end")
HANDBOOK_SPLITTER = RecursiveCharacterTextSplitter(chunk_size=400, chunk_overlap=40, separators=["。", "；", ""], keep_separator="end")
HEADERS = [("#", "h1"), ("##", "h2"), ("###", "h3")]


def _date_number(value: str | None, default: int) -> int:
    return int(value.replace("-", "")) if value else default


def condition_metadata(chunk: dict) -> dict:
    """适用条件元数据（标量，便于向量库过滤）。"""
    applies = chunk["applies_to"]
    return {"clause_id": chunk["clause_id"], "version": chunk["version"], ID_KEY: chunk["chunk_id"],
            "category": chunk.get("category") or "", "product_scope": applies["product_scope"],
            "effective_from": _date_number(applies["effective_from"], NO_START),
            "effective_until": _date_number(applies["effective_until"], NO_END),
            "reason_kind": applies["reason_kind"]}


def parent_documents(source_path: Path = POLICY_PATH) -> list[Document]:
    return [Document(id=chunk["chunk_id"], page_content=f"{chunk['title']}\n{chunk['body']}",
                     metadata={**condition_metadata(chunk), "clause": chunk})
            for chunk in load_policy_chunks(source_path)]


def child_documents(source_path: Path = POLICY_PATH, handbook_path: Path = HANDBOOK_PATH,
                    faq_path: Path = FAQ_PATH) -> list[Document]:
    chunks = load_policy_chunks(source_path)
    versions = {}
    for chunk in chunks:
        versions.setdefault(chunk["clause_id"], []).append(chunk)
    children = []

    def add(text: str, parent: dict, source: str, ref: str) -> None:
        children.append(Document(page_content=text, metadata={
            **condition_metadata(parent), "source": source, "source_ref": ref,
            # 增量索引按来源分组：同一来源的子文档整体新增、更新或删除。
            "source_id": f"{source}:{ref}:{parent['chunk_id']}"}))

    for chunk in chunks:
        for index, piece in enumerate(CLAUSE_SPLITTER.split_text(chunk["body"])):
            # 用稳定的条款编号而非源文件位置，增删其他条款时不影响本条子文档的内容哈希。
            add(f"{chunk['title']}：{piece}", chunk, "clause", f"{chunk['chunk_id']}#{index}")
    sections = MarkdownHeaderTextSplitter(HEADERS, strip_headers=True).split_text(Path(handbook_path).read_text(encoding="utf-8"))
    number = 0
    for section in sections:
        heading = " / ".join(section.metadata[key] for _, key in HEADERS if key in section.metadata)
        # 标题切分后同一章节的段落以单个换行相连；本文档每段为一行。
        for paragraph in (p.strip() for p in section.page_content.split("\n") if p.strip()):
            match = REFERENCE.search(paragraph)
            if match is None:
                continue  # 文档开头的说明段落，不属于具体条款
            number += 1
            # 段落是溯源单位；只有超长段落才再按句切分，切出的小块沿用整段的依据。
            # 依据编号只用于关联父条款，不放进检索文本，避免编号字符干扰分词检索。
            for piece in HANDBOOK_SPLITTER.split_text(REFERENCE.sub("", paragraph).strip()):
                for clause_id in (ref.strip() for ref in match[1].split("、")):
                    for parent in versions[clause_id]:
                        add(f"{heading}：{piece}", parent, "handbook", f"HANDBOOK-{number:03d}")
    for faq in load_faqs(faq_path, source_path):
        for clause_id in faq["clause_ids"]:
            for parent in versions[clause_id]:
                add(f"问：{faq['question']}\n答：{faq['answer']}", parent, "faq", faq["faq_id"])
    return children


def scope_catalog(chunks: list[dict]) -> dict[str, set[str]]:
    """商品 SKU -> 适用的商品范围（含通用范围），用于向量检索按商品过滤。"""
    catalog = {}
    for chunk in chunks:
        for sku in chunk["product_scope"]["skus"]:
            catalog.setdefault(sku, set()).add(chunk["applies_to"]["product_scope"])
    return catalog


def vector_filter(context: dict, catalog: dict[str, set[str]]) -> dict | None:
    """把已知的商品、下单日期和原因类型转为 Chroma 元数据过滤条件；未知条件不过滤，由适用性检查追问。"""
    conditions = []
    sku, ordered_on, reason_kind = context.get("sku"), context.get("ordered_on"), context.get("reason_kind")
    if sku:
        conditions.append({"product_scope": {"$in": sorted(catalog.get(sku, {"__none__"}))}})
    if ordered_on:
        day = int(date.fromisoformat(str(ordered_on)).strftime("%Y%m%d"))
        conditions += [{"effective_from": {"$lte": day}}, {"effective_until": {"$gt": day}}]
    if reason_kind:
        conditions.append({"reason_kind": {"$in": [reason_kind, "any"]}})
    if not conditions:
        return None
    return conditions[0] if len(conditions) == 1 else {"$and": conditions}

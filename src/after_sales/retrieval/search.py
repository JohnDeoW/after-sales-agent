"""政策混合检索链：BM25 + 本地向量（RRF 融合）→ 交叉编码器重排 → 合并多问法 → 无依据阈值 → 适用性过滤 → 版本冲突检测。

LCEL 组成：
  {检索问法, 用户原话} 并行经 ContextualCompressionRetriever（EnsembleRetriever 父子检索 + 本地重排）
  → merge_queries（两问法结果合并去重：有重排得分时取最高分排序，否则按 RRF）
  → evidence_threshold（重排得分低于阈值的候选不作为依据）
  → applicability_filter（按商品、下单日期、原因类型筛选，并补齐同类条款的其他版本）
  → conflict_check（同等适用而规则不同的版本不能凭排名裁决）
返回结构与工具 search_policy 一致；检索结果只作为咨询证据，不决定办理资格与金额。
"""

from datetime import date
from functools import partial
from operator import itemgetter
from pathlib import Path

from langchain_classic.retrievers import ContextualCompressionRetriever, EnsembleRetriever
from langchain_core.runnables import RunnableBranch, RunnableLambda, RunnablePassthrough
from langchain_core.stores import InMemoryStore
from langchain_core.documents import Document

from after_sales.retrieval.documents import CLAUSE_SPLITTER, ID_KEY, scope_catalog, vector_filter
from after_sales.retrieval.index import CHAINS, INDEX_PATH, get_reranker, read_index_chunks, vector_store
from after_sales.retrieval.retrievers import ParentBM25Retriever, ScoredParentDocumentRetriever, ScoredReranker, tokenize

RRF_C = 60
MAX_CANDIDATES = 20
MODES = ("rerank", "hybrid", "bm25", "vector")  # rerank = 混合检索 + 交叉编码器重排
DEFAULT_MODE = "rerank"
RERANK_TOP_N = 8
# 交叉编码器原始得分低于此值视为无可靠依据。评测集上有依据与无依据问题的得分分布重叠，
# -2.05～-1.47 区间内指标相同，取其中间值；被误拒的有依据问题由线上“改写重查”补救。
RERANK_THRESHOLD = -1.8


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
        (p.get("rules"), p["body"]) != (group[0].get("rules"), group[0]["body"]) for p in group[1:])]


def applicability(clause: dict, *, sku: str | None, ordered_on: str | None,
                  reason_kind: str | None) -> tuple[bool, set[str]]:
    """先排除已知不适用条件，再返回真正需要澄清的信息。"""
    scope = clause["applies_to"]
    # all_products 表示与具体商品无关的通用条款（如运费、发票），不需要先确定商品。
    any_product = clause["product_scope"].get("all_products", False)
    if sku and sku not in clause["product_scope"]["skus"]:
        return False, set()
    if reason_kind and scope["reason_kind"] not in ("any", reason_kind):
        return False, set()
    start, end = scope["effective_from"], scope["effective_until"]
    if ordered_on and ((start and ordered_on < start) or (end and ordered_on >= end)):
        return False, set()
    missing = set()
    if not sku and not any_product:
        missing.add("sku")
    if (start or end) and not ordered_on:
        missing.add("ordered_on")
    if scope["reason_kind"] != "any" and not reason_kind:
        missing.add("reason_kind")
    return True, missing


# ---- 检索链 ----

def _merge_queries(results: dict) -> dict:
    """检索问法与用户原话两组结果合并去重：有重排得分时按两问法中的最高分排序，否则按 RRF。"""
    merged = {}
    for query_name in ("rewritten_hits", "original_hits"):
        for rank, document in enumerate(results[query_name]):
            key = document.metadata[ID_KEY]
            entry = merged.setdefault(key, {**document.metadata["clause"], "retrieval": {"rrf": 0.0, "queries": []}})
            info = entry["retrieval"]
            info["rrf"] += 1 / (RRF_C + rank + 1)
            info["queries"].append(query_name.removesuffix("_hits"))
            if "rerank_score" in document.metadata:
                info["rerank_score"] = max(info.get("rerank_score", float("-inf")), document.metadata["rerank_score"])
            for score_key in ("bm25_score", "vector_score", "matched"):
                if score_key in document.metadata and score_key not in info:
                    info[score_key] = document.metadata[score_key]

    def order(clause):
        info = clause["retrieval"]
        return (-info.get("rerank_score", 0.0), -info["rrf"], clause["chunk_id"])
    candidates = sorted(merged.values(), key=order)[:MAX_CANDIDATES]
    for clause in candidates:
        clause["retrieval"]["rrf"] = round(clause["retrieval"]["rrf"], 5)
    return {**results, "candidates": candidates}


def _evidence_threshold(results: dict, threshold: float) -> dict:
    """重排得分低于阈值的候选不作为依据；全部低于阈值即“无依据”（向量检索总会返回近邻，不能据此回答）。"""
    ranked = results["candidates"]
    scores = [c["retrieval"]["rerank_score"] for c in ranked if "rerank_score" in c["retrieval"]]
    if not scores:
        return {**results, "ranked": ranked, "best_score": None, "threshold": None}
    kept = [c for c in ranked if c["retrieval"]["rerank_score"] >= threshold]
    return {**results, "candidates": kept, "ranked": ranked, "best_score": max(scores), "threshold": threshold}


def _apply_applicability(results: dict, all_chunks: list[dict]) -> dict:
    sku, ordered_on, reason_kind = results.get("sku"), results.get("ordered_on"), results.get("reason_kind")
    candidates = results["candidates"]
    # 核对命中类别的其他版本，避免冲突版本因排名靠后而被截掉。
    families = {policy_family(c) for c in candidates}
    related = [c for c in all_chunks if policy_family(c) in families]
    missing, applicable = set(), []
    for clause in related:
        applies, fields = applicability(clause, sku=sku, ordered_on=ordered_on, reason_kind=reason_kind)
        if applies:
            missing.update(fields)
            if not fields:
                applicable.append(clause)
    applicable_ids = {c["chunk_id"] for c in applicable}
    policies = [c for c in candidates if c["chunk_id"] in applicable_ids]
    return {**results, "applicable": applicable, "policies": policies, "missing": missing}


def _conflict_check(results: dict) -> dict:
    conflicts = find_conflicts(results["applicable"])
    missing = results["missing"]
    # 缺少必要条件时不把某个版本作为默认办理依据返回。
    status = "conflict" if conflicts else ("clarification_needed" if missing else ("ok" if results["policies"] else "no_match"))
    policies = results["policies"][:5] if status == "ok" else []
    used = {p["chunk_id"] for p in policies}
    # 检索过程摘要（候选条款、各路得分、是否采用），供页面轨迹与 Phoenix 讲解；不含条款正文。
    retrieval = {"best_score": results.get("best_score"), "threshold": results.get("threshold"),
                 "candidates": [{"chunk_id": c["chunk_id"], "title": c["title"], "used": c["chunk_id"] in used,
                                 **{k: v for k, v in c["retrieval"].items() if k != "rrf"}}
                                for c in results.get("ranked", [])[:8]]}
    return {"status": status, "missing_fields": sorted(missing), "policies": policies, "conflicts": conflicts,
            "best_score": results.get("best_score"), "retrieval": retrieval,
            "context": {key: results.get(key) for key in ("sku", "ordered_on", "reason_kind")}}


def load_resources(index_path: Path = INDEX_PATH) -> dict:
    """按索引文件版本缓存检索所需的重资源（条款快照、向量库、BM25）；重建索引后自动换新。"""
    key = (str(index_path.resolve()), index_path.stat().st_mtime_ns)
    if key not in CHAINS:
        all_chunks = read_index_chunks(index_path)
        store = vector_store(index_path)
        docstore = InMemoryStore()
        docstore.mset([(c["chunk_id"], Document(id=c["chunk_id"], page_content=f"{c['title']}\n{c['body']}",
                                                metadata={ID_KEY: c["chunk_id"], "clause": c})) for c in all_chunks])
        stored = store.get(include=["documents", "metadatas"])
        children = [Document(page_content=text, metadata=meta) for text, meta in zip(stored["documents"], stored["metadatas"])]
        CHAINS[key] = {"chunks": all_chunks, "store": store, "docstore": docstore, "catalog": scope_catalog(all_chunks),
                       "bm25": ParentBM25Retriever.from_documents(children, preprocess_func=tokenize, docstore=docstore)}
    return CHAINS[key]


def build_chain(resources: dict, mode: str = DEFAULT_MODE, filters: dict | None = None,
                threshold: float = RERANK_THRESHOLD):
    """按本次检索的元数据过滤条件组装检索链（组装开销很小，重资源已缓存）。
    mode：rerank（混合 + 重排，默认）、hybrid（BM25 + 向量）、bm25、vector，后三者用于评测对比。"""
    if mode not in MODES:
        raise ValueError(f"未知检索方式：{mode}")
    retrievers = []
    if mode != "vector":
        retrievers.append(resources["bm25"].with_config(run_name="bm25_retriever"))
    if mode != "bm25":
        vector = ScoredParentDocumentRetriever(vectorstore=resources["store"], docstore=resources["docstore"],
                                               child_splitter=CLAUSE_SPLITTER, id_key=ID_KEY,
                                               search_kwargs={"filter": filters} if filters else {})
        retrievers.append(vector.with_config(run_name="vector_retriever"))
    retriever = EnsembleRetriever(retrievers=retrievers, weights=[1 / len(retrievers)] * len(retrievers), c=RRF_C, id_key=ID_KEY)
    if mode == "rerank":
        # 过滤条件已固化在本次组装的向量检索器中（ContextualCompressionRetriever 只向基础检索器传回调）。
        retriever = ContextualCompressionRetriever(
            base_compressor=ScoredReranker(model=get_reranker(), top_n=RERANK_TOP_N),
            base_retriever=retriever).with_config(run_name="reranked_retriever")
    optional = RunnableBranch((bool, retriever), RunnableLambda(lambda _: []))
    chain = (RunnablePassthrough.assign(rewritten_hits=itemgetter("question") | retriever,
                                        original_hits=RunnableLambda(lambda x: x.get("user_message") or "") | optional)
             | RunnableLambda(_merge_queries).with_config(run_name="merge_queries")
             | RunnableLambda(partial(_evidence_threshold, threshold=threshold)).with_config(run_name="evidence_threshold")
             | RunnableLambda(partial(_apply_applicability, all_chunks=resources["chunks"])).with_config(run_name="applicability_filter")
             | RunnableLambda(_conflict_check).with_config(run_name="conflict_check"))
    return chain.with_config(run_name="policy_retrieval")


def search_policy(question: str, *, sku: str | None = None, ordered_on: str | None = None,
                  reason_kind: str | None = None, user_message: str | None = None,
                  index_path: Path = INDEX_PATH, mode: str = DEFAULT_MODE, threshold: float = RERANK_THRESHOLD) -> dict:
    if ordered_on:
        ordered_on = date.fromisoformat(ordered_on).isoformat()
    if reason_kind not in (None, "quality", "other"):
        raise ValueError("原因类型须为quality、other或空。")
    context = {"sku": sku, "ordered_on": ordered_on, "reason_kind": reason_kind}
    if not index_path.is_file():
        return {"status": "unavailable", "missing_fields": [], "policies": [], "conflicts": [], "best_score": None,
                "retrieval": None, "context": context}
    resources = load_resources(index_path)
    chain = build_chain(resources, mode, vector_filter(context, resources["catalog"]), threshold)
    message = user_message if user_message and user_message.strip() != question.strip() else None
    return chain.invoke({"question": question, "user_message": message, **context})

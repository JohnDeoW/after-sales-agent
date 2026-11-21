"""政策索引：父条款快照（SQLite）+ 子文档本地向量（Chroma）+ 增量索引（LangChain 索引 API）。

- 向量化使用本地 FastEmbed 模型（BAAI/bge-small-zh-v1.5，ONNX），模型缓存于 data/models，不调用中转站；
  测试环境设置 AFTER_SALES_EMBEDDINGS=fake 时使用确定性替身向量，不加载模型；
- 重排序使用本地交叉编码器（BAAI/bge-reranker-base，ONNX），同样缓存于 data/models；
- 子文档经 index() + SQLRecordManager 按内容哈希增量写入：未改动的不重新计算向量，删除的从索引移除；
- 父条款完整快照写入 policy_chunks 表，供父子检索返回完整条款，并供规则服务核对索引与源数据一致。
"""

import argparse
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from after_sales.config import PROJECT_ROOT
from after_sales.retrieval.documents import child_documents
from after_sales.retrieval.policies import FAQ_PATH, HANDBOOK_PATH, POLICY_PATH, load_policy_chunks

INDEX_PATH = PROJECT_ROOT / "data" / "policy_index.sqlite3"
MODEL_DIR = PROJECT_ROOT / "data" / "models"
EMBEDDING_MODEL = "BAAI/bge-small-zh-v1.5"
RERANKER_MODEL = "BAAI/bge-reranker-base"
COLLECTION = "policy_children"
_embeddings = None
_reranker = None
_stores: dict = {}
CHAINS: dict = {}  # 检索链缓存（search.py 使用），持有向量库引用，释放时一并清空


def get_embeddings():
    """进程内只加载一次本地向量模型。"""
    global _embeddings
    if _embeddings is None:
        if os.environ.get("AFTER_SALES_EMBEDDINGS") == "fake":
            from langchain_core.embeddings import DeterministicFakeEmbedding
            _embeddings = DeterministicFakeEmbedding(size=512)
        else:
            from langchain_community.embeddings import FastEmbedEmbeddings
            _embeddings = FastEmbedEmbeddings(model_name=EMBEDDING_MODEL, cache_dir=str(MODEL_DIR))
    return _embeddings


def get_reranker():
    """进程内只加载一次本地交叉编码器；替身向量环境下用词覆盖率替身。"""
    global _reranker
    if _reranker is None:
        from after_sales.retrieval.retrievers import FastEmbedCrossEncoder, TokenOverlapCrossEncoder
        _reranker = (TokenOverlapCrossEncoder() if os.environ.get("AFTER_SALES_EMBEDDINGS") == "fake"
                     else FastEmbedCrossEncoder(RERANKER_MODEL, str(MODEL_DIR)))
    return _reranker


def vector_dir(index_path: Path) -> Path:
    return index_path.with_name(index_path.stem + "-vectors")


def vector_store(index_path: Path = INDEX_PATH):
    """子文档向量库（余弦距离）；同一索引在进程内复用一个客户端。"""
    from langchain_chroma import Chroma
    key = str(index_path.resolve())
    if key not in _stores:
        _stores[key] = Chroma(collection_name=COLLECTION, embedding_function=get_embeddings(),
                              persist_directory=str(vector_dir(index_path)), collection_metadata={"hnsw:space": "cosine"})
    return _stores[key]


def release_vector_stores() -> None:
    """释放向量库客户端（Windows 下 Chroma 会占用文件）；服务关闭或删除索引文件前调用。"""
    import gc
    from chromadb.api.shared_system_client import SharedSystemClient
    CHAINS.clear()
    _stores.clear()
    # clear_system_cache 只丢弃引用，需先停止各系统才会关闭 sqlite 与向量文件。
    for system in list(SharedSystemClient._identifier_to_system.values()):
        system.stop()
    SharedSystemClient.clear_system_cache()
    gc.collect()


def record_manager(index_path: Path):
    from langchain_classic.indexes import SQLRecordManager
    manager = SQLRecordManager(f"chroma/{COLLECTION}", db_url="sqlite:///" + index_path.resolve().as_posix())
    manager.create_schema()
    return manager


def sync_vectors(index_path: Path, children) -> dict:
    """按内容哈希增量同步子文档：返回新增、更新、跳过、删除的数量。"""
    from langchain_classic.indexes import index
    manager = record_manager(index_path)
    try:
        return dict(index(children, manager, vector_store(index_path), cleanup="full",
                          source_id_key="source_id", key_encoder="sha256"))
    finally:
        manager.engine.dispose()


def rebuild_index(source_path: Path = POLICY_PATH, index_path: Path = INDEX_PATH, *,
                  handbook_path: Path = HANDBOOK_PATH, faq_path: Path = FAQ_PATH) -> int:
    """重建父条款快照并增量同步子文档向量；返回父条款数量。坏数据在写入前报错，不破坏已有索引。"""
    chunks = load_policy_chunks(source_path)
    children = child_documents(source_path, handbook_path, faq_path)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(index_path)) as connection:
        with connection:
            connection.execute("DROP TABLE IF EXISTS policy_search")  # 旧版 FTS5 全文索引，已由混合检索替代
            connection.execute("CREATE TABLE IF NOT EXISTS policy_chunks (chunk_id TEXT PRIMARY KEY, document_json TEXT NOT NULL)")
            connection.execute("DELETE FROM policy_chunks")
            connection.executemany("INSERT INTO policy_chunks VALUES (?, ?)",
                                   [(c["chunk_id"], json.dumps(c, ensure_ascii=False)) for c in chunks])
    try:
        rebuild_index.last_result = sync_vectors(index_path, children)
    finally:
        # 重建是一次性步骤：结束后释放向量库文件，检索时再按需打开。
        release_vector_stores()
    return len(chunks)


def read_index_chunks(index_path: Path = INDEX_PATH) -> list[dict]:
    """规则服务读取完整父条款快照，不靠检索排名遗漏办理所需条款。"""
    if not index_path.is_file():
        raise FileNotFoundError("政策索引不存在，请先重建。")
    with closing(sqlite3.connect(index_path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        return [json.loads(row[0]) for row in connection.execute(
            "SELECT document_json FROM policy_chunks ORDER BY chunk_id")]


def run() -> None:
    parser = argparse.ArgumentParser(description="重建或查询本项目演示政策索引（本地向量 + BM25），不调用中转站模型。")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("rebuild", help="从政策源文件重建父条款快照，并增量同步子文档向量")
    search = commands.add_parser("search", help="查看混合检索结果（含适用性过滤）")
    search.add_argument("query", help="中文政策问题")
    search.add_argument("--sku")
    search.add_argument("--ordered-on")
    search.add_argument("--reason-kind", choices=["quality", "other"])
    args = parser.parse_args()
    if args.command == "rebuild":
        count = rebuild_index()
        print(f"已重建政策索引：{INDEX_PATH}；父条款：{count}；子文档同步：{rebuild_index.last_result}")
    else:
        from after_sales.retrieval.search import search_policy
        result = search_policy(args.query, sku=args.sku, ordered_on=args.ordered_on, reason_kind=args.reason_kind)
        print(json.dumps({"notice": "检索候选与适用性结果，不是办理结论。", "status": result["status"],
                          "missing_fields": result["missing_fields"],
                          "policies": [{k: p.get(k) for k in ("chunk_id", "title", "retrieval")} for p in result["policies"]]},
                         ensure_ascii=False, indent=2))
    release_vector_stores()


if __name__ == "__main__":
    run()

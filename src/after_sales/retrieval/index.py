"""中文双字切分 + SQLite FTS5检索；返回候选证据，不判定政策适用性。"""

import argparse
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from after_sales.config import PROJECT_ROOT
from after_sales.retrieval.policies import POLICY_PATH, load_policy_chunks


INDEX_PATH = PROJECT_ROOT / "data" / "policy_index.sqlite3"
WORD_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+|[a-z0-9]+", re.IGNORECASE)


def tokenize(text: str) -> list[str]:
    """连续中文取相邻双字，单字保留；英文和数字按连续词切分。"""
    terms = []
    for word in WORD_PATTERN.findall(text.lower()):
        if "\u3400" <= word[0] <= "\u9fff" and len(word) > 1:
            terms.extend(word[index:index + 2] for index in range(len(word) - 1))
        else:
            terms.append(word)
    return terms


def rebuild_index(source_path: Path = POLICY_PATH, index_path: Path = INDEX_PATH) -> int:
    # 先完成加载与校验；坏数据不能清空已有索引。
    chunks = load_policy_chunks(source_path)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(index_path)) as connection:
        connection.execute("""CREATE TABLE IF NOT EXISTS policy_chunks (
            chunk_id TEXT PRIMARY KEY, document_json TEXT NOT NULL
        )""")
        connection.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS policy_search USING fts5(
            chunk_id UNINDEXED, title, body, tokenize='unicode61'
        )""")
        with connection:
            connection.execute("DELETE FROM policy_search")
            connection.execute("DELETE FROM policy_chunks")
            connection.executemany(
                "INSERT INTO policy_chunks VALUES (?, ?)",
                [(chunk["chunk_id"], json.dumps(chunk, ensure_ascii=False)) for chunk in chunks],
            )
            connection.executemany(
                "INSERT INTO policy_search (chunk_id, title, body) VALUES (?, ?, ?)",
                [(chunk["chunk_id"], " ".join(tokenize(chunk["title"])),
                  " ".join(tokenize(chunk["body"]))) for chunk in chunks],
            )
    return len(chunks)


def read_index_chunks(index_path: Path = INDEX_PATH) -> list[dict]:
    """规则服务读取完整索引快照，不靠文本排名遗漏办理所需条款。"""
    if not index_path.is_file():
        raise FileNotFoundError("政策索引不存在，请先重建。")
    with closing(sqlite3.connect(index_path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        return [json.loads(row[0]) for row in connection.execute(
            "SELECT document_json FROM policy_chunks ORDER BY chunk_id")]


def search_chunks(query: str, *, limit: int = 5, index_path: Path = INDEX_PATH) -> list[dict]:
    """按文本相关性返回候选条款；商品和版本过滤由后续search_policy负责。"""
    if not 1 <= limit <= 20:
        raise ValueError("候选条款数量须为1至20。")
    terms = list(dict.fromkeys(tokenize(query)))
    if not terms:
        return []
    if not index_path.is_file():
        raise FileNotFoundError("政策索引不存在，请先运行 after-sales-policy-index rebuild。")
    # 仅将切分后的词作为字面量交给MATCH，不执行用户提供的FTS运算符。
    expression = " OR ".join(f'"{term}"' for term in terms)
    with closing(sqlite3.connect(index_path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        rows = connection.execute(
            """SELECT c.document_json, bm25(policy_search, 0.0, 3.0, 1.0) AS rank
               FROM policy_search JOIN policy_chunks c ON c.chunk_id = policy_search.chunk_id
               WHERE policy_search MATCH ? ORDER BY rank, c.chunk_id LIMIT ?""",
            (expression, limit),
        ).fetchall()
    return [{**json.loads(document), "score": -rank} for document, rank in rows]


def run() -> None:
    parser = argparse.ArgumentParser(description="重建或查询本项目演示政策索引，不调用模型。")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("rebuild", help="从政策源文件重新构建全部条款索引")
    search = commands.add_parser("search", help="查看文本检索候选；尚未做商品及版本过滤")
    search.add_argument("query", help="中文政策问题")
    search.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()
    if args.command == "rebuild":
        count = rebuild_index()
        print(f"已重建政策索引：{INDEX_PATH}；条款片段：{count}")
    else:
        print(json.dumps({"notice": "仅为文本检索候选，尚未判断适用性，不是办理结论。",
                          "chunks": search_chunks(args.query, limit=args.limit)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    run()

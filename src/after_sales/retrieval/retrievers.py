"""父子检索器：在子文档（小块）上检索，返回完整父条款，并把命中得分写入父条款元数据。

- ParentBM25Retriever：BM25Retriever 子类，jieba 分词，只保留得分大于 0（有共同词）的子文档；
- ScoredParentDocumentRetriever：ParentDocumentRetriever 子类，用带相关度的向量检索并按下限过滤，
  支持 Chroma 元数据过滤（search_kwargs["filter"]）；
- ScoredReranker：交叉编码器重排压缩器（BaseDocumentCompressor），保留重排得分供“无依据”阈值判断；
  由 ContextualCompressionRetriever 包装混合检索使用；
- 两路结果由 EnsembleRetriever 以 RRF 融合（见 search.py）。得分在 Phoenix 的检索 span 中可见。
"""

import logging
import re
from collections.abc import Sequence
from typing import Any

import jieba
from langchain_classic.retrievers import ParentDocumentRetriever
from langchain_community.retrievers import BM25Retriever
from langchain_community.cross_encoders import BaseCrossEncoder
from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.documents import BaseDocumentCompressor, Document

from after_sales.retrieval.documents import ID_KEY

jieba.setLogLevel(logging.WARNING)
# 售后领域词，避免被切碎（如“运费险”“七天无理由”）。
for word in ("运费险", "七天无理由", "无理由退货", "价保", "价格保护", "拆封", "试穿", "试用", "吊牌", "专票", "红冲",
             "仅退款", "补差价", "换码", "开箱即坏", "少件", "漏发", "错发", "保修", "过保", "寄修", "到账", "原路退回",
             "满减", "优惠券", "预售", "定金", "尾款", "定制", "拒收", "丢件", "签收"):
    jieba.add_word(word)
# 虚词与提问套话（“怎么办”“办理”“能否”等）不表达政策内容，计入 BM25 会把无关条款带进候选。
STOPWORDS = set("的 了 吗 呢 吧 啊 我 你 他 她 是 能 可以 还 也 就 都 要 会 在 和 与 及 或 有 没 不 怎么 什么 如何 一下 一个 这个 那个 请问 "
                "怎么办 怎样 办理 能否 是否 可不可以 行不行 可以吗 想 给 让".split())
TOKEN = re.compile(r"[㐀-鿿A-Za-z0-9]")


def tokenize(text: str) -> list[str]:
    """jieba 搜索模式分词，去掉标点与常见虚词。"""
    return [token for token in jieba.lcut_for_search(text.lower()) if TOKEN.search(token) and token not in STOPWORDS]


def _parents(hits, docstore, score_key: str, limit: int) -> list[Document]:
    """子文档命中按排名映射到父条款：同一父条款只保留排名最高的一次，并记录得分与命中来源。"""
    parents, seen = [], set()
    for child, score in hits:
        parent_id = child.metadata[ID_KEY]
        if parent_id in seen:
            continue
        seen.add(parent_id)
        parent = docstore.mget([parent_id])[0]
        if parent is None:
            continue
        parents.append(Document(id=parent.id, page_content=parent.page_content, metadata={
            **parent.metadata, score_key: round(float(score), 4),
            "matched": f"{child.metadata.get('source')}:{child.metadata.get('source_ref')}"}))
        if len(parents) >= limit:
            break
    return parents


class ParentBM25Retriever(BM25Retriever):
    docstore: Any = None
    parent_k: int = 8
    child_k: int = 30

    def _get_relevant_documents(self, query: str, *, run_manager: CallbackManagerForRetrieverRun) -> list[Document]:
        scores = self.vectorizer.get_scores(self.preprocess_func(query))
        ranked = sorted(zip(self.docs, scores), key=lambda pair: pair[1], reverse=True)
        hits = [(doc, score) for doc, score in ranked[:self.child_k] if score > 0]
        return _parents(hits, self.docstore, "bm25_score", self.parent_k)


class ScoredParentDocumentRetriever(ParentDocumentRetriever):
    min_relevance: float = 0.5
    parent_k: int = 8
    child_k: int = 30

    def _get_relevant_documents(self, query: str, *, run_manager: CallbackManagerForRetrieverRun) -> list[Document]:
        hits = self.vectorstore.similarity_search_with_relevance_scores(query, k=self.child_k, **self.search_kwargs)
        return _parents([(doc, score) for doc, score in hits if score >= self.min_relevance],
                        self.docstore, "vector_score", self.parent_k)


class FastEmbedCrossEncoder(BaseCrossEncoder):
    """本地交叉编码器（FastEmbed ONNX，如 BAAI/bge-reranker-base），输出未归一化的相关度得分。"""

    def __init__(self, model_name: str, cache_dir: str):
        from fastembed.rerank.cross_encoder import TextCrossEncoder
        self.model = TextCrossEncoder(model_name=model_name, cache_dir=cache_dir)

    def score(self, text_pairs: list[tuple[str, str]]) -> list[float]:
        return [float(value) for value in self.model.rerank_pairs(text_pairs)]


class TokenOverlapCrossEncoder(BaseCrossEncoder):
    """测试替身：按问题词在文档中的覆盖率给分（-5 到 5），不加载模型。"""

    def score(self, text_pairs: list[tuple[str, str]]) -> list[float]:
        scores = []
        for query, text in text_pairs:
            words = set(tokenize(query))
            scores.append(10 * len(words & set(tokenize(text))) / len(words) - 5 if words else -5.0)
        return scores


class ScoredReranker(BaseDocumentCompressor):
    """用交叉编码器给（问题，完整条款）打分并重排，得分写入 rerank_score。"""
    model: Any
    top_n: int = 8

    def compress_documents(self, documents: Sequence[Document], query: str, callbacks=None) -> Sequence[Document]:
        if not documents:
            return []
        scores = self.model.score([(query, doc.page_content) for doc in documents])
        ranked = sorted(zip(documents, scores), key=lambda pair: pair[1], reverse=True)[:self.top_n]
        return [Document(id=doc.id, page_content=doc.page_content,
                         metadata={**doc.metadata, "rerank_score": round(float(score), 4)}) for doc, score in ranked]

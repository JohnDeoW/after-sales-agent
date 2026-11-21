"""端到端回答评测（离线，会调用真实模型；不改变在线对话的模型调用次数）。

与线上政策咨询相同的路径：检索（改写问法 + 原话）→ 无可靠依据时模型改写问法重查一次 → PolicyExplainer 回答
（可判 no_answer）→ 另一次模型调用作为评判，逐项核对回答中的政策结论能否由所引用条款原文直接支持。
指标：忠实率、引用命中期望条款比例、有依据问题被误拒比例、无依据问题正确拒答比例、改写重查次数与耗时。
"""

import json
import statistics
from pathlib import Path
from time import perf_counter

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict, Field

from after_sales.agent.llm import create_model, invoke_model
from after_sales.agent.policy import PolicyExplainer, PolicyQueryRewriter, validate_answer
from after_sales.retrieval.search import search_policy
from retrieval_eval import matches, retrieve


class FaithfulnessVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    unsupported_claims: list[str] = Field(description="回答中无法由引用条款原文直接支持的结论（逐条摘出）；全部有依据时为空列表")
    faithful: bool = Field(description="回答中的全部政策结论都能由引用条款原文直接支持时为 true")
    reason: str = Field(description="一句话说明判断理由")


class FaithfulnessJudge:
    def __init__(self, settings):
        self.model = create_model(settings).bind_tools(
            [FaithfulnessVerdict], tool_choice="FaithfulnessVerdict", strict=True, parallel_tool_calls=False)

    def __call__(self, question: str, answer: str, citations: list[dict]) -> FaithfulnessVerdict:
        evidence = [{"chunk_id": c["chunk_id"], "title": c["title"], "body": c["body"]} for c in citations]
        reply = invoke_model(self.model, [
            SystemMessage("你是售后政策回答的忠实度评判，只调用FaithfulnessVerdict。逐项检查回答中的每个政策结论"
                          "（期限、条件、费用、例外、办理方式等）能否由所给引用条款原文直接得出。"
                          "条款没写的内容、改变了数字或条件的内容、把“可能”说成“一定”的内容都算无依据；"
                          "礼貌用语、复述问题、“仅为咨询、未办理”之类说明不算政策结论。不评价回答是否完整。"),
            HumanMessage(json.dumps({"question": question, "answer": answer, "citations": evidence}, ensure_ascii=False)),
        ])
        return FaithfulnessVerdict.model_validate(reply.tool_calls[0]["args"])


def pick(cases: list[dict], limit: int | None) -> list[dict]:
    """全部无依据样例 + 有依据样例；限制数量时对有依据样例等间隔抽样，保持诉求与品类分布。"""
    answerable = [c for c in cases if c["answerable"]]
    if limit and limit < len(answerable):
        step = len(answerable) / limit
        answerable = [answerable[int(i * step)] for i in range(limit)]
    return answerable + [c for c in cases if not c["answerable"]]


def evaluate_faithfulness(settings, cases: list[dict], index_path: Path, mode: str) -> dict:
    explainer, judge, rewriter = PolicyExplainer(settings), FaithfulnessJudge(settings), PolicyQueryRewriter(settings)
    rows = []
    for case in cases:
        started = perf_counter()
        retrieved = retrieve(case, "combined", index_path, mode)
        row = {"id": case["id"], "answerable": case["answerable"], "question": case["question"], "expected": case["expected"],
               "requery": None}
        if retrieved["status"] == "no_match":
            # 与线上一致：首次无可靠依据时改写问法重查一次。
            row["requery"] = rewriter(retrieved["query"], case["question"])
            result = search_policy(row["requery"], user_message=case["question"], index_path=index_path, mode=mode, **case["context"])
            retrieved = {**retrieved, "query": row["requery"], "status": result["status"], "policies": result["policies"]}
        row.update(retrieval_status=retrieved["status"], retrieval_ms=round((perf_counter() - started) * 1000, 2))
        if retrieved["status"] != "ok":
            rows.append({**row, "outcome": "no_evidence"})
            continue
        started = perf_counter()
        answer = validate_answer(explainer(retrieved["query"], retrieved["policies"]), retrieved["policies"])
        row["answer_ms"] = round((perf_counter() - started) * 1000, 2)
        if answer["status"] == "no_answer":
            rows.append({**row, "outcome": "explainer_no_answer"})
            continue
        verdict = judge(case["question"], answer["text"], answer["citations"])
        rows.append({**row, "outcome": "answered", "answer": answer["text"],
                     "citations": [c["chunk_id"] for c in answer["citations"]],
                     "citation_hit": any(matches(e, c) for e in case["expected"] for c in answer["citations"]),
                     "faithful": verdict.faithful, "unsupported_claims": verdict.unsupported_claims, "reason": verdict.reason})
        print(json.dumps({k: rows[-1].get(k) for k in ("id", "faithful", "citation_hit", "requery", "answer_ms")}, ensure_ascii=False), flush=True)
    answered = [r for r in rows if r["outcome"] == "answered"]
    positives, negatives = [r for r in rows if r["answerable"]], [r for r in rows if not r["answerable"]]
    rate = lambda items, key: round(sum(r[key] for r in items) / len(items), 4) if items else None  # noqa: E731
    mean = lambda values: round(statistics.mean(values), 2) if values else None  # noqa: E731
    refused = lambda items: round(sum(r["outcome"] != "answered" for r in items) / len(items), 4) if items else None  # noqa: E731
    summary = {"cases": len(rows), "answerable": len(positives), "unanswerable": len(negatives), "answered": len(answered),
               "false_refusal_rate": refused(positives), "correct_refusal_rate": refused(negatives),
               "requeried": sum(r["requery"] is not None for r in rows),
               "requery_recovered": sum(r["requery"] is not None and r["outcome"] == "answered" for r in positives),
               "faithfulness": rate(answered, "faithful"), "citation_hit_rate": rate([r for r in answered if r["answerable"]], "citation_hit"),
               "retrieval_ms_mean": mean([r["retrieval_ms"] for r in rows]),
               "answer_ms_mean": mean([r["answer_ms"] for r in rows if "answer_ms" in r])}
    return {"retriever": mode, "model": settings.llm_model_id, "summary": summary, "rows": rows}

"""检索评测：不调用模型，用评测集衡量政策检索（search_policy）的召回与排序。

- 指标：Recall@3、Recall@5、MRR（只统计应有答案的样例）、无依据判定准确率（应答“无依据”的样例）、
  有依据样例被误判为无依据的比例、检索耗时；并按诉求、退货原因、商品品类、检索难点分组。
- 输入：用户原话（original）、模型改写问法（rewritten，由识别模型生成后存入评测集），或两者并行检索后合并
  （combined，与线上政策咨询一致）；检索方式可选 hybrid（BM25 + 向量）、bm25、vector，用于对比。
- 期望条款写作 clause_id，或 clause_id@version（需核对版本时）。
- 开启 Phoenix 时同时把评测集与结果记录为 Phoenix 实验。
"""

import csv
import json
import statistics
from collections import defaultdict
from pathlib import Path
from time import perf_counter

from after_sales.retrieval.search import search_policy

CASES_PATH = Path(__file__).with_name("retrieval_cases.json")
GROUPS = ("intent", "reason", "product", "difficulty")


def load_cases(path: Path = CASES_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def matches(expected: str, policy: dict) -> bool:
    return policy["chunk_id"] == expected if "@" in expected else policy["clause_id"] == expected


QUERY_FIELDS = ("original", "rewritten", "combined")


def retrieve(case: dict, query_field: str, index_path: Path, mode: str = "hybrid") -> dict:
    rewritten = case["rewritten"] or case["question"]
    query, message = {"original": (case["question"], None), "rewritten": (rewritten, None),
                      "combined": (rewritten, case["question"])}[query_field]
    started = perf_counter()
    result = search_policy(query, user_message=message, index_path=index_path, mode=mode, **case["context"])
    elapsed = (perf_counter() - started) * 1000
    return {"query": query, "status": result["status"], "missing_fields": result["missing_fields"],
            "ranked": [p["chunk_id"] for p in result["policies"]], "policies": result["policies"], "latency_ms": round(elapsed, 2)}


def score(case: dict, retrieved: dict) -> dict:
    policies, expected = retrieved["policies"], case["expected"]
    answered = retrieved["status"] == "ok" and bool(policies)
    row = {"answered": answered}
    if not case["answerable"]:
        return {**row, "no_answer_correct": not answered}
    def recall(k):
        return sum(any(matches(e, p) for p in policies[:k]) for e in expected) / len(expected)
    rank = next((i + 1 for i, p in enumerate(policies) if any(matches(e, p) for e in expected)), None)
    return {**row, "recall@3": recall(3), "recall@5": recall(5), "mrr": 1 / rank if rank else 0.0,
            "first_hit_rank": rank, "false_no_answer": not answered}


def aggregate(rows: list[dict]) -> dict:
    answerable = [r for r in rows if r["answerable"]]
    unanswerable = [r for r in rows if not r["answerable"]]
    mean = lambda values: round(statistics.mean(values), 4) if values else None  # noqa: E731
    latencies = sorted(r["latency_ms"] for r in rows)
    return {"cases": len(rows), "answerable": len(answerable), "unanswerable": len(unanswerable),
            "recall@3": mean([r["recall@3"] for r in answerable]), "recall@5": mean([r["recall@5"] for r in answerable]),
            "mrr": mean([r["mrr"] for r in answerable]),
            "false_no_answer_rate": mean([float(r["false_no_answer"]) for r in answerable]),
            "no_answer_accuracy": mean([float(r["no_answer_correct"]) for r in unanswerable]),
            "latency_ms_mean": mean([r["latency_ms"] for r in rows]),
            "latency_ms_p95": latencies[max(0, round(len(latencies) * 0.95) - 1)] if latencies else None}


def grouped(rows: list[dict]) -> dict:
    groups = {}
    for name in GROUPS:
        buckets = defaultdict(list)
        for row in rows:
            values = row["tags"][name]
            for value in (values if isinstance(values, list) else [values]):
                buckets[value or "（无）"].append(row)
        groups[name] = {value: aggregate(items) for value, items in sorted(buckets.items(), key=lambda kv: str(kv[0]))}
    return groups


def evaluate(query_field: str, index_path: Path, cases: list[dict], mode: str = "hybrid") -> dict:
    rows = []
    for case in cases:
        retrieved = retrieve(case, query_field, index_path, mode)
        rows.append({"id": case["id"], "query": retrieved["query"], "answerable": case["answerable"],
                     "expected": case["expected"], "tags": case["tags"], "note": case["note"],
                     "status": retrieved["status"], "missing_fields": retrieved["missing_fields"],
                     "ranked": retrieved["ranked"], "latency_ms": retrieved["latency_ms"], **score(case, retrieved)})
    return {"query_field": query_field, "retriever": mode, "summary": aggregate(rows), "groups": grouped(rows), "rows": rows}


def write_results(directory: Path, reports: list[dict], meta: dict) -> None:
    (directory / "results.json").write_text(json.dumps({**meta, "reports": reports}, ensure_ascii=False, indent=2), encoding="utf-8")
    fields = ["retriever", "query_field", "id", "query", "answerable", "status", "expected", "ranked", "recall@3", "recall@5", "mrr",
              "first_hit_rank", "false_no_answer", "no_answer_correct", "latency_ms"]
    with (directory / "results.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for report in reports:
            for row in report["rows"]:
                writer.writerow({**row, "retriever": report["retriever"], "query_field": report["query_field"], "expected": "|".join(row["expected"]),
                                 "ranked": "|".join(row["ranked"])})


def refresh_rewrites(path: Path = CASES_PATH) -> int:
    """用真实识别模型为每个样例生成政策检索问法（IntakeUpdate.policy_question），写回评测集。会产生模型调用。"""
    from after_sales.agent.intake import RequestExtractor
    from after_sales.config import load_settings
    from after_sales.retrieval.policies import load_policy_chunks
    settings = load_settings()
    extractor = RequestExtractor(settings)
    titles = {sku: chunk["product_scope"]["title"] for chunk in load_policy_chunks() for sku in chunk["product_scope"]["skus"]}
    data = load_cases(path)
    for case in data["cases"]:
        ctx = case["context"]
        # 与线上一致：识别模型看到当前订单的商品与下单日期，再把问题归一为政策术语。
        context = {"task": {"case_id": None, "previous_intent": "unknown"}, "state": {"draft": {}}, "case_progress": None,
                   "order": {"id": "ORD-EVAL", "status": "delivered", "ordered_on": ctx["ordered_on"],
                             "items": [{"id": "ITEM-EVAL", "sku": ctx["sku"], "product_name": titles.get(ctx["sku"], ctx["sku"]), "quantity": 1}]},
                   "proposal": None, "policy_refs": [], "history": []}
        update = extractor(case["question"], context)
        case["rewritten"] = update.policy_question or case["question"]
        case["rewritten_intent"] = update.intent
    data["rewritten_by"] = f"{settings.llm_model_id}（IntakeUpdate.policy_question，识别时的检索问法）"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return len(data["cases"])


def log_phoenix_experiment(settings, reports: list[dict], cases: list[dict], index_path: Path) -> str:
    """把评测集上传为 Phoenix 数据集，并以检索函数为任务运行实验（不调用模型）。"""
    from phoenix.client import Client
    from phoenix.client.experiments import run_experiment
    base_url = settings.phoenix_endpoint.removesuffix("/v1/traces")
    client = Client(base_url=base_url)
    dataset = client.datasets.create_dataset(
        name=f"after-sales-retrieval-{len(cases)}-{int(perf_counter() * 1000)}",
        inputs=[{"id": c["id"], "question": c["question"], "rewritten": c["rewritten"], "context": c["context"]} for c in cases],
        outputs=[{"expected": c["expected"], "answerable": c["answerable"]} for c in cases],
        metadata=[{"tags": c["tags"], "note": c["note"]} for c in cases])
    by_id = {c["id"]: c for c in cases}
    for report in reports:
        def task(input, field=report["query_field"], mode=report["retriever"]):
            return retrieve(by_id[input["id"]], field, index_path, mode) | {"policies": None}

        def evaluator(input, output):
            # 有依据的问题记 Recall@3，应答无依据的问题记判定是否正确。
            case = by_id[input["id"]]
            ranked = [{"chunk_id": cid, "clause_id": cid.split("@")[0]} for cid in output["ranked"]]
            result = score(case, {**output, "policies": ranked})
            return result["recall@3"] if case["answerable"] else float(result["no_answer_correct"])
        run_experiment(dataset=dataset, task=task, evaluators={"recall@3_or_no_answer": evaluator},
                       experiment_name=f"{report['retriever']}-{report['query_field']}", client=client, print_summary=False)
    return f"{base_url}/datasets"

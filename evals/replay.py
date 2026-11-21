"""统一评测入口：场景回放（离线预设 / 真实模型）与检索评测（retrieval，不调用模型），分别保存机器结果。"""

import argparse
import csv
import json
import secrets
import sys
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

from fastapi.testclient import TestClient

from after_sales.agent.intake import IntakeUpdate
from after_sales.config import PROJECT_ROOT, Settings, business_today, load_settings
from after_sales.main import create_app
from after_sales.retrieval.index import rebuild_index
from after_sales.storage.database import SCHEMA_PATH, connect_database
from after_sales.storage.seed import seed_demo


def field(data, path):
    for key in path.split("."):
        data = data[key]
    return data


def expect(actual, wanted, label):
    if actual != wanted:
        raise AssertionError(f"{label}: expected {wanted!r}, got {actual!r}")


def offline_explanation(question, policies, assessment=None):
    return {"text": assessment["explanation"] if assessment else policies[0]["body"],
            "citation_ids": [policies[0]["chunk_id"]]}


def replay(scenario, mode, directory, settings, index):
    started = perf_counter()
    result = {"id": scenario["id"], "group": scenario["group"], "mode": mode,
              "fault": scenario.get("fault"), "passed": False, "steps": []}
    path = directory / (scenario["id"] + ".sqlite3")
    path.touch(exist_ok=False)
    with connect_database(path) as connection:
        connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        seed_demo(connection, business_today())
    current = {}
    app = create_app(path, extractor=(lambda *_: IntakeUpdate.model_validate(current)) if mode == "offline" else None,
                     policy_explainer=offline_explanation if mode == "offline" else None, policy_index_path=index)

    def request(client, method, url, body=None):
        begin = perf_counter()
        response = client.request(method, url, **({"json": body} if body is not None else {}))
        data = response.json()
        result["steps"].append({"method": method, "path": url, "input": body,
                                "http_status": response.status_code, "duration_ms": round((perf_counter()-begin)*1000, 2), "result": data})
        if response.status_code >= 400:
            raise AssertionError(f"{method} {url}: HTTP {response.status_code}")
        return data

    try:
        with ExitStack() as stack:
            stack.enter_context(patch("after_sales.main.load_settings", return_value=settings))
            stack.enter_context(patch("after_sales.domain.policy_rules.INDEX_PATH", index))
            customer = stack.enter_context(TestClient(app))
            request(customer, "POST", "/sessions", {"customer_id": "customer-001"})
            if scenario.get("fault") == "order_timeout":
                stack.enter_context(patch.object(app.state.customer_service, "get_order", side_effect=TimeoutError))
            for turn in scenario["turns"]:
                current = turn["fixture"]
                data = request(customer, "POST", "/messages", {"content": turn["message"]})
                for key, value in turn["expect"].items():
                    expect(field(data, key), value, key)
                events = data["workflow"]["events"]
                tools = {e["name"] for e in events if e["kind"] == "tool"}
                for name in turn.get("tools", []):
                    expect(name in tools, True, f"tool:{name}")
                if turn.get("citations"):
                    refs = data.get("citations") or data.get("assessment", {}).get("policy_evidence") or data.get("policy_result", {}).get("policies")
                    expect(bool(refs), True, "policy references")
            if scenario.get("finish"):
                finish_refund(customer, request, data["proposal"], scenario, result)
            else:
                with connect_database(path) as connection:
                    expect(connection.execute("SELECT COUNT(*) FROM refund_operations").fetchone()[0], 0, "no unauthorized refund")
                    expect(connection.execute("SELECT COUNT(*) FROM return_requests").fetchone()[0], 1, "no unconfirmed application (one seed record)")
            if scenario.get("finish"):
                with connect_database(path) as connection:
                    expect(connection.execute("SELECT COUNT(*) FROM refund_operations").fetchone()[0], 1, "one refund")
                    amount = connection.execute("SELECT refunded_amount_cents FROM order_items WHERE id=?", (scenario["item_id"],)).fetchone()[0]
                    expect(amount, scenario["amount_cents"], "persisted refunded amount")
            result["passed"] = True
    except Exception as exc:
        # 业务断言可报告；未知异常不保存可能携带密钥的异常文本。
        result["failure"] = {"type": type(exc).__name__, "detail": str(exc) if isinstance(exc, (AssertionError, KeyError)) else "运行异常，查看脱敏步骤与轨迹。"}
    result["duration_ms"] = round((perf_counter()-started)*1000, 2)
    traces = [step["result"]["workflow"] for step in result["steps"] if isinstance(step["result"], dict) and "workflow" in step["result"]]
    usages = [trace["usage"].get("tokens") for trace in traces if trace["usage"].get("tokens")]
    result["tokens"] = {key: sum(u.get(key, 0) for u in usages) for key in ("input_tokens", "output_tokens", "total_tokens")} if usages else None
    return result


def finish_refund(client, request, proposal, scenario, result):
    post = lambda path, body=None: request(client, "POST", path, body)
    confirmation = post(f"/proposals/{proposal['id']}/confirm", {"version": proposal["version"]})
    record = post("/return-requests", {"confirmation_id": confirmation["confirmation_id"]})["return_request"]
    rid = record["id"]
    if record["status"] == "pending_approval":
        cookie = client.cookies.get("after_sales_session")
        post("/demo/support-sessions", {"support_id": "staff-001"})
        post(f"/support/approvals/{rid}", {"proposal_id": proposal["id"], "version": proposal["version"], "decision": "approved", "reason": "演示审核通过"})
        client.cookies.set("after_sales_session", cookie, domain="testserver.local", path="/")
    post(f"/return-requests/{rid}/shipment", {"operation_key": "ship", "carrier": "演示物流", "tracking_number": "DEMO-REPLAY"})
    cookie = client.cookies.get("after_sales_session")
    post("/demo/warehouse-sessions", {"warehouse_id": "warehouse-001"})
    post(f"/warehouse/return-requests/{rid}/receipt", {"operation_key": "receive"})
    post(f"/warehouse/return-requests/{rid}/inspection", {"operation_key": "inspect", "result": "passed", "reason": "演示检验通过"})
    if scenario["finish"] == "timeout":
        post("/demo/support-sessions", {"support_id": "staff-001"})
        post(f"/demo/return-requests/{rid}/refund-scenario", {"operation_key": "timeout", "scenario": "success_then_timeout"})
    client.cookies.set("after_sales_session", cookie, domain="testserver.local", path="/")
    refund = post(f"/return-requests/{rid}/refund", {"operation_key": "refund"})
    oid = refund["operation"]["id"]
    if scenario["finish"] == "timeout":
        expect(refund["operation"]["status"], "unknown", "timeout status")
        refund = post(f"/refund-operations/{oid}/check")
    expect(refund["operation"]["status"], "succeeded", "refund status")
    expect(refund["operation"]["amount_cents"], scenario["amount_cents"], "refund amount")
    repeat = post(f"/return-requests/{rid}/refund", {"operation_key": "repeated-refund"})
    expect(repeat["operation"]["id"], oid, "no duplicate refund")
    result["final_status"] = request(client, "GET", f"/return-requests/{rid}")["status"]
    expect(result["final_status"], "refunded", "final request state")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["offline", "live", "retrieval"], default="offline")
    parser.add_argument("--select", default="core", help="core / all / 组名 / 场景ID，多个值用逗号分隔；holdout为独立检查组")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--query", choices=["original", "rewritten", "combined", "all"], default="all",
                        help="检索评测的输入：用户原话、模型改写问法、两者并行合并（线上方式）或全部")
    parser.add_argument("--retriever", choices=["hybrid", "bm25", "vector", "all"], default="hybrid",
                        help="检索评测的检索方式：BM25 + 向量混合、仅 BM25、仅向量或全部")
    parser.add_argument("--refresh-rewrites", action="store_true",
                        help="检索评测前用真实识别模型重新生成改写问法并写回评测集（会产生模型调用）")
    args = parser.parse_args()
    if args.mode == "retrieval":
        return run_retrieval(args)
    scenarios = json.loads(Path(__file__).with_name("scenarios.json").read_text(encoding="utf-8"))
    if args.list:
        for s in scenarios: print(s["id"], s["group"])
        return 0
    selectors = args.select.split(",")
    selected = [s for s in scenarios if "all" in selectors or s["id"] in selectors or s["group"] in selectors or ("core" in selectors and s["group"] != "holdout")]
    if not selected: parser.error("没有匹配场景")
    directory = PROJECT_ROOT / "artifacts" / ("replay-" + args.mode + "-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3))
    directory.mkdir(parents=True)
    index = directory / "policies.sqlite3"
    rebuild_index(index_path=index)
    settings = load_settings() if args.mode == "live" else Settings(llm_api_key="offline-no-network")
    report = {"mode": args.mode, "model": settings.llm_model_id if args.mode == "live" else None,
              "llm_timeout_seconds": settings.llm_timeout_seconds,
              "business_date": str(business_today()), "results": []}
    for scenario in selected:
        result = replay(scenario, args.mode, directory, settings, index)
        report["results"].append(result)
        (directory / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({k: result.get(k) for k in ("id", "passed", "duration_ms", "tokens", "failure")}, ensure_ascii=False), flush=True)
    with (directory / "results.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["id", "group", "mode", "passed", "duration_ms", "final_status"], extrasaction="ignore")
        writer.writeheader(); writer.writerows(report["results"])
    print("机器结果：", directory)
    return 0 if all(r["passed"] for r in report["results"]) else 1


def run_retrieval(args) -> int:
    from retrieval_eval import evaluate, load_cases, log_phoenix_experiment, refresh_rewrites, write_results
    if args.refresh_rewrites:
        print("已生成改写问法：", refresh_rewrites(), "条", flush=True)
    data = load_cases()
    directory = PROJECT_ROOT / "artifacts" / ("retrieval-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3))
    directory.mkdir(parents=True)
    index = directory / "policies.sqlite3"
    rebuild_index(index_path=index)
    from after_sales.retrieval.index import EMBEDDING_MODEL, release_vector_stores
    from retrieval_eval import QUERY_FIELDS
    fields = QUERY_FIELDS if args.query == "all" else [args.query]
    modes = ["bm25", "vector", "hybrid"] if args.retriever == "all" else [args.retriever]
    if any(f != "original" for f in fields) and not all(case["rewritten"] for case in data["cases"]):
        raise SystemExit("评测集尚无改写问法，请先加 --refresh-rewrites 生成。")
    reports = [evaluate(field, index, data["cases"], mode) for mode in modes for field in fields]
    meta = {"mode": "retrieval", "retriever": "jieba BM25 + 本地向量（RRF）父子检索 + 适用性过滤",
            "embedding_model": EMBEDDING_MODEL, "cases": len(data["cases"]),
            "rewritten_by": data.get("rewritten_by"), "business_date": str(business_today())}
    write_results(directory, reports, meta)
    for report in reports:
        print(json.dumps({"retriever": report["retriever"], "query": report["query_field"], **report["summary"]}, ensure_ascii=False), flush=True)
    try:
        settings = load_settings()
    except Exception:
        settings = None
    if settings and settings.phoenix_enabled:
        print("Phoenix 实验：", log_phoenix_experiment(settings, reports, data["cases"], index))
    release_vector_stores()
    print("机器结果：", directory)
    return 0


if __name__ == "__main__":
    sys.exit(main())

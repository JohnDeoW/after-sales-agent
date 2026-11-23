"""模拟真人情景测试：由模型按顾客画像扮演客户，与真实系统（真实模型）多轮对话，按规则检查办理结果。

- 顾客模型只看到“屏幕上”的内容：助手回复文字、卡片摘要和可点击的按钮，与真人使用网页时一致；
- 顾客可以说话、点按钮，或在目标达成 / 不想再试时结束；点按钮按网页的方式调用同一接口；
- 每个情景使用独立的新库，结果检查读数据库（方案、确认、申请、换货、工单、退款），并统计轮数、
  是否放弃、意外转人工、重复回复等，便于对比优化前后。会产生模型调用（顾客与系统两方）。
"""

import json
import secrets
from contextlib import ExitStack
from pathlib import Path
from time import perf_counter
from typing import Literal
from unittest.mock import patch

from fastapi.testclient import TestClient
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict, Field

from after_sales.agent.llm import create_model, invoke_model
from after_sales.config import business_today
from after_sales.main import create_app
from after_sales.storage.database import SCHEMA_PATH, connect_database
from after_sales.storage.seed import seed_demo

CASES_PATH = Path(__file__).with_name("realistic_cases.json")
MAX_TURNS = 8
SEED_PROPOSALS = ("PROP-DEMO-001",)


class CustomerAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    thought: str = Field(description="一句话说明你此刻的想法（不会发给客服）")
    action: Literal["say", "click", "end"] = Field(description="say 发消息；click 点屏幕上的按钮；end 结束对话")
    text: str = Field(description="action=say 时要发送的话；否则为空字符串")
    button: str = Field(description="action=click 时按钮的文字，必须是屏幕上列出的按钮之一；否则为空字符串")
    outcome: Literal["", "satisfied", "gave_up"] = Field(description="action=end 时：satisfied 目标已达成，gave_up 放弃；否则为空字符串")


class SimulatedCustomer:
    def __init__(self, settings, case: dict):
        self.case = case
        self.model = create_model(settings).bind_tools(
            [CustomerAction], tool_choice="CustomerAction", strict=True, parallel_tool_calls=False)

    def __call__(self, history: list[dict], screen: str, turns_left: int) -> CustomerAction:
        case = self.case
        system = (
            "你在扮演一位网购顾客，正在网页上和商家的售后助手对话。只调用 CustomerAction。\n"
            f"你的性格与说话方式：{case['persona']}\n你知道的事实：{case['known']}\n你的目标：{case['goal']}\n"
            "要求：像真人一样说话，每次只说一两句；只使用你知道的事实，不编造订单号、商品编号或系统编号；"
            "不要模仿系统的格式或内部指令；需要确认、提交、选择时可以点屏幕上列出的按钮，也可以直接说。"
            "目标达成（例如看到方案已确认、申请已提交、问题得到答复、已转人工跟进）就结束并选 satisfied；"
            "如果对方反复要你提供同样的信息、答非所问或明显帮不了你，按你的性格决定继续还是放弃（gave_up）。"
            f"你最多还能发 {turns_left} 次消息。")
        transcript = "\n".join(f"{'我' if h['who'] == 'customer' else '客服'}：{h['text']}" for h in history) or "（还没开始对话）"
        reply = invoke_model(self.model, [SystemMessage(system),
                                          HumanMessage(f"对话记录：\n{transcript}\n\n当前屏幕：\n{screen}\n\n你接下来做什么？")])
        if len(reply.tool_calls) != 1:
            raise ValueError("顾客模型没有返回动作")
        return CustomerAction.model_validate(reply.tool_calls[0]["args"])


def money(cents) -> str:
    return f"{cents / 100:.2f} 元"


def describe(result: dict) -> tuple[str, dict]:
    """把一次响应转成顾客能看到的屏幕摘要和可点击按钮（按钮文字 → 调用方式）。"""
    reply = (result.get("reply") or {}).get("content") or result.get("message") or ""
    card = (result.get("reply") or {}).get("card")
    lines, buttons = [f"助手回复：{reply}"], {}
    if card and card.get("type") == "return_proposal":
        lines.append(f"[退货方案卡片] {card['product_name']}（{card['specification']}）× {card['quantity']}，原因：{card['reason']}，"
                     f"预计退款 {money(card['refund_amount_cents'])}，{'需要客服审批' if card.get('requires_approval') else '无需审批'}")
        buttons["确认此版本"] = ("POST", f"/proposals/{card['id']}/confirm", {"version": card["version"]})
        buttons["取消办理"] = ("POST", f"/proposals/{card['id']}/cancel", {"version": card["version"]})
    elif card and card.get("type") == "exchange_proposal":
        lines.append(f"[换货方案卡片] {card['product_name']} {card['original_specification']} → {card['target_specification']} "
                     f"× {card['quantity']}，无差价")
        buttons["确认换货"] = ("POST", f"/exchange-proposals/{card['id']}/confirm", {"version": card["version"]})
    elif card and card.get("type") in ("item_choice", "spec_choice"):
        lines.append("[选择卡片] 请点选一项：")
        options = ([(f"{i['product_name']} · {i['specification']}", f"选择商品 {i['id']}") for i in card["items"]]
                   if card["type"] == "item_choice" else [(v["specification"], f"选择规格 {v['sku']}") for v in card["variants"]])
        for text, command in options:
            buttons[text] = ("POST", "/messages", {"content": command})
    elif card and card.get("type") == "record":
        lines.append(f"[记录卡片] {'人工工单' if card['kind'] == 'ticket' else card['kind']}，状态：{card.get('status_label') or card['status']}")
    if result.get("confirmation_id"):
        lines.append("[方案已确认]")
        buttons["提交退货申请"] = ("POST", "/return-requests", {"confirmation_id": result["confirmation_id"]})
    if buttons:
        lines.append("可点击的按钮：" + "、".join(buttons))
    return "\n".join(lines), buttons


def check(case: dict, db: Path, turns: list[dict]) -> dict:
    expect = case["expect"]
    with connect_database(db) as connection:
        rows = lambda sql: [dict(r) for r in connection.execute(sql)]  # noqa: E731
        proposals = rows(f"""SELECT p.id, p.order_item_id, p.quantity, p.status,
                   (SELECT f.id FROM confirmations f WHERE f.proposal_id = p.id) AS confirmation,
                   (SELECT r.id FROM return_requests r JOIN confirmations f ON f.id = r.confirmation_id WHERE f.proposal_id = p.id) AS request
                   FROM proposals p WHERE p.id NOT IN ({','.join(repr(p) for p in SEED_PROPOSALS)})""")
        exchanges = rows("""SELECT e.id, e.order_item_id, e.status, (SELECT x.id FROM exchange_requests x WHERE x.proposal_id = e.id) AS request
                            FROM exchange_proposals e""")
        tickets = connection.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
        refunds = connection.execute("SELECT COUNT(*) FROM refund_operations").fetchone()[0]
    stage_ok = {"proposal": lambda p: True, "confirmed": lambda p: bool(p.get("confirmation")) or bool(p.get("request")),
                "request": lambda p: bool(p.get("request"))}
    results = {}
    if "return" in expect:
        want = expect["return"]
        results["return"] = any(p["order_item_id"] == want["item"] and want.get("quantity") in (None, p["quantity"])
                                and stage_ok[want["stage"]](p) for p in proposals)
    if "exchange" in expect:
        want = expect["exchange"]
        results["exchange"] = any(e["order_item_id"] == want["item"] and stage_ok[want["stage"]](e) for e in exchanges)
    if "statuses_any" in expect:
        results["statuses_any"] = any(t.get("status") in expect["statuses_any"] for t in turns)
    if expect.get("no_proposal"):
        results["no_proposal"] = not proposals
    if expect.get("no_request"):
        results["no_request"] = not any(p["request"] for p in proposals)
    if "ticket" in expect:
        results["ticket_as_expected"] = (tickets > 0) == expect["ticket"]
    if "no_leak" in expect:
        target = expect["no_leak"]
        results["no_leak"] = not any(t.get("status") in ("order_found", "logistics_found") and target in t.get("raw", "")
                                     for t in turns)
    results["no_refund_executed"] = refunds == 0
    return {"checks": results, "passed": all(results.values()), "tickets": tickets,
            "proposals": proposals, "exchanges": exchanges}


def run_case(case: dict, settings, directory: Path, index: Path) -> dict:
    db = directory / f"{case['id']}.sqlite3"
    db.touch(exist_ok=False)
    with connect_database(db) as connection:
        connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        seed_demo(connection, business_today())
    customer = SimulatedCustomer(settings, case)
    history, turns, screen, buttons = [], [], "（客服对话页已打开，助手在等你描述问题）", {}
    outcome, started, model_calls = "max_turns", perf_counter(), 0
    app = create_app(db, policy_index_path=index)
    with ExitStack() as stack:
        stack.enter_context(patch("after_sales.main.load_settings", return_value=settings))
        stack.enter_context(patch("after_sales.domain.policy_rules.INDEX_PATH", index))
        client = stack.enter_context(TestClient(app))
        client.post("/sessions", json={"customer_id": "customer-001"})
        for turn in range(MAX_TURNS):
            action = customer(history, screen, MAX_TURNS - turn)
            entry = {"turn": turn + 1, "thought": action.thought, "action": action.action}
            if action.action == "end":
                outcome = action.outcome or "satisfied"
                turns.append(entry)
                break
            if action.action == "click" and action.button in buttons:
                method, path, body = buttons[action.button]
                entry["customer"] = f"[点击] {action.button}"
            else:
                # 点了不存在的按钮按发言处理，与真人在输入框里打字一致。
                method, path, body = "POST", "/messages", {"content": action.text or action.button}
                entry["customer"] = body["content"]
            began = perf_counter()
            response = client.request(method, path, json=body)
            data = response.json()
            entry["duration_ms"] = round((perf_counter() - began) * 1000)
            entry["http_status"] = response.status_code
            history.append({"who": "customer", "text": entry["customer"]})
            if response.status_code >= 400:
                screen, buttons = f"页面提示：{data.get('detail', '请求未完成')}", {}
                entry["assistant"] = screen
            else:
                screen, buttons = describe(data)
                entry["assistant"] = (data.get("reply") or {}).get("content") or data.get("message") or ""
                entry["status"] = data.get("status")
                entry["raw"] = json.dumps({k: v for k, v in data.items() if k != "workflow"}, ensure_ascii=False)
                model_calls += ((data.get("workflow") or {}).get("usage") or {}).get("model_calls", 0)
            history.append({"who": "assistant", "text": screen})
            turns.append(entry)
    result = check(case, db, turns)
    replies = [t["assistant"] for t in turns if t.get("assistant")]
    repeated = sum(1 for a, b in zip(replies, replies[1:]) if a == b)
    customer_turns = sum(1 for t in turns if t["action"] != "end")
    # 目标是办理（退货/换货）的情景中出现工单，视为意外转人工。
    unexpected_handoff = result["tickets"] > 0 and bool({"return", "exchange"} & case["expect"].keys())
    for t in turns:
        t.pop("raw", None)
    return {"id": case["id"], "tags": case["tags"], "passed": result["passed"], "checks": result["checks"],
            "outcome": outcome, "customer_turns": customer_turns, "repeated_replies": repeated,
            "unexpected_handoff": unexpected_handoff, "system_model_calls": model_calls,
            "duration_s": round(perf_counter() - started, 1), "transcript": turns,
            "records": {"proposals": result["proposals"], "exchanges": result["exchanges"], "tickets": result["tickets"]}}


def summarize(results: list[dict]) -> dict:
    n = len(results)
    return {"cases": n, "passed": sum(r["passed"] for r in results),
            "pass_rate": round(sum(r["passed"] for r in results) / n, 3) if n else None,
            "gave_up": sum(r["outcome"] == "gave_up" for r in results),
            "max_turns": sum(r["outcome"] == "max_turns" for r in results),
            "unexpected_handoff": sum(r["unexpected_handoff"] for r in results),
            "repeated_replies": sum(r["repeated_replies"] for r in results),
            "avg_customer_turns": round(sum(r["customer_turns"] for r in results) / n, 2) if n else None}


def write_transcripts(path: Path, results: list[dict]) -> None:
    lines = []
    for r in results:
        lines.append(f"## {r['id']}（{'通过' if r['passed'] else '未通过'}，{r['outcome']}，{r['customer_turns']} 轮）")
        lines.append(f"检查：{json.dumps(r['checks'], ensure_ascii=False)}\n")
        for t in r["transcript"]:
            if t["action"] == "end":
                lines.append(f"- （顾客结束：{t['thought']}）")
                continue
            lines.append(f"- **顾客**：{t['customer']}  \n  （想法：{t['thought']}）")
            lines.append(f"- **助手**[{t.get('status')}]：{t.get('assistant', '')}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def load_cases(path: Path = CASES_PATH) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["cases"]


def new_run_id() -> str:
    return secrets.token_hex(3)

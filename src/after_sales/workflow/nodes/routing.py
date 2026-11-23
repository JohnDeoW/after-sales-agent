"""读取上下文、明确指令路由与模型识别。"""

import re

from langgraph.runtime import Runtime
from pydantic import ValidationError

from after_sales.agent.context import build_context
from after_sales.agent.intake import IntakeUpdate, IntakeUnavailable
from after_sales.domain.intake_service import match_order_item
from after_sales.domain.proposals import ProposalNotFound
from after_sales.storage.intake import load_context
from after_sales.workflow.nodes.common import get_order, model_step
from after_sales.workflow.nodes.reads import QUERY_INTENTS
from after_sales.workflow.state import State, TurnContext

END_PUNCT = "。！!"
SERVICE_INTENTS = ("repair", "quality_dispute", "human")


def load(s: State, runtime: Runtime[TurnContext]) -> dict:
    ctx = runtime.context
    snapshot = {key: s.get(key) for key in ("conversation", "case_id", "case_revision")}
    context = load_context(ctx.deps.customers.db_path, ctx.session_id, snapshot)
    return {"context": context, "conversation": context["state"], "case_id": context["case_id"],
            "case_revision": context["case_revision"],
            "proposal": ctx.deps.proposals.current(ctx.session_id)["proposal"]}


def pending_exchange(deps, session_id: str, state: dict) -> dict | None:
    proposal_id = state.get("exchange_proposal_id")
    if not proposal_id:
        return None
    try:
        proposal = deps.exchanges.proposal(session_id, proposal_id)
    except ProposalNotFound:
        return None
    return proposal if proposal["status"] == "pending_confirmation" else None


# ---- 明确指令映射表：页面按钮与卡片生成的固定话术，不经过模型识别 ----

def _to(next_node, **command):
    return lambda m, s, rt: {"next": next_node, "command": {k: (v(m) if callable(v) else v) for k, v in command.items()}}


def _read(intent, field):
    return lambda m, s, rt: {"next": "select_read_tool", "update": IntakeUpdate(intent=intent, **{field: m[1]})}


def _choice(m, s, rt):
    # 页面卡片的点击选择：只沿用当前退货/换货诉求，商品项和规格仍由后续节点按订单与库存核对。
    state = s["context"]["state"]
    intent = state.get("last_intent")
    if intent not in ("return", "exchange"):
        return None
    if m[1] == "商品":
        update = IntakeUpdate(intent=intent, item=m[2])
    elif intent == "exchange":
        update = IntakeUpdate(intent="exchange", target_specification=m[2])
    else:
        return {"next": "prepare_context"}
    state["last_intent"] = intent
    return {"next": "exchange" if intent == "exchange" else "select_read_tool", "update": update}


def _confirm_pending_exchange(m, s, rt):
    pending = pending_exchange(rt.context.deps, rt.context.session_id, s["context"]["state"])
    if pending is None:
        return None
    return {"next": "confirm_exchange", "command": {"proposal_id": pending["id"], "version": pending["version"]}}


COMMANDS = [
    (r"(?:转人工|找人工|人工客服|我要人工客服)[。！!]?", lambda m, s, rt: {"next": "service_handoff", "update": IntakeUpdate(intent="human")}),
    (r"确认换货\s+(EXPROP-[A-Za-z0-9-]+)\s+[vV](\d+)[。！!]?", _to("confirm_exchange", proposal_id=lambda m: m[1], version=lambda m: int(m[2]))),
    (r"选择(商品|规格)\s+([A-Za-z0-9-]+)", _choice),
    (r"确认换货[。！!]?", _confirm_pending_exchange),
    (r"查询换货\s+(EXC-[A-Za-z0-9-]+)[。！!]?", _read("exchange_query", "request_id")),
    (r"提交申请\s+(CONF-[A-Za-z0-9-]+)[。！!]?", _to("create_request", confirmation_id=lambda m: m[1])),
    (r"确认方案\s+([A-Za-z0-9-]+)\s+[vV](\d+)[。！!]?", _to("confirm", proposal_id=lambda m: m[1], version=lambda m: int(m[2]))),
    (r"取消方案\s+([A-Za-z0-9-]+)\s+[vV](\d+)[。！!]?", _to("cancel", proposal_id=lambda m: m[1], version=lambda m: int(m[2]))),
    (r"(?:取消退货|取消办理|不退了)[。！!]?", _to("cancel")),
    (r"查询申请\s+(RET-[A-Za-z0-9-]+)[。！!]?", _read("return_query", "request_id")),
    (r"查询案件\s+(CASE-[A-Za-z0-9-]+)[。！!]?", _read("case_query", "case_id")),
    (r"查询工单\s+(TKT-[A-Za-z0-9-]+)[。！!]?", _read("ticket_query", "ticket_id")),
    (r"查询物流\s+(ORD-[A-Za-z0-9-]+)[。！!]?", _read("logistics", "order_id")),
]
COMMAND_NODES = ["prepare_context", "select_read_tool", "confirm", "cancel", "create_request",
                 "confirm_exchange", "service_handoff", "exchange"]


# 嵌在一句话里的确认/提交指令（如照抄“确认方案 PROP-… v1”再补一句话），前面不能带否定。
EMBEDDED = [
    (r"确认方案\s+(PROP-[A-Za-z0-9-]+)\s+[vV](\d+)", lambda m: ("confirm", {"proposal_id": m[1], "version": int(m[2])})),
    (r"确认换货\s+(EXPROP-[A-Za-z0-9-]+)\s+[vV](\d+)", lambda m: ("confirm_exchange", {"proposal_id": m[1], "version": int(m[2])})),
    (r"提交申请\s+(CONF-[A-Za-z0-9-]+)", lambda m: ("create_request", {"confirmation_id": m[1]})),
]
NEGATED = r"(?:不|别|先不|暂不|没有?|不要)\s*(?:确认|提交)"


def route_command(s: State, runtime: Runtime[TurnContext]) -> dict:
    # HTTP 操作路由传入结构化指令，非法编号也交给业务校验，不能退回模型猜测。
    if s.get("command"):
        command = dict(s["command"])
        return {"next": command.pop("action"), "command": command}
    text = runtime.context.content.strip()
    for pattern, handler in COMMANDS:
        match = re.fullmatch(pattern, text)
        if match:
            routed = handler(match, s, runtime)
            if routed is not None:
                return routed
    if not re.search(NEGATED, text):
        for pattern, build in EMBEDDED:
            match = re.search(pattern, text)
            if match:
                next_node, command = build(match)
                return {"next": next_node, "command": command}
    return {"next": "prepare_context"}


def prepare_context(s: State, runtime: Runtime[TurnContext]) -> dict:
    state = s["context"]["state"]
    order_id = {
        "policy": lambda: state.get("policy", {}).get("order_id"),
        "order_query": lambda: state.get("query_order_id"),
        "logistics": lambda: state.get("logistics_order_id"),
        "exchange": lambda: state.get("exchange", {}).get("order_id"),
        **{intent: (lambda: state.get("service_request", {}).get("order_id")) for intent in SERVICE_INTENTS},
    }.get(state.get("last_intent"), lambda: state["draft"].get("order_id"))()
    return {"current_order": get_order(runtime, order_id).get("order") if order_id else None}


CANCEL_RETURN = r"(?:^|[，,。；;！!])\s*(?:我)?(?:不退了|不退货了|取消(?:本次|这次)?(?:退货(?:申请)?|办理))\s*[。！!]*$"
# 口语确认：模型判断为“现在同意”之外，还要有明确的同意用语，且没有否定、疑问、条件或状态声称。
# 单独的“好的”“嗯”“知道了”只是应答，不算确认。
VAGUE_ONLY = r"[\s，,。.!！~～]*(?:好|好的|好吧|嗯+|哦+|噢|知道了|收到|明白|了解|ok|OK)[\s，,。.!！~～]*"
CONSENT = r"确认|同意|就这样|就这么办|就按|按这个|行吧|^行|可以|没问题|办吧|退吧|换吧|提交吧|帮我提交"
DENY = r"不|没|别|等等|等一下|再想|考虑|已经确认|确认过|审批|管理员|忽略|吗|呢|[?？]|如果|怎么|是否|假如"


def explicit_consent(text: str) -> bool:
    text = text.strip()
    return (not re.fullmatch(VAGUE_ONLY, text)) and bool(re.search(CONSENT, text)) and not re.search(DENY, text)
RECOGNIZED_NODES = ["select_read_tool", "query_agent", "clarify", "policy_consultation", "exchange", "service_handoff",
                    "confirm", "cancel", "confirm_exchange"]


def recognize(s: State, runtime: Runtime[TurnContext]) -> dict:
    ctx = runtime.context
    context = s["context"]
    content = ctx.content.strip()

    def extract():
        try:
            value = ctx.deps.extractor(ctx.content, build_context(context, s["current_order"], s["proposal"]))
            return IntakeUpdate.model_validate(value)
        except ValidationError:
            raise IntakeUnavailable("模型未返回有效的识别结果，本轮消息未保存，已有信息保留，请重试。") from None

    update = model_step("IntakeUpdate", extract)
    state = context["state"]
    if update.intent == "confirm_return" and state.get("last_intent") == "exchange":
        exchange = state.get("exchange", {})
        changed = bool(update.clear_fields) or any(
            getattr(update, key) is not None and getattr(update, key) != exchange.get(key)
            for key in ("order_id", "item", "quantity", "target_specification", "reason"))
        pending = pending_exchange(ctx.deps, ctx.session_id, state)
        if pending and explicit_consent(content) and not changed:
            return {"update": update, "next": "confirm_exchange",
                    "command": {"proposal_id": pending["id"], "version": pending["version"]}}
        # 修改了换货内容或只是“好的”等普通应答：继续换货流程，重新展示方案，不记录确认。
        update = update.model_copy(update={"intent": "exchange"})
    if update.intent == "confirm_return":
        draft = state["draft"]
        changed = bool(update.clear_fields) or any(
            getattr(update, key) is not None and getattr(update, key) != draft.get(key)
            for key in ("order_id", "quantity", "reason", "reason_kind", "opened"))
        if update.item is not None:
            item = match_order_item((s["current_order"] or {}).get("items", []), update.item)
            changed = changed or item is None or item["id"] != draft.get("order_item_id")
        if changed:
            # 同一句话修改了办理内容，先展示新方案，不能沿用这次确认。
            update = update.model_copy(update={"intent": "return"})
        elif s["proposal"] and s["proposal"]["status"] in ("pending_confirmation", "confirmed") and explicit_consent(content):
            # 模型意图还需配合当前的明确同意表达；“已经确认”等状态声称不算授权。
            proposal = s["proposal"]
            return {"update": update, "next": "confirm",
                    "command": {"proposal_id": proposal["id"], "version": proposal["version"]}}
    if update.intent == "cancel_return" and context["case_id"]:
        same_case = (update.case_id in (None, context["case_id"])
                     and update.order_id in (None, state["draft"].get("order_id"))
                     and update.request_id is None)
        if re.search(CANCEL_RETURN, content) and same_case:
            # 自然语言识别会等待模型，取消时不能误操作期间切换/更新的案件。
            return {"update": update, "next": "cancel",
                    "command": {"expected_case_id": context["loaded_case_id"],
                                "expected_revision": context["case_revision"]}}
    progress = context.get("progress") or {}
    confirmation = progress.get("confirmation")
    if update.intent == "submit_return" and confirmation and not progress.get("return_request") and explicit_consent(content):
        # 口语要求提交已确认的方案（如“可以，帮我提交”），等同点击“提交退货申请”。
        return {"update": update, "next": "create_request", "command": {"confirmation_id": confirmation["id"]}}
    draft = state["draft"]
    other_item = update.item is not None and draft.get("order_item_id") and (
        match_order_item((s["current_order"] or {}).get("items", []), update.item) or {}).get("id") != draft["order_item_id"]
    new_matter = (update.order_id is not None and update.order_id != draft.get("order_id")) or other_item
    # 退货已取消、已退款，或已受理后要办另一件商品：结束原事项，另开新案件，不再当作修改原申请。
    if update.intent == "return" and (state.get("cancelled") or state.get("completed")
                                      or (progress.get("return_request") and new_matter)):
        context["case_id"] = None
        pending = state.get("pending_requests")
        state.clear()
        state.update(draft={}, query_order_id=None)
        if pending:
            state["pending_requests"] = pending
    if update.other_requests:
        # 一句话里的其他诉求先记下，本轮回复中说明，办完当前这件再处理。
        pending = state.setdefault("pending_requests", [])
        pending.extend(text for text in update.other_requests if text not in pending)
        state["mention_pending"] = list(update.other_requests)
    state["last_intent"] = update.intent
    if update.intent in SERVICE_INTENTS:
        next_node = "service_handoff"
    elif update.intent in ("exchange", "policy"):
        next_node = {"exchange": "exchange", "policy": "policy_consultation"}[update.intent]
    elif update.intent in QUERY_INTENTS:
        # 自然语言查询由 Agent 子图在只读工具白名单内选择工具。
        next_node = "query_agent"
    else:
        next_node = "select_read_tool" if update.intent == "return" else "clarify"
    return {"update": update, "context": context, "next": next_node}

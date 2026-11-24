"""换货、维修/人工转交与政策咨询分支。"""

from langgraph.runtime import Runtime

from after_sales.domain.exchanges import collect_exchange
from after_sales.domain.handoffs import collect_handoff
from after_sales.domain.intake_service import _result, locate_items
from after_sales.domain.policy_consultation import consult_policy
from after_sales.tools.inventory import get_inventory
from after_sales.workflow.nodes.common import get_order, model_step, read
from after_sales.workflow.nodes.returns import locate_reply
from after_sales.workflow.state import State, TurnContext


def exchange(s: State, runtime: Runtime[TurnContext]) -> dict:
    def inventory(order_id, item_id):
        return read("get_inventory", get_inventory, {"order_id": order_id, "item_id": item_id})
    ctx = runtime.context
    state, update = s["context"]["state"], s["update"]
    result = collect_exchange(state, update, lambda oid: get_order(runtime, oid), inventory)
    if result["missing_fields"] == ["order_id"] and not state.get("exchange", {}).get("order_id"):
        # 没有订单号：按描述找商品，唯一命中直接继续，否则给出带订单的候选卡片。
        description = state["exchange"].get("item")
        hits, matched = locate_items(ctx.deps.customers.list_orders(ctx.session_id), description,
                                     ctx.deps.customers.active_item_ids(ctx.session_id))
        if matched and len(hits) == 1:
            located = update.model_copy(update={"order_id": hits[0]["order_id"], "item": hits[0]["id"]})
            result = collect_exchange(state, located, lambda oid: get_order(runtime, oid), inventory)
        elif hits:
            result = _result("collecting_information", locate_reply("换货", description, hits, matched), ["item"], items=hits)
    return {"result": result}


def service_handoff(s: State, runtime: Runtime[TurnContext]) -> dict:
    state = s["context"]["state"]
    state["last_intent"] = s["update"].intent
    return {"result": collect_handoff(state, s["update"], runtime.context.content, lambda oid: get_order(runtime, oid))}


def policy_consultation(s: State, runtime: Runtime[TurnContext]) -> dict:
    ctx = runtime.context

    def search(args):
        return read("search_policy", ctx.deps.policy_tool, args)

    def explain(question, policies, facts=None):
        # 已核实的本人订单事实（如是否购买退货运费险）一并交给解释模型；没有订单时不传。
        if facts:
            return model_step("PolicyAnswer", lambda: ctx.deps.policy_explainer(question, policies, facts=facts))
        return model_step("PolicyAnswer", lambda: ctx.deps.policy_explainer(question, policies))
    rewrite = None
    if ctx.deps.policy_rewriter is not None:
        def rewrite(question, message):
            return model_step("PolicyRequery", lambda: ctx.deps.policy_rewriter(question, message))
    state, update, proposal = s["context"]["state"], s["update"], s["proposal"]
    draft = state["draft"]
    policy = state.setdefault("policy", {})
    # 办理中途的追问（运费、到账时间等）沿用当前办理的订单与商品，不再要用户补订单号。
    if draft.get("order_id") and not (update.order_id or update.sku or policy.get("order_id") or policy.get("sku")):
        policy.update(order_id=draft["order_id"], item=draft.get("order_item_id") or draft.get("item"))
        if draft.get("reason_kind") and not update.reason_kind:
            policy["reason_kind"] = draft["reason_kind"]
    result = consult_policy(state, update, ctx.content, lambda order_id: get_order(runtime, order_id), search, explain, rewrite)
    if proposal and proposal["status"] == "pending_confirmation":
        # 答完追问回到待确认的方案：再附上方案卡片，用户可直接确认或修改。
        result["reply_text"] += f"\n你的退货方案 v{proposal['version']} 仍待确认，可以确认、修改或取消。"
        result["proposal"] = proposal
    return {"result": result}

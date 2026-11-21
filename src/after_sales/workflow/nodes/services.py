"""换货、维修/人工转交与政策咨询分支。"""

from langgraph.runtime import Runtime

from after_sales.domain.exchanges import collect_exchange
from after_sales.domain.handoffs import collect_handoff
from after_sales.domain.policy_consultation import consult_policy
from after_sales.tools.inventory import get_inventory
from after_sales.workflow.nodes.common import get_order, model_step, read
from after_sales.workflow.state import State, TurnContext


def exchange(s: State, runtime: Runtime[TurnContext]) -> dict:
    def inventory(order_id, item_id):
        return read("get_inventory", get_inventory, {"order_id": order_id, "item_id": item_id})
    return {"result": collect_exchange(s["context"]["state"], s["update"], lambda oid: get_order(runtime, oid), inventory)}


def service_handoff(s: State, runtime: Runtime[TurnContext]) -> dict:
    state = s["context"]["state"]
    state["last_intent"] = s["update"].intent
    return {"result": collect_handoff(state, s["update"], runtime.context.content, lambda oid: get_order(runtime, oid))}


def policy_consultation(s: State, runtime: Runtime[TurnContext]) -> dict:
    ctx = runtime.context

    def search(args):
        return read("search_policy", ctx.deps.policy_tool, args)

    def explain(question, policies):
        return model_step("PolicyAnswer", lambda: ctx.deps.policy_explainer(question, policies))
    rewrite = None
    if ctx.deps.policy_rewriter is not None:
        def rewrite(question, message):
            return model_step("PolicyRequery", lambda: ctx.deps.policy_rewriter(question, message))
    return {"result": consult_policy(s["context"]["state"], s["update"], ctx.content,
                                      lambda order_id: get_order(runtime, order_id), search, explain, rewrite)}

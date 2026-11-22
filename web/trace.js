// 运行轨迹：展示节点、模型与工具调用、耗时、token、检索命中与得分和政策引用；不包含提示词、密钥或思维链。
import { el } from "/static/dom.js";
import { roleHeaders } from "/static/api.js";
import { statusLabel } from "/static/labels.js";

const labels = {
  load_context: "读取当前案件", route_command: "检查明确操作", prepare_context: "准备必要上下文",
  recognize: "识别诉求", IntakeUpdate: "理解本轮信息", PolicyAnswer: "依据条款组织解释", PolicyRequery: "改写问法重查",
  select_read_tool: "选择查询", read_tool: "读取业务事实", collect_information: "检查缺失信息",
  evaluate_rules: "校验资格和金额", proposal: "保存方案", clarify: "补问必要信息",
  persist_turn: "保存本轮结果", confirm: "核对方案确认", cancel: "取消办理",
  create_request: "创建申请", policy_consultation: "检索适用政策", exchange: "检查换货条件",
  confirm_exchange: "确认换货", service_handoff: "交接人工", reply: "返回已保存结果",
  query_agent: "Agent 选择查询工具", QueryAgent: "模型选择只读工具", wait: "检查是否需要等待", await_confirmation: "等待方案确认", await_approval: "等待客服审批", finish: "本轮结束",
  // 业务工具
  get_order: "查询订单", list_orders: "列出本人订单", search_policy: "检索政策条款", get_logistics: "查询物流",
  get_inventory: "查询库存", get_case: "查询案件", list_cases: "列出案件", get_ticket: "查询工单", list_tickets: "列出工单",
  get_return_request: "查询退货申请", list_return_requests: "列出退货申请", get_exchange_request: "查询换货申请",
  list_exchange_requests: "列出换货申请", create_return_request: "创建退货申请", create_exchange_request: "创建换货申请",
  confirm_proposal: "记录方案确认", cancel_proposal: "撤回方案",
};
const kinds = { node: ["流程节点", "◇"], model: ["模型调用", "✦"], tool: ["业务工具", "⚙"] };
const eventStatus = { running: "进行中", ok: "完成", error: "失败", interrupted: "已暂停等待", stopped: "已停止" };
const fields = { order_id: "订单", item: "商品项", quantity: "数量", reason: "原因", opened: "拆封情况", target_specification: "目标规格" };
const argNames = { order_id: "订单", item_id: "商品项", request_id: "申请", case_id: "案件", ticket_id: "工单", proposal_id: "方案",
  confirmation_id: "确认记录", version: "版本", sku: "SKU", ordered_on: "下单日期", reason_kind: "原因类型" };
const intents = { return: "退货", order_query: "订单查询", return_query: "申请查询", exchange_query: "换货查询", case_query: "案件进度",
  ticket_query: "工单查询", submit_return: "提交申请", policy: "政策咨询", exchange: "换货", logistics: "物流", repair: "维修",
  quality_dispute: "质量争议", human: "转人工", confirm_return: "确认方案", cancel_return: "取消退货", unknown: "其他" };
const results = { clarification_needed: "需要澄清", order_found: "已找到订单", order_not_found: "未找到订单", logistics_found: "已找到物流",
  ticket_found: "已找到工单", case_found: "已找到案件", return_request_found: "已找到申请", exchange_request_found: "已找到换货申请",
  workflow_handoff: "已转人工", policy_answered: "已答复政策", policy_handoff: "政策待人工", ready_for_evaluation: "信息已齐",
  needs_policy: "缺少政策依据", stopped: "已停止", error: "失败", ok: "完成", resumed: "已恢复等待点",
  ticket_created: "已建人工工单", investigation_created: "已建调查工单", handoff_requested: "已请求人工",
  application_accepted: "申请已受理", exchange_proposed: "已生成换货方案", exchange_out_of_stock: "目标规格缺货",
  exchange_unavailable: "暂不能换货", interrupted: "已暂停等待", conflict: "政策版本冲突" };
const runWaiting = { awaiting_confirmation: "方案已生成，等待你核对并确认。", awaiting_approval: "等待客服审批当前方案。",
  workflow_handoff: "自动办理已停止，已有信息已交给人工。", policy_handoff: "政策依据不足或有冲突，等待人工核对。",
  error: "本轮处理失败，请核对记录后重试。", stopped: "本轮已按预算停止，未完成办理。" };

const failureNames = { timeout: "响应超时", connection: "连接失败", rate_limit: "请求限流", upstream_error: "上游服务错误",
  output_format: "输出格式无效" };

const label = name => labels[name] || name;
const sources = { clause: "条款", handbook: "政策手册", faq: "常见问答" };
const score = value => value == null ? "—" : Number(value).toFixed(2);

// 政策检索：两路检索器（BM25 / 向量）的得分、交叉编码器重排得分与“无依据”阈值，以及最终采用了哪些条款。
function retrievalView(retrieval) {
  const wrap = el("div", undefined, "retrieval");
  const { best_score: best, threshold } = retrieval;
  if (threshold != null) {
    const reliable = best != null && best >= threshold;
    const verdict = el("p", undefined, `retrieval-verdict tone-${reliable ? "ok" : "human"}`);
    verdict.textContent = best == null ? "没有检索到候选条款，判定为无可靠依据。"
      : `最高重排得分 ${score(best)}，阈值 ${score(threshold)}：${reliable ? "有可靠依据" : "无可靠依据（不据此回答）"}`;
    wrap.append(verdict);
  }
  if (!retrieval.candidates?.length) return wrap;
  const scroller = el("div", undefined, "table-scroll");
  const table = el("table", undefined, "retrieval-table");
  const head = el("tr");
  for (const name of ["候选条款", "BM25", "向量", "重排", "采用"]) head.append(el("th", name));
  table.append(el("thead"), el("tbody"));
  table.tHead.append(head);
  for (const c of retrieval.candidates) {
    const row = el("tr", undefined, c.used ? "used" : "");
    const name = el("td");
    const [source] = String(c.matched || "").split(":");
    name.append(el("strong", c.chunk_id), el("small", `${c.title} · 经${sources[source] || source || "条款"}命中`));
    const below = threshold != null && c.rerank_score != null && c.rerank_score < threshold;
    const mark = el("td", c.used ? "✓" : below ? "✕" : "—");
    mark.title = c.used ? "采用为依据" : below ? "重排得分低于阈值" : "未采用（排名靠后或不适用）";
    row.append(name, el("td", score(c.bm25_score)), el("td", score(c.vector_score)),
      el("td", score(c.rerank_score), below ? "below" : ""), mark);
    table.tBodies[0].append(row);
  }
  scroller.append(table);
  wrap.append(scroller, el("p", "BM25 与向量为两路检索器各自的得分（“—”表示该路未命中），两路按 RRF 融合后由交叉编码器重排；"
    + "✓ 为采用的依据，✕ 为重排得分低于阈值；采用前还需通过商品、下单日期与原因的适用性过滤。", "muted trace-note"));
  return wrap;
}
let phoenixUrl = null;
fetch("/observability").then(response => response.ok ? response.json() : {}).then(data => { phoenixUrl = data.phoenix_url || null; }).catch(() => {});
const resultLabel = value => results[value] || statusLabel(value);
const runTone = status => ["error", "stopped"].includes(status) ? "danger" : status === "workflow_handoff" ? "human" : !status ? "info" : "ok";
const ms = value => value == null ? "" : value >= 1000 ? `${(value / 1000).toFixed(1)} 秒` : `${Math.round(value)} ms`;

let trace = null;

function factList(entries) {
  const list = el("dl", undefined, "trace-facts");
  for (const [name, value] of entries) {
    if (value === undefined || value === null || value === "" || (Array.isArray(value) && !value.length)) continue;
    list.append(el("dt", name), el("dd", String(value)));
  }
  return list.children.length ? list : null;
}

function resultEntries(result) {
  return [
    ["结果", result.status && resultLabel(result.status)],
    ["识别意图", result.intent && (intents[result.intent] || result.intent)],
    ["需要补充", result.missing_fields?.map(key => fields[key] || key).join("、")],
    ["规则判断", result.rule_code],
    ["订单", result.order_id],
    ["选择工具", result.tool_name && label(result.tool_name)],
    ["下一步", result.next && label(result.next)],
    ["方案", result.proposal && `${result.proposal.id} · v${result.proposal.version} · ${statusLabel(result.proposal.status)}`],
    ["申请", result.return_request && `${result.return_request.id} · ${statusLabel(result.return_request.status)}`],
    ["工单", result.ticket && `${result.ticket.id} · ${statusLabel(result.ticket.status)}`],
    ["复用已有结果", result.reused === undefined ? null : result.reused ? "是" : "否"],
    ["命中条款数", result.policy_refs?.length],
    ["记录数", result.count],
  ];
}

function eventNode(event, maxDuration, shownCitations) {
  const [kindName, icon] = kinds[event.kind] || [event.kind, "•"];
  const item = el("li", undefined, `trace-item kind-${event.kind} trace-${event.status}`);
  item.append(el("span", icon, "trace-icon"));
  const box = el("details", undefined, "trace-box");
  const summary = el("summary");
  const meta = el("span", undefined, "trace-meta");
  meta.append(el("span", kindName, "trace-kind"), el("span", eventStatus[event.status] || event.status, "trace-state"));
  if (event.via === "mcp") meta.append(el("span", "经 MCP", "trace-via"));
  if (event.duration_ms != null) meta.append(el("span", ms(event.duration_ms)));
  const bar = el("span", undefined, "trace-bar");
  bar.style.setProperty("--w", `${Math.max(2, Math.round(((event.duration_ms || 0) / maxDuration) * 100))}%`);
  summary.append(el("span", label(event.name), "trace-name"), meta, bar);
  box.append(summary);

  const result = event.result || {};
  const args = factList(Object.entries(event.args || {}).map(([key, value]) => [argNames[key] || key, value]));
  if (args) box.append(el("h4", "参数"), args);
  const facts = factList(resultEntries(result));
  if (facts) box.append(el("h4", "结果"), facts);
  if (event.error_type) box.append(el("p", `失败类型：${event.error_type}`, "trace-failure"));
  const failure = event.error_detail;
  if (failure) {
    box.append(el("h4", "模型故障"), factList([
      ["故障分类", failureNames[failure.category] || failure.category],
      ["HTTP 状态", failure.http_status], ["请求编号", failure.request_id],
      ["已等待", ms(failure.elapsed_ms)], ["首个数据", failure.first_chunk_ms == null ? "未收到" : ms(failure.first_chunk_ms)],
      ["尝试次数", failure.attempt],
    ]));
  }
  if (result.selected_tools) {
    // Agent 子图中模型自主选择的只读工具及参数。
    box.append(el("h4", "模型选择的工具"), factList(result.selected_tools.length
      ? result.selected_tools.map(call => [label(call.name), Object.entries(call.args || {}).map(([k, v]) => `${argNames[k] || k} ${v}`).join("，") || "无参数"])
      : [["结果", "未选择工具"]]));
  }
  if (result.retrieval) box.append(el("h4", "检索命中与重排"), retrievalView(result.retrieval));
  if (event.attempts > 1) box.append(el("p", `临时失败后重试，共尝试 ${event.attempts} 次。`, "muted trace-note"));
  const metrics = event.model_metrics;
  if (metrics) {
    box.append(el("h4", "模型响应"), factList([
      ["总耗时", ms(metrics.elapsed_ms)], ["首个数据", metrics.first_chunk_ms == null ? null : ms(metrics.first_chunk_ms)],
      ["尝试次数", metrics.attempts > 1 ? metrics.attempts : null],
    ]));
  }
  // 同一条款在后续保存、回复步骤中重复出现时只给出编号，避免反复展开同一段正文。
  const fresh = (result.citations || []).filter(citation => !shownCitations.has(citation.chunk_id || citation.clause_id));
  const repeated = (result.citations || []).length - fresh.length;
  if (repeated) box.append(el("p", `政策依据同上：${result.citations.map(c => `${c.clause_id} / ${c.version}`).join("、")}`, "muted trace-note"));
  if (fresh.length) {
    box.append(el("h4", "政策依据"));
    for (const citation of fresh) {
      shownCitations.add(citation.chunk_id || citation.clause_id);
      const card = el("div", undefined, "citation");
      card.append(el("strong", `${citation.clause_id} / ${citation.version}`), el("span", citation.title), el("p", citation.body));
      box.append(card);
    }
  }
  const raw = el("details", undefined, "trace-raw");
  raw.append(el("summary", "详细数据"),
    el("pre", JSON.stringify({ kind: event.kind, name: event.name, args: event.args, result: event.result }, null, 2)));
  box.append(raw);
  // 补问、引用政策或失败的步骤默认展开，直接看出“为何补问、依据什么、在哪失败”。
  box.open = event.status === "error" || Boolean(result.missing_fields?.length || fresh.length || result.retrieval);
  item.append(box);
  return item;
}

export function renderTrace(value, progress = null) {
  trace = value;
  const root = document.querySelector("#trace-events");
  root.replaceChildren();
  const events = (value?.events || []).filter(Boolean);
  const last = [...events].reverse().find(event => event.result?.status)?.result;
  document.querySelector("#trace-wait").textContent = progress?.summary ||
    (last?.missing_fields?.length ? `需要补充：${last.missing_fields.map(key => fields[key] || key).join("、")}` : runWaiting[value?.status]) ||
    "这里显示当前会话最近一次运行：识别结果、查询了什么、依据哪条政策，以及在哪一步等待或失败。";
  if (!value) { root.append(el("p", "发送消息后逐步显示运行过程。", "empty")); return; }

  const head = el("div", undefined, "trace-summary");
  const top = el("div", undefined, "trace-summary-top");
  top.append(el("strong", "本轮运行"), el("span", value.status ? resultLabel(value.status) : "处理中", `pill tone-${runTone(value.status)}`));
  const usage = value.usage || {};
  const limits = value.limits || {};
  const budget = (used, max) => max ? `${used} / ${max}` : String(used);
  const counts = { node: 0, model: 0, tool: 0 };
  for (const event of events) counts[event.kind] = (counts[event.kind] || 0) + 1;
  const stats = el("div", undefined, "trace-stats");
  for (const [name, number] of [["总耗时", value.duration_ms == null ? "…" : ms(value.duration_ms)],
    ["流程节点", usage.steps ?? counts.node], ["模型调用 / 上限", budget(usage.model_calls ?? counts.model, limits.max_model_calls)],
    ["工具调用 / 上限", budget(usage.tool_calls ?? counts.tool, limits.max_tool_calls)],
    ["输入 token", usage.tokens?.input_tokens ?? "—"], ["输出 token", usage.tokens?.output_tokens ?? "—"]]) {
    const stat = el("div", undefined, "trace-stat");
    stat.append(el("span", String(number)), el("small", name));
    stats.append(stat);
  }
  head.append(top, stats, el("p", value.id, "item-id"));
  if (phoenixUrl) {
    // Phoenix 开启时提供完整调用链入口（本地开发调试用）；页面轨迹仍是脱敏摘要。
    const link = el("a", "在 Phoenix 中查看完整调用链 ↗", "trace-phoenix");
    link.href = phoenixUrl; link.target = "_blank"; link.rel = "noopener";
    head.append(link);
  }
  root.append(head);

  const maxDuration = Math.max(1, ...events.map(event => event.duration_ms || 0));
  const list = el("ol", undefined, "trace-list");
  const shownCitations = new Set();
  for (const event of events) list.append(eventNode(event, maxDuration, shownCitations));
  root.append(list);
}

export const stepLabel = label;

// hooks.onStep(event) 在每个节点或工具开始/结束时调用；hooks.onDraft(text) 接收政策答复草稿增量。
export async function streamMessage(content, hooks = {}) {
  const response = await fetch("/messages/stream", { method: "POST", credentials: "same-origin", headers: roleHeaders({ "Content-Type": "application/json" }), body: JSON.stringify({ content }) });
  if (!response.ok) throw new Error("消息请求未接受，请核对身份并刷新记录。");
  renderTrace(null);
  let buffer = "", completed = false;
  const reader = response.body.getReader(), decoder = new TextDecoder();
  try {
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      let end;
      while ((end = buffer.indexOf("\n\n")) >= 0) {
        const frame = buffer.slice(0, end); buffer = buffer.slice(end + 2);
        if (!frame.startsWith("data: ")) continue;
        const event = JSON.parse(frame.slice(6));
        if (event.type === "trace") {
          if (trace?.id !== event.run_id) trace = { id: event.run_id, events: [] };
          trace.events[event.event.sequence - 1] = event.event;
          renderTrace(trace);
          hooks.onStep?.(event.event);
        } else if (event.type === "reply_delta") {
          hooks.onDraft?.(event.text);
        } else if (event.type === "result") {
          completed = true; renderTrace(event.data.workflow);
        } else if (event.type === "error") {
          if (event.workflow) renderTrace(event.workflow);
          throw new Error(event.detail);
        }
      }
      if (done) break;
    }
    if (!completed) throw new Error("事件连接中断，请刷新记录核对结果；不会自动重发。");
  } finally { reader.releaseLock(); }
}

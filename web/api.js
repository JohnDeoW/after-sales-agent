// 所有接口请求的唯一入口：统一携带会话 Cookie 和本标签页的演示角色，并把失败转换为可展示的中文信息。

// 每个浏览器标签页记住自己的演示角色，服务端据此读取该角色的会话，多个标签页可同时以不同身份操作。
const ROLE_KEY = "after-sales-demo-role";
const ROLES = ["customer", "support", "warehouse"];

function readRole() {
  try {
    const saved = sessionStorage.getItem(ROLE_KEY);
    if (ROLES.includes(saved)) return saved;
  } catch { /* 隐私模式等无法使用 sessionStorage 时，退回通用会话 */ }
  const fromUrl = new URLSearchParams(location.search).get("role");
  return ROLES.includes(fromUrl) ? fromUrl : null;
}

let tabRole = readRole();

export function getTabRole() { return tabRole; }

export function setTabRole(role) {
  tabRole = ROLES.includes(role) ? role : null;
  try { if (tabRole) sessionStorage.setItem(ROLE_KEY, tabRole); } catch { /* 同上 */ }
}

export function roleHeaders(headers = {}) {
  return tabRole ? { ...headers, "X-Demo-Role": tabRole } : headers;
}

export async function api(path, options = {}) {
  const response = await fetch(path, { credentials: "same-origin", ...options, headers: roleHeaders(options.headers) });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const failure = new Error(typeof data.detail === "string" ? data.detail : "请求未完成，请检查输入后重试。");
    failure.status = response.status;
    throw failure;
  }
  return data;
}

export function post(path, body) {
  return api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
}

export function errorText(failure) {
  return failure instanceof TypeError ? "无法连接服务，请检查服务是否启动后重试。" : failure.message;
}

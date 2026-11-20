// 身份选择：无会话时在欢迎页展示，有会话时通过顶栏“切换身份”打开对话框。
import { api, post, errorText, getTabRole, setTabRole } from "/static/api.js";
import { $ } from "/static/dom.js";
import { roles } from "/static/labels.js";
import { state, setBusy } from "/static/store.js";

const card = $("#identity-card");
const form = $("#identity-form");
const select = $("#identity");
const status = $("#identity-status");
const error = $("#identity-error");
const dialog = $("#identity-dialog");

const selectedRole = () => form.elements.role.value;

export async function loadAccounts() {
  const lists = await Promise.all(Object.entries(roles).map(async ([key, config]) => [key, await api(config.list)]));
  state.accounts = Object.fromEntries(lists);
  renderAccounts();
}

function renderAccounts() {
  const list = state.accounts[selectedRole()] || [];
  select.replaceChildren(...list.map(user => new Option(`${user.display_name} · ${user.id}`, user.id)));
  $("#enter").disabled = !select.options.length;
  status.textContent = select.options.length ? "选择账号后建立新会话；刷新页面会保留当前身份。" : "当前角色暂无可用演示账号。";
}

export function showIdentityError(failure) {
  error.textContent = errorText(failure);
  error.hidden = false;
}

function place(slot, inDialog) {
  slot.append(card);
  card.classList.add("placed");
  $("#close-identity").hidden = !inDialog;
}

export function showWelcome() {
  // 从“新标签页打开”链接进入时预选对应角色。
  if (getTabRole() && form.elements.role.value !== getTabRole()) {
    form.elements.role.value = getTabRole();
    renderAccounts();
  }
  place($("#identity-slot"), false);
  $("#welcome").hidden = false;
  $("#app").hidden = true;
  $("#identity-chip").hidden = true;
}

function openDialog() {
  if (state.busy) return;
  error.hidden = true;
  if (state.user) {
    form.elements.role.value = state.user.role;
    renderAccounts();
    select.value = state.user.id;
    status.textContent = `当前身份：${state.user.display_name}（${state.user.id}）。切换会替换此浏览器的当前会话。`;
  }
  place($("#identity-dialog-slot"), true);
  dialog.showModal();
}

export function showUser(user) {
  $("#welcome").hidden = true;
  $("#app").hidden = false;
  $("#identity-chip").hidden = false;
  $("#identity-avatar").textContent = user.display_name.replace(/^演示/, "").slice(0, 1) || "用";
  $("#identity-name").textContent = user.display_name;
  $("#identity-role").textContent = `${roles[user.role].label} · ${user.id}`;
  if (dialog.open) dialog.close();
}

export function initIdentity(onLogin, onReload) {
  form.addEventListener("change", event => { if (event.target.name === "role") renderAccounts(); });
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (state.busy) return;
    const config = roles[selectedRole()];
    setBusy(true);
    error.hidden = true;
    status.textContent = "正在建立会话…";
    try {
      const { user } = await post(config.login, { [config.key]: select.value });
      setTabRole(user.role);
      await onLogin(user);
    } catch (failure) {
      showIdentityError(failure);
      status.textContent = "身份切换未完成，请重试。";
    } finally { setBusy(false); }
  });
  $("#switch-identity").addEventListener("click", openDialog);
  $("#close-identity").addEventListener("click", () => dialog.close());
  $("#reload-identities").addEventListener("click", onReload);
}

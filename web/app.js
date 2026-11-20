// 页面入口：恢复身份、按角色切换工作台，并把忙碌状态统一应用到各区域的可编辑控件。
import { api, setTabRole } from "/static/api.js";
import { $ } from "/static/dom.js";
import { state, onBusy, setBusy, resetForUser } from "/static/store.js";
import { initIdentity, loadAccounts, showWelcome, showUser, showIdentityError } from "/static/identity.js";
import { initCustomer, loadCustomer, resetCustomerView } from "/static/customer.js";
import { initStaff, loadStaff, resetStaffView, showSupportView } from "/static/staff.js";

function applyBusy(busy) {
  const { ready } = state;
  const set = (selector, disabled) => document.querySelectorAll(selector).forEach(node => { node.disabled = disabled; });
  $("#identity-fields").disabled = busy || !Object.keys(state.accounts).length;
  $("#case-actions").disabled = busy;
  $("#message-fields").disabled = busy || !ready.customer;
  set("#quick-prompts button", busy || !ready.customer);
  set("#message-list .card-actions", busy || !ready.customer);
  set("#case-details fieldset", busy || !ready.customer);
  set("#customer-exchange-details .event-edit", busy || !ready.customer);
  $("#support-queue-fields").disabled = busy;
  set(".support-edit", busy || !ready.support);
  $("#event-queue-fields").disabled = busy;
  set("#event-details .event-edit", busy || !ready.events);
  $("#warehouse-head-fields").disabled = busy;
  set("#warehouse-board .event-edit", busy || !ready.events);
  $("#message-list").setAttribute("aria-busy", String(busy));
}

async function enterUser(user) {
  resetForUser(user);
  showUser(user);
  resetCustomerView();
  resetStaffView();
  $("#customer-workspace").hidden = user.role !== "customer";
  $("#support-tabs").hidden = user.role !== "support";
  $("#warehouse-workspace").hidden = user.role !== "warehouse";
  if (user.role === "support") showSupportView("support");
  else {
    $("#support-workspace").hidden = true;
    $("#event-workspace").hidden = true;
  }
  if (user.role === "customer") await loadCustomer();
  else await loadStaff();
}

async function initialize() {
  setBusy(true);
  $("#reload-identities").hidden = true;
  $("#identity-error").hidden = true;
  try {
    await loadAccounts();
    let user = null;
    try { user = (await api("/demo/sessions/current")).user; }
    catch (failure) { if (failure.status !== 401) throw failure; }
    if (user) {
      setTabRole(user.role);
      await enterUser(user);
    }
    else showWelcome();
  } catch (failure) {
    showWelcome();
    showIdentityError(failure);
    $("#identity-status").textContent = "账号加载未完成。";
    $("#reload-identities").hidden = false;
  } finally { setBusy(false); }
}

onBusy(applyBusy);
initIdentity(enterUser, initialize);
initCustomer();
initStaff();
initialize();

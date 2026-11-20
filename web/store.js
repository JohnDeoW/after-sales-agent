// 页面状态集中在这里；各区域模块只读写自己的分支，忙碌状态由 app.js 统一应用到界面。
export const state = {
  user: null,
  accounts: {},
  busy: false,
  // 某区域最近一次读取失败后置为 false，禁止在未核对服务端结果前继续提交。
  ready: { customer: false, support: false, events: false },
  customer: { focus: "case", ticket: null, exchange: null },
  support: { selected: null, view: "support" },
  events: { selected: null },
};

const busyListeners = [];
export const onBusy = listener => busyListeners.push(listener);

export function setBusy(value) {
  state.busy = value;
  for (const listener of busyListeners) listener(value);
}

export function resetForUser(user) {
  state.user = user;
  state.ready = { customer: false, support: false, events: false };
  state.customer = { focus: "case", ticket: null, exchange: null };
  state.support = { selected: null, view: "support" };
  state.events = { selected: null };
}

// 同一时间只执行一个页面操作；失败不自动重发，由用户刷新核对后再继续。
export async function runTask({ scope, onError }, action) {
  if (state.busy) return;
  setBusy(true);
  try { await action(); }
  catch (failure) {
    if (scope) state.ready[scope] = false;
    onError(failure);
  } finally { setBusy(false); }
}

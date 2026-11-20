// 小型 DOM 工具：只用 textContent 写入文本，避免把服务端或用户文本当作 HTML。
export function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = text;
  if (className) node.className = className;
  return node;
}

export const $ = selector => document.querySelector(selector);

// 数据库时间为 UTC 的 "YYYY-MM-DD HH:MM:SS"，按浏览器本地时间显示。
export function formatTime(value) {
  if (!value) return "";
  const date = new Date(String(value).replace(" ", "T") + (String(value).endsWith("Z") ? "" : "Z"));
  if (Number.isNaN(date.getTime())) return "";
  const today = new Date();
  const sameDay = date.toDateString() === today.toDateString();
  const time = date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
  return sameDay ? time : `${date.getMonth() + 1}月${date.getDate()}日 ${time}`;
}

export const money = cents => `${(cents / 100).toFixed(2)} 元`;

// 距今时长，用于显示待办等待了多久。
export function since(value) {
  if (!value) return "";
  const date = new Date(String(value).replace(" ", "T") + (String(value).endsWith("Z") ? "" : "Z"));
  const minutes = Math.max(0, Math.floor((Date.now() - date.getTime()) / 60000));
  if (Number.isNaN(minutes)) return "";
  if (minutes < 1) return "不到 1 分钟";
  if (minutes < 60) return `${minutes} 分钟`;
  if (minutes < 60 * 24) return `${Math.floor(minutes / 60)} 小时`;
  return `${Math.floor(minutes / 1440)} 天`;
}

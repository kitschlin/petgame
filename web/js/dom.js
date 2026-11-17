// 极简 DOM 辅助：h('div', {class: 'x', onclick: fn}, 子节点…)

export function h(tag, props, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value == null || value === false) continue;
    if (key === 'class') el.className = value;
    else if (key === 'dataset') Object.assign(el.dataset, value);
    else if (key === 'style' && typeof value === 'object') Object.assign(el.style, value);
    else if (key.startsWith('on') && typeof value === 'function') el.addEventListener(key.slice(2), value);
    else if (value === true) el.setAttribute(key, '');
    else el.setAttribute(key, value);
  }
  append(el, children);
  return el;
}

export function append(el, children) {
  for (const child of [children].flat(Infinity)) {
    if (child == null || child === false) continue;
    el.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return el;
}

export function clear(el) {
  el.replaceChildren();
  return el;
}

// 清空后重新填充。和 h() 一样会忽略 null/false、展开数组
// （原生的 Element.append 会把它们渲染成文字）。
export function fill(el, ...children) {
  el.replaceChildren();
  return append(el, children);
}

// 数值展示：整数不带小数，否则固定两位小数
export function fmt(value) {
  return Number.isInteger(value) ? String(value) : value.toFixed(2);
}

export const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export function uid() {
  if (globalThis.crypto?.randomUUID) return crypto.randomUUID();
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

export function formatTime(iso) {
  return iso ? iso.replace('T', ' ') : '';
}

// 悬停 / 键盘聚焦时的小提示。只做补充：图上的数值本身都直接写出来了，不看提示也读得到。

import { fill, h } from '../dom.js';

let tip = null;
let owner = null;

function ensure() {
  if (!tip) {
    tip = h('div', { class: 'viz-tip', role: 'tooltip', hidden: true });
    document.body.append(tip);
    window.addEventListener('hashchange', hideTip);
    window.addEventListener('scroll', hideTip, true);
  }
  return tip;
}

function place(x, y) {
  const box = tip.getBoundingClientRect();
  const left = Math.max(8, Math.min(x + 12, window.innerWidth - box.width - 8));
  const top = y + 16 + box.height > window.innerHeight ? y - box.height - 10 : y + 16;
  tip.style.left = `${left}px`;
  tip.style.top = `${Math.max(8, top)}px`;
}

export function hideTip() {
  if (tip) tip.hidden = true;
  owner = null;
}

// content() 返回提示的内容（节点、字符串或它们的数组）；数值写在前面，名称在后
export function attachTip(el, content) {
  const show = (x, y) => {
    fill(ensure(), content());
    tip.hidden = false;
    owner = el;
    place(x, y);
  };
  el.addEventListener('pointerenter', (event) => show(event.clientX, event.clientY));
  el.addEventListener('pointermove', (event) => { if (owner === el) place(event.clientX, event.clientY); });
  el.addEventListener('pointerleave', hideTip);
  el.addEventListener('focus', () => {
    const box = el.getBoundingClientRect();
    show(box.left + 24, box.bottom - 8);
  });
  el.addEventListener('blur', hideTip);
  if (!el.hasAttribute('tabindex')) el.setAttribute('tabindex', '0');
  return el;
}

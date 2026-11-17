// 界面导览：把页面其余部分压暗，只留一个「聚光灯」照着要讲的区域，旁边一个气泡写说明。
// 只讲解，不替玩家操作；随时可以跳过（Esc），也可以用 ← → 翻页。
//
// steps: [{ target, title, text }]
//   target  选择器或返回元素的函数；找不到（例如 AI Coach 没启用）的那一步自动略过；
//           不写 target 的一步显示在屏幕中间
//   text    字符串、节点或它们的数组

import { fill, h } from '../dom.js';

let active = null; // 同一时间只开一个导览

const resolve = (target) => (typeof target === 'function' ? target() : document.querySelector(target));

export function closeTour() {
  if (active) active.close();
}

export function startTour(steps, { onDone } = {}) {
  closeTour();
  const list = steps.filter((step) => !step.target || resolve(step.target));
  if (!list.length) return null;

  let index = 0;
  let frame = 0;
  let last = '';
  const hole = h('div', { class: 'tour-hole' });
  const bubble = h('div', { class: 'tour-bubble', role: 'dialog', 'aria-live': 'polite' });
  // 遮罩挡住点击，免得导览期间误点了技能或卡片
  const layer = h('div', { class: 'tour-layer', id: 'tour' }, hole, bubble);
  document.body.append(layer);

  function close(finished = false) {
    if (!layer.isConnected) return;
    cancelAnimationFrame(frame);
    layer.remove();
    window.removeEventListener('keydown', onKey, true);
    window.removeEventListener('hashchange', onHash);
    active = null;
    if (onDone) onDone(finished);
  }

  function go(next) {
    if (next < 0) return;
    if (next >= list.length) { close(true); return; }
    index = next;
    const element = list[index].target ? resolve(list[index].target) : null;
    if (element) element.scrollIntoView({ block: 'center', inline: 'nearest' });
    renderBubble();
    last = '';
    place();
  }

  function renderBubble() {
    const step = list[index];
    const final = index === list.length - 1;
    fill(bubble,
      h('div', { class: 'tour-head' },
        h('b', {}, step.title),
        h('span', { class: 'tour-count' }, `${index + 1} / ${list.length}`)),
      h('div', { class: 'tour-text' }, step.text),
      h('div', { class: 'tour-buttons' },
        final ? h('span') : h('button', { class: 'btn ghost small', id: 'tour-skip', onclick: () => close(false) }, '跳过导览'),
        h('span', { class: 'tour-dots' },
          list.map((_, i) => h('i', { class: i === index ? 'on' : '' }))),
        index > 0 ? h('button', { class: 'btn small', id: 'tour-prev', onclick: () => go(index - 1) }, '上一步') : null,
        h('button', { class: 'btn primary small', id: 'tour-next', onclick: () => go(index + 1) },
          final ? (step.done || '知道了，开始玩') : '下一步')));
  }

  // 每帧对一次位置：页面滚动、窗口缩放、内容变化（例如 AI 建议刚回来）时聚光灯跟着走
  function place() {
    cancelAnimationFrame(frame);
    const step = list[index];
    const element = step?.target ? resolve(step.target) : null;
    const pad = 6;
    const box = element ? element.getBoundingClientRect() : null;
    const key = box ? `${box.left}|${box.top}|${box.width}|${box.height}` : 'center';
    if (key !== last || !layer.isConnected) {
      last = key;
      const vw = window.innerWidth;
      const vh = window.innerHeight;
      const bw = Math.min(380, vw - 24);
      bubble.style.width = `${bw}px`;
      const bh = bubble.offsetHeight;
      let left;
      let top;
      if (!box) {
        Object.assign(hole.style, { left: `${vw / 2}px`, top: `${vh / 2}px`, width: '0px', height: '0px' });
        left = (vw - bw) / 2;
        top = (vh - bh) / 2;
      } else {
        Object.assign(hole.style, {
          left: `${box.left - pad}px`,
          top: `${box.top - pad}px`,
          width: `${box.width + pad * 2}px`,
          height: `${box.height + pad * 2}px`,
        });
        // 优先放在区域下方，放不下就放上方，再放不下就贴在区域里面的右下角
        const below = box.bottom + pad + 12;
        const above = box.top - pad - 12 - bh;
        if (below + bh <= vh - 8) top = below;
        else if (above >= 54) top = above;
        else top = Math.max(54, Math.min(box.bottom - bh - 12, vh - bh - 8));
        left = Math.min(Math.max(8, box.left + box.width / 2 - bw / 2), vw - bw - 8);
      }
      bubble.style.left = `${left}px`;
      bubble.style.top = `${top}px`;
    }
    frame = requestAnimationFrame(place);
  }

  function onKey(event) {
    if (event.key === 'Escape') close(false);
    else if (event.key === 'ArrowRight' || event.key === 'Enter') go(index + 1);
    else if (event.key === 'ArrowLeft') go(index - 1);
    else return;
    event.preventDefault();
    event.stopPropagation();
  }
  const onHash = () => close(false);

  window.addEventListener('keydown', onKey, true);
  window.addEventListener('hashchange', onHash);
  active = { close: () => close(false) };
  go(0);
  return active;
}

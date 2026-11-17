// 回放：用战斗记录里保存的事件重现整场战斗，可播放、暂停、单步、拖动进度。

import { api } from '../api.js';
import { h } from '../dom.js';
import { createArena, marksByEvent } from '../components/arena.js';
import { loadSpeed, saveSpeed, SPEEDS, wait } from '../player.js';

export async function mount(root, id) {
  let record;
  try {
    record = await api.battle(id);
  } catch (error) {
    root.append(h('div', { class: 'panel empty' }, error.message, ' ', h('a', { href: '#/records' }, '返回战斗记录')));
    return null;
  }

  const events = record.events;
  const total = events.length;
  let index = 0; // 已经播出的事件数
  let playing = false;
  let runId = 0;
  let speed = loadSpeed() || 1;
  let alive = true;

  const playButton = h('button', { class: 'btn primary', id: 'replay-play', onclick: toggle });
  const slider = h('input', {
    type: 'range', min: 0, max: total, value: 0, class: 'replay-slider',
    oninput: (event) => { pause(); show(Number(event.target.value)); },
  });
  const progress = h('span', { class: 'replay-progress' });
  const speedSelect = h('select', {
    class: 'speed',
    title: '回放速度',
    onchange: (event) => { speed = Number(event.target.value); saveSpeed(speed); },
  }, SPEEDS.filter((s) => s.value).map((s) =>
    h('option', { value: s.value, selected: s.value === speed }, s.label)));

  const arena = createArena({
    statics: record.static,
    opponent: record.opponent,
    live: false,
    tools: [h('a', { class: 'btn ghost', href: `#/records/${id}` }, '返回记录')],
  });
  arena.setMarks(marksByEvent(events, record.marks));

  root.append(
    h('div', { class: 'replay-bar' },
      h('b', {}, '战斗回放'),
      h('button', { class: 'btn', id: 'replay-reset', title: '回到开头', onclick: () => { pause(); show(0); } }, '⏮'),
      h('button', { class: 'btn', id: 'replay-prev', title: '上一步', onclick: () => { pause(); step(-1); } }, '◀'),
      playButton,
      h('button', { class: 'btn', id: 'replay-next', title: '下一步', onclick: () => { pause(); step(1); } }, '▶'),
      h('button', { class: 'btn', id: 'replay-end', title: '跳到结尾', onclick: () => { pause(); show(total); } }, '⏭'),
      slider, progress,
      h('label', { class: 'speed-label' }, '速度', speedSelect)),
    arena.el,
  );

  function update() {
    slider.value = index;
    progress.textContent = `${index} / ${total}`;
    playButton.textContent = playing ? '暂停' : index >= total ? '重新播放' : '播放';
  }

  // 直接跳到「已播出 count 个事件」的状态
  function show(count) {
    index = Math.max(0, Math.min(total, count));
    arena.log.reset(events.slice(0, index));
    arena.render(events[Math.max(index - 1, 0)].view);
    update();
  }

  // 单步：前进或后退到下一条有文字的事件
  function step(direction) {
    let next = index + direction;
    while (next > 0 && next < total && !events[next - 1].text) next += direction;
    if (direction > 0 && next <= total) {
      for (let i = index; i < Math.min(next, total); i += 1) arena.fx(events[i]);
    }
    show(next);
  }

  function pause() {
    playing = false;
    update();
  }

  async function toggle() {
    if (playing) return pause();
    if (index >= total) show(0);
    playing = true;
    const run = ++runId; // 暂停后马上再点播放时，让上一轮循环自行退出
    update();
    while (playing && alive && run === runId && index < total) {
      const event = events[index];
      index += 1;
      arena.render(event.view);
      arena.fx(event);
      arena.log.append(event);
      update();
      await wait(event, speed);
    }
    if (run !== runId) return;
    playing = false;
    if (alive) update();
  }

  show(0);
  return () => { alive = false; playing = false; };
}

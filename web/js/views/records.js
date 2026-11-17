// 战斗记录：列表，以及单场战斗的完整日志。

import { api } from '../api.js';
import { app, OPPONENT_LABEL } from '../app.js';
import { formatTime, h } from '../dom.js';
import { logLine, marksByEvent } from '../components/arena.js';
import { petChip } from '../components/petCard.js';

function resultText(winner) {
  return winner === 'draw' ? '平局' : `${app.sideName(winner)}获胜`;
}

function resultTag(winner) {
  return h('span', { class: `result-tag result-${winner}` }, resultText(winner));
}

// AI 教练启用时才显示复盘入口；未启用时页面与没有这个功能时完全一致
async function coachEnabled() {
  try {
    return (await api.coachStatus()).enabled;
  } catch {
    return false;
  }
}

export async function mount(root) {
  const [battles, coach] = await Promise.all([api.battles(), coachEnabled()]);
  const rows = battles.map((battle) =>
    h('tr', { dataset: { id: battle.id } },
      h('td', {}, formatTime(battle.ended_at)),
      h('td', {}, h('div', { class: 'chips' }, battle.lineups.opp.map((key) => petChip('opp', key)),
        h('span', { class: 'mode-tag' }, OPPONENT_LABEL[battle.opponent || 'manual']))),
      h('td', {}, h('div', { class: 'chips' }, battle.lineups.me.map((key) => petChip('me', key)))),
      h('td', {}, resultTag(battle.winner)),
      h('td', {}, `${battle.rounds} 回合`),
      h('td', { class: 'row-actions' },
        coach ? h('a', { class: 'btn small coach-review-btn', dataset: { act: 'review' }, href: `#/review/${battle.id}` }, '🧠 复盘') : null,
        h('a', { class: 'btn small', href: `#/records/${battle.id}` }, '查看日志'),
        h('a', { class: 'btn small primary', href: `#/replay/${battle.id}` }, '回放'))));

  root.append(
    h('div', { class: 'panel' },
      h('h2', {}, '📜 战斗记录'),
      battles.length
        ? h('table', { class: 'table', id: 'battle-table' },
          h('thead', {}, h('tr', {},
            ['结束时间', '对手方阵容', '我方阵容', '结果', '回合', ''].map((text) => h('th', {}, text)))),
          h('tbody', {}, rows))
        : h('div', { class: 'empty' }, '还没有战斗记录。', h('a', { href: '#/pvp' }, '去打一场 PVP'))),
  );
  return null;
}

export async function mountDetail(root, id) {
  let record;
  try {
    record = await api.battle(id);
  } catch (error) {
    root.append(h('div', { class: 'panel empty' }, error.message, ' ', h('a', { href: '#/records' }, '返回战斗记录')));
    return null;
  }
  const coach = await coachEnabled();
  const marks = marksByEvent(record.events, record.marks);

  const pets = Object.values(record.static.pets);
  const lineup = (side) =>
    h('div', { class: 'detail-lineup' },
      h('b', {}, `${app.sideName(side)}：`),
      pets.filter((pet) => pet.side === side).map((pet) =>
        h('span', { class: `pet-chip side-${side}` },
          h('span', { class: 'chip-emoji' }, pet.emoji),
          `${pet.name}（生命 ${pet.max_hp} · 攻击 ${pet.atk} · 速度 ${pet.speed} · 命中 ${pet.hit}%）`)));

  root.append(
    h('div', { class: 'panel' },
      h('div', { class: 'detail-head' },
        h('h2', {}, '战斗记录 ', resultTag(record.result.winner)),
        h('div', { class: 'row-actions' },
          coach ? h('a', { class: 'btn coach-review-btn', id: 'detail-review', href: `#/review/${id}` }, '🧠 AI 复盘') : null,
          h('a', { class: 'btn primary', id: 'detail-replay', href: `#/replay/${id}` }, '回放'),
          h('a', { class: 'btn ghost', href: '#/records' }, '返回列表'))),
      h('div', { class: 'detail-meta muted' },
        `${formatTime(record.started_at)} 开始 · ${formatTime(record.ended_at)} 结束 · `,
        `对手方${OPPONENT_LABEL[record.opponent || 'manual']}操作 · `,
        `共 ${record.result.rounds} 个大回合、${record.actions.length} 次操作 · 随机种子 ${record.seed}`),
      lineup('opp'),
      lineup('me'),
      h('div', { class: 'log detail-log', id: 'detail-log' },
        record.events.filter((event) => event.text).map((event) => logLine(event, marks.get(event.i))))),
  );
  return null;
}

// AI 指导面板（战斗阶段）：空闲、思考中（重试 n/2）、建议、暂无建议 + 重试，以及开关。
// 只展示，不替玩家操作。面板里不使用 pet-card / log-line / skill-btn 等类名。

import { fill, h } from '../dom.js';
import { MAX_RETRIES } from '../coach.js';
import { attachTip, hideTip } from './tip.js';
import { traceView } from './traceView.js';

const pct = (value) => `${Math.round(value * 100)}%`;
const pp = (value) => Math.round(value * 100);

const BASIS = {
  engine: { text: '依据：推演明显领先', tip: '这个动作的推演胜率明显高于其他动作，AI 跟着推演推荐' },
  judgment: { text: '依据：AI 判断', tip: '推演没有明显领先的动作，AI 按自己的判断原则（击倒、集火、能量、连招、防守）选择' },
};
// 一个动作一行：名称（推演最高的加 ★）、条（按本局最高的胜率归一化）、胜率；下面一行是直接后果。误差在悬停提示里
// 一个动作一行：名称（推演最高的加 ★）、条（按本局最高的胜率归一化）、胜率 ± 误差；下面一行是直接后果
function meter(move, games, top) {
  const width = top > 0 ? Math.round((move.win_rate / top) * 100) : 0;
  const row = h('div', {
    class: `viz-meter move-meter${move.chosen ? ' chosen' : ''}${move.top ? ' best' : ''}`,
    dataset: { skill: move.skill, target: move.target || '' },
  },
    h('span', { class: 'viz-meter-name' }, move.top ? h('em', { class: 'viz-best' }, '★') : null, move.text),
    h('span', { class: 'viz-meter-track' }, h('span', { class: 'viz-meter-fill', style: { width: `${width}%` } })),
    h('span', { class: 'viz-meter-value' }, pct(move.win_rate)),
    h('span', { class: 'move-facts' }, move.facts || ''));
  return attachTip(row, () => [
    h('b', {}, pct(move.win_rate)), ` ${move.text}`,
    h('div', { class: 'viz-tip-sub' },
      `推演 ${games} 局 · 平局 ${pct(move.draw_rate)}${move.top ? ' · 推演胜率最高' : ` · 比最高少 ${pp(move.gap)} 个百分点（误差 ±${pp(move.se)}）`}`
      + `${move.chosen ? ' · AI 推荐' : ''}`),
    h('div', { class: 'viz-tip-sub' }, `直接后果：${move.facts || '—'}`),
  ]);
}

function adviceView(result) {
  const { advice, evidence } = result;
  const basis = BASIS[advice.basis || evidence.basis];
  const top = Math.max(0, ...evidence.moves.map((move) => move.win_rate));
  return [
    h('p', { class: 'coach-move-row' },
      h('span', { class: 'coach-label' }, '👉 推荐'),
      h('span', { class: 'coach-move', id: 'coach-move' }, advice.text),
      h('span', { class: 'coach-move-hint' }, '已在上方高亮，点它就行')),
    basis ? h('p', { class: `coach-basis basis-${advice.basis}`, id: 'coach-basis', title: basis.tip }, basis.text) : null,
    h('p', { class: 'coach-reason', id: 'coach-battle-reason' }, advice.reason),
    advice.risk ? h('p', { class: 'coach-risk' }, h('b', {}, '风险'), advice.risk) : null,
    advice.plan ? h('p', { class: 'coach-plan', id: 'coach-plan' }, h('b', {}, '计划'), advice.plan) : null,
    h('div', { class: 'viz viz-meters', id: 'coach-moves', role: 'group', 'aria-label': '各动作的推演胜率' },
      h('div', {
        class: 'viz-caption',
        title: '执行这一手之后，我方随机出招、对手按它的打法出招，打完为止。胜率只用来比较这一手的各个动作，绝对值偏低，不等于这一局最后的胜算。条长按本局最高的胜率画。',
      }, `各动作推演（每个 ${evidence.games} 局，只用来比较这一手）`),
      evidence.flat ? h('p', { class: 'coach-note' }, '所有动作推演结果相同，分不出高下') : null,
      evidence.moves.map((move) => meter(move, evidence.games, top))),
    traceView(result.trace),
    h('p', { class: 'coach-note' }, `${evidence.label} · AI 只给建议，仍需你自己点击技能和目标`),
  ];
}

export function createBattlePanel({ onRetry, onToggle }) {
  const body = h('div', { class: 'coach-body' });
  const toggle = h('button', { class: 'btn small ghost coach-toggle', id: 'coach-toggle', onclick: onToggle });
  const sub = h('span', { class: 'coach-sub', id: 'battle-coach-mode' }, '操作建议');
  const el = h('aside', { class: 'coach-panel battle-coach', id: 'battle-coach', dataset: { status: 'idle', guide: 'on' } },
    h('div', { class: 'coach-scroll' },
      h('div', { class: 'coach-head' },
        h('b', { class: 'coach-title' }, '🧠 AI 指导'), sub, toggle),
      body));

  // state 来自请求控制器；guide 是开关；idleText 是没有建议时的提示
  function render(state, { guide, idleText }) {
    hideTip();
    el.dataset.status = guide ? state.status : 'off';
    el.dataset.guide = guide ? 'on' : 'off';
    toggle.textContent = guide ? '关闭指导' : '开启指导';
    toggle.title = guide ? '关闭后不再请求建议，随时可以重新打开' : '打开后轮到我方行动时给出建议';
    if (!guide) {
      fill(body, h('p', { class: 'coach-idle' }, 'AI 指导已关闭。对局照常进行，你可以随时重新打开。'));
    } else if (state.status === 'thinking') {
      const text = state.retry > 0 ? `AI 思考中（重试 ${state.retry}/${MAX_RETRIES}）` : 'AI 思考中…';
      fill(body, h('div', { class: 'coach-thinking' }, h('span', { class: 'coach-spinner' }), text),
        h('p', { class: 'coach-note' }, '不用等它：你随时可以自己操作'));
    } else if (state.status === 'ready') {
      fill(body, adviceView(state.result));
      const mode = state.result.trace && state.result.trace.mode;
      sub.textContent = mode === 'session' ? '操作建议 · 整局一段对话' : mode === 'step' ? '操作建议 · 每步单独请求' : '操作建议';
    } else if (state.status === 'empty') {
      fill(body,
        h('div', { class: 'coach-empty' },
          h('b', {}, '暂无建议'), h('span', {}, state.message || ''),
          state.code === 'auth' ? null : h('button', { class: 'btn small', id: 'battle-coach-retry', onclick: onRetry }, '重试')),
        h('p', { class: 'coach-note' }, '对局不受影响，你可以完全手动完成这一局'));
    } else {
      fill(body, h('p', { class: 'coach-idle' }, idleText || '轮到我方宠物行动时，这里会给出本次行动的建议'));
    }
  }

  render({ status: 'idle' }, { guide: true });
  return { el, render };
}

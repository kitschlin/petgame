// AI 指导面板（战斗阶段）：空闲、思考中（重试 n/2）、建议、暂无建议 + 重试，以及开关。
// 只展示，不替玩家操作。面板里不使用 pet-card / log-line / skill-btn 等类名。

import { fill, h } from '../dom.js';
import { MAX_RETRIES } from '../coach.js';
import { attachTip, hideTip } from './tip.js';

const pct = (value) => `${Math.round(value * 100)}%`;

// 各动作的推演胜率：一行一个动作（名称、条、数字）。推荐的那一项用强调色，其余用灰色当背景
function meter(move, games) {
  const row = h('div', {
    class: `viz-meter${move.chosen ? ' chosen' : ''}`,
    dataset: { skill: move.skill, target: move.target || '' },
  },
    h('span', { class: 'viz-meter-name' }, move.text),
    h('span', { class: 'viz-meter-track' },
      h('span', { class: 'viz-meter-fill', style: { width: `${Math.round(move.win_rate * 100)}%` } })),
    h('span', { class: 'viz-meter-value' }, pct(move.win_rate)));
  return attachTip(row, () => [
    h('b', {}, pct(move.win_rate)), ` ${move.text}`,
    h('div', { class: 'viz-tip-sub' }, `推演 ${games} 局的胜率 · ${move.tier}${move.chosen ? ' · AI 推荐' : ''}`),
  ]);
}

function adviceView(result) {
  const { advice, evidence } = result;
  return [
    h('p', { class: 'coach-move-row' },
      h('span', { class: 'coach-label' }, '👉 推荐'),
      h('span', { class: 'coach-move', id: 'coach-move' }, advice.text),
      h('span', { class: 'coach-move-hint' }, '已在上方高亮，点它就行')),
    h('p', { class: 'coach-reason', id: 'coach-battle-reason' }, advice.reason),
    advice.risk ? h('p', { class: 'coach-risk' }, h('b', {}, '风险'), advice.risk) : null,
    h('div', { class: 'viz viz-meters', id: 'coach-moves', role: 'group', 'aria-label': '各动作的推演胜率' },
      h('div', {
        class: 'viz-caption',
        title: '用来比较各个动作的好坏。推演只往前看一整轮，之后假设对手按常规打法，所以开局时这些数字偏乐观，不等于这一局最后的胜算。',
      }, '各动作的推演胜率（用来比较动作）'),
      evidence.moves.map((move) => meter(move, evidence.games))),
    h('p', { class: 'coach-note' }, `${evidence.label} · AI 只给建议，仍需你自己点击技能和目标`),
  ];
}

export function createBattlePanel({ onRetry, onToggle }) {
  const body = h('div', { class: 'coach-body' });
  const toggle = h('button', { class: 'btn small ghost coach-toggle', id: 'coach-toggle', onclick: onToggle });
  const el = h('aside', { class: 'coach-panel battle-coach', id: 'battle-coach', dataset: { status: 'idle', guide: 'on' } },
    h('div', { class: 'coach-scroll' },
      h('div', { class: 'coach-head' },
        h('b', { class: 'coach-title' }, '🧠 AI 指导'), h('span', { class: 'coach-sub' }, '操作建议'), toggle),
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

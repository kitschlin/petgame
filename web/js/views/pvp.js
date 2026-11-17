// PVP 入口：根据对局相位显示「开始对战」、选将或战斗。

import { api } from '../api.js';
import { app, loadOpponentMode, saveOpponentMode } from '../app.js';
import { clear, fill, h } from '../dom.js';
import { mountBattle } from './battle.js';
import { mountDraft } from './draft.js';
import { openRules } from '../guide.js';

const MODE_INFO = {
  random: { title: '🎲 随机对手', tag: '新手练手', desc: '电脑操作对手方：随机选将，出招时能用特色技能就用，目标随机。你只需要操作我方。' },
  smart: { title: '🧠 智能对手', desc: '电脑操作对手方：8 只宠物都可能用，每一手都针对你已选的宠物；出招前把每个动作都推演一遍再选最好的。很强：自己摸索很难赢，照着 AI 的建议选将和出招才能占到上风。' },
  manual: { title: '👥 同屏手动', desc: '双方都由玩家在同一个界面里轮流操作，适合两个人一起玩或自己研究对局。' },
};

// preset：预先选中的对手模式（再来一局时是上一局的模式）；不传则用上次保存的
function mountIdle(root, show, preset = null) {
  let mode = preset || loadOpponentMode();
  const start = async () => {
    try {
      app.set(await api.startMatch(mode));
      show();
    } catch (error) {
      app.toast(error.message, 'error');
    }
  };
  const cards = h('div', { class: 'mode-cards', id: 'opp-mode' });
  const renderCards = () => fill(cards,
    Object.entries(MODE_INFO).map(([key, info]) =>
      h('button', {
        class: `mode-card${mode === key ? ' on' : ''}`,
        dataset: { mode: key },
        onclick: () => { mode = key; saveOpponentMode(key); renderCards(); },
      }, h('b', {}, info.title, info.tag ? h('em', { class: 'mode-tag' }, info.tag) : null), h('span', {}, info.desc))));
  renderCards();
  const note = h('p', { class: 'muted' }, '进入选将后，双方属性锁定到本场战斗结束，期间不能进行 PVE 历练。');
  root.append(
    h('div', { class: 'panel start-panel' },
      h('h2', {}, '⚔️ PVP 对战'),
      h('p', {}, '双方轮流从各自的宠物池拖拽选出 3 只宠物（对手方先选），然后轮流行动，最多 10 个大回合。',
        h('button', { class: 'link-btn', id: 'start-rules', onclick: openRules }, '📖 不懂规则？看玩法速览')),
      h('div', { class: 'mode-label' }, '对手方由谁操作？'),
      cards,
      note,
      h('button', { class: 'btn primary big', id: 'start-match', onclick: start }, '开始选将')),
  );
  // AI Coach 启用时：告诉第一次玩的人怎么上手（没启用时不插入这一行，页面与没有这个功能时一致）
  let alive = true;
  api.coachStatus().then((status) => {
    if (!alive || !status.enabled) return;
    note.before(h('p', { class: 'coach-start-tip', id: 'coach-start-tip' },
      h('b', {}, '🧠 第一次玩？'),
      '选将时把 AI 军师高亮的那张卡片拖进站位槽，战斗里点 AI 指导高亮的技能和目标——全程照着做，面对智能对手也有六成左右的胜算。',
      '输了就点「AI 复盘」，看输在哪、该练哪一项。'));
  }).catch(() => {});
  return () => { alive = false; };
}

export function mount(root) {
  let cleanup = null;

  // 相位变化（开始选将、选完进入战斗、再来一局）时重新挂载对应的子页面。
  // 再来一局：上一局仍是「已结束」，但先回到选择对手模式的开局页，由玩家点「开始选将」才开新局
  function show(next = null) {
    if (cleanup) cleanup();
    cleanup = null;
    clear(root);
    window.scrollTo(0, 0);
    const phase = app.state.phase;
    if (phase === 'idle') cleanup = mountIdle(root, show);
    else if (next?.again && phase === 'finished') cleanup = mountIdle(root, show, next.again);
    else if (phase === 'draft') cleanup = mountDraft(root, show);
    else cleanup = mountBattle(root, show);
  }

  show();
  return () => { if (cleanup) cleanup(); };
}

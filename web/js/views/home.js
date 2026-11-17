// 首页：两个入口 + 双方宠物图鉴（显示当前属性，含 PVE 成长）。

import { app, SIDE_STYLE } from '../app.js';
import { h } from '../dom.js';
import { petCard } from '../components/petCard.js';
import { markSeen, openRules, seen } from '../guide.js';

const PVP_ENTRY = {
  idle: { button: '开始对战', note: '对手方先选，一人一只轮流选出 3 只宠物' },  // 进入后可选随机 / 手动
  draft: { button: '继续选将', note: '有一场对局正在选将' },
  battle: { button: '继续战斗', note: '有一场战斗正在进行' },
  finished: { button: '查看上一局 / 再来一局', note: '上一局已经结束' },
};

export function mount(root) {
  const { phase, roster, config } = app.state;
  const entry = PVP_ENTRY[phase];

  const rosterRow = (side) =>
    h('div', { class: `roster side-${side}` },
      h('div', { class: 'roster-title' },
        h('b', {}, `${app.sideName(side)}宠物`),
        h('span', { class: 'muted' }, SIDE_STYLE[side])),
      h('div', { class: 'pool-cards' },
        config.species.map(({ key }) =>
          petCard(side, key, roster[side][key], { class: 'static', pop: side === 'opp' ? 'down' : 'up' }))));

  // 新手上手条：没关掉之前一直显示在最上面
  const welcome = seen('welcome') ? null : h('div', { class: 'welcome', id: 'welcome' },
    h('div', { class: 'welcome-icon' }, '🐣'),
    h('div', { class: 'welcome-main' },
      h('h2', {}, '第一次玩？三步上手'),
      h('ol', { class: 'welcome-steps' },
        h('li', {}, h('b', {}, '看规则'), '花 1 分钟看「玩法速览」：怎么赢、怎么选将、能量豆怎么用'),
        h('li', {}, h('b', {}, '打一局'), '点「PVP 对战」，先选「🎲 随机对手」熟悉操作；第一次进选将和战斗时会有界面导览'),
        h('li', {}, h('b', {}, '变强'), '跟着紫色的「AI 推荐」挑战「🧠 智能对手」；打不过就去 PVE 历练培养宠物'))),
    h('div', { class: 'welcome-actions' },
      h('button', { class: 'btn primary big', id: 'welcome-rules', onclick: openRules }, '📖 1 分钟看懂玩法'),
      h('button', {
        class: 'btn ghost small', id: 'welcome-close', title: '以后可以从右上角「玩法速览」再打开',
        onclick: () => { markSeen('welcome'); welcome.remove(); },
      }, '我会玩了，不再显示')));

  root.append(
    welcome,
    h('div', { class: 'home-entries' },
      h('a', { class: 'entry entry-pvp', href: '#/pvp', id: 'entry-pvp' },
        h('div', { class: 'entry-icon' }, '⚔️'),
        h('div', {},
          h('h2', {}, 'PVP 对战'),
          h('p', {}, '上方对手、下方我方。对手方可以交给电脑（随机 / 智能）操作，也可以同屏手动操作。选将 → 对战 → 结算 → 回放。'),
          h('p', { class: 'muted' }, entry.note)),
        h('span', { class: 'btn primary' }, entry.button)),
      h('a', { class: `entry entry-pve${app.locked ? ' disabled' : ''}`, href: '#/pve', id: 'entry-pve' },
        h('div', { class: 'entry-icon' }, '🏕️'),
        h('div', {},
          h('h2', {}, 'PVE 属性历练'),
          h('p', {}, '对手方更强。培养我方宠物，成功后永久提升对应属性。'),
          h('p', { class: 'muted' }, app.locked ? '选将和战斗期间属性锁定，暂时不能历练'
            : config.modes.filter((m) => m.enabled).map((m) => `${m.name} ${m.chance}%`).join('，'))),
        h('span', { class: 'btn' }, '进入历练'))),
    h('div', { class: 'panel' },
      h('h2', {}, '宠物图鉴'),
      h('p', { class: 'muted' }, '两套配置的技能与被动完全相同。对手方整体更强，我方可以通过 PVE 历练成长。把鼠标移到卡片上查看完整技能说明。'),
      rosterRow('opp'),
      rosterRow('me')),
  );
  // 第一次打开游戏：自动弹出玩法速览（只弹这一次）
  if (!seen('rules')) openRules();
  return null;
}

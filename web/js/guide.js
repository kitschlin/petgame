// 新手指引：「玩法速览」（分页讲规则的弹窗）+ 各页面的界面导览，以及顶栏右侧的两个入口。
//
// 第一次打开首页时自动弹出玩法速览，第一次进入选将 / 战斗时自动走一遍界面导览；
// 之后只能从顶栏手动打开。看过的记在 localStorage 里：
//   petarena.tutorial          设为 'off' 时不自动弹出任何指引（自动化测试用）
//   petarena.tutorial.<名字>    'done' 表示已经看过

import { api } from './api.js';
import { app } from './app.js';
import { fill, h } from './dom.js';
import { closeTour, startTour } from './components/tour.js';

const KEY = 'petarena.tutorial';

export function seen(name) {
  return localStorage.getItem(KEY) === 'off' || localStorage.getItem(`${KEY}.${name}`) === 'done';
}

export function markSeen(name) {
  localStorage.setItem(`${KEY}.${name}`, 'done');
}

// AI Coach 是否启用：只查一次
let coachPromise = null;
export function coachEnabled() {
  if (!coachPromise) coachPromise = api.coachStatus().then((s) => !!s.enabled).catch(() => false);
  return coachPromise;
}

// ------------------------------------------------------------ 界面导览

let pageTour = null; // 当前页面的导览：() => steps
let helpEl = null;

// 页面挂载时登记自己的导览（离开页面时传 null）。第一次来到这个页面时自动开始
export function setPageTour(name, steps) {
  pageTour = steps ? { name, steps } : null;
  renderHelp();
  if (steps && !seen(name) && !document.querySelector('.modal-overlay')) runPageTour();
}

export function runPageTour() {
  if (!pageTour) return;
  const { name, steps } = pageTour;
  if (startTour(steps())) markSeen(name);
}

export function clearPageTour() {
  pageTour = null;
  closeTour();
  renderHelp();
}

// ------------------------------------------------------------ 玩法速览

const li = (...parts) => h('li', {}, parts);
const tag = (text, kind = '') => h('span', { class: `guide-tag ${kind}` }, text);

function pages(coach) {
  const { config } = app.state;
  const rules = config.rules;
  const species = config.species;
  return [
    {
      icon: '🎯', title: '怎么算赢',
      body: [
        h('p', { class: 'guide-lead' }, '你和对手各带 3 只宠物同时上场，把对面 3 只全部打倒就赢。'),
        h('div', { class: 'guide-sides' },
          h('div', { class: 'guide-side side-opp' },
            h('b', {}, '⬆ 上方 · 对手方'), h('span', {}, '「坚韧控制」：生命拉满、整体更强，代价是速度偏慢')),
          h('div', { class: 'guide-side side-me' },
            h('b', {}, '⬇ 下方 · 我方（你）'), h('span', {}, '「迅捷突击」：速度快、主攻手爆发高，代价是生命偏低'))),
        h('ul', {},
          li(`最多 ${rules.max_rounds} 个大回合，打完还没分出胜负算`, tag('平局')),
          li('两边的宠物种类和技能完全一样，只是数值不同。对手底子更厚，所以要靠', tag('阵容'), '和', tag('出招'), '赢它。'),
          li('打不过也没关系：去', tag('PVE 历练'), '培养我方宠物，属性会永久提升。')),
      ],
    },
    {
      icon: '🐾', title: '选将：一人一只，轮流选',
      body: [
        h('div', { class: 'guide-order' },
          ['对', '我', '对', '我', '对', '我'].map((s, i) =>
            h('span', { class: `guide-step ${s === '对' ? 'side-opp' : 'side-me'}` }, h('i', {}, i + 1), s === '对' ? '对手' : '我方'))),
        h('ul', {},
          li('对手方先选，双方轮流各选 3 只不同的宠物。选中的立刻公开，', h('b', {}, '后选的一方可以针对'), '。'),
          li('把卡片', tag('拖到高亮的站位槽'), '（或者直接点一下卡片），再点', tag('确认'), '。确认后不能撤回。'),
          li('选的顺序就是站位：第 1 只是 1 号位，依此类推。'),
          li('鼠标停在卡片上可以看完整的技能说明。')),
      ],
    },
    {
      icon: '⚔️', title: '战斗：按速度轮流出手',
      body: [
        h('ul', {},
          li('每个大回合开始时，存活的宠物按', h('b', {}, '速度从快到慢'), '排成', tag('行动队列'), '（战斗页顶部那一排头像），每只行动一次。'),
          li('轮到你的宠物时，它上方的操作栏会亮起：先选', tag('普通攻击'), '或它的', tag('特色技能', 'special'), '，再点', tag('橙色闪烁', 'target'), '的目标。不需要目标的技能点了就直接放。'),
          li(tag('普通攻击'), '不花能量豆，造成等于自身攻击力的伤害。'),
          li(tag('被动技能', 'passive'), '自动生效，不用操作。'),
          li('一方全部倒下，战斗立刻结束。')),
        h('div', { class: 'guide-glossary' },
          h('span', {}, h('b', {}, '🛡 护盾'), '先扣护盾再扣血'),
          h('span', {}, h('b', {}, '❄ 寒毒结界'), '小蛇放的：敌方每次行动前掉血，并可能被冻结（跳过这次操作）'),
          h('span', {}, h('b', {}, '🔗 同心水链'), '小鱼放的：伤害先减 5%，再由连着的队友平摊')),
      ],
    },
    {
      icon: '🫘', title: '能量豆：全队共用',
      body: [
        h('div', { class: 'guide-beans' },
          h('span', {}, h('b', {}, rules.energy_start), '开局'),
          h('span', {}, h('b', {}, '+2'), '从第 2 回合起，每回合'),
          h('span', {}, h('b', {}, rules.energy_cap), '上限')),
        h('ul', {},
          li('特色技能要花能量豆（2 或 3 点），不够时按钮会变灰。'),
          li('能量豆是', h('b', {}, '全队共用'), '的：这只宠物花掉了，队友就少了。'),
          li(tag('小鼠'), '的被动让开局多 3 点，主动技能还能再补 3 点；想频繁放大招就带上它。'),
          li('常见思路：这回合先普攻攒豆，下回合放大招，往往比每回合放小技能更划算。')),
      ],
    },
    {
      icon: '📚', title: '8 只宠物速查',
      body: [
        h('div', { class: 'guide-pets' },
          species.map((s) => h('div', { class: 'guide-pet' },
            h('div', { class: 'guide-pet-head' }, h('span', { class: 'guide-pet-emoji' }, s.emoji), h('b', {}, s.name), h('i', {}, s.role)),
            h('p', {}, tag(`${s.skill.name} · ${s.skill.cost ? `${s.skill.cost} 豆` : '免费'}`, 'special'), s.skill.desc),
            h('p', {}, tag(s.passive.name, 'passive'), s.passive.desc)))),
      ],
      wide: true,
    },
    coach ? {
      icon: '🧠', title: '跟着 AI 教练玩',
      body: [
        h('p', { class: 'guide-lead' }, '看不懂也没关系：紫色的', tag('AI 推荐', 'ai'), '就是教练的建议，照着点就行。'),
        h('ul', { class: 'guide-flow' },
          li(h('b', {}, '选将'), '带紫色「AI 推荐」角标的卡片就是 AI 军师推荐的宠物，旁边的面板写着理由和胜率。'),
          li(h('b', {}, '战斗'), '带「AI 推荐」的技能按钮和「AI 建议的目标」就是推荐的出招；战斗日志右边的 AI 指导面板讲为什么这样打。'),
          li(h('b', {}, '复盘'), '打完点「AI 复盘」：看这局输在阵容、操作、属性还是运气，下一局怎么改。'),
          li(h('b', {}, '培养'), '需要练属性时，复盘页和 PVE 页会告诉你练哪只、练哪项、练到多少。')),
        h('p', { class: 'guide-tip' }, '推荐路线：第一局选「🎲 随机对手」熟悉操作，然后挑战「🧠 智能对手」。智能对手很强，全程照着 AI 的建议做，大约有六成胜算。'),
        h('p', { class: 'muted' }, 'AI 只给建议，拖拽、确认、点技能都要你自己来，也可以完全不理它。'),
      ],
    } : {
      icon: '🚀', title: '推荐上手路线',
      body: [
        h('ul', { class: 'guide-flow' },
          li(h('b', {}, '第 1 局'), '选「🎲 随机对手」熟悉操作：电脑随机选将、随机出招。'),
          li(h('b', {}, '进阶'), '挑战「🧠 智能对手」：它每一手都针对你，出招前会推演，自己摸索很难赢。'),
          li(h('b', {}, '变强'), '去「PVE 历练」培养我方宠物的攻击、生命、速度，再回来打。'),
          li(h('b', {}, '双人'), '「👥 同屏手动」：两个人用同一只鼠标轮流操作。')),
        h('p', { class: 'guide-tip' }, '想要 AI 教练（选将推荐、出招指导、赛后复盘）：把项目根目录的 .env.example 复制为 .env，填入 DEEPSEEK_API_KEY 后重启服务。'),
      ],
    },
  ];
}

let rulesModal = null;

export async function openRules() {
  if (rulesModal) return;
  closeTour();
  markSeen('rules');
  const list = pages(await coachEnabled());
  let index = 0;

  const tabs = h('div', { class: 'guide-tabs' });
  const body = h('div', { class: 'guide-body' });
  const buttons = h('div', { class: 'guide-buttons' });
  const box = h('div', { class: 'modal guide-modal', role: 'dialog', 'aria-modal': 'true', id: 'guide' },
    h('div', { class: 'guide-top' },
      h('h2', {}, '📖 玩法速览'),
      h('button', { class: 'btn ghost small guide-close', title: '关闭（Esc）', onclick: () => close() }, '✕')),
    tabs, body, buttons);
  const overlay = h('div', { class: 'modal-overlay guide-overlay', onclick: (e) => { if (e.target === overlay) close(); } }, box);

  function show(next) {
    index = Math.max(0, Math.min(list.length - 1, next));
    const page = list[index];
    box.classList.toggle('wide', !!page.wide);
    fill(tabs, list.map((p, i) => h('button', {
      class: `guide-tab${i === index ? ' on' : ''}`, onclick: () => show(i),
    }, h('span', {}, p.icon), p.title.split('：')[0])));
    fill(body, h('h3', { class: 'guide-title' }, `${page.icon} ${page.title}`), page.body);
    const final = index === list.length - 1;
    fill(buttons,
      h('span', { class: 'guide-count' }, `${index + 1} / ${list.length}`),
      index > 0 ? h('button', { class: 'btn', id: 'guide-prev', onclick: () => show(index - 1) }, '上一页') : null,
      final
        ? h('button', { class: 'btn primary', id: 'guide-done', onclick: () => { close(); if (app.state.phase === 'idle') app.go('#/pvp'); } },
          app.state.phase === 'idle' ? '⚔️ 去对战' : '知道了')
        : h('button', { class: 'btn primary', id: 'guide-next', onclick: () => show(index + 1) }, '下一页'));
  }

  function onKey(event) {
    if (event.key === 'Escape') close();
    else if (event.key === 'ArrowRight') show(index + 1);
    else if (event.key === 'ArrowLeft') show(index - 1);
  }

  function close() {
    overlay.remove();
    window.removeEventListener('keydown', onKey);
    window.removeEventListener('hashchange', close);
    rulesModal = null;
  }

  window.addEventListener('keydown', onKey);
  window.addEventListener('hashchange', close);
  rulesModal = { close };
  show(0);
  document.body.append(overlay);
}

// ------------------------------------------------------------ 顶栏入口

function renderHelp() {
  if (!helpEl) return;
  fill(helpEl,
    pageTour ? h('button', { class: 'help-btn', id: 'help-tour', title: '再看一遍这个页面的操作说明', onclick: runPageTour }, '🧭 界面导览') : null,
    h('button', { class: 'help-btn', id: 'help-rules', title: '规则、能量豆、8 只宠物速查', onclick: openRules }, '📖 玩法速览'));
}

export function mountHelp(el) {
  helpEl = el;
  renderHelp();
}

// 战场：上方对手、下方我方。只负责把服务端给的局面快照画出来，并收集玩家的点击。
// 实时对战和回放共用；回放时 live=false，不显示操作面板。

import { app } from '../app.js';
import { clear, fill, fmt, h } from '../dom.js';

const SIDES = ['opp', 'me'];
const enemyOf = (side) => (side === 'me' ? 'opp' : 'me');

const LOG_CLASS = {
  round_start: 'log-round',
  action: 'log-action',
  damage: 'log-damage',
  heal: 'log-heal',
  shield: 'log-shield',
  ko: 'log-ko',
  frozen: 'log-frozen',
  battle_end: 'log-end',
};

// mark：AI Coach 对这次行动的标注（建议了什么、玩家是否采纳），写在同一行里，不另起一行
export function logLine(event, mark) {
  const line = h('div', { class: `log-line ${LOG_CLASS[event.type] || ''}`, dataset: { i: event.i } }, event.text);
  if (mark) {
    line.append(h('span', {
      class: `coach-mark ${mark.adopted ? 'adopted' : 'ignored'}`,
      title: mark.adopted ? '这一步采纳了 AI 的建议' : '这一步 AI 给了别的建议，玩家没有采纳',
    }, mark.adopted ? 'AI 建议 · 已采纳' : 'AI 建议 · 未采纳'));
  }
  return line;
}

// 标注按「第几次行动」保存；日志按事件画。这里换算成 事件序号 → 标注
export function marksByEvent(events, marks) {
  const map = new Map();
  if (!marks) return map;
  let index = 0;
  for (const event of events) {
    if (event.type !== 'action') continue;
    if (marks[index]) map.set(event.i, marks[index]);
    index += 1;
  }
  return map;
}

export function createArena({ statics, live = false, onAct = null, tools = null, opponent = 'manual' }) {
  const config = app.state.config;
  const info = statics.pets;
  const pets = {};
  const sides = {};
  let view = null;
  let options = null; // 当前行动宠物可用的动作；null 表示暂时不能操作
  let selected = null; // 已选中、正在等待点目标的动作
  let advice = null; // AI 建议的动作 { skill, target }：只用来高亮，不会替玩家点
  let marks = new Map(); // 事件序号 → 这次行动的采纳标注

  // ------------------------------------------------------------ 搭建 DOM

  function buildPet(pet) {
    const refs = {
      hpFill: h('div', { class: 'hp-fill' }),
      hpText: h('span', { class: 'hp-text' }),
      shieldFill: h('div', { class: 'shield-fill' }),
      shieldText: h('span', { class: 'shield-text' }),
      badges: h('div', { class: 'badges' }),
      fx: h('div', { class: 'fx' }),
    };
    refs.shield = h('div', { class: 'shield-bar', hidden: true }, refs.shieldFill, refs.shieldText);
    const species = app.species(pet.species);
    refs.root = h(
      'div',
      {
        class: `bpet side-${pet.side}`,
        dataset: { pet: pet.id, species: pet.species },
        // 悬停查看技能说明
        title: `${species.name} · ${species.role}\n【${species.skill.name}】${species.skill.desc}\n【${species.passive.name}】${species.passive.desc}`,
        onclick: () => clickPet(pet.id),
      },
      h('div', { class: 'bpet-top' },
        h('span', { class: 'bpet-slot' }, `${pet.slot} 号位`),
        refs.badges),
      h('div', { class: 'bpet-body' },
        h('div', { class: 'avatar' }, pet.emoji, h('div', { class: 'ice-cover' }, '❄')),
        h('div', { class: 'bpet-info' },
          h('div', { class: 'bpet-name' }, pet.name),
          h('div', { class: 'hp-bar' }, refs.hpFill, refs.hpText),
          refs.shield,
          h('div', { class: 'bpet-stats' },
            h('span', { title: '攻击' }, `攻 ${pet.atk}`),
            h('span', { title: `最终速度（基础 ${pet.base_speed}）` }, `速 ${pet.speed}`),
            h('span', { title: '效果命中' }, `命 ${pet.hit}%`)))),
      refs.fx,
    );
    pets[pet.id] = refs;
    return refs.root;
  }

  function buildSide(side) {
    const refs = {
      beans: h('div', { class: 'beans' }),
      effects: h('div', { class: 'effects' }),
      panel: h('div', { class: 'action-panel', dataset: { side } }),
    };
    const head = h('div', { class: 'side-head' },
      h('span', { class: 'side-name' }, app.sideName(side),
        side === 'opp' && opponent === 'random' ? h('span', { class: 'ai-tag', title: '电脑随机操作' }, '🎲 随机') : null,
        side === 'opp' && opponent === 'smart' ? h('span', { class: 'ai-tag', title: '电脑智能操作' }, '🧠 智能') : null),
      refs.beans, refs.effects);
    const row = h('div', { class: 'pets-row' },
      [1, 2, 3].map((slot) => buildPet(info[`${side}${slot}`])));
    // 两个操作面板都靠近中线：对手方在宠物下面，我方在宠物上面
    const parts = side === 'opp' ? [head, row, refs.panel] : [refs.panel, row, head];
    refs.root = h('div', { class: `side side-${side}`, dataset: { side } }, parts);
    sides[side] = refs;
    return refs.root;
  }

  const roundEl = h('div', { class: 'round' });
  const queueEl = h('div', { class: 'queue' });
  const logEl = h('div', { class: 'log', role: 'log' });
  const logWrap = h('div', { class: 'log-wrap' }, h('div', { class: 'log-title' }, '战斗日志'), logEl);
  const el = h('section', { class: `arena ${live ? 'live' : 'replay'}` },
    h('div', { class: 'arena-bar' }, roundEl, queueEl, h('div', { class: 'arena-tools' }, tools)),
    buildSide('opp'),
    buildSide('me'),
    logWrap);

  // ---------------------------------------------------------------- 绘制

  function chip(id, state) {
    const pet = info[id];
    return h('span', {
      class: `qchip side-${pet.side} ${state}`,
      title: `${app.sideName(pet.side)}${pet.name}`,
      dataset: { pet: id },
    }, pet.emoji);
  }

  function badge(text, kind, title) {
    return h('span', { class: `badge ${kind}`, title }, text);
  }

  function render(next) {
    view = next;
    roundEl.textContent = `第 ${Math.max(view.round, 1)} / ${statics.max_rounds} 回合`;

    fill(queueEl,
      h('span', { class: 'queue-label' }, '行动队列'),
      ...view.done.map((id) => chip(id, 'done')),
      view.current ? chip(view.current, 'current') : null,
      ...view.upcoming.map((id) => chip(id, 'upcoming')),
    );

    for (const [id, state] of Object.entries(view.pets)) {
      const refs = pets[id];
      const max = info[id].max_hp;
      const ratio = Math.max(0, Math.min(1, state.hp / max));
      refs.hpFill.style.width = `${ratio * 100}%`;
      refs.hpFill.classList.toggle('low', ratio <= 0.3);
      refs.hpText.textContent = `${fmt(state.hp)} / ${fmt(max)}`;
      refs.shield.hidden = state.shield <= 0;
      refs.shieldFill.style.width = `${Math.min(1, state.shield / max) * 100}%`;
      refs.shieldText.textContent = `护盾 ${fmt(state.shield)}`;
      refs.root.classList.toggle('dead', !state.alive);
      refs.root.classList.toggle('frozen', state.frozen);
      refs.root.classList.toggle('linked', state.linked);
      refs.root.classList.toggle('current', view.current === id);
      fill(refs.badges,
        state.linked ? badge('🔗 水链', 'chain', '同心水链连接中：伤害减免 5% 后由连接宠物均分') : null,
        state.frozen ? badge('❄ 冻结', 'ice', '被冻结，跳过本次操作') : null,
        !state.alive ? badge('已倒下', 'down') : null,
      );
    }

    for (const side of SIDES) {
      const team = view.teams[side];
      const refs = sides[side];
      fill(refs.beans,
        h('span', { class: 'beans-label' }, '能量豆'),
        Array.from({ length: statics.energy_cap }, (_, i) =>
          h('i', { class: `bean${i < team.beans ? ' on' : ''}` })),
        h('span', { class: 'beans-count' }, `${team.beans} / ${statics.energy_cap}`),
      );
      fill(refs.effects,
        team.barrier ? badge(
          `❄ ${app.sideName(side)}寒毒结界 · 剩余 ${team.barrier.remaining} 回合`, 'ice',
          `作用于${app.sideName(enemyOf(side))}：每次行动机会开始前受到 ${fmt(team.barrier.atk * 0.3)} 点伤害，冻结概率 ${team.barrier.hit}%`,
        ) : null,
        team.chain ? badge(
          `🔗 同心水链 · 剩余 ${team.chain.remaining} 回合`, 'chain',
          `连接 ${team.chain.members.length} 只宠物：伤害减免 5% 后均分`,
        ) : null,
      );
      refs.root.classList.toggle('under-barrier', !!view.teams[enemyOf(side)].barrier);
      refs.root.classList.toggle('active', !!view.current && info[view.current].side === side);
    }
    renderPanels();
  }

  function renderPanels() {
    const actingSide = view?.current ? info[view.current].side : null;
    const canAct = live && options && view?.awaiting;
    for (const side of SIDES) {
      const panel = clear(sides[side].panel);
      if (!live) continue;
      if (!canAct || side !== actingSide) {
        let text = '';
        if (view?.phase === 'finished') text = '战斗已结束';
        else if (!canAct) text = '结算中…';
        else text = `等待${app.sideName(actingSide)}操作`;
        panel.append(h('span', { class: 'panel-idle' }, text));
        continue;
      }
      const actor = info[view.current];
      const skillDesc = app.species(actor.species).skill.desc;
      fill(panel,
        h('span', { class: 'panel-actor' }, `轮到 ${actor.emoji} ${actor.name}：`),
        options.map((option) => [
          h('button', {
            class: `btn skill-btn ${option.skill}${selected === option ? ' selected' : ''}${
              side === 'me' && advice && advice.skill === option.skill && option.enabled ? ' coach-rec' : ''}`,
            disabled: !option.enabled,
            title: option.skill === 'basic' ? config.basic.desc : skillDesc,
            dataset: { skill: option.skill },
            onclick: () => choose(option),
          },
          option.name,
          option.skill === 'special'
            ? h('span', { class: 'cost' }, option.cost ? `${option.cost} 豆` : '免费') : null),
          option.enabled ? null : h('span', { class: 'reason' }, option.reason),
        ]),
        h('span', { class: 'panel-hint' },
          selected ? '点击高亮的宠物选择目标（再点一次技能可取消）' : ''),
        // AI 建议也写在操作栏里：玩家的视线就在这儿，不用去右下角找
        side === 'me' && advice
          ? h('span', { class: 'panel-advice', id: 'panel-advice', title: advice.reason || '' },
            h('b', {}, '🧠 AI 推荐'), advice.text) : null,
      );
    }
    const advised = canAct && actingSide === 'me' && advice ? advice.target : null;
    for (const [id, refs] of Object.entries(pets)) {
      refs.root.classList.toggle('targetable', !!selected && selected.targets.includes(id));
      refs.root.classList.toggle('coach-target', advised === id);
    }
  }

  // ---------------------------------------------------------------- 操作

  function choose(option) {
    if (!option.enabled) return;
    if (option.targets === null) return fire(option.skill, null); // 无需选目标，直接释放
    selected = selected === option ? null : option;
    renderPanels();
  }

  function clickPet(id) {
    if (selected && selected.targets.includes(id)) fire(selected.skill, id);
  }

  function fire(skill, target) {
    selected = null;
    options = null;
    renderPanels();
    onAct(skill, target);
  }

  // ---------------------------------------------------------------- 特效

  function float(id, text, kind) {
    const layer = pets[id].fx;
    const node = h('span', { class: `float ${kind}` }, text);
    node.style.setProperty('--i', layer.childElementCount); // 同时出现的多个数字错开显示
    layer.append(node);
    node.addEventListener('animationend', () => node.remove());
  }

  function pulse(id, name, ms = 400) {
    const root = pets[id].root;
    root.classList.remove(name);
    void root.offsetWidth; // 重新触发动画
    root.classList.add(name);
    setTimeout(() => root.classList.remove(name), ms);
  }

  function fx(event) {
    if (event.type === 'damage') {
      for (const part of event.parts) {
        if (part.absorbed) float(part.pet, `护盾 -${fmt(part.absorbed)}`, 'shield');
        if (part.lost) float(part.pet, `-${fmt(part.lost)}`, 'damage');
        if (part.absorbed || part.lost) pulse(part.pet, 'hit');
      }
    } else if (event.type === 'hp_cost') {
      float(event.pet, `-${fmt(event.amount)}`, 'damage');
    } else if (event.type === 'heal') {
      if (event.actual) float(event.pet, `+${fmt(event.actual)}`, 'heal');
    } else if (event.type === 'shield') {
      if (event.replaced) float(event.pet, `护盾 ${fmt(event.amount)}`, 'shield');
    } else if (event.type === 'action') {
      float(event.pet, event.name, 'skill');
      pulse(event.pet, 'acting', 500);
    } else if (event.type === 'frozen') {
      float(event.pet, '冻结', 'ice');
    } else if (event.type === 'pursuit_check' && event.success) {
      float(event.pet, '协同补刀', 'skill');
      pulse(event.pet, 'acting', 500);
    }
  }

  // ---------------------------------------------------------------- 日志

  const log = {
    append(event) {
      if (!event.text) return;
      logEl.append(logLine(event, marks.get(event.i)));
      logEl.scrollTop = logEl.scrollHeight;
    },
    reset(events) {
      fill(logEl, events.filter((e) => e.text).map((event) => logLine(event, marks.get(event.i))));
      logEl.scrollTop = logEl.scrollHeight;
    },
  };

  return {
    el,
    render,
    fx,
    log,
    // 开放操作（传入动作列表）或锁定操作（传入 null）
    setOptions(next) {
      options = next && next.length ? next : null;
      selected = null;
      renderPanels();
    },
    // 高亮 AI 建议的技能按钮和目标（传 null 取消）。面板每个事件都会重画，所以高亮在重画时加上
    setAdvice(next) {
      advice = next;
      renderPanels();
    },
    // 采纳标注：事件序号 → { advised, adopted }
    setMarks(next) {
      marks = next;
    },
    addMark(eventIndex, mark) {
      marks.set(eventIndex, mark);
    },
    // 把一个侧栏（AI 指导面板）放到战斗日志右边，两者等高
    attach(aside) {
      if (logWrap.parentElement !== el) return;
      const row = h('div', { class: 'log-row' });
      logWrap.replaceWith(row);
      row.append(logWrap, aside);
    },
  };
}

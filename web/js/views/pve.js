// PVE 属性历练：选我方宠物、副本和模式，按概率判定成败（对手方属性固定，不能历练）。
// 每次挑战带一个唯一的 challenge_id，由服务端一次性完成判定和结算；
// 重新打开结果只是按 id 读取，不会重新抽取。

import { api } from '../api.js';
import { app } from '../app.js';
import { fill, formatTime, h, sleep, uid } from '../dom.js';
import { createGrowthCard } from '../components/growthCard.js';

const STORE_KEY = 'petarena.pve';
const PACE = 650; // 每次挑战的最短用时（毫秒），让玩家来得及停止或切换模式
const MAX_COUNT = 999;

export async function mount(root) {
  const config = app.state.config;
  const dungeons = Object.fromEntries(config.dungeons.map((d) => [d.key, d]));
  const modes = Object.fromEntries(config.modes.map((m) => [m.key, m])); // 含已关闭的模式：旧记录里还有
  const usable = config.modes.filter((m) => m.enabled); // 现在能选的模式（本版只有手动模式）

  let saved = {};
  try {
    saved = JSON.parse(sessionStorage.getItem(STORE_KEY)) || {};
  } catch {
    // 忽略损坏的本地记录
  }
  const SIDE = 'me';
  const sel = {
    species: app.species(saved.species) ? saved.species : 'cat',
    dungeon: saved.dungeon in dungeons ? saved.dungeon : 'atk',
    mode: usable.some((m) => m.key === saved.mode) ? saved.mode : usable[0].key,
    count: Number.isInteger(saved.count) ? saved.count : 10,
  };

  const records = await api.pveRecords(100);
  let result = null; // 当前展示的结果
  if (saved.last) {
    try {
      result = await api.pveRecord(saved.last); // 重新打开结果页：只读取，不重抽
    } catch {
      // 记录不存在就不展示
    }
  }

  let busy = false; // 有一次挑战正在进行
  let auto = null; // 自动挑战进度 { done, wins, total }
  let stopRequested = false;
  let status = '';
  let alive = true;

  const page = h('section', { class: 'pve' });
  root.append(page);

  // AI 培养建议：启用时放在页面最上面；未启用时页面与没有这个功能时完全一致
  let growthCard = null;
  api.coachStatus().then((coachStatus) => {
    if (!alive || !coachStatus.enabled) return;
    growthCard = createGrowthCard({
      // 只是帮玩家选好宠物和副本；挑战仍然要自己点
      onPick: (item) => { choose('species', item.pet); choose('dungeon', item.dungeon); },
    });
    render();
    growthCard.load();
  }).catch(() => {}); // 查询失败就当作未启用

  const persist = () =>
    sessionStorage.setItem(STORE_KEY, JSON.stringify({ ...sel, last: result?.record.id }));
  const stats = () => app.state.roster[SIDE][sel.species];
  const capped = () => sel.dungeon === 'hit' && stats().hit >= config.rules.hit_cap;
  const countValid = () => Number.isInteger(sel.count) && sel.count >= 1 && sel.count <= MAX_COUNT;
  const running = () => busy || auto !== null;

  // ---------------------------------------------------------------- 挑战

  async function send(body) {
    for (let attempt = 0; ; attempt += 1) {
      try {
        return await api.challenge(body);
      } catch (error) {
        if (error.status || attempt >= 2) throw error;
        await sleep(400); // 网络抖动：用同一个 challenge_id 重试，不会重复结算
      }
    }
  }

  // 结算一次挑战。mode 在开始时就确定，中途切换模式不影响这一次。
  async function settleOne(mode) {
    const body = { challenge_id: uid(), side: SIDE, species: sel.species, dungeon: sel.dungeon, mode };
    busy = true;
    render();
    try {
      const [res] = await Promise.all([send(body), sleep(PACE)]);
      app.state.roster[body.side][body.species] = res.stats;
      result = res;
      records.unshift(res.record);
      persist();
      return res;
    } finally {
      busy = false;
    }
  }

  async function runManual() {
    if (running() || app.locked) return;
    status = '';
    try {
      await settleOne('manual');
    } catch (error) {
      app.toast(error.message, 'error');
    }
    if (alive) render();
  }

  async function runAuto() {
    if (running() || app.locked || !countValid()) return;
    auto = { done: 0, wins: 0, total: sel.count };
    stopRequested = false;
    status = '';
    let reason = '已达到指定次数';
    try {
      while (auto.done < auto.total) {
        if (!alive) return;
        if (stopRequested) { reason = '玩家停止'; break; }
        // 模式切换从下一次挑战开始生效：切回手动后不再自动发起
        if (sel.mode !== 'auto') { reason = '已切换为手动模式'; break; }
        if (capped()) { reason = `效果命中已达到上限 ${config.rules.hit_cap}%`; break; }
        const res = await settleOne('auto');
        auto.done += 1;
        if (res.record.success) auto.wins += 1;
      }
    } catch (error) {
      reason = error.message;
      app.toast(error.message, 'error');
    }
    status = `自动挑战结束（${reason}）：完成 ${auto.done} / ${auto.total} 次，成功 ${auto.wins} 次`;
    auto = null;
    if (alive) render();
  }

  function stop() {
    stopRequested = true;
    render();
  }

  async function reopen(id) {
    if (running()) return;
    try {
      result = await api.pveRecord(id);
      persist();
      render();
    } catch (error) {
      app.toast(error.message, 'error');
    }
  }

  function choose(key, value) {
    if (key !== 'mode' && running()) return; // 进行中只能切换模式或停止
    sel[key] = value;
    persist();
    render();
  }

  // ---------------------------------------------------------------- 绘制

  function describe(record) {
    const species = app.species(record.species);
    return `${species.emoji}${species.name} · ${dungeons[record.dungeon].name} · ${modes[record.mode].name}`;
  }

  function change(record) {
    const name = config.stats[record.stat];
    const unit = record.stat === 'hit' ? '%' : '';
    return record.success
      ? `${name} ${record.before}${unit} → ${record.after}${unit}`
      : `${name} ${record.before}${unit}（未变化）`;
  }

  function petGrid(disabled) {
    const stat = dungeons[sel.dungeon].stat;
    const cell = (key, label, value) =>
      h('span', { class: `mini-stat${stat === key ? ' focus' : ''}` }, h('i', {}, label), h('b', {}, value));
    return h('div', { class: 'pve-pets', id: 'pve-pets' },
      config.species.map((species) => {
        const s = app.state.roster[SIDE][species.key];
        return h('button', {
          class: `pve-pet side-${SIDE}${sel.species === species.key ? ' on' : ''}`,
          disabled, dataset: { species: species.key }, onclick: () => choose('species', species.key),
        },
        h('span', { class: 'pve-pet-emoji' }, species.emoji),
        h('span', { class: 'pve-pet-name' }, species.name),
        h('span', { class: 'mini-stats' },
          cell('max_hp', '生命', s.max_hp), cell('atk', '攻击', s.atk),
          cell('speed', '速度', s.speed), cell('hit', '命中', `${s.hit}%`)));
      }));
  }

  function dungeonRow(disabled) {
    return h('div', { class: 'pve-dungeons', id: 'pve-dungeons' },
      config.dungeons.map((dungeon) =>
        h('button', {
          class: `dungeon${sel.dungeon === dungeon.key ? ' on' : ''}`,
          disabled, dataset: { dungeon: dungeon.key }, onclick: () => choose('dungeon', dungeon.key),
        }, h('b', {}, dungeon.name), h('span', {}, dungeon.effect))));
  }

  function modeRow() {
    if (usable.length === 1) { // 只剩一种模式：直接写出来，不用选
      const only = usable[0];
      return h('span', { class: 'mode-fixed', id: 'pve-modes', dataset: { mode: only.key } },
        `${only.name} · 通关概率 ${only.chance}%`);
    }
    return h('div', { class: 'seg', id: 'pve-modes' },
      usable.map((mode) =>
        h('button', {
          class: `seg-btn${sel.mode === mode.key ? ' on' : ''}`,
          disabled: app.locked, dataset: { mode: mode.key }, onclick: () => choose('mode', mode.key),
        }, `${mode.name} · 通关概率 ${mode.chance}%`)));
  }

  function actionRow() {
    if (auto) {
      const current = Math.min(auto.done + 1, auto.total);
      return h('div', { class: 'pve-actions' },
        h('span', { class: 'pve-progress', id: 'pve-progress' },
          `自动挑战中：第 ${current} / ${auto.total} 次（已成功 ${auto.wins} 次）`),
        h('button', { class: 'btn danger', id: 'pve-stop', disabled: stopRequested, onclick: stop },
          stopRequested ? '本次结算后停止…' : '停止'),
        sel.mode !== 'auto'
          ? h('span', { class: 'muted' }, '已切换为手动模式，本次结算后停止自动挑战') : null);
    }
    const blocked = app.locked ? '选将和战斗期间不能进行 PVE'
      : capped() ? `效果命中已达到上限 ${config.rules.hit_cap}%` : '';
    if (sel.mode === 'manual') {
      return h('div', { class: 'pve-actions' },
        h('button', {
          class: 'btn primary big', id: 'pve-go', disabled: busy || !!blocked, onclick: runManual,
        }, busy ? '挑战中…' : '挑战一次'),
        h('span', { class: 'muted' }, blocked));
    }
    const go = h('button', {
      class: 'btn primary big', id: 'pve-go',
      disabled: busy || !!blocked || !countValid(), onclick: runAuto,
    }, busy ? '挑战中…' : '开始自动挑战');
    const input = h('input', {
      type: 'number', id: 'pve-count', min: 1, max: MAX_COUNT, step: 1, value: sel.count,
      disabled: busy || app.locked,
      oninput: (event) => {
        sel.count = Number(event.target.value);
        go.disabled = busy || !!blocked || !countValid();
        if (countValid()) persist();
      },
    });
    return h('div', { class: 'pve-actions' },
      h('label', { class: 'count-label' }, '最大次数', input),
      go,
      h('span', { class: 'muted' }, blocked || `连续挑战 1–${MAX_COUNT} 次；达到次数、手动停止或属性封顶时结束`));
  }

  function resultCard() {
    if (busy && !auto) {
      return h('div', { class: 'pve-result pending', id: 'pve-result' }, '⏳ 挑战中…');
    }
    if (!result) {
      return h('div', { class: 'pve-result none', id: 'pve-result' }, '还没有挑战结果');
    }
    const { record } = result;
    return h('div', {
      class: `pve-result ${record.success ? 'win' : 'lose'}`, id: 'pve-result',
      dataset: { id: record.id, success: record.success },
    },
    h('div', { class: 'pve-result-title' }, record.success ? '✅ 挑战成功' : '❌ 挑战失败'),
    h('div', {}, describe(record), `（通关概率 ${record.chance}%）`),
    h('div', { class: 'pve-result-change' }, change(record)),
    h('div', { class: 'muted' }, formatTime(record.time)));
  }

  function recordTable() {
    if (!records.length) return h('div', { class: 'empty' }, '还没有 PVE 记录');
    return h('table', { class: 'table', id: 'pve-table' },
      h('thead', {}, h('tr', {},
        ['时间', '宠物', '副本', '模式', '结果', '属性变化'].map((text) => h('th', {}, text)))),
      h('tbody', {}, records.slice(0, 100).map((record) => {
        const species = app.species(record.species);
        return h('tr', {
          class: `clickable${result?.record.id === record.id ? ' on' : ''}`,
          title: '点击重新打开这次挑战的结果',
          dataset: { id: record.id }, onclick: () => reopen(record.id),
        },
        h('td', {}, formatTime(record.time)),
        h('td', {}, `${species.emoji} ${species.name}`),
        h('td', {}, dungeons[record.dungeon].name),
        h('td', {}, modes[record.mode].name),
        h('td', {}, h('span', { class: `result-tag ${record.success ? 'win' : 'lose'}` }, record.success ? '成功' : '失败')),
        h('td', {}, change(record)));
      })));
  }

  function render() {
    const disabled = running() || app.locked;
    if (growthCard) growthCard.refresh(); // 属性可能刚变：路线上的进度跟着更新
    fill(page,
      growthCard ? growthCard.el : null,
      h('div', { class: 'panel' },
        h('h2', {}, '🏕️ PVE 属性历练'),
        app.locked
          ? h('div', { class: 'notice', id: 'pve-locked' },
            '选将和战斗期间属性锁定，不能进行 PVE。',
            h('a', { href: '#/pvp' }, '回到对局'))
          : h('p', { class: 'muted' }, `培养我方宠物来对抗更强的对手方。PVE 不进行实际战斗，每次挑战${usable.length === 1 ? `做一次概率判定（通关概率 ${usable[0].chance}%）` : '按所选模式做一次概率判定'}；失败不增加属性。`),
        h('div', { class: 'field' }, h('span', { class: 'field-label' }, '① 我方宠物'), petGrid(disabled)),
        h('div', { class: 'field' }, h('span', { class: 'field-label' }, '② 副本'), dungeonRow(disabled)),
        h('div', { class: 'field' }, h('span', { class: 'field-label' }, '③ 模式'), modeRow()),
        actionRow(),
        status ? h('div', { class: 'pve-status', id: 'pve-status' }, status) : null),
      h('div', { class: 'pve-bottom' },
        h('div', { class: 'panel' }, h('h3', {}, '挑战结果'), resultCard()),
        h('div', { class: 'panel grow' }, h('h3', {}, 'PVE 记录'), recordTable())),
    );
  }

  render();
  return () => {
    alive = false;
    if (growthCard) growthCard.dispose();
  };
}

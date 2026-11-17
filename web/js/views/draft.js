// 选将：上方对手方宠物池，下方我方宠物池，中间是双方各 3 个站位槽。
// 当前选择方把宠物拖到自己的下一个空槽，点「确认」后锁定；确认前可以拖回或换一只。
// 轮到我方时，AI 军师面板给出本手推荐并高亮对应的卡片；它只给建议，不替玩家选。

import { api } from '../api.js';
import { app, SIDE_STYLE } from '../app.js';
import { createDraftCoach } from '../coach.js';
import { createCoachPanel } from '../components/coachPanel.js';
import { fill, h } from '../dom.js';
import { confirmDialog } from '../components/modal.js';
import { petCard, petChip } from '../components/petCard.js';
import { clearPageTour, setPageTour } from '../guide.js';

export function mountDraft(root, onPhaseChange) {
  let pending = null; // 已拖进槽位、尚未确认的宠物
  let dragging = null; // 'pool' | 'slot'
  let mountLater = false; // AI 军师的面板要等当前这次拖拽结束再画出来
  let busy = false;
  let unmounted = false;
  let coach = null; // AI 军师：未启用时保持 null，页面与没有这个功能时完全一致
  let coachPanel = null;

  const page = h('section', { class: 'draft' });
  root.append(page);

  // ---- AI 军师 ----

  const draftToken = () => `${app.state.match.id}:draft:${app.state.match.draft.step}`;

  // 当前这一手的推荐宠物；建议不属于当前局面时为 null
  function advisedPet() {
    const state = coach?.state;
    const match = app.state.match;
    if (!state || state.status !== 'ready' || !match || match.phase !== 'draft') return null;
    if (match.draft.turn !== 'me' || state.token !== draftToken()) return null;
    return state.result.advice.pet;
  }

  // 高亮推荐的卡片。直接改现有节点而不是整页重绘，免得打断正在进行的拖拽
  function highlight() {
    page.querySelectorAll('.pet-card.coach-pick').forEach((card) => {
      card.classList.remove('coach-pick');
      card.querySelector('.coach-badge')?.remove();
    });
    const key = advisedPet();
    if (!key) return;
    const card = page.querySelector(`.pool[data-side=me] .pet-card[data-species=${key}]`);
    if (card && !card.classList.contains('picked')) {
      card.classList.add('coach-pick');
      card.append(h('span', { class: 'coach-badge' }, 'AI 推荐'));
    }
  }

  function renderCoach() {
    if (!coachPanel) return;
    const turn = app.state.match?.draft.turn;
    coachPanel.render(coach.state, turn === 'opp' ? '对手方选择中 · AI 只为我方提供建议' : null);
    highlight();
  }

  // 只在两个时机请求建议：进入页面时，以及一次选择确认成功之后（不能放在 render 里，拖拽会频繁重绘）
  function requestAdvice() {
    if (!coach || unmounted) return;
    const match = app.state.match;
    if (match && match.phase === 'draft' && match.draft.turn === 'me') coach.request(draftToken());
    else coach.cancel();
  }

  // 界面导览：第一次进入选将时自动走一遍（等知道 AI 军师有没有启用之后，好决定讲不讲它）
  function tourSteps() {
    const computer = app.state.match.opponent !== 'manual';
    return [
      { target: '.draft-bar', title: '选将顺序',
        text: '对手方先选，然后一人一只轮流，共 6 手。右边的圆点是进度：「对」是对手方，「我」是我方，带圈的是现在这一手。' },
      { target: '.pool[data-side=opp]', title: '上方：对手方宠物池',
        text: computer
          ? '对手方由电脑操作，轮到它时会立刻选好，并在下方提示选了谁。它的宠物生命更高、整体更强，但速度偏慢。鼠标停在卡片上可以看完整技能。'
          : '同屏手动：轮到对手方时，由另一位玩家从这里选。它的宠物生命更高、整体更强，但速度偏慢。' },
      { target: '.board', title: '中间：双方站位槽',
        text: '选的顺序就是站位：第 1 只是 1 号位。对手选了谁会马上显示在这里，后选的你可以针对它搭配。' },
      { target: '#coach-panel', title: '🧠 AI 军师',
        text: ['轮到你选时，这里给出本手推荐、理由和推演胜率；推荐的卡片带紫色',
          h('b', { class: 'tour-ai' }, 'AI 推荐'), '角标。第一次玩，照着选就好。'] },
      { target: '.pool[data-side=me]', title: '下方：我方宠物池',
        text: '把一只宠物拖到高亮的站位槽（或者直接点一下卡片）。放错了可以拖回来，或点槽里的 ✕。' },
      { target: '#confirm-pick', title: '确认',
        text: '点「确认」锁定这一手，确认后不能撤回。6 手选完自动进入战斗。' },
    ];
  }
  const registerTour = () => { if (!unmounted && app.state.phase === 'draft') setPageTour('draft', tourSteps); };

  api.coachStatus().then((status) => {
    if (unmounted || !status.enabled || app.state.phase !== 'draft') { registerTour(); return; }
    coachPanel = createCoachPanel({ onRetry: () => coach.retry() });
    coach = createDraftCoach({ onChange: renderCoach });
    // 整页重绘会移除正在拖动的卡片，那次拖拽就落空了：正在拖的话，等它放下或取消之后再把面板画出来
    if (dragging) mountLater = true;
    else render();
    requestAdvice();
    registerTour();
  }).catch(registerTour); // 查询失败就当作未启用

  // 拖拽结束（没有放进槽位）：补上拖拽期间推迟的重绘
  function settle() {
    if (!mountLater || unmounted) return;
    mountLater = false;
    render();
  }

  function place(species) {
    pending = species;
    // 重绘会移除被拖动的节点，它的 dragend 可能不再触发，这里手动收尾
    dragging = null;
    mountLater = false; // 下面这次重绘会把面板一起画出来
    page.classList.remove('dragging');
    render();
  }

  async function confirmPick() {
    if (!pending || busy) return;
    busy = true;
    const { turn, picks: before } = app.state.match.draft;
    coach?.cancel(); // 玩家已经做出选择：进行中的请求作废，旧建议不再显示
    try {
      app.set(await api.pick(turn, pending));
      pending = null;
      busy = false;
      announceAiPicks(before.length);
      if (app.state.phase !== 'draft') return onPhaseChange(); // 6 次选完，进入战斗
      render();
      requestAdvice();
    } catch (error) {
      busy = false;
      app.toast(error.message, 'error');
      render();
      requestAdvice(); // 这一手还没选成：把建议要回来（服务端有缓存）
    }
  }

  async function abandon() {
    if (!(await confirmDialog('放弃本局？', '放弃后本次选将作废，不会计入战斗记录。', '放弃本局'))) return;
    coach?.cancel();
    try {
      app.set(await api.abandon());
      app.go('#/');
    } catch (error) {
      app.toast(error.message, 'error');
      requestAdvice();
    }
  }

  function statusBar(draft) {
    const steps = draft.order.map((side, index) => {
      const state = index < draft.step ? 'done' : index === draft.step ? 'now' : '';
      return h('span', { class: `step side-${side} ${state}`, title: `第 ${index + 1} 次：${app.sideName(side)}` },
        side === 'opp' ? '对' : '我');
    });
    return h('div', { class: 'draft-bar' },
      h('div', { class: 'draft-title' },
        h('b', {}, `选将 · 第 ${draft.step + 1} / ${draft.order.length} 次`),
        h('span', { class: `turn-tag side-${draft.turn}` }, `轮到${app.sideName(draft.turn)}选择`)),
      h('div', { class: 'steps' }, steps),
      h('button', { class: 'btn ghost', id: 'abandon', onclick: abandon }, '放弃本局'));
  }

  const isAi = (side) => app.state.match.opponent !== 'manual' && side === 'opp';
  const smart = () => app.state.match.opponent === 'smart';

  // 提示电脑刚刚替对手方选了什么
  function announceAiPicks(from) {
    const picks = app.state.match.draft.picks.slice(from).filter(([side]) => isAi(side));
    if (picks.length) {
      const names = picks.map(([, key]) => app.species(key).name).join('、');
      app.toast(smart() ? `对手方选择了 ${names}` : `对手方随机选择了 ${names}`);
    }
  }

  function pool(side, match) {
    const draft = match.draft;
    const active = draft.turn === side && !isAi(side);
    const taken = draft.lineups[side];
    const cards = app.state.config.species.map(({ key }) => {
      const picked = taken.includes(key);
      const placed = active && pending === key;
      const canDrag = active && !picked && !busy;
      const state = picked ? 'picked' : placed ? 'placed' : canDrag ? 'available' : '';
      return petCard(side, key, match.roster[side][key], {
        class: state,
        pop: side === 'opp' ? 'down' : 'up',
        attrs: {
          draggable: canDrag ? 'true' : null,
          ondragstart: canDrag ? (event) => {
            dragging = 'pool';
            event.dataTransfer.setData('text/plain', key);
            event.dataTransfer.effectAllowed = 'move';
            page.classList.add('dragging');
          } : null,
          ondragend: () => { dragging = null; page.classList.remove('dragging'); settle(); },
          // 也可以直接点击卡片放入槽位
          onclick: canDrag ? () => place(key) : null,
        },
        extra: picked ? h('div', { class: 'card-mask' }, '已选') : null,
      });
    });
    const el = h('div', {
      class: `pool side-${side} ${active ? 'active' : 'locked'}`,
      dataset: { side },
      // 把槽位里未确认的宠物拖回宠物池
      ondragover: (event) => { if (active && dragging === 'slot') event.preventDefault(); },
      ondrop: (event) => {
        if (active && dragging === 'slot') { event.preventDefault(); place(null); }
      },
    },
    h('div', { class: 'pool-title' },
      h('b', {}, `${app.sideName(side)}宠物池`),
      h('span', { class: 'muted' }, SIDE_STYLE[side]),
      h('span', { class: 'pool-state' },
        isAi(side) ? (smart() ? '🧠 电脑智能选择' : '🎲 电脑随机选择')
          : active ? '请拖拽一只宠物到站位槽' : '等待对方选择')),
    h('div', { class: 'pool-cards' }, cards));
    return el;
  }

  function slots(side, draft) {
    const lineup = draft.lineups[side];
    const cells = [];
    for (let index = 0; index < draft.team_size; index += 1) {
      const number = h('span', { class: 'slot-no' }, `${index + 1} 号位`);
      if (index < lineup.length) {
        cells.push(h('div', { class: `slot filled side-${side}`, dataset: { slot: index + 1 } },
          number, petChip(side, lineup[index]), h('span', { class: 'lock' }, '🔒 已锁定')));
        continue;
      }
      const isTarget = draft.turn === side && index === lineup.length;
      if (!isTarget) {
        cells.push(h('div', { class: `slot vacant side-${side}`, dataset: { slot: index + 1 } }, number));
        continue;
      }
      // 当前选择方的下一个空槽：拖放目标
      const cell = h('div', {
        class: `slot target side-${side}${pending ? ' has-pending' : ''}`,
        dataset: { slot: index + 1 },
        id: 'drop-slot',
        ondragover: (event) => {
          if (dragging !== 'pool') return;
          event.preventDefault();
          event.dataTransfer.dropEffect = 'move';
          cell.classList.add('over');
        },
        ondragleave: () => cell.classList.remove('over'),
        ondrop: (event) => {
          if (dragging !== 'pool') return;
          event.preventDefault();
          place(event.dataTransfer.getData('text/plain'));
        },
      }, number);
      if (pending) {
        cell.append(
          h('div', {
            class: 'pending-pet',
            draggable: 'true',
            title: '拖回宠物池可以取消',
            ondragstart: (event) => {
              dragging = 'slot';
              event.dataTransfer.setData('text/plain', pending);
              event.dataTransfer.effectAllowed = 'move';
            },
            ondragend: () => { dragging = null; settle(); },
          }, petChip(side, pending)),
          h('button', { class: 'slot-remove', title: '放回宠物池', onclick: () => place(null) }, '✕'),
        );
      } else {
        cell.append(h('span', { class: 'slot-hint' }, '拖到这里'));
      }
      cells.push(cell);
    }
    return h('div', { class: `slots side-${side}` },
      h('span', { class: 'slots-label' }, `${app.sideName(side)}阵容`), cells);
  }

  function render() {
    const match = app.state.match;
    const draft = match.draft;
    const name = pending ? app.species(pending).name : null;
    const main = [
      slots('opp', draft),
      h('div', { class: 'confirm-row' },
        h('span', { class: 'confirm-hint' },
          pending
            ? `${app.sideName(draft.turn)}选择了${name}，确认后不能撤回`
            : `${app.sideName(draft.turn)}：从宠物池拖一只宠物到高亮的站位槽`),
        h('button', {
          class: 'btn primary', id: 'confirm-pick', disabled: !pending || busy, onclick: confirmPick,
        }, pending ? `确认选择 ${name}` : '确认')),
      slots('me', draft),
    ];
    fill(page,
      statusBar(draft),
      pool('opp', match),
      // 启用 AI 军师时，面板放在站位槽右侧；面板节点只创建一次，每次重绘复用
      coachPanel
        ? h('div', { class: 'board with-coach' }, h('div', { class: 'board-main' }, main), coachPanel.el)
        : h('div', { class: 'board' }, main),
      pool('me', match),
    );
    renderCoach();
  }

  render();
  announceAiPicks(0); // 随机 / 智能模式下对手方已经先选了第一只
  return () => {
    unmounted = true;
    coach?.dispose();
    clearPageTour();
  };
}

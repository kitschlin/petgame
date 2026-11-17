// 战斗：把服务端的事件逐条播出来；轮到谁行动，就开放谁那一方的操作面板。
// AI 指导（启用时）：轮到我方宠物行动时在日志右侧给出建议，并高亮推荐的技能和目标。

import { api } from '../api.js';
import { app } from '../app.js';
import { h } from '../dom.js';
import { createCoach } from '../coach.js';
import { createArena, marksByEvent } from '../components/arena.js';
import { createBattlePanel } from '../components/battlePanel.js';
import { confirmDialog, openModal } from '../components/modal.js';
import { createPlayer, loadSpeed, saveSpeed, SPEEDS } from '../player.js';
import { clearPageTour, setPageTour } from '../guide.js';

const GUIDE_KEY = 'petarena.guide';

export function mountBattle(root, onPhaseChange) {
  const match = app.state.match;
  const battle = match.battle;
  const events = battle.events;
  let options = battle.options;
  let view = battle.view;
  let token = battle.token; // 标识当前这个决策点：建议只对它有效
  let finished = match.phase === 'finished';
  let winner = battle.view.winner;
  let rounds = battle.view.round;
  let modal = null;
  let unmounted = false;

  let coach = null; // AI 指导：未启用时保持为 null，页面与没有这个功能时完全一致
  let coachPanel = null;
  let guide = localStorage.getItem(GUIDE_KEY) !== 'off';
  let settled = false; // 动画播完、轮到玩家操作了：建议这时才展示出来

  const toolButton = h('button', { class: 'btn ghost', id: 'battle-tool' });
  const speed = h('select', {
    class: 'speed',
    title: '动画速度',
    onchange: (event) => {
      const value = Number(event.target.value);
      saveSpeed(value);
      player.setSpeed(value);
    },
  }, SPEEDS.map((s) => h('option', { value: s.value, selected: s.value === loadSpeed() }, s.label)));

  const arena = createArena({
    statics: battle.static,
    opponent: match.opponent,
    live: true,
    onAct: act,
    tools: [h('label', { class: 'speed-label' }, '速度', speed), toolButton],
  });
  root.append(arena.el);

  function updateTool() {
    toolButton.textContent = finished ? '查看结算' : '放弃本局';
    toolButton.onclick = finished ? showEnd : abandon;
  }

  // ---- AI 指导 ----

  // 现在等待操作的是不是我方的宠物
  const myTurn = () => !finished && !!view.awaiting && battle.static.pets[view.current]?.side === 'me';

  function renderCoach() {
    if (!coachPanel) return;
    const state = coach.state;
    // 建议可能在动画还没播完时就回来了：先按「思考中」显示，等轮到玩家操作再亮出来
    const ready = state.status === 'ready' && settled && state.token === token;
    const shown = state.status === 'ready' && !ready ? { status: 'thinking', retry: 0 } : state;
    let idleText = null;
    if (finished) idleText = '战斗已结束。可以在「游戏结束」里打开复盘。';
    else if (!myTurn()) idleText = '对手方行动中 · AI 只为我方提供建议';
    coachPanel.render(shown, { guide, idleText });
    arena.setAdvice(ready && guide ? state.result.advice : null);
  }

  // 请求当前这个决策点的建议。在收到我方操作的结果后马上调用，这样请求和动画同时进行
  function requestAdvice() {
    if (!coach || unmounted) return;
    if (guide && token && myTurn()) coach.request(token);
    else coach.cancel();
  }

  function toggleGuide() {
    guide = !guide;
    localStorage.setItem(GUIDE_KEY, guide ? 'on' : 'off');
    requestAdvice();
    renderCoach();
  }

  // ---- 界面导览：第一次轮到我方操作时自动走一遍（这时操作栏和 AI 指导都已经出来了） ----

  let coachChecked = false;
  let tourReady = false;
  const tourSteps = () => [
    { target: '.arena-bar', title: '回合与行动队列',
      text: `每个大回合，存活的宠物按速度从快到慢排队，每只行动一次（蓝圈是我方，橙圈是对手方）。最多 ${battle.static.max_rounds} 回合，打不完算平局。` },
    { target: '.side[data-side=opp] .pets-row', title: '上方：对手方',
      text: '把这 3 只全部打倒就赢。血条下面的蓝条是护盾，会先挡伤害；卡片右上角的徽章表示冻结、水链等状态。鼠标停在宠物上可以看它的技能。' },
    { target: '.side[data-side=me] .beans', title: '能量豆',
      text: '全队共用：开局 3 点，从第 2 回合起每回合 +2，最多 8 点。特色技能要花能量豆，普通攻击免费。' },
    { target: '.action-panel[data-side=me]', title: '轮到你出招',
      text: ['先点「普通攻击」或特色技能，再点', h('b', { class: 'tour-target' }, '橙色闪烁'),
        '的目标；不需要目标的技能点了就直接放。能量豆不够的技能是灰的。'] },
    { target: '#battle-coach', title: '🧠 AI 指导',
      text: ['每次轮到你都会给出推荐动作和理由，推荐的按钮和目标带紫色', h('b', { class: 'tour-ai' }, 'AI 推荐'),
        '标记。下面的条形图是各个动作的推演胜率，越长越好。只是建议，点不点由你。'] },
    { target: '.log-wrap', title: '战斗日志',
      text: '伤害、治疗、冻结、补刀都写在这里。打完之后可以看回放和战斗记录。' },
  ];
  function maybeTour() {
    if (tourReady || unmounted || !coachChecked || !settled || !myTurn()) return;
    tourReady = true;
    setPageTour('battle', tourSteps);
  }

  api.coachStatus().then((status) => {
    coachChecked = true;
    if (unmounted || !status.enabled) { maybeTour(); return; }
    coachPanel = createBattlePanel({ onRetry: () => coach.retry(), onToggle: toggleGuide });
    coach = createCoach({ fetch: api.coachBattle, onChange: renderCoach });
    arena.attach(coachPanel.el);
    requestAdvice();
    renderCoach();
    maybeTour();
  }).catch(() => { coachChecked = true; maybeTour(); }); // 查询失败就当作未启用

  // 已经播放过的事件（例如刷新页面后）直接写进日志，不再重播
  const seenKey = `petarena.seen.${match.id}`;
  let seen = Math.min(Number(sessionStorage.getItem(seenKey) || 0), events.length);
  arena.setMarks(marksByEvent(events, battle.marks));
  arena.log.reset(events.slice(0, seen));
  arena.render(events[Math.max(seen - 1, 0)].view);

  const player = createPlayer({
    onEvent(event, animated) {
      arena.render(event.view);
      if (animated) arena.fx(event);
      arena.log.append(event);
      seen = event.i + 1;
      sessionStorage.setItem(seenKey, String(seen));
    },
    onIdle: settle,
  });

  function settle() {
    updateTool();
    settled = true;
    if (finished) showEnd();
    else arena.setOptions(options);
    renderCoach();
    maybeTour();
  }

  // 面板上正显示着的那条建议（开着指导、已经展示出来、而且就是针对当前这次行动的）
  function shownAdvice() {
    const state = coach?.state;
    return guide && settled && state && state.status === 'ready' && state.token === token ? state.result.advice_id : null;
  }

  async function act(skill, target) {
    const adviceId = shownAdvice();
    settled = false;
    if (coach) {
      coach.cancel(); // 玩家已经做出选择：进行中的请求作废，旧建议不再显示
      renderCoach();
    }
    try {
      const result = await api.action(skill, target, adviceId);
      options = result.options;
      view = result.view;
      token = result.token;
      finished = result.phase === 'finished';
      winner = result.view.winner;
      rounds = result.view.round;
      if (result.mark) { // 这次行动是否采纳了建议：标在日志里对应的那一行
        const mine = result.events.find((event) => event.type === 'action');
        if (mine) arena.addMark(mine.i, result.mark);
      }
      player.push(result.events);
      requestAdvice();
      if (finished) app.refresh().catch(() => {}); // 战斗结束，解除导航里的 PVE 锁定
    } catch (error) {
      app.toast(error.message, 'error');
      settled = true;
      arena.setOptions(options);
      requestAdvice(); // 这次没操作成：把建议要回来（服务端有缓存）
    }
  }

  async function abandon() {
    if (!(await confirmDialog('放弃本局？', '放弃后这场战斗作废，不会计入战斗记录。', '放弃本局'))) return;
    if (coach) coach.cancel();
    try {
      app.set(await api.abandon());
      app.go('#/');
    } catch (error) {
      app.toast(error.message, 'error');
      requestAdvice();
    }
  }

  function showEnd() {
    if (modal) return;
    const close = () => { if (modal) modal.close(); modal = null; };
    const go = (hash) => () => { close(); app.go(hash); };
    // 再来一局：回到开局页重新选择对手模式（预先选中本局的模式），不直接开新局
    const again = () => {
      close();
      onPhaseChange({ again: match.opponent });
    };
    const verdict = winner === 'draw' ? '平局' : `${app.sideName(winner)}获胜！`;
    modal = openModal({
      title: '游戏结束',
      className: `end-modal result-${winner}`,
      body: [
        h('div', { class: 'verdict' }, verdict),
        h('div', { class: 'muted' },
          winner === 'draw' ? '10 个大回合未分胜负' : `战斗在第 ${rounds} 个大回合结束`),
      ],
      buttons: [
        coach ? h('button', { class: 'btn coach-review-btn', dataset: { act: 'review' }, onclick: go(`#/review/${match.id}`) }, '🧠 AI 复盘') : null,
        h('button', { class: 'btn', dataset: { act: 'record' }, onclick: go(`#/records/${match.id}`) }, '查看战斗记录'),
        h('button', { class: 'btn', dataset: { act: 'replay' }, onclick: go(`#/replay/${match.id}`) }, '回放'),
        h('button', { class: 'btn primary', dataset: { act: 'again' }, onclick: again }, '再来一局'),
        h('button', { class: 'btn ghost', dataset: { act: 'close' }, onclick: close }, '关闭'),
      ],
    });
  }

  updateTool();
  if (seen < events.length) player.push(events.slice(seen));
  else settle();

  return () => {
    unmounted = true;
    player.stop();
    if (coach) coach.dispose();
    if (modal) modal.close();
    clearPageTour();
  };
}

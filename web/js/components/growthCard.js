// 培养建议卡片：引擎给出的培养项目 + 教练的讲解。复盘页和 PVE 页共用。
// 默认（data.basis === 'facts'）：不做模拟，只按台阶给方向——几项各自独立的建议，没有胜算。
// 完整模式（COACH_GROWTH=full）：逐项模拟后选出的路线，带「现在 → 练完」的胜算和每一步的增益。
// 只给建议：点「去练这一项」只是帮你选好宠物和副本，仍需自己点挑战。

import { api } from '../api.js';
import { app } from '../app.js';
import { fill, h } from '../dom.js';
import { createCoach, MAX_RETRIES } from '../coach.js';
import { attachTip, hideTip } from './tip.js';

const pct = (value) => `${Math.round(value * 100)}%`;
const names = (keys) => keys.map((key) => app.species(key).name).join('、');
const OPPONENT = { random: '随机对手', smart: '智能对手', manual: '手动对手' };
const BASIS = { guided: '照战斗指导出招、对手按它真实打法的整局模拟', plain: '双方按常规打法的整局模拟' };

// 一个试过、但增益没过关的候选：增益和它的误差（两倍标准误）
function triedText(item) {
  const gain = Math.round(item.gain * 100);
  const effect = gain > 0 ? `约 +${gain}%，误差 ±${Math.round(2 * item.se * 100)}%` : '没有增益';
  return `${item.text}（${item.points} 点，期望约 ${Math.round(item.attempts)} 次挑战）：${effect}`;
}

export function createGrowthCard({ onPick }) {
  const body = h('div', { class: 'growth-body' });
  const el = h('section', { class: 'panel growth-card', id: 'growth-card', dataset: { status: 'idle' } },
    h('h3', {}, '🧠 AI 培养建议'), body);
  const coach = createCoach({ fetch: (token, attempt, signal) => api.coachGrowth(attempt, signal), onChange: render });

  const current = (item) => app.state.roster.me[item.pet][item.stat];

  // 胜算：现在 / 练完整条路线，两行对齐的条，竖线是目标
  function routeMeter(data) {
    const basis = BASIS[data.basis];
    const row = (name, value, chosen, note) => attachTip(h('div', { class: `viz-meter growth-meter${chosen ? ' chosen' : ''}` },
      h('span', { class: 'viz-meter-name' }, name),
      h('span', { class: 'viz-meter-track' },
        h('span', { class: 'viz-meter-fill', style: { width: pct(value) } }),
        h('span', { class: 'viz-meter-goal', style: { left: pct(data.goal) } })),
      h('span', { class: 'viz-meter-value' }, pct(value))), () => [
      h('b', {}, pct(value)), ` ${note}`,
      h('div', { class: 'viz-tip-sub' }, `${basis}，${data.sample} 局 · 竖线是目标 ${pct(data.goal)}`)]);
    return h('div', { class: 'viz growth-route', id: 'growth-route', role: 'group', 'aria-label': '培养前后的胜算' },
      row('现在的胜算', data.now, false, '不培养时的胜算'),
      data.steps.length ? row('练完整条路线', data.after, true, `练完这 ${data.points} 点之后的胜算`) : null);
  }

  function stepView(step, why) {
    const done = step.items.every((item) => current(item) >= item.to);
    return h('li', { class: `growth-step${done ? ' done' : ''}`, dataset: { step: step.id } },
      h('div', { class: 'growth-step-head' },
        h('span', { class: 'growth-step-id' }, step.id),
        h('b', { class: 'growth-step-text' }, step.text),
        h('span', { class: 'muted' }, `期望约 ${Math.round(step.attempts)} 次挑战`),
        step.gain == null ? null
          : h('span', { class: 'growth-gain', title: '这一步带来的增益：把整条路线量出来的总增益，按引擎对各步的估计分摊' },
            `这一步约 +${Math.round(step.gain * 100)}%`)),
      why ? h('p', { class: 'growth-why' }, why) : null,
      step.facts.length ? h('ul', { class: 'growth-facts' }, step.facts.map((fact) => h('li', {}, fact))) : null,
      h('div', { class: 'growth-items' }, step.items.map((item) => {
        const now = current(item);
        const left = item.to - now;
        return h('span', { class: `growth-item${left <= 0 ? ' done' : ''}`, dataset: { pet: item.pet, stat: item.stat } },
          `${app.species(item.pet).emoji} ${app.species(item.pet).name}${app.state.config.stats[item.stat]} ${now} → ${item.to}`,
          left <= 0
            ? h('span', { class: 'growth-done' }, '✓ 已练到')
            : h('button', {
              class: 'btn small', dataset: { act: 'train' }, title: '只是帮你选好宠物和副本，仍需自己点挑战',
              onclick: () => onPick(item),
            }, `去练这一项（还差 ${left}）`));
      })));
  }

  function planView(plan) {
    const { data, advice } = plan;
    const why = Object.fromEntries((advice?.steps || []).map((step) => [step.id, step.why]));
    const parts = [
      h('p', { class: `growth-headline tone-${plan.verdict === 'none' ? 'calm' : plan.needed ? 'go' : 'hold'}`, id: 'growth-headline' },
        plan.headline),
    ];
    if (!data) return parts;
    const tried = data.tried || [];  // 旧版本算出来的方案没有这一项
    const light = data.basis === 'facts';  // 只按台阶给方向：没有胜算
    parts.push(h('p', { class: 'muted' },
      `针对：${OPPONENT[plan.opponent] || '对手'}的 ${data.targets.map(names).join('；')}，我方用 ${names(data.lineup)}。`
      + (light ? '' : `目标胜算 ${pct(data.goal)}。`)));
    if (!light) parts.push(routeMeter(data));
    if (advice?.summary) parts.push(h('p', { class: 'growth-summary' }, h('b', {}, '教练'), advice.summary));
    if (data.steps.length) {
      parts.push(h('ol', { class: 'growth-steps' }, data.steps.map((step) => stepView(step, why[step.id]))));
      const total = `一共 ${data.points} 点（期望约 ${Math.round(data.attempts)} 次挑战）。`;
      parts.push(h('p', { class: 'growth-stop' }, h('b', {}, '到此为止'),
        advice?.stop || (light ? (data.notes || [])[0] || '练到写的数值就停。'
          : data.reached ? `${total}练完就到目标了，不用再往上练。`
            : tried.length ? `${total}接着模拟过的方案增益都不显著（见下），不用再往上练。`
              : `${total}一条路线最多安排这么几步：练完再打一局，按新的结果接着安排。`)));
    }
    if (light && (data.notes || []).length > 1) {
      parts.push(h('div', { class: 'growth-avoid', id: 'growth-avoid' },
        h('b', {}, '别练'), h('ul', {}, data.notes.slice(1).map((note) => h('li', {}, note)))));
    }
    // 路线为什么停在这里：接着模拟过的方案里看起来最好的几个，增益也没过关
    if (tried.length) {
      parts.push(h('div', { class: 'growth-tried', id: 'growth-tried' },
        h('b', {}, data.steps.length ? '再往下练' : '试过了'),
        h('span', {}, data.steps.length
          ? '接着模拟过的方案里，看起来最好的几个增益也在误差范围内，所以路线到此为止：'
          : '模拟里看起来最好的几个方案，增益也都在误差范围内，不值得为它们花挑战次数：'),
        h('ul', {}, tried.map((item) => h('li', {}, triedText(item))))));
    }
    if (data.avoid.length) {
      parts.push(h('div', { class: 'growth-avoid', id: 'growth-avoid' },
        h('b', {}, '别练'),
        h('ul', {}, data.avoid.map((item) => h('li', {}, item.text))),
        advice?.avoid ? h('p', {}, advice.avoid) : null));
    }
    parts.push(h('p', { class: 'coach-note' },
      light ? `这几项${data.label}。建议都练成：属性的效果是台阶式的，只练其中一项不一定见效`
        : `胜算的口径：${BASIS[data.basis]}（现在、练完各 ${data.sample} 局，误差约 ±${Math.round(100 / Math.sqrt(data.sample))}%）。${data.label}`,
      ' · AI 只给建议，仍需你自己去挑战'));
    return parts;
  }

  function render(state = coach.state) {
    hideTip();
    el.dataset.status = state.status;
    if (state.status === 'thinking') {
      const text = state.waiting ? '后台正在逐项模拟培养方案…'
        : state.retry > 0 ? `AI 思考中（重试 ${state.retry}/${MAX_RETRIES}）` : 'AI 思考中…';
      fill(body, h('div', { class: 'coach-thinking' }, h('span', { class: 'coach-spinner' }), text));
    } else if (state.status === 'ready') {
      fill(body, planView(state.result.plan));
    } else if (state.status === 'empty') {
      fill(body, h('div', { class: 'coach-empty' },
        h('b', {}, '暂无建议'), h('span', {}, state.message || ''),
        state.code === 'auth' ? null : h('button', { class: 'btn small', id: 'growth-retry', onclick: () => coach.retry() }, '重试')));
    } else {
      fill(body, h('p', { class: 'coach-idle' }, '打完一局 PVP 之后，这里会告诉你值不值得培养、练哪一项、练到哪里为止。'));
    }
  }

  render();
  return {
    el,
    load: () => coach.request('growth'),
    refresh: () => render(), // 属性变了（刚练成一点）：按新的数值重画进度
    dispose: () => coach.dispose(),
  };
}

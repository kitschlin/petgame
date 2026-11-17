// AI 军师面板：空闲、思考中（重试 n/2）、建议、暂无建议 + 重试。
// 只展示，不替玩家操作。面板里不使用 pet-card / log-line / skill-btn 等类名。

import { app } from '../app.js';
import { fill, h } from '../dom.js';
import { MAX_RETRIES } from '../coach.js';
import { traceView } from './traceView.js';

const pct = (value) => `${Math.round(value * 100)}%`;

function pet(key, extra) {
  const species = app.species(key);
  return h('span', { class: 'coach-pet', dataset: { species: key } },
    h('span', { class: 'coach-pet-emoji' }, species.emoji), species.name, extra);
}

function names(keys) {
  return keys.map((key) => app.species(key).name).join('、');
}


// 经验起作用时：修正前后的胜率，以及修正依据了多少推演和实战。
// 经验有两个来源：随游戏内置的（离线把「照指导出招」的对阵普查过一遍）和这位玩家自己打出来的。
function experienceView(experience, shown) {
  if (!experience || !experience.active) return null;
  const [win, draw, loss] = experience.real;
  // 面板上的「推演胜率」已经是修正后的数字（可能还用新种子复核过）；这里倒推出修正前是多少，两处对得上
  const base = Math.min(1, Math.max(0, shown - (experience.adjusted - experience.base)));
  const seeded = experience.seed_games || 0;
  const own = experience.guided_games - seeded;
  const fresh = experience.battles === 0;  // 一局都还没打：数字按「照着 AI 指导出招」算
  const basis = [
    seeded ? `内置经验：这组对阵离线推演过 ${seeded} 局` : null,
    own > 0 ? `${seeded ? '你的对局之后又' : '这组对阵照指导'}推演过 ${own} 局` : null,
    !experience.guided_games
      ? (experience.borrowed_games ? `借用同一套对手阵容下 ${experience.borrowed_games} 局推演` : '参考这类对手的整体表现') : null,
    win + draw + loss ? `实战 ${win} 胜 ${draw} 平 ${loss} 负` : null,
    experience.lessons ? `参考经验 ${experience.lessons} 条` : null,
  ].filter(Boolean).join(' · ');
  const untouched = !experience.guided_games && win + draw + loss === 0 && pct(base) === pct(shown);
  const after = fresh ? '照 AI 指导出招' : '按你的操作';
  return h('p', {
    class: 'coach-exp', id: 'coach-exp', dataset: { changed: untouched ? 'no' : 'yes', seeded: seeded ? 'yes' : 'no' },
    title: '常规口径：假设你按常规打法出招时的胜率。\n'
      + (fresh ? '照 AI 指导出招：战斗里照着 AI 指导的高亮出招时的胜率（你还没有打过，先按这个前提算）。'
        : `按你的操作：用最近 ${experience.battles} 场对局里你的操作水平（${Math.round(experience.quality * 100)}%：0 = 常规打法，100 = 每一步都打到推演最优）和真实战绩修正之后的胜率。`),
  },
    h('b', {}, '经验'),
    untouched ? `这套阵容还没有实战或推演过的记录，沿用常规口径 ${pct(shown)}`
      : `常规口径 ${pct(base)} → ${after} ${pct(shown)}`,
    untouched ? null : h('i', {}, basis));
}

// Top10 阵容表（经验库的数字）：Agent 只能在这里面选。推荐和备选高亮
function topView(advice, evidence) {
  if (!evidence.top || !evidence.top.length) return null;
  const alts = new Set(advice.alternatives.map((alt) => alt.id));
  return h('details', { class: 'coach-top', id: 'coach-top' },
    h('summary', {}, `Top${evidence.top.length} 阵容（经验库推演，AI 只在这里面选）`),
    h('table', {},
      h('tbody', {}, evidence.top.map((row) => h('tr', {
        class: row.id === advice.id ? 'chosen' : alts.has(row.id) ? 'alt' : '',
        dataset: { id: row.id },
      },
        h('td', {}, row.id),
        h('td', {}, names(row.lineup)),
        h('td', {}, `本手 ${app.species(row.pick).name}`),
        h('td', {}, pct(row.win_rate)),
        h('td', {}, row.id === advice.id ? '推荐' : alts.has(row.id) ? '备选' : ''))))));
}

function adviceView(result) {
  const { advice, evidence } = result;
  const chosen = evidence.chosen;
  const top = evidence.top && evidence.top[0];
  // 对手还没选完、但预测里只有一种补法（智能对手面对这套阵容时的选择是确定的）：写明预计它补谁
  const worst = chosen.replies > 1
    ? `最差 ${pct(chosen.worst_win_rate)}（对手补${names(chosen.worst_reply)}）`
    : chosen.worst_reply.length ? `预计对手补${names(chosen.worst_reply)}` : '对手阵容已确定';
  return [
    h('div', { class: 'coach-pick-row' },
      h('span', { class: 'coach-label' }, '👉 推荐'),
      h('span', { id: 'coach-pick' }, pet(advice.pet)),
      chosen.rank > 1 && top ? h('span', {
        class: 'coach-tag', id: 'coach-gap',
        title: `AI 没有选表上第一的 ${top.id}（${names(top.lineup)}，${pct(top.win_rate)}），理由见下`,
      }, `${chosen.id}，比 ${top.id} 低 ${Math.round(chosen.gap_to_top * 100)} 个百分点`) : null,
      h('span', {
        class: `coach-rate ${chosen.win_rate >= 0.55 ? 'good' : chosen.win_rate < 0.45 ? 'bad' : 'even'}`,
        title: `${evidence.label}\n共 ${chosen.samples} 局${chosen.verified ? '（选定后用新种子复核）' : ''}`,
      },
        h('b', {}, `推演胜率 ${pct(chosen.win_rate)}`), h('i', {}, worst))),
    experienceView(evidence.experience, chosen.win_rate),
    chosen.win_rate < 0.3
      ? h('p', { class: 'coach-low', id: 'coach-low' }, h('b', {}, '提示'), '胜算偏低：这局打完看复盘，里面会告诉你换阵容还是培养哪一项。') : null,
    h('p', { class: 'coach-reason', id: 'coach-reason' }, advice.reason),
    h('p', { class: 'coach-strategy', title: `计划阵容（按站位）：${names(advice.plan)}` },
      h('b', {}, '思路'), advice.strategy),
    advice.alternatives.map((alt) =>
      h('p', { class: 'coach-alt', title: alt.reason },
        h('b', {}, '备选'), pet(alt.pet), h('i', {}, pct(alt.win_rate)),
        h('span', {}, alt.reason))),
    topView(advice, evidence),
    traceView(result.trace),
    h('p', { class: 'coach-note' }, `${evidence.label} · AI 只给建议，仍需你自己拖拽并确认`),
  ];
}

export function createCoachPanel({ onRetry }) {
  const body = h('div', { class: 'coach-body' });
  const el = h('aside', { class: 'coach-panel draft-coach', id: 'coach-panel', dataset: { status: 'idle' } },
    h('div', { class: 'coach-scroll' },
      h('div', { class: 'coach-head' },
        h('b', { class: 'coach-title' }, '🧠 AI 军师'), h('span', { class: 'coach-sub' }, '阵容搭配')),
      body));

  // state 来自请求控制器；idleText 是没有请求时的提示（例如轮到对手方）
  function render(state, idleText) {
    el.dataset.status = state.status;
    if (state.status === 'thinking') {
      const text = state.retry > 0 ? `AI 思考中（重试 ${state.retry}/${MAX_RETRIES}）` : 'AI 思考中…';
      fill(body, h('div', { class: 'coach-thinking' }, h('span', { class: 'coach-spinner' }), text),
        h('p', { class: 'coach-note' }, '不用等它：你随时可以自己选'));
    } else if (state.status === 'ready') {
      fill(body, adviceView(state.result));
    } else if (state.status === 'empty') {
      fill(body,
        h('div', { class: 'coach-empty' },
          h('b', {}, '暂无建议'), h('span', {}, state.message || ''),
          state.code === 'auth' ? null : h('button', { class: 'btn small', id: 'coach-retry', onclick: onRetry }, '重试')),
        h('p', { class: 'coach-note' }, '对局不受影响，你可以完全手动完成选将'));
    } else {
      fill(body, h('p', { class: 'coach-idle' }, idleText || '轮到我方选将时，这里会给出本手推荐'));
    }
  }

  render({ status: 'idle' });
  return { el, render };
}

// 没有配置 API 时的 AI 军师面板：只提示怎么配置，不给任何建议（不退回假模型）。
// 用单独的 id（coach-setup），界面导览里讲 AI 军师的那一步因此会略过。
export function createSetupPanel() {
  const code = (text) => h('code', { class: 'coach-code' }, text);
  return {
    el: h('aside', { class: 'coach-panel draft-coach coach-setup', id: 'coach-setup', dataset: { status: 'setup' } },
      h('div', { class: 'coach-scroll' },
        h('div', { class: 'coach-head' },
          h('b', { class: 'coach-title' }, '🧠 AI 军师'), h('span', { class: 'coach-sub' }, '未配置 API')),
        h('div', { class: 'coach-body' },
          h('div', { class: 'coach-empty' }, h('b', {}, '请先配置 LLM API'),
            h('span', {}, 'AI 军师、AI 指导、AI 复盘都需要一个大模型接口')),
          h('p', { class: 'coach-setup-step' }, '在项目根目录的 ', code('.env'), ' 里写上（OpenAI 兼容接口即可）：'),
          h('pre', { class: 'coach-code-block' },
            'COACH_API_KEY=sk-...\n'
            + 'COACH_BASE_URL=https://api.siliconflow.cn/v1\n'
            + 'COACH_MODEL=deepseek-ai/DeepSeek-V3.2'),
          h('p', { class: 'coach-setup-step' }, '然后重启服务（', code('python run.py'), '）。用 DeepSeek 官方接口时只填 ',
            code('COACH_API_KEY'), ' 即可。'),
          h('p', { class: 'coach-note' }, '对局不受影响，你可以完全手动完成选将和战斗')))),
    render() {},
  };
}

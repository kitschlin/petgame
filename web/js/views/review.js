// 战后复盘：引擎把这一局重演一遍、逐步对照推演之后的结论，加上教练的讲解。
// 引擎分析先出来；定向推演和讲解在后台完成后陆续出现，所以这一页会自己刷新几次。

import { api } from '../api.js';
import { app, OPPONENT_LABEL } from '../app.js';
import { fill, formatTime, h } from '../dom.js';
import { createCoach, MAX_RETRIES } from '../coach.js';
import { createGrowthCard } from '../components/growthCard.js';
import { petChip } from '../components/petCard.js';
import { attachTip, hideTip } from '../components/tip.js';

const POLL_MS = 1500;
const pct = (value) => `${Math.round(value * 100)}%`;
const signed = (value) => {
  const points = Math.round(value * 100);
  return points === 0 ? '0%' : `${points > 0 ? '+' : '−'}${Math.abs(points)}%`;
};
const names = (keys) => keys.map((key) => app.species(key).name).join('、');

// 四项归因：名称、说明（悬停或聚焦时显示）
const PARTS = [
  ['stats', '属性底子', '现有属性下，对这套对手阵容最好的阵容有几成胜算（相对五五开）'],
  ['lineup', '阵容', '实际阵容比最好的阵容差了多少'],
  ['play', '操作', '各步比推演最优的动作少赚的胜算之和（照着建议打就是 0）'],
  ['luck', '运气与对手', '冻结、补刀、同速等随机结果，以及对手比预想的打得好或差'],
];
const BASIS = {
  guided: '口径：照战斗指导出招、对上对手真实打法的整局推演',
  table: '口径：对阵表（双方常规打法）；后台的定向推演完成后会更新',
};
const GRADES = {
  best: ['最优', 'good'], good: ['不错', 'good'], only: ['别无选择', 'none'],
  slip: ['小瑕疵', 'warning'], mistake: ['失误', 'serious'], blunder: ['重大失误', 'critical'],
};
const MOMENT_TAG = {
  blunder: '重大失误', mistake: '失误', slip: '小瑕疵', good: '好棋', luck: '转折', lineup: '阵容', foe: '对手', ceiling: '推演',
};
const RESULT = { me: '我方获胜', opp: '对手方获胜', draw: '平局' };

function verdictText(data) {
  const { facts, verdict, goal } = data;
  const ceiling = facts.ceiling;
  const guided = ceiling && ceiling.guided;
  if (verdict === 'win') return '这一局赢了。下面是赢在哪里，以及还能打得更好的地方。';
  if (!verdict) return '后台还在推演这套对手阵容（用来判断换阵容、练属性有没有用），稍等一会儿就有结论。';
  if (verdict === 'train') {
    // 最好的阵容有四五成胜算时，输掉这一局本身多半是运气（归因图上「运气」那一项最大），但结论仍然是该练属性：
    // 把两件事都说清楚，免得结论和图看上去对不上
    const unlucky = facts.attribution.luck < facts.attribution.stats;
    return guided
      ? `照战斗指导出招的推演里，对这套对手阵容最好的阵容（${names(ceiling.lineup)}）也只有约 ${pct(ceiling.value)} 的胜算，`
        + `到不了 ${pct(goal)} 的目标。${unlucky ? '这一局输掉有运气的成分（见归因图），但想稳定地赢，' : ''}`
        + '问题不在选将和操作，要靠培养来补。看下面的培养建议。'
      : `按常规打法，现有属性下最好的阵容也到不了 ${pct(goal)} 的目标：要靠培养来补。`;
  }
  if (verdict === 'lineup') {
    const versus = ceiling.replies
      ? `我方换成 ${names(ceiling.lineup)}（按站位）时，它按选将习惯多半会拿出 ${ceiling.replies.map(names).join(' 或 ')}，这样`
      : `对这套对手阵容，${names(ceiling.lineup)}（按站位）`;
    return `${versus}约 ${pct(ceiling.value)} 的胜算，实际的 ${names(facts.my_lineup)} 约 ${pct(ceiling.played)}：输在阵容上。这条经验已经记下，以后选将时军师会把它算进去（一局的证据有限，同样的结果多出现几次，推荐才会明显改变）。`
      + (ceiling.value < goal ? `不过换了阵容也到不了 ${pct(goal)} 的目标，还要配合培养：看下面的培养建议。` : '');
  }
  if (verdict === 'play') {
    return `阵容没有问题，但操作上一共少赚了约 ${pct(facts.quality.missed)} 的胜算：看下面「关键回合」里标出的几步。`;
  }
  return '阵容和操作都没有明显问题，这一局主要输在运气和对手发挥上。照这样再打，赢面不小。';
}

// 归因图：一行一项，从中间的零线向左（对我方不利）或向右（有利）伸出；数值直接写在右边
function attributionView(facts) {
  const parts = facts.attribution;
  const total = facts.outcome - 0.5;
  const reach = Math.max(0.5, ...PARTS.map(([key]) => Math.abs(parts[key])));
  const bar = (value) => {
    const width = `${(Math.abs(value) / reach) * 50}%`;
    return h('div', { class: 'attr-track' },
      h('span', { class: 'attr-zero' }),
      Math.abs(value) >= 0.005
        ? h('span', { class: `attr-fill ${value >= 0 ? 'pos' : 'neg'}`, style: value >= 0 ? { left: '50%', width } : { right: '50%', width } })
        : null);
  };
  const row = ([key, label, note]) => attachTip(h('tr', { dataset: { part: key } },
    h('th', { scope: 'row' }, label), h('td', { class: 'attr-bar' }, bar(parts[key])),
    h('td', { class: 'attr-value' }, signed(parts[key]))), () => [
    h('b', {}, signed(parts[key])), ` ${label}`, h('div', { class: 'viz-tip-sub' }, note)]);
  return h('div', { class: 'viz', id: 'review-attribution' },
    h('table', { class: 'attr-table' },
      h('caption', {}, `结果相对「五五开」的差距，拆成四项（相加正好等于合计）。${BASIS[facts.basis] || BASIS.table}`),
      h('tbody', {},
        PARTS.map(row),
        h('tr', { class: 'attr-total', dataset: { part: 'total' } },
          h('th', { scope: 'row' }, '合计'), h('td', { class: 'attr-bar' }, bar(total)),
          h('td', { class: 'attr-value' }, signed(total))))),
    h('div', { class: 'viz-legend' },
      h('span', {}, h('i', { class: 'viz-swatch neg' }), '对我方不利'),
      h('span', {}, h('i', { class: 'viz-swatch pos' }), '对我方有利'),
      h('span', { class: 'muted' }, `合计 ${signed(total)} = ${RESULT[facts.winner]}（${facts.outcome === 1 ? '胜' : facts.outcome === 0 ? '负' : '平'}）相对 50%`)));
}

function gradeTag(grade) {
  const [label, tone] = GRADES[grade] || [grade, 'none'];
  return h('span', { class: `grade-tag tone-${tone}`, dataset: { grade } }, h('i', {}), label);
}

function decisionTable(facts) {
  const rows = facts.decisions.map((d) => h('tr', { dataset: { idx: d.idx, grade: d.grade } },
    h('td', {}, `第 ${d.round} 回合`),
    h('td', {}, d.label),
    h('td', {}, d.played_text),
    h('td', {}, gradeTag(d.grade)),
    h('td', { class: 'num' }, d.grade === 'only' ? '—' : `${pct(d.v_played)} / ${pct(d.v_best)}`),
    h('td', {}, d.grade === 'best' || d.grade === 'only' ? '—'
      : d.grade === 'good' ? h('span', { class: 'muted' }, '与最优相当') : d.best_text),
    h('td', {}, d.advised == null ? h('span', { class: 'muted' }, '没有请求')
      : h('span', { class: `coach-mark ${d.adopted ? 'adopted' : 'ignored'}` }, d.adopted ? '已采纳' : '未采纳'))));
  return h('table', { class: 'table review-table', id: 'review-decisions' },
    h('thead', {}, h('tr', {},
      ['回合', '宠物', '实际操作', '评级', '胜算（实际 / 最优）', '推演更好的动作', 'AI 建议'].map((text) => h('th', {}, text)))),
    h('tbody', {}, rows));
}

export async function mount(root, id) {
  const page = h('section', { class: 'review' });
  root.append(page);
  let alive = true;
  let timer = null;
  let data = null;
  let growthCard = null;

  const links = () => h('div', { class: 'row-actions' },
    h('a', { class: 'btn', href: `#/replay/${id}` }, '回放'),
    h('a', { class: 'btn', href: `#/records/${id}` }, '查看日志'),
    h('a', { class: 'btn ghost', href: '#/records' }, '返回列表'));

  // 讲解失败后由玩家点「重试」：成功时返回的就是完整的复盘数据
  const reporter = createCoach({
    fetch: (token, attempt, signal) => api.coachReport(id, attempt, signal),
    onChange: (state) => {
      if (state.status === 'ready') data = state.result;
      render();
    },
  });

  function reportView() {
    const report = data.report;
    const state = reporter.state;
    if (report) {
      const comments = Object.fromEntries(report.moments.map((moment) => [moment.id, moment.comment]));
      return [
        h('p', { class: 'review-summary', id: 'review-summary' }, report.summary),
        h('h4', {}, '下一局可以这样做'),
        h('ol', { class: 'review-tips', id: 'review-tips' }, report.tips.map((tip) => h('li', {}, tip))),
        momentsView(comments),
      ];
    }
    let status;
    if (state.status === 'thinking') {
      status = h('div', { class: 'coach-thinking' }, h('span', { class: 'coach-spinner' }),
        state.waiting ? '后台还在处理这一局…' : state.retry > 0 ? `AI 思考中（重试 ${state.retry}/${MAX_RETRIES}）` : 'AI 思考中…');
    } else if (data.stages.working) {
      status = h('div', { class: 'coach-thinking' }, h('span', { class: 'coach-spinner' }),
        data.stages.reflected ? '教练正在写讲解…' : '后台正在推演这套对手阵容，讲解稍后就到…');
    } else {
      status = h('div', { class: 'coach-empty' },
        h('b', {}, '暂无讲解'), h('span', {}, state.message || '引擎的分析不受影响'),
        state.code === 'auth' ? null
          : h('button', { class: 'btn small', id: 'review-retry', onclick: () => reporter.request('report') }, '重试'));
    }
    return [status, momentsView({})];
  }

  function momentsView(comments) {
    const moments = data.facts.moments;
    if (!moments.length) return null;
    return [
      h('h4', {}, '关键回合与结论'),
      h('ul', { class: 'review-moments', id: 'review-moments' }, moments.map((moment) =>
        h('li', { dataset: { id: moment.id, kind: moment.kind } },
          h('span', { class: `moment-tag kind-${moment.kind}` }, MOMENT_TAG[moment.kind] || '事实'),
          h('span', {}, moment.text),
          comments[moment.id] ? h('p', { class: 'moment-comment' }, h('b', {}, '教练'), comments[moment.id]) : null))),
    ];
  }

  function lessonsView() {
    const lessons = data.lessons || [];
    if (!lessons.length) return h('p', { class: 'muted' }, data.report ? '这一局没有沉淀出新的经验条目。' : '讲解完成后，值得长期记住的经验会出现在这里。');
    return h('ul', { class: 'review-lessons', id: 'review-lessons' }, lessons.map((lesson) =>
      h('li', { dataset: { polarity: lesson.polarity } },
        h('span', { class: `lesson-tag ${lesson.polarity}` }, lesson.polarity === 'do' ? '宜' : '忌'),
        h('span', {}, lesson.text),
        h('span', { class: 'muted' },
          `涉及 ${names(lesson.pets)}${lesson.against.length ? ` · 针对对手的 ${names(lesson.against)}` : ''} · 印证 ${lesson.support} 次`))));
  }

  function header(extra) {
    const record = data.record;
    return h('div', { class: 'panel' },
      h('div', { class: 'detail-head' },
        h('h2', {}, '🧠 AI 复盘 ',
          record ? h('span', { class: `result-tag result-${record.winner}` }, RESULT[record.winner]) : null),
        links()),
      record ? h('div', { class: 'review-lineups' },
        h('span', { class: 'muted' }, `${formatTime(record.ended_at)} · 对手方${OPPONENT_LABEL[record.opponent] || '手动'}操作 · 第 ${record.rounds} 回合结束`),
        h('span', { class: 'chips' }, h('b', {}, '我方'), record.my.map((key) => petChip('me', key))),
        h('span', { class: 'chips' }, h('b', {}, '对手方'), record.opp.map((key) => petChip('opp', key)))) : null,
      extra);
  }

  function render() {
    hideTip();
    if (!data) return fill(page, h('div', { class: 'panel empty' }, '正在读取复盘…'));
    if (!data.ok) {
      const text = data.code === 'disabled' ? 'AI 教练没有启用，所以没有复盘。配置好模型的 key 之后重新启动即可（见 README）。' : data.message;
      return fill(page, h('div', { class: 'panel empty', id: 'review-off' }, text, ' ', h('a', { href: `#/records/${id}` }, '查看战斗记录')));
    }
    if (data.status === 'pending') {
      return fill(page, header(h('div', { class: 'coach-thinking', id: 'review-pending' },
        h('span', { class: 'coach-spinner' }), '引擎正在把这一局重演一遍、逐步对照推演…')));
    }
    if (data.status === 'legacy') {
      return fill(page, header(h('div', { class: 'notice', id: 'review-legacy' },
        '这场对局是旧版规则下的记录，按现在的规则已经无法逐步重演，所以只保留结果，不做复盘，也不计入经验。')));
    }
    const { facts } = data;
    const quality = facts.quality;
    fill(page,
      header(h('div', { class: `review-verdict verdict-${data.verdict || 'pending'}`, id: 'review-verdict' },
        h('b', {}, data.verdict === 'win' ? '结论' : `主因：${data.cause}`), h('span', {}, verdictText(data)))),
      h('div', { class: 'review-grid' },
        h('div', { class: 'panel' },
          h('h3', {}, '胜负归因'),
          attributionView(facts),
          h('p', { class: 'coach-note' },
            `有选择余地的决策 ${quality.decisions} 次，其中 ${quality.good} 次打到了推演最优或与之相当`,
            quality.advised ? `；AI 给了 ${quality.advised} 次建议，采纳 ${quality.adopted} 次` : '；这一局没有使用 AI 指导',
            facts.shown != null ? `。选将时面板显示的胜率是 ${pct(facts.shown)}` : '', `。${facts.label}。`)),
        h('div', { class: 'panel review-report', id: 'review-report' }, h('h3', {}, '教练讲解'), reportView())),
      growthCard ? growthCard.el : null,
      !data.latest && data.verdict !== 'win'
        ? h('p', { class: 'muted', id: 'review-older' }, '培养建议只针对最近一场对局，在 PVE 历练页的顶部。') : null,
      h('div', { class: 'panel' }, h('h3', {}, '沉淀的经验'),
        h('p', { class: 'muted' }, '以后选将时，军师会参考这些来自真实对局的经验；同一条被再次印证时会累加。'), lessonsView()),
      h('div', { class: 'panel' }, h('h3', {}, '我方每一步'), decisionTable(facts)));
  }

  async function load() {
    try {
      const [review, record] = await Promise.all([api.coachReview(id), data?.record ? null : api.battle(id).catch(() => null)]);
      if (!alive) return;
      const summary = record ? {
        winner: record.result.winner, rounds: record.result.rounds, ended_at: record.ended_at, opponent: record.opponent,
        my: record.lineups.me.map((pet) => pet.species), opp: record.lineups.opp.map((pet) => pet.species),
      } : data?.record;
      data = { ...review, record: summary };
    } catch (error) {
      if (!alive) return;
      data = { ok: false, message: error.message };
    }
    const ready = data.ok && data.status === 'ready';
    if (ready && data.latest && data.verdict && data.verdict !== 'win' && !growthCard) { // 培养建议只针对最近一场对局
      growthCard = createGrowthCard({
        onPick: (item) => { // 带着选好的宠物和副本去 PVE 页
          let saved = {};
          try { saved = JSON.parse(sessionStorage.getItem('petarena.pve')) || {}; } catch { /* 忽略损坏的本地记录 */ }
          sessionStorage.setItem('petarena.pve', JSON.stringify({ ...saved, species: item.pet, dungeon: item.dungeon }));
          app.go('#/pve');
        },
      });
      growthCard.load();
    }
    render();
    if (data.ok && (data.status === 'pending' || data.stages?.working)) timer = setTimeout(load, POLL_MS);
  }

  render();
  await load();
  return () => {
    alive = false;
    clearTimeout(timer);
    reporter.dispose();
    if (growthCard) growthCard.dispose();
    hideTip();
  };
}

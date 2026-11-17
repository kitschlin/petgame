// Agent 的「思考过程」：按顺序列出 LLM 写下的想法、调用了哪些工具（参数、结果摘要、编号）、最后提交了什么。
// 来自建议接口返回的 trace.steps；默认折叠，只展示，不影响任何操作。

import { h } from '../dom.js';

const TOOL_NAMES = {
  search_combo_experience: '查组合经验',
  search_lessons: '查经验条目',
  evaluate_moves: '引擎推演',
  get_state: '查看局面',
  submit_recommendation: '提交推荐',
  submit_move: '提交动作',
};

function args(value) {
  const parts = Object.entries(value || {})
    .filter(([, v]) => v != null && v !== '' && !(Array.isArray(v) && !v.length))
    .map(([k, v]) => `${k}=${Array.isArray(v) ? v.join('/') : typeof v === 'object' ? JSON.stringify(v) : v}`);
  return parts.join('，');
}

function step(item) {
  if (item.kind === 'thought') return h('li', { class: 'trace-thought' }, h('span', {}, '💭'), item.text);
  const name = TOOL_NAMES[item.tool] || item.tool;
  if (item.kind === 'submit') {
    const { reason, plan, ...rest } = item.args || {};
    return h('li', { class: 'trace-submit' }, h('span', {}, '✅'), h('b', {}, name), ` ${args(rest)}`);
  }
  return h('li', { class: `trace-tool${item.error ? ' failed' : ''}` },
    h('span', {}, '🔧'), h('b', {}, name), item.id ? h('em', {}, item.id) : null,
    args(item.args) ? ` (${args(item.args)})` : '',
    item.ms != null ? h('i', {}, `${item.ms} ms`) : null,
    h('div', { class: 'trace-result' }, item.error || item.summary || ''));
}

export function traceView(trace, extra) {
  if (!trace || !trace.steps || !trace.steps.length) return null;
  const tools = trace.steps.filter((s) => s.kind === 'tool').length;
  const seconds = trace.total_ms != null ? `，${(trace.total_ms / 1000).toFixed(1)} 秒` : '';
  return h('details', { class: 'coach-trace', id: 'coach-trace' },
    h('summary', {}, `思考过程（调用工具 ${tools} 次，LLM ${trace.llm_calls} 次${seconds}）`),
    extra || null,
    h('ol', {}, trace.steps.map(step)));
}

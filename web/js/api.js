// 后端接口封装。出错时抛出 Error，message 是服务端给出的中文提示。

async function request(method, path, body, signal) {
  const response = await fetch(path, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
    signal, // 传入 AbortSignal 可以中途取消（AI 建议用）
  });
  let data = null;
  try {
    data = await response.json();
  } catch {
    // 非 JSON 响应
  }
  if (!response.ok) {
    const detail = typeof data?.detail === 'string' ? data.detail : `请求失败（${response.status}）`;
    const error = new Error(detail);
    error.status = response.status;
    throw error;
  }
  return data;
}

export const api = {
  state: () => request('GET', '/api/state'),
  startMatch: (opponent = 'manual') => request('POST', '/api/match', { opponent }),
  abandon: () => request('DELETE', '/api/match'),
  pick: (side, species) => request('POST', '/api/match/pick', { side, species }),
  // adviceId：操作时面板上正显示着的那条 AI 建议（用来标注是否采纳）；没有显示就不传
  action: (skill, target, adviceId = null) => request('POST', '/api/match/action', { skill, target, advice_id: adviceId }),
  battles: () => request('GET', '/api/battles'),
  battle: (id) => request('GET', `/api/battles/${encodeURIComponent(id)}`),
  challenge: (body) => request('POST', '/api/pve/challenges', body),
  pveRecords: (limit = 50) => request('GET', `/api/pve/records?limit=${limit}`),
  pveRecord: (id) => request('GET', `/api/pve/records/${encodeURIComponent(id)}`),
  // AI Coach：失败也返回 200，用 { ok: false, code, retryable } 表示
  coachStatus: () => request('GET', '/api/coach/status'),
  coachDraft: (token, attempt, signal) => request('POST', '/api/coach/draft', { token, attempt }, signal),
  coachBattle: (token, attempt, signal) => request('POST', '/api/coach/battle', { token, attempt }, signal),
  coachReview: (id, signal) => request('GET', `/api/coach/review/${encodeURIComponent(id)}`, null, signal),
  coachReport: (id, attempt, signal) =>
    request('POST', `/api/coach/review/${encodeURIComponent(id)}/report`, { attempt }, signal),
  coachGrowth: (attempt, signal) => request('POST', '/api/coach/growth', { attempt }, signal),
};

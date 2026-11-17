// AI Coach 的请求控制器：自动重试、取消、丢弃迟到的结果。选将、战斗、复盘讲解、培养建议共用。
//
// 状态（state.status）：
//   idle      没有请求（不是我方回合，或已取消）
//   thinking  请求中；retry 是第几次自动重试（0 表示首次请求）；waiting 表示后台还在计算，正在等
//   ready     拿到建议；result 是服务端返回的完整结果
//   empty     自动重试后仍失败：显示「暂无建议」和重试按钮
//
// 重试只针对当前这一手 / 这一次行动：玩家完成操作或局面推进后调用 cancel()，进行中的请求立即作废。

import { api } from './api.js';
import { sleep } from './dom.js';

export const MAX_RETRIES = 2; // 自动重试次数：每次建议最多请求 3 次
const POLL_MS = 1500; // 服务端回答「后台还在计算」时，过这么久再问
const MAX_POLLS = 200;

// fetch(token, attempt, signal) 发出一次请求，返回服务端的 { ok, ... }
export function createCoach({ fetch, onChange }) {
  let runId = 0; // 单调递增：只有最新一次 request() 的结果才会被采用
  let controller = null;
  let state = { status: 'idle' };
  let disposed = false;

  function set(next) {
    state = next;
    if (!disposed) onChange(state);
  }

  function abort() {
    runId += 1;
    if (controller) controller.abort();
    controller = null;
  }

  async function request(token) {
    abort();
    const mine = runId;
    controller = new AbortController();
    const { signal } = controller;
    let message = 'AI 请求失败';
    let code = null;
    let polls = 0;
    for (let attempt = 1; attempt <= MAX_RETRIES + 1; attempt += 1) {
      set({ status: 'thinking', token, retry: attempt - 1, waiting: polls > 0 });
      let result;
      try {
        result = await fetch(token, attempt, signal);
      } catch (error) {
        if (mine !== runId) return; // 已取消
        result = { ok: false, code: 'network', retryable: true, message: '无法连接服务器' };
      }
      if (mine !== runId) return; // 迟到的建议：丢弃
      if (result.ok) {
        controller = null;
        set({ status: 'ready', token, result });
        return;
      }
      if (result.code === 'stale' || result.code === 'not_your_turn') {
        controller = null;
        set({ status: 'idle' }); // 局面已经变化，不算失败
        return;
      }
      if (result.code === 'pending' && polls < MAX_POLLS) {
        polls += 1; // 后台还在计算：等一会儿再问，不算一次失败
        set({ status: 'thinking', token, retry: attempt - 1, waiting: true });
        await sleep(POLL_MS);
        if (mine !== runId) return;
        attempt -= 1;
        continue;
      }
      message = result.message || message;
      code = result.code;
      if (!result.retryable) break;
    }
    controller = null;
    set({ status: 'empty', token, message, code });
  }

  return {
    request,
    // 手动重试：重新获得一轮自动重试机会
    retry() {
      if (state.token) request(state.token);
    },
    cancel() {
      abort();
      set({ status: 'idle' });
    },
    dispose() {
      disposed = true;
      abort();
    },
    get state() {
      return state;
    },
  };
}

export function createDraftCoach({ onChange }) {
  return createCoach({ fetch: api.coachDraft, onChange });
}

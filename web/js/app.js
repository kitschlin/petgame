// 全局状态：服务端 /api/state 的最新结果，以及几个到处要用的小工具。

import { api } from './api.js';

const listeners = new Set();
let toastTimer = null;

export const app = {
  state: null,

  set(state) {
    this.state = state;
    listeners.forEach((fn) => fn(state));
  },

  async refresh() {
    this.set(await api.state());
    return this.state;
  },

  onChange(fn) {
    listeners.add(fn);
    return () => listeners.delete(fn);
  },

  go(hash) {
    if (location.hash === hash) window.dispatchEvent(new HashChangeEvent('hashchange'));
    else location.hash = hash;
  },

  toast(message, kind = 'info') {
    const el = document.getElementById('toast');
    el.textContent = message;
    el.className = `show ${kind}`;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.className = ''; }, 2600);
  },

  species(key) {
    return this.state.config.species.find((s) => s.key === key);
  },

  sideName(side) {
    return this.state.config.sides.find((s) => s.key === side).name;
  },

  // 选将或战斗进行中：属性锁定，不能进入 PVE
  get locked() {
    return this.state.phase === 'draft' || this.state.phase === 'battle';
  },
};

export const SIDE_STYLE = { me: '迅捷突击', opp: '坚韧控制' };

// 对手方操作模式：上次选的会记住，默认随机
const OPPONENT_KEY = 'petarena.opponent';
export function loadOpponentMode() {
  const saved = localStorage.getItem(OPPONENT_KEY);
  return saved === 'manual' || saved === 'smart' ? saved : 'random';
}
export function saveOpponentMode(mode) {
  localStorage.setItem(OPPONENT_KEY, mode);
}
export const OPPONENT_LABEL = { manual: '手动', random: '随机', smart: '智能' };

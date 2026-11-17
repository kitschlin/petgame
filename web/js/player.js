// 事件播放器：把服务端返回的事件按顺序、带停顿地播出来。实时对战和回放共用同一套节奏。

import { sleep } from './dom.js';

const DELAY = {
  battle_start: 500,
  energy_init: 350,
  speed_bonus: 350,
  shield: 450,
  round_start: 800,
  turn_start: 120,
  await_input: 0,
  action: 500,
  damage: 650,
  ko: 600,
  pursuit_check: 450,
  freeze_check: 400,
  frozen: 900,
  heal: 550,
  hp_cost: 450,
  energy: 450,
  pull: 450,
  barrier_cast: 600,
  barrier_end: 450,
  chain_cast: 600,
  chain_end: 450,
  turn_end: 60,
  round_end: 150,
  battle_end: 400,
};

export function delayOf(event) {
  return DELAY[event.type] ?? 150;
}

export const SPEEDS = [
  { value: 1, label: '1×' },
  { value: 2, label: '2×' },
  { value: 4, label: '4×' },
  { value: 0, label: '跳过动画' },
];

export function loadSpeed() {
  const raw = localStorage.getItem('petarena.speed');
  if (raw === null) return 1; // 没有保存过：正常速度（Number(null) 是 0，会被当成「跳过动画」）
  const saved = Number(raw);
  return SPEEDS.some((s) => s.value === saved) ? saved : 1;
}

export function saveSpeed(value) {
  localStorage.setItem('petarena.speed', String(value));
}

export function wait(event, speed) {
  return speed ? sleep(delayOf(event) / speed) : Promise.resolve();
}

export function createPlayer({ onEvent, onIdle }) {
  const queue = [];
  let running = false;
  let stopped = false;
  let speed = loadSpeed();

  async function loop() {
    running = true;
    while (queue.length && !stopped) {
      const event = queue.shift();
      onEvent(event, speed > 0); // 第二个参数：是否播放动画
      await wait(event, speed);
    }
    running = false;
    if (!stopped) onIdle();
  }

  return {
    push(events) {
      queue.push(...events);
      if (!running) loop();
    },
    setSpeed(value) {
      speed = value;
    },
    stop() {
      stopped = true;
      queue.length = 0;
    },
    get busy() {
      return running;
    },
  };
}

// 入口：路由 + 顶部导航。每次切换页面前先向服务端同步一次状态。

import { app } from './app.js';
import { clear, fill, h } from './dom.js';
import { clearPageTour, mountHelp } from './guide.js';
import * as home from './views/home.js';
import * as pvp from './views/pvp.js';
import * as pve from './views/pve.js';
import * as records from './views/records.js';
import * as replay from './views/replay.js';
import * as review from './views/review.js';

const routes = [
  [/^#?\/?$/, home, 'home'],
  [/^#\/pvp$/, pvp, 'pvp'],
  [/^#\/pve$/, pve, 'pve'],
  [/^#\/records$/, records, 'records'],
  [/^#\/records\/([\w-]+)$/, { mount: records.mountDetail }, 'records'],
  [/^#\/replay\/([\w-]+)$/, replay, 'records'],
  [/^#\/review\/([\w-]+)$/, review, 'records'],
];

const root = document.getElementById('app');
const nav = document.getElementById('nav');
let cleanup = null;
let current = 'home';
let token = 0;

function renderNav() {
  if (!app.state) return;
  const phase = app.state.phase;
  const pvpLabel = phase === 'draft' ? 'PVP 对战 · 选将中' : phase === 'battle' ? 'PVP 对战 · 战斗中' : 'PVP 对战';
  const link = (key, hash, label, attrs = {}) =>
    h('a', { href: hash, class: `nav-link${current === key ? ' active' : ''}`, ...attrs }, label);
  fill(nav,
    link('home', '#/', '首页'),
    link('pvp', '#/pvp', pvpLabel),
    link('pve', '#/pve', 'PVE 历练', app.locked ? { title: '选将和战斗期间不能进入 PVE' } : {}),
    link('records', '#/records', '战斗记录'),
  );
}

async function render() {
  const mine = ++token;
  const hash = location.hash || '#/';
  const route = routes.find(([pattern]) => pattern.test(hash)) || routes[0];
  const params = hash.match(route[0])?.slice(1) || [];
  try {
    await app.refresh();
  } catch (error) {
    app.toast(`无法连接服务器：${error.message}`, 'error');
    return;
  }
  if (mine !== token) return; // 期间又切换了页面
  if (cleanup) cleanup();
  cleanup = null;
  clearPageTour(); // 界面导览属于上一个页面
  clear(root);
  current = route[2];
  renderNav();
  window.scrollTo(0, 0);
  try {
    const result = await route[1].mount(root, ...params);
    if (mine === token) cleanup = result || null;
    else if (result) result();
  } catch (error) {
    console.error(error);
    fill(root, h('div', { class: 'panel empty' }, `页面加载失败：${error.message}`));
  }
}

mountHelp(document.getElementById('help'));
app.onChange(renderNav);
window.addEventListener('hashchange', render);
render();

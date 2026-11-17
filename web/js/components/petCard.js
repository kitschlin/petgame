// 宠物卡片：名字、定位、四项属性、特色技能与被动。用于选将、首页图鉴和 PVE。

import { app } from '../app.js';
import { h } from '../dom.js';

function stat(label, value, title) {
  return h('span', { class: 'pc-stat', title }, h('i', {}, label), h('b', {}, value));
}

function costText(cost) {
  return cost ? `${cost} 豆` : '免费';
}

export function petCard(side, key, stats, options = {}) {
  const species = app.species(key);
  const boosted = stats.battle_speed !== stats.speed;
  const speed = boosted ? `${stats.speed}→${stats.battle_speed}` : stats.speed;
  const speedTitle = boosted
    ? `基础速度 ${stats.speed}，开战后 ${stats.battle_speed}（被动 +100）`
    : `基础速度 ${stats.speed}`;

  return h(
    'div',
    {
      class: `pet-card side-${side} ${options.class || ''}`,
      dataset: { species: key, side },
      ...(options.attrs || {}),
    },
    h('div', { class: 'pc-head' },
      h('span', { class: 'pc-emoji' }, species.emoji),
      h('div', { class: 'pc-title' },
        h('div', { class: 'pc-name' }, species.name),
        h('div', { class: 'pc-role' }, species.role))),
    h('div', { class: 'pc-stats' },
      stat('生命', stats.max_hp, '生命上限'),
      stat('攻击', stats.atk, '攻击'),
      stat('速度', speed, speedTitle),
      stat('命中', `${stats.hit}%`, '效果命中')),
    h('div', { class: 'pc-skill' },
      h('div', { class: 'pc-skill-name' },
        h('b', {}, species.skill.name),
        h('span', { class: 'cost' }, costText(species.skill.cost))),
      h('p', {}, species.skill.desc)),
    h('div', { class: 'pc-skill passive' },
      h('div', { class: 'pc-skill-name' },
        h('b', {}, species.passive.name),
        h('span', { class: 'tag' }, '被动')),
      h('p', {}, species.passive.desc)),
    // 悬停时展开的完整说明
    h('div', { class: `pc-pop ${options.pop === 'up' ? 'up' : 'down'}` },
      h('div', { class: 'pc-pop-title' }, `${species.emoji} ${species.name} · ${species.role}`),
      h('div', {}, h('b', {}, `特色技能【${species.skill.name}】${costText(species.skill.cost)}`)),
      h('p', {}, species.skill.desc),
      h('div', {}, h('b', {}, `被动【${species.passive.name}】`)),
      h('p', {}, species.passive.desc)),
    options.extra,
  );
}

// 只有头像和名字的小卡片：用于站位槽、记录列表
export function petChip(side, key, extra) {
  const species = app.species(key);
  return h('span', { class: `pet-chip side-${side}`, title: `${app.sideName(side)}${species.name}` },
    h('span', { class: 'chip-emoji' }, species.emoji), species.name, extra);
}

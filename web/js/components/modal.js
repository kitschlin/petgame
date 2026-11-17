// 弹窗：结束提示、二次确认。

import { h } from '../dom.js';

export function openModal({ title, body, buttons, className = '' }) {
  const overlay = h('div', { class: `modal-overlay ${className}` },
    h('div', { class: 'modal', role: 'dialog', 'aria-modal': 'true' },
      h('h2', { class: 'modal-title' }, title),
      h('div', { class: 'modal-body' }, body),
      h('div', { class: 'modal-buttons' }, buttons)));
  document.body.append(overlay);
  return { el: overlay, close: () => overlay.remove() };
}

export function confirmDialog(title, message, confirmText = '确定') {
  return new Promise((resolve) => {
    const done = (answer) => { modal.close(); resolve(answer); };
    const modal = openModal({
      title,
      body: message,
      className: 'confirm',
      buttons: [
        h('button', { class: 'btn', dataset: { act: 'cancel' }, onclick: () => done(false) }, '取消'),
        h('button', { class: 'btn danger', dataset: { act: 'confirm' }, onclick: () => done(true) }, confirmText),
      ],
    });
  });
}

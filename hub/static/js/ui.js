// 通用控件：下拉菜单、页面内的确认和输入对话框。
// 规则：带 ▾ 的按钮点开是菜单（选一个）；带圆点的是开关（开或关）；普通按钮做一次动作。
// 下拉菜单包在原生 <select> 外面：select 隐藏起来仍是唯一的数据来源，原来读 .value、监听 change 的代码都不用改。
import { esc } from './api.js';

let openMenu = null;

function closeMenu() {
  if (!openMenu) return;
  openMenu.menu.remove();
  openMenu.btn.classList.remove('open');
  openMenu.btn.setAttribute('aria-expanded', 'false');
  openMenu = null;
}

document.addEventListener('pointerdown', e => {
  if (openMenu && !openMenu.menu.contains(e.target) && !openMenu.btn.contains(e.target)) closeMenu();
}, true);
document.addEventListener('keydown', e => {
  if (!openMenu) return;
  const items = [...openMenu.menu.querySelectorAll('.mi:not(.off)')];
  const i = items.indexOf(document.activeElement);
  if (e.key === 'Escape') { const b = openMenu.btn; closeMenu(); b.focus(); e.preventDefault(); }
  else if (e.key === 'ArrowDown') { (items[i + 1] || items[0])?.focus(); e.preventDefault(); }
  else if (e.key === 'ArrowUp') { (items[i - 1] || items[items.length - 1])?.focus(); e.preventDefault(); }
});
window.addEventListener('resize', closeMenu);
document.addEventListener('scroll', e => { if (openMenu && !openMenu.menu.contains(e.target)) closeMenu(); }, true);

// 在按钮旁边弹出一个菜单。items: [{value, label, desc, on, off}]，onPick(value)
export function popMenu(btn, items, onPick, opts = {}) {
  if (openMenu?.btn === btn && !opts.reopen) { closeMenu(); return; }   // 右键菜单（reopen）每次都重新打开
  closeMenu();
  const menu = document.createElement('div');
  menu.className = 'menu';
  menu.setAttribute('role', 'listbox');
  menu.innerHTML = (opts.title ? `<div class="menu-h">${esc(opts.title)}</div>` : '') + items.map((it, k) => it.sep ? '<div class="menu-sep"></div>' : `
    <button type="button" class="mi${it.on ? ' on' : ''}${it.off ? ' off' : ''}" data-k="${k}" ${it.off ? 'disabled' : ''} role="option" aria-selected="${!!it.on}">
      <span class="ck">${it.on ? '✓' : ''}</span><span class="tx"><span class="lb">${esc(it.label)}</span>${it.desc ? `<span class="ds">${esc(it.desc)}</span>` : ''}</span></button>`).join('');
  document.body.appendChild(menu);
  const r = btn.getBoundingClientRect(), mh = menu.offsetHeight, mw = Math.max(menu.offsetWidth, r.width);
  menu.style.minWidth = r.width + 'px';
  const below = r.bottom + 6 + mh < innerHeight || r.top < mh + 12;
  menu.style.top = (below ? r.bottom + 6 : r.top - mh - 6) + 'px';
  menu.style.left = Math.max(8, Math.min(r.left, innerWidth - mw - 8)) + 'px';
  menu.addEventListener('click', e => {
    const b = e.target.closest('.mi'); if (!b || b.disabled) return;
    closeMenu(); onPick(items[+b.dataset.k].value); btn.focus();
  });
  btn.classList.add('open'); btn.setAttribute('aria-expanded', 'true');
  openMenu = { btn, menu };
  (menu.querySelector('.mi.on') || menu.querySelector('.mi:not(.off)'))?.focus();
}

// 把一个 <select> 换成同样风格的下拉按钮。选项上的 data-desc 会显示成说明
export function enhanceSelect(sel) {
  if (sel.dataset.enhanced) return;
  sel.dataset.enhanced = '1';
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'dd' + (sel.dataset.cls ? ' ' + sel.dataset.cls : '');
  btn.title = sel.title || '';
  btn.setAttribute('aria-haspopup', 'listbox');
  sel.after(btn);
  sel.classList.add('dd-native');
  const label = () => {
    const o = sel.options[sel.selectedIndex];
    btn.innerHTML = `<span class="dd-lb">${esc(o ? (o.dataset.short || o.textContent) : '')}</span><span class="dd-cv">▾</span>`;
    btn.disabled = sel.disabled;
  };
  label();
  // 代码直接改 sel.value 时不会触发事件，这里接住 value 的设置，按钮文字跟着变
  const desc = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value');
  Object.defineProperty(sel, 'value', { get() { return desc.get.call(this); }, set(v) { desc.set.call(this, v); label(); } });
  new MutationObserver(label).observe(sel, { childList: true, subtree: true, attributes: true });
  sel.addEventListener('change', label);
  btn.addEventListener('click', () => {
    const items = [...sel.options].map(o => ({ value: o.value, label: o.textContent, desc: o.dataset.desc || '', on: o.selected, off: o.disabled }));
    popMenu(btn, items, v => { sel.value = v; sel.dispatchEvent(new Event('change', { bubbles: true })); }, { title: sel.dataset.menuTitle });
  });
}

export function enhanceAll(root = document) {
  root.querySelectorAll('select:not([data-native])').forEach(enhanceSelect);
}

// ---------------------------------------------------------------- 页面内对话框（代替浏览器的 confirm / prompt）
function dialog({ title, text = '', input = null, ok = '确定', danger = false, cancel = '取消' }) {
  return new Promise(resolve => {
    const wrap = document.createElement('div');
    wrap.className = 'dlg-wrap';
    wrap.innerHTML = `<div class="dlg" role="dialog" aria-modal="true">
      ${title ? `<div class="dlg-t">${esc(title)}</div>` : ''}${text ? `<div class="dlg-x">${esc(text)}</div>` : ''}
      ${input ? `<input class="dlg-in" value="${esc(input.value || '')}" placeholder="${esc(input.placeholder || '')}">` : ''}
      <div class="dlg-b"><button type="button" class="btn" data-a="no">${esc(cancel)}</button>
        <button type="button" class="btn ${danger ? 'danger' : 'primary'}" data-a="ok">${esc(ok)}</button></div></div>`;
    document.body.appendChild(wrap);
    const inp = wrap.querySelector('.dlg-in');
    const done = v => { wrap.remove(); document.removeEventListener('keydown', key, true); resolve(v); };
    const yes = () => done(input ? inp.value.trim() : true);
    const no = () => done(input ? null : false);
    const key = e => {
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); no(); }
      if (e.key === 'Enter' && !e.shiftKey && (inp ? document.activeElement === inp : true)) { e.preventDefault(); e.stopPropagation(); yes(); }
    };
    document.addEventListener('keydown', key, true);
    wrap.addEventListener('pointerdown', e => { if (e.target === wrap) no(); });
    wrap.querySelector('[data-a=ok]').onclick = yes;
    wrap.querySelector('[data-a=no]').onclick = no;
    requestAnimationFrame(() => (inp ? (inp.focus(), inp.select()) : wrap.querySelector('[data-a=ok]').focus()));
  });
}

export const confirmBox = (title, text = '', opts = {}) => dialog({ title, text, ...opts });
export const promptBox = (title, opts = {}) => dialog({ title, text: opts.text || '', input: { value: opts.value, placeholder: opts.placeholder }, ok: opts.ok || '确定' });

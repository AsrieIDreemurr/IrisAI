// 独立窗口（无边框）里，网页顶栏当标题栏用：拖动、双击最大化、右上角最小化 / 最大化 / 关闭，四边和四角拖动调大小。
// 只在独立窗口里生效（有 window.pywebview）；用浏览器打开时什么都不做。
import { $ } from './api.js';

const MIN_W = 900, MIN_H = 600;

function ready() {
  return new Promise(resolve => {
    if (window.pywebview?.api?.drag) return resolve(window.pywebview.api);
    window.addEventListener('pywebviewready', () => resolve(window.pywebview.api), { once: true });
    setTimeout(() => resolve(window.pywebview?.api?.drag ? window.pywebview.api : null), 3000);
  });
}

const ICONS = {
  min: '<svg viewBox="0 0 10 10"><path d="M1 5.5h8" stroke="currentColor" stroke-width="1.1"/></svg>',
  max: '<svg viewBox="0 0 10 10"><rect x="1.5" y="1.5" width="7" height="7" rx="1.2" fill="none" stroke="currentColor" stroke-width="1.1"/></svg>',
  restore: '<svg viewBox="0 0 10 10"><rect x="1.5" y="3" width="5.5" height="5.5" rx="1" fill="none" stroke="currentColor" stroke-width="1.1"/><path d="M3.5 3V2.2c0-.4.3-.7.7-.7h4c.4 0 .7.3.7.7v4c0 .4-.3.7-.7.7H7" fill="none" stroke="currentColor" stroke-width="1.1"/></svg>',
  close: '<svg viewBox="0 0 10 10"><path d="M1.5 1.5l7 7M8.5 1.5l-7 7" stroke="currentColor" stroke-width="1.1"/></svg>',
};

export async function initWinFrame() {
  const api = await ready();
  if (!api) return;
  const root = document.documentElement;
  root.classList.add('in-app');

  // 右上角三个按钮
  const ctl = document.createElement('div');
  ctl.className = 'win-ctl';
  ctl.innerHTML = `<button type="button" data-w="min" title="最小化">${ICONS.min}</button>
    <button type="button" data-w="max" title="最大化">${ICONS.max}</button>
    <button type="button" data-w="close" class="x" title="关闭（缩到右下角托盘）">${ICONS.close}</button>`;
  $('.titlebar').appendChild(ctl);
  const setMax = m => {
    root.classList.toggle('win-max', m);
    const b = ctl.querySelector('[data-w=max]');
    b.innerHTML = m ? ICONS.restore : ICONS.max; b.title = m ? '还原' : '最大化';
  };
  const toggleMax = async () => setMax(await api.toggle_max());
  // 最大化的状态以窗口那边为准：页面刷新、在入口页和工作台之间切换后，以及窗口大小变了（系统、快捷键挪过），都重新问一次
  const sync = async () => { try { setMax(!!await api.is_max()); } catch (e) {} };
  sync();
  let syncTimer = 0;
  addEventListener('resize', () => { clearTimeout(syncTimer); syncTimer = setTimeout(sync, 250); });
  ctl.addEventListener('click', e => {
    const b = e.target.closest('[data-w]'); if (!b) return;
    if (b.dataset.w === 'min') api.minimize();
    if (b.dataset.w === 'max') toggleMax();
    if (b.dataset.w === 'close') api.close();
  });

  // 顶栏空白处：按住拖动，双击最大化 / 还原。按钮、搜索框这些可以点的地方不算。
  // 以前交给 Windows 原生的“按住标题栏拖动”（api.drag），但调用是在后台线程发的，鼠标捕获在网页控件手里，经常拖不动（2026-11-08）。
  // 现在和调大小一样：网页跟着鼠标算位置交给窗口。最大化时一拖先还原；松手时鼠标在屏幕最上沿就最大化。
  const bar = $('.titlebar');
  const interactive = t => t.closest('button, a, input, select, textarea, .search, .win-ctl, [data-open-cust]');
  bar.addEventListener('pointerdown', async e => {
    if (e.button !== 0 || e.detail > 1 || interactive(e.target)) return;
    e.preventDefault();
    bar.setPointerCapture(e.pointerId);
    const sx = e.screenX, sy = e.screenY;
    let base = null, moved = false, pending = null, busy = false, up = false;
    const apply = async () => {
      if (busy || !pending) return;
      busy = true; const b = pending; pending = null;
      await api.set_bounds(...b);
      busy = false; if (pending) apply();
    };
    const move = async ev => {
      const dx = ev.screenX - sx, dy = ev.screenY - sy;
      if (!moved && Math.hypot(dx, dy) < 4) return;          // 抖一下不算拖
      if (!moved) {
        moved = true;
        if (root.classList.contains('win-max')) {             // 最大化时：先还原，窗口横向按原来的比例放在鼠标下面
          const [mx, , mw] = await api.bounds();
          setMax(await api.toggle_max());
          const [, , w, h] = await api.bounds();
          const fx = (sx - mx) / mw;
          base = [sx - w * fx, sy - 16, w, h];
        } else base = await api.bounds();
      }
      if (!base || up) return;
      pending = [base[0] + dx, base[1] + dy, base[2], base[3]]; apply();
    };
    const end = ev => {
      up = true;
      bar.removeEventListener('pointermove', move); bar.removeEventListener('pointerup', end); bar.removeEventListener('pointercancel', end);
      if (moved && ev.screenY <= 0 && !root.classList.contains('win-max')) toggleMax();   // 拖到屏幕顶上松手 = 最大化
    };
    bar.addEventListener('pointermove', move); bar.addEventListener('pointerup', end); bar.addEventListener('pointercancel', end);
  });
  bar.addEventListener('dblclick', e => { if (!interactive(e.target)) toggleMax(); });

  // 四边和四角：拖动调大小。网页按鼠标位置算新的位置和大小，交给窗口
  const edges = ['n', 's', 'e', 'w', 'ne', 'nw', 'se', 'sw'];
  for (const d of edges) {
    const h = document.createElement('div');
    h.className = 'win-rs win-rs-' + d;
    document.body.appendChild(h);
    h.addEventListener('pointerdown', async e => {
      if (root.classList.contains('win-max')) return;
      e.preventDefault();
      h.setPointerCapture(e.pointerId);
      const [x0, y0, w0, h0] = await api.bounds();
      const sx = e.screenX, sy = e.screenY;
      let pending = null, busy = false;
      const apply = async () => {
        if (busy || !pending) return;
        busy = true; const b = pending; pending = null;
        await api.set_bounds(...b);
        busy = false; if (pending) apply();
      };
      const move = ev => {
        const dx = ev.screenX - sx, dy = ev.screenY - sy;
        let x = x0, y = y0, w = w0, hh = h0;
        if (d.includes('e')) w = Math.max(MIN_W, w0 + dx);
        if (d.includes('s')) hh = Math.max(MIN_H, h0 + dy);
        if (d.includes('w')) { w = Math.max(MIN_W, w0 - dx); x = x0 + w0 - w; }
        if (d.includes('n')) { hh = Math.max(MIN_H, h0 - dy); y = y0 + h0 - hh; }
        pending = [x, y, w, hh]; apply();
      };
      const up = () => { h.removeEventListener('pointermove', move); h.removeEventListener('pointerup', up); h.removeEventListener('pointercancel', up); };
      h.addEventListener('pointermove', move); h.addEventListener('pointerup', up); h.addEventListener('pointercancel', up);
    });
  }
  setMax(await api.is_max());
}

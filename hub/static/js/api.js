// 和后端通信、几个通用小工具。
// 服务连不上（网页服务在重启、断网）：fetch 直接抛 TypeError。统一换成看得懂的话，弹一次提示（8 秒内不重复），
// 状态栏也显示“连不上”（js/status.js 听 net 事件）。后台定时刷新传 quiet: true，只改状态栏、不弹提示（不然断网时每 8 秒弹一次）。没人接这个错误的地方（新对话、换模型）不再在控制台报未处理的错误
export const net = { down: false };
let netToast = 0;
function netDown(quiet) {
  if (!net.down) { net.down = true; emit('net', { down: true }); }
  if (!quiet && Date.now() - netToast > 8000) { netToast = Date.now(); toast('连不上服务（可能在重启），稍后再试', 3500); }
  const e = new Error('连不上服务（可能在重启），稍后再试'); e.netDown = true; return e;
}
addEventListener('unhandledrejection', e => { if (e.reason?.netDown) e.preventDefault(); });

export async function api(path, opts = {}) {
  const { quiet, ...rest } = opts;
  const init = { headers: {}, ...rest };
  if (opts.body && typeof opts.body !== 'string') { init.body = JSON.stringify(opts.body); init.headers['Content-Type'] = 'application/json'; }
  let r;
  try { r = await fetch(path, init); }
  catch (e) { if (e?.name === 'AbortError') throw e; throw netDown(quiet); }
  if (net.down) { net.down = false; emit('net', { down: false }); }
  if (r.status === 401) { location.href = '/login'; throw new Error('需要登录'); }
  const data = r.headers.get('content-type')?.includes('json') ? await r.json() : await r.text();
  if (!r.ok) throw new Error(data?.detail || data?.error || `请求失败 ${r.status}`);
  return data;
}

// 订阅一次运行的事件流。返回关闭函数。
export function events(runId, onEvent, start = 0) {
  const es = new EventSource(`/api/runs/${runId}/events?start=${start}`);
  let cursor = start - 1;
  es.onmessage = e => { const ev = JSON.parse(e.data); const seq = Number(e.lastEventId || ev.seq); if (Number.isFinite(seq)) { if (seq <= cursor) return; cursor = seq; } onEvent(ev); if (ev.type === 'done') es.close(); };
  es.onerror = () => { if (es.readyState === EventSource.CLOSED) onEvent({ type: 'lost' }); };
  watchStream(es, s => onEvent({ type: 'net', ...s }));
  return () => es.close();
}

// 事件流断了、浏览器在自己重连时告诉页面（以前页面上看不出来，回答像卡住了）：
// {state: 'reconnecting', last: 最后收到内容的时间}；重新连上时 {state: 'ok'}。彻底连不上时还是各页面自己的 lost
export function watchStream(es, cb) {
  let last = Date.now(), lost = false;
  es.addEventListener('message', () => { last = Date.now(); });
  es.addEventListener('error', () => { if (es.readyState === EventSource.CONNECTING && !lost) { lost = true; cb({ state: 'reconnecting', last }); } });
  es.addEventListener('open', () => { if (lost) { lost = false; cb({ state: 'ok' }); } });
}

// 页面上的重连提示：“连接断了，正在重连…（最后收到内容 12:01:33，35 秒前）”，秒数每秒更新。
// show(html) 显示，返回停止函数（连上、彻底断开、跑完时调用）
export function reconnectNote(last, show) {
  const t = new Date(last).toLocaleTimeString('zh-CN', { hour12: false });
  const paint = () => show(`<span class="spin"></span>连接断了，正在重连…（最后收到内容 ${t}，${Math.max(0, Math.round((Date.now() - last) / 1000))} 秒前）`);
  paint();
  const id = setInterval(paint, 1000);
  return () => clearInterval(id);
}

export const bus = new EventTarget();
export const emit = (name, detail) => bus.dispatchEvent(new CustomEvent(name, { detail }));
export const on = (name, fn) => bus.addEventListener(name, e => fn(e.detail));

export const $ = (s, root = document) => root.querySelector(s);
export const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
export const fname = p => String(p).split(/[\\/]/).pop();

export function toast(text, ms = 2200) {
  const t = document.createElement('div');
  t.className = 'toast'; t.textContent = text; document.body.appendChild(t);
  setTimeout(() => t.remove(), ms);
}

export function md(text) {
  const html = window.marked.parse(text || '', { breaks: true, gfm: true });
  const clean = window.DOMPurify.sanitize(html);
  const box = document.createElement('div');
  box.innerHTML = clean;
  box.querySelectorAll('a[href]').forEach(a => { a.target = '_blank'; a.rel = 'noopener noreferrer'; });
  return box.innerHTML;
}

export const settings = { data: null };
export async function saveSettings(patch) {
  settings.data = await api('/api/settings', { method: 'PUT', body: patch });
  return settings.data;
}

// 键盘可用：开关（span.chip）、活动栏图标、侧栏分区标题不是原生按钮，自动补上 tabindex 和角色，Enter / 空格当点击。
// 开关的 aria-checked 跟着 .on 走。自己处理了按键的（调用了 preventDefault）不会重复触发
const KBD = 'span.chip, .activity a[data-mod], .sec-h';
function kbdPrep(el) {
  if (!el.hasAttribute('tabindex')) el.tabIndex = 0;
  const chip = el.matches('span.chip');
  if (!el.hasAttribute('role')) el.setAttribute('role', chip ? 'switch' : 'button');
  if (chip) { el.setAttribute('aria-checked', el.classList.contains('on')); el.setAttribute('aria-disabled', el.classList.contains('off')); }
  if (el.matches('.sec-h')) el.setAttribute('aria-expanded', !el.parentElement.classList.contains('closed'));
}
function kbdScan(root) {
  if (root.nodeType !== 1) return;
  if (root.matches(KBD)) kbdPrep(root);
  root.querySelectorAll(KBD).forEach(kbdPrep);
}
if (typeof document !== 'undefined') {
  const start = () => {
    kbdScan(document.body);
    new MutationObserver(list => {
      for (const m of list) {
        if (m.type === 'childList') m.addedNodes.forEach(kbdScan);
        else if (m.target.matches(KBD)) kbdPrep(m.target);
        else if (m.target.classList.contains('section')) { const h = m.target.querySelector(':scope > .sec-h'); if (h) kbdPrep(h); }
      }
    }).observe(document.body, { subtree: true, childList: true, attributes: true, attributeFilter: ['class'] });
  };
  if (document.body) start(); else document.addEventListener('DOMContentLoaded', start);
  document.addEventListener('keydown', e => {
    if (e.defaultPrevented || (e.key !== 'Enter' && e.key !== ' ') || !e.target.matches?.(KBD)) return;
    e.preventDefault(); e.target.click();
  });
}

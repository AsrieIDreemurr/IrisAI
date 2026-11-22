// 用 Node 运行网页里的真实脚本：画布、窗口、事件都用空壳代替，按帧推进动画，定时读出变量。
// 用法：node page_harness.js 任务.json
// 任务：{ html, frames, every, watch: [表达式], actions: [{frame, code}], width, height }
const fs = require('fs');
const job = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const html = fs.readFileSync(job.html, 'utf8');
const W = job.width || 1280, H = job.height || 720;

const scripts = [...html.matchAll(/<script(?![^>]*\bsrc=)([^>]*)>([\s\S]*?)<\/script>/gi)]
  .filter(m => !/type\s*=\s*["'](?!module|text\/javascript|application\/javascript)/i.test(m[1]))
  .map(m => m[2]);
if (!scripts.length) { console.log('网页里没有内嵌脚本'); process.exit(1); }
// 3D 网页（WebGL / three.js）在这里没有真实的图形环境，跑出来的结果不可信，直接说明
if (/getContext\(\s*['"](webgl2?|experimental-webgl)['"]|\bTHREE\.|three(\.module)?(\.min)?\.js|WebGLRenderer/.test(html)) {
  console.log('这个网页用到 WebGL / three.js，run_js 的运行器没有真实的 3D 图形环境，不能运行它。' +
              '请改用 view_result：用 watch 在真实浏览器里读变量（例如 ball.position.y），用 actions 模拟拖动，同时截图看效果。');
  process.exit(2);
}

// ---- 空壳 ----
const listeners = [];   // {target, type, fn}
const noop = new Proxy(function () {}, { get: (t, k) => k === Symbol.toPrimitive ? () => 0 : noop, apply: () => noop });
const gradient = () => ({ addColorStop() {} });
const ctx = new Proxy({ canvas: null }, {
  get(t, k) {
    if (k in t) return t[k];
    if (k === 'createRadialGradient' || k === 'createLinearGradient' || k === 'createConicGradient') return gradient;
    if (k === 'measureText') return () => ({ width: 10 });
    if (k === 'getImageData' || k === 'createImageData') return () => ({ data: new Uint8ClampedArray(4) });
    return noop;
  },
  set(t, k, v) { t[k] = v; return true; },
});
function element(tag, id) {
  const el = {
    tagName: String(tag).toUpperCase(), id, width: W, height: H, style: {}, dataset: {}, children: [], textContent: '', innerHTML: '', value: '',
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    getContext: () => ctx, addEventListener(type, fn) { listeners.push({ target: el, type, fn }); }, removeEventListener() {},
    getBoundingClientRect: () => ({ left: 0, top: 0, x: 0, y: 0, width: W, height: H, right: W, bottom: H }),
    appendChild(c) { el.children.push(c); return c; }, removeChild() {}, setAttribute() {}, getAttribute: () => null,
    querySelector: () => element('div'), querySelectorAll: () => [], focus() {}, blur() {},
    // 点击只触发这个元素自己的监听（以及 onclick），再冒泡到 document / window
    click() { dispatch(el, 'click', {}); },
    dispatchEvent(ev) { dispatch(el, ev.type, ev); return true; },
  };
  return el;
}
const els = {};
// getElementById('start') 和 querySelector('#start') 要拿到同一个对象
const getEl = key => { const id = String(key).replace(/^#/, ''); return (els[id] = els[id] || element(/canvas/i.test(html) ? 'canvas' : 'div', id)); };
ctx.canvas = getEl('__canvas');
const document = {
  getElementById: getEl, querySelector: sel => getEl(sel), querySelectorAll: () => [], createElement: tag => element(tag),
  addEventListener(type, fn) { listeners.push({ target: document, type, fn }); }, removeEventListener() {},
  body: element('body'), documentElement: element('html'), readyState: 'complete',
};
let now = 0, rafQueue = [], timers = [], timerId = 0;
const window = {
  innerWidth: W, innerHeight: H, devicePixelRatio: 1, document,
  addEventListener(type, fn) { listeners.push({ target: window, type, fn }); }, removeEventListener() {},
  getComputedStyle: () => ({ getPropertyValue: () => '' }), matchMedia: () => ({ matches: false, addEventListener() {} }),
};
const requestAnimationFrame = fn => { rafQueue.push(fn); return rafQueue.length; };
const cancelAnimationFrame = () => {};
const setTimeout = (fn, ms = 0) => { timers.push({ id: ++timerId, at: now + ms, fn }); return timerId; };
const setInterval = (fn, ms = 16) => { const id = ++timerId; const t = { id, at: now + ms, fn, every: Math.max(ms, 1) }; timers.push(t); return id; };
const clearTimeout = id => { timers = timers.filter(t => t.id !== id); };
const performance = { now: () => now };
// Date 也按模拟时间走：脚本常用 Date.now() 计时，用真实时钟的话 200 帧几毫秒就跑完了
const T0 = Date.now();
class FakeDate extends Date {
  constructor(...a) { if (a.length) super(...a); else super(T0 + now); }
  static now() { return T0 + now; }
}
const logs = [];
const console_ = { log: (...a) => logs.push(a.join(' ')), warn: (...a) => logs.push('警告 ' + a.join(' ')), error: (...a) => logs.push('错误 ' + a.join(' ')), info() {}, debug() {} };
const Image = function () { return element('img'); };

function mkEvent(type, props) {
  const ev = { type, preventDefault() {}, stopPropagation() {}, clientX: 0, clientY: 0, button: 0, buttons: 1, ...props };
  if (ev.touches === undefined && type.startsWith('touch')) ev.touches = [{ clientX: ev.clientX, clientY: ev.clientY }];
  return ev;
}
function dispatch(el, type, props) {
  const ev = mkEvent(type, { ...props, target: el, currentTarget: el });
  for (const l of listeners.filter(l => l.type === type && l.target === el)) l.fn(ev);
  if (typeof el['on' + type] === 'function') el['on' + type](ev);
  for (const l of listeners.filter(l => l.type === type && (l.target === document || l.target === window))) l.fn(ev);
}
// __fire('click', { target: 'start' })：只点 id 为 start 的元素；不写 target 时发给所有监听（拖动画布等）
function fire(type, props = {}) {
  if (props.target != null) { dispatch(typeof props.target === 'string' ? getEl(props.target) : props.target, type, props); return; }
  const ev = mkEvent(type, props);
  for (const l of listeners.filter(l => l.type === type)) l.fn(ev);
}
function frame() {
  now += 1000 / 60;
  const due = timers.filter(t => t.at <= now);
  for (const t of due) { t.fn(); if (t.every) t.at += t.every; else timers = timers.filter(x => x !== t); }
  const q = rafQueue; rafQueue = [];
  for (const fn of q) fn(now);
}

// ---- 运行 ----
const names = { window, document, requestAnimationFrame, cancelAnimationFrame, setTimeout, setInterval, clearTimeout, clearInterval: clearTimeout,
                performance, Date: FakeDate, console: console_, Image, __fire: fire, __frame: frame, self: window, globalThis: window, navigator: { userAgent: 'node' } };
let evaluate;
try {
  evaluate = new Function(...Object.keys(names), scripts.join('\n;\n') + '\n;return (__e) => eval(__e);')(...Object.values(names));
  for (const l of listeners.filter(l => l.type === 'load' || l.type === 'DOMContentLoaded')) l.fn({});
} catch (e) {
  console.log('脚本加载出错：' + e.message); process.exit(1);
}
const fmt = v => typeof v === 'number' ? Math.round(v * 100) / 100 : v;
const watch = job.watch || [];
function report(f) {
  const vals = watch.map(w => {
    try { return `${w} = ${JSON.stringify(evaluate(w), (k, v) => fmt(v))}`; } catch (e) { return `${w} = 出错：${e.message}`; }
  });
  console.log(`帧 ${f}：` + (vals.join('；') || '（没有指定要看的变量）'));
}
const frames = Math.min(job.frames ?? 120, 6000), every = Math.max(job.every || 10, 1);
const actions = (job.actions || []).slice().sort((a, b) => a.frame - b.frame);
report(0);
try {
  for (let f = 1; f <= frames; f++) {
    for (const a of actions.filter(a => a.frame === f)) { evaluate(a.code); console.log(`帧 ${f}：执行 ${a.code}`); }
    frame();
    if (f % every === 0 || f === frames) report(f);
  }
} catch (e) {
  console.log('运行出错：' + e.message); process.exit(1);
}
if (logs.length) console.log('页面输出（最后 20 条）：\n' + logs.slice(-20).join('\n'));

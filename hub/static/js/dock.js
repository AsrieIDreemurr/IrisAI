// 任务面板：固定在输入框上方，显示当前对话的计划和验收。平时只有一行，点开看详情。
import { $, emit, esc } from './api.js';

const LEVEL = {
  quick: ['快速检查', '不写验收标准，写完只做语法检查'],
  standard: ['标准检查', '先定验收标准，做完逐条实际验证'],
  strict: ['严格检查', '标准检查之外，再请独立验收员只看证据复判'],
};
const CLS = { '通过': 'ok', '不通过': 'err', '待验证': 'todo', '待复验': 'todo' };
let st = {};            // {plan, note, criteria, unfinished, level, review}
let live = false;
let tab = null;
try { tab = localStorage.getItem('dock-tab') || null; } catch (e) {}

const pre = t => `<pre class="mono">${esc(t)}</pre>`;
const shot = p => `<img src="/api/files/raw?path=${encodeURIComponent(p)}" data-zoom="1" alt="" loading="lazy">`;

function bar() {
  const pills = [];
  const plan = st.plan || [];
  if (plan.length) {
    const done = plan.filter(i => i.status === 'completed').length;
    const cur = plan.find(i => i.status === 'in_progress');
    const icon = done === plan.length ? '<span class="st ok">✓</span>' : cur && live ? '<span class="spin"></span>' : '<span class="st">○</span>';
    pills.push(`<button class="pill${tab === 'plan' ? ' on' : ''}" data-tab="plan">${icon}<b>计划</b><span class="num">${done}/${plan.length}</span>
      ${cur ? `<span class="cur">${esc(cur.content)}</span>` : ''}</button>`);
  }
  const crit = st.criteria || [];
  if (crit.length || st.unfinished?.length) {
    const ok = crit.filter(c => c.status === '通过').length;
    const bad = crit.some(c => c.status === '不通过') || st.unfinished?.length;
    const kind = st.review?.status === 'running' ? 'run' : bad ? 'err' : crit.length && ok === crit.length ? 'ok' : 'todo';
    const icon = { run: '<span class="spin"></span>', err: '<span class="st err">!</span>', ok: '<span class="st ok">✓</span>', todo: '<span class="st wait">○</span>' }[kind];
    const extra = st.review?.status === 'running' ? '独立验收中' : st.unfinished?.length ? '有未完成' : st.review?.issues?.length ? '验收员有异议' : '';
    pills.push(`<button class="pill acc-${kind}${tab === 'acc' ? ' on' : ''}" data-tab="acc">${icon}<b>验收</b><span class="num">${ok}/${crit.length} 通过</span>
      ${extra ? `<span class="cur">${extra}</span>` : ''}</button>`);
  }
  return pills.join('');
}

function planBody() {
  const icon = { completed: '<span class="st ok">✓</span>', in_progress: live ? '<span class="spin"></span>' : '<span class="st">●</span>', pending: '<span class="st">○</span>' };
  return `<div class="dock-h"><b>计划</b><span class="sp"></span><button class="icon-btn" data-close title="收起">×</button></div>` +
    (st.plan || []).map(i => `<div class="pi ${i.status}">${icon[i.status] || ''}<span>${esc(i.content)}</span></div>`).join('') +
    (st.note ? `<div class="pnote">${esc(st.note)}</div>` : '') +
    ((st.findings || []).length ? `<div class="label" style="margin-top:6px">已确认的事实</div><ul class="finds">${st.findings.map(f => `<li>${esc(f)}</li>`).join('')}</ul>` : '');
}

function accBody() {
  const lv = LEVEL[st.level] || LEVEL.standard;
  const rv = st.review;
  const rvText = !rv ? '' : rv.status === 'running' ? '独立验收员正在看证据……' : rv.status === 'error' ? rv.text
    : rv.issues?.length ? `独立验收员有 ${rv.issues.length} 条异议：` : '独立验收员复判：全部通过';
  const rvList = rv?.issues?.length ? `<ol>${rv.issues.map(x => `<li>${esc(x)}</li>`).join('')}</ol>` : '';
  return `<div class="dock-h"><b>验收</b><span class="lv" title="${esc(lv[1])}">${lv[0]} · ${esc(lv[1])}</span><span class="sp"></span>
      <button class="icon-btn" data-close title="收起">×</button></div>` +
    (rvText ? `<div class="rv ${rv.issues?.length || rv.status === 'error' ? 'bad' : rv.status === 'running' ? '' : 'good'}">${esc(rvText)}${rvList}</div>` : '') +
    (st.criteria || []).map((c, i) => `<details class="acc ${CLS[c.status] || 'todo'}"><summary><span class="tag">${esc(c.status)}</span><span class="std">${i + 1}. ${esc(c.standard)}</span></summary>
      <div class="acc-body"><div class="label">验证方法：${esc(c.method)}</div>${c.need?.length ? `<div class="label">要的证据：${c.need.map(esc).join('、')}</div>` : ''}${c.note ? `<div>${esc(c.note)}</div>` : ''}
      ${(c.evidence || []).map(e => `<div class="ev"><div class="label">证据：${esc(e.title)}</div>${pre(e.summary)}
        ${(e.images || []).length ? `<div class="shots">${e.images.map(shot).join('')}</div>` : ''}</div>`).join('')}</div></details>`).join('') +
    (st.unfinished?.length ? `<div class="acc-unf">${st.unfinished.map(u => `<div>未完成：${esc(u)}</div>`).join('')}</div>` : '');
}

function render() {
  const has = (st.plan || []).length || (st.criteria || []).length || (st.unfinished || []).length;
  $('#dock').hidden = !has;
  if (!has) return;
  if (tab === 'plan' && !(st.plan || []).length) tab = null;
  if (tab === 'acc' && !(st.criteria || []).length && !(st.unfinished || []).length) tab = null;
  $('#dock-bar').innerHTML = bar();
  const body = $('#dock-body');
  const keep = [...body.querySelectorAll('details[open]')].map(d => d.querySelector('.std')?.textContent);
  body.hidden = !tab;
  body.innerHTML = tab === 'plan' ? planBody() : tab === 'acc' ? accBody() : '';
  body.querySelectorAll('details').forEach(d => { if (keep.includes(d.querySelector('.std')?.textContent)) d.open = true; });
}

function pulse(name) {
  const p = $(`#dock-bar .pill[data-tab="${name}"]`);
  if (p) { p.classList.remove('pulse'); void p.offsetWidth; p.classList.add('pulse'); }
}

export const dock = {
  load(state) { st = { ...(state || {}) }; st.review = null; render(); },
  setLive(b) { live = b; render(); },
  level(l) { st.level = l; if (tab === 'acc') render(); },
  plan(items, note, findings) { st.plan = items || []; st.note = note || ''; if (findings) st.findings = findings; render(); pulse('plan'); },
  acc(criteria, unfinished) { st.criteria = criteria || []; st.unfinished = unfinished || []; render(); pulse('acc'); },
  review(ev) { st.review = ev; render(); pulse('acc'); },
  open(name) { tab = name; try { localStorage.setItem('dock-tab', tab || ''); } catch (e) {} render(); },
};

export function initDock() {
  $('#dock').addEventListener('click', e => {
    const z = e.target.closest('img[data-zoom]'); if (z) { emit('zoom', z.src); return; }
    const p = e.target.closest('.pill');
    if (p) { dock.open(tab === p.dataset.tab ? null : p.dataset.tab); return; }
    if (e.target.closest('[data-close]')) dock.open(null);
  });
}

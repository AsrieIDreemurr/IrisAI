// 人格的记忆页面（知识库 → 记忆层 → “打开记忆”）：时间线、常识、网络图、已淡忘、整理记录。
// 数据：GET /api/memory/notes?persona=；操作：钉住 / 忘掉 / 恢复 / 现在整理。后端 hub\app\memory.py。
import { api, emit, esc, toast } from './api.js';
import { confirmBox } from './ui.js';

let wrap = null, persona = null, data = null, tab = 'time', sel = null;

const pct = x => Math.round(x * 100);
const day = t => t.slice(0, 10);

function strengthBar(n) {
  return `<span class="mp-bar" title="记忆强度 ${pct(n.strength)}%（被想起 ${n.recalls} 次；越久没想起越弱，重要的和钉住的忘得慢）"><b style="width:${pct(n.strength)}%"></b></span>`;
}

function row(n) {
  return `<div class="mp-row${n.pinned ? ' pin' : ''}${sel === n.id ? ' sel' : ''}" data-id="${esc(n.id)}">
    <span class="mp-kind ${n.kind === '常识' ? 'fact' : ''}">${esc(n.kind)}</span>
    <div class="mp-main"><div>${esc(n.text)}</div>
      ${n.quote ? `<small>原话：「${esc(n.quote)}」</small>` : ''}
      <small>${esc(n.time)} · 重要度 ${n.imp} · ${esc(n.who || '')}</small></div>
    ${strengthBar(n)}
    <span class="mp-acts">
      <button class="btn sm" data-pin="${esc(n.id)}" data-v="${n.pinned ? 0 : 1}" title="钉住的记忆不会淡忘，每轮都会想起">${n.pinned ? '取消钉住' : '钉住'}</button>
      <button class="btn sm" data-edit="${esc(n.path)}">改</button>
      <button class="btn sm" data-forget="${esc(n.id)}">忘掉</button></span></div>`;
}

function timeline(list) {
  if (!list.length) return '<div class="hint">还没有记忆。在开了这个人格的对话里聊几句就会有。</div>';
  const groups = {};
  for (const n of list) (groups[day(n.time)] ||= []).push(n);
  return Object.entries(groups).map(([d, ns]) => `<div class="mp-day"><div class="mp-date">${esc(d)}</div>${ns.map(row).join('')}</div>`).join('');
}

// 网络图：每条记忆一个点（大小 = 强度，颜色 = 经历 / 常识），突触是连线（粗细 = 连接强度）。简单的力导向布局，算 300 轮。
function graph() {
  const nodes = [...data.notes].sort((a, b) => b.strength - a.strength).slice(0, 120);
  const ids = new Set(nodes.map(n => n.id));
  const links = data.links.filter(l => ids.has(l.a) && ids.has(l.b));
  if (!nodes.length) return '<div class="hint">还没有记忆。</div>';
  const W = 760, H = 460, P = {};
  nodes.forEach((n, i) => { const a = i * 2.4; P[n.id] = { x: W / 2 + Math.cos(a) * (40 + i * 2), y: H / 2 + Math.sin(a) * (40 + i * 1.4), vx: 0, vy: 0 }; });
  // 斥力随点数调：n 个点各推 C/d，和往中间的拉力 0.02·r 在 r ≈ 180 处平衡
  const C = Math.max(20, 0.02 * 180 * 180 / nodes.length);
  for (let it = 0; it < 300; it++) {
    const k = 1 - it / 300;
    for (let i = 0; i < nodes.length; i++) for (let j = i + 1; j < nodes.length; j++) {
      const p = P[nodes[i].id], q = P[nodes[j].id];
      let dx = p.x - q.x, dy = p.y - q.y, d2 = dx * dx + dy * dy + 0.01;
      const f = C / d2; p.vx += dx * f; p.vy += dy * f; q.vx -= dx * f; q.vy -= dy * f;
    }
    for (const l of links) {
      const p = P[l.a], q = P[l.b], dx = q.x - p.x, dy = q.y - p.y, d = Math.hypot(dx, dy) + 0.01;
      const f = (d - 60) * 0.02 * (0.3 + l.w); p.vx += dx / d * f; p.vy += dy / d * f; q.vx -= dx / d * f; q.vy -= dy / d * f;
    }
    for (const n of nodes) {
      const p = P[n.id];
      p.vx += (W / 2 - p.x) * 0.02; p.vy += (H / 2 - p.y) * 0.03;      // 往中间拉，点少时不会被推到角落
      const v = Math.hypot(p.vx, p.vy), cap = v > 12 ? 12 / v : 1;      // 一轮最多挪 12 像素，开头的大斥力不会把点甩到边上
      p.x = Math.max(14, Math.min(W - 14, p.x + p.vx * cap * k)); p.y = Math.max(14, Math.min(H - 14, p.y + p.vy * cap * k)); p.vx *= 0.5; p.vy *= 0.5;
    }
  }
  const lines = links.map(l => `<line x1="${P[l.a].x.toFixed(1)}" y1="${P[l.a].y.toFixed(1)}" x2="${P[l.b].x.toFixed(1)}" y2="${P[l.b].y.toFixed(1)}"
    stroke-width="${(0.6 + l.w * 3).toFixed(1)}" class="mp-link${sel && (l.a === sel || l.b === sel) ? ' on' : ''}"/>`).join('');
  const dots = nodes.map(n => `<circle cx="${P[n.id].x.toFixed(1)}" cy="${P[n.id].y.toFixed(1)}" r="${(4 + n.strength * 9).toFixed(1)}"
    class="mp-dot ${n.kind === '常识' ? 'fact' : ''}${n.pinned ? ' pin' : ''}${sel === n.id ? ' sel' : ''}" data-node="${esc(n.id)}"><title>${esc(n.text)}</title></circle>`).join('');
  const cur = sel && data.notes.find(n => n.id === sel);
  const near = cur ? links.filter(l => l.a === sel || l.b === sel).map(l => ({ n: data.notes.find(x => x.id === (l.a === sel ? l.b : l.a)), w: l.w }))
    .filter(x => x.n).sort((a, b) => b.w - a.w) : [];
  return `<div class="mp-graph"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="记忆网络图">${lines}${dots}</svg>
    <div class="mp-legend"><span><i class="mp-dot-i"></i>经历</span><span><i class="mp-dot-i fact"></i>常识</span><span>点越大越牢，线越粗连得越紧。显示最牢的 ${nodes.length} 条，${links.length} 条连接</span></div>
    ${cur ? `<div class="mp-pick">${row(cur)}${near.length ? `<div class="hint">连着的记忆：</div>${near.map(x => `<div class="mp-near"><span class="ct">${x.w.toFixed(2)}</span> ${esc(x.n.text)}</div>`).join('')}` : '<div class="hint">没有连着的记忆</div>'}</div>`
      : '<div class="hint">点一个点看它是哪条记忆、连着哪些记忆。</div>'}</div>`;
}

function render() {
  const n = data.notes.length, facts = data.notes.filter(x => x.kind === '常识');
  const last = data.last_sleep ? new Date(data.last_sleep * 1000).toLocaleString() : '还没整理过';
  const body = tab === 'time' ? timeline(data.notes) : tab === 'fact' ? timeline(facts) : tab === 'graph' ? graph() :
    tab === 'gone' ? (data.gone.map(g => `<div class="mp-row"><span class="mp-kind">${g.merged ? '已合并' : '已淡忘'}</span>
        <div class="mp-main"><div>${esc(g.text)}</div><small>${esc(g.time)}</small></div>
        <span class="mp-acts"><button class="btn sm" data-restore="${esc(g.file)}">恢复</button></span></div>`).join('') || '<div class="hint">没有淡忘的记忆。</div>') :
    `<pre class="mp-log">${esc(data.log || '还没有整理记录。')}</pre>`;
  wrap.querySelector('.mp').innerHTML = `
    <div class="mp-h"><b>${esc(persona)} 的记忆</b><span class="hint">${n} 条（常识 ${facts.length}）· 上次整理：${esc(last)}</span><span class="sp"></span>
      <button class="btn sm" data-sleep="1" title="合并重复、归纳常识、连接衰减、淡忘很久没想起的琐事。平时每晚 3–6 点自动做">现在整理</button>
      <button class="btn sm" data-open-dir="1">打开文件夹</button><button class="btn sm" data-close="1">关闭</button></div>
    <div class="mp-tabs">${[['time', '时间线'], ['fact', '常识'], ['graph', '网络图'], ['gone', `已淡忘（${data.gone.length}）`], ['log', '整理记录']]
      .map(([k, t]) => `<button class="${tab === k ? 'on' : ''}" data-tab="${k}">${t}</button>`).join('')}</div>
    <div class="mp-body">${body}</div>`;
}

async function load() { data = await api('/api/memory/notes?persona=' + encodeURIComponent(persona)); render(); }

async function onClick(e) {
  const t = e.target;
  if (t === wrap || t.closest('[data-close]')) { close(); return; }
  const tb = t.closest('[data-tab]'); if (tb) { tab = tb.dataset.tab; render(); return; }
  const nd = t.closest('[data-node]'); if (nd) { sel = sel === nd.dataset.node ? null : nd.dataset.node; render(); return; }
  const pn = t.closest('[data-pin]');
  if (pn) { await api('/api/memory/pin', { method: 'POST', body: { persona, id: pn.dataset.pin, pinned: pn.dataset.v === '1' } }); await load(); return; }
  const ed = t.closest('[data-edit]'); if (ed) { emit('open-file', { path: ed.dataset.edit }); close(); return; }
  const fg = t.closest('[data-forget]');
  if (fg) {
    if (!await confirmBox('忘掉这条记忆？', '会移到 _已淡忘，以后不会再想起；在“已淡忘”里可以恢复。', { ok: '忘掉' })) return;
    await api('/api/memory/forget', { method: 'POST', body: { persona, id: fg.dataset.forget } }); if (sel === fg.dataset.forget) sel = null; await load(); return;
  }
  const rs = t.closest('[data-restore]');
  if (rs) { await api('/api/memory/restore', { method: 'POST', body: { persona, file: rs.dataset.restore } }); toast('已恢复'); await load(); return; }
  if (t.closest('[data-open-dir]')) { emit('open-file', { path: data.dir + '\\笔记\\' }); close(); return; }
  const sl = t.closest('[data-sleep]');
  if (sl) {
    sl.disabled = true; sl.innerHTML = '<span class="spin"></span>整理中';
    try { const r = await api('/api/memory/sleep', { method: 'POST', body: { persona } }); toast(r.log[0], 5000); tab = 'log'; }
    catch (err) { toast(err.message, 5000); }
    await load();
  }
}

function close() { wrap?.remove(); wrap = null; document.removeEventListener('keydown', onKey); emit('kb-changed'); }
function onKey(e) { if (e.key === 'Escape') close(); }

export async function openMemory(name) {
  persona = name; tab = 'time'; sel = null;
  wrap = document.createElement('div');
  wrap.className = 'dlg-wrap';
  wrap.innerHTML = '<div class="mp" role="dialog" aria-label="记忆"><div class="hint"><span class="spin"></span> 读取中</div></div>';
  document.body.appendChild(wrap);
  wrap.addEventListener('click', onClick);
  document.addEventListener('keydown', onKey);
  try { await load(); } catch (err) { toast(err.message, 4000); close(); }
}

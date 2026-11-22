// 设置页的“模型”卡片：模型清单（config/models.yaml）里每个模型的用途、能力、上下文、占用，现在加载了没有（GET /api/models）。
// 下面是下载区：按功能分组勾选下载，缺的程序（llama.cpp、llama-swap、ComfyUI）一起装（/api/models/downloads）。
// 打开设置页时取一次，点“刷新”再取。改模型在清单里改。
import { api, esc, on, toast } from './api.js';

const ROLE = { chat: '对话默认', write: '精写', fast: '快速 / 随手问', ja_zh: '日→中翻译', translate: '翻译', ocr: '识字',
  embed: '知识库向量', rerank: '知识库重排', image: '图像' };
const k = n => n >= 1024 ? Math.round(n / 1024) + 'k' : String(n);
let card, body, loading = false;

export const MODELS_CARD = `<section class="set-card mt-card" id="mt-card">
    <div class="set-h st-h">模型<span class="sp"></span><button type="button" class="btn sm" data-mt="refresh">刷新</button></div>
    <div class="mt-body"><div class="hint">读取中……</div></div>
    <div class="set-h st-h mt-dl-h">下载</div>
    <div class="mt-dl"><div class="hint">读取中……</div></div>
    <div class="hint mt-foot">换模型、改参数：改 <code>config\\models.yaml</code>，再重启服务。</div></section>`;

export function initModels(root) {
  card = root.querySelector('#mt-card'); body = card.querySelector('.mt-body');
  dl = card.querySelector('.mt-dl');
  on('mod', m => { active = m === 'set'; if (active) { load(); loadDl(); } });
  card.querySelector('[data-mt=refresh]').addEventListener('click', () => { load(); loadDl(); });
  dl.addEventListener('click', dlClick);
  dl.addEventListener('change', dlSum);
}

// ---------------------------------------------------------------- 下载
let dl, active = false, dlData = null, poll = null;
const GB = n => (n / 1e9).toFixed(n >= 1e10 ? 0 : 1) + ' GB';

async function loadDl() {
  try { dlData = await api('/api/models/downloads', { quiet: true }); renderDl(); }
  catch (e) { dl.innerHTML = `<div class="hint">${esc(e.message)}</div>`; }
}

function renderDl() {
  const d = dlData, p = d.progress, running = p?.state === 'running';
  const progs = Object.entries(d.programs || {}).filter(([, v]) => !v.present).map(([k]) => k);
  const rows = d.groups.map(g => {
    const done = !g.missing;
    return `<label class="mt-dl-row${done ? ' done' : ''}"><input type="checkbox" value="${esc(g.id)}" ${done || running ? 'disabled' : ''} ${!done && g.required ? 'checked' : ''}>
      <b>${esc(g.id)}</b><span class="hint">${esc(g.desc)}</span><span class="sp"></span>
      <span class="mt-st${done ? ' on' : ''}">${done ? '已下载' : '缺 ' + GB(g.missing_size)}</span></label>`;
  }).join('');
  let bar = '';
  if (p && (running || p.state !== 'done' || Date.now() / 1000 - p.started < 600)) {
    const pct = p.total ? Math.min(100, p.done / p.total * 100) : 0;
    const st = running ? `${p.index}/${p.count} ${esc(p.file)}${p.phase && p.phase !== '下载' ? ' · ' + esc(p.phase) : ''} · ${GB(p.done)} / ${GB(p.total)}`
      : p.state === 'done' ? '下载完成' : p.state === 'stopped' ? `已停止 · ${GB(p.done)} / ${GB(p.total)}` : esc(p.message || '下载失败');
    bar = `<div class="mt-prog"><div class="mt-bar"><i style="width:${pct.toFixed(1)}%"></i></div><div class="hint">${st}</div></div>`;
  }
  dl.innerHTML = `<div class="mt-dl-list">${rows}</div>${bar}
    <div class="mt-dl-act">${progs.length ? `<span class="hint">还会装：${progs.map(esc).join('、')}</span>` : ''}<span class="sp"></span>
      ${running ? '<button type="button" class="btn sm" data-dl="stop">停止</button>' : '<span class="hint" id="mt-dl-sum"></span><button type="button" class="btn sm primary" data-dl="go">下载</button>'}</div>`;
  dlSum();
  clearTimeout(poll);
  if (running && active) poll = setTimeout(async () => {
    try { dlData.progress = (await api('/api/models/downloads', { quiet: true })).progress; } catch (e) {}
    if (dlData.progress?.state === 'running') renderDl(); else loadDl();
  }, 2000);
}

const picked = () => [...dl.querySelectorAll('input:checked')].map(x => x.value);
function dlSum() {
  const s = dl.querySelector('#mt-dl-sum'); if (!s || !dlData) return;
  const n = dlData.groups.filter(g => picked().includes(g.id)).reduce((a, g) => a + g.missing_size, 0);
  s.textContent = n ? '共 ' + GB(n) : '';
}

async function dlClick(e) {
  const b = e.target.closest('[data-dl]'); if (!b) return;
  try {
    if (b.dataset.dl === 'go') {
      const g = picked(); if (!g.length) { toast('先勾选要下载的'); return; }
      await api('/api/models/downloads', { method: 'POST', body: { groups: g } });
    } else await api('/api/models/downloads/stop', { method: 'POST' });
    setTimeout(loadDl, 800);
  } catch (err) { toast(err.message, 4000); }
}


async function load() {
  if (loading) return;
  loading = true;
  try {
    render(await api('/api/models', { quiet: true }));
  } catch (e) {
    body.innerHTML = `<div class="hint">读不到模型表：${esc(e.message)}</div>`;
  } finally { loading = false; }
}

function state(m, ok) {
  if (m.files_ok === false) return '<span class="mt-st bad" title="清单里写的模型文件不在">文件不全</span>';
  if (!ok) return '';
  if (m.state === 'ready') return '<span class="mt-st on">已加载</span>';
  if (m.state) return `<span class="mt-st">${m.state === 'starting' ? '加载中' : esc(m.state)}</span>`;
  return '';
}

function render(d) {
  body.innerHTML = (d.ok ? '' : '<div class="hint">调度器没连上，加载状态看不到。</div>') + '<ul class="mt-list">' + d.models.map(m => {
    const tags = m.roles.map(r => `<span class="mt-tag">${ROLE[r] || esc(r)}</span>`).join('') +
      (m.menu ? '<span class="mt-tag soft">对话可选</span>' : '');
    const ctx = m.ctx ? '上下文 ' + k(m.ctx) + (m.agent_ctx && m.agent_ctx !== m.ctx ? `（Agent 用 ${k(m.agent_ctx)}）` : '')
      : m.agent_ctx ? 'Agent 上下文 ' + k(m.agent_ctx) : '';
    const info = [m.device === 'cpu' ? '内存常驻' : '显卡', m.vision ? '能看图' : '', ctx, m.size || ''].filter(Boolean).map(esc).join(' · ');
    return `<li title="${esc(m.title + '\n' + m.id)}"><div class="mt-l1"><b>${esc(m.name)}</b>${tags}<span class="sp"></span>${state(m, d.ok)}</div>
      <div class="mt-l2">${info}</div></li>`;
  }).join('') + '</ul>';
}

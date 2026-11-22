// 翻译模块：贴文字翻译，或者翻译右边打开的文件。后台按段翻译，网页逐段显示。
import { $, api, emit, esc, fname, on, reconnectNote, settings, toast, watchStream } from './api.js';
import { promptBox } from './ui.js';

const LS = 'tr';            // 本机记住输入和选项
let info = { glossaries: [], ext: [] };
let job = null, stopStream = null, viewFile = null;
let state = { lines: [], file: null, t0: 0, n: 0, done: 0, units: 0, align: true };
const ENGINE = { sakura: 'Sakura', hy: 'Hy-MT2' };
const LANG = { zh: '中', en: '英', ja: '日' };

function load() { try { return JSON.parse(localStorage.getItem(LS) || '{}'); } catch (e) { return {}; } }
function save(patch) { try { localStorage.setItem(LS, JSON.stringify({ ...load(), ...patch })); } catch (e) {} }

function opts() {
  const [source, target] = $('#tr-dir').value === 'auto' ? ['auto', ''] : $('#tr-dir').value.split('>');
  return { source, target, engine: $('#tr-engine').value, glossary: $('#tr-gloss').value, align: $('#tr-align').classList.contains('on') };
}

async function refreshInfo() {
  info = await api('/api/tr/info');
  const cur = load().gloss || '';
  $('#tr-gloss').innerHTML = '<option value="">不用术语表</option>' +
    info.glossaries.map(g => `<option value="${esc(g.name)}">术语表：${esc(g.name)}（${g.count} 条）</option>`).join('') +
    '<option value="+">新建术语表……</option>';
  $('#tr-gloss').value = info.glossaries.some(g => g.name === cur) ? cur : '';
  fileChip();
}

// ---------------------------------------------------------------- 术语表
async function openGloss(name) {
  $('#tr-gloss-name').textContent = name;
  const g = info.glossaries.find(x => x.name === name);
  $('#tr-gloss-text').value = g ? (await api('/api/files/read?path=' + encodeURIComponent(g.path))).text : '';
  $('#tr-gloss-path').textContent = g ? g.path : '保存后放在 沙箱\\术语表';
  $('#tr-gloss-box').hidden = false; $('#tr-gloss-text').focus();
}

async function saveGloss() {
  const name = $('#tr-gloss-name').textContent;
  const r = await api('/api/tr/glossaries', { method: 'PUT', body: { name, text: $('#tr-gloss-text').value } });
  toast(`已保存，${r.count} 条`); save({ gloss: name });
  await refreshInfo(); $('#tr-gloss').value = name; $('#tr-gloss-path').textContent = r.path;
  emit('files-refresh');
}

// ---------------------------------------------------------------- 右边打开的文件
function fileChip() {
  const c = $('#tr-file');
  const ok = viewFile && info.ext.some(e => viewFile.toLowerCase().endsWith(e));
  c.hidden = !ok;
  if (ok) c.textContent = '翻译文件：' + fname(viewFile);
}

// ---------------------------------------------------------------- 显示
function render() {
  const out = $('#tr-out');
  if (!job && !state.lines.length && !state.file && !state.error) {   // 一段都没译出来就出错时也要显示错误（以前回到空白说明，看不出失败了）
    out.innerHTML = `<div class="empty"><span class="flower deco-only"></span>
      <div>日文默认用 Sakura，其他语言用 Hy-MT2，中文默认译成英文。</div>
      <div class="hint">右边打开 txt、md、srt、vtt、ass、docx、epub 文件时，可以整个文件翻译，译文另存为“原名.zh.扩展名”。</div></div>`;
    return;
  }
  const bi = $('#tr-bi').classList.contains('on');
  const pct = state.n ? Math.round(state.done / state.n * 100) : 0;
  const secs = (Date.now() - state.t0) / 1000;
  const eta = job && state.done ? Math.round(secs / state.done * (state.n - state.done)) : null;
  const head = `<div class="tr-head">${job ? '<span class="spin"></span>' : ''}
    <span>${esc(state.what || '')}</span>${state.net ? `<span class="tr-net">${state.net}</span>` : ''}<span class="num">${state.n ? `${state.done} / ${state.n} 段` : job ? '准备中' : ''}</span>
    ${job ? `<span class="meter"><b style="width:${pct}%"></b></span>${eta != null ? `<span class="num">约还要 ${eta} 秒</span>` : ''}` : ''}
    <span class="sp"></span>${!job && state.lines.length ? '<button class="btn sm" id="tr-copy">复制译文</button>' : ''}</div>`;
  const rows = state.lines.map(r => r.gap ? '<div class="gap"></div>' :
    `<div class="ln${r.fb ? ' fb' : ''}"${r.fb ? ' title="这一段行数对不上，改成一行一行译的"' : ''}>${bi && r.s ? `<span class="s">${esc(r.s)}</span>` : ''}${esc(r.t)}</div>`).join('');
  const done = state.file ? `<div class="tr-done"><div>译文已保存：<b>${esc(state.fileShow)}</b></div><div class="row">
      <button class="btn" data-open="${esc(state.file)}">在右边打开</button>
      ${settings.config.local ? `<button class="btn" data-reveal="${esc(state.file)}">在资源管理器中显示</button>` : ''}</div></div>` : '';
  const err = state.error ? `<div class="err-line">${esc(state.error)}</div>` : '';
  const keep = out.scrollTop + out.clientHeight >= out.scrollHeight - 40;
  out.innerHTML = head + err + done + `<div class="tr-lines${bi ? ' bi' : ''}">${rows}</div>`;
  if (keep && job) out.scrollTop = out.scrollHeight;
}

function onEvent(ev) {
  if (ev.type === 'start') {
    state.n = ev.n; state.units = ev.units;
    state.what = `${LANG[ev.source] || ev.source} → ${LANG[ev.target] || ev.target} · ${ENGINE[ev.engine] || ev.model}`;
    $('#tr-sub').textContent = ENGINE[ev.engine] || ev.model;
  } else if (ev.type === 'chunk') {
    state.done = ev.i + 1;
    if (state.align) ev.src.forEach((s, k) => state.lines.push({ s, t: ev.out[k] ?? '', fb: ev.fallback }));
    else { state.lines.push({ s: ev.src.join('\n\n'), t: ev.out[0] }); state.lines.push({ gap: true }); }
  } else if (ev.type === 'done') {
    finish();
    if (ev.file) { state.file = ev.file; state.fileShow = ev.show; emit('files-refresh'); emit('open-file', { path: ev.file, auto: true, beside: true }); }   // 译文放在原文下面一栏
    if (ev.text != null) state.text = ev.text;
    state.what += ` · ${ev.secs} 秒${ev.stopped ? '（已停止）' : ''}`;
    emit('status-refresh');
  } else if (ev.type === 'error') {
    finish(); state.error = '翻译出错：' + ev.message;
  } else if (ev.type === 'lost') {
    finish(); state.error = '和服务的连接断了。服务重启过的话，翻译任务已经停止。';
  }
  render();
}

function finish() {
  stopNote?.(); stopNote = null; state.net = '';
  job = null; save({ job: null });
  $('#tr-stop').hidden = true; $('#tr-go').hidden = false;
}

let stopNote = null;
function follow(id, start = 0) {
  job = id; $('#tr-stop').hidden = false; $('#tr-go').hidden = true;
  const es = new EventSource(`/api/tr/jobs/${id}/events?start=${start}`);
  es.onmessage = e => { const ev = JSON.parse(e.data); onEvent(ev); if (ev.type === 'done' || ev.type === 'error') es.close(); };
  es.onerror = () => { if (es.readyState === EventSource.CLOSED && job === id) onEvent({ type: 'lost' }); };
  // 断了、浏览器在重连：标题栏显示重连提示，连上后去掉（以前看起来像卡住了）
  watchStream(es, s => {
    stopNote?.(); stopNote = null; state.net = '';
    if (s.state === 'reconnecting' && job === id) stopNote = reconnectNote(s.last, h => { state.net = h; render(); });
    else render();
  });
  stopStream = () => es.close();
}

export async function startJob(body) {
  if (job) { toast('上一个翻译还没完成'); return; }
  state = { lines: [], file: null, t0: Date.now(), n: 0, done: 0, align: body.path ? true : body.align, what: body.path ? '翻译 ' + fname(body.path) : '' };
  render();
  let r;
  try { r = await api('/api/tr/jobs', { method: 'POST', body }); }
  catch (e) { state.error = e.message; render(); return; }
  if (r.agent_busy) toast('Agent 正在运行：翻译要换显卡上的模型，Agent 下一步会重新加载，多等十几秒', 4500);
  save({ job: r.job, jobBody: { path: body.path || null, align: state.align } });
  follow(r.job);
}

function go(useFile = false) {
  const o = opts();
  if (useFile) return startJob({ ...o, path: viewFile });
  const text = $('#tr-input').value;
  if (!text.trim()) { toast('先输入要翻译的文字'); return; }
  startJob({ ...o, text });
}

// 别的模块（识字）用：翻译一段文字，返回译文
export async function translateText(text, extra = {}) {
  const r = await api('/api/tr/jobs', { method: 'POST', body: { text, align: false, ...extra } });
  if (r.agent_busy) toast('Agent 正在运行：翻译要换显卡上的模型，Agent 下一步会重新加载', 4000);
  return new Promise((resolve, reject) => {
    const es = new EventSource(`/api/tr/jobs/${r.job}/events`);
    let model = '';
    es.onmessage = e => {
      const ev = JSON.parse(e.data);
      if (ev.type === 'start') model = ENGINE[ev.engine] || ev.model;
      if (ev.type === 'done') { es.close(); resolve({ text: ev.text, model, secs: ev.secs }); }
      if (ev.type === 'error') { es.close(); reject(new Error(ev.message)); }
    };
    es.onerror = () => { if (es.readyState === EventSource.CLOSED) reject(new Error('连接断了')); };
  });
}

export function setInput(text) {
  $('#tr-input').value = text; save({ text }); count();
}

function count() {
  const n = $('#tr-input').value.length;
  $('#tr-count').textContent = n ? `${n} 字` : '';
}

export async function initTranslate() {
  const s = load();
  $('#tr-input').value = s.text || '';
  if (s.dir) $('#tr-dir').value = s.dir;
  if (s.engine) $('#tr-engine').value = s.engine;
  if (s.align === false) $('#tr-align').classList.remove('on');
  if (s.bi === false) $('#tr-bi').classList.remove('on');
  count();
  await refreshInfo();

  $('#tr-input').addEventListener('input', () => { save({ text: $('#tr-input').value }); count(); });
  $('#tr-input').addEventListener('keydown', e => { if (e.key === 'Enter' && e.ctrlKey) { e.preventDefault(); go(); } });
  $('#tr-go').onclick = () => go();
  $('#tr-file').onclick = () => go(true);
  $('#tr-stop').onclick = async () => { if (job) await api(`/api/tr/jobs/${job}/stop`, { method: 'POST' }); toast('当前这一段译完就停'); };
  $('#tr-clear').onclick = () => { if (job) return toast('翻译还在进行'); setInput(''); state = { lines: [] }; $('#tr-sub').textContent = ''; render(); };
  $('#tr-dir').onchange = () => save({ dir: $('#tr-dir').value });
  $('#tr-engine').onchange = () => save({ engine: $('#tr-engine').value });
  for (const id of ['#tr-align', '#tr-bi']) $(id).onclick = () => {
    $(id).classList.toggle('on');
    save({ align: $('#tr-align').classList.contains('on'), bi: $('#tr-bi').classList.contains('on') });
    if (id === '#tr-bi') render();
  };
  $('#tr-gloss').onchange = async () => {
    const v = $('#tr-gloss').value;
    if (v === '+') {
      const name = await promptBox('新建术语表', { placeholder: '名字，例如作品名', ok: '新建' });
      $('#tr-gloss').value = load().gloss || '';
      if (name && name.trim()) openGloss(name.trim());
      return;
    }
    save({ gloss: v });
  };
  $('#tr-gloss-edit').onclick = async () => {
    const v = $('#tr-gloss').value;
    if (!v) { const name = await promptBox('新建术语表', { placeholder: '名字，例如作品名', ok: '新建' }); if (name && name.trim()) openGloss(name.trim()); return; }
    openGloss(v);
  };
  $('#tr-gloss-save').onclick = () => saveGloss().catch(e => toast(e.message));
  $('#tr-gloss-close').onclick = () => { $('#tr-gloss-box').hidden = true; };

  $('#tr-out').addEventListener('click', e => {
    if (e.target.id === 'tr-copy') {
      const text = state.text ?? state.lines.filter(r => !r.gap).map(r => r.t).join('\n');
      navigator.clipboard.writeText(text).then(() => toast('已复制'), () => toast('复制失败'));
      return;
    }
    const o = e.target.closest('[data-open]'); if (o) emit('open-file', { path: o.dataset.open, beside: true });
    const r = e.target.closest('[data-reveal]'); if (r) emit('reveal', r.dataset.reveal);
  });
  on('active-file', p => { viewFile = p; fileChip(); });

  // 刷新页面前在翻译：接着看（服务端从头重放事件）
  if (s.job) {
    state = { lines: [], file: null, t0: Date.now(), n: 0, done: 0, align: s.jobBody?.align ?? true, what: s.jobBody?.path ? '翻译 ' + fname(s.jobBody.path) : '' };
    follow(s.job);
  }
  render();
}

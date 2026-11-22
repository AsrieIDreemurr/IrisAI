// 对话：列表、历史、发送、按时间顺序显示 Agent 的思考 / 步骤 / 回答、确认卡片。
import { $, api, emit, esc, events, fname, md, on, reconnectNote, saveSettings, settings, toast } from './api.js';
import { confirmBox, popMenu } from './ui.js';
import { activeFile } from './viewer.js';
import { dock } from './dock.js';
import { applyPending } from './kbpick.js';
import { applyPendingPersona } from './personapick.js';
import { seCardFor } from './selfedit.js';

let conv = null;        // 当前对话
let project = null;     // 当前对话的项目文件夹 {path, show}
export const currentProject = () => project;
let runId = null;       // 正在运行的任务
let stopStream = null;
let refFile = false;
const chat = () => $('#chat');
const TEXT_EXT = /\.(txt|md|markdown|html?|css|js|mjs|json|py|ya?ml|ini|csv|srt|ass|log|xml|toml|bat|ps1|sh|ts)$/i;
const IMG_EXT = /\.(png|jpe?g|webp|gif|bmp)$/i;

// ---------------------------------------------------------------- 对话列表
// 展开的归档分组。本机存的值坏了（不是 JSON、不是数组）就只清掉这一项，不能让整个对话页打不开（I04）
const openGroups = (() => {
  try {
    const v = JSON.parse(localStorage.getItem('arch-open') || '[]');
    if (Array.isArray(v)) return new Set(v.filter(x => typeof x === 'string'));
  } catch (e) {}
  try { localStorage.removeItem('arch-open'); } catch (e) {}
  return new Set();
})();

export async function loadConvs() {
  const [list, arch] = await Promise.all([api('/api/convs'), api('/api/convs?archived=1')]);
  $('#conv-count').textContent = list.length || '';
  $('#convs').innerHTML = list.length ? list.map(c => `
    <div class="conv${conv && c.id === conv.id ? ' on' : ''}" data-id="${c.id}">
      <b>${esc(c.title)}</b><small>${when(c.updated)}</small>
      <button type="button" class="conv-more" data-op="more" title="改名、归档、删除（也可以右键）">⋯</button></div>`).join('')
    : '<div class="tree-empty" style="padding-left:16px">还没有对话</div>';
  // 已归档：按分组（例如“测试”）放，没分组的放最后
  const groups = {};
  arch.forEach(c => { (groups[c.folder || ''] ||= []).push(c); });
  const names = Object.keys(groups).sort((a, b) => a === '' ? 1 : b === '' ? -1 : a.localeCompare(b, 'zh'));
  $('#arch-count').textContent = arch.length || '';
  $('#archived').innerHTML = arch.length ? names.map(g => `
    <div class="arch-group${openGroups.has(g) ? '' : ' closed'}" data-g="${esc(g)}">
      <div class="ag-h">${esc(g || '未分组')}<span class="cnt">${groups[g].length}</span></div>
      <div class="ag-body">${groups[g].map(c => `
        <div class="conv arch${conv && c.id === conv.id ? ' on' : ''}" data-id="${c.id}" data-folder="${esc(c.folder || '')}">
          <b>${esc(c.title)}</b><small>${when(c.updated)}</small>
          <button type="button" class="conv-more" data-op="more" title="恢复、分组、删除（也可以右键）">⋯</button></div>`).join('')}</div>
    </div>`).join('') : '<div class="tree-empty" style="padding-left:16px">没有归档的对话</div>';
}

function when(t) {
  const d = new Date(t * 1000), now = new Date();
  if (d.toDateString() === now.toDateString()) return '今天 ' + d.toTimeString().slice(0, 5);
  return `${d.getMonth() + 1} 月 ${d.getDate()} 日`;
}

// ---------------------------------------------------------------- 步骤详情（展开后看到的内容）
const pre = (t, cls = '') => `<pre class="mono ${cls}">${esc(t)}</pre>`;
const btns = path => path ? `<div class="row">
  <button class="btn" data-open="${esc(path)}">在右边打开</button>
  ${settings.config.local ? `<button class="btn" data-reveal="${esc(path)}">在资源管理器中显示</button>` : ''}</div>` : '';

function stepBody(tool, d = {}, result = '', extra = {}) {
  switch (tool) {
    case 'write_file':
      return `<div>写入 ${d.lines ?? '?'} 行，${d.chars ?? '?'} 字。内容在右边查看，绿色是这次写入的行。</div>${btns(extra.file || d.path)}`;
    case 'make_dir': case 'delete_path': case 'list_dir': case 'read_file':
      return (tool === 'list_dir' || tool === 'read_file' ? pre(result.slice(0, 3000)) : `<div>${esc(result)}</div>`) + btns(extra.file || d.path);
    case 'move_path':
      return `<div>${esc(result)}</div>` + btns(extra.file);
    case 'run_python':
      return `<div class="label">代码</div>${pre(d.code || '')}<div class="label">输出</div>${pre(result || '（没有输出）', 'out')}`;
    case 'check_file':
      return pre(result) + btns(d.path);
    case 'web_search':
      return `<div class="label">搜索词：${esc(d.query || '')}</div>${pre(result)}`;
    case 'fetch_url':
      return `<div><a href="${esc(d.url || '')}" target="_blank" rel="noopener noreferrer">${esc(d.url || '')}</a></div>${pre(result.slice(0, 3000))}`;
    case 'translate':
      return `<div class="label">原文</div>${pre(d.text || '')}<div class="label">译文</div>${pre(result)}`;
    case 'generate_image':
      return `<div class="label">提示词</div>${pre(d.prompt || '')}<div>${esc(result)}</div>` + btns(extra.image);
    case 'self_edit_propose':
      return extra.selfedit ? seCardFor(extra.selfedit) : `<div>${esc(result)}</div>`;
    case 'propose_plan':
      return `<ol>${(d.steps || []).map(s => `<li>${esc(s)}</li>`).join('')}</ol>`;
    case 'edit_file':
      return `<div class="label">原文</div>${pre(d.old_text || '')}<div class="label">改成</div>${pre(d.new_text || '')}<div>${esc(result)}</div>` + btns(extra.file || d.path);
    case 'search_files':
      return `<div class="label">搜索：${esc(d.query || '')}</div>${pre(result)}`;
    case 'run_js':
      return d.page
        ? `<div class="label">运行 ${esc(d.page)} 的真实脚本 ${d.frames || 120} 帧，观察：${esc((d.watch || []).join('；'))}</div>${pre(result, 'out')}`
        : `<div class="label">代码</div>${pre(d.code || '')}<div class="label">输出</div>${pre(result || '（没有输出）', 'out')}`;
    case 'view_result':
      return `<div class="shots">${(extra.images || []).map(p => `<img src="/api/files/raw?path=${encodeURIComponent(p)}" data-zoom="1" alt="" loading="lazy" decoding="async">`).join('')}</div>${pre(result)}`;
    case 'update_plan': case 'set_acceptance': case 'record_check': case 'ask_user':
      return `<div>${esc(result)}</div>`;
    default:
      return pre(JSON.stringify(d, null, 1) + (result ? '\n\n' + result : ''));
  }
}

// ---------------------------------------------------------------- 一轮 AI 回答：按时间顺序排列
class Turn {
  constructor(parent) {
    this.el = document.createElement('div');
    this.el.className = 'a';
    this.el.innerHTML = `<div class="who"><span class="spark"></span><span class="m"></span><span class="turn-sum"></span></div>
      <div class="flow"></div><div class="status-line"></div>`;
    parent.appendChild(this.el);
    this.steps = new Map(); this.seg = null; this.segText = ''; this.thinkEl = null; this.t0 = Date.now(); this.live = false;
  }
  q(s) { return this.el.querySelector(s); }
  flow() { return this.q('.flow'); }
  model(name) { this.q('.who .m').textContent = name; }
  status(html) { this.q('.status-line').innerHTML = html || ''; }
  sum() {
    const n = this.steps.size;
    const t = ((Date.now() - this.t0) / 1000).toFixed(0);
    this.q('.turn-sum').textContent = this.live ? (n ? `进行中 · 已执行 ${n} 步` : '进行中') : (n ? `${n} 步` + (this.sec ? ` · 用时 ${this.sec} 秒` : '') : '');
  }
  // 思考：每轮一块，生成时展开，这一轮结束就收起
  think(text) {
    this.closeSeg();
    if (!this.thinkEl) {
      this.flow().insertAdjacentHTML('beforeend', '<details class="think" open><summary>思考</summary><div class="think-body"></div></details>');
      this.thinkBox = this.flow().lastElementChild; this.thinkEl = this.thinkBox.querySelector('.think-body'); this.thinkText = '';
    }
    this.thinkText += text; this.thinkEl.textContent = this.thinkText; this.thinkEl.scrollTop = this.thinkEl.scrollHeight;
    this.thinkBox.querySelector('summary').textContent = `思考 · ${this.thinkText.length} 字`;
  }
  closeThink() {
    if (this.thinkBox) { this.thinkBox.open = false; }
    this.thinkEl = null; this.thinkBox = null;
  }
  thinkStatic(text) {
    if (!text) return;
    this.flow().insertAdjacentHTML('beforeend', `<details class="think"><summary>思考 · ${text.length} 字</summary><div class="think-body"></div></details>`);
    this.flow().lastElementChild.querySelector('.think-body').textContent = text;
  }
  delta(text) {
    this.closeThink();
    if (!this.seg) { this.seg = document.createElement('div'); this.seg.className = 'md-body cursor'; this.flow().appendChild(this.seg); this.segText = ''; }
    this.segText += text;
    if (!this.pending) { this.pending = true; requestAnimationFrame(() => { this.pending = false; if (this.seg) this.seg.innerHTML = md(this.segText); }); }
  }
  closeSeg() {
    if (this.seg) { this.seg.innerHTML = md(this.segText); this.seg.classList.remove('cursor'); linkPaths(this.seg); if (!this.segText.trim()) this.seg.remove(); }
    this.seg = null;
  }
  endRound() { this.closeSeg(); this.closeThink(); }
  text(content) {
    const d = document.createElement('div'); d.className = 'md-body'; d.innerHTML = md(content); linkPaths(d); this.flow().appendChild(d);
  }
  step(ev) {
    this.endRound();
    let row = this.steps.get(ev.id);
    if (!row) {
      row = document.createElement('details'); row.className = 'step'; row.dataset.id = ev.id;
      row.innerHTML = '<summary></summary><div class="step-body"></div>';
      this.flow().appendChild(row); this.steps.set(ev.id, row);
    }
    const icon = { running: '<span class="spin"></span>', waiting: '<span class="st wait">…</span>', ok: '<span class="st ok">✓</span>',
                   error: '<span class="st err">✕</span>', denied: '<span class="st err">－</span>' }[ev.status] || '';
    const note = ev.note || { waiting: '等你确认', denied: '没有执行', error: '失败' }[ev.status] || (ev.problems ? `发现 ${ev.problems} 个问题` : '');
    row.querySelector('summary').innerHTML = `${icon}<span class="t">${esc(ev.title)}</span>${note ? `<span class="snote ${ev.status}">${esc(note)}</span>` : ''}<span class="num">${ev.sec != null ? ev.sec + ' 秒' : ''}</span>`;
    if (ev.status !== 'running' || ev.detail) row.querySelector('.step-body').innerHTML = stepBody(ev.tool, ev.detail || {}, ev.result || '', ev);
    if (ev.image && !this.q(`img[data-p="${CSS.escape(ev.image)}"]`)) {
      row.insertAdjacentHTML('afterend', `<img class="gen" data-p="${esc(ev.image)}" data-open="${esc(ev.image)}" src="/api/files/raw?path=${encodeURIComponent(ev.image)}" alt="" loading="lazy" decoding="async" title="点击在右边打开">`);
    }
    this.sum();
  }
  card(ev, live) {
    this.endRound();
    const c = document.createElement('div');
    c.className = 'confirm'; c.dataset.cid = ev.id || '';
    const ask = ev.kind === 'ask';
    const buttons = ask
      ? `<div class="row">${(ev.options || []).map(o => `<button class="btn" data-act="answer" data-val="${esc(o)}">${esc(o)}</button>`).join('')}</div>
         <textarea placeholder="或者自己写，按回车发送" data-free="1"></textarea>`
      : `<div class="row"><button class="btn primary" data-act="ok">允许</button><button class="btn" data-act="no">取消</button><button class="btn" data-act="edit">修改</button></div>`;
    c.innerHTML = `<div class="head"><span class="dot wait"></span>${esc(ev.title)}</div>
      ${(ev.steps || []).length ? `<ol>${ev.steps.map(s => `<li>${esc(s)}</li>`).join('')}</ol>` : ''}
      ${live ? buttons : ''}<div class="done"></div>`;
    this.flow().appendChild(c);
    if (ev.decision) this.cardDone(c, ev.decision, ev.note);
  }
  cardDone(c, decision, note) {
    c = typeof c === 'string' ? this.q(`.confirm[data-cid="${c}"]`) : c;
    if (!c) return;
    c.querySelector('.row')?.remove(); c.querySelector('textarea')?.remove();
    c.querySelector('.dot').className = 'dot' + (decision === 'ok' || decision === 'answer' ? '' : ' wait');
    c.querySelector('.done').textContent = { ok: '已允许。', no: '已取消，没有执行。', edit: '要求修改：' + (note || ''), answer: '你的回答：' + (note || '') }[decision] || '';
  }
  // 计划和验收显示在输入框上方的任务面板里；这一轮结束时，在回答末尾留一行验收结果
  accLine(criteria, unfinished) {
    if (!criteria?.length && !unfinished?.length) return;
    this.q('.acc-line')?.remove();
    const ok = criteria.filter(c => c.status === '通过').length;
    const bad = criteria.filter(c => c.status === '不通过').length;
    const kind = unfinished?.length || bad ? 'err' : ok === criteria.length ? 'ok' : 'todo';
    const text = `验收 ${ok}/${criteria.length} 通过` + (bad ? ` · ${bad} 条不通过` : '') + (unfinished?.length ? ` · ${unfinished.length} 项未完成` : '');
    this.flow().insertAdjacentHTML('beforeend', `<button class="acc-line ${kind}" data-dock="acc"><span class="dot"></span>${text}<span class="go">查看</span></button>`);
  }
  // 人格的记忆：这一轮想起了什么、记下了什么（记下的可以撤销 = 移到 _已淡忘）
  mem(ev, kind) {
    const items = ev.items || [];
    if (!items.length) return;
    this.endRound();
    const head = kind === 'recall' ? `想起了 ${items.length} 件事` : `记下了 ${items.filter(x => x.new !== false).length} 件事` +
      (items.some(x => x.new === false) ? `（${items.filter(x => x.new === false).length} 件早就记得）` : '');
    const li = items.map(x => `<li><span>${x.time ? `<small>${esc(x.time.slice(0, 10))}</small> ` : ''}${esc(x.text)}` +
      (x.via ? ' <small>（联想到的）</small>' : '') + (x.new === false ? ' <small>（早就记得）</small>' : '') + '</span>' +
      (kind === 'saved' && x.new !== false ? `<button class="btn sm" data-mem-forget="${esc(x.id)}" data-persona="${esc(ev.persona)}">撤销</button>` : '') + '</li>').join('');
    this.flow().insertAdjacentHTML('beforeend', `<details class="mem-line ${kind}"><summary><span class="spark"></span>${head}</summary><ul>${li}</ul></details>`);
  }
  reminder(text) {
    this.endRound();
    this.flow().insertAdjacentHTML('beforeend', `<div class="reminder"><span class="label">程序提醒</span>${esc(text)}</div>`);
  }
  review(ev) {
    dock.review(ev);
    if (ev.status === 'running') { this.status('<span class="spin"></span>独立验收员正在看证据'); return; }
    this.status('');
    const t = ev.status === 'error' ? ev.text : ev.issues?.length ? '独立验收员发现问题：' + ev.issues.join('；') : '独立验收员：全部通过';
    this.flow().insertAdjacentHTML('beforeend', `<div class="reminder ${ev.issues?.length ? 'bad' : 'good'}"><span class="label">验收员</span>${esc(t)}</div>`);
  }
  replaceFinal(text) {
    const segs = this.flow().querySelectorAll(':scope > .md-body');
    const last = segs[segs.length - 1];
    if (last) { last.innerHTML = md(text); linkPaths(last); }
  }
}

// 回答里用反引号写的路径变成可点的链接
const PATHLIKE = /^(?:[A-Za-z]:[\\/]|沙箱(?:[\\/]|$)|[^\s`'"<>|?*:]+[\\/])[^<>|?*"]*$/;
function linkPaths(el) {
  el.querySelectorAll('code').forEach(c => {
    if (c.closest('pre')) return;
    const t = c.textContent.trim();
    if (PATHLIKE.test(t) && t.length < 260) { c.classList.add('path-link'); c.dataset.open = t; c.title = '点击在右边打开'; }
  });
}

// ---------------------------------------------------------------- 历史记录 → 显示
function renderHistory(msgs, state, running = false) {
  const box = chat(); box.innerHTML = '';
  dock.load(state);
  if (!msgs.length) { empty(); return; }
  let turn = null, last = null;
  const args = new Map();
  for (const m of msgs) {
    if (m.role === 'assistant' && m.meta?.prompt_n != null) last = m.meta;
    if (m.role === 'user' && m.meta?.system) {     // 程序插入的提醒，属于当前这一轮
      if (!turn) { turn = new Turn(box); turn.model(nameOf(conv.model)); }
      turn.reminder(String(m.content).replace(/<\/?系统提醒>/g, '')); continue;
    }
    if (m.role === 'user') { if (turn) turn.sum(); userBubble(m.content); turn = null; continue; }
    if (m.meta?.interrupted) {
      if (!turn) { turn = new Turn(box); turn.model(nameOf(conv.model)); }
      turn.reminder(m.content.replace(/[（）]/g, '')); continue;
    }
    if (!turn) { turn = new Turn(box); turn.model(nameOf(m.meta?.model || conv.model)); }
    if (m.role === 'assistant') {
      if (m.meta?.model) turn.model(nameOf(m.meta.model));
      turn.thinkStatic(m.meta?.think);
      if (m.content) turn.text(m.content);
      for (const c of m.tool_calls || []) { try { args.set(c.id, JSON.parse(c.function.arguments || '{}')); } catch (e) { args.set(c.id, {}); } }
    } else if (m.role === 'tool') {
      const meta = m.meta || {};
      if (meta.confirm) turn.card(meta.confirm, false);
      const a = args.get(m.tool_call_id) || {};
      const d = meta.tool === 'write_file' ? { path: a.path, lines: (a.content || '').split('\n').length, chars: (a.content || '').length } : a;
      turn.step({ id: m.tool_call_id, title: meta.title || '工具', status: meta.status || 'ok', sec: meta.sec, tool: meta.tool,
                  detail: d, result: m.content || '', image: meta.image, images: meta.images, file: meta.file, problems: meta.problems, note: meta.note });
    }
  }
  if (turn) {
    turn.sum();
    if (state && !running && turn.steps.size) turn.accLine(state.criteria || [], state.unfinished || []);
  }
  ctxMeter(last);
  // 历史记录一次性出现，不做浮现动画（只给新内容做）
  box.querySelectorAll('.u, .a, .flow > *').forEach(e => e.classList.add('na'));
  box.scrollTop = box.scrollHeight;
}

function userBubble(text) {
  const d = document.createElement('div'); d.className = 'u'; d.textContent = text; chat().appendChild(d);
}

// 上下文用量：最近一步发给模型的 token（缓存里直接用的 + 新读的）占这次运行能用的总量（llm_done 的 ctx）
const kTok = n => n >= 10240 ? Math.round(n / 1024) + 'k' : (n / 1024).toFixed(1) + 'k';
function ctxMeter(m) {
  const el = $('#ctx-meter'); if (!el) return;
  const used = m ? (m.prompt_n || 0) + (m.cache_n || 0) : 0;
  if (!used) { el.hidden = true; return; }
  const pct = m.ctx ? Math.min(100, used / m.ctx * 100) : 0;
  el.hidden = false;
  el.querySelector('.lb').textContent = `上下文 ${kTok(used)}` + (m.ctx ? ` / ${kTok(m.ctx)}` : '');
  el.querySelector('.bar').hidden = !m.ctx;
  el.querySelector('.bar b').style.width = pct.toFixed(1) + '%';
  el.classList.toggle('warn', pct >= 75 && pct < 90);
  el.classList.toggle('full', pct >= 90);
  el.title = `最近一步发给模型 ${used.toLocaleString()} token：缓存里直接用的 ${(m.cache_n || 0).toLocaleString()}，新读的 ${(m.prompt_n || 0).toLocaleString()}。` +
    (m.ctx ? `\n这次运行最多能用 ${m.ctx.toLocaleString()}（含给回答留的部分），快满时程序会压缩较早的历史。` : '\n这条是旧记录，没有记总量。');
}

function empty() {
  ctxMeter(null);
  chat().innerHTML = `<div class="empty"><span class="flower"></span><div>直接说要做什么。沙箱里的文件可以随意读写；沙箱外的操作会先问你。</div></div>`;
}

const nameOf = id => settings.config.chat_models.find(m => m.id === id)?.name || id;

// ---------------------------------------------------------------- 运行中
function busy(b) {
  $('#btn-send').hidden = b; $('#btn-stop').hidden = !b;
  document.querySelector(`.conv[data-id="${conv?.id}"] b`)?.classList.toggle('busy', b);
}

function attach(rid, turn) {
  runId = rid; busy(true); turn.live = true; turn.sum(); dock.setLive(true);
  stopStream?.();
  const convId = conv?.id;
  let started = false, failed = false, stopNote = null, netSaved = '';
  emit('agent-mood', { mood: 'think' });          // 看板娘：Agent 在跑
  stopStream = events(rid, ev => {
    const atBottom = chat().scrollHeight - chat().scrollTop - chat().clientHeight < 120;
    switch (ev.type) {
      case 'start': turn.model(nameOf(ev.model)); dock.level(ev.level); break;
      case 'llm': turn.status(`<span class="spin"></span>${started ? '等模型回应' : '等待模型（没加载时要 20 秒左右）'}`); break;
      case 'think': turn.status('<span class="spin"></span>思考中'); turn.think(ev.text); break;
      case 'emote': emit('agent-emote', ev.face); break;   // 开了人格时模型标的表情，桌宠换脸（js/pet.js）
      case 'delta': started = true; turn.status(''); turn.delta(ev.text); emit('agent-talk', ev.text); break;   // 桌宠对口型（js/pet.js）
      case 'writing': started = true; turn.endRound(); turn.status(`<span class="spin"></span>${esc(ev.title)} · 已生成 ${ev.chars.toLocaleString()} 字`); break;
      case 'llm_done': started = true; turn.endRound(); turn.status(''); ctxMeter(ev); emit('agent-round'); break;
      case 'step': turn.status(ev.status === 'running' ? `<span class="spin"></span>${esc(ev.title)}` : ''); turn.step(ev); emit('agent-step', ev); break;
      case 'confirm': turn.card(ev, true); turn.status(ev.kind === 'ask' ? '等你回答' : '等你确认'); emit('agent-wait', ev); break;
      case 'state': dock.load({ plan: ev.plan, criteria: ev.criteria, level: ev.level, findings: ev.findings }); break;
      case 'plan': dock.plan(ev.items, ev.note, ev.findings); break;
      case 'acceptance': dock.acc(ev.criteria, ev.unfinished); if (ev.final) turn.accLine(ev.criteria, ev.unfinished); break;
      case 'reminder': turn.reminder(ev.text); break;
      case 'recall': turn.mem(ev, 'recall'); break;
      case 'memory': turn.mem(ev, 'saved'); break;
      case 'memory_forget': toast('已忘掉一条记忆'); break;
      case 'status': turn.status(`<span class="spin"></span>${esc(ev.text)}`); break;
      case 'review': turn.review(ev); break;
      case 'final': turn.replaceFinal(ev.text); emit('agent-final', ev.text); break;
      case 'confirm_done': turn.cardDone(ev.id, ev.decision, ev.note); turn.status(''); break;
      case 'file': emit('file-changed', ev); break;
      case 'project': project = { path: ev.path, show: ev.show }; emit('project', project); break;
      case 'refresh': emit('files-refresh'); break;
      case 'title': $('#conv-title').textContent = ev.title; loadConvs(); break;
      case 'notice': turn.status(esc(ev.text)); break;
      case 'error': failed = true; turn.status(''); turn.flow().insertAdjacentHTML('beforeend', `<div class="err-line">${esc(ev.text)}</div>`); break;
      case 'net':                                    // 断了、浏览器在重连：先显示重连提示，连上后换回原来的状态行
        if (ev.state === 'reconnecting') { netSaved = turn.q('.status-line').innerHTML; stopNote?.(); stopNote = reconnectNote(ev.last, h => turn.status(h)); }
        else { stopNote?.(); stopNote = null; turn.status(netSaved); }
        break;
      case 'lost': stopNote?.(); stopNote = null; recover(rid, turn, convId); break;
      case 'done':
        stopNote?.(); stopNote = null;
        turn.endRound(); turn.live = false; turn.sec = ((Date.now() - turn.t0) / 1000).toFixed(0); turn.sum();
        emit('agent-mood', failed || /停止|Stopped/.test(turn.q('.status-line').textContent) ? { mood: 'error', ms: 8000 } : { mood: 'done', ms: 6000 });
        if (!/停止|Stopped/.test(turn.q('.status-line').textContent)) turn.status('');
        runId = null; busy(false); dock.setLive(false); loadConvs(); emit('files-refresh'); emit('status-refresh'); break;
    }
    if (atBottom) chat().scrollTop = chat().scrollHeight;
  });
  turn.el.addEventListener('keydown', e => {
    const ta = e.target.closest('textarea[data-free]');
    if (ta && e.key === 'Enter' && !e.shiftKey && ta.value.trim()) { e.preventDefault(); answer(ta.closest('.confirm'), 'answer', ta.value.trim()); }
  });
  turn.el.addEventListener('click', async e => {
    const b = e.target.closest('.confirm button'); if (!b) return;
    const card = b.closest('.confirm'), act = b.dataset.act;
    if (act === 'answer') { answer(card, 'answer', b.dataset.val); return; }
    if (act === 'edit' && !card.querySelector('textarea')) {
      card.insertAdjacentHTML('beforeend', '<textarea placeholder="说明要怎么改，然后按回车"></textarea>');
      const ta = card.querySelector('textarea'); ta.focus();
      ta.addEventListener('keydown', ev => { if (ev.key === 'Enter' && !ev.shiftKey) { ev.preventDefault(); answer(card, 'edit', ta.value); } });
      return;
    }
    answer(card, act, card.querySelector('textarea')?.value);
  });
}

// 事件流彻底连不上了（浏览器自己重连失败才会到这里：服务重启过、这次运行的事件已经回收、断网太久）。
// 问服务这个对话现在怎样：还在跑就重新接上；已经跑完就载入存下的回答；中断了、什么都没存下就说清楚
async function recover(rid, turn, convId) {
  if (runId === rid) runId = null;
  busy(false); dock.setLive(false); emit('agent-mood', {});
  turn.live = false; turn.sum();
  turn.status('<span class="spin"></span>连接断了，正在查这次运行的情况');
  let d = null;
  for (let i = 0; i < 4 && !d; i++) {               // 服务刚重启时可能还没起来，等几秒再问
    try { d = await api('/api/convs/' + convId); } catch (e) { await new Promise(r => setTimeout(r, 2500)); }
  }
  if (conv?.id !== convId) return;                   // 用户已经换到别的对话
  if (!d) { turn.status('连不上服务。服务恢复后点一下这个对话就能接着看。'); return; }
  const msgs = d.msgs || [], lastUser = msgs.map(m => m.role === 'user' && !m.meta?.system).lastIndexOf(true);
  const answered = msgs.slice(lastUser + 1).some(m => m.role === 'assistant' && m.content);
  if (d.run || answered) { await openConv(convId); if (!d.run) toast('连接断过，已载入存下的回答'); return; }
  turn.status('这次运行中断了（服务可能重启过），回答没有保存。可以重新发送。');
}

async function answer(card, decision, note) {
  try { await api(`/api/runs/${runId}/confirm`, { method: 'POST', body: { id: card.dataset.cid, decision, note } }); }
  catch (e) { toast(e.message); }
}

// ---------------------------------------------------------------- 打开 / 新建 / 发送
// 连点几个对话时只认最后一次：慢的旧响应回来时直接作废，界面、选中项、事件订阅都属于最后点的那个（I03）
// 打开和发送不能交错：点“新对话”后马上打字回车，新对话还没打开完就发出去了，打开时会把刚接上的事件流关掉，
// 服务那边答完了，页面一直停在“进行中”（2026-11-13 测出）。所以发送前先等打开完；正在往同一个对话发时，打开要等发完
let openSeq = 0, opening = null, sendWait = null, sendCid = null;
export function openConv(id) {
  const p = doOpen(id);
  opening = p;
  p.catch(() => {}).finally(() => { if (opening === p) opening = null; });
  return p;
}
async function doOpen(id) {
  const my = ++openSeq;
  if (sendWait && sendCid === id) await sendWait;
  if (my !== openSeq) return;
  stopStream?.(); stopStream = null; runId = null; busy(false);
  const d = await api('/api/convs/' + id);
  if (my !== openSeq) return;
  conv = d.conv;
  localStorage.setItem('conv', id);
  emit('conv', id);
  project = d.project ? { path: d.project, show: d.project_show } : null;
  emit('project', project);
  $('#conv-title').textContent = conv.title;
  $('#conv-model').textContent = nameOf(conv.model);
  $('#model').value = conv.model;
  let msgs = d.msgs;
  if (d.run) {   // 还在运行：历史只显示到最后一条用户消息，后面用事件流重放
    const last = msgs.map(m => m.role === 'user' && !m.meta?.system).lastIndexOf(true);
    msgs = msgs.slice(0, last + 1);
  }
  renderHistory(msgs, d.state, !!d.run);
  if (d.run) { const t = new Turn(chat()); attach(d.run, t); }
  document.querySelectorAll('.conv').forEach(c => c.classList.toggle('on', c.dataset.id === id));
}

async function newConv() {
  conv = await api('/api/convs', { method: 'POST', body: { model: $('#model').value } });
  await loadConvs(); await openConv(conv.id);
  $('#input').focus();
}

// 发送：请求在路上时不能再发（以前要等服务回话才算“在运行”，连按回车会发两次）；发送失败把打的字放回输入框（I03）
let sending = false;
async function send() {
  const input = $('#input');
  const draft = input.value;
  let text = draft.trim();
  if (!text || runId || sending) return;
  sending = true;
  let turn = null, cid = null, release = null;
  try {
    if (opening) await opening.catch(() => {});      // 对话还没打开完：等它打开好再发
    if (runId) { sending = false; return; }           // 打开的对话正在跑
    if (!conv) { conv = await api('/api/convs', { method: 'POST', body: { model: $('#model').value } }); await applyPending(conv.id); await applyPendingPersona(conv.id); }
    cid = conv.id;
    sendCid = cid; sendWait = new Promise(r => { release = r; });
    localStorage.setItem('conv', cid);
    const f = activeFile();
    if (refFile && f) text = `（右边打开的文件：${f}）\n` + text;
    input.value = ''; grow();
    if (chat().querySelector('.empty')) chat().innerHTML = '';
    userBubble(text);
    turn = new Turn(chat());
    turn.model(nameOf($('#model').value)); turn.status('<span class="spin"></span>发送中');
    $('#conv-model').textContent = nameOf($('#model').value);
    chat().scrollTop = chat().scrollHeight;
    const r = await api(`/api/convs/${cid}/send`, { method: 'POST',
      body: { text, model: $('#model').value, web: settings.data.web, think: settings.data.think, level: $('#level').value } });
    if (conv?.id === cid) attach(r.run, turn);       // 发出去以后用户换了对话：不把这次运行接到别的对话的界面上
    loadConvs();
  } catch (e) {
    if (turn && conv?.id === cid) { turn.status(''); turn.flow().innerHTML = `<div class="err-line">${esc(e.message)}（打的字已放回输入框）</div>`; }
    else toast(e.message, 4000);
    if (!input.value.trim()) { input.value = draft; grow(); }
  } finally {
    sending = false;
    if (release) { release(); sendWait = null; sendCid = null; }
  }
}

// 输入框随内容变高，最多 10 行
function grow() {
  const t = $('#input'); t.style.height = 'auto'; t.style.height = Math.min(t.scrollHeight, 220) + 'px';
}

function chips() {
  // 自改、高权限：和设置页的两个开关是同一组设置。状态栏已经有“自改 · 已开启 / 高权限”，这里不再另显示
  const se = settings.data.self_edit_enabled === true, full = se && settings.data.self_edit_full === true;
  $('#chip-se').classList.toggle('on', se);
  $('#chip-sef').classList.toggle('on', full);
  $('#chip-sef').classList.toggle('off', !se);          // 自改没开时高权限是灰的
  $('#chip-file').classList.toggle('on', refFile);
}

// 改自改开关：存到服务端后，状态栏和设置页的开关一起更新
async function saveSelfEdit(patch) {
  try { await saveSettings(patch); } catch (e) { toast(e.message); }
  chips(); emit('selfedit-settings'); emit('selfedit-sync');
}

export async function initChat() {
  $('#model').innerHTML = settings.config.chat_models.map(m => `<option value="${m.id}" data-desc="${esc(m.note || '')}">${esc(m.name)}</option>`).join('');
  $('#model').value = settings.data.default_model;
  $('#model').onchange = () => saveSettings({ default_model: $('#model').value });
  $('#level').value = settings.data.level || 'standard';
  $('#level').onchange = () => { saveSettings({ level: $('#level').value }); dock.level($('#level').value); };
  $('#input').addEventListener('input', grow);
  chips();
  $('#chip-se').onclick = () => {
    const cur = settings.data.self_edit_enabled === true;
    saveSelfEdit({ self_edit_enabled: !cur, ...(cur ? { self_edit_full: false } : {}) });     // 关掉自改，高权限一起关
  };
  $('#chip-sef').onclick = async () => {
    if (settings.data.self_edit_enabled !== true) { toast('先打开“自改”，再开高权限'); return; }
    const full = settings.data.self_edit_full === true;
    // 和设置页一样，打开高权限前要确认
    if (!full && !await confirmBox('打开高权限？', 'Agent 可以修改副本中的所有内容，包括自改机制、提示词、密钥配置、登录和权限检查。通过检查后自动应用并备份，可以撤回。', { ok: '打开' })) return;
    saveSelfEdit({ self_edit_full: !full });
  };
  on('selfedit-sync', chips);          // 设置在别处改了（设置页、服务端），按钮跟着变
  on('selfedit-settings', chips);
  $('#chip-file').onclick = () => { refFile = !refFile; chips(); };
  on('active-file', p => { $('#chip-file').hidden = !p; if (!p) refFile = false; chips(); });
  $('#btn-send').onclick = send;
  $('#btn-stop').onclick = () => runId && api(`/api/runs/${runId}/stop`, { method: 'POST' });
  $('#btn-new').onclick = newConv;
  $('#input').addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
  });
  chat().addEventListener('click', e => {
    const z = e.target.closest('img[data-zoom]'); if (z) { emit('zoom', z.src); return; }
    const dk = e.target.closest('[data-dock]'); if (dk) { dock.open(dk.dataset.dock); return; }
    const r = e.target.closest('[data-reveal]'); if (r) { emit('reveal', r.dataset.reveal); return; }
    const o = e.target.closest('[data-open]'); if (o) emit('open-file', { path: o.dataset.open });
    const mf = e.target.closest('[data-mem-forget]');
    if (mf) {
      api('/api/memory/forget', { method: 'POST', body: { persona: mf.dataset.persona, id: mf.dataset.memForget } })
        .then(() => { mf.closest('li').classList.add('gone'); mf.remove(); toast('已撤销，这条记忆移到了 _已淡忘'); })
        .catch(err => toast(err.message, 4000));
    }
  });
  // Agent 写了文本或图片文件：自动在右边打开
  on('file-changed', d => { if (runId && (TEXT_EXT.test(d.path) || IMG_EXT.test(d.path))) emit('open-file', { path: d.path, auto: true }); });
  // 原地编辑：改名、归档分组。回车保存，Esc 取消，点别处也保存
  const inlineEdit = (c, value, placeholder, save) => {
    const b = c.querySelector('b');
    const inp = document.createElement('input');
    inp.className = 'conv-edit'; inp.value = value; inp.placeholder = placeholder;
    b.replaceWith(inp); inp.focus(); inp.select();
    let done = false;
    const finish = async ok => {
      if (done) return; done = true;
      if (ok && inp.value.trim() !== value) { try { await save(inp.value.trim()); } catch (err) { toast(err.message); } }
      loadConvs();
    };
    inp.addEventListener('keydown', ev => { if (ev.key === 'Enter') finish(true); if (ev.key === 'Escape') finish(false); ev.stopPropagation(); });
    inp.addEventListener('blur', () => finish(true));
    inp.addEventListener('click', ev => ev.stopPropagation());
  };
  const leave = id => { if (conv?.id === id) { conv = null; empty(); dock.load(null); $('#conv-title').textContent = '新对话'; emit('conv', null); project = null; emit('project', null); } };
  const onConvClick = async e => {
    const gh = e.target.closest('.ag-h');
    if (gh) {
      const g = gh.parentElement; g.classList.toggle('closed');
      if (g.classList.contains('closed')) openGroups.delete(g.dataset.g); else openGroups.add(g.dataset.g);
      try { localStorage.setItem('arch-open', JSON.stringify([...openGroups])); } catch (err) {}
      return;
    }
    const c = e.target.closest('.conv'); if (!c) return;
    if (e.target.closest('.conv-more')) { convMenu(e.target.closest('.conv-more'), c); return; }
    if (!c.querySelector('input')) openConv(c.dataset.id);
  };
  // 对话的操作收在“⋯”和右键菜单里，悬停时不再直接露出按钮，免得误点
  const convMenu = (anchor, c, opts = {}) => {
    const arch = c.classList.contains('arch');
    popMenu(anchor, arch
      ? [{ value: 'restore', label: '恢复', desc: '放回上面的对话列表' }, { value: 'group', label: '分组…' }, { sep: true }, { value: 'delete', label: '彻底删除…' }]
      : [{ value: 'rename', label: '改名' }, { value: 'archive', label: '归档…', desc: '移到下面“已归档”，可以恢复' }, { sep: true }, { value: 'delete', label: '彻底删除…' }],
      op => convOp(c, op), opts);
  };
  const convOp = async (c, op) => {
    const id = c.dataset.id, title = c.querySelector('b')?.textContent || '';
    if (op === 'rename') {
      inlineEdit(c, title, '标题', async t => {
        if (!t) return;
        await api('/api/convs/' + id, { method: 'PATCH', body: { title: t } });
        if (conv?.id === id) $('#conv-title').textContent = t;
      });
    } else if (op === 'group') {
      inlineEdit(c, c.dataset.folder || '', '分组名，例如 测试；留空表示不分组', g => api('/api/convs/' + id, { method: 'PATCH', body: { folder: g } }));
    } else if (op === 'archive') {
      if (!await confirmBox('归档这个对话？', `“${title}”会从对话列表移到下面的“已归档”，随时可以恢复。`, { ok: '归档' })) return;
      await api('/api/convs/' + id, { method: 'PATCH', body: { archived: 1 } });
      leave(id); loadConvs(); toast('已归档，在下面“已归档”里可以恢复');
    } else if (op === 'restore') {
      await api('/api/convs/' + id, { method: 'PATCH', body: { archived: 0 } });
      loadConvs(); toast('已恢复');
    } else if (op === 'delete') {
      if (!await confirmBox('彻底删除这个对话？', `“${title}”的对话记录删掉后找不回来。项目文件夹里的文件不受影响。`, { ok: '删除', danger: true })) return;
      try { await api('/api/convs/' + id, { method: 'DELETE' }); leave(id); loadConvs(); }
      catch (err) { toast(err.message, 4000); }
    }
  };
  $('#convs').addEventListener('click', onConvClick);
  $('#archived').addEventListener('click', onConvClick);
  const onConvMenu = e => {
    const c = e.target.closest('.conv'); if (!c || c.querySelector('input')) return;
    e.preventDefault(); convMenu(c, c, { reopen: true });
  };
  $('#convs').addEventListener('contextmenu', onConvMenu);
  $('#archived').addEventListener('contextmenu', onConvMenu);
  const sa = $('#sec-arch');
  sa.querySelector('.sec-h').addEventListener('click', () => sa.classList.toggle('closed'));
  await loadConvs();
  const last = localStorage.getItem('conv');
  if (last) { try { await openConv(last); return; } catch (e) {} }
  empty();
}

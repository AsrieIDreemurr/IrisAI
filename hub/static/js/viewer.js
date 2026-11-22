// 查看器：标签页，文本带行号（本轮写入的行标绿），Markdown 渲染，图片预览。
// 可以上下分成两栏，每栏有自己的标签（例如上面原文、下面译文；上面图片、下面识字）。
import { $, api, emit, esc, fname, md, on, settings, toast } from './api.js';
import { confirmBox, popMenu } from './ui.js';

// 右侧也能放工具页（识字、翻译、设置）：标签的 path 写成 tool:xx。
// 同一个页面元素在“单独页面”和“右侧标签”之间搬来搬去，不复制，所以两边的状态是同一份
const TOOLS = { ocr: '识字', tr: '翻译', set: '设置' };
const TOOL_ICON = { ocr: '#ic-ocr', tr: '#ic-tr', set: '#ic-set' };
const isTool = p => String(p).startsWith('tool:');
const TOOL_EL = {};
const toolEl = id => TOOL_EL[id] ||= document.getElementById('mod-' + id);   // 记住元素：搬来搬去时不会因为暂时不在页面里而找不到
// 工具页元素放回中间那栏（隐藏着），不再显示在右侧
function park(id) {
  const el = toolEl(id); if (!el || !el.classList.contains('docked')) return;
  el.classList.remove('docked'); el.hidden = true;
  document.querySelector('#work > .splitter').before(el);
}

const DRAG_TAB = 'application/x-hub-tab';    // 拖标签到另一栏
const DRAG_PATH = 'application/x-hub-path';  // 从文件管理器、图库拖文件进来

// 每栏：{tabs: [{path, name, data, raw, ver}], active, el}。至少一栏，最多两栏
const groups = [];
let focus = 0;            // 新打开的文件放进哪一栏（最近点过的那栏）
let ratio = 0.5;          // 上面那栏占的高度
try { ratio = Math.min(0.8, Math.max(0.2, +localStorage.getItem('viewSplit') || 0.5)); } catch (e) {}
const marks = new Map();  // 路径 -> 本轮改动的行号
const key = p => p.toLowerCase();
const IMG = /\.(png|jpe?g|webp|gif|bmp)$/i;
// 系统报错换成中文
const zh = msg => /Errno 13|Permission/i.test(msg) ? '没有权限读取' : /Errno 2|No such/i.test(msg) ? '文件不存在' : /Errno/i.test(msg) ? '读取失败' : msg;
const size = n => n >= 1048576 ? (n / 1048576).toFixed(1) + ' MB' : n >= 1024 ? (n / 1024).toFixed(0) + ' KB' : n + ' 字节';
const bump = t => { t.ver = (t.ver || 0) + 1; };
// 编辑：t.edit = {text 当前内容, base 上次保存的内容}
const dirty = t => !!t.edit && t.edit.text !== t.edit.base;

const lf = s => (s || '').replace(/\r\n/g, '\n');

function startEdit(t) {
  const text = lf(t.data.text);
  t.edit = { text, base: text, focus: true }; bump(t); render();
}

// stay：保存后继续编辑（Ctrl+S）；否则回到查看
async function saveEdit(t, stay, force = false) {
  const text = t.edit.text;
  try {
    const d = await api('/api/files/save', { method: 'POST', body: { path: t.path, text, mtime: t.data.mtime, force } });
    t.data = d;
    if (t.edit) t.edit.base = text;
    t.outdated = null; t.gone = false;
    if (!stay) { t.edit = null; bump(t); }
    marks.delete(key(t.path));
    render(); toast('已保存');
  } catch (e) {
    if (/别处被改过/.test(e.message)) {
      if (await confirmBox('文件在别处被改过了', '可能是 Agent 或别的程序刚改过。用你现在的内容覆盖它吗？', { ok: '覆盖' })) return saveEdit(t, stay, true);
      return;
    }
    toast('保存失败：' + e.message, 4000);
  }
}

// 文件在别处被改过（Agent、别的程序、另一个窗口）：重新读。
// 没在编辑：换成新内容，保持滚动位置；编辑着但没改：编辑框换成新内容；已经改了：不动，提示用户自己选
async function refresh(t, who = '') {
  if (t.loading || isTool(t.path)) return;
  if (dirty(t)) { if (t.outdated !== (who || '别处')) { t.outdated = who || '别处'; render(); } return; }
  t.loading = true;
  const g0 = groupOfTab(t), sc = g0?.active === t.path ? g0.el.querySelector('.views .edit-area, .views .view') : null;
  const top = sc?.scrollTop || 0, left = sc?.scrollLeft || 0;
  try { t.data = await api('/api/files/read?path=' + encodeURIComponent(t.path)); t.gone = false; }
  catch (e) { if (t.data && /不存在/.test(e.message)) t.gone = true; else t.data = { error: e.message, kind: 'error' }; }
  t.loading = false; t.outdated = null;
  if (t.edit && !t.gone && t.data.text != null) { const text = lf(t.data.text); t.edit = { text, base: text }; }
  bump(t); render();
  const g = groupOfTab(t), v = g?.active === t.path ? g.el.querySelector('.views .edit-area, .views .view') : null;
  if (v) { v.scrollTop = top; v.scrollLeft = left; }
}

// 定时核对正在显示的文件的修改时间（窗口在前台时每 4 秒，切回窗口时马上）
async function check() {
  if (document.hidden) return;
  const ts = groups.map(g => g.tabs.find(x => x.path === g.active))
    .filter(t => t && !isTool(t.path) && t.data && !t.data.error && t.data.mtime != null && !t.loading);
  if (!ts.length) return;
  let r;
  try { r = await api('/api/files/stat', { method: 'POST', body: { paths: ts.map(t => t.path) }, quiet: true }); } catch (e) { return; }
  for (const t of ts) {
    const m = r.mtimes?.[t.path];
    if (m === undefined) continue;
    if (m === null) { if (!t.gone) { t.gone = true; render(); } continue; }
    if (t.gone) { t.gone = false; render(); }
    if (Math.abs(m - t.data.mtime) > 0.001 && t.seenM !== m) { t.seenM = m; refresh(t); }
  }
}

async function cancelEdit(t) {
  if (dirty(t) && !await confirmBox('放弃改动？', '还没保存的改动会丢掉。', { ok: '放弃改动' })) return;
  const stale = t.outdated;
  t.edit = null; bump(t); render();
  if (stale) { t.outdated = null; refresh(t); }   // 编辑时文件在别处被改过：放弃编辑后换成新内容
}

function find(path) {
  for (const g of groups) { const t = g.tabs.find(x => key(x.path) === key(path)); if (t) return { g, t }; }
  return null;
}
const groupOfTab = t => groups.find(g => g.tabs.includes(t));
const total = () => groups.reduce((n, g) => n + g.tabs.length, 0);

function addGroup() {
  const el = document.createElement('div');
  el.className = 'vgroup';
  el.innerHTML = `<div class="tabs" role="tablist"></div>
    <div class="crumbs" hidden><span class="path"></span><span class="sp"></span><span class="legend"></span></div>
    <div class="views"></div>`;
  const g = { tabs: [], active: null, el };
  groups.push(g);
  layout();
  return g;
}

// 某栏空了而且还有另一栏：去掉这一栏
function dropEmpty() {
  if (groups.length < 2) return;
  const i = groups.findIndex(g => !g.tabs.length && !g.fresh);   // 刚分出来、还没放东西的那栏留着
  if (i < 0) return;
  const [g] = groups.splice(i, 1);
  for (const el of g.el.querySelectorAll('.views > .mod.docked')) park(el.id.slice(4));
  g.el.remove();
  focus = 0;
  layout();
}

const hsplit = document.createElement('div');
hsplit.className = 'vsplit'; hsplit.title = '拖动调整上下高度';

function layout() {
  const box = $('#vgroups'), two = groups.length > 1;
  const kids = two ? [groups[0].el, hsplit, groups[1].el] : [groups[0].el];
  if (kids.some((k, i) => box.children[i] !== k) || box.children.length !== kids.length) box.replaceChildren(...kids);
  box.classList.toggle('split', two);
  groups.forEach((g, i) => {
    g.el.dataset.g = i;
    g.el.style.flex = two ? `${i ? 1 - ratio : ratio} 1 0` : '1 1 0';
  });
}

function renderTabs(g, gi) {
  const two = groups.length > 1;
  const bar = g.el.querySelector('.tabs');
  bar.innerHTML = g.tabs.map(t => `
    <div class="tab${t.path === g.active ? ' on' : ''}${isTool(t.path) ? ' tool' : ''}" draggable="true" data-path="${esc(t.path)}" title="${esc(isTool(t.path) ? t.name : t.data?.show || t.path)}">
      <svg><use href="${isTool(t.path) ? TOOL_ICON[t.path.slice(5)] : t.data?.kind === 'image' ? '#ic-pic' : '#ic-doc'}"/></svg>${esc(t.name)}
      ${dirty(t) ? '<span class="mod dirty" title="还没保存"></span>' : marks.has(key(t.path)) ? '<span class="mod" title="本轮改动"></span>' : ''}<span class="x" data-close="1">×</span></div>`).join('') +
    '<span class="sp"></span>' +
    (two ? `<button type="button" class="tab-split on" title="${gi ? '收起下面这一栏' : '收起上面这一栏'}（标签并到另一栏）"><svg><use href="#ic-split"/></svg></button>`
         : `<button type="button" class="tab-split" title="上下分成两栏"><svg><use href="#ic-split"/></svg></button>`);
  // 选中的标签露出来
  const on = bar.querySelector('.tab.on');
  if (on) {
    if (on.offsetLeft < bar.scrollLeft) bar.scrollLeft = on.offsetLeft - 6;
    else if (on.offsetLeft + on.offsetWidth > bar.scrollLeft + bar.clientWidth) bar.scrollLeft = on.offsetLeft + on.offsetWidth - bar.clientWidth + 6;
  }
}

function body(t) {
  const d = t.data;
  if (!d) return '<div class="empty-view">正在读取……</div>';
  if (d.error && d.kind !== 'binary') return `<div class="empty-view">${esc(zh(d.error))}</div>`;
  if (d.kind === 'dir') {
    return `<div class="view dirview on">${d.entries.length ? d.entries.map(e => `
      <div class="ent" data-open="${esc(e.path)}"><svg><use href="${e.dir ? '#ic-dir' : IMG.test(e.name) ? '#ic-pic' : '#ic-doc'}"/></svg>${esc(e.name)}
      <span class="sz num">${e.dir ? '文件夹' : size(e.size)}</span></div>`).join('') : '<div class="empty-view">空文件夹</div>'}</div>`;
  }
  if (d.kind === 'image') {
    const when = new Date(d.mtime * 1000).toLocaleString('zh-CN');
    return `<div class="view imgview on"><img src="/api/files/raw?path=${encodeURIComponent(d.path)}&t=${d.mtime}" alt="${esc(d.name)}">
      <div class="meta num"><span>${(d.size / 1024).toFixed(0)} KB</span><span>${esc(when)}</span></div></div>`;
  }
  if (d.kind === 'video') {
    const when = new Date(d.mtime * 1000).toLocaleString('zh-CN');
    return `<div class="view vidview on"><video src="/api/files/raw?path=${encodeURIComponent(d.path)}&t=${d.mtime}" controls preload="metadata" playsinline></video>
      <div class="meta num"><span>${(d.size / 2 ** 20).toFixed(1)} MB</span><span>${esc(when)}</span></div></div>`;
  }
  if (d.kind === 'binary') return `<div class="empty-view">这个文件不能以文字显示：${esc(zh(d.error || ''))}</div>`;
  if (t.edit) return '<div class="view editview on"><textarea class="edit-area mono" spellcheck="false" aria-label="编辑文件内容"></textarea></div>';
  if (d.kind === 'markdown' && !t.raw) return `<div class="view md on"><div class="md-body">${md(d.text)}</div></div>`;
  const add = marks.get(key(d.path)) || new Set();
  const lines = d.text.split(/\r?\n/);
  if (lines.length > 1 && lines[lines.length - 1] === '') lines.pop();
  return `<div class="view code mono on">${lines.map((l, i) =>
    `<div class="l${add.has(i + 1) ? ' add' : ''}" data-n="${i + 1}"><span>${i + 1}</span><span>${esc(l) || ' '}</span></div>`).join('')}</div>`;
}

// 只在这一栏显示的内容变了时才重画，另一栏的滚动位置不会因为点了这一栏而丢掉
function renderView(g) {
  const views = g.el.querySelector('.views'), crumbs = g.el.querySelector('.crumbs');
  const t = g.tabs.find(x => x.path === g.active);
  if (t && isTool(t.path)) {
    const el = toolEl(t.path.slice(5));
    // 这一栏原来放着别的工具页（例如上面识字换成翻译）：先放回原处，不能被直接挤掉
    for (const x of views.querySelectorAll(':scope > .mod.docked')) if (x !== el) park(x.id.slice(4));
    if (views.firstElementChild !== el || views.children.length !== 1) views.replaceChildren(el);
    el.hidden = false; el.classList.add('docked');
    crumbs.hidden = true; g.shown = t; g.shownVer = t.ver;
    return;
  }
  for (const el of views.querySelectorAll(':scope > .mod.docked')) park(el.id.slice(4));
  if (!t) {
    crumbs.hidden = true; g.shown = null;
    views.innerHTML = groups.length > 1 ? '<div class="empty-view">在这一栏打开：点左边的文件，或者把标签拖到这里。</div>' : '';
    return;
  }
  if (!t.data && !t.loading) refresh(t);     // 标签被标成要重读（例如 Agent 改过、改了名）：轮到显示时再读
  if (g.shown !== t || g.shownVer !== t.ver || !views.firstElementChild) {
    views.innerHTML = body(t);
    g.shown = t; g.shownVer = t.ver;
    const ta = views.querySelector('.edit-area');
    if (ta) { ta.value = t.edit.text; if (t.edit.focus) { t.edit.focus = false; ta.focus(); ta.setSelectionRange(0, 0); } }
  }
  const d = t.data || {};
  crumbs.hidden = false;
  crumbs.querySelector('.path').textContent = (d.show || t.path).replace(/\\/g, ' › ');
  const n = marks.get(key(t.path))?.size;
  const gone = t.gone ? '<span class="edit-st" title="被移走、改名或删除了">文件已不在原处</span>' : '';
  if (t.edit) {
    crumbs.querySelector('.legend').innerHTML = gone + (t.outdated && !t.gone ? `<span class="edit-st">${t.outdated === 'Agent' ? 'Agent 刚改过这个文件' : '文件在别处被改过'}</span>` +
      '<button class="btn" data-edit-reload="1" title="丢掉你在这里的改动，换成文件现在的内容">载入新版本</button>' : '') +
      (dirty(t) ? '<span class="edit-st">未保存</span>' : '<span>编辑中 · Ctrl+S 保存</span>') +
      '<button class="btn" data-edit-cancel="1">取消</button><button class="btn primary" data-edit-save="1">保存</button>';
    return;
  }
  const canEdit = d.editable && !t.gone && (d.kind === 'text' || d.kind === 'markdown');
  crumbs.querySelector('.legend').innerHTML = gone + (settings.config.local && d.path ? `<button class="btn" data-reveal="${esc(d.path)}">在资源管理器中显示</button>` : '') +
    (canEdit ? '<button class="btn" data-edit="1" title="直接改这个文件">编辑</button>' : '') +
    (d.kind === 'markdown' ? `<span class="chip md-raw">${t.raw ? '看排版' : '看原文'}</span>` : '') +
    (n ? `<span>绿色：本轮写入 ${n} 行</span>` : '') + (d.encoding && d.encoding !== 'utf-8-sig' && d.encoding !== 'utf-8' ? `<span>编码 ${esc(d.encoding)}</span>` : '');
}

// 其他页面要知道“右边正在看的文件”：先看最近点过的那栏，那栏是工具页就看另一栏
let lastFile;
function currentFile() {
  for (const i of [focus, 1 - focus]) {
    const a = groups[i]?.active;
    if (a && !isTool(a)) return a;
  }
  return null;
}

function render() {
  dropEmpty();
  // 没有在任何一栏显示的工具页先放回去
  const shown = new Set(groups.map(g => g.active).filter(p => p && isTool(p)).map(p => p.slice(5)));
  for (const id of Object.keys(TOOLS)) if (!shown.has(id)) park(id);
  groups.forEach((g, i) => {
    if (g.tabs.length) g.fresh = false;
    renderTabs(g, i); renderView(g);
    g.el.classList.toggle('focus', i === focus);
  });
  const empty = !total();
  $('#empty-view').hidden = !empty;
  $('#vgroups').hidden = empty;
  $('#work').classList.toggle('view-empty', empty);
  if (empty) app().classList.remove('view-open');
  const f = currentFile();
  if (f !== lastFile) { lastFile = f; emit('active-file', f); }
}

// 窄屏时查看器平时隐藏，打开文件或工具页时盖住页面，“返回”收起
const app = () => document.getElementById('app');
const narrow = matchMedia('(max-width: 900px)');
function showViewer() { if (narrow.matches) { app().classList.add('view-open'); app().classList.remove('side-open'); } }

// 空白时的提示跟着页面说：生图、知识库页没有左边的文件管理器
const HINTS = {
  chat: '在左边文件管理器里点文件，或点对话里的文件名，会在这里打开。',
  tr: '在左边文件管理器里点要翻译的文件，会在这里打开。',
  ocr: '在左边文件管理器里点图片，会在这里打开。',
  img: '点图片下面的“打开”，会在这里看原图和信息。',
  kb: '点子库里的文件或检索结果，会在这里打开。',
};

// 放在另一栏：和正在看的文件对照（例如译文）。没有正在看的文件就放在当前栏
function besideGroup() {
  const f = currentFile();
  if (!f) return groups[focus];
  const fg = groups.find(g => g.active === f);
  return groups.find(g => g !== fg) || addGroup();
}

// opts：line 跳到某行；reload 重新读；auto Agent 自动打开（窄屏不盖住对话）；beside 放到另一栏；group 指定哪一栏
export async function open(path, opts = {}) {
  if (typeof path !== 'string' || !path.trim() || path === 'null' || path === 'undefined') return;   // 没有路径就不开空标签
  let hit = find(path), g, t;
  if (hit) ({ g, t } = hit);
  else {
    g = opts.group != null ? groups[opts.group] || groups[focus] : opts.beside ? besideGroup() : groups[focus];
    t = { path, name: fname(path) };
    g.tabs.push(t);
  }
  g.active = t.path;
  if (opts.beside && !opts.auto) focus = groups.indexOf(g);
  if (!opts.reload && !opts.auto) showViewer();   // Agent 运行中自动打开的文件不盖住对话
  if (!t.data || opts.reload) {
    render();
    try {
      t.data = await api('/api/files/read?path=' + encodeURIComponent(t.path));
      if (t.data.path && t.data.path !== t.path) {
        const now = groupOfTab(t);
        if (now?.active === t.path) now.active = t.data.path;
        t.path = t.data.path;
      }
    } catch (e) { t.data = { error: e.message, kind: 'error' }; }
    bump(t);
  }
  if (!groupOfTab(t)) return;     // 读的时候标签被关了
  if (opts.line && t.data?.kind === 'markdown' && !t.raw) { t.raw = true; bump(t); }   // 要跳到某一行时显示原文（带行号）
  render();
  if (t.data?.kind === 'dir') emit('reveal-tree', t.data.path);
  if (opts.line) {
    const l = groupOfTab(t)?.el.querySelector(`.views .l[data-n="${opts.line}"]`);
    if (l) { l.scrollIntoView({ block: 'center' }); l.animate([{ outline: '2px solid var(--accent)' }, { outline: '2px solid transparent' }], 1400); }
  }
}

// 在右侧打开一个工具页（已经开着就切过去）。右边正开着文件时放到另一栏，上下对照着用
export function dockTool(id) {
  if (!TOOLS[id]) return;
  const path = 'tool:' + id, hit = find(path);
  if (hit) { hit.g.active = path; focus = groups.indexOf(hit.g); render(); showViewer(); return; }
  const g = currentFile() ? besideGroup() : groups[focus];
  g.tabs.push({ path, name: TOOLS[id], data: { kind: 'tool' } });
  g.active = path; focus = groups.indexOf(g);
  render(); showViewer();
}

// 工具页要回到单独的页面：右侧那个标签关掉
export function undockTool(id) {
  const hit = find('tool:' + id);
  if (hit) close(hit.g, hit.t.path);
  park(id);
}

async function close(g, path, ask = true) {
  const t = g.tabs.find(x => x.path === path);
  if (!t) return;
  if (ask && dirty(t) && !await confirmBox('关掉这个标签？', `“${t.name}”还有没保存的改动，关掉会丢掉。`, { ok: '不保存，关掉' })) return;
  const i = g.tabs.indexOf(t);
  if (i < 0) return;
  g.tabs.splice(i, 1);
  if (isTool(path)) park(path.slice(5));
  if (g.active === path) g.active = g.tabs[Math.max(0, i - 1)]?.path || null;
  render();
}

// 把标签挪到另一栏；只有一栏时先分出下面一栏
function moveTab(from, path, to) {
  const t = from.tabs.find(x => x.path === path);
  if (!t || from === to) return;
  if (!to) to = groups.find(x => x !== from) || addGroup();
  from.tabs.splice(from.tabs.indexOf(t), 1);
  if (from.active === path) from.active = from.tabs[0]?.path || null;
  to.tabs.push(t); to.active = t.path;
  focus = groups.indexOf(to);
  if (!from.tabs.length) focus = 0;   // 原来那栏空了会被去掉
  render();
}

// 标签栏上的分栏按钮：一栏时分成上下两栏（当前标签多于一个时，把选中的挪到下面）；两栏时把这一栏并到另一栏
function toggleSplit(g) {
  if (groups.length < 2) {
    if (g.tabs.length > 1) { moveTab(g, g.active, null); return; }
    addGroup().fresh = true; focus = 1; render(); return;
  }
  const other = groups.find(x => x !== g);
  g.fresh = false;
  for (const t of g.tabs) other.tabs.push(t);
  if (!other.active) other.active = g.active;
  g.tabs = [];
  focus = groups.indexOf(other);
  render();
}

export function activeFile() { return currentFile(); }

export function initViewer() {
  const box = $('#vgroups');
  addGroup();
  const gOf = el => groups[+el?.closest('.vgroup')?.dataset.g] || null;

  box.addEventListener('pointerdown', e => {
    const g = gOf(e.target); if (!g) return;
    const i = groups.indexOf(g);
    if (i !== focus) { focus = i; groups.forEach((x, k) => x.el.classList.toggle('focus', k === focus));
      const f = currentFile(); if (f !== lastFile) { lastFile = f; emit('active-file', f); } }
  });

  box.addEventListener('click', e => {
    const g = gOf(e.target); if (!g) return;
    const cur = () => g.tabs.find(x => x.path === g.active);
    if (e.target.closest('[data-edit]')) { startEdit(cur()); return; }
    if (e.target.closest('[data-edit-save]')) { saveEdit(cur(), false); return; }
    if (e.target.closest('[data-edit-cancel]')) { cancelEdit(cur()); return; }
    if (e.target.closest('[data-edit-reload]')) {
      const t = cur();
      confirmBox('载入新版本？', '你在这里还没保存的改动会丢掉，换成文件现在的内容。', { ok: '载入新版本' })
        .then(ok => { if (ok && t.edit) { t.edit.text = t.edit.base; refresh(t); } });
      return;
    }
    if (e.target.closest('.tab-split')) { toggleSplit(g); return; }
    const tab = e.target.closest('.tab');
    if (tab) { if (e.target.dataset.close) close(g, tab.dataset.path); else { g.active = tab.dataset.path; render(); check(); } return; }
    const rv = e.target.closest('[data-reveal]'); if (rv) { emit('reveal', rv.dataset.reveal); return; }
    if (e.target.closest('.md-raw')) { const t = g.tabs.find(x => x.path === g.active); t.raw = !t.raw; bump(t); render(); return; }
    const l = e.target.closest('.views .l'); if (l) $('#st-pos').textContent = `第 ${l.dataset.n} 行`;
    const img = e.target.closest('.imgview img'); if (img) emit('zoom', img.src);
    const ent = e.target.closest('.dirview .ent'); if (ent) open(ent.dataset.open, { group: groups.indexOf(g) });
  });
  box.addEventListener('auxclick', e => { const tab = e.target.closest('.tab'), g = gOf(e.target); if (tab && g && e.button === 1) close(g, tab.dataset.path); });

  // 右键标签：挪到另一栏、关闭
  box.addEventListener('contextmenu', e => {
    const tab = e.target.closest('.tab'), g = gOf(e.target); if (!tab || !g) return;
    e.preventDefault();
    const gi = groups.indexOf(g), two = groups.length > 1, path = tab.dataset.path;
    popMenu(tab, [
      { value: 'move', label: two ? (gi ? '移到上面一栏' : '移到下面一栏') : '移到下面一栏', off: !two && g.tabs.length < 2,
        desc: !two && g.tabs.length < 2 ? '先打开另一个文件' : '' },
      { sep: true },
      { value: 'close', label: '关闭' },
      { value: 'others', label: '关闭这一栏的其他标签', off: g.tabs.length < 2 },
    ], v => {
      if (v === 'move') moveTab(g, path, groups.find(x => x !== g) || null);
      else if (v === 'close') close(g, path);
      else if (v === 'others') (async () => {
        const rest = g.tabs.filter(t => t.path !== path), n = rest.filter(dirty).length;
        if (n && !await confirmBox('关掉其他标签？', `其中 ${n} 个还有没保存的改动，关掉会丢掉。`, { ok: '不保存，关掉' })) return;
        for (const t of rest) { g.tabs.splice(g.tabs.indexOf(t), 1); if (isTool(t.path)) park(t.path.slice(5)); }
        g.active = path; render();
      })();
    }, { reopen: true });
  });

  // 标签栏：滚轮左右滚动（标签多到放不下时）
  box.addEventListener('wheel', e => {
    const bar = e.target.closest('.tabs');
    if (!bar || bar.scrollWidth <= bar.clientWidth || Math.abs(e.deltaX) > Math.abs(e.deltaY)) return;
    bar.scrollLeft += e.deltaY; e.preventDefault();
  }, { passive: false });

  // 拖动：标签拖到另一栏；只有一栏时拖到下半部分就分出下面一栏。文件管理器、图库里的文件拖进来就在那一栏打开
  let dragTab = null;
  box.addEventListener('dragstart', e => {
    const tab = e.target.closest('.tab'), g = gOf(e.target); if (!tab || !g) return;
    dragTab = { g, path: tab.dataset.path };
    e.dataTransfer.setData(DRAG_TAB, tab.dataset.path); e.dataTransfer.effectAllowed = 'move';
  });
  box.addEventListener('dragend', () => { dragTab = null; clearDrop(); });
  const clearDrop = () => groups.forEach(g => g.el.classList.remove('drop-here', 'drop-below'));
  const zone = e => {
    const types = e.dataTransfer.types, g = gOf(e.target);
    if (!g || !(types.includes(DRAG_TAB) || types.includes(DRAG_PATH))) return null;
    const views = g.el.querySelector('.views').getBoundingClientRect();
    const lower = e.clientY > views.top + views.height / 2;
    if (groups.length < 2 && lower && (!dragTab || dragTab.g.tabs.length > 1)) return { g, below: true };
    if (dragTab && dragTab.g === g) return null;
    return { g, below: false };
  };
  box.addEventListener('dragover', e => {
    const z = zone(e); clearDrop(); if (!z) return;
    e.preventDefault(); e.dataTransfer.dropEffect = 'move';
    z.g.el.classList.add(z.below ? 'drop-below' : 'drop-here');
  });
  box.addEventListener('dragleave', e => { if (!box.contains(e.relatedTarget)) clearDrop(); });
  box.addEventListener('drop', e => {
    const z = zone(e); clearDrop(); if (!z) return;
    e.preventDefault();
    const target = z.below ? null : z.g;
    if (dragTab) { moveTab(dragTab.g, dragTab.path, target); dragTab = null; return; }
    const p = e.dataTransfer.getData(DRAG_PATH); if (!p) return;
    const hit = find(p);
    if (!hit) { open(p, { group: groups.indexOf(target || addGroup()) }); return; }
    if (target === hit.g || (!target && hit.g.tabs.length < 2)) { hit.g.active = hit.t.path; render(); }
    else moveTab(hit.g, hit.t.path, target);
  });

  // 上下两栏之间的分隔条
  hsplit.addEventListener('pointerdown', e => {
    hsplit.setPointerCapture(e.pointerId); hsplit.classList.add('drag');
    const r = box.getBoundingClientRect();
    const move = ev => { ratio = Math.min(0.8, Math.max(0.2, (ev.clientY - r.top) / r.height)); layout(); };
    const up = () => { hsplit.classList.remove('drag'); hsplit.removeEventListener('pointermove', move); hsplit.removeEventListener('pointerup', up);
                       try { localStorage.setItem('viewSplit', ratio.toFixed(3)); } catch (err) {} };
    hsplit.addEventListener('pointermove', move); hsplit.addEventListener('pointerup', up);
  });
  hsplit.addEventListener('dblclick', () => { ratio = 0.5; layout(); try { localStorage.setItem('viewSplit', '0.5'); } catch (err) {} });

  // 编辑框：记下内容；Ctrl+S 保存后接着编辑；Tab 输入制表符
  box.addEventListener('input', e => {
    const ta = e.target.closest('.edit-area'), g = gOf(ta); if (!ta || !g) return;
    const t = g.tabs.find(x => x.path === g.active); if (!t?.edit) return;
    const was = dirty(t); t.edit.text = ta.value;
    if (was !== dirty(t)) render();
  });
  box.addEventListener('keydown', e => {
    const ta = e.target.closest('.edit-area'), g = gOf(ta); if (!ta || !g) return;
    const t = g.tabs.find(x => x.path === g.active); if (!t?.edit) return;
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') { e.preventDefault(); t.edit.text = ta.value; saveEdit(t, true); }
    else if (e.key === 'Tab' && !e.shiftKey && !e.ctrlKey) { e.preventDefault(); document.execCommand('insertText', false, '\t'); }
    else if (e.key === 'Escape') e.preventDefault();     // 不让 Esc 收起窄屏的查看器而丢掉焦点
  });
  addEventListener('beforeunload', e => { if (groups.some(g => g.tabs.some(dirty))) { e.preventDefault(); e.returnValue = ''; } });

  // 底部的“＋”：在右边打开识字、翻译、设置。右边开着文件时放到另一栏
  $('#btn-dock').addEventListener('click', e => {
    popMenu(e.currentTarget, Object.entries(TOOLS).map(([id, name]) => ({ value: id, label: name,
      desc: { ocr: '贴截图识别文字，结果可以送进对话或翻译', tr: '翻译文字或右边打开的文件', set: '外观、装饰、Agent 自改' }[id],
      on: !!find('tool:' + id) })), id => emit('dock-tool', id), { title: '在右边打开' });
  });

  // 中键点文件名（对话里、知识库、翻译结果等带 data-open 的地方）：在另一栏打开，和正在看的对照
  document.addEventListener('mousedown', e => { if (e.button === 1 && e.target.closest('[data-open]')) e.preventDefault(); });
  document.addEventListener('auxclick', e => {
    if (e.button !== 1) return;
    const o = e.target.closest('[data-open]'); if (!o || o.closest('#vgroups')) return;
    e.preventDefault(); emit('open-file', { path: o.dataset.open, beside: true });
  });

  setInterval(check, 4000);
  addEventListener('focus', check);
  document.addEventListener('visibilitychange', check);

  on('open-file', d => open(d?.path, d || {}));
  on('mod', m => { if (HINTS[m]) $('#empty-hint').textContent = HINTS[m]; });
  // 文件管理器里移动、改名：打开着的标签换成新路径；删除：关掉
  const inside = (p, base) => key(p) === key(base) || key(p).startsWith(key(base) + '\\');
  on('files-moved', ({ src, dst }) => {
    for (const g of groups) {
      let hit = false;
      for (const t of g.tabs) if (!isTool(t.path) && inside(t.path, src)) {
        const np = dst + t.path.slice(src.length);
        if (g.active === t.path) g.active = np;
        t.path = np; t.name = fname(np); hit = true;
        // 编辑着的保留内容只改路径；其他的标成要重读，显示时再读
        if (t.edit && t.data) t.data = { ...t.data, path: np, show: undefined }; else t.data = null;
        bump(t);
      }
    }
    render();
  });
  on('files-trashed', ({ path }) => {
    for (const g of groups) for (const t of [...g.tabs]) if (!isTool(t.path) && inside(t.path, path)) close(g, t.path, false);
  });
  $('#btn-view-back').onclick = () => app().classList.remove('view-open');
  // Agent 改了文件：记下改动的行；如果正显示着就刷新
  on('file-changed', d => {
    if (d.lines?.length) marks.set(key(d.path), new Set(d.lines));
    const hit = d.path && find(d.path);
    if (!hit) { render(); return; }
    if (hit.g.active === hit.t.path || dirty(hit.t)) refresh(hit.t, 'Agent');
    else { hit.t.data = null; bump(hit.t); render(); }
  });
  render();
}

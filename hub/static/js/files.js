// 文件管理器：默认只显示当前页面对应的文件夹（对话的项目文件夹、翻译、识字、图片、知识库），“全部”开关显示整个沙箱和授权目录。
// 点文件夹展开，点文件在右边打开。右键：新建文件夹、改名、移到回收站、彻底删除（2026-11-14），回收站右键可以清空；
// 拖到文件夹上就是移动（生图页图库里的图也能拖）。
// 只有沙箱和设成可写的授权目录能整理，根目录本身不能动。
import { $, api, emit, esc, on, settings, toast } from './api.js';
import { confirmBox, popMenu, promptBox } from './ui.js';

const ICON = { dir: '#ic-dir', img: '#ic-pic', doc: '#ic-doc' };
const IMG = /\.(png|jpe?g|webp|gif|bmp)$/i;
const expanded = new Set(JSON.parse(localStorage.getItem('expanded') || '[]'));
const changed = new Map();   // 路径 -> '新建' / '已改'
const closed = new Set(JSON.parse(localStorage.getItem('closed-secs') || '[]'));   // 折叠起来的分区
function saveClosed() { try { localStorage.setItem('closed-secs', JSON.stringify([...closed])); } catch (e) {} }
let active = null;
let focus = null;            // {name, path, hint}：当前页面对应的文件夹
let showAll = localStorage.getItem('files-all') === '1';

const key = p => p.toLowerCase();
function saveExpanded() { try { localStorage.setItem('expanded', JSON.stringify([...expanded])); } catch (e) {} }

function nodeHtml(e, depth) {
  const isOpen = e.dir && expanded.has(key(e.path));
  const badge = changed.get(key(e.path));
  return `<div class="node${active && key(active) === key(e.path) ? ' on' : ''}${curDir && key(curDir) === key(e.path) ? ' cur' : ''}" style="padding-left:${12 + depth * 16}px"
            data-path="${esc(e.path)}" data-dir="${e.dir ? 1 : ''}" data-depth="${depth}" title="${esc(e.path)}"${canWrite(e.path) ? ' draggable="true"' : ''}>
    <span class="tw">${e.dir ? (isOpen ? '⌄' : '›') : ''}</span>
    <svg><use href="${e.dir ? ICON.dir : IMG.test(e.name) ? ICON.img : ICON.doc}"/></svg>${esc(e.name)}
    ${badge ? `<span class="badge chg">${badge}</span>` : ''}
    ${settings.config.local ? `<button class="reveal" data-reveal="${esc(e.path)}" title="在资源管理器中显示">打开位置</button>` : ''}</div>
    ${isOpen ? `<div class="kids" data-for="${esc(e.path)}"></div>` : ''}`;
}

async function fill(container, path, depth) {
  let list;
  try { list = await api('/api/files/list?path=' + encodeURIComponent(path)); }
  catch (err) { container.innerHTML = `<div class="tree-empty">${esc(/不存在/.test(err.message) ? '还没有文件' : err.message)}</div>`; return; }
  container.innerHTML = list.length ? list.map(e => nodeHtml(e, depth)).join('') : '<div class="tree-empty">空文件夹</div>';
  await Promise.all([...container.querySelectorAll(':scope > .kids')].map(k => fill(k, k.dataset.for, depth + 1)));
}

// 页面切换或打开别的对话时调用
export function setFocus(f) {
  focus = f;
  return refresh();
}

export async function refresh() {
  const all = await api('/api/files/roots');
  $('#btn-files-all').classList.toggle('on', showAll);
  $('#side-label').textContent = showAll ? '文件管理器 · 全部' : '文件管理器';
  if (!showAll && !focus?.path) {
    roots = all;
    $('#roots').innerHTML = `<div class="tree-empty" style="padding:6px 16px">${esc(focus?.hint || '这个页面没有对应的文件夹。点右上角“全部”看整个沙箱。')}</div>`;
    return;
  }
  roots = showAll ? all : [{ name: focus.name, path: focus.path, mode: 'rw', sandbox: true }];
  // 先在页面外面建好整棵树再换上去，避免刷新时内容先变空、滚动位置跳回顶部
  const box = document.createElement('div');
  box.innerHTML = roots.map(r => `
    <div class="section${closed.has('root:' + key(r.path)) ? ' closed' : ''}" data-sec="root:${esc(key(r.path))}">
      <div class="sec-h" title="${esc(r.path)}" data-root="${esc(r.path)}">${esc(r.name)}${r.sandbox ? '' : r.mode === 'ro' ? ' · 只读' : ' · 可写'}</div>
      <div class="sec-body tree" data-tree="${esc(r.path)}"></div>
    </div>`).join('');
  await Promise.all([...box.querySelectorAll('[data-tree]')].map(t => fill(t, t.dataset.tree, 0)));
  $('#roots').replaceChildren(...box.children);
}

function toggleSec(sec) {
  sec.classList.toggle('closed');
  if (sec.classList.contains('closed')) closed.add(sec.dataset.sec); else closed.delete(sec.dataset.sec);
  saveClosed();
}

let roots = [];
let timer;

// ---------------------------------------------------------------- 整理文件
const DRAG = 'application/x-hub-path';
let curDir = null;          // 最近点过的文件夹：“新建文件夹”按钮建在这里
const trim = p => p.replace(/[\\/]+$/, '');
const under = (p, base) => key(p) === key(trim(base)) || key(p).startsWith(key(trim(base)) + '\\');
const rootOf = p => roots.find(r => under(p, r.path));
const canWrite = p => { const r = rootOf(p); return !!r && (r.sandbox || r.mode === 'rw'); };
const isRoot = p => roots.some(r => key(trim(r.path)) === key(trim(p)));
const parentOf = p => p.replace(/[\\/][^\\/]+$/, '');
const baseName = p => trim(p).split(/[\\/]/).pop();
const recycleBin = () => trim(settings.config.sandbox || '') + '\\.回收站';
const inRecycle = p => !!settings.config.sandbox && under(p, recycleBin()) && key(trim(p)) !== key(recycleBin());
function show(p) {
  const sb = trim(settings.config.sandbox || '');
  return sb && under(p, sb) ? '沙箱' + p.slice(sb.length) : p;
}

async function post(url, body) {
  try { return await api(url, { method: 'POST', body }); }
  catch (e) { toast(e.message, 4000); return null; }
}

export async function newFolder(parent) {
  if (!parent || !canWrite(parent)) { toast('这里是只读的，不能新建文件夹'); return; }
  const name = await promptBox('新建文件夹', { text: '建在 ' + show(parent), placeholder: '文件夹名字', ok: '新建' });
  if (!name) return;
  const r = await post('/api/files/mkdir', { parent, name });
  if (!r) return;
  expanded.add(key(parent)); saveExpanded();
  curDir = r.path;
  emit('files-changed', { path: r.path });
  await reveal(r.path);
}

async function rename(p) {
  const old = baseName(p);
  const name = await promptBox('改名', { text: show(p), value: old, ok: '改名' });
  if (!name || name === old) return;
  const r = await post('/api/files/move', { src: p, name });
  if (r?.moved) moved(p, r.path);
}

async function trash(p, isDir) {
  if (!await confirmBox(`删除${isDir ? '文件夹' : ''}“${baseName(p)}”？`, '移到 沙箱\\.回收站，需要时可以从那里找回来。', { ok: '删除', danger: true })) return;
  if (!await post('/api/files/trash', { path: p })) return;
  if (curDir && under(curDir, p)) curDir = null;
  emit('files-trashed', { path: p });
  toast('已移到回收站');
  refresh();
}

// 彻底删除：不进回收站，不能恢复
async function destroy(p, isDir) {
  const what = `${isDir ? '文件夹' : ''}“${baseName(p)}”`;
  if (!await confirmBox(`彻底删除${what}？`, `${show(p)}${isDir ? ' 和里面的所有东西' : ''}会直接删掉，不进回收站，不能恢复。`, { ok: '彻底删除', danger: true })) return;
  if (!await post('/api/files/delete', { path: p })) return;
  if (curDir && under(curDir, p)) curDir = null;
  emit('files-trashed', { path: p });
  toast('已彻底删除');
  refresh();
}

async function emptyRecycle() {
  if (!await confirmBox('清空回收站？', '沙箱\\.回收站 里的所有东西会直接删掉，不能恢复。', { ok: '清空', danger: true })) return;
  const r = await post('/api/files/empty-trash', {});
  if (!r) return;
  toast(r.failed.length ? `删了 ${r.deleted.length} 项，${r.failed.length} 项没删掉（${r.failed[0].why}）` : `回收站已清空（${r.deleted.length} 项）`, 4000);
  refresh();
}

export async function moveTo(src, dir) {
  if (key(parentOf(src)) === key(trim(dir)) || under(dir, src)) return;
  const r = await post('/api/files/move', { src, dst: dir });
  if (r?.moved) { expanded.add(key(trim(dir))); saveExpanded(); moved(src, r.path); }
}

function moved(src, dst) {
  // 展开过的子文件夹、当前文件夹跟着换路径
  const k = key(src);
  for (const e of [...expanded]) if (e === k || e.startsWith(k + '\\')) { expanded.delete(e); expanded.add(key(dst) + e.slice(k.length)); }
  saveExpanded();
  if (curDir && under(curDir, src)) curDir = dst + curDir.slice(src.length);
  emit('files-moved', { src, dst });
  reveal(dst);
}

function menuFor(anchor, p, isDir) {
  const root = isRoot(p), w = canWrite(p), ro = w ? '' : '只读';
  const items = [];
  if (isDir && !(settings.config.sandbox && under(p, recycleBin()))) items.push({ value: 'mkdir', label: '新建文件夹', desc: ro, off: !w });   // 回收站里不能建
  if (settings.config.sandbox && key(trim(p)) === key(recycleBin())) {
    items.push({ value: 'empty', label: '清空回收站', desc: '彻底删除里面所有东西' });
  } else if (inRecycle(p)) {
    items.push({ value: 'destroy', label: '彻底删除', desc: '不能恢复' });
  } else if (!root) {
    items.push({ value: 'rename', label: '改名', desc: ro, off: !w });
    items.push({ value: 'trash', label: '删除', desc: w ? '移到回收站，可以找回' : ro, off: !w });
    items.push({ value: 'destroy', label: '彻底删除', desc: w ? '不进回收站，不能恢复' : ro, off: !w });
  }
  if (settings.config.local) { if (items.length) items.push({ sep: true }); items.push({ value: 'reveal', label: '在资源管理器中显示' }); }
  popMenu(anchor, items, v => {
    if (v === 'mkdir') newFolder(p);
    else if (v === 'rename') rename(p);
    else if (v === 'trash') trash(p, isDir);
    else if (v === 'destroy') destroy(p, isDir);
    else if (v === 'empty') emptyRecycle();
    else if (v === 'reveal') emit('reveal', p);
  }, { title: baseName(p) || p, reopen: true });
}

// 拖动：文件管理器里的文件、文件夹，生图页图库里的图，拖到文件夹或根目录标题上
function dropTarget(e) {
  const t = e.target.closest('.node[data-dir="1"], .sec-h[data-root]');
  if (!t || !e.dataTransfer.types.includes(DRAG)) return null;
  const p = t.dataset.path || t.dataset.root;
  return canWrite(p) ? { el: t, path: p } : null;
}

// 展开到某个文件所在的位置，并闪一下
export async function reveal(path, refreshFirst = true) {
  const k = key(path);
  const SEP = '\\';
  const trim = p => p.replace(/[\\/]+$/, '');
  const root = roots.find(r => k === key(r.path) || k.startsWith(key(trim(r.path)) + SEP));
  if (!root) return;
  let cur = trim(root.path);
  const parts = path.slice(root.path.length).split(/[\\/]/).filter(Boolean);
  for (const part of parts.slice(0, -1)) { cur = cur + SEP + part; expanded.add(key(cur)); }
  saveExpanded();
  const sk = 'root:' + key(root.path);
  if (closed.has(sk)) { closed.delete(sk); saveClosed(); }
  if (refreshFirst) await refresh();
  const n = [...document.querySelectorAll('#roots .node')].find(x => key(x.dataset.path) === k);
  if (n) { n.scrollIntoView({ block: 'nearest' }); n.classList.remove('flash'); void n.offsetWidth; n.classList.add('flash'); }
}

// Agent 改了文件：稍等一下再刷新，展开到它所在的文件夹
export function fileChanged(path, isNew) {
  markChanged(path, isNew);
  clearTimeout(timer);
  timer = setTimeout(() => reveal(path), 300);
}

export function markChanged(path, isNew) {
  if (!changed.has(key(path)) || changed.get(key(path)) !== '新建') changed.set(key(path), isNew ? '新建' : '已改');
}

export function initFiles() {
  $('#roots').addEventListener('click', async e => {
    const rv = e.target.closest('[data-reveal]');
    if (rv) { e.stopPropagation(); emit('reveal', rv.dataset.reveal); return; }
    const h = e.target.closest('.sec-h');
    if (h) { toggleSec(h.parentElement); return; }
    const n = e.target.closest('.node');
    if (!n) return;
    const p = n.dataset.path;
    if (n.dataset.dir) {
      curDir = p;
      document.querySelectorAll('#roots .node.cur').forEach(x => x.classList.remove('cur'));
      n.classList.add('cur');
      if (expanded.has(key(p))) {
        expanded.delete(key(p));
        n.querySelector('.tw').textContent = '›';
        n.nextElementSibling?.classList.contains('kids') && n.nextElementSibling.remove();
      } else {
        expanded.add(key(p));
        n.querySelector('.tw').textContent = '⌄';
        const k = document.createElement('div'); k.className = 'kids'; k.dataset.for = p;
        n.after(k); await fill(k, p, +n.dataset.depth + 1);
      }
      saveExpanded();
    } else {
      emit('open-file', { path: p });
    }
  });
  // 中键点文件：在右边另一栏打开，和正在看的上下对照
  $('#roots').addEventListener('mousedown', e => { if (e.button === 1 && e.target.closest('.node')) e.preventDefault(); });
  $('#roots').addEventListener('auxclick', e => {
    const n = e.target.closest('.node'); if (e.button !== 1 || !n || n.dataset.dir) return;
    e.preventDefault(); emit('open-file', { path: n.dataset.path, beside: true });
  });
  $('#roots').addEventListener('contextmenu', e => {
    const n = e.target.closest('.node'), h = e.target.closest('.sec-h[data-root]');
    if (!n && !h) return;
    e.preventDefault();
    if (n) menuFor(n, n.dataset.path, !!n.dataset.dir);
    else menuFor(h, h.dataset.root, true);
  });
  $('#roots').addEventListener('dragstart', e => {
    const n = e.target.closest('.node[draggable="true"]'); if (!n) return;
    e.dataTransfer.setData(DRAG, n.dataset.path); e.dataTransfer.effectAllowed = 'move';
  });
  $('#roots').addEventListener('dragover', e => {
    const t = dropTarget(e); if (!t) return;
    e.preventDefault(); e.dataTransfer.dropEffect = 'move';
    document.querySelectorAll('#roots .dragover').forEach(x => x !== t.el && x.classList.remove('dragover'));
    t.el.classList.add('dragover');
  });
  $('#roots').addEventListener('dragleave', e => { const t = e.target.closest('.dragover'); if (t && !t.contains(e.relatedTarget)) t.classList.remove('dragover'); });
  $('#roots').addEventListener('drop', e => {
    const t = dropTarget(e);
    document.querySelectorAll('#roots .dragover').forEach(x => x.classList.remove('dragover'));
    if (!t) return;
    e.preventDefault();
    const src = e.dataTransfer.getData(DRAG);
    if (src) moveTo(src, t.path);
  });
  // 标题栏的“新建文件夹”：建在最近点过的文件夹里，没点过就建在当前页面的文件夹
  $('#btn-mkdir').onclick = () => {
    const visible = p => p && roots.some(r => under(p, r.path));
    newFolder(visible(curDir) ? curDir : showAll ? settings.config.sandbox : focus?.path);
  };
  const cs = $('#sec-convs');
  if (closed.has('convs')) cs.classList.add('closed');
  cs.querySelector('.sec-h').addEventListener('click', () => toggleSec(cs));
  $('#btn-refresh').onclick = refresh;
  $('#btn-files-all').onclick = () => {
    showAll = !showAll;
    try { localStorage.setItem('files-all', showAll ? '1' : '0'); } catch (e) {}
    refresh();
  };
  on('active-file', p => {
    active = p;
    document.querySelectorAll('#roots .node').forEach(n => n.classList.toggle('on', !!p && key(n.dataset.path) === key(p)));
  });
  return refresh();
}

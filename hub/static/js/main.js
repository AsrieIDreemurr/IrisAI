// 入口：读取配置，初始化各模块，处理布局（侧栏、分隔条、弹出层、看大图）。
import { $, api, emit, on, settings, toast } from './api.js';
import { initLook } from './look.js';
import { initFiles, refresh as refreshFiles, fileChanged, reveal, setFocus } from './files.js';
import { initViewer, dockTool, undockTool } from './viewer.js';
import { initChat, openConv, currentProject } from './chat.js';
import { initDock } from './dock.js';
import { initPalette } from './palette.js';
import { initTranslate } from './translate.js';
import { initOcr } from './ocr.js';
import { initImage } from './image.js';
import { initKb } from './kb.js';
import { initKbPick } from './kbpick.js';
import { initPersonaPick } from './personapick.js';
import { initSelfEdit } from './selfedit.js';
import { enhanceAll } from './ui.js';
import { initWinFrame } from './winframe.js';
import { initInkFrame } from './inkframe.js';

// 转圈图标经常被整块重画，重画后动画会从头开始，看起来一顿一顿的。
// 每个新出现的转圈都按同一个时钟对齐相位，重画前后角度连续。
const SPIN_MS = 800;
new MutationObserver(muts => {
  for (const m of muts) for (const n of m.addedNodes) {
    if (n.nodeType !== 1) continue;
    const list = n.classList.contains('spin') ? [n] : n.querySelectorAll('.spin');
    for (const s of list) s.style.animationDelay = `-${(performance.now() % SPIN_MS).toFixed(0)}ms`;
  }
}).observe(document.documentElement, { childList: true, subtree: true });
import { initStatus } from './status.js';

async function main() {
  const cfg = await api('/api/config');
  settings.config = cfg; settings.data = cfg.settings;
  initLook();
  initViewer();
  initStatus();
  initDock();
  initPalette();
  await initFiles();
  await initChat();
  initOcr();
  await initTranslate();
  await initImage();
  await initKb();
  await initKbPick();
  await initPersonaPick();
  initSelfEdit();
  enhanceAll();      // 所有下拉框换成统一的下拉菜单（原生 select 留着做数据来源）
  initWinFrame();   // 独立窗口里：顶栏当标题栏用（浏览器里不起作用）
  initInkFrame();   // 主题要手画墨线框时（--ink-sel）给部件画框

  on('files-refresh', () => refreshFiles());
  on('open-conv', id => openConv(id));
  on('file-changed', d => fileChanged(d.path, d.new));
  on('reveal-tree', p => reveal(p));
  on('reveal', async p => {
    reveal(p);
    try { await api('/api/files/reveal', { method: 'POST', body: { path: p } }); } catch (e) { toast(e.message); }
  });
  on('zoom', src => {
    const z = document.createElement('div'); z.className = 'zoom'; z.innerHTML = `<img src="${src}" alt="">`;
    z.onclick = () => z.remove(); document.body.appendChild(z);
  });

  // 刷新：独立窗口（WebView2）里浏览器自带的快捷键是关掉的，这里自己处理 F5 和 Ctrl+R
  document.addEventListener('keydown', e => {
    if (e.key === 'F5' || ((e.ctrlKey || e.metaKey) && (e.key === 'r' || e.key === 'R'))) { e.preventDefault(); location.reload(); }
  });
  // 侧栏开关
  // 未完成的模块
  document.querySelectorAll('.activity a.todo').forEach(a => a.onclick = () => toast(a.title));
  // 窄屏：侧栏是抽屉，标题栏左边的按钮开关；点外面、选了对话、按 Esc 收起
  const setDrawer = open => { $('#app').classList.toggle('side-open', open); $('#btn-side').setAttribute('aria-expanded', open); };
  $('#btn-side').onclick = () => setDrawer(!$('#app').classList.contains('side-open'));
  document.addEventListener('pointerdown', e => {
    if ($('#app').classList.contains('side-open') && !e.target.closest('.sidebar, #btn-side, .menu, .dlg, .pop')) setDrawer(false);
  });
  document.addEventListener('keydown', e => {
    if (e.key !== 'Escape' || e.defaultPrevented || e.target.closest?.('.palette')) return;
    if ($('#app').classList.contains('side-open')) setDrawer(false);
    else $('#app').classList.remove('view-open');
  });
  // 切换模块：对话 / 翻译 / 识字 占左边一栏，右边的查看器共用
  const switchMod = m => {
    if (!$('#mod-' + m)) m = 'chat';
    undockTool(m);       // 这个页面正在对话右侧开着：收回来，作为单独页面显示
    document.querySelectorAll('.work > .mod').forEach(x => { x.hidden = x.id !== 'mod-' + m; });
    $('#work').classList.toggle('wide', m === 'set');   // 设置页用整个宽度，不要右边的查看器
    $('#work').classList.toggle('solo', m === 'img' || m === 'kb');   // 生图、知识库：右边没打开文件时收起，内容用整个宽度
    // 侧栏：对话、翻译、识字、生图要文件管理器；对话列表只有对话页要；知识库、设置整栏隐藏
    $('#app').classList.toggle('side-off', !['chat', 'tr', 'ocr', 'img'].includes(m));
    $('#app').classList.toggle('no-convs', m !== 'chat');
    document.querySelectorAll('.activity a[data-mod]').forEach(a => a.classList.toggle('on', a.dataset.mod === m));
    try { localStorage.setItem('mod', m); } catch (e) {}
    setDrawer(false); $('#app').classList.remove('view-open');
    emit('mod', m);
    const f = { chat: '#input', tr: '#tr-input', img: '#img-composer textarea:not([hidden])', kb: '#kb-q' }[m]; if (f) $(f)?.focus();
  };
  document.querySelectorAll('.activity a[data-mod]').forEach(a => a.onclick = () => switchMod(a.dataset.mod));
  on('switch-mod', switchMod);
  // 在对话右侧打开识字、翻译、设置：先回到对话页，再在右侧加一个标签
  on('dock-tool', id => { switchMod('chat'); dockTool(id); });
  document.addEventListener('click', e => { const b = e.target.closest('[data-dock-tool]'); if (b) emit('dock-tool', b.dataset.dockTool); });
  // 文件管理器跟着页面走：对话页看这个对话的项目文件夹，其他页看各自的输出文件夹
  let mod = 'chat', project = currentProject();
  // 图像页分图片、视频两部分，文件管理器分别看 沙箱\图片、沙箱\视频（初值从图像页存的标签读）
  let imgMode = 'image';
  try { imgMode = JSON.parse(localStorage.getItem('img') || '{}').tab === 'video' ? 'video' : 'image'; } catch (e) {}
  const sb = settings.config.sandbox;
  const focusFor = m => ({
    chat: project ? { name: project.show.replace(/^沙箱\\项目\\/, '项目：'), path: project.path }
                  : { hint: '这个对话还没有项目文件夹，发第一条消息后自动建在 沙箱\\项目 里。点右上角“全部”看整个沙箱。' },
    tr: { name: '翻译输出', path: sb + '\\翻译' }, ocr: { name: '识字的截图', path: sb + '\\识字' },
    img: imgMode === 'video' ? { name: '视频', path: sb + '\\视频' } : { name: '图片', path: sb + '\\图片' }, kb: { name: '知识库', path: sb.replace(/\\sandbox$/i, '\\knowledge') },
  }[m] || null);
  on('mod', m => { mod = m; setFocus(focusFor(m)).catch(() => {}); });
  on('img-mode', v => { if (v === imgMode) return; imgMode = v; if (mod === 'img') setFocus(focusFor('img')).catch(() => {}); });
  on('project', p => { project = p; if (mod === 'chat') setFocus(focusFor('chat')).catch(() => {}); });
  on('open-conv', () => { switchMod('chat'); setDrawer(false); });
  for (const id of ['#convs', '#archived']) $(id).addEventListener('click', e => { if (e.target.closest('.conv') && !e.target.closest('.ops') && !e.target.closest('input')) switchMod('chat'); });
  // 从入口页跳过来：?mod=tr 打开某个页面，?conv=编号 打开某个对话；用完把网址收回成 /app
  const qp = new URLSearchParams(location.search);
  if (qp.get('conv')) await openConv(qp.get('conv')).catch(() => toast('这个对话不存在了'));
  let m0 = qp.get('mod') || (qp.get('conv') ? 'chat' : '');
  if (!m0) { try { m0 = localStorage.getItem('mod') || 'chat'; } catch (e) { m0 = 'chat'; } }
  if (location.search) history.replaceState(null, '', '/app');
  switchMod(m0);
  // 状态栏弹出层
  document.addEventListener('click', e => {
    const t = e.target.closest('[data-toggle]');
    if (t) {
      const p = document.querySelector(t.dataset.toggle);
      document.querySelectorAll('.pop.open').forEach(x => { if (x !== p) x.classList.remove('open'); });
      p.classList.toggle('open'); p.style.left = Math.max(4, Math.min(t.offsetLeft, innerWidth - p.offsetWidth - 8)) + 'px'; return;
    }
    document.querySelectorAll('.pop.open').forEach(p => { if (!p.contains(e.target)) p.classList.remove('open'); });
  });
  // 拖动分隔条，宽度记在本机
  const work = $('#work'), sp = work.querySelector('.splitter');
  const w = localStorage.getItem('chatw'); if (w) work.style.setProperty('--chatw', w);
  sp.addEventListener('pointerdown', e => {
    sp.setPointerCapture(e.pointerId); sp.classList.add('drag');
    const r = work.getBoundingClientRect();
    const move = ev => work.style.setProperty('--chatw', Math.min(Math.max(ev.clientX - r.left, 320), r.width - 280) + 'px');
    const up = () => { sp.classList.remove('drag'); sp.removeEventListener('pointermove', move); sp.removeEventListener('pointerup', up);
                       try { localStorage.setItem('chatw', work.style.getPropertyValue('--chatw')); } catch (e) {} };
    sp.addEventListener('pointermove', move); sp.addEventListener('pointerup', up);
  });
}

main().catch(e => { document.body.insertAdjacentHTML('beforeend', `<div class="toast">启动失败：${e.message}</div>`); });

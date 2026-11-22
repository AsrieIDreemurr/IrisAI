// 入口页：随手问（9B 轻量 Agent，CPU 上运行，不占显卡）、功能入口、最近的对话、状态。
// 主题素材里有“看板”时是桌宠布局（样式见 portal.css）：随手问变成看板娘头上的对话气泡，她的表情跟着随手问换。
import { $, api, esc, events, md, reconnectNote, toast } from './api.js';
import { initWinFrame } from './winframe.js';
import { initInkFrame } from './inkframe.js';
import { applySkin, setMood } from './skin.js';
import { initPet, loadFaces, loadLines, tellDesktop, setPetFaces, setPetFrames, petEmote, petTalk, petStep, petWait, petRound, petFinal, place, pet, petBox, petOn, calm, walkTo, show, say } from './pet.js';
import { initArtFace, initFacePanel, hasFaces, hasPetFrames } from './artface.js';

const LS = 'portal';
// 桌面程序的聊天小窗（点桌面桌宠弹出来，desktop_app.py 打开 /?petchat=1）：只显示随手问对话框，样式和入口页一样
const SOLO = new URLSearchParams(location.search).has('petchat');
const deskApi = () => window.pywebview?.api;
let cid = null, rid = null, stop = null, cfg = null;
let cur = null;              // 正在写的那一段回答 {el, text}
const steps = new Map();

function load() { try { return JSON.parse(localStorage.getItem(LS) || '{}'); } catch (e) { return {}; } }
function save(p) { try { localStorage.setItem(LS, JSON.stringify({ ...load(), ...p })); } catch (e) {} }

// 外观和主题跟工作台一致（设置在工作台的“设置”页里改）
// 桌面程序托盘菜单切换“桌宠在桌面上”时调（desktop_app.py 用 evaluate_js）：存进设置，再按新设置排页面
window.irisPetMode = async mode => {
  const look = { ...(cfg?.settings?.look || {}), petMode: mode === 'desktop' ? 'desktop' : 'web' };
  try { await api('/api/settings', { method: 'PUT', body: { look } }); if (cfg?.settings) cfg.settings.look = look; } catch (e) { toast(e.message); return; }
  location.reload();          // 入口页的桌宠布局在加载时排好，换模式直接重新加载最稳
};

async function applyLook(l = {}) {
  const r = document.documentElement;
  if (l.accent) r.style.setProperty('--accent', l.accent);
  if (l.radius != null) r.style.setProperty('--radius', l.radius + 'px');
  if (l.fs) r.style.setProperty('--fs', l.fs + 'px');
  r.style.setProperty('--gap', { compact: .75, normal: 1, comfy: 1.25 }[l.density] || 1);
  r.dataset.theme = l.mode || 'system';
  r.dataset.deco = l.deco || 'on';
  const s = await applySkin(l.skin);
  if (load().art) save({ art: null });     // 清掉三版候选预览时记下的选择（已定为半身特写）
  // 看板娘：和工作台同一套规则（自己上传的图优先，其次主题的看板；设置里能隐藏）。默认主题不显示桌宠
  const src = l.mascotSrc || s?.files?.mascot || '';
  // 桌宠放到桌面上（设置里选的）：入口页按看板图关闭处理，随手问是普通布局；告诉桌面程序现在的模式和主题
  r.dataset.mascot = (l.mascot || 'on') === 'on' && src && l.petMode !== 'desktop' ? 'on' : 'off';
  tellDesktop((l.mascot || 'on') === 'on' && !l.mascotSrc ? l.petMode : 'web', l.skin);
  r.toggleAttribute('data-mascot-own', !!l.mascotSrc);
  if (src) document.querySelectorAll('img[data-mascot-img], img[data-pet-img]').forEach(img => { img.src = src; });
  // 桌宠的表情帧：主题文件夹里有 素材/桌宠.json 才有（自己上传了看板图就不用）
  if (s && !l.mascotSrc && r.dataset.mascot === 'on') { loadFaces(s.css.replace(/skin\.css.*$/, '')); loadLines(s.persona); }
}

// ---------------------------------------------------------------- 桌宠和随手问气泡
// 桌宠本身（表情、拖动、重力、戳的反应）在 js/pet.js，入口页和工作台共用；这里只管随手问的气泡：
// 她和左边的对话气泡重叠时，气泡从下往上缩、让出位置，空间太小就自动收成她头顶的一行（她走开后恢复）。气泡的尖角始终指着她。
const wide = () => matchMedia('(min-width: 901px)').matches;
const bub = { userClosed: false, autoClosed: false, busy: null, target: undefined };
const bubble = () => $('#p-pet-col .p-bubble');
const homeX = () => $('#p-pet-col').getBoundingClientRect().left + 18;

// 她每次挪动后（pet.js 调）：按她的位置排气泡——让位、自动收起、小标签、尖角
function arrange() {
  if (!petOn() || !wide()) return;
  const r = document.documentElement, box = petBox(), w = box.offsetWidth, h = box.offsetHeight;
  // 让位：她和气泡那一栏左右有重叠，气泡的底边就停在她头顶上方
  const col = $('#p-pet-col').getBoundingClientRect();
  const over = pet.x < col.right && pet.x + w > col.left + 16;
  const gap = over ? Math.max(innerHeight - pet.y - h * .1 + 22 - (innerHeight - col.bottom), 16) : 16;
  r.style.setProperty('--bub-gap', gap + 'px');
  const room = col.bottom - gap - (col.top + 16);
  // 留一点回差，拖着经过边界时不会来回开合
  if (bub.autoClosed ? room > 260 : room < 200) bub.autoClosed = !bub.autoClosed;
  setClosed(bub.userClosed || bub.autoClosed);
  placePill(); aim();
}

// 收起后的那一行：放在她头顶正上方，贴着窗口边时往里挪
function placePill() {
  const col = $('#p-pet-col'), r = document.documentElement;
  if (!col.classList.contains('closed') || !petOn() || !wide()) { r.style.removeProperty('--pill-x'); r.style.removeProperty('--pill-y'); return; }
  const b = bubble(), w = b.offsetWidth, h = b.offsetHeight, bw = petBox().offsetWidth, bh = petBox().offsetHeight;
  r.style.setProperty('--pill-x', Math.min(Math.max(pet.x + bw / 2 - w / 2, 8), innerWidth - w - 8) + 'px');
  r.style.setProperty('--pill-y', Math.max(pet.y + bh * .1 - h - 18, 46) + 'px');
}

// 气泡尖角：放到离她最近的那条边上（避开圆角），朝她的头转；她压在气泡上时不显示
function aim() {
  const tail = $('#p-tail'); if (!tail || !petOn()) return;
  const b = bubble().getBoundingClientRect(), p = $('#p-pet').getBoundingClientRect();
  if (!b.width || !p.width) return;
  const px = p.left + p.width / 2, py = p.top + p.height * .3;
  if (px > b.left && px < b.right && py > b.top && py < b.bottom) { tail.setAttribute('hidden', ''); return; }   // SVG 没有 .hidden 属性
  // 离圆角至少 m。她在气泡下方（或上方）时一律用底边（顶边），斜着指过去；
  // 只有她和气泡上下平齐时才用左右两边。气泡太矮（收起成一行）时只用上下两条边
  const m = Math.min(36, b.width / 2), short = b.height < 2 * 30 + 26;
  const cx = Math.min(Math.max(px, b.left + m), b.right - m), cy = Math.min(Math.max(py, b.top + 30), b.bottom - 30);
  const dx = px - cx, dy = py - (short ? b.top + b.height / 2 : cy);
  const below = py > b.bottom - 30, above = py < b.top + 30;
  let x, y, normal;
  if (short || below || above) { x = cx; y = dy > 0 ? b.bottom : b.top; normal = dy > 0 ? 90 : -90; }
  else { x = dx > 0 ? b.right : b.left; y = cy; normal = dx > 0 ? 0 : 180; }
  // 底边平贴在这条边上（不跟着转，转了底边会一头陷进气泡、一头翘出来），只把尖头往她的方向偏，最多偏 50°
  let d = ((Math.atan2(py - y, px - x) * 180 / Math.PI - normal + 540) % 360) - 180;
  d = Math.max(-50, Math.min(50, d)) * Math.PI / 180;
  const n = [Math.cos(normal * Math.PI / 180), Math.sin(normal * Math.PI / 180)], t = [-n[1], n[0]];
  const bx = x - b.left, by = y - b.top, len = 15, hw = 10, off = Math.tan(d) * len;
  const pt = (along, out) => `${(bx + t[0] * along + n[0] * out).toFixed(1)} ${(by + t[1] * along + n[1] * out).toFixed(1)}`;
  const tip = pt(off, len);
  tail.querySelector('.f').setAttribute('d', `M${pt(-hw, -2)}L${tip}L${pt(hw, -2)}Z`);    // 底边压进气泡 2px 盖住边框线
  tail.querySelector('.s').setAttribute('d', `M${pt(-hw, 0)}L${tip}L${pt(hw, 0)}`);       // 两条斜边从气泡边框线上长出来
  tail.removeAttribute('hidden');
}

// 收起 / 展开的动画：收起时气泡淡出、往她那边缩，小标签从她头顶弹出来；展开反过来
function setClosed(closed) {
  const col = $('#p-pet-col');
  if ((bub.target ?? col.classList.contains('closed')) === closed) return;
  bub.target = closed;
  const swap = () => {
    col.classList.toggle('closed', closed);
    $('#p-fold').textContent = closed ? '展开' : '收起';
    $('#p-fold').title = closed ? '展开对话' : '收起对话';
    placePill(); aim();
  };
  // 上一次开合还没播完就又要变：旧动画作废（它不再切换状态），直接从这次开始
  if (bub.busy) { const old = bub.busy; bub.busy = null; old.onfinish = old.oncancel = null; old.cancel(); }
  if (calm() || !petOn() || !wide() || !bubble().animate) { swap(); return; }
  const b = bubble(), out = closed
    ? [{ opacity: 1, transform: 'none' }, { opacity: 0, transform: 'translateY(14px) scale(.94)' }]
    : [{ opacity: 1, transform: 'none' }, { opacity: 0, transform: 'translateY(8px) scale(.7)' }];
  const inn = closed
    ? [{ opacity: 0, transform: 'translateY(10px) scale(.6)' }, { opacity: 1, transform: 'none' }]
    : [{ opacity: 0, transform: 'translateY(16px) scale(.96)' }, { opacity: 1, transform: 'none' }];
  b.style.transformOrigin = '50% 100%';
  const a1 = b.animate(out, { duration: closed ? 150 : 110, easing: 'ease-in' });
  bub.busy = a1;
  a1.onfinish = () => {
    if (bub.busy !== a1) return;
    swap();
    const a2 = b.animate(inn, { duration: closed ? 200 : 230, easing: 'cubic-bezier(.2,.9,.3,1.2)' });
    bub.busy = a2;
    a2.onfinish = () => { if (bub.busy === a2) bub.busy = null; aim(); };
  };
  // 页面在后台时动画可能不走：0.5 秒还没切换就直接切到最终状态
  setTimeout(() => { if (bub.busy === a1) { bub.busy = null; a1.onfinish = null; a1.cancel(); swap(); } }, 500);
}

// 用户按“收起 / 展开”。她挡着气泡时要展开，她就往右边跳开让位
function fold(open) {
  bub.userClosed = !open;
  save({ closed: !open });
  if (!petOn() || !wide()) { setClosed(!open); return; }
  if (open && bub.autoClosed) {
    const col = $('#p-pet-col').getBoundingClientRect();
    walkTo(col.right + 12, () => { show('shy', 1600); say('让一让～'); });
    return;
  }
  place();
  if (open && wide()) $('#p-input').focus();
}


// 桌宠在桌面上时：桌面程序把她站的位置（换成这个页面的坐标，不算上下浮动）告诉这里，
// 她站到左边对话框前面时，对话框底边退到她头顶上方，和网页桌宠的让位一样（至少留 220px 高）
let deskPet = null;
window.irisDeskPet = r => { deskPet = r; deskArrange(); };
function deskArrange() {
  const r = document.documentElement, col = $('#p-pet-col');
  if (SOLO || r.dataset.mascot === 'on' || !col) return;
  const c = col.getBoundingClientRect();
  let gap = 16;
  if (deskPet && wide() && deskPet.x < c.right - 8 && deskPet.x + deskPet.w > c.left + 16 && deskPet.y < c.bottom && deskPet.y + deskPet.h > c.top) {
    gap = Math.max(c.bottom - (deskPet.y + deskPet.h * .1) + 22, 16);
    gap = Math.min(gap, Math.max(c.height - 220, 16));
  }
  r.style.setProperty('--desk-gap', gap + 'px');
}
addEventListener('resize', deskArrange);

// 聊天小窗：收起、在工作台打开交给桌面程序；每次被叫出来（desktop_app.py 调 petChatOpened）
// 入口页那边换了话题就重新加载接上，然后把光标放进输入框；点到别处（小窗失去焦点）就收起
function initSolo() {
  document.body.classList.add('p-solo');
  $('#p-fold').textContent = '收起'; $('#p-fold').title = '收起小窗';
  $('#p-fold').onclick = e => { e.stopPropagation(); deskApi()?.close_chat?.(); };
  $('#p-open').textContent = '在工作台打开';
  $('#p-open').addEventListener('click', e => { e.preventDefault(); if (cid) deskApi()?.open_main?.('/app?conv=' + cid); });
  window.petChatOpened = () => {
    if ((load().cid || null) !== cid && !rid) { location.reload(); return; }
    $('#p-input').focus(); box().scrollTop = box().scrollHeight;
  };
  addEventListener('blur', () => setTimeout(() => { if (!document.hasFocus()) deskApi()?.close_chat?.(); }, 150));
  document.addEventListener('keydown', e => { if (e.key === 'Escape') deskApi()?.close_chat?.(); });
}

function initBubble() {
  initPet({ key: LS, homeX, floorY: h => innerHeight - h - 4, top: 40, onMove: arrange, canDrag: wide, typeInput: '#p-input' });
  setTimeout(() => $('#p-pet-col').classList.add('pet-ready'), 60);      // 排好第一次之后气泡的边距才有动画
  new ResizeObserver(() => { placePill(); aim(); }).observe(bubble());
}

// ---------------------------------------------------------------- 调整布局（只在桌宠布局、右栏显示立绘时可用）
// 拖立绘改位置，滚轮或滑块改大小，滑块改中间卡片的宽度。记在本机（各台电脑屏幕不一样），“恢复默认”清掉
const LAY = { dx: 0, dy: 0, s: 100, w: 1080, fa: 62, fl: 14 };   // fa/fl：脚部渐隐的起点和长度（占立绘高度的 %）
// 按主题分开记（2026-11-08）：各主题的立绘不一样（恋恋是半身、觉是全身），一个主题调的渐隐、位置不该套到另一个上。
// 主题可以在 skin.css 的 :root 上写自己的默认值：--lay-dx / --lay-dy / --lay-s / --lay-w / --lay-fa / --lay-fl（数字，不带单位）
const layKey = () => 'layout_' + (document.documentElement.dataset.skin || 'default');
function layDefaults() {
  const cs = getComputedStyle(document.documentElement), d = { ...LAY };
  for (const k of ['dx', 'dy', 's', 'w', 'fa', 'fl']) { const v = parseFloat(cs.getPropertyValue('--lay-' + k)); if (!isNaN(v)) d[k] = v; }
  return d;
}
const lay = () => ({ ...layDefaults(), ...(load()[layKey()] || (layKey() === 'layout_koishi' ? load().layout : null) || {}) });   // 恋恋沿用以前没分主题时记下的
const artShown = () => getComputedStyle($('.p-art-col')).display !== 'none';

function applyLay(L = lay()) {
  const r = document.documentElement, on = r.hasAttribute('data-skin-mascot');   // 默认主题保持原样
  const set = (k, v) => on ? r.style.setProperty(k, v) : r.style.removeProperty(k);
  set('--art-dx', L.dx + 'px'); set('--art-dy', L.dy + 'px'); set('--art-s', L.s / 100); set('--stage-w', L.w + 'px');
  // 起点 100% 时不淡出：终点放到 101%，整张都在不透明的那一段里
  set('--fade-a', L.fa + '%'); set('--fade-b', (L.fa >= 100 ? 101 : Math.min(L.fa + L.fl, 100)) + '%');
  $('#p-tune-s').value = L.s; $('#p-tune-s-v').textContent = L.s + '%';
  $('#p-tune-w').value = L.w; $('#p-tune-w-v').textContent = L.w + 'px';
  $('#p-tune-fa').value = L.fa; $('#p-tune-fa-v').textContent = L.fa >= 100 ? '不淡出' : L.fa + '%';
  $('#p-tune-fl').value = L.fl; $('#p-tune-fl-v').textContent = L.fl ? L.fl + '%' : '一刀切';
}
function setLay(p) { const L = { ...lay(), ...p }; save({ [layKey()]: L }); applyLay(L); }

let closeFaces = () => {};
function tune(on) {
  const r = document.documentElement;
  r.toggleAttribute('data-tuning', on);
  $('#p-tunebar').hidden = !on;
  $('#p-tune-face').hidden = !hasFaces() && !hasPetFrames();
  if (!on) closeFaces();
  $('#p-tune').textContent = on ? '完成调整' : '调整布局';
}

function initTune() {
  const art = $('.p-side-art');
  const sync = () => { const ok = artShown(); $('#p-tune').hidden = !ok; if (!ok) tune(false); };
  applyLay(); sync();
  addEventListener('resize', sync);
  closeFaces = initFacePanel($('#p-tune-face'), $('#p-facepanel'), () => cfg.settings);
  initArtFace({ load, save });
  $('#p-tune').onclick = () => tune(!document.documentElement.hasAttribute('data-tuning'));
  $('#p-tune-done').onclick = () => tune(false);
  $('#p-tune-reset').onclick = () => { save({ [layKey()]: null, ...(layKey() === 'layout_koishi' ? { layout: null } : {}) }); applyLay(); };
  $('#p-tune-s').oninput = e => setLay({ s: +e.target.value });
  $('#p-tune-w').oninput = e => setLay({ w: +e.target.value });
  $('#p-tune-fa').oninput = e => setLay({ fa: +e.target.value });
  $('#p-tune-fl').oninput = e => setLay({ fl: +e.target.value });
  addEventListener('keydown', e => { if (e.key === 'Escape' && document.documentElement.hasAttribute('data-tuning')) tune(false); });
  // 拖立绘：只在调整模式下能拖
  let d = null;
  art.addEventListener('pointerdown', e => {
    if (!document.documentElement.hasAttribute('data-tuning') || e.button !== 0) return;
    e.preventDefault();
    const L = lay(); d = { id: e.pointerId, x: e.clientX, y: e.clientY, dx: L.dx, dy: L.dy };
    art.setPointerCapture(e.pointerId); art.classList.add('dragging');
  });
  art.addEventListener('pointermove', e => {
    if (!d || e.pointerId !== d.id) return;
    const L = { ...lay(), dx: Math.round(d.dx + e.clientX - d.x), dy: Math.round(d.dy + e.clientY - d.y) };
    applyLay(L); d.last = L;
  });
  const end = e => { if (!d || e.pointerId !== d.id) return; if (d.last) setLay(d.last); d = null; art.classList.remove('dragging'); };
  art.addEventListener('pointerup', end); art.addEventListener('pointercancel', end);
  art.addEventListener('wheel', e => {
    if (!document.documentElement.hasAttribute('data-tuning')) return;
    e.preventDefault();
    setLay({ s: Math.min(180, Math.max(60, lay().s + (e.deltaY < 0 ? 4 : -4))) });
  }, { passive: false });
}

const when = t => {
  const d = new Date(t * 1000), now = new Date();
  return d.toDateString() === now.toDateString() ? d.toTimeString().slice(0, 5) : `${d.getMonth() + 1} 月 ${d.getDate()} 日`;
};

// ---------------------------------------------------------------- 对话显示
const box = () => $('#p-msgs');
function scroll() { const b = box(); if (b.scrollHeight - b.scrollTop - b.clientHeight < 160) b.scrollTop = b.scrollHeight; }
function hideEmpty() { $('#p-empty')?.remove(); }

function userMsg(text) {
  hideEmpty();
  box().insertAdjacentHTML('beforeend', `<div class="p-u">${esc(text)}</div>`);
}
function newTurn() {
  hideEmpty();
  const a = document.createElement('div');
  a.className = 'p-a';
  a.innerHTML = '<div class="who">随手问 · Qwen3.5 9B</div>';
  box().appendChild(a);
  return a;
}
function turnEl() { return box().lastElementChild?.classList.contains('p-a') ? box().lastElementChild : newTurn(); }
function textBlock(text) {
  const d = document.createElement('div'); d.className = 'md-body'; d.innerHTML = md(text); turnEl().appendChild(d); return d;
}
function status(t) { $('#p-status').innerHTML = t; }
function busy(b) { $('#p-send').hidden = b; $('#p-stop').hidden = !b; }

function card(ev) {
  const c = document.createElement('div');
  c.className = 'confirm'; c.dataset.cid = ev.id || '';
  const ask = ev.kind === 'ask';
  c.innerHTML = `<div class="head"><span class="dot wait"></span>${esc(ev.title)}</div>
    ${(ev.steps || []).length ? `<ol>${ev.steps.map(s => `<li>${esc(s)}</li>`).join('')}</ol>` : ''}
    ${ask ? `<div class="row">${(ev.options || []).map(o => `<button class="btn" data-act="answer" data-val="${esc(o)}">${esc(o)}</button>`).join('')}</div>
             <textarea placeholder="或者自己写，按回车发送" data-free="1"></textarea>`
          : '<div class="row"><button class="btn primary" data-act="ok">允许</button><button class="btn" data-act="no">取消</button></div>'}
    <div class="done"></div>`;
  turnEl().appendChild(c);
}
function cardDone(id, decision, note) {
  const c = box().querySelector(`.confirm[data-cid="${CSS.escape(id || '')}"]`); if (!c) return;
  c.querySelector('.row')?.remove(); c.querySelector('textarea')?.remove();
  c.querySelector('.dot').className = 'dot' + (decision === 'ok' || decision === 'answer' ? '' : ' wait');
  c.querySelector('.done').textContent = { ok: '已允许。', no: '已取消，没有执行。', answer: '你的回答：' + (note || '') }[decision] || '';
}
async function answer(c, decision, note) {
  try { await api(`/api/runs/${rid}/confirm`, { method: 'POST', body: { id: c.dataset.cid, decision, note } }); }
  catch (e) { toast(e.message); }
}

function attach(id) {
  rid = id; busy(true); cur = null; steps.clear();
  stop?.();
  let failed = false, stopNote = null, netSaved = '';
  setMood('think');
  stop = events(id, ev => {
    switch (ev.type) {
      case 'net':                                  // 断了、浏览器在重连：显示重连提示，连上后换回原来的状态
        if (ev.state === 'reconnecting') { netSaved = $('#p-status').innerHTML; stopNote?.(); stopNote = reconnectNote(ev.last, status); }
        else { stopNote?.(); stopNote = null; status(netSaved); }
        break;
      case 'llm': status('<span class="spin"></span> 想一想'); break;
      case 'emote': petEmote(ev.face); break;
      case 'delta':
        status(''); petTalk(ev.text);
        if (!cur) cur = { text: '', el: textBlock('') };
        cur.text += ev.text; cur.el.innerHTML = md(cur.text); cur.el.classList.add('cursor'); break;
      case 'llm_done': petRound(); if (cur) cur.el.classList.remove('cursor'); cur = null; break;
      case 'step': {
        let el = steps.get(ev.id);
        if (!el) { el = document.createElement('div'); el.className = 'p-step'; turnEl().appendChild(el); steps.set(ev.id, el); }
        const icon = { running: '<span class="spin"></span>', ok: '<span class="st ok">✓</span>', error: '<span class="st err">✕</span>',
                       denied: '<span class="st err">－</span>', waiting: '<span class="st">…</span>' }[ev.status] || '';
        el.innerHTML = `${icon}<span>${esc(ev.title)}</span>`;
        if (ev.status === 'running') status(`<span class="spin"></span> ${esc(ev.title)}`);
        petStep(ev);
        break;
      }
      case 'confirm': petWait(ev.kind); card(ev); status(ev.kind === 'ask' ? '等你回答' : '等你确认'); break;
      case 'confirm_done': cardDone(ev.id, ev.decision, ev.note); status(''); break;
      case 'final': petFinal(ev.text); if (cur) { cur.text = ev.text; cur.el.innerHTML = md(ev.text); } else textBlock(ev.text); break;
      case 'notice': case 'status': status(esc(ev.text)); break;
      case 'recall': case 'memory': {      // 开了人格时：这轮想起了什么、记下了什么（细节在工作台的记忆页）
        const items = ev.type === 'memory' ? ev.items.filter(x => x.new !== false) : ev.items;
        if (items.length) turnEl().insertAdjacentHTML(ev.type === 'recall' ? 'afterbegin' : 'beforeend',
          `<div class="p-mem" title="${esc(items.map(x => x.text).join('\n'))}">${ev.type === 'recall' ? '想起了' : '记下了'} ${items.length} 件事</div>`);
        break;
      }
      case 'error': failed = true; turnEl().insertAdjacentHTML('beforeend', `<div class="err-line">${esc(ev.text)}</div>`); break;
      case 'lost': stopNote?.(); stopNote = null; recover(); break;
      case 'done': {
        stopNote?.(); stopNote = null;
        box().querySelectorAll('.cursor').forEach(e => e.classList.remove('cursor'));
        const stopped = /停/.test($('#p-status').textContent);
        setMood(failed || stopped ? 'error' : 'done', failed || stopped ? 8000 : 6000);
        rid = null; busy(false); if (!stopped) status(''); recent(); break;
      }
    }
    scroll();
  });
}

// 人格（输入框里的“人格”按钮）：对话已建好就直接改这个对话的人格；还没建（新话题）就记在本机，发第一条时再设。
// 新话题沿用上次选的人格。和工作台的 js/personapick.js 同一套接口。
let persona = null, personas = [];

function personaLabel() {
  const b = $('#p-persona');
  b.innerHTML = `<span class="dd-lb">${persona ? '人格 · ' + esc(persona) : '人格'}</span><span class="dd-cv">▾</span>`;
  b.classList.toggle('on', !!persona);
}

function personaRender() {
  $('#p-persona-pop').innerHTML = `<section><div class="label"><span class="spark"></span>人格：选了以后，回答用这个人格的身份和语气，并带上它的记忆</div>
    <label class="kb-opt"><input type="radio" name="p-persona" value=""${persona ? '' : ' checked'}><span><b>不用人格</b><br><small>普通助手</small></span></label>
    ${personas.map(p => `<label class="kb-opt"><input type="radio" name="p-persona" value="${esc(p.name)}"${p.name === persona ? ' checked' : ''}>
      <span><b>${esc(p.name)}</b><br><small>${esc(p.desc || '没有说明')}</small></span></label>`).join('')}
    ${personas.length ? '' : '<div class="hint">还没有人格。在工作台知识库的人格层新建一个。</div>'}</section>`;
}

async function initPersona() {
  const pop = $('#p-persona-pop'), b = $('#p-persona');
  const close = () => { pop.classList.remove('open'); b.classList.remove('open'); };
  const list = async () => { personas = (await api('/api/personas').catch(() => ({ personas: [] }))).personas; };
  await list();
  persona = cid ? (await api(`/api/convs/${cid}/persona`).catch(() => ({ persona: null }))).persona : (load().persona || null);
  if (persona && !personas.some(p => p.name === persona)) persona = null;      // 人格被移走了
  personaLabel();
  b.onclick = async e => {
    e.stopPropagation();
    if (pop.classList.contains('open')) return close();
    await list(); personaRender(); pop.classList.add('open'); b.classList.add('open');
  };
  pop.addEventListener('click', e => e.stopPropagation());
  pop.addEventListener('change', async () => {
    const v = pop.querySelector('input[name=p-persona]:checked')?.value || null;
    try { persona = cid ? (await api(`/api/convs/${cid}/persona`, { method: 'PUT', body: { persona: v } })).persona : v; }
    catch (err) { toast(err.message, 4000); }
    save({ persona }); personaLabel(); close();
  });
  document.addEventListener('click', close);
}

async function send(text) {
  text = (text ?? $('#p-input').value).trim();
  if (!text || rid) return;
  $('#p-input').value = '';
  try {
    if (!cid) {
      cid = (await api('/api/convs', { method: 'POST', body: { model: (cfg || await api('/api/config')).roles?.fast } })).id; save({ cid });
      if (persona) persona = (await api(`/api/convs/${cid}/persona`, { method: 'PUT', body: { persona } }).catch(() => ({ persona: null }))).persona;
      personaLabel();
    }
    userMsg(text); newTurn(); scroll();
    status('<span class="spin"></span> 发送中');
    // 随手问一直允许联网（2026-11-08 去掉了联网开关）
    const r = await api(`/api/convs/${cid}/send`, { method: 'POST', body: { text, profile: 'lite', web: true } });
    $('#p-open').hidden = false; $('#p-open').href = '/app?conv=' + cid;
    attach(r.run);
  } catch (e) { status(''); toast(e.message, 4000); }
}

// 事件流彻底连不上了（服务重启过、这次运行的事件已回收、断网太久）：等服务能回话了，重新载入这个话题。
// 还在跑就接着看，跑完了就显示存下的回答，中断了、什么都没存就说清楚。先等服务回话：restore 请求失败会把话题清掉
async function recover() {
  rid = null; busy(false); setMood();
  status('<span class="spin"></span> 连接断了，正在查这次运行的情况');
  let ok = false;
  for (let i = 0; i < 4 && !ok && cid; i++) {
    try { await api('/api/convs/' + cid); ok = true; } catch (e) { await new Promise(r => setTimeout(r, 2500)); }
  }
  if (!ok) { status('连不上服务，服务恢复后刷新页面接着看'); return; }
  box().innerHTML = ''; steps.clear(); cur = null;
  await restore();
  if (!rid) status(box().lastElementChild?.classList.contains('p-u') ? '这次运行中断了（服务可能重启过），回答没有保存，可以重新发送' : '');
  emptyHint();
}

async function restore() {
  cid = load().cid || null;
  if (!cid) return;
  let d;
  try { d = await api('/api/convs/' + cid); } catch (e) { cid = null; save({ cid: null }); return; }
  if (d.conv.archived) { cid = null; save({ cid: null }); return; }
  $('#p-open').hidden = false; $('#p-open').href = '/app?conv=' + cid;
  let msgs = d.msgs;
  if (d.run) { const last = msgs.map(m => m.role).lastIndexOf('user'); msgs = msgs.slice(0, last + 1); }
  for (const m of msgs) {
    if (m.role === 'user' && !(m.meta || {}).system) userMsg(m.content);
    else if (m.role === 'assistant' && m.content && !(m.tool_calls || []).length) { textBlock(m.content); }
  }
  if (d.run) { newTurn(); attach(d.run); }
  box().scrollTop = box().scrollHeight;
}

// 还没说过话时的一句招呼（发第一句就消失）
function emptyHint() {
  if (box().children.length) return;
  box().innerHTML = '<div class="p-empty" id="p-empty"><div class="p-empty-t">想问什么，直接打字就好。<br>查资料、算个数、看看文件都行。</div></div>';
}

// ---------------------------------------------------------------- 左边：问候、最近的对话、状态
function hello() {
  const d = new Date(), h = d.getHours();
  $('#p-hi').textContent = h < 5 ? '夜深了' : h < 11 ? '早上好' : h < 13 ? '中午好' : h < 18 ? '下午好' : '晚上好';
  $('#p-date').textContent = `${d.getMonth() + 1} 月 ${d.getDate()} 日 · 周${'日一二三四五六'[d.getDay()]}`;
}

async function recent() {
  try {
    const list = (await api('/api/convs')).slice(0, 6);
    $('#p-recent').innerHTML = list.length ? list.map(c => `<a href="/app?conv=${c.id}"><span>${esc(c.title)}</span><small>${when(c.updated)}</small></a>`).join('')
      : '<div class="hint">还没有对话</div>';
  } catch (e) {}
}

async function stat() {
  try {
    const s = await api('/api/status', { quiet: true });
    const gpu = s.models.filter(m => m.gpu).map(m => m.name).join('、') || '空闲';
    const cpu = s.models.filter(m => !m.gpu).map(m => m.name.replace(/（.*?）/, '')).join('、') || '无';
    const v = s.gpu ? `${s.gpu.used.toFixed(1)} / ${s.gpu.total.toFixed(0)} GB` : '—';
    $('#p-stat').innerHTML = `<div class="kv"><span>显卡</span><span>${esc(gpu)}</span></div>
      <div class="kv"><span>显存</span><span>${v}</span></div>${s.gpu ? `<div class="meter"><b style="width:${(s.gpu.used / s.gpu.total * 100).toFixed(0)}%"></b></div>` : ''}
      <div class="kv"><span>内存常驻</span><span>${esc(cpu)}</span></div>
      <div class="kv"><span>内存</span><span>${s.ram.used.toFixed(0)} / ${s.ram.total.toFixed(0)} GB</span></div>`;
  } catch (e) {}
}

async function main() {
  cfg = await api('/api/config');
  setPetFaces(cfg.settings.pet_faces); setPetFrames(cfg.settings.pet_frames);
  await applyLook(cfg.settings.look || {});
  if (SOLO) {
    document.documentElement.dataset.mascot = 'off';      // 小窗里不放网页桌宠（她就在桌面上）
    initSolo();
  } else {
    bub.userClosed = !!load().closed;
    if (bub.userClosed && (!petOn() || !wide())) setClosed(true);
    $('#p-fold').onclick = () => fold($('#p-pet-col').classList.contains('closed'));
    $('#p-pet-col').querySelector('.p-chat-h').addEventListener('click', e => {
      if ($('#p-pet-col').classList.contains('closed') && !e.target.closest('a, button')) fold(true);
    });
    if (petOn()) initBubble();
    initTune();
    deskArrange();
  }
  initWinFrame();
  initInkFrame();
  hello(); setInterval(hello, 60000);
  $('#p-send').onclick = () => send();
  $('#p-stop').onclick = () => rid && api(`/api/runs/${rid}/stop`, { method: 'POST' });
  $('#p-input').addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); } });
  $('#p-new').onclick = () => {
    if (rid) { toast('这个话题还在运行，先停止再开新话题'); return; }
    cid = null; save({ cid: null }); $('#p-open').hidden = true;
    box().innerHTML = ''; location.reload();
  };
  box().addEventListener('click', e => {
    const b = e.target.closest('.confirm button'); if (!b) return;
    const c = b.closest('.confirm');
    answer(c, b.dataset.act === 'answer' ? 'answer' : b.dataset.act, b.dataset.val);
  });
  box().addEventListener('keydown', e => {
    const ta = e.target.closest('textarea[data-free]');
    if (ta && e.key === 'Enter' && !e.shiftKey && ta.value.trim()) { e.preventDefault(); answer(ta.closest('.confirm'), 'answer', ta.value.trim()); }
  });
  document.addEventListener('keydown', e => { if (e.key === 'F5' || ((e.ctrlKey || e.metaKey) && (e.key === 'r' || e.key === 'R'))) { e.preventDefault(); location.reload(); } });
  await restore();
  initPersona();          // 在 restore 之后：接着上次的对话时读那个对话的人格
  emptyHint();
  if (SOLO) { $('#p-input').focus(); return; }     // 小窗不用最近的对话、状态
  recent(); stat(); setInterval(stat, 10000);
  if (matchMedia('(min-width: 901px)').matches) $('#p-input').focus();   // 窄屏时聚焦会把页面滚到底
}

main().catch(e => document.body.insertAdjacentHTML('beforeend', `<div class="toast">打开失败：${esc(e.message)}</div>`));

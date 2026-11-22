// 大立绘的表情和情绪符号（2026-11-08）。
// 跟着入口页的情况换：问候的时段（早上 / 中午 / 下午 / 晚上 / 深夜）、随手问的状态（根元素 data-mood：
// think 在回答 / done 回答完 / error 出错 / idle 很久没动静），点一下立绘有反应。
// 两种素材，主题有哪种用哪种：
//   部件（素材\立绘部件\部件.json，scripts\export_parts.py 生成）：右栏立绘按部件一层层叠出来（固定层 + 每类一个选项，顺序同 PSD），
//     表情 = 每类选哪个（预设在 部件.json 的 presets；用户改的、新建的存本机 custom）。换表情时只有变了的部件淡入淡出；原图留着但看不见（撑尺寸、给“调整布局”拖）。
//   整张差分（素材\立绘差分\<表情>.webp）：盖在原图上淡入淡出。窄屏横幅里的立绘、没有部件的主题用这种。
// 符号（心形、闪亮、汗……）：素材\立绘差分\清单.json 里的 marks，单独的小图按百分比位置放在一层和原图一样大的框里。
// “调整布局 → 表情…”面板：按情况（每种情况用哪个表情、符号）和编辑表情（每个部件选哪个，另存、删除、恢复）两页。设置按主题存本机。
import { esc, saveSettings, toast } from './api.js';
import { PET_SITS, setPetFaces, setPetFrames, previewFace } from './pet.js';

export const SITS = [
  ['早上', '早上（5–11 点）'], ['中午', '中午（11–13 点）'], ['下午', '下午（13–18 点）'], ['晚上', '晚上（18–24 点）'], ['深夜', '深夜（0–5 点）'],
  ['思考', '随手问在回答'], ['完成', '回答完（几秒）'], ['出错', '出错或停止（几秒）'], ['空闲', '10 分钟没动静'], ['点她', '点一下立绘'],
];
const DEF = { on: true, marks: true, fade: 280, poke: 3, custom: {},
  map: { 早上: '笑脸', 中午: '微笑', 下午: '微笑', 晚上: '微笑', 深夜: '闭眼', 思考: '半眼', 完成: '笑脸', 出错: '慌张', 空闲: '闭眼', 点她: '惊讶_脸红' },
  mmap: { 早上: '', 中午: '', 下午: '', 晚上: '', 深夜: '', 思考: '困惑', 完成: '闪亮', 出错: '慌张', 空闲: '', 点她: '心形' } };

let skinDef = {}, key = 'faces';    // 主题清单里的默认搭配（defaults）；设置按主题分开存（faces_<主题>）
let cfg = null, store = null, list = [], marks = [], parts = null, dir = '', pdir = '';
let layers = [], frames = [], stacks = [], cur = '', curMark = null, poked = 0, pokeTimer = 0, preview = null;
let petFrames = null, petPv = 'idle', getSettings = () => ({});     // 桌宠的帧（素材\桌宠.json，名字 -> 网址）；“桌宠”页正在看的帧

const conf = () => {
  const s = store.load()[key] || {}, d = skinDef;
  return { ...DEF, ...d, ...s, map: { ...DEF.map, ...(d.map || {}), ...(s.map || {}) }, mmap: { ...DEF.mmap, ...(d.mmap || {}), ...(s.mmap || {}) },
    custom: { ...(s.custom || {}) } };
};
const timeSit = (h = new Date().getHours()) => h < 5 ? '深夜' : h < 11 ? '早上' : h < 13 ? '中午' : h < 18 ? '下午' : '晚上';
const MOOD = { think: '思考', done: '完成', error: '出错', idle: '空闲' };
const enc = p => p.split('/').map(encodeURIComponent).join('/');
const url = (...p) => dir + p.map(encodeURIComponent).join('/') + '.webp';
const purl = f => pdir + enc(f);
const label = n => n.replace(/_/g, ' · ');

// 表情名字：部件模式 = 预设 + 自己建的；整张模式 = 清单里的
export function faceNames() {
  if (!parts) return list;
  return [...new Set([...Object.keys(parts.presets), ...Object.keys(cfg.custom)])];
}
const builtin = n => !!parts?.presets[n];
function recipe(n) { return cfg.custom[n] || parts?.presets[n] || null; }

// 现在是哪种情况：点她 > 随手问的状态 > 时段（预览另算，在 paint 里优先）
export function situation() {
  if (poked > Date.now()) return '点她';
  return MOOD[document.documentElement.dataset.mood] || timeSit();
}

// ---------------------------------------------------------------- 搭层
function sizeLike(el, base) {
  const fit = () => { el.style.width = base.offsetWidth + 'px'; el.style.height = base.offsetHeight + 'px'; };
  new ResizeObserver(fit).observe(base); base.addEventListener('load', fit); fit();
}
const place = (img, pos) => Object.assign(img.style, { left: pos[0] + '%', top: pos[1] + '%', width: pos[2] + '%' });

function build() {
  layers.forEach(L => L.imgs.forEach(i => i.remove())); frames.forEach(f => f.el.remove()); stacks.forEach(s => s.el.remove());
  layers = []; frames = []; stacks = [];
  for (const base of document.querySelectorAll('img[data-skin-img="art"]')) {
    const side = base.classList.contains('p-side-art');
    let after = base;
    if (side && parts) {
      // 部件模式：一层和原图一样大的框，按 stack 的顺序放固定层和每类两张（轮流淡入淡出）
      const el = document.createElement('div');
      el.className = 'p-side-art p-art-parts'; el.setAttribute('aria-hidden', 'true');
      const slots = {};
      for (const s of parts.stack) {
        if (s.fixed) { const i = new Image(); i.alt = ''; i.src = purl(s.fixed); place(i, s.pos); i.className = 'fx'; el.appendChild(i); }
        else { const a = new Image(), b = new Image(); a.alt = b.alt = ''; el.append(a, b); slots[s.cat] = { imgs: [a, b], act: 0, cur: '' }; }
      }
      base.after(el); after = el; sizeLike(el, base);
      stacks.push({ el, slots });
    } else if (list.length) {
      const mk = () => { const i = new Image(); i.className = base.className + ' p-art-face'; i.alt = ''; i.setAttribute('aria-hidden', 'true'); after.after(i); after = i; return i; };
      layers.push({ base, imgs: [mk(), mk()], act: 0 });
    }
    if (side && marks.length) {
      const el = document.createElement('div');
      el.className = 'p-side-art p-art-mark'; el.setAttribute('aria-hidden', 'true');
      after.after(el); sizeLike(el, base);
      frames.push({ el });
    }
  }
  document.documentElement.removeAttribute('data-art-parts');      // 部件载好以后才藏原图（revealWhenReady）
}

// 换部件：新的放到旧的上面淡入，淡完再撤掉旧的（2026-11-08：以前新旧一起淡入淡出，中间半透明的一下会露出底下的东西——
// 觉的身体层里眼睛底下画着血泪，换眼睛时会闪过血迹）。选项的 file 为空 = 这一类什么都不画（比如“血泪”= 不画眼睛）。
function paintParts(name) {
  const r = (name && recipe(name)) || (parts && recipe(parts.default || Object.keys(parts.presets)[0])) || {};
  const ms = cfg.fade + 40;
  for (const S of stacks) for (const [cat, slot] of Object.entries(S.slots)) {
    const opt = r[cat] && parts.cats[cat]?.options[r[cat]];
    const want = opt?.file || '';
    if (want === slot.cur) continue;
    slot.cur = want;
    const next = slot.imgs[slot.act ^ 1], now = slot.imgs[slot.act];
    clearTimeout(slot.tt);
    if (!want) { now.classList.remove('on'); next.classList.remove('on'); continue; }
    const swap = () => {
      if (slot.cur !== want) return;
      now.after(next);                                 // 新的在上面
      next.classList.add('on'); slot.act ^= 1;
      slot.tt = setTimeout(() => { if (slot.cur === want) now.classList.remove('on'); }, ms);
    };
    place(next, opt.pos);
    next.onload = swap;
    if (next.getAttribute('src') === purl(want) && next.complete) swap(); else next.src = purl(want);
  }
}

// 部件第一次全部载好之前，原图照常显示（不然先载好的身体层会露出眼睛底下的东西）；载好了再藏原图
async function revealWhenReady() {
  const root = document.documentElement;
  root.removeAttribute('data-art-parts');
  const imgs = stacks.flatMap(S => [...S.el.querySelectorAll('img.fx, img.on')]);
  // 等“载完”（complete），不用 decode()：页面在后台、窗口被挡住时 decode() 可能一直不返回
  await Promise.all(imgs.map(i => (i.complete && i.naturalWidth) ? null : new Promise(r => { i.addEventListener('load', r, { once: true }); i.addEventListener('error', r, { once: true }); setTimeout(r, 8000); })));
  if (stacks.length) root.setAttribute('data-art-parts', '');
}

function paintFace(face) {
  if (stacks.length) paintParts(face);
  const u = face && list.includes(face) ? url(face) : '';      // 整张差分（横幅里的立绘、没有部件的主题）
  if (u === cur) return;
  cur = u;
  for (const L of layers) {
    const next = L.imgs[L.act ^ 1], now = L.imgs[L.act];
    if (!u) { now.style.setProperty('--o', 0); next.style.setProperty('--o', 0); continue; }
    const swap = () => { if (cur !== u) return; next.style.setProperty('--o', 1); now.style.setProperty('--o', 0); L.act ^= 1; };
    next.onload = swap;
    if (next.getAttribute('src') === u && next.complete) swap(); else next.src = u;
  }
}

function paintMark(name) {
  const m = marks.find(x => x.name === name) || null;
  if ((m?.name || '') === (curMark?.name || '')) return;
  curMark = m;
  for (const f of frames) {
    f.el.querySelectorAll('img:not(.out)').forEach(i => { i.classList.add('out'); setTimeout(() => i.remove(), 600); });
    if (!m) continue;
    const i = new Image(); i.alt = '';
    Object.assign(i.style, { left: m.left + '%', top: m.top + '%', width: m.width + '%' });
    i.onload = () => requestAnimationFrame(() => i.classList.add('in'));
    i.src = url('符号', m.name);
    f.el.appendChild(i);
  }
}

function paint() {
  if (!hasFaces()) return;
  document.documentElement.style.setProperty('--face-fade', cfg.fade + 'ms');
  const sit = situation();
  // 关掉“换表情”时：部件模式叠默认表情，整张模式回到原图
  paintFace(preview ? preview.face : cfg.on ? cfg.map[sit] : (parts ? null : null));
  paintMark(preview ? preview.mark : cfg.marks ? cfg.mmap[sit] : '');
}

async function getJson(u) { try { const r = await fetch(u); return r.ok ? await r.json() : null; } catch (e) { return null; } }

async function loadList() {
  const skin = document.documentElement.dataset.skin, root = document.documentElement;
  list = []; marks = []; parts = null; cur = ''; curMark = null; petFrames = null; petParts = null;
  root.removeAttribute('data-faces');
  if (!skin || skin === 'default') { build(); return; }
  const base = `/s/skins/${encodeURIComponent(skin)}/${encodeURIComponent('素材')}/`;
  dir = base + encodeURIComponent('立绘差分') + '/'; pdir = base + encodeURIComponent('立绘部件') + '/';
  const [man, pm, pf] = await Promise.all([getJson(dir + encodeURIComponent('清单.json')), getJson(pdir + encodeURIComponent('部件.json')),
    getJson(base + encodeURIComponent('桌宠.json'))]);
  if (document.documentElement.dataset.skin !== skin) return;       // 等的时候又换了主题
  list = man?.faces || []; marks = man?.marks || []; parts = pm;
  petFrames = pf?.frames ? Object.fromEntries(Object.entries(pf.frames).map(([k, v]) => [k, base + enc(v)])) : null;
  petParts = petFrames ? await getJson(base + encodeURIComponent('桌宠部件') + '/' + encodeURIComponent('部件.json')) : null;
  skinDef = man?.defaults || {}; key = 'faces_' + skin; cfg = conf();
  build();
  if (hasFaces()) root.setAttribute('data-faces', '');
  paint();
  if (stacks.length) setTimeout(revealWhenReady, 30);
  // 空下来再把其余的图先读进缓存，换的时候不闪
  setTimeout(() => {
    const us = [...list.map(f => url(f)), ...marks.map(m => url('符号', m.name))];
    if (parts) for (const c of Object.values(parts.cats)) for (const o of Object.values(c.options)) if (o.file) us.push(purl(o.file));
    us.forEach(u => { const i = new Image(); i.src = u; });
  }, 4000);
}

export const hasFaces = () => !!(parts || list.length);
export const hasPetFrames = () => !!petFrames;

export function setPreview(p) { preview = p; paint(); }

function saveCfg(p) { cfg = { ...cfg, ...p }; store.save({ [key]: cfg }); paint(); }

// ---------------------------------------------------------------- 调整布局里的“表情”面板
let tab = 'sit', editing = '';
const opt = (v, sel, text = label(v)) => `<option value="${esc(v)}"${v === sel ? ' selected' : ''}>${esc(text)}</option>`;

function sitHtml() {
  const names = faceNames(), now = situation(), mk = marks.map(m => m.name);
  return `<div class="pf-rows${mk.length ? ' two' : ''}"><div class="pf-row pf-th"><span>情况</span><span>表情</span>${mk.length ? '<span>符号</span>' : ''}<span></span></div>
      ${SITS.map(([k, t]) => `<div class="pf-row${k === now ? ' now' : ''}"><span>${esc(t)}${k === now ? ' <small>现在</small>' : ''}</span>
      <select data-sit="${esc(k)}"${cfg.on ? '' : ' disabled'}>${names.map(n => opt(n, cfg.map[k])).join('')}</select>
      ${mk.length ? `<select data-msit="${esc(k)}"${cfg.marks ? '' : ' disabled'}>${opt('', cfg.mmap[k], '不加')}${mk.map(n => opt(n, cfg.mmap[k])).join('')}</select>` : ''}
      <button type="button" class="btn sm" data-look="${esc(k)}" title="在立绘上预览这种情况">看</button></div>`).join('')}</div>
    <div class="pf-sl"><label>淡入 <input type="range" data-pf="fade" min="0" max="1000" step="20" value="${cfg.fade}"><output>${cfg.fade} 毫秒</output></label>
      <label>点她后保持 <input type="range" data-pf="poke" min="1" max="10" step="1" value="${cfg.poke}"><output>${cfg.poke} 秒</output></label></div>
    <div class="hint">改的时候立绘会先预览；收起或完成调整后回到现在这种情况。“完成”“出错”的长短跟着看板娘，几秒后回到时段的样子。</div>`;
}

function editHtml() {
  const names = faceNames();
  if (!editing || !names.includes(editing)) editing = names[0];
  const r = recipe(editing) || {}, isB = builtin(editing), changed = isB && !!cfg.custom[editing];
  // 部件按叠放顺序列（和 PSD 一样从下到上）
  const cats = parts.stack.filter(s => s.cat).map(s => s.cat);
  return `<div class="pf-ed">
      <div class="pf-ed-h"><select data-ed="pick">${names.map(n => opt(n, editing, label(n) + (builtin(n) ? (cfg.custom[n] ? '（改过）' : '') : '（自己建的）'))).join('')}</select>
        ${changed ? '<button type="button" class="btn sm" data-ed="revert" title="改回主题自带的样子">恢复原样</button>' : ''}
        ${isB ? '' : '<button type="button" class="btn sm" data-ed="del">删除</button>'}</div>
      <div class="pf-parts">${cats.map(c => { const C = parts.cats[c]; return `<label><span>${esc(C.name)}</span><select data-cat="${esc(c)}">
        ${C.optional ? opt('', r[c] || '', '不要') : ''}${Object.keys(C.options).map(o => opt(o, r[c], o)).join('')}</select></label>`; }).join('')}</div>
      <div class="pf-ed-new"><input data-ed="name" placeholder="新表情的名字" maxlength="20"><button type="button" class="btn sm" data-ed="add" title="照着现在这个表情复制一份">另存为新表情</button></div>
      <div class="hint">选的时候立绘直接显示这个表情。改内置的表情会记成“改过”，可以随时恢复原样；新建的表情会出现在“按情况”的下拉里。</div>
    </div>`;
}

// ---------------------------------------------------------------- “桌宠”页：哪种情况用哪一帧（网页桌宠和桌面桌宠都按这个，存在设置的 pet_faces[主题]）
const FRAME_NAME = { idle: '平常', think: '思考', done: '完成', error: '沮丧', yawn: '打哈欠', wave: '挥手', surprise: '惊讶', held: '被拎着',
  dizzy: '晕', pout: '鼓腮', wink: '眨眼', idea: '灵光', hum: '哼歌', shy: '害羞', cheer: '开心', hope: '期待' };
const ANIM = new Set(['blink1', 'blink2', 'talk1', 'talk2', 'sleep1', 'sleep2']);      // 眨眼、对口型、睡觉的动画帧，不单独选
const fname = n => FRAME_NAME[n] ? `${FRAME_NAME[n]}（${n}）` : n;
const skinKey = () => document.documentElement.dataset.skin || 'default';
const petMap = () => (getSettings().pet_faces || {})[skinKey()] || {};

// 部件（素材\桌宠部件\部件.json，scripts\export_pet_parts.py 导出）：“编辑表情”里一项一项换。改过的配方存在设置的 pet_frames[主题][帧名]，
// 服务端按配方拼图（/api/pet/frame），网页桌宠和桌面桌宠都用。眨眼、说话、睡觉第二帧跟着改过的底帧走（app/petparts.py 的 DERIVE）
let petParts = null, petSub = 'sit', petEdit = 'idle', petStamp = Date.now();
const DERIVE = { blink1: 'idle', blink2: 'idle', talk1: 'idle', talk2: 'idle', sleep2: 'sleep1' };
const petCustom = () => (getSettings().pet_frames || {})[skinKey()] || {};
const petNames = () => [...new Set([...Object.keys(petFrames).filter(n => !ANIM.has(n)), ...Object.keys(petCustom())])];
const petChanged = n => !!petCustom()[n] || (DERIVE[n] && !!petCustom()[DERIVE[n]]);
const frameUrl = n => petParts && petChanged(n)
  ? `/api/pet/frame/${encodeURIComponent(skinKey())}/${encodeURIComponent(n)}.png?t=${petStamp}` : (petFrames[n] || petFrames.idle || '');
function petRecipe(n) {
  const c = petCustom()[n], p = petParts?.presets[n];
  return c ? { ...(p || {}), ...c } : p ? { ...p } : { ...(petParts?.presets.idle || {}) };
}

function petHtml() {
  const sub = petParts ? `<div class="pf-tabs pf-subtabs"><button type="button" data-psub="sit" class="${petSub === 'sit' ? 'on' : ''}">按情况</button>
    <button type="button" data-psub="edit" class="${petSub === 'edit' ? 'on' : ''}">编辑表情</button></div>` : '';
  return sub + (petParts && petSub === 'edit' ? petEditHtml() : petSitHtml());
}

function petSitHtml() {
  const m = petMap(), names = petNames();
  const pv = names.includes(petPv) ? petPv : 'idle';
  return `<div class="pf-pet"><div class="pf-pet-pv"><img src="${esc(frameUrl(pv))}" alt=""><span>${esc(fname(pv))}</span></div>
      <div class="pf-pet-rows">${PET_SITS.map(([k, t, d]) => { const v = m[k] || d; return `<div class="pf-row${m[k] ? ' changed' : ''}"><span>${esc(t)}</span>
        <select data-pet="${esc(k)}">${d ? '' : opt('', v, '随机（几种轮流）')}${names.map(n => opt(n, v, fname(n))).join('')}</select>
        <button type="button" class="btn sm" data-pet-look="${esc(k)}" title="让她换上这个表情看看">看</button></div>`; }).join('')}</div></div>
    <div class="hint">网页里和桌面上的桌宠都按这里换表情；每个主题分开记，存在设置里，各台设备一样。改过的那行左边有一道竖线。</div>`;
}

function petEditHtml() {
  const names = petNames(), custom = petCustom();
  if (!names.includes(petEdit)) petEdit = 'idle';
  const r = petRecipe(petEdit), isB = !!petParts.presets[petEdit], changed = isB && !!custom[petEdit];
  const cats = Object.entries(petParts.cats);
  const one = ([c, C]) => `<label><span>${esc(C.name)}</span><select data-pcat="${esc(c)}">${C.options.map(o => opt(o.id, r[c], o.label)).join('')}</select></label>`;
  const many = ([c, C]) => `<div class="pf-pmulti"><span>${esc(C.name)}</span><div>${C.options.map(o =>
    `<label class="pf-chk"><input type="checkbox" data-pmulti="${esc(c)}" value="${esc(o.id)}"${(r[c] || []).includes(o.id) ? ' checked' : ''}>${esc(o.label)}</label>`).join('')}</div></div>`;
  return `<div class="pf-pet"><div class="pf-pet-pv"><img src="${esc(frameUrl(petEdit))}" alt=""><span>${esc(fname(petEdit))}</span></div>
      <div class="pf-pet-ed">
        <div class="pf-ed-h"><select data-ped="pick">${names.map(n => opt(n, petEdit, fname(n) + (petParts.presets[n] ? (custom[n] ? '（改过）' : '') : '（自己建的）'))).join('')}</select>
          ${changed ? '<button type="button" class="btn sm" data-ped="revert" title="改回原来的样子">恢复原样</button>' : ''}
          ${isB ? '' : '<button type="button" class="btn sm" data-ped="del">删除</button>'}</div>
        <div class="pf-parts">${cats.filter(([, C]) => !C.multi).map(one).join('')}</div>
        ${cats.filter(([, C]) => C.multi && C.options.length).map(many).join('')}
        <div class="pf-ed-new"><input data-ped="name" placeholder="新表情的名字" maxlength="20"><button type="button" class="btn sm" data-ped="add" title="照着现在这个表情复制一份">另存为新表情</button></div>
      </div></div>
    <div class="hint">改的时候她会换上这个表情。改“平常”时，眨眼和说话的帧跟着换成新的脸；改“睡觉”同理。自己建的表情会出现在“按情况”的下拉里。</div>`;
}

async function savePetFrames(all) {
  const s = getSettings();
  s.pet_frames = all;
  try { await saveSettings({ pet_frames: all }); } catch (e) { toast(e.message, 4000); }
  petStamp = Date.now();
  setPetFrames(all);
}
async function setPetFrame(name, r) {
  const all = { ...(getSettings().pet_frames || {}) }, cur = { ...(all[skinKey()] || {}) };
  if (r) cur[name] = r; else delete cur[name];
  if (Object.keys(cur).length) all[skinKey()] = cur; else delete all[skinKey()];
  await savePetFrames(all);
}

async function savePet(k, v) {
  const s = getSettings(), all = { ...(s.pet_faces || {}) }, def = PET_SITS.find(x => x[0] === k)?.[2] ?? '';
  const cur = { ...(all[skinKey()] || {}) };
  if (!v || v === def) delete cur[k]; else cur[k] = v;
  if (Object.keys(cur).length) all[skinKey()] = cur; else delete all[skinKey()];
  s.pet_faces = all; setPetFaces(all);
  try { await saveSettings({ pet_faces: all }); } catch (e) { toast(e.message, 4000); }
}
async function resetPet() {
  const s = getSettings(), all = { ...(s.pet_faces || {}) };
  delete all[skinKey()];
  s.pet_faces = all; setPetFaces(all);
  try { await saveSettings({ pet_faces: all }); } catch (e) { toast(e.message, 4000); }
}
function petLook(f) {
  if (!f) { const names = Object.keys(petFrames).filter(n => !ANIM.has(n) && n !== 'idle'); f = names[Math.floor(Math.random() * names.length)]; }
  petPv = f; previewFace(f);
}

function tabsList() {
  const t = [];
  if (hasFaces()) { t.push(['sit', parts ? '立绘 · 按情况' : '立绘']); if (parts) t.push(['edit', '立绘 · 编辑表情']); }
  if (petFrames) t.push(['pet', '桌宠']);
  return t;
}

function panelHtml() {
  const ts = tabsList();
  if (!ts.some(x => x[0] === tab)) tab = ts[0]?.[0] || 'sit';
  const bar = ts.length > 1 ? `<div class="pf-tabs">${ts.map(([k, t]) => `<button type="button" data-tab="${k}" class="${tab === k ? 'on' : ''}">${t}</button>`).join('')}</div>` : '';
  if (tab === 'pet') return `<div class="pf-h"><b>桌宠表情</b><span class="sp"></span>
      <button type="button" class="btn sm" data-pf="petreset" title="这个主题的桌宠表情都回到默认">恢复默认</button><button type="button" class="btn sm" data-pf="close">收起</button></div>
    ${bar}${petHtml()}`;
  return panelArtHtml(bar);
}

function panelArtHtml(bar) {
  const mk = marks.length;
  return `<div class="pf-h"><b>立绘表情</b>
      <label title="关掉后一直是原来的表情"><input type="checkbox" data-pf="on"${cfg.on ? ' checked' : ''}> 换表情</label>
      ${mk ? `<label title="头上的心形、闪亮、汗这些"><input type="checkbox" data-pf="marks"${cfg.marks ? ' checked' : ''}> 显示符号</label>` : ''}
      <span class="sp"></span><button type="button" class="btn sm" data-pf="reset" title="情况搭配、淡入、保持时间回到默认（自己建的表情和改过的部件不动）">恢复默认</button><button type="button" class="btn sm" data-pf="close">收起</button></div>
    ${bar}
    ${parts && tab === 'edit' ? editHtml() : sitHtml()}`;
}

export function initFacePanel(btn, panel, settingsOf) {
  if (settingsOf) getSettings = settingsOf;
  const render = () => { panel.innerHTML = panelHtml(); };
  const close = () => { panel.hidden = true; btn.classList.remove('on'); setPreview(null); };
  const look = k => setPreview({ face: cfg.on ? cfg.map[k] : null, mark: cfg.marks ? cfg.mmap[k] : '' });
  const lookFace = n => setPreview({ face: n, mark: '' });
  btn.onclick = () => { if (!panel.hidden) return close(); render(); panel.hidden = false; btn.classList.add('on'); };
  panel.addEventListener('change', e => {
    const s = e.target.closest('[data-sit]'), m = e.target.closest('[data-msit]'), c = e.target.closest('[data-cat]');
    const pt = e.target.closest('[data-pet]'), pc = e.target.closest('[data-pcat]'), pm = e.target.closest('[data-pmulti]');
    if (pc || pm) {                                  // 桌宠换一个部件：存成这一帧的配方，她换上看看
      const r = petRecipe(petEdit);
      if (pc) r[pc.dataset.pcat] = pc.value;
      else r[pm.dataset.pmulti] = [...panel.querySelectorAll(`[data-pmulti="${CSS.escape(pm.dataset.pmulti)}"]:checked`)].map(x => x.value);
      setPetFrame(petEdit, r).then(() => { render(); petLook(petEdit); }); return;
    }
    if (e.target.dataset.ped === 'pick') { petEdit = e.target.value; render(); petLook(petEdit); return; }
    if (pt) { const k = pt.dataset.pet, v = pt.value; savePet(k, v).then(render); petLook(v); render(); return; }
    if (s) { saveCfg({ map: { ...cfg.map, [s.dataset.sit]: s.value } }); look(s.dataset.sit); return; }
    if (m) { saveCfg({ mmap: { ...cfg.mmap, [m.dataset.msit]: m.value } }); look(m.dataset.msit); return; }
    if (c) {                                         // 改一个部件：存成这个表情的自定义配方
      const r = { ...(recipe(editing) || {}) };
      if (c.value) r[c.dataset.cat] = c.value; else delete r[c.dataset.cat];
      saveCfg({ custom: { ...cfg.custom, [editing]: r } }); render(); lookFace(editing); return;
    }
    if (e.target.dataset.ed === 'pick') { editing = e.target.value; render(); lookFace(editing); return; }
    const k = e.target.dataset.pf;
    if (k === 'on' || k === 'marks') { saveCfg({ [k]: e.target.checked }); render(); setPreview(null); }
  });
  panel.addEventListener('input', e => {
    const k = e.target.dataset.pf;
    if (k === 'fade' || k === 'poke') { saveCfg({ [k]: +e.target.value }); e.target.nextElementSibling.textContent = k === 'fade' ? cfg.fade + ' 毫秒' : cfg.poke + ' 秒'; }
  });
  panel.addEventListener('click', e => {
    const tb = e.target.closest('[data-tab]');
    if (tb) { tab = tb.dataset.tab; render(); if (tab === 'edit') lookFace(editing || faceNames()[0]); else setPreview(null); return; }
    const ps = e.target.closest('[data-psub]');
    if (ps) { petSub = ps.dataset.psub; render(); if (petSub === 'edit') petLook(petEdit); return; }
    const pe = e.target.closest('[data-ped]')?.dataset.ped;
    if (pe === 'revert') { setPetFrame(petEdit, null).then(() => { render(); petLook(petEdit); }); return; }
    if (pe === 'del') {                              // 删自己建的：用到它的情况回到默认
      const name = petEdit, m = { ...petMap() };
      for (const [k, v] of Object.entries(m)) if (v === name) delete m[k];
      const all = { ...(getSettings().pet_faces || {}) };
      if (Object.keys(m).length) all[skinKey()] = m; else delete all[skinKey()];
      getSettings().pet_faces = all; setPetFaces(all); saveSettings({ pet_faces: all }).catch(e2 => toast(e2.message, 4000));
      petEdit = 'idle'; setPetFrame(name, null).then(render); return;
    }
    if (pe === 'add') {
      const inp = panel.querySelector('[data-ped=name]'), name = inp.value.trim().replace(/[\\/?#%]/g, '');
      if (!name || petNames().includes(name) || ANIM.has(name)) { inp.focus(); return; }
      setPetFrame(name, petRecipe(petEdit)).then(() => { petEdit = name; render(); petLook(name); }); return;
    }
    const pl = e.target.closest('[data-pet-look]');
    if (pl) { const k = pl.dataset.petLook, d = PET_SITS.find(x => x[0] === k)?.[2] || ''; petLook(petMap()[k] || d); render(); return; }
    const lk = e.target.closest('[data-look]');
    if (lk) { look(lk.dataset.look); return; }
    const ed = e.target.closest('[data-ed]')?.dataset.ed;
    if (ed === 'revert') { const c = { ...cfg.custom }; delete c[editing]; saveCfg({ custom: c }); render(); lookFace(editing); return; }
    if (ed === 'del') {
      const c = { ...cfg.custom }; delete c[editing];
      const fallback = parts.default || Object.keys(parts.presets)[0];
      const map = Object.fromEntries(Object.entries(cfg.map).map(([k, v]) => [k, v === editing ? fallback : v]));   // 用到它的情况换回默认表情
      saveCfg({ custom: c, map }); editing = ''; render(); lookFace(faceNames()[0]); return;
    }
    if (ed === 'add') {
      const name = panel.querySelector('[data-ed=name]').value.trim().replace(/[\\/]/g, '');
      if (!name || faceNames().includes(name)) { panel.querySelector('[data-ed=name]').focus(); return; }
      saveCfg({ custom: { ...cfg.custom, [name]: { ...(recipe(editing) || {}) } } }); editing = name; render(); lookFace(name); return;
    }
    const k = e.target.closest('[data-pf]')?.dataset.pf;
    if (k === 'close') close();
    if (k === 'petreset') { resetPet().then(render); return; }
    if (k === 'reset') { const custom = cfg.custom; store.save({ [key]: { custom } }); cfg = conf(); render(); setPreview(null); }
  });
  return close;
}

export async function initArtFace(s) {
  store = s; cfg = conf();
  const root = document.documentElement;
  new MutationObserver(paint).observe(root, { attributes: true, attributeFilter: ['data-mood'] });
  new MutationObserver(loadList).observe(root, { attributes: true, attributeFilter: ['data-skin'] });
  setInterval(paint, 60000);                  // 时段变了换表情
  // 点一下立绘：露出来的那部分能点（被卡片盖住的地方点不到）
  for (const base of document.querySelectorAll('img[data-skin-img="art"]')) base.addEventListener('click', () => {
    if (!hasFaces() || root.hasAttribute('data-tuning')) return;
    poked = Date.now() + cfg.poke * 1000; paint();
    clearTimeout(pokeTimer); pokeTimer = setTimeout(paint, cfg.poke * 1000 + 30);
  });
  await loadList();
}

// 图像模块：分“图片”和“视频”两部分。
// 图片：生成（中文描述可先转标签、画风预设、LoRA）、改图（Qwen-Image）、放大（RealESRGAN）；视频：MiniMax H3（带声音，首帧图可选，LoRA 里有加速 LoRA）。
// 尺寸、张数、秒数可以自定义（下拉的“自定义…”）。
// 下面是最近生成的图库，跟着当前部分只显示图片或只显示视频。
import { $, api, emit, esc, fname, on, reconnectNote, settings, toast, watchStream } from './api.js';
import { confirmBox, popMenu, promptBox } from './ui.js';

const LS = 'img';
let info = { models: [], presets: [], history: [], video_models: [], video_folders: [] };
let job = null, tab = 't2i', active = false, viewFile = null;
const src = { edit: null, up: null, vid: null };     // {path} 或 {data}，显示用 url
const isVideo = () => tab === 'video';
const BASES = { image: '图片', video: '视频' };       // 沙箱下的文件夹
const live = [];                          // 这次任务已出的图
const IMG = /\.(png|jpe?g|webp|gif|bmp)$/i;
const DRAG = 'application/x-hub-path';    // 和文件管理器约定的拖动数据类型

function load() { try { return JSON.parse(localStorage.getItem(LS) || '{}'); } catch (e) { return {}; } }
function save(p) { try { localStorage.setItem(LS, JSON.stringify({ ...load(), ...p })); } catch (e) {} }
const raw = p => '/api/files/raw?path=' + encodeURIComponent(p);

async function refresh() {
  info = await api('/api/img/info');
  const s = load();
  $('#img-model').innerHTML = info.models.map(m => `<option value="${m.id}"${m.ok ? '' : ' disabled'}>${esc(m.name)}${m.ok ? '' : '（没下载）'}</option>`).join('');
  if (s.model && info.models.some(m => m.id === s.model && m.ok)) $('#img-model').value = s.model;
  modelHint();
  vmodelSelect(s.vmodel);
  presetSelect(s.preset);
  folderSelect();
  gallery(); loraBox();
}

// Qwen-Image 直接读中文句子：不显示“转成标签”，提示也换掉
const T2I_HINT = $('#img-prompt').placeholder;
function modelHint() {
  const q = info.models.find(m => m.id === $('#img-model').value)?.kind === 'qwen';
  $('#img-totags').hidden = q;
  $('#img-prompt').placeholder = q ? '描述画面，直接写中文句子；画面里要出现的字写在引号里。Ctrl+Enter 生成' : T2I_HINT;
}

function vmodelSelect(cur) {
  $('#img-vmodel').innerHTML = info.video_models.map(m => `<option value="${m.id}"${m.ok ? '' : ' disabled'}>${esc(m.name)}${m.ok ? '' : '（没下载）'}</option>`).join('');
  if (cur && info.video_models.some(m => m.id === cur && m.ok)) $('#img-vmodel').value = cur;
}

// “存到”：沙箱\图片（视频是 沙箱\视频）或它下面的子文件夹；最后一项新建。图片和视频各记各的
const NEW_FOLDER = '*new';          // 文件夹名里不能有 *，不会和真的文件夹重名
const folderKey = () => isVideo() ? 'vfolder' : 'folder';
function folderSelect(cur = load()[folderKey()] || '') {
  const list = (isVideo() ? info.video_folders : info.folders) || [], b = BASES[isVideo() ? 'video' : 'image'];
  if (cur && !list.includes(cur)) cur = '';
  $('#img-folder').title = '保存位置';
  $('#img-folder').innerHTML = `<option value="" data-short="存到 ${b}">${b}</option>` +
    list.map(f => `<option value="${esc(f)}" data-short="存到 ${esc(f)}">${b}\\${esc(f)}</option>`).join('') +
    `<option value="${NEW_FOLDER}" data-desc="建在 沙箱\\${b} 下">新建文件夹…</option>`;
  $('#img-folder').value = cur;
}
const folder = () => $('#img-folder').value || null;

async function reloadInfo() {
  const d = await api('/api/img/info').catch(() => null);
  if (!d) return;
  info = d; presetSelect($('#img-preset').value || load().preset); vmodelSelect($('#img-vmodel').value || load().vmodel); folderSelect(); gallery(); loraBox();
}

function presetSelect(cur) {
  $('#img-preset').innerHTML = '<option value="">不用预设</option>' + info.presets.map(p => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join('');
  $('#img-preset').value = info.presets.some(p => p.id === cur) ? cur : '';
  note();
}

function note() {
  const p = info.presets.find(x => x.id === $('#img-preset').value);
  $('#img-preset-note').textContent = p ? [p.artists?.length ? `画师 ${p.artists.length} 位` : '', p.note || ''].filter(Boolean).join(' · ') : '';
}

// ---------------------------------------------------------------- LoRA
// 图片和视频各选各的：[{name, strength}]。只把和当前模型对得上的发出去（看不出类型的也发）。加速 LoRA 也在这里选。
const lsel = { image: [], video: [] };
let loraOpen = false, loraUp = '';            // 面板开着没；正在上传的提示
const part = () => isVideo() ? 'video' : 'image';
const modelOf = m => m === 'video' ? $('#img-vmodel').value : $('#img-model').value;
const archOf = m => info.model_arch?.[modelOf(m)] || '';
const loraBy = n => (info.loras || []).find(x => x.name === n);
const fits = (x, arch) => x && (!x.arch || x.arch === arch);
const activeLoras = m => lsel[m].filter(x => fits(loraBy(x.name), archOf(m))).map(x => ({ name: x.name, strength: x.strength }));
const nameOf = m => (m === 'video' ? info.video_models : info.models).find(x => x.id === modelOf(m))?.name.replace(/（.*/, '') || '';
function saveLoras() { save({ loras: lsel.image, vloras: lsel.video }); }

// 视频的默认步数：选了加速 LoRA 就是它的步数，否则是模型的
function vDefaultSteps() {
  const k = activeLoras('video').map(x => loraBy(x.name)?.steps).filter(Boolean);
  return k.length ? Math.min(...k) : info.video_steps?.[$('#img-vmodel').value] || '';
}

function loraBtns() {
  for (const [id, m] of [['#img-lora-btn', 'image'], ['#img-vlora-btn', 'video']]) {
    const n = activeLoras(m).length;
    $(id).textContent = n ? `LoRA · ${n}` : 'LoRA'; $(id).classList.toggle('on', n > 0);
  }
}

function loraBox() {
  loraBtns();
  const box = $('#img-lora-box'), card = $('#img-lora-card');
  box.classList.toggle('open', loraOpen && ['t2i', 'video'].includes(tab));
  const m = part(), arch = archOf(m);
  const rows = lsel[m].map((x, k) => {
    const L = loraBy(x.name), bad = !L ? '文件缺失' : !fits(L, arch) ? '与模型不匹配' : '';
    const trig = L?.triggers?.length ? `<div class="lora-sub">触发词 ${L.triggers.map(t => `<button type="button" class="tag" data-trig="${esc(t)}" title="加进提示词">${esc(t)}</button>`).join('')}</div>` : '';
    return `<div class="lora-row${bad ? ' bad' : ''}" data-k="${k}">
      <div class="lora-main"><span class="lora-name" title="${esc(x.name)}">${esc(L?.title || x.name)}</span>
        ${bad ? `<span class="err-line">${bad}</span>` : ''}<span class="sp"></span>
        <input type="range" min="-1" max="2" step="0.05" value="${x.strength}" data-st title="强度">
        <input class="ctl-in num" value="${x.strength}" data-stn inputmode="decimal" title="强度">
        <button type="button" class="btn sm" data-lora-del>移除</button></div>${trig}</div>`;
  }).join('');
  const d = m === 'image' ? info.image_defaults?.[modelOf('image')] || {} : { steps: vDefaultSteps() };
  const s = load();
  card.innerHTML = `<div class="lora-head"><b>LoRA</b><span class="sp"></span>
      <button type="button" class="btn sm" data-lora-add>添加</button><button type="button" class="btn sm" data-lora-open title="${esc(info.lora_dir || '')}">文件夹</button>
      <button type="button" class="btn sm" data-lora-refresh>刷新</button></div>
    ${rows || `<div class="lora-sub">${loraUp ? '' : '未选择。可把 .safetensors 拖到这里'}</div>`}
    ${loraUp ? `<div class="lora-sub"><span class="spin"></span> ${esc(loraUp)}</div>` : ''}
    <div class="lora-tune">步数<input class="ctl-in num" id="img-lsteps" placeholder="${esc(d.steps ?? '')}" value="${esc(s[m === 'video' ? 'vsteps' : 'steps'] ?? '')}" inputmode="numeric">
      ${m === 'image' ? `CFG<input class="ctl-in num" id="img-lcfg" placeholder="${esc(d.cfg ?? '')}" value="${esc(s.cfg ?? '')}" inputmode="decimal">` : ''}</div>`;
}

function loraAdd(btn) {
  const m = part(), arch = archOf(m), have = new Set(lsel[m].map(x => x.name));
  const list = (info.loras || []).filter(x => fits(x, arch) && !have.has(x.name));
  if (!list.length) { toast('没有可添加的 LoRA'); return; }
  popMenu(btn, list.map(x => ({ value: x.name, label: x.title, desc: [x.arch ? x.arch_name : '类型未知', x.size_mb + ' MB'].join(' · ') })),
    v => { lsel[m].push({ name: v, strength: 1 }); saveLoras(); loraBox(); }, { title: '添加 LoRA' });
}

function loraUpload(files) {
  const f = files.find(x => /\.(safetensors|sft)$/i.test(x.name));
  if (!f) { toast('只支持 .safetensors'); return; }
  const x = new XMLHttpRequest();
  x.open('POST', '/api/img/loras?name=' + encodeURIComponent(f.name));
  x.upload.onprogress = e => { if (e.lengthComputable) { loraUp = `${f.name} ${Math.round(e.loaded / e.total * 100)}%`; loraBox(); } };
  x.onload = async () => {
    loraUp = '';
    let r = {}; try { r = JSON.parse(x.responseText); } catch (e) {}
    if (x.status !== 200) { toast(r.detail || '保存失败', 4500); loraBox(); return; }
    info.loras = r.loras;
    const L = loraBy(r.name), m = part();
    if (fits(L, archOf(m))) { lsel[m].push({ name: r.name, strength: 1 }); saveLoras(); toast('已添加'); }
    else toast('已保存，但与当前模型不匹配', 4000);
    loraBox();
  };
  x.onerror = () => { loraUp = ''; toast('保存失败'); loraBox(); };
  loraUp = f.name; loraBox();
  x.send(f);
}

function initLora() {
  const s = load();
  lsel.image = Array.isArray(s.loras) ? s.loras : [];
  lsel.video = Array.isArray(s.vloras) ? s.vloras : (info.loras || []).filter(x => x.arch === 'h3' && x.steps).map(x => ({ name: x.name, strength: 1 }));   // 第一次用：默认开着加速 LoRA
  for (const id of ['#img-lora-btn', '#img-vlora-btn']) $(id).onclick = () => { loraOpen = !loraOpen; loraBox(); };
  const box = $('#img-lora-card');
  box.addEventListener('click', e => {
    const t = e.target, m = part();
    if (t.closest('[data-lora-add]')) loraAdd(t.closest('button'));
    else if (t.closest('[data-lora-refresh]')) reloadInfo().then(() => toast(`${(info.loras || []).length} 个 LoRA`));
    else if (t.closest('[data-lora-open]')) api('/api/img/loras/open', { method: 'POST' }).catch(err => toast(err.message));
    else if (t.closest('[data-lora-del]')) { lsel[m].splice(+t.closest('.lora-row').dataset.k, 1); saveLoras(); loraBox(); }
    else if (t.closest('[data-trig]')) {
      const ta = $(isVideo() ? '#img-vid-text' : '#img-prompt'), w = t.closest('[data-trig]').dataset.trig;
      if (!ta.value.includes(w)) { ta.value = ta.value.trim() ? ta.value.trim().replace(/[,，]$/, '') + ', ' + w : w; ta.dispatchEvent(new Event('input')); }
      ta.focus();
    }
  });
  box.addEventListener('input', e => {
    const t = e.target, row = t.closest('.lora-row'), m = part();
    if (row && (t.matches('[data-st]') || t.matches('[data-stn]'))) {
      const v = Math.max(-2, Math.min(2, parseFloat(t.value)));
      if (isNaN(v)) return;
      lsel[m][+row.dataset.k].strength = Math.round(v * 100) / 100; saveLoras();
      if (t.matches('[data-st]')) row.querySelector('[data-stn]').value = lsel[m][+row.dataset.k].strength; else row.querySelector('[data-st]').value = v;
      loraBtns();
    }
    if (t.id === 'img-lsteps') save({ [m === 'video' ? 'vsteps' : 'steps']: t.value.trim() });
    if (t.id === 'img-lcfg') save({ cfg: t.value.trim() });
  });
  box.addEventListener('dragover', e => { if (e.dataTransfer.types.includes('Files')) { e.preventDefault(); box.classList.add('over'); } });
  box.addEventListener('dragleave', () => box.classList.remove('over'));
  box.addEventListener('drop', e => { if (!e.dataTransfer.files.length) return; e.preventDefault(); box.classList.remove('over'); loraUpload([...e.dataTransfer.files]); });
  $('#img-model').addEventListener('change', loraBox);
  $('#img-model').addEventListener('change', modelHint);
  $('#img-vmodel').addEventListener('change', loraBox);
}
// ---------------------------------------------------------------- 自定义尺寸、张数、秒数
// 下拉最后一项“自定义…”：弹框填值，填好的值作为新选项留在下拉里。尺寸存成“宽x高”，按模型要求取整
function wh(v, lo, hi, step) {
  const m = String(v).match(/(\d+)\s*[x×*,，\s]\s*(\d+)/i); if (!m) return null;
  const r = n => Math.min(hi, Math.max(lo, Math.round(n / step) * step));
  return `${r(+m[1])}x${r(+m[2])}`;
}
function int(v, lo, hi) { const n = Math.round(+String(v).replace(/[^\d.]/g, '')); return n ? String(Math.min(hi, Math.max(lo, n))) : null; }
const CUSTOM = {
  '#img-size': { ask: '尺寸（宽×高）', ph: '1024×1536', parse: v => wh(v, 512, 2048, 64), label: v => v.replace('x', '×') },
  '#img-count': { ask: '张数（1～16）', ph: '6', parse: v => int(v, 1, 16), label: v => v + ' 张' },
  '#img-vsize': { ask: '尺寸（宽×高）', ph: '768×768', parse: v => wh(v, 256, 1280, 32), label: v => v.replace('x', '×') },
  '#img-vsecs': { ask: '秒数（1～20）', ph: '8', parse: v => int(v, 1, 20), label: v => v + ' 秒' },
};
function setOpt(sel, v) {
  if (v == null || v === '' || v === '*custom') return;
  v = String(v);
  if (![...sel.options].some(o => o.value === v)) {
    const c = CUSTOM['#' + sel.id], ok = c?.parse(v);
    if (!ok || ok !== v) return;
    sel.querySelector('[value="*custom"]').before(new Option(c.label(v), v));
  }
  sel.value = v;
}
function initCustom() {
  for (const id in CUSTOM) {
    const sel = $(id), c = CUSTOM[id];
    let prev = sel.value;
    sel.addEventListener('change', async () => {
      if (sel.value !== '*custom') { prev = sel.value; save({ [id.slice(5)]: prev }); return; }
      const t = await promptBox(c.ask, { placeholder: c.ph, ok: '确定' });
      const v = t && c.parse(t);
      if (!v) { if (t) toast('格式不对'); sel.value = prev; return; }
      setOpt(sel, v); prev = v; save({ [id.slice(5)]: v });
    });
  }
}
const numOrNull = v => (v ?? '').toString().trim() === '' ? null : +v;

// ---------------------------------------------------------------- 图库
// 在 沙箱\图片（视频是 沙箱\视频）下哪个子文件夹（根目录返回空）
const subOf = r => (r.show || '').replace(/^沙箱\\(图片|视频)\\?/, '').replace(/\\?[^\\]*$/, '');

// 卡片说明：子文件夹名单独放（界面切成英文时，文件夹名是你起的，不翻）
const metaHtml = (r, meta) => { const sub = subOf(r); return (sub ? `<span data-noi18n>${esc(sub)}</span>${meta ? ' · ' : ''}` : '') + esc(meta); };

function vcard(r) {
  const ls = r.model === 'h3turbo' ? [{ name: '加速' }] : r.loras || [], fast = ls.filter(x => x.name === '加速' || loraBy(x.name)?.steps).length;
  const mname = (info.video_models.find(m => m.id === (r.model === 'h3turbo' ? 'h3' : r.model))?.name.replace(/（.*/, '') || r.model) +
    (fast ? ' 加速' : '') + (ls.length > fast ? ` + LoRA ${ls.length - fast}` : '');
  const meta = [mname, r.size, r.length ? r.length + ' 秒' : '',
                r.first_frame ? '有首帧' : '', r.secs != null ? '用时 ' + r.secs + ' 秒' : ''].filter(Boolean).join(' · ');
  // 缩略图：鼠标移上去静音预览，点一下在右边播放（有完整的进度条、音量）
  return `<div class="card" title="${esc(r.prompt || '')}"><div class="vthumb" data-vpath="${esc(r.path)}"><video src="${raw(r.path)}" muted loop preload="metadata" playsinline></video>
    <svg class="i vplay"><use href="#ic-play"/></svg></div>
    <div class="meta2">${metaHtml(r, meta)}</div>
    <div class="acts" data-path="${esc(r.path)}">
      <button data-act="open">播放</button><button data-act="vagain">同参数</button><button data-act="copy">复制提示词</button><button data-act="del" class="del">删除</button></div></div>`;
}

function card(r) {
  if (r.kind === 'video') return vcard(r);
  const meta = [r.kind === 'edit' ? '改图' : r.kind === 'upscale' ? '放大' : (info.models.find(m => m.id === r.model)?.name || r.model) + (r.loras?.length ? ` + LoRA ${r.loras.length}` : ''),
                r.seed != null ? '种子 ' + r.seed : '', r.secs != null ? r.secs + ' 秒' : ''].filter(Boolean).join(' · ');
  return `<div class="card" title="${esc(r.prompt || '')}"><img src="${raw(r.path)}" alt="" loading="lazy" data-zoom-src="${esc(r.path)}" draggable="true">
    <div class="meta2">${metaHtml(r, meta)}</div>
    <div class="acts" data-path="${esc(r.path)}">
      <button data-act="open">打开</button><button data-act="edit">改图</button><button data-act="up">放大</button><button data-act="vid">做视频</button>
      ${r.kind === 't2i' ? '<button data-act="again">同参数</button><button data-act="copy">复制提示词</button>' : ''}<button data-act="del" class="del">删除</button></div></div>`;
}

function gallery() {
  const v = isVideo(), mine = r => (r.kind === 'video') === v;
  const seen = new Set(live.map(r => r.path));
  const pending = job && jobKind === (v ? 'video' : 'image') ? `<div class="card pending"><span class="spin"></span><span id="img-pending-text">生成中</span></div>` : '';
  $('#img-gallery').classList.toggle('vids', v);
  $('#img-gal-label').textContent = '最近生成';
  $('#img-gallery').innerHTML = pending + live.filter(mine).slice().reverse().map(card).join('') +
    info.history.filter(r => !seen.has(r.path) && mine(r)).map(card).join('') ||
    `<div class="hint">还没有${v ? '视频' : '图片'}</div>`;
}

// 删除：移到 沙箱\.回收站（和文件管理器的删除一样，能找回来）
async function delRec(p) {
  try { await api('/api/files/trash', { method: 'POST', body: { path: p } }); } catch (e) { toast(e.message); return; }
  for (const list of [live, info.history]) { const i = list.findIndex(r => r.path === p); if (i >= 0) list.splice(i, 1); }
  emit('files-trashed', { path: p }); emit('files-refresh');
  gallery(); toast('已移到回收站');
}

const findRec = p => live.find(r => r.path === p) || info.history.find(r => r.path === p);

// ---------------------------------------------------------------- 任务
function status(html) { $('#img-status').innerHTML = html; }

function onEvent(ev) {
  if (ev.type === 'status') status(`<div><span class="spin"></span> ${esc(ev.text)}</div>`);
  else if (ev.type === 'tags') { status(`<div>转成的标签：</div><div class="tags">${esc(ev.text)}</div>`); }
  else if (ev.type === 'start') {
    const prev = $('#img-status').querySelector('.tags')?.outerHTML || '';
    status(`${prev}<div>${esc(ev.model)}${ev.size ? ' · ' + esc(ev.size) : ''}${jobKind === 'video' ? '' : ` · 共 ${ev.n} 张`}</div>` +
      (ev.positive ? `<details><summary>实际发给模型的提示词</summary><div class="tags">${esc(ev.positive)}</div><div class="tags">负面：${esc(ev.negative)}</div></details>` : ''));
    $('#img-sub').textContent = ev.model;
  } else if (ev.type === 'image') {
    live.push(ev); gallery(); emit('files-refresh');
  } else if (ev.type === 'done' || ev.type === 'error' || ev.type === 'lost') {
    stopNote?.(); stopNote = null;
    job = null; save({ job: null }); syncBtns();
    if (ev.type === 'error') status(`<div class="err-line">${esc(ev.message)}</div>`);
    if (ev.type === 'lost') status('<div class="err-line">和服务的连接断了</div>');
    if (ev.type === 'done') $('#img-status').insertAdjacentHTML('beforeend', `<div>完成，${ev.secs} 秒${ev.stopped ? '（已停止）' : ''}</div>`);
    gallery(); emit('status-refresh');
    api('/api/img/info').then(d => { info.history = d.history; gallery(); });
  }
}

let stopNote = null, netSaved = '', jobKind = 'image';
function follow(id, kind = load().jobKind || 'image') {
  job = id; jobKind = kind; syncBtns(); gallery();
  const es = new EventSource(`/api/jobs/${id}/events`);
  es.onmessage = e => { const ev = JSON.parse(e.data); onEvent(ev); if (['done', 'error'].includes(ev.type)) es.close(); };
  es.onerror = () => { if (es.readyState === EventSource.CLOSED && job === id) { stopNote?.(); stopNote = null; onEvent({ type: 'lost' }); } };
  // 断了、浏览器在重连：显示重连提示，连上后换回原来的状态
  watchStream(es, s => {
    stopNote?.(); stopNote = null;
    if (s.state === 'reconnecting') { netSaved = $('#img-status').innerHTML; stopNote = reconnectNote(s.last, status); }
    else status(netSaved);
  });
}

async function start(body) {
  if (job) { toast('上一个任务还没完成'); return; }
  status('<div><span class="spin"></span> 准备中</div>');
  live.length = 0;
  try {
    const r = await api('/api/img/jobs', { method: 'POST', body });
    if (r.agent_busy) toast(`Agent 正在运行：生成${body.kind === 'video' ? '视频' : '图片'}要换显卡上的模型，Agent 下一步会重新加载`, 4500);
    const kind = body.kind === 'video' ? 'video' : 'image';
    save({ job: r.job, jobKind: kind });
    follow(r.job, kind);
  } catch (e) { status(`<div class="err-line">${esc(e.message)}</div>`); }
}

function vidBody() {
  const seed = $('#img-vseed').value.trim(), s = src.vid;
  return { kind: 'video', prompt: $('#img-vid-text').value, model: $('#img-vmodel').value, size: $('#img-vsize').value, secs: +$('#img-vsecs').value,
           seed: seed === '' ? null : +seed, folder: folder(), loras: activeLoras('video'), steps: numOrNull(load().vsteps),
           ...(s ? (s.path ? { path: s.path } : { image: s.data }) : {}) };
}

function genBody(extra = {}) {
  const seed = $('#img-seed').value.trim();
  return { kind: 't2i', prompt: $('#img-prompt').value, model: $('#img-model').value, preset: $('#img-preset').value || null,
           size: $('#img-size').value, count: +$('#img-count').value, seed: seed === '' ? null : +seed, folder: folder(),
           loras: activeLoras('image'), steps: numOrNull(load().steps), cfg: numOrNull(load().cfg), ...extra };
}

function srcBody(which) {
  const s = src[which];
  if (!s) { toast('先放一张图'); return null; }
  return s.path ? { path: s.path } : { image: s.data };
}

// ---------------------------------------------------------------- 改图、放大的原图
function setSrc(which, s) {
  src[which] = s;
  const box = document.querySelector(`.img-src[data-src="${which}"]`);
  box.classList.toggle('has', !!s);
  if (which === 'vid') {        // 视频的首帧图可以不放
    box.innerHTML = s ? `<img src="${esc(s.url)}" alt=""><div class="vid-hint"><button class="btn sm" data-clear-vid>不用首帧</button></div>` :
      `<div class="ocr-empty"><div>首帧图（可选）：粘贴或拖入</div>
       ${viewFile ? `<button class="btn" data-use-view="vid">用右边打开的图片</button>` : ''}</div>`;
    return;
  }
  box.innerHTML = s ? `<img src="${esc(s.url)}" alt="">` :
    `<div class="ocr-empty"><div>粘贴图片（Ctrl+V）、拖进来，或者在下面的图库里点“${which === 'edit' ? '改图' : '放大'}”</div>
     ${viewFile ? `<button class="btn" data-use-view="${which}">用右边打开的图片</button>` : ''}</div>`;
}

function fromBlob(which, blob) {
  if (!blob?.type.startsWith('image/')) { toast('这不是图片'); return; }
  const r = new FileReader();
  r.onload = () => setSrc(which, { data: r.result, url: r.result });
  r.readAsDataURL(blob);
}

// 底部输入框三个标签共用：只显示当前标签的文本框、选项和发送按钮；任务进行中发送换成停止
function syncBtns() {
  document.querySelectorAll('#img-composer [data-for]').forEach(x => { x.hidden = x.dataset.for !== tab || (!!job && x.classList.contains('send')); });
  $('#img-stop').hidden = !job;
}

function switchTab(t) {
  const was = isVideo();
  tab = t;
  document.querySelectorAll('#img-tabs b').forEach(b => b.classList.toggle('on', b.dataset.tab === t));
  document.querySelectorAll('#img-mode b').forEach(b => b.classList.toggle('on', b.dataset.mode === (isVideo() ? 'video' : 'image')));
  $('#img-tabs').hidden = isVideo();
  document.querySelectorAll('.img-tab').forEach(x => { x.hidden = x.dataset.tab !== t; });
  syncBtns();
  save(isVideo() ? { tab: t } : { tab: t, imgTab: t });
  if (was !== isVideo()) { folderSelect(); gallery(); }
  if (info.loras) loraBox();
  emit('img-mode', isVideo() ? 'video' : 'image');      // 左边文件管理器跟着换到 沙箱\视频 / 沙箱\图片
}

// ---------------------------------------------------------------- 画风预设
function peFill(id) {
  const p = info.presets.find(x => x.id === id) || { id: '', name: '', artists: [], style: '', negative: '' };
  $('#img-pe-pick').value = p.id;
  $('#img-pe-name').value = p.name; $('#img-pe-artists').value = (p.artists || []).join('\n');
  $('#img-pe-style').value = p.style || ''; $('#img-pe-neg').value = p.negative || '';
}

function peOpen() {
  $('#img-pe-pick').innerHTML = info.presets.map(p => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join('') + '<option value="">新建预设</option>';
  peFill($('#img-preset').value || (info.presets[0]?.id ?? ''));
  $('#img-preset-box').hidden = false;
}

async function peSave() {
  const name = $('#img-pe-name').value.trim();
  if (!name) { toast('写个名字'); return; }
  const id = $('#img-pe-pick').value || 'p' + Date.now().toString(36);
  const item = { ...(info.presets.find(x => x.id === id) || {}), id, name,
                 artists: $('#img-pe-artists').value.split('\n').map(s => s.trim()).filter(Boolean),
                 style: $('#img-pe-style').value.trim(), negative: $('#img-pe-neg').value.trim() };
  const list = info.presets.some(x => x.id === id) ? info.presets.map(x => x.id === id ? item : x) : [...info.presets, item];
  info.presets = (await api('/api/img/presets', { method: 'PUT', body: { presets: list } })).presets;
  presetSelect(id); save({ preset: id }); peOpen(); peFill(id); toast('已保存');
}

async function peDel() {
  const id = $('#img-pe-pick').value;
  if (!id || !await confirmBox('删除这个画风预设？', '', { ok: '删除', danger: true })) return;
  info.presets = (await api('/api/img/presets', { method: 'PUT', body: { presets: info.presets.filter(x => x.id !== id) } })).presets;
  presetSelect(''); peOpen();
}

// ---------------------------------------------------------------- 初始化
export async function initImage() {
  const s = load();
  $('#img-prompt').value = s.prompt || '';
  setOpt($('#img-size'), s.size); setOpt($('#img-count'), s.count);
  $('#img-vid-text').value = s.vprompt || '';
  setOpt($('#img-vsize'), s.vsize); setOpt($('#img-vsecs'), s.vsecs);
  initCustom();
  switchTab(s.tab || 't2i');
  setSrc('edit', null); setSrc('up', null); setSrc('vid', null);
  await refresh();
  initLora(); loraBox();

  $('#img-tabs').onclick = e => { const b = e.target.closest('b'); if (b) switchTab(b.dataset.tab); };
  $('#img-mode').onclick = e => { const b = e.target.closest('b'); if (b) switchTab(b.dataset.mode === 'video' ? 'video' : (load().imgTab || 't2i')); };
  $('#img-vid-text').addEventListener('input', () => save({ vprompt: $('#img-vid-text').value }));
  $('#img-vid-text').addEventListener('keydown', e => { if (e.key === 'Enter' && e.ctrlKey) { e.preventDefault(); start(vidBody()); } });
  $('#img-vid-go').onclick = () => start(vidBody());
  for (const id of ['#img-vmodel']) $(id).onchange = () => save({ [id.slice(5)]: $(id).value });
  $('#img-prompt').addEventListener('input', () => save({ prompt: $('#img-prompt').value }));
  $('#img-prompt').addEventListener('keydown', e => { if (e.key === 'Enter' && e.ctrlKey) { e.preventDefault(); start(genBody()); } });
  $('#img-go').onclick = () => start(genBody());
  $('#img-stop').onclick = async () => { if (job) await api(`/api/jobs/${job}/stop`, { method: 'POST' }); toast(jobKind === 'video' ? '已停止' : '这张画完就停'); };
  for (const id of ['#img-model']) $(id).onchange = () => save({ [id.slice(5)]: $(id).value });
  $('#img-preset').onchange = () => {
    const p = info.presets.find(x => x.id === $('#img-preset').value);
    if (p?.model && info.models.some(m => m.id === p.model && m.ok)) { $('#img-model').value = p.model; save({ model: p.model }); modelHint(); }
    save({ preset: $('#img-preset').value }); note();
  };
  $('#img-totags').onclick = async () => {
    const text = $('#img-prompt').value.trim();
    if (!text) return;
    $('#img-totags').textContent = '转换中……';
    try {
      const r = await api('/api/img/tags', { method: 'POST', body: { text, model: $('#img-model').value } });
      $('#img-prompt').value = r.tags; save({ prompt: r.tags });
    } catch (e) { toast(e.message, 4000); }
    $('#img-totags').textContent = '转成标签';
  };
  $('#img-preset-edit').onclick = peOpen;
  $('#img-pe-pick').onchange = () => peFill($('#img-pe-pick').value);
  $('#img-pe-save').onclick = () => peSave().catch(e => toast(e.message));
  $('#img-pe-del').onclick = () => peDel().catch(e => toast(e.message));
  $('#img-pe-close').onclick = () => { $('#img-preset-box').hidden = true; };

  $('#img-edit-go').onclick = () => { const b = srcBody('edit'); if (b) start({ kind: 'edit', instruction: $('#img-edit-text').value, folder: folder(), ...b }); };
  $('#img-edit-text').addEventListener('keydown', e => { if (e.key === 'Enter' && e.ctrlKey) { e.preventDefault(); $('#img-edit-go').click(); } });
  $('#img-up-go').onclick = () => { const b = srcBody('up'); if (b) start({ kind: 'upscale', scale: +$('#img-scale').value, folder: folder(), ...b }); };

  $('#img-body').addEventListener('click', e => {
    const z = e.target.closest('img[data-zoom-src]'); if (z) { emit('zoom', z.src); return; }
    const u = e.target.closest('[data-use-view]'); if (u && viewFile) { setSrc(u.dataset.useView, { path: viewFile, url: raw(viewFile) }); return; }
    if (e.target.closest('[data-clear-vid]')) { setSrc('vid', null); return; }
    const vt = e.target.closest('.vthumb'); if (vt) { emit('open-file', { path: vt.dataset.vpath }); return; }
    const b = e.target.closest('.acts button'); if (!b) return;
    const p = b.parentElement.dataset.path, r = findRec(p) || { path: p };
    if (b.dataset.act === 'open') emit('open-file', { path: p });
    if (b.dataset.act === 'del') delRec(p);
    if (b.dataset.act === 'edit') { setSrc('edit', { path: p, url: raw(p) }); switchTab('edit'); $('#img-edit-text').focus(); }
    if (b.dataset.act === 'up') { setSrc('up', { path: p, url: raw(p) }); switchTab('up'); }
    if (b.dataset.act === 'vid') { setSrc('vid', { path: p, url: raw(p) }); switchTab('video'); $('#img-vid-text').focus(); }
    if (b.dataset.act === 'vagain') {
      $('#img-vid-text').value = r.prompt || ''; $('#img-vseed').value = r.seed ?? '';
      const vm = r.model === 'h3turbo' ? 'h3' : r.model;          // 11-22 的“H3 加速”= H3 + 加速 LoRA
      if (vm && info.video_models.some(m => m.id === vm && m.ok)) $('#img-vmodel').value = vm;
      lsel.video = r.model === 'h3turbo' ? (info.loras || []).filter(x => x.arch === 'h3' && x.steps).map(x => ({ name: x.name, strength: 1 })) : (r.loras || []).map(x => ({ ...x }));
      save({ vmodel: $('#img-vmodel').value, vsteps: r.steps ?? '' }); saveLoras(); loraBox();
      setOpt($('#img-vsize'), r.sizekey); setOpt($('#img-vsecs'), r.length);
      save({ vprompt: r.prompt || '', vsize: $('#img-vsize').value, vsecs: $('#img-vsecs').value }); toast(r.first_frame ? '已填好，首帧图需重新放' : '已填好');
    }
    if (b.dataset.act === 'again') {
      $('#img-prompt').value = r.prompt || ''; $('#img-seed').value = r.seed ?? '';
      if (r.model) { $('#img-model').value = r.model; modelHint(); }
      lsel.image = (r.loras || []).map(x => ({ ...x })); save({ steps: r.steps ?? '', cfg: r.cfg ?? '' }); saveLoras();
      if (r.sizekey) { setOpt($('#img-size'), r.sizekey); save({ size: $('#img-size').value }); }
      switchTab('t2i'); loraBox(); toast('已填好');
    }
    if (b.dataset.act === 'copy') navigator.clipboard.writeText(r.positive || r.prompt || '').then(() => toast('已复制'), () => toast('复制失败'));
  });
  for (const which of ['edit', 'up', 'vid']) {
    const box = document.querySelector(`.img-src[data-src="${which}"]`);
    box.addEventListener('dragover', e => { e.preventDefault(); box.classList.add('over'); });
    box.addEventListener('dragleave', () => box.classList.remove('over'));
    box.addEventListener('drop', e => {
      e.preventDefault(); box.classList.remove('over');
      const p = e.dataTransfer.getData(DRAG);          // 从图库或文件管理器拖来的图
      if (p) { if (IMG.test(p)) setSrc(which, { path: p, url: raw(p) }); else toast('这不是图片'); return; }
      fromBlob(which, [...e.dataTransfer.files][0]);
    });
  }
  // 视频缩略图：鼠标移上去静音预览
  $('#img-gallery').addEventListener('pointerover', e => { const v = e.target.closest('.vthumb')?.querySelector('video'); if (v && v.paused) v.play().catch(() => {}); });
  $('#img-gallery').addEventListener('pointerout', e => {
    const t = e.target.closest('.vthumb'); if (!t || t.contains(e.relatedTarget)) return;
    const v = t.querySelector('video'); v.pause(); v.currentTime = 0;
  });
  // 图库里的图可以拖到左边文件管理器的文件夹上（移动），或拖到改图、放大的原图框里
  $('#img-gallery').addEventListener('dragstart', e => {
    const im = e.target.closest('img[data-zoom-src]'); if (!im) return;
    e.dataTransfer.setData(DRAG, im.dataset.zoomSrc); e.dataTransfer.effectAllowed = 'copyMove';
  });
  // 存到哪个文件夹
  $('#img-folder').onchange = async () => {
    if ($('#img-folder').value !== NEW_FOLDER) { save({ [folderKey()]: folder() || '' }); return; }
    const b = BASES[isVideo() ? 'video' : 'image'];
    const name = await promptBox('新建文件夹', { placeholder: '文件夹名字', ok: '新建' });
    if (!name) { folderSelect(); return; }
    try {
      await api('/api/files/mkdir', { method: 'POST', body: { parent: settings.config.sandbox + '\\' + b, name } });
      save({ [folderKey()]: name.trim() }); await reloadInfo(); emit('files-refresh');
    } catch (err) { toast(err.message, 4000); folderSelect(); }
  };
  // 文件管理器里移动、改名、删除之后：图库的路径、改图和放大的原图跟着变
  on('files-moved', ({ src: from, dst }) => {
    const k = from.toLowerCase(), fix = p => p && (p.toLowerCase() === k || p.toLowerCase().startsWith(k + '\\')) ? dst + p.slice(from.length) : p;
    live.forEach(r => { r.path = fix(r.path); });
    for (const w of ['edit', 'up', 'vid']) if (src[w]?.path && fix(src[w].path) !== src[w].path) { const p = fix(src[w].path); setSrc(w, { path: p, url: raw(p) }); }
    for (const [key, b] of [['folder', '图片'], ['vfolder', '视频']]) {
      const f = load()[key];
      if (f) { const full = settings.config.sandbox + '\\' + b + '\\' + f, nf = fix(full); if (nf !== full) save({ [key]: nf.replace(new RegExp('^.*?\\\\' + b + '\\\\'), '') }); }
    }
    reloadInfo();
  });
  on('files-trashed', () => reloadInfo());
  on('files-changed', () => reloadInfo());
  document.addEventListener('paste', e => {
    if (!active || tab === 't2i') return;
    const it = [...(e.clipboardData?.items || [])].find(i => i.type.startsWith('image/'));
    if (it) { e.preventDefault(); fromBlob(tab === 'edit' ? 'edit' : tab === 'video' ? 'vid' : 'up', it.getAsFile()); }
  });
  on('mod', m => { active = m === 'img'; if (active) reloadInfo(); });
  on('active-file', p => {
    viewFile = p && IMG.test(p) ? p : null;
    for (const w of ['edit', 'up', 'vid']) if (!src[w]) setSrc(w, null);
  });
  if (s.job) follow(s.job);
}

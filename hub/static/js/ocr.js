// 识字模块：贴图或拖图，可以框选一部分，PaddleOCR-VL 识别；识别结果可以翻译；也可以对图片提问。
import { $, api, emit, esc, fname, md, on, toast } from './api.js';
import { translateText, setInput } from './translate.js';

const MAX_PIXELS = 2_800_000;   // PaddleOCR-VL 一张图最多按约 280 万像素处理，再大先缩小
const MIN_PIXELS = 200_000;     // 框得很小时放大一些，小字更容易认
let img = null;                 // {url, path(已有文件), blob, w, h, saved}
let sel = null;                 // 框选区域，按原图像素
let viewFile = null, busy = false, active = false;
const IMG = /\.(png|jpe?g|webp|gif|bmp)$/i;

function show() {
  const has = !!img;
  $('#ocr-empty').hidden = has; $('#ocr-stage').hidden = !has;
  $('#ocr-drop').classList.toggle('has', has);
  $('#ocr-go').disabled = $('#ocr-go-tr').disabled = !has || busy;
  $('#ocr-unsel').hidden = !sel;
  $('#ocr-hint').textContent = !has ? '' : sel ? `已框选 ${sel.w}×${sel.h}` : `在图上拖动，可以只识别框住的部分（整张 ${img.w}×${img.h}）`;
  const box = $('#ocr-sel');
  box.hidden = !sel;
  if (sel) {
    const k = $('#ocr-img').clientWidth / img.w;
    Object.assign(box.style, { left: sel.x * k + 'px', top: sel.y * k + 'px', width: sel.w * k + 'px', height: sel.h * k + 'px' });
  }
}

function setImage(src, path = null) {
  const el = $('#ocr-img');
  el.onload = () => { img.w = el.naturalWidth; img.h = el.naturalHeight; show(); };
  el.onerror = () => { toast('图片打不开'); clear(); };
  if (img?.url?.startsWith('blob:')) URL.revokeObjectURL(img.url);
  img = { url: src, path, w: 0, h: 0, saved: path };
  sel = null; el.src = src;
  $('#ocr-res').hidden = true; $('#ocr-trans').hidden = true; $('#ocr-ans').innerHTML = '';
  $('#ocr-saved').textContent = path ? fname(path) : '';
  show();
}

function fromBlob(blob) {
  if (!blob || !blob.type.startsWith('image/')) { toast('这不是图片'); return; }
  setImage(URL.createObjectURL(blob));
}

function clear() {
  if (img?.url?.startsWith('blob:')) URL.revokeObjectURL(img.url);
  img = null; sel = null; $('#ocr-img').removeAttribute('src');
  $('#ocr-res').hidden = true; $('#ocr-ans').innerHTML = ''; $('#ocr-sub').textContent = '';
  show();
}

// 按框选裁切，太大缩小、太小放大，转成 PNG
function prepared() {
  const r = sel || { x: 0, y: 0, w: img.w, h: img.h };
  if (!sel && img.path) return null;    // 整张已有文件：直接让服务读文件
  let k = 1;
  const px = r.w * r.h;
  if (px > MAX_PIXELS) k = Math.sqrt(MAX_PIXELS / px);
  else if (px < MIN_PIXELS) k = Math.min(3, Math.sqrt(MIN_PIXELS / px));
  const c = document.createElement('canvas');
  c.width = Math.max(1, Math.round(r.w * k)); c.height = Math.max(1, Math.round(r.h * k));
  const g = c.getContext('2d');
  g.imageSmoothingQuality = 'high';
  g.drawImage($('#ocr-img'), r.x, r.y, r.w, r.h, 0, 0, c.width, c.height);
  return c.toDataURL('image/png');
}

function imageBody() {
  const data = prepared();
  if (!data) return { path: img.path };
  // 贴进来的整张图第一次识别时存到 沙箱\识字；框选的部分不存
  return { image: data, save: !img.saved && !sel };
}

async function recognize(thenTranslate = false) {
  if (!img || busy) return;
  busy = true; show();
  $('#ocr-res').hidden = false; $('#ocr-trans').hidden = true;
  $('#ocr-text').value = ''; $('#ocr-text').placeholder = '正在识别……（第一次要加载模型，十几秒）';
  $('#ocr-sub').innerHTML = '<span class="spin"></span>';
  try {
    const r = await api('/api/ocr', { method: 'POST', body: { ...imageBody(), mode: $('#ocr-mode').value } });
    if (r.agent_busy) toast('Agent 正在运行：识字要换显卡上的模型，Agent 下一步会重新加载', 4000);
    $('#ocr-text').value = r.text; $('#ocr-text').placeholder = r.text ? '' : '没有识别到文字';
    $('#ocr-sub').textContent = `PaddleOCR-VL · ${r.secs} 秒`;
    if (r.path && !img.path) { img.saved = r.path; $('#ocr-saved').textContent = '原图存在 ' + r.show; emit('files-refresh'); }
    fit();
    emit('status-refresh');
    if (thenTranslate && r.text) await doTranslate();
  } catch (e) {
    $('#ocr-sub').textContent = ''; $('#ocr-text').placeholder = '识别失败：' + e.message; toast('识别失败：' + e.message);
  } finally { busy = false; show(); }
}

function fit() { const t = $('#ocr-text'); t.style.height = 'auto'; t.style.height = Math.min(Math.max(t.scrollHeight + 4, 120), 420) + 'px'; }

async function doTranslate() {
  const text = $('#ocr-text').value.trim();
  if (!text) { toast('没有要翻译的文字'); return; }
  const box = $('#ocr-trans');
  box.hidden = false; box.innerHTML = '<span class="spin"></span> 正在翻译……';
  try {
    const r = await translateText(text);
    box.innerHTML = `${esc(r.text)}<div class="hint" style="margin-top:4px">${esc(r.model)} · ${r.secs} 秒</div>`;
    emit('status-refresh');
  } catch (e) { box.textContent = '翻译失败：' + e.message; }
}

async function ask() {
  const q = $('#ocr-q').value.trim();
  if (!img) { toast('先贴一张图：Ctrl+V，或者拖进来'); return; }
  if (!q) return;
  const out = $('#ocr-ans');
  out.innerHTML = '<span class="spin"></span>';
  $('#ocr-ask-go').disabled = true;
  try {
    const r = await fetch('/api/ocr/ask', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...imageBody(), save: false, question: q, ocr_text: $('#ocr-res').hidden ? '' : $('#ocr-text').value }) });
    if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(d.detail || d.error || `请求失败 ${r.status}`); }
    $('#ocr-ask-model').textContent = r.headers.get('X-Model') || '';
    const reader = r.body.getReader(), dec = new TextDecoder();
    let text = '';
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      text += dec.decode(value, { stream: true });
      out.innerHTML = md(text); out.classList.add('cursor');
    }
    out.classList.remove('cursor');
    emit('status-refresh');
  } catch (e) { out.textContent = '提问失败：' + e.message; }
  finally { $('#ocr-ask-go').disabled = false; }
}

// 在图上拖动框选
function initSelect() {
  const stage = $('#ocr-stage');
  stage.addEventListener('pointerdown', e => {
    if (!img?.w) return;
    const r = $('#ocr-img').getBoundingClientRect(), k = img.w / r.width;
    const x0 = (e.clientX - r.left) * k, y0 = (e.clientY - r.top) * k;
    stage.setPointerCapture(e.pointerId);
    const move = ev => {
      const x1 = Math.min(Math.max((ev.clientX - r.left) * k, 0), img.w), y1 = Math.min(Math.max((ev.clientY - r.top) * k, 0), img.h);
      const s = { x: Math.round(Math.min(x0, x1)), y: Math.round(Math.min(y0, y1)), w: Math.round(Math.abs(x1 - x0)), h: Math.round(Math.abs(y1 - y0)) };
      sel = s.w > 8 && s.h > 8 ? s : null; show();
    };
    const up = () => { stage.removeEventListener('pointermove', move); stage.removeEventListener('pointerup', up); };
    stage.addEventListener('pointermove', move); stage.addEventListener('pointerup', up);
  });
  new ResizeObserver(() => sel && show()).observe($('#ocr-img'));
}

export function initOcr() {
  initSelect();
  $('#ocr-pick').onclick = () => $('#ocr-file').click();
  $('#ocr-file').onchange = e => { const f = e.target.files[0]; if (f) fromBlob(f); e.target.value = ''; };
  $('#ocr-use-view').onclick = () => viewFile && setImage('/api/files/raw?path=' + encodeURIComponent(viewFile), viewFile);
  $('#ocr-clear').onclick = clear;
  $('#ocr-unsel').onclick = () => { sel = null; show(); };
  $('#ocr-go').onclick = () => recognize(false);
  $('#ocr-go-tr').onclick = () => recognize(true);
  $('#ocr-tr').onclick = doTranslate;
  $('#ocr-copy').onclick = () => navigator.clipboard.writeText($('#ocr-text').value).then(() => toast('已复制'), () => toast('复制失败'));
  $('#ocr-send').onclick = () => { setInput($('#ocr-text').value); emit('switch-mod', 'tr'); };
  $('#ocr-text').addEventListener('input', fit);
  $('#ocr-ask-go').onclick = ask;
  $('#ocr-q').addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); ask(); } });

  const drop = $('#ocr-drop');
  drop.addEventListener('dragover', e => { e.preventDefault(); drop.classList.add('over'); });
  drop.addEventListener('dragleave', () => drop.classList.remove('over'));
  drop.addEventListener('drop', e => {
    e.preventDefault(); drop.classList.remove('over');
    const f = [...e.dataTransfer.files].find(f => f.type.startsWith('image/'));
    if (f) fromBlob(f); else toast('拖进来的不是图片');
  });
  // 识字页打开时，Ctrl+V 贴图（输入框里贴文字照常）
  document.addEventListener('paste', e => {
    // 识字页开着（单独页面，或者放在对话右侧）时，Ctrl+V 贴图
    if (!active && !document.querySelector('#mod-ocr.docked:not([hidden])')) return;
    const it = [...(e.clipboardData?.items || [])].find(i => i.type.startsWith('image/'));
    if (!it) return;
    e.preventDefault(); fromBlob(it.getAsFile());
  });
  on('active-file', p => { viewFile = p && IMG.test(p) ? p : null; $('#ocr-use-view').hidden = !viewFile; });
  on('mod', m => { active = m === 'ocr'; });
  show();
}

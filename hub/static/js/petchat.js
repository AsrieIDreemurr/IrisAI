// 工作台里点桌宠：她身边弹出随手问的对话气泡，就地问一句（样式在 css/pet.css 的 .pa-）。
// 和入口页的随手问是同一个话题、同一个人格（都记在本机 localStorage 的 portal 里），模型也一样（9B，CPU 上跑，不占显卡）。
// 气泡开着时她不闲逛、答完也不再冒“结果第一句”（答案就在气泡里）；收起来时答完，照常在头上说第一句。
import { api, esc, events, md, reconnectNote, settings, toast } from './api.js';
import { setMood } from './skin.js';
import { pet, petBox, petTalk, petStep, petWait, petRound, petFinal, petEmote } from './pet.js';

const LS = 'portal';
const load = () => { try { return JSON.parse(localStorage.getItem(LS) || '{}'); } catch (e) { return {}; } };
const save = p => { try { localStorage.setItem(LS, JSON.stringify({ ...load(), ...p })); } catch (e) {} };
let el = null, cid = null, rid = null, stop = null, cur = null, loaded = false, solo = false;   // solo：桌面程序里的聊天小窗（petchat.html）
const steps = new Map();
// 随手问用的模型：模型清单（config/models.yaml）里的 fast。工作台里已有配置，桌面小窗自己取一次
const fastModel = async () => (settings.config || await api('/api/config').catch(() => ({}))).roles?.fast;
const q = s => el.querySelector(s);

function build() {
  document.body.insertAdjacentHTML('beforeend', `<div class="pa" id="pa" role="dialog" aria-label="随手问" hidden>
    <div class="pa-h"><b>随手问</b><span class="pa-who"></span><span class="sp"></span>
      <button type="button" class="pa-btn" data-a="new" title="新话题">新话题</button>
      <a class="pa-btn" data-a="open" hidden>在对话里打开</a>
      <button type="button" class="pa-x" data-a="close" aria-label="关闭">✕</button></div>
    <div class="pa-msgs"></div>
    <div class="pa-status"></div>
    <div class="pa-in"><textarea rows="1" placeholder="问点什么……（回车发送）"></textarea>
      <button type="button" class="pa-send" data-a="send" aria-label="发送">➤</button>
      <button type="button" class="pa-send stop" data-a="stop" hidden>停止</button></div></div>`);
  el = document.getElementById('pa');
  el.addEventListener('click', e => {
    const a = e.target.closest('[data-a]')?.dataset.a;
    if (solo && a === 'open' && cid) {          // 小窗里不打开完整对话，交给桌面程序的主窗口
      e.preventDefault(); window.pywebview?.api?.open_main?.('/app?conv=' + cid); return;
    }
    if (a === 'close') close();
    else if (a === 'send') send();
    else if (a === 'stop') rid && api(`/api/runs/${rid}/stop`, { method: 'POST' });
    else if (a === 'new') fresh();
    else if (a === 'ok' || a === 'no' || a === 'answer') answer(e.target.closest('.pa-card'), a, e.target.dataset.val);
  });
  const ta = q('textarea');
  ta.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
    if (e.key === 'Escape') close();
  });
  ta.addEventListener('input', () => { ta.style.height = 'auto'; ta.style.height = Math.min(ta.scrollHeight, 110) + 'px'; });
  // 点到气泡和她以外的地方就收起（在跑也没关系，再点她回来接着看）
  document.addEventListener('pointerdown', e => {
    if (!solo && !el.hidden && !el.contains(e.target) && !petBox()?.contains(e.target)) close();
  }, true);
  addEventListener('resize', () => { if (!el.hidden) placeAsk(); });
}

// 气泡放在她身旁头部高度：右边放得下就放右边，不然放左边；尖角指着她
export function placeAsk() {
  if (!el || el.hidden || solo) return;
  const b = petBox().getBoundingClientRect(), w = el.offsetWidth, h = el.offsetHeight;
  const right = b.right - b.width * .12 + w + 8 <= innerWidth;
  const x = right ? b.right - b.width * .12 : b.left + b.width * .12 - w;
  const y = Math.min(Math.max(b.top + b.height * .35 - h, 46), innerHeight - h - 8);
  el.style.left = Math.max(8, x) + 'px'; el.style.top = y + 'px';
  el.classList.toggle('left', !right);
  el.style.setProperty('--tail-y', Math.min(Math.max(b.top + b.height * .35 - y - 10, 14), h - 26) + 'px');
}

export async function openAsk() {
  if (!el) build();
  if (!el.hidden) { q('textarea').focus(); return; }
  el.hidden = false; pet.askOpen = true;
  placeAsk();
  q('textarea').focus();
  if (!loaded) { loaded = true; await restore(); placeAsk(); }
}
function close() {
  if (solo) { window.pywebview?.api?.close_chat?.(); return; }      // 小窗：让桌面程序把窗口收起来
  if (!el || el.hidden) return; el.hidden = true; pet.askOpen = false;
}

// 桌面程序的聊天小窗：气泡铺满整个窗口。每次被叫出来时（desktop_app.py 调 petChatOpened），
// 入口页那边换了话题就重新接上，然后把光标放进输入框
export async function initSolo() {
  solo = true; build();
  el.classList.add('solo'); el.hidden = false; pet.askOpen = true; loaded = true;
  await restore();
  window.petChatOpened = async () => {
    if ((load().cid || null) !== cid && !rid) await restore();
    who(); q('textarea').focus(); scroll();
  };
  // 点到别处（小窗失去焦点）就收起，和网页里点外面收起一样；在跑也没关系，再点她回来接着看
  addEventListener('blur', () => setTimeout(() => { if (!document.hasFocus()) close(); }, 150));
  q('textarea').focus();
}

function who() {
  const p = load().persona;
  q('.pa-who').textContent = p ? '· ' + p : '';
}
const box = () => q('.pa-msgs');
const scroll = () => { box().scrollTop = box().scrollHeight; };
function empty() {
  if (!box().children.length) box().innerHTML = '<div class="pa-empty">想问什么直接打字～<br>查资料、算个数、看看文件都行。</div>';
}
function turn() {
  let t = box().lastElementChild;
  if (!t?.classList.contains('pa-a')) { box().insertAdjacentHTML('beforeend', '<div class="pa-a"></div>'); t = box().lastElementChild; }
  return t;
}
function userMsg(text) { box().querySelector('.pa-empty')?.remove(); box().insertAdjacentHTML('beforeend', `<div class="pa-u">${esc(text)}</div>`); }
function textBlock(text) { const d = document.createElement('div'); d.className = 'md-body'; d.innerHTML = md(text); turn().appendChild(d); return d; }
const status = t => { q('.pa-status').innerHTML = t; };
const busy = b => { q('[data-a=send]').hidden = b; q('[data-a=stop]').hidden = !b; };
function openLink() { const a = q('[data-a=open]'); a.hidden = !cid; if (cid) a.href = '/app?conv=' + cid; }

// 打开时接上入口页随手问的话题：显示最近几句，还在跑就接着看
// 事件流彻底连不上了：等服务能回话了重新载入这个话题（和入口页的 recover 一样；先等服务回话，restore 请求失败会把话题清掉）
async function recover() {
  busy(false); rid = null; setMood();
  status('<span class="spin"></span> 连接断了，正在查这次运行的情况');
  let ok = false;
  for (let i = 0; i < 4 && !ok && cid; i++) {
    try { await api('/api/convs/' + cid); ok = true; } catch (e) { await new Promise(r => setTimeout(r, 2500)); }
  }
  if (!ok) { status('连不上服务，服务恢复后点“在对话里打开”接着看'); return; }
  await restore();
  if (!rid) status(box().lastElementChild?.classList.contains('pa-u') ? '这次运行中断了（服务可能重启过），回答没有保存，可以重新发送' : '');
}

async function restore() {
  cid = load().cid || null; who(); openLink();
  box().innerHTML = '';
  if (cid) {
    try {
      const d = await api('/api/convs/' + cid);
      if (d.conv.archived) throw new Error('archived');
      let msgs = d.msgs;
      if (d.run) { const last = msgs.map(m => m.role).lastIndexOf('user'); msgs = msgs.slice(0, last + 1); }
      msgs = msgs.filter(m => (m.role === 'user' && !(m.meta || {}).system) || (m.role === 'assistant' && m.content && !(m.tool_calls || []).length));
      for (const m of msgs.slice(-6)) {
        if (m.role === 'user') userMsg(m.content);
        else textBlock(m.content);
      }
      if (d.run) attach(d.run);
    } catch (e) { cid = null; save({ cid: null }); openLink(); }
  }
  empty(); scroll();
}

function fresh() {
  if (rid) { toast('这个话题还在运行，先停止再开新话题'); return; }
  cid = null; save({ cid: null }); openLink();
  box().innerHTML = ''; empty(); status('');
  q('textarea').focus();
}

async function send() {
  const ta = q('textarea'), text = ta.value.trim();
  if (!text || rid) return;
  ta.value = ''; ta.style.height = 'auto';
  try {
    if (!cid) {
      cid = (await api('/api/convs', { method: 'POST', body: { model: await fastModel() } })).id; save({ cid });
      const p = load().persona;
      if (p) await api(`/api/convs/${cid}/persona`, { method: 'PUT', body: { persona: p } }).catch(() => save({ persona: null }));
      who(); openLink();
    }
    userMsg(text); scroll(); placeAsk();
    status('<span class="spin"></span> 发送中');
    const r = await api(`/api/convs/${cid}/send`, { method: 'POST', body: { text, profile: 'lite', web: true } });
    attach(r.run);
  } catch (e) { status(''); toast(e.message, 4000); }
}

function card(ev) {
  const ask = ev.kind === 'ask';
  turn().insertAdjacentHTML('beforeend', `<div class="pa-card" data-cid="${esc(ev.id || '')}"><b>${esc(ev.title)}</b>
    ${(ev.steps || []).length ? `<ol>${ev.steps.map(s => `<li>${esc(s)}</li>`).join('')}</ol>` : ''}
    <div class="pa-row">${ask ? (ev.options || []).map(o => `<button class="pa-btn" data-a="answer" data-val="${esc(o)}">${esc(o)}</button>`).join('')
      : '<button class="pa-btn on" data-a="ok">允许</button><button class="pa-btn" data-a="no">取消</button>'}</div></div>`);
}
async function answer(c, decision, note) {
  if (!c || !rid) return;
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
        if (ev.state === 'reconnecting') { netSaved = q('.pa-status').innerHTML; stopNote?.(); stopNote = reconnectNote(ev.last, status); }
        else { stopNote?.(); stopNote = null; status(netSaved); }
        break;
      case 'llm': status('<span class="spin"></span> 想一想'); break;
      case 'emote': petEmote(ev.face); break;
      case 'delta':
        status(''); petTalk(ev.text);
        if (!cur) cur = { text: '', el: textBlock('') };
        cur.text += ev.text; cur.el.innerHTML = md(cur.text); break;
      case 'llm_done': petRound(); cur = null; break;
      case 'step': {
        petStep(ev);
        let s = steps.get(ev.id);
        if (!s) { s = document.createElement('div'); s.className = 'pa-step'; turn().appendChild(s); steps.set(ev.id, s); }
        const icon = { running: '<span class="spin"></span>', ok: '✓', error: '✕', denied: '－', waiting: '…' }[ev.status] || '';
        s.innerHTML = `${icon} ${esc(ev.title)}`;
        if (ev.status === 'running') status(`<span class="spin"></span> ${esc(ev.title)}`);
        break;
      }
      case 'confirm': petWait(ev.kind); card(ev); status(ev.kind === 'ask' ? '等你回答' : '等你确认'); break;
      case 'confirm_done': {
        const c = box().querySelector(`.pa-card[data-cid="${CSS.escape(ev.id || '')}"] .pa-row`);
        if (c) c.textContent = { ok: '已允许。', no: '已取消。', answer: '你的回答：' + (ev.note || '') }[ev.decision] || '';
        status(''); break;
      }
      case 'final': petFinal(ev.text); if (cur) { cur.text = ev.text; cur.el.innerHTML = md(ev.text); } else textBlock(ev.text); break;
      case 'notice': case 'status': status(esc(ev.text)); break;
      case 'error': failed = true; turn().insertAdjacentHTML('beforeend', `<div class="pa-err">${esc(ev.text)}</div>`); break;
      case 'lost': stopNote?.(); stopNote = null; recover(); break;
      case 'done': {
        stopNote?.(); stopNote = null;
        const stopped = /停/.test(q('.pa-status').textContent);
        setMood(failed || stopped ? 'error' : 'done', failed || stopped ? 8000 : 6000);
        rid = null; busy(false); if (!stopped) status('');
        break;
      }
    }
    scroll(); placeAsk();
  });
}

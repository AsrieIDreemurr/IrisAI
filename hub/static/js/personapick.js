// 对话输入框旁的“人格”按钮：这个对话用哪个人格（knowledge\人格），或者不用。
// 对话还没建时先记着，发第一条消息建好对话后再存。和 kbpick.js 同一套做法。
import { $, api, emit, esc, on } from './api.js';

let cid = null;
let cur = null;            // 当前对话的人格名；null = 不用
let pending = undefined;   // 对话还没建时选的
let list = [];

async function load() { list = (await api('/api/personas')).personas; }

function label() {
  const name = pending !== undefined ? pending : cur;
  const chip = $('#chip-persona');
  chip.innerHTML = `<span class="dd-lb">${name ? '人格 · ' + esc(name) : '人格'}</span><span class="dd-cv">▾</span>`;
  chip.classList.toggle('on', !!name);
  const tag = $('#conv-persona');
  tag.hidden = !name; tag.textContent = name || '';
  tag.title = name ? `这个对话用人格“${name}”` : '';
}

function render() {
  const name = pending !== undefined ? pending : cur;
  $('#pop-persona').innerHTML = `
    <section><div class="label"><span class="spark"></span>人格：选了以后，回答用这个人格的身份和语气</div>
      <label class="kb-opt"><input type="radio" name="persona" value=""${name ? '' : ' checked'}>
        <span><b>不用人格</b><br><small>普通助手</small></span></label>
      ${list.map(p => `<label class="kb-opt"><input type="radio" name="persona" value="${esc(p.name)}"${p.name === name ? ' checked' : ''}>
        <span><b>${esc(p.name)}</b> <small>${p.chars} 字</small><br><small>${esc(p.desc || '没有说明')}</small></span></label>`).join('')}
      ${list.length ? '' : '<div class="hint">还没有人格。在知识库页面的人格层新建一个。</div>'}</section>
    <section><div class="hint">人格只改说话方式，不改干活的规则和规范。<a href="#" data-goto-kb>管理人格</a></div></section>`;
}

async function save() {
  const v = $('#pop-persona').querySelector('input[name=persona]:checked')?.value || null;
  if (cid) { cur = (await api(`/api/convs/${cid}/persona`, { method: 'PUT', body: { persona: v } })).persona; pending = undefined; }
  else pending = v;
  label();
}

// 发第一条消息、对话刚建好时调用
export async function applyPendingPersona(id) {
  cid = id;
  if (pending !== undefined) {
    cur = (await api(`/api/convs/${id}/persona`, { method: 'PUT', body: { persona: pending } }).catch(() => ({ persona: null }))).persona;
    pending = undefined; label();
  }
}

export async function initPersonaPick() {
  await load().catch(() => {});
  label();
  const pop = $('#pop-persona'), chip = $('#chip-persona');
  const close = () => { pop.classList.remove('open'); chip.classList.remove('open'); };
  chip.onclick = async e => {
    e.stopPropagation();
    if (pop.classList.contains('open')) return close();
    $('#pop-kb').classList.remove('open'); $('#chip-kb').classList.remove('open');
    await load().catch(() => {});
    render(); pop.classList.add('open'); chip.classList.add('open');
  };
  pop.addEventListener('change', save);
  pop.addEventListener('click', e => {
    e.stopPropagation();
    if (e.target.closest('[data-goto-kb]')) { e.preventDefault(); close(); emit('switch-mod', 'kb'); }
  });
  $('#chip-kb').addEventListener('click', close);
  document.addEventListener('click', close);
  on('conv', async id => {
    cid = id; pending = undefined;
    cur = id ? (await api(`/api/convs/${id}/persona`).catch(() => ({ persona: null }))).persona : null;
    label();
  });
  on('kb-changed', () => load().then(label).catch(() => {}));
}

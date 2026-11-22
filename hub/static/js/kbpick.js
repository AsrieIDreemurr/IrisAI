// 对话输入框旁的“知识”按钮：勾选这个对话附带哪些规范（全文给 Agent）、打开哪些其他层的库（Agent 才能检索）。
// 对话还没建时先记着，发第一条消息建好对话后再存。
import { $, api, esc, on } from './api.js';

let cid = null;
let sel = null;          // {norms:[], other:[]}；null 表示用默认
let info = { norms: [], libs: [] };

const size = n => n >= 1000 ? (n / 1000).toFixed(1) + 'k 字节' : n + ' 字节';

async function loadInfo() { info = await api('/api/kb'); }

function defaults() { return { norms: info.norms.filter(n => n.default).map(n => n.name), other: [] }; }

function label() {
  const s = sel || defaults();
  const n = s.norms.length, o = s.other.length;
  const chip = $('#chip-kb');
  chip.innerHTML = `<span class="dd-lb">知识${n || o ? ` · ${[n ? '规范 ' + n : '', o ? '其他 ' + o : ''].filter(Boolean).join('，')}` : ''}</span><span class="dd-cv">▾</span>`;
  chip.classList.toggle('on', !!(n || o));
}

function render() {
  const s = sel || defaults();
  const know = info.libs.filter(l => l.layer === '知识' && l.files);
  const other = info.libs.filter(l => l.layer === '其他');
  $('#pop-kb').innerHTML = `
    <section><div class="label"><span class="spark"></span>规范：勾选后全文附在这个对话里</div>
      ${info.norms.map(n => `<label class="kb-opt"><input type="checkbox" data-norm="${esc(n.name)}"${s.norms.includes(n.name) ? ' checked' : ''}>
        <span><b>${esc(n.title)}</b> <small>${size(n.size)}</small><br><small>${esc(n.desc || '没有说明')}</small></span></label>`).join('') || '<div class="hint">规范层还是空的</div>'}
      <div class="hint">没勾的规范，Agent 需要时也能自己读。</div></section>
    <section><div class="label"><span class="spark"></span>其他层：勾选后 Agent 才能检索</div>
      ${other.map(l => `<label class="kb-opt"><input type="checkbox" data-other="${esc(l.id)}"${s.other.includes(l.id) ? ' checked' : ''}>
        <span><b>${esc(l.name)}</b> <small>${l.files} 个文件</small><br><small>${esc(l.desc || '')}</small></span></label>`).join('') || '<div class="hint">其他层还没有子库</div>'}</section>
    <section><div class="hint">知识层始终可以检索：${know.map(l => esc(l.name)).join('、') || '还没有文件'}</div></section>`;
}

async function save() {
  const pop = $('#pop-kb');
  sel = { norms: [...pop.querySelectorAll('[data-norm]:checked')].map(x => x.dataset.norm),
          other: [...pop.querySelectorAll('[data-other]:checked')].map(x => x.dataset.other) };
  label();
  if (cid) sel = await api(`/api/convs/${cid}/kb`, { method: 'PUT', body: sel });
}

// 发第一条消息、对话刚建好时调用
export async function applyPending(id) {
  cid = id;
  if (sel) sel = await api(`/api/convs/${id}/kb`, { method: 'PUT', body: sel });
}

export async function initKbPick() {
  await loadInfo().catch(() => {});
  label();
  $('#chip-kb').onclick = async e => {
    e.stopPropagation();
    const pop = $('#pop-kb');
    if (pop.classList.contains('open')) { pop.classList.remove('open'); $('#chip-kb').classList.remove('open'); return; }
    await loadInfo().catch(() => {});
    render(); pop.classList.add('open'); $('#chip-kb').classList.add('open');
  };
  $('#pop-kb').addEventListener('change', save);
  $('#pop-kb').addEventListener('click', e => e.stopPropagation());
  document.addEventListener('click', () => { $('#pop-kb').classList.remove('open'); $('#chip-kb').classList.remove('open'); });
  on('conv', async id => {
    cid = id;
    sel = id ? await api(`/api/convs/${id}/kb`).catch(() => null) : null;
    label();
  });
  on('kb-changed', () => loadInfo().then(label).catch(() => {}));
}

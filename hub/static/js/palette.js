// 标题栏搜索：Ctrl+K 打开，按名字找对话和文件。上下键选择，回车打开，Esc 关闭。
import { $, api, emit, esc } from './api.js';

let box, input, list, items = [], sel = 0, timer, seq = 0;

function show() {
  box.hidden = false; input.value = ''; input.focus();
  requestAnimationFrame(() => box.classList.add('open'));
  search('');
}

function hide() { box.classList.remove('open'); box.hidden = true; }

async function search(q) {
  const my = ++seq;
  const convs = await api('/api/convs').catch(() => []);
  const words = q.toLowerCase().split(/\s+/).filter(Boolean);
  const hit = t => words.every(w => t.toLowerCase().includes(w));
  let res = convs.filter(c => hit(c.title)).slice(0, q ? 8 : 6).map(c => ({ kind: 'conv', id: c.id, name: c.title, sub: '对话' }));
  if (words.length) {
    const files = await api('/api/files/find?q=' + encodeURIComponent(q)).catch(() => []);
    res = res.concat(files.slice(0, 30).map(f => ({ kind: 'file', path: f.path, name: f.name, sub: f.show, dir: f.dir })));
  }
  if (my !== seq) return;   // 输入变了，丢掉旧结果
  items = res; sel = 0; draw(q);
}

function draw(q) {
  if (!items.length) { list.innerHTML = `<div class="pal-empty">${q ? '没有找到' : '还没有对话'}</div>`; return; }
  let last = '';
  list.innerHTML = items.map((it, i) => {
    const head = it.kind !== last ? `<div class="pal-sec">${it.kind === 'conv' ? '对话' : '文件'}</div>` : '';
    last = it.kind;
    const icon = it.kind === 'conv' ? '#ic-chat' : it.dir ? '#ic-dir' : '#ic-doc';
    return head + `<div class="pal-it${i === sel ? ' on' : ''}" data-i="${i}"><svg class="i"><use href="${icon}"/></svg>
      <span class="nm">${esc(it.name)}</span><span class="sub">${esc(it.kind === 'conv' ? '' : it.sub)}</span></div>`;
  }).join('');
  list.querySelector('.on')?.scrollIntoView({ block: 'nearest' });
}

function pick(i) {
  const it = items[i]; if (!it) return;
  hide();
  if (it.kind === 'conv') emit('open-conv', it.id);
  else { emit('open-file', { path: it.path }); emit('reveal-tree', it.path); }
}

export function initPalette() {
  box = document.createElement('div');
  box.className = 'palette'; box.hidden = true;
  box.innerHTML = `<div class="pal-card"><div class="pal-in"><svg class="i"><use href="#ic-search"/></svg>
    <input placeholder="输入对话标题或文件名" spellcheck="false"><kbd>Esc</kbd></div><div class="pal-list"></div></div>`;
  document.body.appendChild(box);
  input = box.querySelector('input'); list = box.querySelector('.pal-list');
  input.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(() => search(input.value.trim()), 150); });
  input.addEventListener('keydown', e => {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault(); sel = (sel + (e.key === 'ArrowDown' ? 1 : -1) + items.length) % Math.max(items.length, 1); draw(input.value);
    } else if (e.key === 'Enter') { e.preventDefault(); pick(sel); }
    else if (e.key === 'Escape') hide();
  });
  list.addEventListener('click', e => { const it = e.target.closest('.pal-it'); if (it) pick(+it.dataset.i); });
  list.addEventListener('mousemove', e => {
    const it = e.target.closest('.pal-it'); if (it && +it.dataset.i !== sel) { sel = +it.dataset.i; list.querySelectorAll('.pal-it').forEach(x => x.classList.toggle('on', +x.dataset.i === sel)); }
  });
  box.addEventListener('mousedown', e => { if (e.target === box) hide(); });
  $('#btn-search').onclick = show;
  document.addEventListener('keydown', e => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); box.hidden ? show() : hide(); }
  });
}

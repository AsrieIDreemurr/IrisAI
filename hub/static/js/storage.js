// 设置页的“存储”卡片：各类东西占了多少空间（GET /api/storage，后端 app/storage.py），
// 测试副本、网页检查用的浏览器资料可以一键挪进回收站（POST /api/storage/clean）。
// 第一次滚到这张卡片时才去统计（要走一遍文件夹），之后点“重新统计”。
// 回收站可以清空（POST /api/storage/delete）。2026-11-22 起“待删除”合并进回收站，以前的 _待删除_ 文件夹也列在回收站里。
import { api, esc, on, toast } from './api.js';
import { confirmBox } from './ui.js';

const size = b => b >= 2 ** 30 ? (b / 2 ** 30).toFixed(1) + ' GB' : b >= 2 ** 20 ? Math.round(b / 2 ** 20) + ' MB' : Math.max(0, Math.round(b / 1024)) + ' KB';
let card, body, data = null, loading = false, open = new Set(['tests']);

export const STORAGE_CARD = `<section class="set-card st-card" id="st-card">
    <div class="set-h st-h">存储<span class="sp"></span><button type="button" class="btn sm" data-st="refresh" hidden>重新统计</button></div>
    <div class="st-body"><div class="hint st-wait">滚到这里时统计……</div></div></section>`;

export function initStorage(root) {
  card = root.querySelector('#st-card'); body = card.querySelector('.st-body');
  const ob = new IntersectionObserver(es => { if (es.some(e => e.isIntersecting) && !data) { ob.disconnect(); load(); } });
  ob.observe(card);
  // 打开设置页时卡片已经露在外面也统计一次（浏览器在后台时上面的观察可能不触发）
  on('mod', m => {
    if (m !== 'set') return;
    setTimeout(() => { if (!data && !loading && card.getClientRects().length && card.getBoundingClientRect().top < innerHeight) { ob.disconnect(); load(); } }, 300);
  });
  card.addEventListener('click', onClick);
  card.addEventListener('toggle', e => { const d = e.target.closest?.('details[data-cat]'); if (d) d.open ? open.add(d.dataset.cat) : open.delete(d.dataset.cat); }, true);
}

async function load() {
  if (loading) return;
  loading = true;
  body.innerHTML = '<div class="hint"><span class="spin"></span> 正在统计……</div>';
  try { data = await api('/api/storage'); render(); }
  catch (e) { body.innerHTML = `<div class="err-line">${esc(e.message)}</div>`; }
  finally { loading = false; card.querySelector('[data-st=refresh]').hidden = false; }
}

function render() {
  const disks = data.disks.map(d => `<div class="st-disk"><b>${esc(d.drive)} 盘</b>
      <span class="meter"><b style="width:${(100 * d.used / d.total).toFixed(0)}%"></b></span>
      <span class="num">剩 ${size(d.free)} / 共 ${size(d.total)}</span></div>`).join('');
  const cats = data.cats.map(c => {
    const can = c.movable ? c.items.filter(i => !i.recent) : [];
    const btn = c.movable && c.items.length ? `<button type="button" class="btn sm" data-st="clean" data-cat="${c.id}"${can.length ? '' : ' disabled'}
        title="${can.length ? '挪进回收站，可以找回' : '都是 30 分钟内改过的，可能正在用'}">挪进回收站${can.length ? `（${size(can.reduce((s, i) => s + i.bytes, 0))}）` : ''}</button>`
      : c.deletable && c.bytes ? `<button type="button" class="btn sm danger" data-st="del" data-cat="${c.id}" title="彻底删除，不能恢复">清空</button>` : '';
    const rows = c.items.map(i => `<li><span class="st-name" title="${esc(i.path)}">${esc(i.name)}</span>
        ${i.in_use === true ? '<span class="st-tag on">在用</span>' : i.in_use === false ? '<span class="st-tag">没在用</span>' : ''}
        ${c.movable && i.recent ? '<span class="st-tag warn">刚改过</span>' : ''}
        <span class="num">${size(i.bytes)}</span>
        ${c.deletable && i.bytes ? `<button type="button" class="st-del" data-st="del" data-cat="${c.id}" data-path="${esc(i.path)}" title="彻底删除，不能恢复">删除</button>` : ''}</li>`).join('');
    return `<details class="st-cat" data-cat="${c.id}"${open.has(c.id) ? ' open' : ''}>
        <summary><b>${esc(c.name)}</b><span class="num">${size(c.bytes)}</span>${btn}</summary>
        <div class="st-desc">${esc(c.desc)}</div>
        ${c.items.length ? `<ul>${rows}</ul>` : '<div class="st-desc">（空）</div>'}</details>`;
  }).join('');
  body.innerHTML = `<div class="st-disks">${disks}</div>${cats}`;
}

async function onClick(e) {
  const b = e.target.closest('[data-st]'); if (!b) return;
  if (b.dataset.st === 'refresh') return load();
  if (b.dataset.st === 'del') { e.preventDefault(); return del(b); }
  if (b.dataset.st !== 'clean') return;
  e.preventDefault();                                   // 按钮在 summary 里：别顺带展开 / 收起
  const c = data.cats.find(x => x.id === b.dataset.cat), can = c.items.filter(i => !i.recent);
  const ok = await confirmBox(`把${c.name}挪进回收站？`, `${can.length} 项，共 ${size(can.reduce((s, i) => s + i.bytes, 0))}。` +
    (c.items.length > can.length ? `另有 ${c.items.length - can.length} 项刚改过，这次不挪。` : ''), { ok: '挪走' });
  if (!ok) return;
  b.disabled = true;
  try {
    const r = await api('/api/storage/clean', { method: 'POST', body: { cat: c.id } });
    toast(`挪走 ${size(r.moved.reduce((s, i) => s + i.bytes, 0))}` + (r.skipped.length ? `，${r.skipped.length} 项没挪（${r.skipped[0].why}）` : ''), 4500);
  } catch (err) { toast(err.message, 4000); }
  load();
}

// 彻底删除：回收站里的一项（data-path），或者整个清空
async function del(b) {
  const c = data.cats.find(x => x.id === b.dataset.cat), p = b.dataset.path;
  const items = p ? c.items.filter(i => i.path === p) : c.items, total = size(items.reduce((s, i) => s + i.bytes, 0));
  const what = p ? `删除 ${items[0]?.name}` : '清空回收站';
  if (!await confirmBox(`彻底${what}？`, `共 ${total}，不能恢复。`, { ok: '彻底删除', danger: true })) return;
  b.disabled = true;
  try {
    const r = await api('/api/storage/delete', { method: 'POST', body: { cat: c.id, path: p || null } });
    const n = size(r.deleted.reduce((s, i) => s + i.bytes, 0));
    toast(r.failed.length ? `删掉 ${n}，${r.failed.length} 项没删掉（${r.failed[0].why}）` : `已腾出 ${n}`, 4500);
  } catch (err) { toast(err.message, 4000); }
  load();
}

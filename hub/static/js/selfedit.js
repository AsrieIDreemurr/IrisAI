// Agent 自改的审核：对话里的卡片和状态栏“自改”弹出层共用。应用、拒绝、撤回只在这里，Agent 没有对应的工具。
import { $, api, emit, esc, on, settings, toast } from './api.js';
import { confirmBox } from './ui.js';

const STATUS = { editing: '正在改', proposed: '等你审核', applying: '应用中或应用中断', apply_failed: '应用失败，可撤回恢复', applied: '已上线', rejected: '已拒绝', reverted: '已撤回' };
const KIND = { changed: '改', added: '新增', deleted: '删除' };
const when = t => t ? new Date(t * 1000).toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '';

export function seCard(r) {
  if (!r) return '';
  const files = (r.files || []).map(([f, k]) => `<li><span class="mono">${esc(f)}</span> <small>${KIND[k] || k}</small></li>`).join('');
  const tests = r.tests ? `<div class="hint">${r.tests.ok ? '检查通过' : '检查没通过'}：${esc((r.tests.notes || []).filter(n => !n.startsWith('测试实例在')).slice(0, 3).join('；') || '语法和主要接口都正常')}</div>` : '';
  const acts = r.status === 'proposed'
    ? `<button class="btn primary" data-se-act="apply" data-se="${r.id}" ${r.can_apply === false ? 'disabled title="需要开启自改进及此副本所需的权限"' : ''}>应用</button><button class="btn" data-se-act="reject" data-se="${r.id}">拒绝</button>`
    : ['applied', 'apply_failed', 'applying'].includes(r.status) ? `<button class="btn" data-se-act="revert" data-se="${r.id}">撤回</button>` : '';
  return `<div class="se-card" data-se-card="${r.id}">
    <div class="se-head"><b>${esc(r.goal || '自改')}</b><span class="se-st ${r.status}">${STATUS[r.status] || r.status}</span>
      <span class="hint">${r.full ? '高权限 · ' : ''}${when(r.t_applied || r.t_proposed || r.t)}</span></div>
    ${r.summary ? `<div class="se-sum">${esc(r.summary)}</div>` : ''}
    ${files ? `<ul class="se-files">${files}</ul>` : ''}${tests}
    <div class="row">${r.diff ? `<button class="btn" data-open="${esc(r.diff)}">看改动对比</button>` : ''}${acts}
      ${r.restart && r.status === 'proposed' ? '<span class="hint">应用后服务会在没有任务运行时自动重启</span>' : ''}</div></div>`;
}

async function act(id, what) {
  if (what === 'apply' && !await confirmBox('应用这个改动？', '上线前会备份，之后可以撤回。', { ok: '应用' })) return;
  if (what === 'revert' && !await confirmBox('撤回这个改动？', '恢复到应用之前的样子。', { ok: '撤回' })) return;
  let r;
  try { r = await api(`/api/selfedit/${id}/${what}`, { method: 'POST' }); }
  catch (e) {
    if (what === 'revert' && /仍然撤回/.test(e.message) && await confirmBox('应用之后又改过，仍然撤回？', e.message, { ok: '仍然撤回', danger: true })) {
      r = await api(`/api/selfedit/${id}/revert`, { method: 'POST', body: { force: true } });
    } else { toast(e.message, 6000); return; }
  }
  document.querySelectorAll(`[data-se-card="${id}"]`).forEach(c => { c.outerHTML = seCard(r); });
  toast({ apply: r.restart ? '已应用，服务会在空闲时自动重启，之后刷新页面' : '已应用，刷新页面生效',
          reject: '已拒绝', revert: r.restart ? '已撤回，服务会在空闲时自动重启' : '已撤回，刷新页面生效' }[what], 5000);
  refreshPop();
}

async function refreshPop() {
  try {
    const d = await api('/api/selfedit', { quiet: true });
    // 开关可能在别处被改了（另一台设备、测试实例、服务端自己关掉）：页面里存的值和服务端不一样，就同步过来，
    // 设置页的开关跟着变。以前设置页只认页面打开时读到的值，自改已经关了，开关还显示开着
    if (settings.data && (settings.data.self_edit_enabled !== d.enabled || settings.data.self_edit_full !== d.full)) {
      settings.data.self_edit_enabled = d.enabled; settings.data.self_edit_full = d.full;
      emit('selfedit-sync');
    }
    const wait = d.items.filter(r => r.status === 'proposed').length;
    $('#st-se').textContent = '自改 · ' + (d.enabled ? (d.full ? '高权限' : '已开启') : '已关闭') + (wait ? ` · ${wait} 个待审` : '');
    $('#st-se').classList.toggle('alert', wait > 0);
    const hint = d.enabled ? '还没有自改记录。在对话里让 Agent 改界面或流程时会出现在这里。' : '自改进已关闭。可以在设置里开启；已有记录仍可查看、拒绝或撤回。';
    $('#pop-se-body').innerHTML = (!d.enabled ? `<div class="hint">${hint}</div>` : '') + (d.items.map(seCard).join('') || (d.enabled ? `<div class="hint">${hint}</div>` : ''));
    d.items.forEach(r => document.querySelectorAll(`[data-se-card="${r.id}"]`).forEach(c => { c.outerHTML = seCard(r); }));
  } catch (e) { /* 服务重启中 */ }
}

// 对话里的卡片：先占位，再按编号取最新状态
export function seCardFor(id) {
  setTimeout(async () => {
    try { const r = await api('/api/selfedit/' + id); document.querySelectorAll(`[data-se-card="${id}"]`).forEach(c => { c.outerHTML = seCard(r); }); }
    catch (e) { /* 忽略 */ }
  }, 0);
  return `<div class="se-card" data-se-card="${id}"><span class="spin"></span></div>`;
}

export function initSelfEdit() {
  document.addEventListener('click', e => {
    const b = e.target.closest('[data-se-act]');
    if (b) { e.stopPropagation(); act(b.dataset.se, b.dataset.seAct); }
  });
  refreshPop();
  on('selfedit-settings', refreshPop);
  on('mod', m => { if (m === 'set') refreshPop(); });          // 打开设置页时取一次最新的
  window.addEventListener('focus', refreshPop);                // 切回窗口时也取一次
  setInterval(refreshPop, 15000);
  document.addEventListener('click', e => { if (e.target.closest('#st-se-wrap')) refreshPop(); });
}

// 知识库页面：上面搜索，下面三层（规范、知识、其他）。文件拖进子库就加进去，后台建索引。
import { $, api, emit, esc, fname, on, settings, toast } from './api.js';
import { confirmBox, promptBox } from './ui.js';
import { openMemory } from './memory.js';

let info = { libs: [], norms: [], status: {} };
const open = new Set();          // 展开的子库
const filesOf = {};              // 子库 -> 文件列表
let viewFile = null, pickLib = null, timer = null, active = false;
const hist = {};                // 规范名 -> 修改历史（展开时才取）
const phist = {};               // 人格名 -> 修改历史
const LAYER_NOTE = { '规范': '可以附在对话里（输入框旁的“知识”按钮），Agent 也能按需读取全文',
                     '知识': 'Agent 自己检索', '其他': 'Agent 默认不碰；在对话里打开某个库后才检索，也可以在这里自己搜' };
const size = n => n >= 1048576 ? (n / 1048576).toFixed(1) + ' MB' : n >= 1024 ? (n / 1024).toFixed(0) + ' KB' : n + ' 字节';

async function refresh() {
  info = await api('/api/kb');
  info.personas = (await api('/api/personas').catch(() => ({ personas: [] }))).personas;
  info.memory = (await api('/api/memory').catch(() => ({ memory: [] }))).memory;
  for (const id of open) if (!id.startsWith('记忆/')) filesOf[id] = (await api('/api/kb/files?lib=' + encodeURIComponent(id))).files;
  render(); status();
  emit('kb-changed');
}

function status() {
  const s = info.status || {};
  $('#kb-sub').textContent = s.running ? `正在建索引 ${s.done} / ${s.total}：${s.file}` :
    `${info.libs.reduce((a, l) => a + l.files, 0)} 个文件，${info.libs.reduce((a, l) => a + l.chunks, 0)} 段` + (s.errors ? `，${s.errors} 个出错` : '');
  clearTimeout(timer);
  if (s.running && active) timer = setTimeout(() => refresh().catch(() => {}), 2000);
}

function scopeOptions() {
  const cur = $('#kb-scope').value;
  $('#kb-scope').innerHTML = '<option value="">全部库</option><option value="@知识">知识层</option><option value="@其他">其他层</option>' +
    info.libs.map(l => `<option value="${esc(l.id)}">${esc(l.id)}</option>`).join('');
  if (cur) $('#kb-scope').value = cur;
}

function libCard(l) {
  const fs = filesOf[l.id] || [];
  const body = open.has(l.id) ? `<div class="body">
      ${fs.map(f => `<div class="kfile"><span class="nm" data-open="${esc(f.path)}" title="${esc(f.path)}">${f.linked ? '↗ ' : ''}${esc(f.name)}</span>
        ${f.error ? `<span class="err" title="${esc(f.error)}">出错</span>` : `<span class="ct">${f.chunks} 段</span>`}
        ${f.linked ? '' : `<button data-remove="${esc(f.path)}" title="移到 knowledge\\.回收站">移走</button>`}</div>`).join('') || '<div class="hint">还没有文件</div>'}
      <div class="row"><button class="btn sm" data-pick="${esc(l.id)}">选择文件</button>
        ${viewFile ? `<button class="btn sm" data-addview="${esc(l.id)}">添加右边打开的 ${esc(fname(viewFile))}</button>` : ''}
        <span class="hint">也可以把文件拖到这个框里，或者直接放进文件夹</span></div></div>` : '';
  return `<div class="lib" data-lib="${esc(l.id)}">
    <div class="head" data-toggle-lib="${esc(l.id)}"><span>${open.has(l.id) ? '▾' : '▸'}</span><span class="nm">${esc(l.name)}</span>
      <span class="acts"><button data-desc="${esc(l.id)}">改说明</button></span><span class="ct">${l.files} 个文件 · ${l.chunks} 段</span></div>
    <div class="desc">${esc(l.desc || '还没有说明。写一句这个库放什么，Agent 靠它判断该不该查这里。')}</div>${body}</div>`;
}

function memLayer() {
  const ms = info.memory || [];
  if (!ms.length) return '<div class="hint">还没有人格，也就还没有记忆。</div>';
  return ms.map(m => {
    const id = '记忆/' + m.persona;
    const body = open.has(id) ? `<div class="body">${m.recent.map(n => `<div class="kfile"><span class="nm" data-open="${esc(n.path)}" title="打开这条笔记，可以直接改">
        <small>${esc(n.time.slice(5))} · ${esc(n.kind)} · 重要度 ${n.imp}</small> ${esc(n.text)}</span></div>`).join('') || '<div class="hint">还没有记忆</div>'}
      <div class="row"><button class="btn sm primary" data-memory="${esc(m.persona)}">打开记忆页</button><button class="btn sm" data-open="${esc(m.dir + '\\笔记\\')}">打开笔记文件夹</button>
        <span class="hint">最近 20 条。忘掉的在 _已淡忘 里（${m.gone} 条），移回 笔记 就能恢复</span></div></div>` : '';
    return `<div class="lib"><div class="head" data-toggle-lib="${esc(id)}"><span>${open.has(id) ? '▾' : '▸'}</span><span class="nm">${esc(m.persona)}</span>
      <span class="ct">${m.notes} 条记忆${m.gone ? ` · ${m.gone} 条已淡忘` : ''}</span></div>${body}</div>`;
  }).join('');
}

function render() {
  scopeOptions();
  const norms = info.norms.map(n => `<div class="norm"><div class="main"><b data-open="${esc(n.path)}" style="cursor:pointer">${esc(n.title)}</b>
      <small>${esc(n.desc || '还没有说明')} · ${size(n.size)}</small></div>
      <span class="chip${n.default ? ' on' : ''}" data-default="${esc(n.name)}" title="新对话自动附带这份规范">默认带上</span>
      <button class="btn sm" data-ndesc="${esc(n.name)}">改说明</button><button class="btn sm" data-hist="${esc(n.name)}">${hist[n.name] ? '收起历史' : '历史'}</button>
      ${hist[n.name] ? `<div class="norm-hist">${hist[n.name].map(h => `<div class="h"><b>${esc(h.stamp.replace(/(\d{4})(\d\d)(\d\d)-(\d\d)(\d\d)\d\d/, '$2-$3 $4:$5'))}</b>
        <span>${esc(h.who)}${h.created ? '新建' : '修改'}：${esc(h.reason || '没写原因')}</span>
        ${h.backup ? `<button class="btn sm" data-restore="${esc(n.name)}" data-stamp="${esc(h.stamp)}" title="恢复到这次修改之前的内容">撤回这次修改</button>` : ''}</div>`).join('') || '<span class="hint">还没有修改记录</span>'}</div>` : ''}</div>`).join('') ||
    '<div class="hint">把规范文件（md、txt、docx……）放进 knowledge\\规范 文件夹，或拖到这里。</div>';
  const layer = name => {
    const ls = info.libs.filter(l => l.layer === name);
    return `<div class="layer"><div class="lh"><b>${name}层</b><span class="hint">${LAYER_NOTE[name]}</span><span class="sp"></span>
      <button class="btn sm" data-newlib="${name}">新建子库</button></div>${ls.map(libCard).join('') || '<div class="hint">还没有子库</div>'}</div>`;
  };
  const stamp = s => s.replace(/(\d{4})(\d\d)(\d\d)-(\d\d)(\d\d)\d\d/, '$2-$3 $4:$5');
  const personas = (info.personas || []).map(p => `<div class="norm"><div class="main"><b data-open="${esc(p.path)}" style="cursor:pointer" title="在右边打开，可以直接编辑">${esc(p.name)}</b>
      <small>${esc(p.desc || '还没有说明')} · ${p.chars} 字</small></div>
      <button class="btn sm" data-open="${esc(p.path)}">编辑</button><button class="btn sm" data-pdesc="${esc(p.name)}">改说明</button>
      <button class="btn sm" data-phist="${esc(p.name)}">${phist[p.name] ? '收起历史' : '历史'}</button>
      <button class="btn sm" data-plines="${esc(p.name)}" title="按人格卡让模型重写桌宠在各种情况下说的话（旧的留在人格文件夹里）">生成桌宠台词</button>
      <button class="btn sm" data-premove="${esc(p.name)}" title="移到 knowledge\\.回收站">移走</button>
      ${phist[p.name] ? `<div class="norm-hist">${phist[p.name].map(h => `<div class="h"><b>${esc(stamp(h.stamp))}</b>
        <span>${esc(h.who)}${h.created ? '新建' : '修改'}：${esc(h.reason || '没写原因')}</span>
        ${h.backup ? `<button class="btn sm" data-prestore="${esc(p.name)}" data-stamp="${esc(h.stamp)}" title="恢复到这次修改之前的内容">撤回这次修改</button>` : ''}</div>`).join('') || '<span class="hint">还没有修改记录</span>'}</div>` : ''}</div>`).join('') ||
    '<div class="hint">还没有人格。新建一个会生成一张带提示的人格卡，在右边改好就能在对话里选。</div>';
  // 系统提示词：点名字在右边打开、直接改；开头注释写着什么时候用
  const sys = (info.sysprompts || []).map(p => `<div class="norm"><div class="main"><b data-open="${esc(p.path)}" style="cursor:pointer" title="在右边打开，可以直接编辑">${esc(p.name)}</b>
      <span class="hint">${esc(p.when)}</span><span class="sp"></span>${p.custom ? '<span class="chip on" title="和默认内容不一样">已修改</span>' +
      `<button class="btn sm" data-sysreset="${esc(p.name)}">恢复默认</button>` : ''}</div></div>`).join('');
  $('#kb-layers').innerHTML = `<div class="layer"><div class="lh"><b>系统提示词</b><span class="hint">每次发消息时发给模型的工作规则</span></div>${sys}</div>` +
    `<div class="layer"><div class="lh"><b>人格层</b><span class="hint">在对话里（输入框旁的“人格”按钮）选一个，回答就用它的身份和语气</span><span class="sp"></span>
      <button class="btn sm" data-newpersona="1">新建人格</button></div>${personas}</div>` +
    `<div class="layer"><div class="lh"><b>记忆层</b><span class="hint">开了人格的对话里自动记下、自动想起；每个人格一份，笔记都是文本，可以直接改</span></div>${memLayer()}</div>` +
    `<div class="layer" data-lib="规范"><div class="lh"><b>规范层</b><span class="hint">${LAYER_NOTE['规范']}</span></div>${norms}</div>` +
    layer('知识') + layer('其他');
}

// ---------------------------------------------------------------- 搜索
async function search() {
  const q = $('#kb-q').value.trim();
  if (!q) return;
  const sc = $('#kb-scope').value;
  const libs = !sc ? null : sc.startsWith('@') ? info.libs.filter(l => l.layer === sc.slice(1)).map(l => l.id) : [sc];
  $('#kb-hits').innerHTML = '<div class="hint"><span class="spin"></span> 搜索中</div>';
  try {
    const r = await api('/api/kb/search', { method: 'POST', body: { query: q, libs, count: 8 } });
    $('#kb-secs').textContent = `${r.hits.length} 段 · ${r.secs} 秒`;
    $('#kb-hits').innerHTML = r.hits.map((h, i) => `<div class="hit" data-i="${i}">
      <div class="src"><b>${esc(h.lib)}</b><span>${esc(fname(h.path))} ${esc(h.loc)}</span><span>相关度 ${h.score}</span></div>
      <div class="txt">${esc(h.text)}</div>
      <div class="row"><button class="btn sm" data-open="${esc(h.path)}" data-line="${h.line || ''}">在右边打开</button>
        <button class="btn sm" data-insert="${i}">插入到对话</button><button class="btn sm" data-more="${i}">展开</button></div></div>`).join('') ||
      '<div class="hint">没有找到。换个说法，或者用更具体的关键词。</div>';
    $('#kb-hits').hits = r.hits;
  } catch (e) { $('#kb-hits').innerHTML = `<div class="err-line">${esc(e.message)}</div>`; }
}

// ---------------------------------------------------------------- 加文件
async function upload(lib, fileList) {
  const fd = new FormData();
  fd.append('lib', lib);
  for (const f of fileList) fd.append('files', f);
  const r = await fetch('/api/kb/upload', { method: 'POST', body: fd });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) { toast(d.detail || '添加失败', 4000); return; }
  toast(`已添加 ${d.saved.length} 个文件，后台建索引`);
  open.add(lib); setTimeout(() => refresh().catch(() => {}), 600);
}

export async function initKb() {
  $('#kb-go').onclick = search;
  $('#kb-q').addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); search(); } });
  $('#kb-scan').onclick = async () => { await api('/api/kb/scan', { method: 'POST' }); toast('开始扫描'); setTimeout(() => refresh().catch(() => {}), 800); };
  $('#kb-file').onchange = e => { if (pickLib && e.target.files.length) upload(pickLib, e.target.files); e.target.value = ''; };

  $('#kb-body').addEventListener('click', async e => {
    const t = e.target;
    const o = t.closest('[data-open]');
    if (o) { emit('open-file', { path: o.dataset.open, line: +o.dataset.line || undefined }); return; }
    const tl = t.closest('[data-toggle-lib]');
    if (tl && !t.closest('.acts')) {
      const id = tl.dataset.toggleLib;
      if (open.has(id)) open.delete(id); else open.add(id);
      await refresh(); return;
    }
    const d = t.closest('[data-desc]');
    if (d) {
      const l = info.libs.find(x => x.id === d.dataset.desc);
      const v = await promptBox('这个库放什么？', { text: '写一句话，Agent 靠它判断该不该查这里。', value: l?.desc || '', ok: '保存' });
      if (v != null) { await api('/api/kb/libs', { method: 'PATCH', body: { lib: d.dataset.desc, desc: v.trim() } }); await refresh(); }
      return;
    }
    const nd = t.closest('[data-ndesc]');
    if (nd) {
      const n = info.norms.find(x => x.name === nd.dataset.ndesc);
      const v = await promptBox('这份规范在什么时候用？', { text: '写一句话，Agent 靠它判断要不要读。', value: n?.desc || '', ok: '保存' });
      if (v != null) { await api('/api/kb/norms', { method: 'PATCH', body: { name: nd.dataset.ndesc, desc: v.trim() } }); await refresh(); }
      return;
    }
    const hs = t.closest('[data-hist]');
    if (hs) {
      const name = hs.dataset.hist;
      if (hist[name]) delete hist[name];
      else hist[name] = (await api('/api/kb/norms/history?name=' + encodeURIComponent(name))).history;
      render(); return;
    }
    const rs = t.closest('[data-restore]');
    if (rs) {
      if (!await confirmBox('撤回这次修改？', '这份规范会恢复到这次修改之前的内容。当前内容会先存进历史，可以再撤回回来。', { ok: '撤回' })) return;
      try { hist[rs.dataset.restore] = (await api('/api/kb/norms/restore', { method: 'POST', body: { name: rs.dataset.restore, stamp: rs.dataset.stamp } })).history; toast('已撤回'); await refresh(); }
      catch (err) { toast(err.message, 4000); }
      return;
    }
    const mp = t.closest('[data-memory]');
    if (mp) { openMemory(mp.dataset.memory); return; }
    // ---- 系统提示词
    const sr = t.closest('[data-sysreset]');
    if (sr) {
      if (!await confirmBox(`把${sr.dataset.sysreset}恢复成默认内容？`, '现在的版本会存进 knowledge\.历史。', { ok: '恢复默认' })) return;
      try { info.sysprompts = (await api('/api/kb/sysprompt/reset', { method: 'POST', body: { name: sr.dataset.sysreset } })).sysprompts; render(); toast('已恢复，下一条消息起生效'); }
      catch (err) { toast(err.message); }
      return;
    }
    // ---- 人格层
    const np = t.closest('[data-newpersona]');
    if (np) {
      const name = await promptBox('新建人格', { text: '名字就是对话里显示的名字，例如 恋恋。', placeholder: '人格名字', ok: '下一步' });
      if (!name?.trim()) return;
      const desc = (await promptBox('一句话说明', { text: '在对话里选人格时显示，以后可以再改。', placeholder: '例如 古明地恋，轻快、随性', ok: '新建' })) || '';
      try {
        const r = await api('/api/personas', { method: 'POST', body: { name: name.trim(), desc: desc.trim() } });
        await refresh(); emit('open-file', { path: r.path }); toast('已新建，在右边按提示改人格卡');
      } catch (err) { toast(err.message, 4000); }
      return;
    }
    const pd = t.closest('[data-pdesc]');
    if (pd) {
      const p = info.personas.find(x => x.name === pd.dataset.pdesc);
      const v = await promptBox('一句话说明', { text: '在对话里选人格时显示。', value: p?.desc || '', ok: '保存' });
      if (v != null) { await api('/api/personas', { method: 'PATCH', body: { name: pd.dataset.pdesc, desc: v.trim() } }); await refresh(); }
      return;
    }
    const ph = t.closest('[data-phist]');
    if (ph) {
      const name = ph.dataset.phist;
      if (phist[name]) delete phist[name];
      else phist[name] = (await api('/api/personas/history?name=' + encodeURIComponent(name))).history;
      render(); return;
    }
    const pr = t.closest('[data-prestore]');
    if (pr) {
      if (!await confirmBox('撤回这次修改？', '人格卡会恢复到这次修改之前的内容。当前内容会先存进历史，可以再撤回回来。', { ok: '撤回' })) return;
      try { phist[pr.dataset.prestore] = (await api('/api/personas/restore', { method: 'POST', body: { name: pr.dataset.prestore, stamp: pr.dataset.stamp } })).history; toast('已撤回'); await refresh(); }
      catch (err) { toast(err.message, 4000); }
      return;
    }
    const pl = t.closest('[data-plines]');
    if (pl) {
      const name = pl.dataset.plines;
      if (!await confirmBox('重新生成桌宠台词？', `按“${name}”的人格卡让模型重写桌宠台词（戳她、打招呼、干活时说的话）。要切换到显卡上的大模型，大约半分钟到一分钟；旧的台词库改名留在人格文件夹里。`, { ok: '生成' })) return;
      pl.disabled = true; pl.textContent = '生成中…';
      try { await api('/api/personas/petlines', { method: 'POST', body: { name } }); toast('桌宠台词已更新，刷新页面后生效', 3500); }
      catch (err) { toast(err.message, 4000); }
      pl.disabled = false; pl.textContent = '生成桌宠台词';
      return;
    }
    const pm = t.closest('[data-premove]');
    if (pm) {
      if (!await confirmBox('移走这个人格？', `人格“${pm.dataset.premove}”会移到 knowledge\\.回收站，不会真删。正在用它的对话会变回不用人格。`, { ok: '移走', danger: true })) return;
      try { await api('/api/personas/remove', { method: 'POST', body: { name: pm.dataset.premove } }); delete phist[pm.dataset.premove]; await refresh(); }
      catch (err) { toast(err.message, 4000); }
      return;
    }
    const df = t.closest('[data-default]');
    if (df) {
      const n = info.norms.find(x => x.name === df.dataset.default);
      await api('/api/kb/norms', { method: 'PATCH', body: { name: n.name, default: !n.default } }); await refresh(); return;
    }
    const nl = t.closest('[data-newlib]');
    if (nl) {
      const name = await promptBox(`在${nl.dataset.newlib}层新建子库`, { placeholder: '子库名字，例如 某某游戏', ok: '下一步' });
      if (!name?.trim()) return;
      const desc = (await promptBox('这个库放什么？', { text: '一句话，以后可以再改。', placeholder: '例如 某某游戏的剧情、人物和术语', ok: '新建' })) || '';
      try { const r = await api('/api/kb/libs', { method: 'POST', body: { layer: nl.dataset.newlib, name: name.trim(), desc: desc.trim() } }); open.add(r.lib); await refresh(); }
      catch (err) { toast(err.message, 4000); }
      return;
    }
    const pk = t.closest('[data-pick]');
    if (pk) { pickLib = pk.dataset.pick; $('#kb-file').click(); return; }
    const av = t.closest('[data-addview]');
    if (av && viewFile) {
      try { await api('/api/kb/add', { method: 'POST', body: { lib: av.dataset.addview, path: viewFile } }); toast('已添加，后台建索引'); setTimeout(() => refresh().catch(() => {}), 600); }
      catch (err) { toast(err.message, 4000); }
      return;
    }
    const rm = t.closest('[data-remove]');
    if (rm) {
      if (!await confirmBox('移出知识库？', `${fname(rm.dataset.remove)} 会移到 knowledge\\.回收站，不会真删。`, { ok: '移出', danger: true })) return;
      try { await api('/api/kb/remove', { method: 'POST', body: { path: rm.dataset.remove } }); setTimeout(() => refresh().catch(() => {}), 500); }
      catch (err) { toast(err.message, 4000); }
      return;
    }
    const hits = $('#kb-hits').hits || [];
    const ins = t.closest('[data-insert]');
    if (ins) {
      const h = hits[+ins.dataset.insert];
      const box = $('#input');
      box.value = (box.value ? box.value + '\n\n' : '') + `参考资料（${h.lib} · ${h.path} ${h.loc}）：\n${h.text}\n\n`;
      emit('switch-mod', 'chat'); box.focus(); box.dispatchEvent(new Event('input'));
      return;
    }
    const mo = t.closest('[data-more]');
    if (mo) { const card = mo.closest('.hit'); card.classList.toggle('open'); mo.textContent = card.classList.contains('open') ? '收起' : '展开'; }
  });
  // 拖文件到子库（规范层拖到“规范”框里）
  const body = $('#kb-body');
  body.addEventListener('dragover', e => {
    const l = e.target.closest('[data-lib]'); if (!l) return;
    e.preventDefault(); document.querySelectorAll('.lib.drop,.layer.drop').forEach(x => x.classList.remove('drop')); l.classList.add('drop');
  });
  body.addEventListener('dragleave', e => { const l = e.target.closest('[data-lib]'); if (l && !l.contains(e.relatedTarget)) l.classList.remove('drop'); });
  body.addEventListener('drop', e => {
    const l = e.target.closest('[data-lib]'); if (!l) return;
    e.preventDefault(); l.classList.remove('drop');
    if (e.dataTransfer.files.length) upload(l.dataset.lib, e.dataTransfer.files);
  });
  on('active-file', p => { viewFile = p && !/[\\/]$/.test(p) ? p : null; if (active) render(); });
  on('mod', m => { active = m === 'kb'; if (active) refresh().catch(e => toast(e.message)); });
}

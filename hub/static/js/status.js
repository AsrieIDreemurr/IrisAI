// 状态栏：显卡上的模型、显存、内存常驻模型、沙箱。每 4 秒刷新一次。
import { $, api, esc, on, settings, toast } from './api.js';

// 模型的短名字、在不在 CPU 上：来自模型清单（/api/config 的 models）
const meta = id => (settings.config?.models || []).find(m => m.id === id);
const onCpu = m => meta(m.id)?.device === 'cpu';
const kv = (a, b) => `<div class="kv"><span>${a}</span><span>${b}</span></div>`;
const mins = s => s >= 60 ? Math.round(s / 60) + ' 分钟后' : s + ' 秒后';
let timer;

// 网页服务连不上（在重启、断网）：状态栏左边显示红点和“网页服务连不上”，连上后下一次刷新自动恢复
function offline() {
  $('#st-model').textContent = '网页服务连不上（可能在重启）';
  $('#st-dot').style.background = '#e5534b';
}

async function tick() {
  let s;
  try { s = await api('/api/status', { quiet: true }); } catch (e) { if (e.netDown) offline(); return; }
  const gpuModels = s.models.filter(m => !onCpu(m));
  const cpuModels = s.models.filter(onCpu);
  const g = gpuModels[0];
  const name = m => meta(m.id)?.name || m.name;
  $('#st-model').textContent = !s.ok ? '调度器未连接' : g ? name(g) + (g.state && g.state !== 'ready' ? '（加载中）' : '') : '显卡空闲';
  $('#st-dot').style.background = !s.ok ? '#e5534b' : '#fff';
  if (s.gpu) {
    $('#st-vram').textContent = `显存 ${s.gpu.used.toFixed(1)} / ${s.gpu.total.toFixed(0)} GB`;
    $('#st-vram-bar').style.width = (100 * s.gpu.used / s.gpu.total).toFixed(0) + '%';
  }
  $('#st-cpu').textContent = `内存常驻 ${cpuModels.length}`;
  $('#st-unload').textContent = g?.unload_in != null ? '约 ' + mins(g.unload_in) + '卸载' : '';
  $('#pop-gpu-body').innerHTML = (g ? kv('模型', esc(name(g))) : kv('模型', '无')) +
    (s.gpu ? `<div class="meter" style="margin:6px 0 4px"><b style="width:${(100 * s.gpu.used / s.gpu.total).toFixed(0)}%"></b></div>` +
      kv('显存', `${s.gpu.used.toFixed(1)} / ${s.gpu.total.toFixed(1)} GB`) + kv('显卡占用率', s.gpu.util + '%') : '') +
    (g?.unload_in != null ? kv('自动卸载', '约 ' + mins(g.unload_in)) : '') +
    `<div class="kv" style="color:var(--muted);font-size:12px">显存包括其他程序（浏览器、壁纸等）的占用。</div>`;
  $('#pop-cpu-body').innerHTML = (cpuModels.length ? cpuModels.map(m => kv(esc(name(m)), m.state === 'ready' ? '已加载' : esc(m.state || ''))).join('')
    : '<div class="kv"><span>还没有加载，第一次用到时自动加载</span></div>') + kv('内存', `${s.ram.used.toFixed(1)} / ${s.ram.total.toFixed(0)} GB`);
  const dirs = settings.data.allowed_dirs || [];
  $('#pop-sb-body').innerHTML = kv('位置', esc(s.sandbox)) + kv('授权目录', dirs.length ? dirs.length + ' 个' : '无') +
    '<div class="kv" style="color:var(--muted);font-size:12px">沙箱内 Agent 可以随意读写；沙箱外的操作会先问你。</div>';
}

export function initStatus() {
  $('#btn-unload').onclick = async () => {
    const r = await api('/api/unload', { method: 'POST' }); toast(r.ok ? '已卸载全部模型。' : '卸载失败。'); tick();
  };
  on('status-refresh', tick);
  on('net', d => { if (d.down) offline(); else tick(); });
  tick(); clearInterval(timer); timer = setInterval(tick, 4000);
}

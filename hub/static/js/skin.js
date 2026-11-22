// 主题：根元素的 data-skin 切换配色和装饰图形（样式在 /s/skins/<主题>/skin.css）。
// 主题文件夹的“素材”里放了图就自动用上：根元素加 data-skin-<位置>，图片地址放在 --skin-<位置> 变量里，
// 带 data-skin-img="<位置>" 的 <img> 也会换上这张图。位置：art 立绘、banner 横幅、mascot 看板、bg 背景
import { api, on } from './api.js';

export const SLOTS = ['art', 'banner', 'mascot', 'bg'];
let list = null;

// 看板娘的心情：根元素 data-mood = think（Agent 在跑）/ error / done / idle（很久没动静），没有就是默认表情。
// 主题样式按它换看板图（见 skins/koishi/skin.css）；没有心情图的主题不受影响
let moodTimer = 0, idleTimer = 0;
const IDLE_MS = 10 * 60e3;
export function setMood(mood = '', ms = 0) {
  const r = document.documentElement;
  clearTimeout(moodTimer); clearTimeout(idleTimer);
  if (mood) r.dataset.mood = mood; else delete r.dataset.mood;
  if (ms) moodTimer = setTimeout(() => setMood(), ms);         // 出错、完成过一会儿回到默认
  if (!mood || ms) idleTimer = setTimeout(() => { if (!r.dataset.mood) r.dataset.mood = 'idle'; }, (ms || 0) + IDLE_MS);
}
on('agent-mood', d => setMood(d?.mood, d?.ms));
setMood();

export async function skins() {
  if (!list) list = await api('/api/skins').catch(() => []);
  return list;
}

// 返回当前主题（默认主题返回 null）
export async function applySkin(id) {
  const r = document.documentElement;
  const preview = new URLSearchParams(location.search).get('skin');   // 网址加 ?skin=koishi 只预览，不改设置
  if (preview) id = preview;
  const s = id && id !== 'default' ? (await skins()).find(x => x.id === id) : null;
  r.dataset.skin = s ? s.id : 'default';
  let link = document.getElementById('skin-css');
  if (s) {
    if (!link) { link = document.createElement('link'); link.rel = 'stylesheet'; link.id = 'skin-css'; document.head.appendChild(link); }
    if (link.getAttribute('href') !== s.css) link.setAttribute('href', s.css);
  } else link?.remove();
  for (const k of SLOTS) {
    const u = s?.files?.[k];
    r.toggleAttribute('data-skin-' + k, !!u);
    if (u) r.style.setProperty('--skin-' + k, `url("${u}")`); else r.style.removeProperty('--skin-' + k);
    document.querySelectorAll(`img[data-skin-img="${k}"]`).forEach(img => { if (u) img.src = u; else img.removeAttribute('src'); });
  }
  try { localStorage.setItem('skin', s ? s.id : 'default'); } catch (e) {}
  setIcon(s);
  return s;
}

// 应用图标（标签页小图标）：主题文件夹里有 icon.png 就用它（古明地恋主题是恋之瞳），没有就用默认的 IrisAI 虹膜。
// 桌面程序的窗口、托盘图标由 scripts/desktop_app.py 按同样的规则选（看 icon.ico）
const DEFAULT_ICON = '/s/img/app.png';
function setIcon(s) {
  const put = u => {
    let l = document.querySelector('link[rel="icon"]');
    if (!l) { l = document.createElement('link'); l.rel = 'icon'; document.head.appendChild(l); }
    if (l.getAttribute('href') !== u) l.setAttribute('href', u);
  };
  if (!s) return put(DEFAULT_ICON);
  const u = `/s/skins/${encodeURIComponent(s.id)}/icon.png`;
  fetch(u, { method: 'HEAD' }).then(r => put(r.ok ? u : DEFAULT_ICON)).catch(() => put(DEFAULT_ICON));
}

// 设置页：外观（强调色 / 圆角 / 字号 / 密度 / 深浅色）、装饰（花饰 / 看板图）、Agent 自改。保存在服务器，局域网里其他设备打开也一样。
import { emit, on, saveSettings, settings, toast } from './api.js';
import { confirmBox } from './ui.js';
import { applySkin, skins } from './skin.js';
import { initPet, loadFaces, loadLines, tellDesktop, deskSurfaces, setPetFaces, setPetFrames } from './pet.js';
import { openAsk, placeAsk } from './petchat.js';
import { STORAGE_CARD, initStorage } from './storage.js';
import { MODELS_CARD, initModels } from './models.js';

const DEFAULTS = { accent: '#5b8def', radius: 10, fs: 14, density: 'normal', mode: 'system', deco: 'on', mascot: 'on', mascotSrc: '', skin: 'default', petMode: 'web', lang: 'zh' };   // petMode：桌宠在网页里 / 桌面上
const root = document.documentElement;
let look = {};
let saveTimer;

function apply() {
  root.style.setProperty('--accent', look.accent);
  root.style.setProperty('--radius', look.radius + 'px');
  root.style.setProperty('--fs', look.fs + 'px');
  root.style.setProperty('--gap', { compact: .75, normal: 1, comfy: 1.25 }[look.density] || 1);
  root.dataset.theme = look.mode;
  root.dataset.deco = look.deco;
  try { localStorage.setItem('look', JSON.stringify({ mode: look.mode, skin: look.skin, accent: look.accent, radius: look.radius, deco: look.deco })); } catch (e) {}
  try { localStorage.removeItem('deco-v'); } catch (e) {}   // 清掉方案预览时记下的选择（2026-11-07 已定稿为 A）
  delete root.dataset.decoV;
  applySkin(look.skin).then(s => {
    // 默认主题不显示看板图（css/app.css 用 img/app.png 的剪影代替），这里给个占位地址只是让查看区的剪影照常出现；
    // 以前占位用的 img/mascot.png（AI 生成的图）2026-11-14 已删除
    const src = look.mascotSrc || (s ? s.files.mascot : '/s/img/app.png');
    // 桌宠放到桌面上时，网页里按看板图关闭处理（不放桌宠）；告诉桌面程序现在的模式和主题
    root.dataset.mascot = look.mascot === 'on' && src && look.petMode !== 'desktop' ? 'on' : 'off';
    tellDesktop(look.mascot === 'on' && !look.mascotSrc ? look.petMode : 'web', look.skin);
    root.toggleAttribute('data-mascot-own', !!look.mascotSrc);   // 自己上传的看板图：主题不按心情换表情
    if (src) document.querySelectorAll('img[data-mascot-img], img[data-pet-img]').forEach(img => { img.src = src; });
    if (s?.files?.mascot && root.dataset.mascot === 'on') {
      if (!petStarted) startPet(s, src);
      else {                                  // 桌宠已经在跑：换成新主题的帧（自己上传的看板图就不用帧）
        document.querySelector('img[data-pet-img]').src = src;
        loadFaces(look.mascotSrc ? null : s.css.replace(/skin\.css.*$/, ''));
        loadLines(look.mascotSrc ? null : s.persona);   // 台词用主题角色的人格台词库
      }
    } else if (petStarted) { loadFaces(null); loadLines(null); }   // 换到没有看板的主题：桌宠藏起来（data-skin-mascot 没了），帧也清掉
  });
}

// 主题有看板图时，工作台里也放桌宠（js/pet.js）：站在底部状态栏上，可以拖；
// 原位在左下角、侧栏（资源管理器）底下居中，侧栏收起时贴着左边（2026-11-07 用户要求，原来在右下角）。
// 工作台不能双击回原位（入口页还能）。位置记在本机的 pet-app2 里；换了原位，旧的 pet-app 清掉，大家都从新原位开始
let petStarted = false;
// 桌面桌宠也能站在对话页的输入框上（桌面模式时 pet.js 把位置报给桌面程序）
deskSurfaces(() => [...document.querySelectorAll('.composer .box')].filter(b => b.offsetParent).map((b, i) => {
  const r = b.getBoundingClientRect(); return { id: 'composer' + i, left: r.left, right: r.right, top: r.top };
}));
function startPet(s, src) {
  if (petStarted) return;
  petStarted = true;
  try { localStorage.removeItem('pet-app'); } catch (e) {}
  const bar = () => document.getElementById('statusbar').getBoundingClientRect().top;
  initPet({
    key: 'pet-app2', top: 40, typeInput: '#input', dblHome: false, wander: true,   // 闲时自己溜达
    onClick: openAsk, onMove: placeAsk,   // 单击弹出随手问气泡，拖动时气泡跟着走
    homeX: () => {
      const w = document.getElementById('p-petbox')?.offsetWidth || 160;
      const sb = document.querySelector('.sidebar')?.getBoundingClientRect();
      return sb && sb.width > w && getComputedStyle(document.querySelector('.sidebar')).visibility !== 'hidden'
        ? sb.left + (sb.width - w) / 2 : 56;
    },
    floorY: h => bar() - h + 6,                // 脚踩在状态栏上
    // 台面：对话页的输入框上沿。她落在输入框那一段时站在框上，不挡着输入
    platforms: () => [...document.querySelectorAll('.composer .box')].filter(b => b.offsetParent).map(b => b.getBoundingClientRect()),
  });
  document.querySelector('img[data-pet-img]').src = src;
  if (!look.mascotSrc) { loadFaces(s.css.replace(/skin\.css.*$/, '')); loadLines(s.persona); }
}

function save() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => saveSettings({ look }).catch(() => {}), 400);
}

// 桌面程序托盘菜单切换“桌宠在桌面上”时调（desktop_app.py 用 evaluate_js）
window.irisPetMode = mode => {
  look.petMode = mode === 'desktop' ? 'desktop' : 'web';
  const sel = document.querySelector('#set-body [data-k="petMode"]'); if (sel) sel.value = look.petMode;
  apply(); save();
};

export function initLook() {
  look = { ...DEFAULTS, ...(settings.data.look || {}) };
  if (window.I18N && I18N.lang !== look.lang) { I18N.set(look.lang); return; }   // 别的设备改过语言：跟着换（会刷新页面）
  setPetFaces(settings.data.pet_faces); setPetFrames(settings.data.pet_frames);       // 桌宠表情搭配（入口页“调整布局 → 表情… → 桌宠”里改）
  const panel = document.getElementById('set-body');
  const row = (name, desc, ctl) => `<div class="set-row"><div class="set-lb"><b>${name}</b>${desc ? `<small>${desc}</small>` : ''}</div><div class="set-ctl">${ctl}</div></div>`;
  panel.innerHTML = `
    <section class="set-card"><div class="set-h">外观</div>
      ${row('主题', '配色和装饰图形；主题文件夹的“素材”里放了图会自动用上', '<select data-k="skin"><option value="default">默认</option></select>')}
      ${row('强调色', '按钮、选中项、开关打开时的颜色', '<input type="color" data-k="accent">')}
      ${row('圆角', '整个页面的圆角大小，0 是直角', '<input type="range" min="0" max="16" data-k="radius"><span class="set-val" data-v="radius"></span>')}
      ${row('字号', '', '<input type="range" min="12" max="18" data-k="fs"><span class="set-val" data-v="fs"></span>')}
      ${row('密度', '对话、列表里的上下间距', '<select data-k="density"><option value="compact">紧凑</option><option value="normal">标准</option><option value="comfy">宽松</option></select>')}
      ${row('语言', '界面文字的语言；英文时 Agent 默认也用英文回答', '<select data-k="lang"><option value="zh">中文</option><option value="en">English</option></select>')}
      ${row('深浅', '', '<select data-k="mode"><option value="system">跟随系统</option><option value="light">浅色</option><option value="dark">深色</option></select>')}
    </section>
    <section class="set-card"><div class="set-h">装饰</div>
      ${row('装饰图形', '活动栏、侧栏、入口页上的小图形和动画', '<select data-k="deco"><option value="on">显示</option><option value="off">隐藏</option></select>')}
      ${row('看板图', '侧栏下方和右边空白处的图', '<select data-k="mascot"><option value="on">显示</option><option value="off">隐藏</option></select>')}
      ${row('桌宠', '网页里：在页面里走动、点她聊天。桌面上：跳出网页站在 Windows 任务栏上（要用 IrisAI 桌面程序打开），网页里不再显示', '<select data-k="petMode"><option value="web">网页里</option><option value="desktop">桌面上</option></select>')}
      ${row('换一张看板图', '会缩到 512 像素以内再保存', '<label class="btn sm set-file">选择图片<input type="file" accept="image/*" data-file hidden></label>')}
    </section>
    <section class="set-card"><div class="set-h">Agent 自改</div>
      ${row('启用自改进', '允许 Agent 修改规范，并在副本中改进自己的界面和流程；关闭后停止自改操作', '<span class="chip" data-se-enabled role="switch" tabindex="0">关</span>')}
      ${row('高权限', '开启自改进后可用：允许修改副本中的所有内容，包括自改机制、提示词、密钥配置、登录和权限检查；通过检查后自动应用并备份，可撤回', '<span class="chip" data-se-full role="switch" tabindex="0">关</span>')}
    </section>
    ${MODELS_CARD}
    ${STORAGE_CARD}
    <div class="set-foot"><button type="button" class="btn cust-reset">外观和装饰恢复默认</button></div>`;
  initStorage(panel);
  initModels(panel);
  const se = panel.querySelector('[data-se-full]');
  const seEnabled = panel.querySelector('[data-se-enabled]');
  // 圆点开关模拟勾选框的 checked / disabled，下面原来的逻辑照用
  for (const el of [se, seEnabled]) {
    Object.defineProperty(el, 'checked', { get() { return this.classList.contains('on'); },
      set(v) { this.classList.toggle('on', !!v); this.textContent = v ? '开' : '关'; } });
    Object.defineProperty(el, 'disabled', { get() { return this.classList.contains('off'); }, set(v) { this.classList.toggle('off', !!v); } });
    const flip = () => { if (el.disabled) return; el.checked = !el.checked; el.dispatchEvent(new Event('change')); };
    el.addEventListener('click', flip);
    el.addEventListener('keydown', e => { if (e.key === ' ' || e.key === 'Enter') { e.preventDefault(); flip(); } });
  }
  const syncSelfEdit = () => {
    seEnabled.checked = settings.data.self_edit_enabled === true;
    se.checked = seEnabled.checked && settings.data.self_edit_full === true;
    seEnabled.disabled = false;
    se.disabled = !seEnabled.checked;
  };
  const saveSelfEdit = async patch => {
    seEnabled.disabled = se.disabled = true;
    try { await saveSettings(patch); emit('selfedit-settings'); }
    catch (e) { toast(e.message); }
    finally { syncSelfEdit(); }
  };
  seEnabled.addEventListener('change', () => saveSelfEdit({ self_edit_enabled: seEnabled.checked,
    ...(!seEnabled.checked ? { self_edit_full: false } : {}) }));
  se.addEventListener('change', async () => {
    if (se.checked && !await confirmBox('打开高权限？', 'Agent 可以修改副本中的所有内容，包括自改机制、提示词、密钥配置、登录和权限检查。通过检查后自动应用并备份，可以撤回。', { ok: '打开' })) { syncSelfEdit(); return; }
    saveSelfEdit({ self_edit_full: se.checked });
  });
  syncSelfEdit();
  on('selfedit-sync', syncSelfEdit);          // 服务端的值和页面里的不一样了（见 selfedit.js），开关跟着变
  const vals =() => panel.querySelectorAll('[data-v]').forEach(el => { el.textContent = look[el.dataset.v] + ' 像素'; });
  const sync = () => { panel.querySelectorAll('[data-k]').forEach(el => { el.value = look[el.dataset.k]; }); vals(); };
  const onInput = e => {
    const k = e.target.dataset.k; if (!k) return;
    look[k] = e.target.type === 'range' ? +e.target.value : e.target.value;
    if (k === 'lang') { saveSettings({ look }).catch(() => {}).finally(() => window.I18N?.set(look.lang)); return; }
    if (k === 'skin') {           // 换主题时强调色换成主题建议的颜色，之后可以再改
      const s = (list || []).find(x => x.id === look.skin);
      look.accent = s?.accent || DEFAULTS.accent; sync();
    }
    apply(); save(); vals();
  };
  panel.addEventListener('input', onInput);
  panel.addEventListener('change', e => { if (e.target.tagName === 'SELECT') onInput(e); });
  panel.querySelector('[data-file]').addEventListener('change', e => {
    const f = e.target.files[0]; if (!f) return;
    // 缩到 512 像素以内再存，避免设置文件太大
    const img = new Image();
    img.onload = () => {
      const s = Math.min(1, 512 / Math.max(img.width, img.height));
      const c = document.createElement('canvas'); c.width = img.width * s; c.height = img.height * s;
      c.getContext('2d').drawImage(img, 0, 0, c.width, c.height);
      look.mascotSrc = c.toDataURL('image/jpeg', .88); look.mascot = 'on'; sync(); apply(); save();
    };
    img.src = URL.createObjectURL(f);
  });
  panel.querySelector('.cust-reset').onclick = () => { look = { ...DEFAULTS, lang: look.lang }; sync(); apply(); save(); };
  let list = null;
  skins().then(l => {
    list = l;
    panel.querySelector('[data-k="skin"]').insertAdjacentHTML('beforeend', l.map(s => `<option value="${s.id}">${s.name}</option>`).join(''));
    sync();
  });
  sync(); apply();
}

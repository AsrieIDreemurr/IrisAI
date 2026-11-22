// 桌宠：入口页和工作台共用（样式在 css/pet.css）。只在主题素材里有“看板”时出现（根元素 data-skin-mascot）。
// - 可以拖，松手受重力掉到地面（页面给的 floor），落地压扁一下，摔得高会晕；单击有反应，双击走回原位。
// - 主题文件夹里有 素材/桌宠.json（帧清单，scripts/export_pet_frames.py 生成）时：眨眼、回答时对口型、
//   跟着心情（根元素 data-mood：think / done / error / idle）换表情、闲久了打哈欠再睡、随机小动作、各种互动反应。
//   没有清单就是一张静态图。
// - 跟着 Agent 干活：调工具时按工具换表情、偶尔说一句；等确认时期待；答完在气泡里说回答的第一句（出错说没做完）。
// - cfg.wander 打开时（工作台）闲着会自己在她站的那一层溜达，往右走时左右翻转。
// 页面用 initPet(cfg) 接上：key 存位置的 localStorage 键，homeX() 原位，floorY(h) 地面，top 最高能拖到哪，
// onMove() 每次位置变了之后调（入口页用它排气泡），canDrag() 现在能不能拖，typeInput 打字时一脸期待的输入框。
import { setMood } from './skin.js';
import { api, on } from './api.js';

const $ = s => document.querySelector(s);
const root = document.documentElement;
export const calm = () => matchMedia('(prefers-reduced-motion: reduce)').matches;
// ---------------------------------------------------------------- 桌面模式
// 设置里“桌宠”选了“桌面上”时，网页里不放桌宠（按看板图关闭处理），由 IrisAI 桌面程序里的桌宠（scripts/desktop_pet.py）出场。
// 这里把 Agent 的动静（调工具、说话、表情、心情、答完）转给桌面程序：window.pywebview.api.pet_event，只在桌面程序里有。
// 说话的字攒 150 毫秒一起发，免得一个字调一次。src 区分是哪个页面发的（工作台、入口页、聊天小窗各算各的心情）
const SRC = Math.random().toString(36).slice(2, 8);
const desk = () => window.pywebview?.api;
let talkBuf = '', talkTimer = 0;
function send(ev) { try { desk()?.pet_event?.({ ...ev, src: SRC })?.catch?.(() => {}); } catch (e) {} }
function link(ev) {
  if (!desk()?.pet_event) return;
  if (ev.type === 'talk') {
    talkBuf += ev.text;
    if (!talkTimer) talkTimer = setTimeout(() => { talkTimer = 0; const t = talkBuf; talkBuf = ''; send({ type: 'talk', text: t }); }, 150);
    return;
  }
  if (talkTimer) { clearTimeout(talkTimer); talkTimer = 0; send({ type: 'talk', text: talkBuf }); talkBuf = ''; }
  send(ev);
}
let lastMood = null;
new MutationObserver(() => {
  const m = root.dataset.mood || '';
  if (m !== lastMood) { lastMood = m; link({ type: 'mood', mood: m }); }
}).observe(root, { attributes: true, attributeFilter: ['data-mood'] });
// 页面告诉桌面程序：桌宠放在哪（web / desktop）、现在是哪个主题（换主题时桌面桌宠跟着换角色）
let deskMode = null;
export function tellDesktop(mode, skin) {
  deskMode = [mode || 'web', skin || 'default'];
  try { desk()?.pet_mode?.(...deskMode)?.catch?.(() => {}); } catch (e) {}
}
addEventListener('pywebviewready', () => { if (deskMode) tellDesktop(...deskMode); });

// 桌面模式：页面上桌面桌宠能站的地方（对话页的输入框）报给桌面程序（desktop_app.py 换成屏幕坐标），
// 有变化才报；页面藏起来时报空。只有主窗口有 pet_surfaces，聊天小窗、普通浏览器里什么都不做
let surfFn = null, surfLast = '';
export function deskSurfaces(fn) { surfFn = fn; }
setInterval(() => {
  if (deskMode?.[0] !== 'desktop' || !desk()?.pet_surfaces) return;
  const list = document.hidden || !surfFn ? [] : surfFn().map((r, i) => ({ id: r.id || 's' + i, l: Math.round(r.left), r: Math.round(r.right), t: Math.round(r.top) }));
  const s = JSON.stringify([list, devicePixelRatio]);
  if (s === surfLast) return;
  surfLast = s;
  try { desk().pet_surfaces(list, devicePixelRatio)?.catch?.(() => {}); } catch (e) {}
}, 250);

export const petOn = () => root.hasAttribute('data-skin-mascot') && root.dataset.mascot === 'on';
export const pet = { x: 0, y: 0, falling: 0 };
let cfg = { key: 'pet', homeX: () => 34, floorY: h => innerHeight - h - 4, top: 40, onMove: () => {}, canDrag: () => true, typeInput: null, platforms: () => [], dblHome: true, onClick: null };   // dblHome：双击回到原位（工作台关掉）；onClick：单击时页面要做的事

const load = () => { try { return JSON.parse(localStorage.getItem(cfg.key) || '{}'); } catch (e) { return {}; } };
const save = p => { try { localStorage.setItem(cfg.key, JSON.stringify({ ...load(), ...p })); } catch (e) {} };
export const petBox = () => $('#p-petbox');
const floor = () => cfg.floorY(petBox().offsetHeight);

// 落脚点：从 fromY（她现在的上沿）往下，她脚下最近的台面。台面由页面给（cfg.platforms：[{left, right, top}]，
// 比如工作台输入框的上沿，她就站在输入框上，不挡着它）；都没有就是地面
function landAt(fromY) {
  const box = petBox(), h = box.offsetHeight, cx = pet.x + box.offsetWidth / 2;
  let y = floor();
  for (const p of cfg.platforms()) {
    if (cx < p.left + 12 || cx > p.right - 12) continue;
    const stand = p.top - h + 4;
    if (stand >= fromY - 2 && stand < y) y = stand;
  }
  return y;
}

// 页面上还没有桌宠（工作台）就建一个
function ensureMarkup() {
  if (petBox()) return;
  document.body.insertAdjacentHTML('beforeend', `<div class="p-petbox" id="p-petbox">
    <span class="p-say" id="p-say" role="status"></span>
    <button type="button" class="p-pet" id="p-pet"><img data-pet-img alt="看板娘"></button></div>`);
}

// ---------------------------------------------------------------- 位置
// 把她放到 (pet.x, pet.y)（夹在窗口里），再让页面按她的位置排别的东西
export function place() {
  if (!petOn() || !cfg.canDrag()) return;
  const box = petBox(), w = box.offsetWidth;
  pet.x = Math.min(Math.max(pet.x, 0), innerWidth - w);
  pet.y = Math.min(Math.max(pet.y, cfg.top), floor());
  root.style.setProperty('--pet-x', pet.x + 'px'); root.style.setProperty('--pet-y', pet.y + 'px');
  cfg.onMove();
}

// 重力：松手后往下掉，落地弹一下；落定后记下横向位置
function fall() {
  cancelAnimationFrame(pet.falling);
  const ground = landAt(pet.y);                     // 落在脚下最近的台面上（没有台面就是地面）
  const drop = ground - pet.y;                      // 要掉多高：太矮不换表情，很高落地会晕
  hold(drop > 40 ? F('fall', 'surprise') : '');
  if (calm()) { pet.y = ground; place(); landed(drop); return; }
  let v = 0, last = performance.now(), bounced = 0, frames = 0;
  // 页面在后台时浏览器不跑逐帧动画：0.4 秒一帧都没跑，就直接落地，免得卡在半空
  setTimeout(() => { if (!frames) { cancelAnimationFrame(pet.falling); pet.y = ground; place(); landed(drop); } }, 400);
  const step = now => {
    frames++;
    const dt = Math.min((now - last) / 1000, .05); last = now;
    v += 2600 * dt; pet.y += v * dt;
    if (pet.y >= ground) {
      pet.y = ground;
      if (v > 520 && bounced < 2) { v = -v * .28; bounced++; squash(); }
      else { place(); landed(drop); return; }
    }
    place();
    pet.falling = requestAnimationFrame(step);
  };
  pet.falling = requestAnimationFrame(step);
}
function landed(drop = 0) {
  squash(); save({ pet: { x: pet.x / innerWidth } });
  hold('');
  if (drop > 320) { show(F('dizzy', 'dizzy'), 1900); say(line('dizzy'), 1600); } else draw();
}
function squash() { const b = petBox(); b.classList.remove('land'); void b.offsetWidth; b.classList.add('land'); }

// 沿地面走到 x（双击回原位、让位时用）。
// stroll：闲逛——按步速一跳一跳地走，脸朝走的方向，高度不变（不会走着走着跳上台面）
export function walkTo(x, done, stroll = false) {
  cancelAnimationFrame(pet.falling);
  const box = petBox();
  const from = pet.x, to = Math.min(Math.max(x, 0), innerWidth - box.offsetWidth), t0 = performance.now();
  const dur = calm() ? 0 : stroll ? Math.max(700, Math.abs(to - from) / 95 * 1000) : 420;
  if (!stroll) pet.y = floor();
  else { turn(to > from ? 1 : -1); box.classList.add('walk'); }
  let frames = 0;
  const finish = () => {
    box.classList.remove('walk');
    pet.x = to; if (!stroll) pet.y = landAt(-1e9); place();
    save({ pet: Math.abs(to - cfg.homeX()) < 2 ? null : { x: to / innerWidth } });
    if (!stroll) hop();
    else if (landAt(pet.y) - pet.y > 3) return fall();   // 走出了台面边：掉下去
    done?.();
  };
  setTimeout(() => { if (!frames) { cancelAnimationFrame(pet.falling); finish(); } }, 400);   // 同 fall()：后台时直接走到
  const step = now => {
    frames++;
    const k = dur ? Math.min((now - t0) / dur, 1) : 1;
    pet.x = from + (to - from) * (stroll ? k : 1 - (1 - k) ** 3);
    if (!stroll) pet.y = landAt(-1e9);               // 走过台面就站到台面上
    place();
    if (k < 1) pet.falling = requestAnimationFrame(step);
    else finish();
  };
  pet.falling = requestAnimationFrame(step);
}

// 朝向：素材是正面，往右走时左右翻过来，停下一会儿再转回正面
function turn(dir) { petBox().classList.toggle('flip', dir > 0); }

// 闲逛：普通状态下每 14–38 秒走一小段（cfg.wander 打开时，工作台用）。
// 只在她现在站的那一层走：站在台面上就在台面范围里走；站在地面上就不走进台面（输入框）底下，免得挡住输入
function wanderLoop() {
  setTimeout(() => {
    const t = wanderTarget();
    if (t != null) walkTo(t, () => {
      if (Math.random() < .3) { show(rand(STOP), 2000); if (Math.random() < .6) say(line('stroll'), 1600); }
      setTimeout(() => { if (!petBox().classList.contains('walk')) turn(-1); }, 1600 + Math.random() * 2000);
    }, true);
    wanderLoop();
  }, 14000 + Math.random() * 24000);
}
const STOP = ['hum', 'wink', 'idea', 'shy', 'wave'];
function wanderTarget() {
  if (!petOn() || !cfg.canDrag() || pet.dragging || pet.hover || pet.askOpen || document.hidden || calm()) return null;
  if (want() !== 'idle' || talking() || performance.now() - face.typed < 12000) return null;
  const box = petBox(), w = box.offsetWidth, h = box.offsetHeight, cx = pet.x + w / 2;
  let lo = 0, hi = innerWidth - w;
  const plats = cfg.platforms();
  const on = plats.find(p => cx >= p.left + 12 && cx <= p.right - 12 && Math.abs(p.top - h + 4 - pet.y) < 3);
  if (on) { lo = on.left + 12 - w / 2; hi = on.right - 12 - w / 2; }
  else if (Math.abs(pet.y - floor()) < 3) {
    for (const p of plats) {                       // 台面在她身子的高度范围里才算挡路
      if (p.bottom < pet.y || p.top > pet.y + h) continue;
      const a = p.left - w, b = p.right;           // 她的左边落在 (a, b) 里就和台面重叠
      if (pet.x <= a) hi = Math.min(hi, a);
      else if (pet.x >= b) lo = Math.max(lo, b);
      else return null;                             // 已经和台面叠着（被拖过去的），不动
    }
  } else return null;                               // 悬在半空（不该发生），不动
  if (hi - lo < 60) return null;
  const d = 90 + Math.random() * 260;
  let to = pet.x + (Math.random() < .5 ? -d : d);
  if (to < lo || to > hi) to = pet.x - (to - pet.x);   // 碰到边就往另一边走
  to = Math.min(Math.max(to, lo), hi);
  return Math.abs(to - pet.x) < 40 ? null : to;
}

// ---------------------------------------------------------------- 说话、跳、戳
// 台词：主题的角色有人格台词库（knowledge\人格\<名字>\桌宠台词.json，skin.json 的 persona 指定）就用它的，
// 缺的分类和没有台词库时用这里的通用台词。分类和后端 kb.PET_KEYS 一致
const BASE = {
  greet: { late: ['还没睡呀？'], morning: ['早上好～'], noon: ['中午好～'], afternoon: ['下午好～'], evening: ['晚上好～'] },
  poke: ['嗯？', '在呢～', '要问点什么吗？', '嘿嘿', '今天也辛苦了', '♡'], pout: ['别戳啦～', '哼'], wake: ['唔……醒了醒了'],
  held: ['要带我去哪？'], dizzy: ['晕～'], home: ['回来啦'], idle: ['嗨～', '♪～', '嘿嘿', '啊，想到了！', '加油！'],
  sleepy: ['好困……'], stroll: ['♪～'], hover: ['嗯？'], fail: ['唔……这次没做完'],
  tool: { search: ['我找找……', '查一下～', '翻翻看'], write: ['记下来～', '动笔了', '写好这个'], run: ['跑一下看看', '会成功吗……'],
          draw: ['画画中♪', '等我画完～'], translate: ['翻译中～', '认真读着呢'], delete: ['要删东西了'], error: ['唔，出错了……'] },
  wait: { ask: ['问你个事～'], confirm: ['这个要你点一下'] },
};
let own = null, ownName = '';
const rand = a => a[Math.floor(Math.random() * a.length)];
const dig = (o, path) => path.split('.').reduce((v, k) => v?.[k], o);
export const line = path => { const a = dig(own, path); return rand(a?.length ? a : dig(BASE, path) || ['']); };
// 换主题时调：name 是这个主题角色的人格名，没有就用通用台词
export async function loadLines(name) {
  if (name === ownName) return;
  ownName = name || '';
  let d = null;
  if (name) try { d = (await api('/api/personas/petlines?name=' + encodeURIComponent(name))).lines; } catch (e) { d = null; }
  if (ownName === (name || '')) own = d;
}
let sayTimer = 0;
export function say(text, ms = 2200) {
  const s = $('#p-say'); if (!s) return;
  face.said = performance.now();
  s.textContent = text; s.classList.toggle('long', text.length > 12); s.classList.add('on');
  // 气泡居中在她头上，靠近窗口边时往里挪，不出屏幕
  s.style.setProperty('--say-dx', '0px');
  const b = petBox().getBoundingClientRect(), half = s.offsetWidth / 2, c = b.left + b.width / 2;
  s.style.setProperty('--say-dx', Math.min(Math.max(c, half + 6), innerWidth - half - 6) - c + 'px');
  clearTimeout(sayTimer); sayTimer = setTimeout(() => s.classList.remove('on'), ms);
}

// ---------------------------------------------------------------- 跟着 Agent 干活
// 每调一个工具（chat.js / portal.js 转过来的 step 事件）换个表情；台词 5 秒内最多一句，免得一直冒泡
const TOOL_ACT = [
  [/^(web_search|fetch_url|search_files|read_file|list_dir|recall_memory)$/, 'think', 'search'],   // 工具、表情、台词分类
  [/^(write_file|edit_file|make_dir|move_path|remember)$/, 'idea', 'write'],
  [/^(run_python|run_js|check_file|view_result)$/, 'hope', 'run'],
  [/^generate_image$/, 'hum', 'draw'],
  [/^(translate|ocr_image)$/, 'hum', 'translate'],
  [/^delete_path$/, 'surprise', 'delete'],
];
export function petStep(ev) {
  if (ev) link({ type: 'step', tool: ev.tool || '', status: ev.status || '' });
  if (!face.frames || !ev) return;
  const now = performance.now(), quiet = now - face.said < 5000;
  if (ev.status === 'error') { show(F('tool.error', 'error'), 1600); if (!quiet) say(line('tool.error'), 1600); return; }
  if (ev.status !== 'running') return;
  const hit = TOOL_ACT.find(([re]) => re.test(ev.tool || ''));
  if (!hit) return;
  show(F('tool.' + hit[2], hit[1]), 1600);
  if (!quiet && Math.random() < .6) say(line('tool.' + hit[2]), 1700);
}
// 等用户确认 / 回答
export function petWait(kind) { link({ type: 'wait', kind }); if (!face.frames) return; show(F('wait', 'hope'), 2200); say(line(kind === 'ask' ? 'wait.ask' : 'wait.confirm'), 2400); }
// 开了人格的回答里，模型标的表情（后端 emote 事件，帧名见 kb.EMOTES）
export function petEmote(f) { link({ type: 'emote', face: f }); if (face.frames?.[f]) show(F('emote.' + f, f), 2600); }
on('agent-emote', petEmote);
on('agent-step', petStep);
on('agent-wait', d => petWait(d?.kind));

// 回答的文字：最后一轮说的话记下来，答完在气泡里说第一句
export function petRound() { link({ type: 'round' }); face.roundEnd = true; }
export function petFinal(text) { link({ type: 'final', text: text || '' }); face.buf = text || ''; }
on('agent-round', petRound);
on('agent-final', petFinal);
function gist(t) {
  // 去掉代码块、标题行（不算第一句）、行内代码和链接的记号、列表符号、加粗斜体
  t = (t || '').replace(/```[\s\S]*?```/g, ' ').replace(/^\s*#{1,6}\s.*$/gm, '')
    .replace(/`([^`]*)`/g, '$1').replace(/!?\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/^[\s#>*+|-]+|^\d+\.\s*/gm, '').replace(/[*_~]/g, '').trim();
  const m = t.match(/^[\s\S]*?[。！？!?\n]/);
  const s = (m ? m[0] : t).trim().replace(/[。\n]$/, '');
  return s.length > 34 ? s.slice(0, 33) + '…' : s;
}
// 答完：完成说第一句，出错说没做完。页面在后台时等切回来再说
function finished(mood) {
  if (pet.askOpen) return;                          // 随手问气泡开着：答案就在气泡里，不重复说
  const text = mood === 'done' ? gist(face.buf) : line('fail');
  if (!text) return;
  const go = () => say(text, mood === 'done' ? Math.min(6500, 2500 + text.length * 110) : 3000);
  if (!document.hidden) return go();
  const v = () => { removeEventListener('visibilitychange', v); setTimeout(go, 400); };
  addEventListener('visibilitychange', v);
}
export function hop() {
  const box = petBox(); if (!box) return;
  box.classList.remove('hop'); void box.offsetWidth; box.classList.add('hop');
}
function poke() {
  hop();
  const f = pokeFace();
  if (!f) say(line('poke'));
  if (f === 'pout' || f === 'surprise') return;      // 鼓腮、吓一跳的时候不冒心
  const box = petBox();
  for (let i = 0; i < 3; i++) {
    const h = document.createElement('span');
    h.className = 'p-heart'; h.textContent = '♥';
    h.style.cssText = `--hx:${30 + Math.random() * 40}%;--hd:${(Math.random() - .5) * 60}px;--hs:${11 + Math.random() * 8}px;animation-delay:${i * .12}s`;
    box.appendChild(h); setTimeout(() => h.remove(), 1500);
  }
}

// ---------------------------------------------------------------- 表情
// 哪种情况用哪一帧：入口页“调整布局 → 表情… → 桌宠”里改，存在设置的 pet_faces[主题]（桌面桌宠 desktop_pet.py 也读同一份）。
// 没改的用 def；poke、act 默认空 = 从一组里随机
export const PET_SITS = [
  ['greet', '打招呼、回到原位', 'wave'], ['think', '在回答（思考）', 'think'], ['done', '回答完', 'done'], ['error', '出错、没做完', 'error'],
  ['wait', '等你确认或回答', 'hope'], ['typing', '你在打字（网页里）', 'hope'], ['hover', '鼠标停到她身上', 'wink'],
  ['poke', '被戳', ''], ['pout', '连戳（鼓腮）', 'pout'], ['wake', '睡着时被戳醒', 'surprise'], ['yawn', '犯困、醒来', 'yawn'],
  ['act', '闲时的小动作', ''], ['held', '被拎着', 'held'], ['fall', '从高处掉下来', 'surprise'], ['dizzy', '摔晕', 'dizzy'],
  ['hop', '跳上输入框（桌面上）', 'cheer'],
  ['tool.search', '查资料、读文件', 'think'], ['tool.write', '写文件、记东西', 'idea'], ['tool.run', '运行代码', 'hope'],
  ['tool.draw', '生图', 'hum'], ['tool.translate', '翻译、识字', 'hum'], ['tool.delete', '删文件', 'surprise'], ['tool.error', '工具出错', 'error'],
  ['emote.cheer', '人格标“开心”', 'cheer'], ['emote.wink', '人格标“得意”', 'wink'], ['emote.shy', '人格标“害羞”', 'shy'],
  ['emote.pout', '人格标“不满”', 'pout'], ['emote.surprise', '人格标“惊讶”', 'surprise'], ['emote.think', '人格标“思考”', 'think'],
  ['emote.hope', '人格标“期待”', 'hope'], ['emote.hum', '人格标“愉快”', 'hum'], ['emote.idea', '人格标“灵光”', 'idea'],
  ['emote.yawn', '人格标“困”', 'yawn'], ['emote.error', '人格标“沮丧”', 'error'], ['emote.wave', '人格标“打招呼”', 'wave'],
];
let petFaces = {}, petFrames = {}, customV = '';
// 按部件改过的帧（入口页“调整布局 → 表情… → 桌宠 → 编辑表情”，存在设置的 pet_frames[主题]）：服务端按配方拼（/api/pet/frame），
// 先整张读进来换成本地网址，眨眼、对口型换帧时不用再请求。眨眼、说话、睡觉第二帧跟着改过的底帧走（和 app/petparts.py 的 DERIVE 一样）
const DERIVE = { blink1: 'idle', blink2: 'idle', talk1: 'idle', talk2: 'idle', sleep2: 'sleep1' };
export function setPetFrames(all) { petFrames = all && typeof all === 'object' ? all : {}; customFrames(); }
async function customFrames() {
  if (!face.base) return;
  const skin = root.dataset.skin || 'default', dir = face.dir, my = petFrames[skin] || {};
  const v = JSON.stringify([skin, my]);
  if (v === customV) return;
  customV = v;
  const names = new Set(Object.keys(my));
  for (const [n, b] of Object.entries(DERIVE)) if (my[b] && face.base[n]) names.add(n);
  const got = { ...face.base };
  await Promise.all([...names].map(async n => {
    try {
      const r = await fetch(`/api/pet/frame/${encodeURIComponent(skin)}/${encodeURIComponent(n)}.png?v=${encodeURIComponent(v.length + ':' + Date.now())}`);
      if (r.ok) got[n] = URL.createObjectURL(await r.blob());
    } catch (e) {}
  }));
  if (customV !== v || face.dir !== dir) return;         // 等的时候又改了
  for (const [k, u] of Object.entries(face.frames || {})) if (u.startsWith('blob:') && got[k] !== u) URL.revokeObjectURL(u);
  face.frames = got; face.cur = ''; draw();
}
export function setPetFaces(all) { petFaces = all && typeof all === 'object' ? all : {}; draw(); }
const F = (k, d) => { const v = petFaces[root.dataset.skin || 'default']?.[k]; return v && face.frames?.[v] ? v : d; };
// 预览：入口页面板里选帧时，网页桌宠和桌面桌宠都先换上看看
export function previewFace(f) { link({ type: 'preview', face: f }); if (face.frames?.[f]) show(f, 2400); }
const face = { frames: null, cur: '', hold: '', temp: '', tempUntil: 0, talkUntil: 0, pokes: [], sleep: 'sleep1', hoverCool: 0, typeCool: 0,
  typed: -1e9, said: -1e9, buf: '', roundEnd: false, mood: '' };
const MOOD_FACE = { think: 'think', done: 'done', error: 'error' };
const wait = ms => new Promise(r => setTimeout(r, ms));
const moodNow = () => root.dataset.mood || '';

// 换主题时再调一次：dir 变了就换成新主题的帧；dir 为空或新主题没有 桌宠.json 就清掉（保持看板静图）。
// 以前只认第一次（if (face.frames) return），切主题后桌宠还是旧主题的帧，眨一下眼又换回旧角色（2026-11-08 修）
let loopsOn = false;
export async function loadFaces(dir) {
  if (dir === face.dir) return;
  face.dir = dir;
  let frames = null;
  if (dir) {
    try {
      const m = await (await fetch(dir + '素材/桌宠.json')).json();
      frames = Object.fromEntries(Object.entries(m.frames).map(([k, v]) => [k, dir + '素材/' + v]));
    } catch (e) { frames = null; }
  }
  if (face.dir !== dir) return;              // 等待期间又换了主题
  face.base = frames; face.frames = frames && { ...frames }; face.cur = ''; customV = '';
  if (!frames) return;
  customFrames();
  Object.values(face.frames).forEach(u => { const i = new Image(); i.src = u; });    // 先全载进来，换帧不闪
  draw();
  if (loopsOn) return;                        // 眨眼、发呆、对口型这些循环只开一次，换主题不重复开
  loopsOn = true;
  blinkLoop(); idleLoop(); talkLoop();
  setInterval(() => { face.sleep = face.sleep === 'sleep1' ? 'sleep2' : 'sleep1'; if (moodNow() === 'idle') draw(); }, 1800);   // ZZZ 和鼻涕泡交替
  new MutationObserver(() => {
    const m = moodNow(), was = face.mood; face.mood = m;
    if (m === 'think' && was !== 'think') { face.buf = ''; face.roundEnd = false; }   // 新一次运行
    if ((m === 'done' || m === 'error') && was === 'think') finished(m);
    if (m === 'idle') show(F('yawn', 'yawn'), 2600);
    draw();
  })
    .observe(root, { attributes: true, attributeFilter: ['data-mood'] });
  greet();
}

// 该显示哪一帧：拎着 / 摔着 > 临时表情 > 心情（思考、完成、出错、睡觉） > 普通
function want() {
  if (face.hold) return face.hold;
  if (face.temp && performance.now() < face.tempUntil) return face.temp;
  const m = MOOD_FACE[moodNow()];
  return moodNow() === 'idle' ? face.sleep : m ? F(m, m) : 'idle';
}
function draw(name = want()) {
  if (!face.frames) return;
  const u = face.frames[name] || face.frames.idle, img = $('#p-pet img');
  if (img && face.cur !== u) { face.cur = u; img.src = u; }
}
export function show(name, ms = 1800) {
  if (!face.frames) return;
  face.temp = name; face.tempUntil = performance.now() + ms; draw();
  clearTimeout(face.tt); face.tt = setTimeout(() => draw(), ms + 20);
}
function hold(name) { face.hold = name; draw(); }
const talking = () => performance.now() < face.talkUntil && moodNow() !== 'done' && moodNow() !== 'error';   // 答完就闭嘴

// 眨眼：普通表情时每 2.5–6.5 秒眨一次，五分之一的时候连眨两下
function blinkLoop() {
  setTimeout(async () => {
    if (want() === 'idle' && !talking()) {
      for (let n = Math.random() < .2 ? 2 : 1; n > 0; n--) {
        for (const [f, ms] of [['blink1', 55], ['blink2', 85], ['blink1', 55]]) { if (want() !== 'idle') break; draw(f); await wait(ms); }
        draw(); if (n > 1) await wait(140);
      }
    }
    blinkLoop();
  }, 2500 + Math.random() * 4000);
}

// 对口型：回答一个个字冒出来时嘴一张一合，停下 0.35 秒后合上。入口页直接调，工作台的对话发 agent-talk 事件
export function petTalk(text) {
  link({ type: 'talk', text: typeof text === 'string' ? text : '' });
  face.talkUntil = performance.now() + 350;
  if (typeof text !== 'string') return;
  if (face.roundEnd) { face.buf = ''; face.roundEnd = false; }   // 新一轮开口：只留最后一轮说的话
  face.buf += text;
}
on('agent-talk', petTalk);
function talkLoop() {
  let k = 0;
  setInterval(() => {
    if (talking() && !face.hold && !(face.temp && performance.now() < face.tempUntil)) draw(['talk1', 'idle', 'talk2', 'idle'][k++ % 4]);
    else if (k) { k = 0; draw(); }
  }, 110);
}

// 随机小动作：普通状态下每 20–45 秒来一个，偶尔配一句话；半夜更爱打哈欠
const ACTS = ['wave', 'hum', 'wink', 'idea', 'shy', 'cheer', 'hope'];
function idleLoop() {
  setTimeout(() => {
    if (want() === 'idle' && !talking() && !document.hidden) {
      const late = new Date().getHours() < 6 && Math.random() < .5;
      const f = late ? F('yawn', 'yawn') : F('act', '') || rand(ACTS), text = line(late ? 'sleepy' : 'idle');
      show(f, 2400);
      if (Math.random() < .45) say(text, 2000);
      if (f === 'wave' || f === 'cheer') hop();
    }
    idleLoop();
  }, 20000 + Math.random() * 25000);
}

function greet() {
  const h = new Date().getHours();
  const k = h < 5 ? 'late' : h < 11 ? 'morning' : h < 13 ? 'noon' : h < 18 ? 'afternoon' : 'evening';
  setTimeout(() => { show(F('greet', 'wave'), 2400); say(line('greet.' + k)); hop(); }, 900);   // 等台词库读进来
}

// 戳她的反应。睡着时戳醒；2.5 秒内戳 4 下就鼓腮。返回 false 表示没有表情帧，用原来的随机话
const POKE = ['surprise', 'wink', 'shy', 'cheer', 'wave', 'hope'];
function pokeFace() {
  if (!face.frames) return false;
  const now = performance.now();
  face.pokes = face.pokes.filter(t => now - t < 2500); face.pokes.push(now);
  if (moodNow() === 'idle') { setMood(); show(F('wake', 'surprise'), 1600); say(line('wake')); return 'surprise'; }
  // 正在鼓腮：再戳也继续鼓腮，时间往后延
  const P = F('pout', 'pout');
  if (face.temp === P && now < face.tempUntil) { show(P, 2400); say(line('pout')); return 'pout'; }
  if (face.pokes.length >= 4) { face.pokes = []; show(P, 2400); say(line('pout')); return 'pout'; }
  const f = F('poke', '') || rand(POKE);
  show(f, 1800); say(line('poke')); return f;
}

// ---------------------------------------------------------------- 接上页面
// 拖动：按下后移动超过 4px 才算拖，不然算单击；双击走回原位
export function initPet(c = {}) {
  cfg = { ...cfg, ...c };
  ensureMarkup();
  root.setAttribute('data-pet-on', '');
  const box = petBox(), btn = $('#p-pet');
  let drag = null, clickTimer = 0, justDragged = false;
  btn.addEventListener('pointerdown', e => {
    if (e.button !== 0 || !cfg.canDrag()) return;
    cancelAnimationFrame(pet.falling); box.classList.remove('walk'); pet.dragging = true;
    drag = { id: e.pointerId, sx: e.clientX, sy: e.clientY, ox: e.clientX - pet.x, oy: e.clientY - pet.y, moved: false };
    btn.setPointerCapture(e.pointerId);
  });
  btn.addEventListener('pointermove', e => {
    if (!drag || e.pointerId !== drag.id) return;
    if (!drag.moved && Math.hypot(e.clientX - drag.sx, e.clientY - drag.sy) < 4) return;
    if (!drag.moved) { drag.moved = true; box.classList.add('held'); hold(F('held', 'held')); say(line('held'), 1600); }
    pet.x = e.clientX - drag.ox; pet.y = e.clientY - drag.oy;
    place();
  });
  const end = e => {
    if (!drag || e.pointerId !== drag.id) return;
    justDragged = drag.moved;
    const moved = drag.moved; drag = null; pet.dragging = false;
    box.classList.remove('held');
    if (moved) fall();
  };
  btn.addEventListener('pointerup', end);
  btn.addEventListener('pointercancel', end);
  // 拖完松手浏览器还会补一个 click，用“刚拖过”过滤；单击等一下，确认不是双击再反应
  btn.addEventListener('click', e => {
    if (e.detail > 1 || justDragged) { justDragged = false; return; }
    clearTimeout(clickTimer);
    cfg.onClick?.();                                 // 工作台：弹出随手问气泡（js/petchat.js）
    if (!cfg.dblHome) return poke();                 // 没有双击回家时不用等，单击马上反应
    clickTimer = setTimeout(poke, 220);
  });
  btn.addEventListener('dblclick', () => { if (!cfg.dblHome) return; clearTimeout(clickTimer); walkTo(cfg.homeX(), () => { show(F('greet', 'wave'), 1800); say(line('home')); }); });
  // 鼠标停到她身上：有时眨眼比心（8 秒内不重复）
  btn.addEventListener('pointerleave', () => { pet.hover = false; });
  btn.addEventListener('pointerenter', () => {
    pet.hover = true;
    if (drag || want() !== 'idle' || performance.now() < face.hoverCool || Math.random() > .35) return;
    face.hoverCool = performance.now() + 8000; show(F('hover', 'wink'), 1300); if (Math.random() < .5) say(line('hover'), 1400);
  });
  // 打字：一脸期待（3 秒内不重复）
  if (cfg.typeInput) document.addEventListener('input', e => {
    if (!e.target.matches?.(cfg.typeInput)) return;
    face.typed = performance.now();                   // 打字时不闲逛
    if (performance.now() < face.typeCool || want() !== 'idle') return;
    face.typeCool = performance.now() + 3000; show(F('typing', 'hope'), 1600);
  });
  btn.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); cfg.onClick?.(); poke(); } });
  const home = () => { const s = load().pet; pet.x = s && typeof s.x === 'number' ? s.x * innerWidth : cfg.homeX(); pet.y = landAt(-1e9); place(); };
  home();
  addEventListener('resize', () => { if (cfg.canDrag()) home(); });
  if (cfg.wander) wanderLoop();
}

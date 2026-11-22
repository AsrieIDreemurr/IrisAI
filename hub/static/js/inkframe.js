// 手画墨线框（2026-11-08，古明地觉主题用，其他主题可以照用）：给主题指定的部件盖一层 SVG，画粗墨线的框。
// 画法按用户给的例子：转角是尖角（看得出角度，不是圆角，也不一定是直角），不是圆头；一整条墨带，粗细有变化、略抖。
// 主题怎么用（全在主题的 skin.css 里，不用改别的）：
//   :root[data-skin=xx] { --ink-sel: ".p-card, .composer .box"; }   要画框的选择器（字符串，逗号分隔）
//   :root[data-skin=xx] .p-card { --ink-w: 5; --ink-c: #111; }       线宽（像素，不带单位）、颜色；不写就是 4 和墨色
// 部件的 CSS 边框要设成透明、宽度和墨线差不多（框就画在那一圈上），框由这里画。部件大小变了、新出现（新消息等）会自动重画；换主题时清掉重扫。
// 框画在部件的边框位置上；overflow 不是 visible 的部件会把它裁掉（边框在内边距外），主题要给这些部件 overflow: visible，或者别列进来。<textarea>、<table> 这种不能放子元素的不要列进来。
const NS = 'http://www.w3.org/2000/svg';
let sel = '', ro = null, mo = null;
const framed = new Set();

function rng(seed) { let s = seed % 2147483647 || 1; return () => (s = (s * 16807) % 2147483647) / 2147483647; }

// 一整条墨带（不是两条线叠起来——叠两遍会看出分层，2026-11-08 用户反馈）：
// 外圈：四个角各抖一下、长边加几个转折点，全用直线连，转角是尖的；
// 内圈：外圈每个点往里收一段“线宽”（每个点的线宽都不一样，0.7–1.3 倍），角上沿两个方向都收，所以内圈的角也是尖的；
// 外圈和内圈之间整块涂墨（evenodd），粗细变化来自每个点线宽的不同。
function band(w, h, lw, jit, R) {
  const J = () => (R() * 2 - 1) * jit;
  const o = .5;                                            // 外圈贴着边框盒的外沿（抖动只往里，不出界）
  const corners = [[o + Math.abs(J()), o + Math.abs(J())], [w - o - Math.abs(J()), o + Math.abs(J())],
                   [w - o - Math.abs(J()), h - o - Math.abs(J())], [o + Math.abs(J()), h - o - Math.abs(J())]];
  // 每个点记着往里收的方向：角 = 两个方向，边上的点 = 边的法向（框基本是横平竖直的，用轴向就够）
  const IN = [[1, 1], [-1, 1], [-1, -1], [1, -1]], EDGE = [[0, 1], [-1, 0], [0, -1], [1, 0]];
  const outer = [], dirs = [];
  for (let i = 0; i < 4; i++) {
    const [x1, y1] = corners[i], [x2, y2] = corners[(i + 1) % 4], len = Math.hypot(x2 - x1, y2 - y1);
    outer.push([x1, y1]); dirs.push(IN[i]);
    const n = Math.min(4, Math.floor(len / 150));          // 长边每 150px 一个转折点
    for (let k = 1; k <= n; k++) {
      const t = k / (n + 1), [ex, ey] = EDGE[i], wob = Math.abs(J()) * .7;
      outer.push([x1 + (x2 - x1) * t + ex * wob, y1 + (y2 - y1) * t + ey * wob]); dirs.push(EDGE[i]);
    }
  }
  const inner = outer.map(([x, y], i) => { const t = lw * (.65 + R() * .5); return [x + dirs[i][0] * t, y + dirs[i][1] * t]; });
  const poly = pts => 'M' + pts.map(p => p[0].toFixed(1) + ' ' + p[1].toFixed(1)).join('L') + 'Z';
  return poly(outer) + poly(inner.reverse());
}

// 框画在部件的边框位置上（不画进内边距）：盖住整个边框盒，墨带从外沿往里约一个线宽，正好落在 CSS 边框那一圈。
// 所以主题要把这些部件的 CSS 边框设成和墨线差不多宽的透明边框（border: calc(var(--ink-w) * 1px) solid transparent），
// 内容就不会被墨线压住（2026-11-08 用户反馈：框太靠里、挡东西）。
function draw(el) {
  const cs = getComputedStyle(el);
  const bl = parseFloat(cs.borderLeftWidth) || 0, br = parseFloat(cs.borderRightWidth) || 0, bt = parseFloat(cs.borderTopWidth) || 0, bb = parseFloat(cs.borderBottomWidth) || 0;
  const w = el.clientWidth + bl + br, h = el.clientHeight + bt + bb;
  if (w < 8 || h < 8) return;
  const lw = parseFloat(cs.getPropertyValue('--ink-w')) || 4, color = cs.getPropertyValue('--ink-c').trim() || '#111';
  if (!el.dataset.inkSeed) el.dataset.inkSeed = String(1 + Math.floor(Math.random() * 1e6));
  const R = rng(+el.dataset.inkSeed), jit = Math.min(4.2, lw * .7);
  const d = band(w, h, lw, jit, R);
  let svg = el.querySelector(':scope > svg.ink-frame');
  if (!svg) {
    svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('class', 'ink-frame'); svg.setAttribute('aria-hidden', 'true');
    el.appendChild(svg);
  }
  svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
  Object.assign(svg.style, { left: -bl + 'px', top: -bt + 'px', width: w + 'px', height: h + 'px', right: 'auto', bottom: 'auto' });
  svg.innerHTML = `<path d="${d}" fill="${color}" fill-rule="evenodd"/>`;
}

function add(el) {
  if (framed.has(el)) return;
  framed.add(el);
  if (getComputedStyle(el).position === 'static') el.style.position = 'relative';
  ro.observe(el);
}

function scan(root = document) {
  if (!sel) return;
  try {
    if (root.matches?.(sel)) add(root);
    root.querySelectorAll?.(sel).forEach(add);
  } catch (e) { console.warn('inkframe 选择器有误', sel, e); sel = ''; }
}

function clear() {
  for (const el of framed) { el.querySelector(':scope > svg.ink-frame')?.remove(); ro?.unobserve(el); }
  framed.clear();
}

function reload() {
  const s = getComputedStyle(document.documentElement).getPropertyValue('--ink-sel').trim().replace(/^["']|["']$/g, '');
  if (s === sel && framed.size) return;
  clear(); sel = s;
  if (sel) scan();
}

export function initInkFrame() {
  if (ro) return;
  ro = new ResizeObserver(entries => {
    for (const e of entries) { if (!e.target.isConnected) { framed.delete(e.target); ro.unobserve(e.target); } else draw(e.target); }
  });
  mo = new MutationObserver(muts => {
    if (!sel) return;
    for (const m of muts) for (const n of m.addedNodes) if (n.nodeType === 1 && !n.classList?.contains('ink-frame')) scan(n);
  });
  mo.observe(document.body, { childList: true, subtree: true });
  // 主题换了（data-skin 变）或者主题样式表载完了，都重新读一遍 --ink-sel
  new MutationObserver(() => setTimeout(reload, 60)).observe(document.documentElement, { attributes: true, attributeFilter: ['data-skin', 'data-theme'] });
  document.addEventListener('load', e => { if (e.target.tagName === 'LINK') reload(); }, true);
  reload(); setTimeout(reload, 400); setTimeout(reload, 1500);
}

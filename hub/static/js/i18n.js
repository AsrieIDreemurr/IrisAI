// 界面语言（2026-11-22）：代码里的界面文字是中文；选了英文时，用 /s/i18n/en.json 把页面上的文字、提示、占位符换成英文。
// 普通脚本（不是模块），在每个页面的 <head> 里最先加载，换完再显示页面，不会先闪一下中文。
// 只换界面文字：对话内容、文件内容、文件名、模型输出不换（EXCLUDE 里的区域，和换完还剩中文的文字都保持原样）。
// 语言存在本机（localStorage 的 lang）和设置（look.lang）里；设置页改了会刷新页面。
(function () {
  let lang = 'zh';
  try { lang = localStorage.getItem('lang') === 'en' ? 'en' : 'zh'; } catch (e) {}
  window.I18N = { lang, t: s => s, set(l) { try { localStorage.setItem('lang', l); } catch (e) {} if (l !== lang) location.reload(); } };
  if (lang !== 'en') return;
  document.documentElement.lang = 'en';
  const hide = document.createElement('style');
  hide.textContent = 'html{visibility:hidden}';
  document.head.appendChild(hide);
  const show = () => hide.remove();
  setTimeout(show, 2000);            // 词典读不到也别一直藏着

  const CJK = /[぀-ヿ一-鿿]/;
  const EXCLUDE = '.md-body,.u,.think-body,.step-body,pre,code,textarea,.edit-area,.tags,.tr-out,.tr-lines,.ocr-res,.ocr-ans,.ocr-trans,' +
    '.hit .txt,.conv > b,.path,.crumbs > :not(.legend),.lora-name,.mp-main,.mp-body,.mp-log,.mp-near,.p-u,.p-a,.pa-u,.pa-a,.kfile,[data-noi18n],script,style';
  const ATTRS = ['title', 'placeholder', 'aria-label', 'data-menu-title', 'data-short', 'data-desc', 'alt'];
  const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const WEEK = { 日: 'Sun', 一: 'Mon', 二: 'Tue', 三: 'Wed', 四: 'Thu', 五: 'Fri', 六: 'Sat' };
  const PUNCT = { '，': ', ', '。': '. ', '：': ': ', '；': '; ', '、': ', ', '（': ' (', '）': ') ', '？': '? ', '！': '! ', '～': '~',
    '“': '"', '”': '"', '「': '"', '」': '"', '《': '"', '》': '"', '…': '…', '—': '—' };
  let dict = {}, multi = null, single = null;
  const cache = new Map();
  const esc = s => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

  function build(d) {
    dict = d;
    const ks = Object.keys(d).sort((a, b) => b.length - a.length);
    const one = k => (k.match(/[一-鿿]/g) || []).length === 1;
    multi = new RegExp(ks.filter(k => !one(k)).map(esc).join('|'), 'g');
    // 只有一个汉字的键（字、秒、张……）只在挨着数字时替换，免得把别的中文拆碎
    const ones = ks.filter(one).map(esc).join('|');
    single = new RegExp(`(?<=\\d\\s?)(?:${ones})|(?:${ones})(?=\\s?\\d)`, 'g');
  }

  function t(text) {
    if (!text || !CJK.test(text)) return text;
    if (cache.has(text)) return cache.get(text);
    const lead = text.match(/^\s*/)[0], trail = text.match(/\s*$/)[0];
    let s = text.trim(), out = text;
    if (dict[s] != null) out = lead + dict[s] + trail;
    else {
      s = s.replace(/(\d{1,2})\s*月\s*(\d{1,2})\s*日/g, (m, a, b) => `${MONTHS[a - 1] || a} ${b}`)
           .replace(/周([日一二三四五六])/g, (m, w) => WEEK[w]).replace(/\b([A-Za-z]):?\s*盘/g, (m, c) => `${c.toUpperCase()}: drive`);
      s = s.replace(multi, m => '\u0001' + dict[m] + '\u0001');
      const masks = [];      // 词典换完后，剩下的引号里的东西（文件名、用户写的）不翻，也不算没翻完
      s = s.replace(/“[^”]*”|「[^」]*」/g, m => { masks.push(m.slice(1, -1)); return `\u0002${masks.length - 1}\u0003`; });
      s = s.replace(single, m => '\u0001' + dict[m] + '\u0001');
      if (!CJK.test(s.replace(/\u0002\d+\u0003/g, ''))) {
        s = s.replace(/[，。：；、（）？！～“”「」《》]/g, m => PUNCT[m]);
        s = s.replace(/([A-Za-z0-9%)\]"])\u0001(?=[A-Za-z0-9("])/g, '$1 ').replace(/([A-Za-z0-9%)"])\u0001/g, '$1 ')
             .replace(/\u0001(?=[A-Za-z0-9(])/g, ' ').replace(/\u0001/g, '');
        s = s.replace(/\u0002(\d+)\u0003/g, (m, i) => `"${masks[i]}"`);
        s = s.replace(/ {2,}/g, ' ').replace(/ ([,.:;)!?])/g, '$1').replace(/\( /g, '(').trim();
        out = lead + s + trail;
      }
    }
    cache.set(text, out);
    return out;
  }
  window.I18N.t = t;

  const skip = el => !el || !!el.closest(EXCLUDE);
  function doNode(n) {
    if (n.nodeType === 3) {
      if (!CJK.test(n.data) || skip(n.parentElement)) return;
      const v = t(n.data); if (v !== n.data) n.data = v;
    } else if (n.nodeType === 1) {
      // 内容区里的文字不翻；提示（title、placeholder）照样翻——换完还剩中文的（用户写的提示词、文件名）会保持原样
      const ex = skip(n);
      if (!n.closest('.conv > b')) for (const a of ATTRS) {
        const v = n.getAttribute(a);
        if (v && CJK.test(v)) { const w = t(v); if (w !== v) n.setAttribute(a, w); }
      }
      if (n.tagName === 'INPUT' && n.type === 'button' && CJK.test(n.value)) n.value = t(n.value);
      if (!ex) for (let c = n.firstChild; c; c = c.nextSibling) doNode(c);
    }
  }
  function start() {
    doNode(document.documentElement);
    if (document.title) document.title = t(document.title);
    new MutationObserver(ms => {
      for (const m of ms) {
        if (m.type === 'childList') m.addedNodes.forEach(doNode);
        else if (m.type === 'characterData') doNode(m.target);
        else if (m.type === 'attributes') doNode(m.target.nodeType === 1 ? m.target : m.target.parentNode);
      }
    }).observe(document.documentElement, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ATTRS });
    show();
  }
  fetch('/s/i18n/en.json').then(r => r.json()).then(d => {
    build(d);
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
  }).catch(show);
})();

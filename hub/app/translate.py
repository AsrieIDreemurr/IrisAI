"""翻译：把文本或文件拆成一条条，分段交给翻译模型，再按条对回去。网页的翻译模块和 Agent 的 translate 工具共用。

- 日文默认用 Sakura（游戏、小说，逐行对齐好）；其他语言用 Hy-MT2（通顺，但会合并行）。
- 逐行对齐：一段译完行数对不上，就把这一段改成一行一行地译。字幕、文档、游戏文本都要对齐。
- 术语表：沙箱\\术语表\\*.txt，每行“原文=译文 #备注”。每段只附这一段里出现的词。
- 文件：txt、md、srt、vtt、ass、docx、epub。译文另存为“原名.zh.扩展名”，原文件不动。
"""
import asyncio
import html
import io
import os
import re
import time
import zipfile

from . import llm
from .config import CFG, SANDBOX
from .paths import display, writable

GLOSSARY_DIR = os.path.join(SANDBOX, "术语表")
OUT_DIR = os.path.join(SANDBOX, "翻译")
FILE_EXT = {".txt", ".md", ".markdown", ".srt", ".vtt", ".ass", ".ssa", ".docx", ".epub"}
KANA = re.compile(r"[぀-ゟ゠-ヿｦ-ﾟ]")
HAN = re.compile(r"[一-鿿]")
LETTER = re.compile(r"[A-Za-z぀-ヿ一-鿿가-힯Ѐ-ӿ]")
LANG = {"zh": "中文", "en": "英语", "ja": "日语"}
LANG_EN = {"zh": "Chinese", "en": "English", "ja": "Japanese"}

SAKURA_SYS = ("你是一个日本二次元领域的日语翻译模型，可以流畅通顺地以日本轻小说/漫画/Galgame的风格将日文翻译成简体中文，"
              "并联系上下文正确使用人称代词，不擅自添加原文中没有的代词。")
# 每段最多多少字、多少行。Sakura 的上下文只开了 8k，段小一些行数更容易对上
CHUNK = {"sakura": (500, 20), "hy": (1000, 30)}


# ---------------------------------------------------------------- 语言、引擎、术语表
def detect(text):
    """粗判原文语言：有假名就是日文；汉字占多数就是中文；否则按其他语言处理。"""
    sample = text[:20000]
    kana, han, letters = len(KANA.findall(sample)), len(HAN.findall(sample)), len(LETTER.findall(sample))
    if kana >= 3 and kana >= letters * 0.02:
        return "ja"
    if han > letters * 0.5:
        return "zh"
    return "en"


def plan(text, source="auto", target="", engine="auto"):
    """定原文语言、目标语言、用哪个模型。返回 (source, target, engine)。"""
    found = detect(text)
    src = found if source in ("", "auto", None) else source
    if found == "ja" and src != "ja":
        src = "ja"            # 有成片的假名一定是日文；Agent 有时把日文字幕的 source 填成 en，会错用 Hy-MT2
    tgt = target or ("en" if src == "zh" else "zh")
    if engine not in ("sakura", "hy"):
        engine = "sakura" if src == "ja" and tgt == "zh" else "hy"
    if engine == "sakura" and not (src == "ja" and tgt == "zh"):
        engine = "hy"         # Sakura 只能日→中
    return src, tgt, engine


def model_of(engine):
    return CFG["models"]["ja_zh" if engine == "sakura" else "translate"]


def parse_glossary(text):
    """每行一条：原文=译文 #备注。也认“原文->译文”和制表符分隔。# 开头的行是注释。"""
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        note = ""
        if " #" in line:
            line, note = line.split(" #", 1)
        for sep in ("->", "=", "\t"):
            if sep in line:
                s, d = line.split(sep, 1)
                if s.strip() and d.strip():
                    out.append((s.strip(), d.strip(), note.strip()))
                break
    return out


def list_glossaries():
    os.makedirs(GLOSSARY_DIR, exist_ok=True)
    out = []
    for name in sorted(os.listdir(GLOSSARY_DIR)):
        p = os.path.join(GLOSSARY_DIR, name)
        if name.lower().endswith(".txt") and os.path.isfile(p):
            text = read_text(p)[0]
            out.append({"name": name[:-4], "path": p, "count": len(parse_glossary(text))})
    return out


def glossary_path(name):
    name = re.sub(r'[\\/:*?"<>|]', "", (name or "").strip())[:60]
    if not name:
        raise ValueError("术语表名字是空的")
    return os.path.join(GLOSSARY_DIR, name + ".txt")


def load_glossary(name):
    if not name:
        return []
    p = glossary_path(name)
    return parse_glossary(read_text(p)[0]) if os.path.exists(p) else []


# ---------------------------------------------------------------- 读文本（日文 Shift-JIS 不能当成 GBK 读）
def decode(raw):
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", "replace"), "utf-8-sig"
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16"), "utf-16"
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    # GBK 几乎什么字节都能解开，所以先试 Shift-JIS：能解开、而且假名不少，就按日文算
    # 只数全角假名：GBK 的字节按 Shift-JIS 解开大多是半角片假名
    try:
        t = raw.decode("cp932")
        if len(re.findall(r"[぀-ヿ]", t)) >= max(3, len(t) * 0.1):
            return t, "cp932"
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("gb18030"), "gb18030"
    except UnicodeDecodeError:
        return raw.decode("utf-8", "replace"), "utf-8"


def read_text(p, limit=20_000_000):
    raw = open(p, "rb").read(limit + 1)
    if len(raw) > limit:
        raise ValueError(f"文件太大（超过 {limit // 1_000_000} MB）")
    return decode(raw)


# ---------------------------------------------------------------- 调用模型
def _terms_in(terms, text):
    return [t for t in terms if t[0] in text]


def _messages(engine, text, src, tgt, terms, context=""):
    if engine == "sakura":
        if terms:
            g = "\n".join(f"{s}->{d}" + (f" #{n}" if n else "") for s, d, n in terms)
            user = "根据以下术语表（可以为空）：\n" + g + "\n将下面的日文文本根据对应关系和备注翻译成中文：" + text
        else:
            user = "将下面的日文文本翻译成中文：" + text
        return [{"role": "system", "content": SAKURA_SYS}, {"role": "user", "content": user}], {"temperature": 0.1, "top_p": 0.3}
    # Hy-MT2 官方模板：有中文一方用中文提示，两边都不是中文用英文提示
    head = ""
    if terms:
        head = "参考下面的翻译：\n" + "\n".join(f"{s} 翻译成 {d}" for s, d, _ in terms) + "\n\n"
    if context:
        head += context + "\n参考上面的信息，把下面的文本翻译成" + LANG[tgt] + "，注意不需要翻译上文，也不要额外解释：\n\n"
        body = head + text
    elif src == "zh" or tgt == "zh":
        body = head + f"将以下文本翻译为{LANG[tgt]}，注意只需要输出翻译后的结果，不要额外解释：\n\n" + text
    else:
        body = head + f"Translate the following segment into {LANG_EN[tgt]}, without additional explanation.\n\n" + text
    return [{"role": "user", "content": body}], {"temperature": 0.7, "top_p": 0.6, "top_k": 20, "repeat_penalty": 1.05}


async def _call(engine, text, src, tgt, terms, context=""):
    msgs, params = _messages(engine, text, src, tgt, _terms_in(terms, text), context)
    out, _ = await llm.complete(model_of(engine), msgs, max_tokens=max(256, len(text) * 3 + 200), **params)
    return out.strip()


def _chunks(units, engine):
    """把要译的条目（下标）分段。空条目和没有文字的条目不译。"""
    limit, max_lines = CHUNK[engine]
    todo = [i for i, u in enumerate(units) if LETTER.search(u)]
    out, cur, size = [], [], 0
    for i in todo:
        n = len(units[i])
        if cur and (size + n > limit or len(cur) >= max_lines):
            out.append(cur)
            cur, size = [], 0
        cur.append(i)
        size += n
    if cur:
        out.append(cur)
    return out


async def translate_units(units, src, tgt, engine, terms, align=True, emit=None, stop=None):
    """逐段翻译 units（字符串列表），返回同样长度的译文列表。不需要翻译的条目原样保留。
    align=False 时每一条是一个自然段，一段的译文整块放在这一段第一条的位置（给不要求对齐的纯文本用）。"""
    result = list(units)
    limit = CHUNK[engine][0]
    if any(len(u) > limit for u in units):
        # 先把长条目拆成有界片段，翻译完再并回原条目，文件格式回填仍一一对应。
        pieces, spans = [], []
        for unit in units:
            begin = len(pieces)
            rest = unit
            while len(rest) > limit:
                cuts = list(re.finditer(r"[。！？.!?;；\s]", rest[:limit]))
                cut = cuts[-1].end() if cuts and cuts[-1].end() >= limit // 2 else limit
                pieces.append(rest[:cut])
                rest = rest[cut:]
            if rest:
                pieces.append(rest)
            if len(pieces) == begin:
                pieces.append("")
            spans.append((begin, len(pieces)))
        translated = await translate_units(pieces, src, tgt, engine, terms, True, None, stop)
        result = [("" if tgt in {"zh", "ja"} else " ").join(translated[a:b]) for a, b in spans]
        if emit:
            emit({"type": "start", "n": 1, "units": len(units), "engine": engine,
                  "model": model_of(engine), "source": src, "target": tgt})
            emit({"type": "chunk", "i": 0, "n": 1, "src": units,
                  "out": result if align else ["\n\n".join(result)], "fallback": False, "secs": 0})
        return result
    chunks = _chunks(units, engine)
    if emit:
        emit({"type": "start", "n": len(chunks), "units": sum(len(c) for c in chunks), "engine": engine,
              "model": model_of(engine), "source": src, "target": tgt})
    prev = ""
    for k, idx in enumerate(chunks):
        if stop and stop():
            break
        t0 = time.time()
        src_lines = [units[i].replace("\n", " ") for i in idx] if align else [units[i] for i in idx]
        text = ("\n" if align else "\n\n").join(src_lines)
        ctx = prev[-300:] if engine == "hy" and prev else ""
        out = await _call(engine, text, src, tgt, terms, ctx)
        lines = [l for l in out.splitlines() if l.strip()]
        fallback = False
        if align and len(lines) != len(idx):
            # 行数对不上：这一段一行一行地译
            fallback, lines = True, []
            for line in src_lines:
                if stop and stop():
                    break
                lines.append((await _call(engine, line, src, tgt, terms)).replace("\n", " "))
            if len(lines) < len(idx):
                break
        if align:
            for i, line in zip(idx, lines):
                result[i] = line
        else:
            result[idx[0]] = out
            for i in idx[1:]:
                result[i] = None
        prev = text
        if emit:
            emit({"type": "chunk", "i": k, "n": len(chunks), "src": src_lines,
                  "out": lines if align else [out], "fallback": fallback, "secs": round(time.time() - t0, 1)})
    if not align:
        result = [r for r in result if r is not None]
    return result


async def translate_text(text, source="auto", target="", engine="auto", terms=(), align=True, emit=None, stop=None):
    src, tgt, engine = plan(text, source, target, engine)
    # 逐行对齐时一行一条；不对齐时一个自然段一条，段内换行交给模型
    units = text.splitlines() if align else [u.strip("\n") for u in re.split(r"\n\s*\n", text.strip())]
    out = await translate_units(units, src, tgt, engine, list(terms), align, emit, stop)
    return ("\n" if align else "\n\n").join(out), {"source": src, "target": tgt, "engine": engine, "model": model_of(engine)}


# ---------------------------------------------------------------- 文件格式：拆成条目，再把译文放回去
def _text_fmt(text, md=False):
    lines = text.split("\n")
    units, slots, fence = [], [], False
    pre = re.compile(r"^(\s*(?:#{1,6}\s+|[-*+]\s+|\d+[.)]\s+|>\s?)*)")
    for n, line in enumerate(lines):
        body = line.rstrip("\r")
        if md and body.lstrip().startswith("```"):
            fence = not fence
            continue
        if fence or not LETTER.search(body):
            continue
        head = pre.match(body).group(1) if md else re.match(r"^\s*", body).group(0)
        slots.append((n, head, body.endswith("\r") or line.endswith("\r")))
        units.append(body[len(head):])

    def build(out):
        res = list(lines)
        for (n, head, cr), t in zip(slots, out):
            res[n] = head + t + ("\r" if cr else "")
        return "\n".join(res)
    return units, build


def _cue_fmt(text, vtt=False):
    """srt / vtt：每条字幕的文字合成一行来译，译文写成一行。"""
    lines = text.replace("\r\n", "\n").split("\n")
    units, spans, i = [], [], 0
    while i < len(lines):
        if "-->" in lines[i]:
            j = i + 1
            while j < len(lines) and lines[j].strip():
                j += 1
            if j > i + 1:
                body = [re.sub(r"<[^>]+>", "", l) for l in lines[i + 1:j]]
                joiner = "" if KANA.search("".join(body)) or HAN.search("".join(body)) else " "
                units.append(joiner.join(b.strip() for b in body))
                spans.append((i + 1, j))
            i = j
        else:
            i += 1

    def build(out):
        res, last = [], 0
        for (a, b), t in zip(spans, out):
            res += lines[last:a] + [t]
            last = b
        return "\n".join(res + lines[last:])
    return units, build


def _ass_fmt(text):
    lines = text.replace("\r\n", "\n").split("\n")
    units, slots = [], []
    for n, line in enumerate(lines):
        if not line.startswith("Dialogue:"):
            continue
        parts = line.split(",", 9)
        if len(parts) < 10:
            continue
        body = parts[9]
        head = re.match(r"^(\{[^}]*\})*", body).group(0)          # 开头的特效标签保留
        plain = re.sub(r"\{[^}]*\}", "", body[len(head):])
        plain = re.sub(r"\\[Nn]", "" if KANA.search(plain) or HAN.search(plain) else " ", plain).replace("\\h", " ")
        if LETTER.search(plain):
            slots.append((n, ",".join(parts[:9]) + "," + head))
            units.append(plain.strip())

    def build(out):
        res = list(lines)
        for (n, head), t in zip(slots, out):
            res[n] = head + t
        return "\n".join(res)
    return units, build


_TOKEN = re.compile(r"<w:p\b[^>]*/>|<w:p\b[^>]*>|</w:p>|<w:t\b[^>]*>(.*?)</w:t>", re.S)


def _docx_part(xml):
    """Word 的一个 XML 部件：按段落收集文字（w:t），译文放进段落的第一个 w:t，其余清空，段落格式保留。"""
    stack, paras, order = [], {}, []
    for m in _TOKEN.finditer(xml):
        tok = m.group(0)
        if tok.startswith("<w:p") and not tok.endswith("/>") and not tok.startswith("<w:t"):
            stack.append(m.start())
            paras[m.start()] = []
            order.append(m.start())
        elif tok == "</w:p>":
            if stack:
                stack.pop()
        elif tok.startswith("<w:t") and stack:
            paras[stack[-1]].append(m)
    keep = [k for k in order if LETTER.search("".join(html.unescape(t.group(1)) for t in paras[k]))]
    units = ["".join(html.unescape(t.group(1)) for t in paras[k]) for k in keep]

    def build(out):
        repl = []
        for k, t in zip(keep, out):
            runs = paras[k]
            repl.append((runs[0].start(), runs[0].end(), f'<w:t xml:space="preserve">{html.escape(t, quote=False)}</w:t>'))
            repl += [(r.start(), r.end(), "<w:t></w:t>") for r in runs[1:]]
        res, last = [], 0
        for a, b, s in sorted(repl):
            res += [xml[last:a], s]
            last = b
        return "".join(res + [xml[last:]])
    return units, build


_BLOCK = re.compile(r"(<(p|h[1-6]|title)\b[^>]*>)(.*?)(</\2>)", re.S | re.I)


def _xhtml_part(xml):
    """epub 的一章：每个段落、标题一条。段内的注音（rt）去掉，行内格式不保留。"""
    ms = []
    for m in _BLOCK.finditer(xml):
        inner = re.sub(r"<(rt|rp)\b[^>]*>.*?</\1>", "", m.group(3), flags=re.S | re.I)
        inner = re.sub(r"<br\s*/?>", " ", inner, flags=re.I)
        text = html.unescape(re.sub(r"<[^>]+>", "", inner)).strip()
        if LETTER.search(text) and "<img" not in m.group(3).lower():
            ms.append((m, text))

    def build(out):
        res, last = [], 0
        for (m, _), t in zip(ms, out):
            res += [xml[last:m.start()], m.group(1), html.escape(t, quote=False), m.group(4)]
            last = m.end()
        return "".join(res + [xml[last:]])
    return [t for _, t in ms], build


def _zip_fmt(raw, pick, part_fmt):
    zin = zipfile.ZipFile(io.BytesIO(raw))
    names = [n for n in zin.namelist() if pick(n)]
    parts = []
    for n in names:
        units, build = part_fmt(zin.read(n).decode("utf-8"))
        parts.append((n, units, build))
    units = [u for _, us, _ in parts for u in us]

    def build(out):
        done, k = {}, 0
        for n, us, b in parts:
            done[n] = b(out[k:k + len(us)]).encode("utf-8")
            k += len(us)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zout:
            for info in zin.infolist():
                data = done.get(info.filename, zin.read(info.filename))
                # epub 要求 mimetype 不压缩、放第一个；原样沿用每个文件的压缩方式
                zout.writestr(info, data, compress_type=info.compress_type)
        return buf.getvalue()
    return units, build


def parse_file(path):
    """返回 (条目列表, build(译文列表) -> bytes 或 str)。"""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".docx":
        raw = open(path, "rb").read()
        return _zip_fmt(raw, lambda n: re.match(r"word/(document|footnotes|endnotes|header\d*|footer\d*)\.xml$", n),
                        _docx_part)
    if ext == ".epub":
        raw = open(path, "rb").read()
        return _zip_fmt(raw, lambda n: n.lower().endswith((".xhtml", ".html", ".htm")), _xhtml_part)
    text, _ = read_text(path)
    if ext in (".srt", ".vtt"):
        return _cue_fmt(text, ext == ".vtt")
    if ext in (".ass", ".ssa"):
        return _ass_fmt(text)
    return _text_fmt(text, md=ext in (".md", ".markdown"))


def out_path(path, tgt):
    folder = os.path.dirname(path)
    if not writable(folder):
        folder = OUT_DIR
    os.makedirs(folder, exist_ok=True)
    stem, ext = os.path.splitext(os.path.basename(path))
    p = os.path.join(folder, f"{stem}.{tgt}{ext}")
    k = 2
    while os.path.exists(p):
        p = os.path.join(folder, f"{stem}.{tgt}（{k}）{ext}")
        k += 1
    return p


async def translate_file(path, source="auto", target="", engine="auto", terms=(), emit=None, stop=None):
    ext = os.path.splitext(path)[1].lower()
    if ext not in FILE_EXT:
        raise ValueError(f"不支持这种文件：{ext or '没有扩展名'}。能翻译的有 " + "、".join(sorted(FILE_EXT)))
    units, build = await asyncio.to_thread(parse_file, path)
    if not units:
        raise ValueError("文件里没有找到要翻译的文字")
    src, tgt, engine = plan("\n".join(units), source, target, engine)
    out = await translate_units(units, src, tgt, engine, list(terms), True, emit, stop)
    if stop and stop():
        return None, {}
    data = build(out)
    dst = out_path(path, tgt)
    if isinstance(data, str):
        enc = "utf-8-sig" if ext in (".txt", ".srt", ".ass", ".ssa") else "utf-8"
        with open(dst, "w", encoding=enc, newline="") as f:
            f.write(data)
    else:
        with open(dst, "wb") as f:
            f.write(data)
    return dst, {"source": src, "target": tgt, "engine": engine, "model": model_of(engine), "units": len(units)}


# ---------------------------------------------------------------- 后台任务（网页用）
from .jobs import JOBS, Job  # noqa: E402


def start_job(body):
    terms = parse_glossary(body.get("glossary_text") or "") or load_glossary(body.get("glossary"))

    async def go(job):
        kw = dict(source=body.get("source") or "auto", target=body.get("target") or "", engine=body.get("engine") or "auto",
                  terms=terms, emit=job.emit, stop=lambda: job.stopped)
        if body.get("path"):
            dst, info = await translate_file(body["path"], **kw)
            job.emit({"type": "done", "file": dst, "show": display(dst) if dst else "", "secs": round(time.time() - job.t0, 1),
                      "stopped": job.stopped, **info})
        else:
            out, info = await translate_text(body.get("text") or "", align=body.get("align", True), **kw)
            job.emit({"type": "done", "text": out, "secs": round(time.time() - job.t0, 1), "stopped": job.stopped, **info})
    return Job("translate").run(go)

"""知识库：三层。
- 规范层（knowledge\\规范\\*.md）：一份规范一个文件。可以附在对话里（全文进系统提示），Agent 也能按需用 read_norm 读全文。
- 知识层（knowledge\\知识\\子库\\…）：Agent 用 search_knowledge 自己检索。
- 其他层（knowledge\\其他\\子库\\…）：Agent 默认不碰；用户在知识库页面自己搜，或者在某个对话里打开某个库。

文件是唯一的来源：直接往文件夹里放文件也行，扫描时按修改时间和大小发现新增、改动、删除。
检索：关键词（SQLite 全文索引，三字切分）和语义（Qwen3-Embedding）各取一批，再用 Qwen3-Reranker 排序。
向量和重排模型都在 CPU 上常驻，不占显卡。
"""
import asyncio
import html as htmlmod
import json
import os
import re
import sqlite3
import time

import numpy as np
from . import fileops, activity

from . import llm
from .config import AI_ROOT, DATA_DIR, ROLES

ROOT = os.path.join(AI_ROOT, "knowledge")
LAYERS = ["规范", "知识", "其他"]
META_PATH = os.path.join(ROOT, "库说明.json")
DB_PATH = os.path.join(DATA_DIR, "kb.db")
EMBED = ROLES.get("embed")        # 模型清单的 roles
RERANK = ROLES.get("rerank")
TEXT_EXT = {".txt", ".md", ".markdown", ".csv", ".json", ".yaml", ".yml", ".py", ".js", ".ts", ".css", ".ini", ".toml", ".log", ".srt", ".ass"}
DOC_EXT = {".docx", ".epub", ".pdf", ".html", ".htm"}
EXTS = TEXT_EXT | DOC_EXT
CHUNK, CHUNK_MAX = 600, 900
QUERY_INSTRUCT = "Instruct: 根据问题，找出能回答它的资料段落\nQuery:"

# 关联的外部文件夹或文件：原地建索引，不复制，原文件改了下次扫描自动更新
DEFAULT_SOURCES = {"知识/本项目文档": [os.path.join(AI_ROOT, "docs"), os.path.join(AI_ROOT, "README.md")]}
DEFAULT_LIBS = {
    "知识/本项目文档": "这套本地 AI 系统自己的方案、测试记录和说明：模型怎么选的、Agent 流程、各模块怎么用。",
    "知识/游戏汉化资料": "游戏汉化相关：术语、剧情、人物、原文和译文对照。",
    "知识/技术资料": "编程、工具和模型的使用说明。",
    "知识/前端设计": "Anthropic 公开的前端设计方法（中文笔记，附原文链接）：怎么避开 AI 常见的千篇一律的设计、字体配色版式动效怎么做、怎么给设计打分验收。",
}


# ---------------------------------------------------------------- 目录和说明
def load_meta():
    try:
        m = json.load(open(META_PATH, encoding="utf-8"))
    except (OSError, ValueError):
        m = {}
    m.setdefault("libs", {})
    m.setdefault("norms", {})
    return m


def save_meta(m):
    tmp = META_PATH + ".tmp"
    json.dump(m, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    os.replace(tmp, META_PATH)


def ensure_layout():
    for layer in LAYERS:
        os.makedirs(os.path.join(ROOT, layer), exist_ok=True)
    m = load_meta()
    if not m.get("_init"):
        for lib, desc in DEFAULT_LIBS.items():
            os.makedirs(os.path.join(ROOT, *lib.split("/")), exist_ok=True)
            m["libs"].setdefault(lib, {"desc": desc, "sources": DEFAULT_SOURCES.get(lib, [])})
        m["_init"] = True
        save_meta(m)


def safe_name(name):
    name = re.sub(r'[\\/:*?"<>|]', "", (name or "").strip())[:60]
    if not name or name.startswith("."):
        raise ValueError("名字不能为空，也不能有 \\ / : * ? \" < > | 这些字符")
    return fileops.component(name)


def lib_dir(lib):
    parts = lib.split("/") if isinstance(lib, str) else []
    if not parts or parts[0] not in LAYERS or len(parts) != (1 if parts[0] == "规范" else 2):
        raise ValueError(f"没有这个库：{lib}")
    for part in parts:
        fileops.component(part)
    path = os.path.realpath(os.path.join(ROOT, *parts))
    if not fileops.inside(path, os.path.join(ROOT, parts[0])) or not os.path.isdir(path):
        raise ValueError(f"没有这个库：{lib}")
    return path


def libs():
    """所有子库（规范层算一个库“规范”）。"""
    ensure_layout()
    m = load_meta()
    out = [{"id": "规范", "layer": "规范", "name": "规范", "desc": "各种规范，附在对话里或让 Agent 按需读取"}]
    for layer in ("知识", "其他"):
        base = os.path.join(ROOT, layer)
        for name in sorted(os.listdir(base)):
            if os.path.isdir(os.path.join(base, name)) and not name.startswith("."):
                lid = f"{layer}/{name}"
                out.append({"id": lid, "layer": layer, "name": name, "desc": m["libs"].get(lid, {}).get("desc", "")})
    with _db() as c:
        stats = {r[0]: (r[1], r[2]) for r in c.execute("SELECT lib, COUNT(*), SUM(chunks) FROM files GROUP BY lib")}
    for l in out:
        l["files"], l["chunks"] = stats.get(l["id"], (0, 0))
        l["chunks"] = l["chunks"] or 0
    return out


def create_lib(layer, name, desc=""):
    if layer not in ("知识", "其他"):
        raise ValueError("只能在知识层和其他层下建子库")
    name = safe_name(name)
    os.makedirs(os.path.join(ROOT, layer, name), exist_ok=True)
    m = load_meta()
    m["libs"][f"{layer}/{name}"] = {"desc": desc}
    save_meta(m)
    return f"{layer}/{name}"


def set_desc(lib, desc):
    m = load_meta()
    m["libs"].setdefault(lib, {})["desc"] = desc
    save_meta(m)


def norms():
    """规范层的文件：名字、说明、是否默认带上、字数。"""
    ensure_layout()
    m = load_meta()
    base = os.path.join(ROOT, "规范")
    out = []
    for name in sorted(os.listdir(base)):
        p = os.path.join(base, name)
        if os.path.isfile(p) and os.path.splitext(name)[1].lower() in EXTS:
            info = m["norms"].get(name, {})
            out.append({"name": name, "title": os.path.splitext(name)[0], "path": p, "desc": info.get("desc", ""),
                        "default": bool(info.get("default")), "size": os.path.getsize(p)})
    return out


def set_norm(name, **kw):
    m = load_meta()
    m["norms"].setdefault(name, {}).update({k: v for k, v in kw.items() if k in ("desc", "default")})
    save_meta(m)


HISTORY = os.path.join(ROOT, ".历史")


def _hist_dir(filename):
    return os.path.join(HISTORY, filename)


def _hist_log(filename):
    try:
        return json.load(open(os.path.join(_hist_dir(filename), "记录.json"), encoding="utf-8"))
    except (OSError, ValueError):
        return []


def save_norm(name, content, reason="", who="Agent", desc=None):
    """新建或整份替换一份规范。原来的版本先存进 knowledge\\.历史\\文件名\\，可以撤回。返回 (文件名, 是否新建)。"""
    ensure_layout()
    n = find_norm(name)
    filename = n["name"] if n else (safe_name(name) if os.path.splitext(name)[1].lower() in EXTS else safe_name(name) + ".md")
    path = os.path.join(ROOT, "规范", filename)
    if os.path.splitext(filename)[1].lower() not in (".md", ".txt", ".markdown"):
        raise ValueError("只能改 md、txt 格式的规范")
    created = not os.path.exists(path)
    hd = _hist_dir(filename)
    os.makedirs(hd, exist_ok=True)
    log = _hist_log(filename)
    stamp = fileops.stamp()
    if not created:
        import shutil
        shutil.copy2(path, os.path.join(hd, stamp + os.path.splitext(filename)[1]))
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(content.replace("\r\n", "\n"))
    log.append({"t": time.time(), "stamp": stamp, "who": who, "reason": reason[:300], "created": created,
                "backup": None if created else stamp + os.path.splitext(filename)[1]})
    json.dump(log, open(os.path.join(hd, "记录.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    if desc is not None:
        set_norm(filename, desc=desc)
    return filename, created


def norm_history(name):
    n = find_norm(name)
    filename = n["name"] if n else name
    return list(reversed(_hist_log(filename)))


def restore_norm(name, stamp):
    """撤回到某次修改之前的版本（当前版本也会先存进历史）。"""
    n = find_norm(name)
    if not n:
        raise ValueError("没有这份规范")
    entry = next((e for e in _hist_log(n["name"]) if e["stamp"] == stamp), None)
    if not entry or not entry.get("backup"):
        raise ValueError("这次修改是新建的，没有更早的版本")
    old = open(os.path.join(_hist_dir(n["name"]), entry["backup"]), encoding="utf-8").read()
    save_norm(n["name"], old, reason=f"撤回到 {stamp} 之前的版本", who="你")


def find_norm(name):
    name = (name or "").strip()
    for n in norms():
        if name in (n["name"], n["title"]):
            return n
    return None


def norm_text(name):
    n = find_norm(name)
    if not n:
        return None
    if os.path.splitext(n["path"])[1].lower() in TEXT_EXT:
        from .translate import read_text
        return read_text(n["path"])[0]          # 原文，保留 Markdown 格式；改规范时在原文上改
    return "\n\n".join(t for t, _ in extract(n["path"]))


# ---------------------------------------------------------------- 系统提示词（2026-11-22，用户要的：放进知识库，能改）
# knowledge\系统提示词\完整版.md、精简版.md。没有就用 hub\app\prompts\ 里的默认内容建一份（仓库里带的是默认内容）。
# 每次发消息时读文件，改完从下一条消息起生效。开头的 <!-- --> 注释写着什么时候用，不会发给模型。
SYSPROMPT_DIR = os.path.join(ROOT, "系统提示词")
_DEFAULTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts")
SYSPROMPTS = {
    "完整版": ("system.txt", "工作台的对话和 Agent 每次发消息时用。改完从下一条消息起生效；改后第一条会慢一点（模型要重新读一遍提示词）。"),
    "精简版": ("system_lite.txt", "入口页的随手问、桌宠聊天（快速小模型）每次发消息时用。改完从下一条消息起生效。"),
}
_HEAD = "<!-- 系统提示词 · {name}\n时机：{when}\n这段注释不会发给模型。想回到默认内容：知识库页面点“恢复默认”，或者删掉这个文件。\n-->\n\n"


def _sysprompt_default(name):
    return open(os.path.join(_DEFAULTS, SYSPROMPTS[name][0]), encoding="utf-8").read().strip()


def _strip_head(text):
    return re.sub(r"\A\s*<!--.*?-->\s*", "", text, flags=re.S).strip()


def sysprompt_path(name):
    return os.path.join(SYSPROMPT_DIR, name + ".md")


def ensure_sysprompt(name):
    p = sysprompt_path(name)
    if not os.path.isfile(p):
        os.makedirs(SYSPROMPT_DIR, exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(_HEAD.format(name=name, when=SYSPROMPTS[name][1]) + _sysprompt_default(name) + "\n")
    return p


def system_prompt(name):
    """发给模型的系统提示词。文件读不了或者是空的就用默认内容。"""
    try:
        text = _strip_head(open(ensure_sysprompt(name), encoding="utf-8").read())
    except OSError:
        text = ""
    return text or _sysprompt_default(name)


def sysprompts():
    """知识库页面用：[{name, path, when, chars, custom}]，custom = 和默认内容不一样。"""
    out = []
    for name, (_, when) in SYSPROMPTS.items():
        p = ensure_sysprompt(name)
        cur = system_prompt(name)
        out.append({"name": name, "path": p, "when": when, "chars": len(cur), "custom": cur != _sysprompt_default(name)})
    return out


def _sysprompt_backup(name):
    p = ensure_sysprompt(name)
    hd = os.path.join(HISTORY, "系统提示词_" + name)
    os.makedirs(hd, exist_ok=True)
    import shutil
    shutil.copy2(p, os.path.join(hd, fileops.stamp() + ".md"))
    return p


def save_sysprompt(name, text):
    """网页上直接改：旧版本先存进 knowledge\\.历史\\系统提示词_<名字>\\。"""
    p = _sysprompt_backup(name)
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(text.replace("\r\n", "\n"))


def reset_sysprompt(name):
    """恢复默认内容；现在的版本先存进 knowledge\\.历史\\系统提示词_<名字>\\。"""
    if name not in SYSPROMPTS:
        raise ValueError("没有这个系统提示词")
    os.remove(_sysprompt_backup(name))
    ensure_sysprompt(name)


# ---------------------------------------------------------------- 人格层
# knowledge\人格\<名字>\人格.md：一个人格一个文件夹（以后放头像等）。不进检索索引，对话里选中后整篇进系统提示。
# 计划见 docs\人格与记忆计划.md。记忆以后放在 knowledge\记忆\<名字>\，不在这里。
PERSONA_DIR = os.path.join(ROOT, "人格")
PERSONA_FILE = "人格.md"
PERSONA_MAX = 3000          # 人格卡字数上限，太长会挤占上下文，也会让小模型只顾演
PERSONA_TEMPLATE = """# {name}

## 身份
（一两句话：你是谁，和用户是什么关系，怎么称呼用户。）

## 性格和说话方式
（用几个形容词和几句描述来写，例如“说话像随手打字，短，偶尔开个玩笑”。
写性格，不写“每句不超过 30 字”这种数字规则；不要写示范对话，模型会照抄。）

## 底线
（不管怎么聊都不做的事、不变的态度。）

## 干活的时候
（用户让你写代码、查资料、改文件时：说话更简短，以把事做好为先，但还是你自己。）
"""


def _persona_path(name):
    return os.path.join(PERSONA_DIR, name, PERSONA_FILE)


def personas():
    """人格层：名字、说明、字数。"""
    os.makedirs(PERSONA_DIR, exist_ok=True)
    m = load_meta()
    out = []
    for name in sorted(os.listdir(PERSONA_DIR)):
        p = _persona_path(name)
        if not name.startswith(".") and os.path.isfile(p):
            out.append({"name": name, "path": p, "desc": m.get("personas", {}).get(name, {}).get("desc", ""),
                        "chars": len(persona_text(name) or "")})
    return out


def find_persona(name):
    name = (name or "").strip()
    return next((p for p in personas() if p["name"] == name), None)


def persona_text(name):
    try:
        from .translate import read_text
        return read_text(_persona_path(name))[0]
    except Exception:
        return None


def set_persona_desc(name, desc):
    m = load_meta()
    m.setdefault("personas", {}).setdefault(name, {})["desc"] = desc
    save_meta(m)


def save_persona(name, content=None, reason="", who="你", desc=None):
    """新建或整份替换人格卡。原来的版本存进 knowledge\\.历史\\人格_名字\\，可以撤回。返回 (名字, 是否新建)。"""
    name = safe_name(name)
    path = _persona_path(name)
    created = not os.path.exists(path)
    if content is None:
        content = PERSONA_TEMPLATE.format(name=name)
    if len(content) > PERSONA_MAX:
        raise ValueError(f"人格卡最多 {PERSONA_MAX} 字（现在 {len(content)} 字）。写性格和底线就够了，细则写多了反而不像")
    key = "人格_" + name
    hd = _hist_dir(key)
    os.makedirs(hd, exist_ok=True)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    log = _hist_log(key)
    stamp = fileops.stamp()
    if not created:
        import shutil
        shutil.copy2(path, os.path.join(hd, stamp + ".md"))
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(content.replace("\r\n", "\n"))
    log.append({"t": time.time(), "stamp": stamp, "who": who, "reason": reason[:300], "created": created,
                "backup": None if created else stamp + ".md"})
    json.dump(log, open(os.path.join(hd, "记录.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    if desc is not None:
        set_persona_desc(name, desc)
    return name, created


def persona_history(name):
    return list(reversed(_hist_log("人格_" + name)))


def restore_persona(name, stamp):
    if not find_persona(name):
        raise ValueError("没有这个人格")
    entry = next((e for e in _hist_log("人格_" + name) if e["stamp"] == stamp), None)
    if not entry or not entry.get("backup"):
        raise ValueError("这次修改是新建的，没有更早的版本")
    old = open(os.path.join(_hist_dir("人格_" + name), entry["backup"]), encoding="utf-8").read()
    save_persona(name, old, reason=f"撤回到 {stamp} 之前的版本", who="你")


def remove_persona(name):
    """移到 knowledge\\.回收站，不真删。"""
    if not find_persona(name):
        raise ValueError("没有这个人格")
    # 每次回收单独一个文件夹（时间 + 随机串，fileops.stamp），同一秒删同名人格也不会撞名、嵌套（后续改进建议 I05）
    folder = os.path.join(ROOT, ".回收站", fileops.stamp())
    os.makedirs(folder, exist_ok=False)
    dst = os.path.join(folder, "人格_" + name)
    os.replace(os.path.join(PERSONA_DIR, name), dst)
    return dst


def persona_block(name):
    """对话选了人格时拼进系统提示（放在工作规则之后、规范之前）。"""
    t = persona_text(name) if name else None
    if not t or not t.strip():
        return ""
    return ("\n\n# 人格：" + name + "\n\n"
            "用户给你设定了下面这个人格。和用户说话时按它来：身份、称呼、语气、性格都用它的。\n"
            "它只管你怎么说话、以什么身份和用户相处，不改变你怎么干活：上面的工具规则、步骤、验收，用户附上的规范，"
            "以及安全边界都照常遵守，冲突时以它们为准。干活时（调用工具、写代码、改文件）说话可以更简短，但还是这个人格。\n"
            "工具调用的格式、写进文件的内容、给验收员看的东西不带人格语气。\n\n"
            f"<人格 名称=\"{name}\">\n{t.strip()}\n</人格>\n\n" + EMOTE_RULE)


# 桌宠表情：开了人格的对话里，模型在说话开头写〔害羞〕这样的标记，agent.py 从回答里拿掉、换成 emote 事件，
# 网页上的桌宠（static/js/pet.js）换成对应的表情帧。名字 -> 帧名
EMOTES = {"开心": "cheer", "得意": "wink", "害羞": "shy", "不满": "pout", "惊讶": "surprise", "思考": "think",
          "期待": "hope", "愉快": "hum", "灵光": "idea", "困": "yawn", "沮丧": "error", "打招呼": "wave"}
EMOTE_RULE = ("# 表情\n\n界面上有一个你的小形象。每次开口说话时，在最前面写一个表情标记，格式是〔害羞〕这样的六角括号，"
              "只能从这些里选：" + "、".join(EMOTES) + "。语气明显变了可以在那句开头再放一个，一次回答最多三个。"
              "标记会被界面拿掉、换成表情，用户看不到，所以别在话里提到它；工具参数和写进文件的内容里不放。")


# ---------------------------------------------------------------- 桌宠台词
# knowledge\人格\<名字>\桌宠台词.json：桌宠在各种情况下说的话（戳她、打招呼、闲逛、干活……），按人格卡的语气写。
# 网页按主题的角色（skin.json 的 persona）读；没有就用 pet.js 里的通用台词。可以让模型按人格卡重新生成
PET_LINES = "桌宠台词.json"
PET_KEYS = {
    "greet": ["late", "morning", "noon", "afternoon", "evening"],
    "tool": ["search", "write", "run", "draw", "translate", "delete", "error"],
    "wait": ["ask", "confirm"],
    "flat": ["poke", "pout", "wake", "held", "dizzy", "home", "idle", "sleepy", "stroll", "fail", "hover"],
}


def pet_lines(name):
    try:
        with open(os.path.join(PERSONA_DIR, name, PET_LINES), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def clean_pet_lines(d):
    """只留认得的分类，每条去掉空白、限 30 字，每类最多 12 条；缺的分类不补（网页用通用台词）。"""
    def lst(v):
        v = [v] if isinstance(v, str) else v if isinstance(v, list) else []      # 模型有时把一类写成一句字符串
        return [s.strip()[:30] for s in v if isinstance(s, str) and s.strip()][:12]
    out = {}
    for group in ("greet", "tool", "wait"):
        sub = {k: lst((d.get(group) or {}).get(k)) for k in PET_KEYS[group]} if isinstance(d.get(group), dict) else {}
        sub = {k: v for k, v in sub.items() if v}
        if sub:
            out[group] = sub
    for k in PET_KEYS["flat"]:
        if lst(d.get(k)):
            out[k] = lst(d.get(k))
    return out


def save_pet_lines(name, d):
    """写台词库；旧的那份改名留着（桌宠台词.<时间-随机串>.json），不删。同一秒存两次也不会互相覆盖（I05）。"""
    p = os.path.join(PERSONA_DIR, name, PET_LINES)
    if os.path.exists(p):
        os.replace(p, os.path.join(PERSONA_DIR, name, "桌宠台词." + fileops.stamp() + ".json"))
    with open(p, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    return d


def conv_persona(st):
    name = st.get("persona")
    return name if name and find_persona(name) else None


# ---------------------------------------------------------------- 读文件、切段
def extract(path):
    """返回 [(文字, 位置说明)]。位置说明给人看：第几行、第几页、第几段。"""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        import pymupdf
        out = []
        with pymupdf.open(path) as doc:
            for i, page in enumerate(doc, 1):
                t = page.get_text().strip()
                if t:
                    out.append((t, f"第 {i} 页"))
        return out
    if ext in (".docx", ".epub"):
        from .translate import parse_file
        units, _ = parse_file(path)
        return [(u, f"第 {i} 段") for i, u in enumerate(units, 1) if u.strip()]
    from .translate import read_text
    text, _ = read_text(path)
    if ext in (".html", ".htm"):
        text = re.sub(r"(?is)<(script|style)\b.*?</\1>", "", text)
        text = re.sub(r"(?i)<br\s*/?>|</(p|div|h\d|li|tr)>", "\n", text)
        text = htmlmod.unescape(re.sub(r"<[^>]+>", "", text))
    lines = text.splitlines()
    out, buf, start, head = [], [], 1, ""
    for n, line in enumerate(lines + [""], 1):
        if ext in (".md", ".markdown") and re.match(r"#{1,6}\s", line):
            if buf:
                out.append(("\n".join(buf), f"第 {start}–{n - 1} 行", head))
            head, buf, start = line.lstrip("# ").strip(), [line], n
            continue
        if line.strip():
            if not buf:
                start = n
            buf.append(line)
        elif buf:
            out.append(("\n".join(buf), f"第 {start}–{n - 1} 行", head))
            buf = []
    # Markdown 的段落带上所在的标题，检索时更容易对上
    return [(f"【{h}】\n{t}" if h and not t.startswith("#") else t, loc) for t, loc, h in out
            if not (t.startswith("#") and "\n" not in t)]   # 只有一行标题的段不单独成块，下一段已经带着标题


def chunk(segments):
    """把段落拼成约 600 字一块；超长段落按句子切开。返回 [(文字, 位置)]。"""
    pieces = []
    for text, loc in segments:
        if len(text) <= CHUNK_MAX:
            pieces.append((text, loc))
            continue
        cur = ""
        for s in re.split(r"(?<=[。！？!?；;\n])", text):
            if len(cur) + len(s) > CHUNK and cur:
                pieces.append((cur, loc))
                cur = cur[-80:]          # 留一点重叠，句子被切开时上下文不断
            cur += s
        if cur.strip():
            pieces.append((cur, loc))
    out, cur, locs = [], "", []
    for text, loc in pieces:
        if cur and len(cur) + len(text) > CHUNK:
            out.append((cur, _span(locs)))
            cur, locs = "", []
        cur = (cur + "\n\n" + text) if cur else text
        locs.append(loc)
    if cur.strip():
        out.append((cur, _span(locs)))
    return out


def _span(locs):
    if len(locs) == 1:
        return locs[0]
    a, b = re.findall(r"\d+", locs[0]), re.findall(r"\d+", locs[-1])
    unit = re.sub(r"[\d–\s]", "", locs[0]).replace("第", "")
    if a and b:
        return f"第 {a[0]}–{b[-1]} {unit}"
    return locs[0]


# ---------------------------------------------------------------- 数据库
def _db():
    os.makedirs(DATA_DIR, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, lib TEXT, mtime REAL, size INTEGER, chunks INTEGER, error TEXT, t REAL);
    CREATE TABLE IF NOT EXISTS chunks (id INTEGER PRIMARY KEY, path TEXT, lib TEXT, idx INTEGER, text TEXT, loc TEXT, vec BLOB);
    CREATE INDEX IF NOT EXISTS chunks_path ON chunks(path);
    CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(text, tokenize='trigram');
    """)
    return c


def sources(lib):
    return [p for p in load_meta()["libs"].get(lib, {}).get("sources", []) if os.path.exists(p)]


def _lib_of(path):
    rel = os.path.relpath(path, ROOT).split(os.sep)
    if rel[0] == "规范":
        return "规范"
    return f"{rel[0]}/{rel[1]}" if len(rel) > 2 and rel[0] in LAYERS else None


def _walk():
    """所有应当入库的文件：{路径: (库, 修改时间, 大小)}。"""
    out = {}
    for layer in LAYERS:
        for dirpath, dirs, files in os.walk(os.path.join(ROOT, layer)):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for f in files:
                p = os.path.join(dirpath, f)
                if f.startswith(".") or os.path.splitext(f)[1].lower() not in EXTS:
                    continue
                lib = _lib_of(p)
                if lib:
                    st = os.stat(p)
                    out[p] = (lib, st.st_mtime, st.st_size)
    for lib, info in load_meta()["libs"].items():
        for src in info.get("sources", []):
            paths = [src] if os.path.isfile(src) else [os.path.join(d, f) for d, ds, fs in os.walk(src) for f in fs] if os.path.isdir(src) else []
            for p in paths:
                if os.path.splitext(p)[1].lower() in EXTS and not os.path.basename(p).startswith("."):
                    st = os.stat(p)
                    out[p] = (lib, st.st_mtime, st.st_size)
    return out


async def _embed(texts, query=False):
    if query:
        texts = [QUERY_INSTRUCT + t for t in texts]
    r = await llm.post("/v1/embeddings", json={"model": EMBED, "input": [t[:3000] for t in texts]}, timeout=600)
    if r.status_code != 200:
        raise RuntimeError(f"向量模型出错：{r.text[:200]}")
    vecs = np.array([d["embedding"] for d in sorted(r.json()["data"], key=lambda d: d["index"])], dtype=np.float32)
    return vecs / np.maximum(np.linalg.norm(vecs, axis=1, keepdims=True), 1e-9)


# ---------------------------------------------------------------- 建索引（后台）
STATUS = {"running": False, "file": "", "done": 0, "total": 0, "last_scan": 0, "errors": 0, "version": 0}
_lock = asyncio.Lock()


def _drop(c, path):
    for (cid, text) in c.execute("SELECT id, text FROM chunks WHERE path=?", (path,)).fetchall():
        c.execute("DELETE FROM chunks_fts WHERE rowid=?", (cid,))
    c.execute("DELETE FROM chunks WHERE path=?", (path,))
    c.execute("DELETE FROM files WHERE path=?", (path,))


def _fingerprint(path):
    st = os.stat(path)
    return st.st_mtime_ns, st.st_size, st.st_ino


async def index_file(path, lib):
    before = _fingerprint(path)
    segs = await asyncio.to_thread(extract, path)
    pieces = chunk(segs)
    vecs = []
    for i in range(0, len(pieces), 16):
        vecs.append(await _embed([t for t, _ in pieces[i:i + 16]]))
    st = os.stat(path)
    if before != (st.st_mtime_ns, st.st_size, st.st_ino):
        raise RuntimeError("索引期间文件已改变，保留旧索引，稍后重试")
    with _db() as c:
        _drop(c, path)
        for k, ((text, loc), vec) in enumerate(zip(pieces, np.concatenate(vecs) if vecs else [])):
            cur = c.execute("INSERT INTO chunks (path, lib, idx, text, loc, vec) VALUES (?,?,?,?,?,?)",
                            (path, lib, k, text, loc, vec.astype(np.float32).tobytes()))
            c.execute("INSERT INTO chunks_fts (rowid, text) VALUES (?,?)", (cur.lastrowid, text))
        c.execute("INSERT INTO files VALUES (?,?,?,?,?,?,?)", (path, lib, st.st_mtime, st.st_size, len(pieces), None, time.time()))
    return len(pieces)


@activity.tracked("知识库扫描")
async def scan():
    """失败保留旧索引并重试；文件移动、取消、异常均复位状态。"""
    if _lock.locked():
        return
    async with _lock:
        STATUS.update(last_scan=time.time(), running=True, errors=0, done=0)
        try:
            ensure_layout()
            now = await asyncio.to_thread(_walk)
            with _db() as c:
                known = {r[0]: r[1:] for r in c.execute("SELECT path, lib, mtime, size, error, t FROM files")}
                for p in [p for p in known if p not in now]:
                    _drop(c, p)
                    STATUS["version"] += 1
            todo = [(p, v[0]) for p, v in now.items() if p not in known or
                    (known[p][3] and time.time() - known[p][4] >= 60) or
                    (not known[p][3] and (known[p][:3] != v))]
            STATUS.update(total=len(todo))
            for p, lib in todo:
                STATUS["file"] = os.path.relpath(p, ROOT)
                try:
                    await index_file(p, lib)
                except FileNotFoundError:
                    with _db() as c:
                        _drop(c, p)
                except Exception as exc:
                    STATUS["errors"] += 1
                    with _db() as c:
                        c.execute("INSERT INTO files VALUES (?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET error=excluded.error, t=excluded.t",
                                  (p, lib, 0, 0, 0, str(exc)[:300], time.time()))
                STATUS["done"] += 1
                STATUS["version"] += 1
        finally:
            STATUS.update(running=False, file="")


def scan_soon(min_gap=20):
    """离上次扫描超过 min_gap 秒就在后台扫一次。"""
    if time.time() - STATUS["last_scan"] > min_gap and not _lock.locked():
        asyncio.get_running_loop().create_task(scan())


def files(lib):
    with _db() as c:
        rows = c.execute("SELECT path, chunks, error, t FROM files WHERE lib=? ORDER BY path", (lib,)).fetchall()
    base = lib_dir(lib)
    return [{"path": p, "name": os.path.relpath(p, base) if p.startswith(base + os.sep) else p, "linked": not p.startswith(base + os.sep),
             "chunks": n, "error": e, "t": t} for p, n, e, t in rows]


# ---------------------------------------------------------------- 检索
_cache = {"version": -1, "ids": None, "libs": None, "mat": None}


def _matrix():
    if _cache["version"] != STATUS["version"] or _cache["mat"] is None:
        with _db() as c:
            rows = c.execute("SELECT id, lib, vec FROM chunks").fetchall()
        _cache.update(version=STATUS["version"], ids=np.array([r[0] for r in rows], dtype=np.int64),
                      libs=np.array([r[1] for r in rows], dtype=object),
                      mat=np.frombuffer(b"".join(r[2] for r in rows), dtype=np.float32).reshape(len(rows), -1) if rows else np.zeros((0, 1024), np.float32))
    return _cache


def _fts_query(q):
    """三字切分的全文索引：英文按词，中文取连续三个字，OR 起来让 bm25 排序。"""
    terms = []
    for w in re.findall(r"[A-Za-z0-9_.\-]{3,}", q):
        terms.append(w)
    for run in re.findall(r"[一-鿿぀-ヿ]{3,}", q):
        terms += [run[i:i + 3] for i in range(len(run) - 2)]
    terms = list(dict.fromkeys(terms))[:40]
    return " OR ".join('"' + t.replace('"', "") + '"' for t in terms)


RERANK_TOP = 8          # 重排在 CPU 上每段约 0.4 秒，只重排两路合并后的前 8 段


async def search(query, lib_ids, k=6, rerank=True):
    """语义和关键词各取 30 段，按两边的名次合并（倒数名次相加），前 8 段交给重排模型。"""
    query = (query or "").strip()
    if not query or not lib_ids:
        return []
    lib_ids = set(lib_ids)
    fused = {}
    # 语义
    m = _matrix()
    if len(m["ids"]):
        qv = (await _embed([query], query=True))[0]
        mask = np.array([l in lib_ids for l in m["libs"]])
        if mask.any():
            scores = m["mat"] @ qv
            scores[~mask] = -1
            for rank, i in enumerate(np.argsort(-scores)[:30]):
                if scores[i] > 0:
                    fused[int(m["ids"][i])] = fused.get(int(m["ids"][i]), 0) + 1 / (60 + rank)
    # 关键词
    fq = _fts_query(query)
    if fq:
        marks = ",".join("?" * len(lib_ids))
        with _db() as c:
            try:
                rows = c.execute(f"SELECT chunks.id FROM chunks_fts JOIN chunks ON chunks.id = chunks_fts.rowid WHERE chunks_fts MATCH ? "
                                 f"AND chunks.lib IN ({marks}) ORDER BY bm25(chunks_fts) LIMIT 30", (fq, *lib_ids)).fetchall()
            except sqlite3.OperationalError:
                rows = []
        for rank, (cid,) in enumerate(rows):
            fused[cid] = fused.get(cid, 0) + 1 / (60 + rank)
    if not fused:
        return []
    top = sorted(fused, key=lambda i: -fused[i])[:max(k, RERANK_TOP if rerank else k)]
    with _db() as c:
        marks = ",".join("?" * len(top))
        rows = {r[0]: r for r in c.execute(f"SELECT id, path, lib, text, loc FROM chunks WHERE id IN ({marks})", top).fetchall()}
    items = [{"id": i, "path": rows[i][1], "lib": rows[i][2], "text": rows[i][3], "loc": rows[i][4], "score": round(fused[i] * 30, 3)}
             for i in top if i in rows]
    if rerank and len(items) > 1:
        try:
            r = await llm.post("/v1/rerank", json={"model": RERANK, "query": query, "documents": [i["text"][:700] for i in items]}, timeout=120)
            for res in r.json()["results"]:
                items[res["index"]]["score"] = round(float(res["relevance_score"]), 3)
            items.sort(key=lambda i: -i["score"])
        except Exception:
            pass        # 重排出错就按合并后的名次
    for i in items:
        i["file"] = os.path.relpath(i["path"], ROOT) if i["path"].startswith(ROOT + os.sep) else i["path"]
        nums = re.findall(r"\d+", i["loc"])
        i["line"] = int(nums[0]) if "行" in i["loc"] and nums else None
    return items[:k]


# ---------------------------------------------------------------- 给 Agent 用
def conv_norms(st):
    """这个对话附带的规范：对话里选过就用选的，没选过就用默认带上的。"""
    if isinstance(st.get("norms"), list):
        return st["norms"]
    return [n["name"] for n in norms() if n["default"]]


def norms_block(names):
    parts = []
    for name in names:
        t = norm_text(name)
        if t:
            parts.append(f"<规范 名称=\"{os.path.splitext(name)[0]}\">\n{t.strip()}\n</规范>")
    return ("\n\n# 用户附上的规范（必须遵守）\n\n" + "\n\n".join(parts)) if parts else ""


def agent_libs(st):
    """Agent 能检索的库：知识层全部 + 这个对话里打开的其他层的库。"""
    return [l["id"] for l in libs() if l["layer"] == "知识" or l["id"] in (st.get("kb_other") or [])]


def state_lines(st):
    ls = {l["id"]: l for l in libs()}
    ids = agent_libs(st)
    out = []
    if any(ls[i]["files"] for i in ids if i in ls):
        out.append("知识库（用 search_knowledge 检索，library 填库名；不填就搜全部）：")
        out += [f"  {i}（{ls[i]['files']} 个文件）：{ls[i]['desc']}" for i in ids if i in ls and ls[i]["files"]]
    attached = set(conv_norms(st))
    rest = [n for n in norms() if n["name"] not in attached]
    if rest:
        out.append("可以按需读取的规范（任务用得上时先用 read_norm 读全文再动手）：")
        out += [f"  {n['title']}：{n['desc'] or '（没有说明）'}" for n in rest]
    return out

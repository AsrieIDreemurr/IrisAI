"""路径归属判断：沙箱内随意；授权目录按只读 / 可写；其他位置一律要用户确认。"""
import contextvars
import os
import re

from .config import AI_ROOT, SANDBOX, load_settings

KB_ROOT = os.path.join(AI_ROOT, "knowledge")


# 当前运行所属对话的项目文件夹：{"path": ...}。每次运行在自己的异步任务里设置，不同对话互不影响；
# 用字典是为了起好标题后给文件夹改名时，同一次运行里的各处都能看到新路径
PROJECT = contextvars.ContextVar("project", default=None)
PROJECTS = os.path.join(SANDBOX, "项目")


def base():
    h = PROJECT.get()
    return h["path"] if h else SANDBOX


def _into_project(p):
    """沙箱根目录下还不存在的东西（新文件、新文件夹）放进项目文件夹；已经存在的照旧。
    模型常照着界面写法写“沙箱\\index.html”，这样新文件也会落在这个对话自己的文件夹里。"""
    proj = base()
    if proj == SANDBOX or not _within(p, SANDBOX):
        return p
    rel = os.path.relpath(p, os.path.realpath(SANDBOX))
    first = rel.split(os.sep)[0]
    parts = rel.split(os.sep)
    if first == "项目" and len(parts) > 1:
        # “沙箱\项目\xx”：xx 是已有的项目文件夹就照旧；不是（模型漏写了项目名）就放进当前项目
        if os.path.isdir(os.path.join(PROJECTS, parts[1])):
            return p
        return os.path.join(proj, *parts[1:])
    if rel == "." or first == "项目" or os.path.exists(os.path.join(SANDBOX, first)):
        return p
    return os.path.join(proj, rel)


def resolve(p):
    """相对路径按当前项目文件夹算（没有项目时按沙箱），项目里没有而沙箱根目录下有的，用根目录那个；
    “沙箱\\xx”写法从沙箱根目录算，但还不存在的新东西放进项目文件夹。"""
    p = (p or "").strip().strip('"`').replace("/", os.sep)
    if p == "沙箱":
        return os.path.realpath(SANDBOX)
    if p.startswith("沙箱" + os.sep):   # 界面上显示的“沙箱\\xx”写法
        p = os.path.join(SANDBOX, p[3:])
    if not p:
        p = base()
    rel = None if os.path.isabs(p) else p
    if rel is not None:
        p = os.path.join(base(), p)
    out = os.path.realpath(os.path.normpath(_into_project(os.path.realpath(os.path.normpath(p)))))
    if rel is not None and base() != SANDBOX and not os.path.exists(out):
        # 模型常照着列目录的显示（“沙箱\\翻译\\a.srt”）去掉“沙箱\\”写成“翻译\\a.srt”：项目里没有、沙箱根目录下有，就用根目录那个。
        # 两处都没有（要新建）时照旧放进项目文件夹。
        alt = os.path.realpath(os.path.normpath(os.path.join(SANDBOX, rel)))
        if _within(alt, SANDBOX) and os.path.exists(alt):
            return alt
    return out


def _project_name(text, limit=20):
    first = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return re.sub(r'[\\/:*?"<>|\r\n\t“”]', "", first)[:limit].strip(" .") or "项目"


def _free(name):
    p, k = os.path.join(PROJECTS, name), 2
    while os.path.exists(p):
        p, k = os.path.join(PROJECTS, f"{name}-{k}"), k + 1
    return p


def new_project(name):
    """建项目文件夹：沙箱\\项目\\对话标题（没有标题时用第一句话的前 24 个字），重名加 -2、-3。"""
    from .fileops import component
    title = _project_name(name, 24)
    try:
        component(title)
    except ValueError:
        title = "项目-" + title
    while True:
        p = _free(title)
        try:
            os.makedirs(p)
            return p
        except FileExistsError:
            continue


def _within(path, root):
    path, root = os.path.normcase(path), os.path.normcase(os.path.realpath(root))
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def roots():
    """文件管理器显示的根目录。"""
    out = [{"name": "沙箱", "path": SANDBOX, "mode": "rw", "sandbox": True},
           {"name": "知识库", "path": KB_ROOT, "mode": "ro", "sandbox": False}]
    for d in load_settings()["allowed_dirs"]:
        out.append({"name": d.get("name") or d["path"], "path": os.path.normpath(d["path"]), "mode": d.get("mode", "ro"), "sandbox": False})
    return out


def zone(path):
    """返回 'sandbox' / 'rw' / 'ro' / 'outside'。"""
    if _within(path, SANDBOX):
        return "sandbox"
    for r in roots()[1:]:
        if _within(path, r["path"]):
            return r["mode"]
    if any(_within(path, s) for s in _kb_sources()):
        return "ro"             # 知识库关联的外部文件夹：只读
    return "outside"


def _kb_sources():
    try:
        import json
        m = json.load(open(os.path.join(KB_ROOT, "库说明.json"), encoding="utf-8"))
        return [s for l in m.get("libs", {}).values() for s in l.get("sources", [])]
    except (OSError, ValueError):
        return []


def readable(path):
    return zone(path) in ("sandbox", "rw", "ro")


def writable(path):
    return zone(path) in ("sandbox", "rw")


def display(path):
    """给人看的路径：沙箱内显示相对路径。"""
    if _within(path, SANDBOX):
        rel = os.path.relpath(path, SANDBOX)
        return "沙箱" if rel == "." else "沙箱\\" + rel
    return path

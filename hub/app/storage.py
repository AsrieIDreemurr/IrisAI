"""存储：哪些东西占了多少空间，能放心挪走的一键挪进回收站（Claude，2026-11-13，后续改进建议 I12）。

2026-11-22 起“待删除”和“回收站”合并成一个回收站（用户要的）：测试副本、浏览器资料挪到 沙箱\\.回收站\\存储清理_<日期>\\<类别>\\；
以前挪出去的 _待删除_* 文件夹（在 trash_dir 下）也列在回收站里，一起清空。
能挪的只有两类，别的只显示：
  tests    测试副本：沙箱里 .xxx-test、.release-<日期>、.audit-fixes-<日期>（不含 .strata-test，几个脚本还在读里面的配置）
  browser  网页检查用的浏览器资料：沙箱\\.运行\\chrome-profile*（view_result 每次新建，用完留下）
挪之前要求服务空闲（没有对话、后台任务、模型调用在跑），30 分钟内改过的文件夹不动（可能正在用）。
截图虽然能挪，但旧对话里引用了它们，挪走后旧对话里的图就显示不出来，所以只显示不挪。
"""
import os
import re
import shutil
import time

from .config import AI_ROOT, COMFY_DIR, SANDBOX, TRASH_DIR, load_settings
from .fileops import remove

ROOT = os.path.normpath(AI_ROOT)
TRASH_PREFIX = "_待删除_"
RECYCLE = {"sandbox": os.path.join(SANDBOX, ".回收站"), "kb": os.path.join(ROOT, "knowledge", ".回收站")}
COMFY_MODELS = os.path.join(COMFY_DIR, "models")
TEST_RE = re.compile(r"^\.(?!strata-)[\w\-]+-test$|^\.release-\d{8}$|^\.audit-fixes-\d{8}$")
RECENT = 30 * 60
_cache = {"t": 0, "data": None}


def _walk(path):
    """(字节数, 文件数, 最新修改时间)。符号链接不跟。"""
    size = count = 0
    newest = 0.0
    stack = [path]
    while stack:
        p = stack.pop()
        try:
            with os.scandir(p) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            st = e.stat(follow_symlinks=False)
                            size += st.st_size
                            count += 1
                            newest = max(newest, st.st_mtime)
                    except OSError:
                        pass
        except OSError:
            pass
    return size, count, newest


def _item(path, name=None, **extra):
    size, count, newest = _walk(path) if os.path.isdir(path) else ((os.path.getsize(path), 1, os.path.getmtime(path)) if os.path.isfile(path) else (0, 0, 0))
    return {"name": name or os.path.basename(path), "path": path, "bytes": size, "files": count, "newest": newest, **extra}


def _children(folder, pick=lambda e: True, **extra):
    if not os.path.isdir(folder):
        return []
    with os.scandir(folder) as it:
        return [_item(e.path, **extra) for e in it if pick(e)]


def trash_parent():
    """“挪到待删除”时在哪个文件夹下建 _待删除_<说明>：设置页改过的优先（文件夹不在了就退回 hub.json 的）。"""
    p = load_settings().get("trash_dir")
    return os.path.normpath(p) if isinstance(p, str) and os.path.isabs(p) and os.path.isdir(p) else TRASH_DIR


def check_trash_dir(p):
    """设置页填的待删除位置：要是已有的文件夹、写绝对路径。返回规范化的路径，不行抛 ValueError。"""
    p = os.path.normpath(os.path.expandvars(str(p).strip().strip('"')))
    if not os.path.isabs(p):
        raise ValueError("要写完整路径，例如 D:\\待删除")
    if not os.path.isdir(p):
        raise ValueError("这个文件夹不存在：" + p)
    return p


def _legacy(tp):
    """以前挪出去的 _待删除_* 文件夹，名字去掉前缀显示。"""
    return [dict(i, name=i["name"][len(TRASH_PREFIX):] or i["name"]) for i in _children(tp, lambda e: e.is_dir() and e.name.startswith(TRASH_PREFIX))]


def _tests():
    return _children(SANDBOX, lambda e: e.is_dir() and TEST_RE.match(e.name))


def _browser():
    return _children(os.path.join(SANDBOX, ".运行"), lambda e: e.is_dir() and e.name.startswith("chrome-profile"))


def _models_in_use():
    """正式调度配置和它调用的启动器（run_*.py）的文字，统一成正斜杠；模型文件夹出现在里面就算在用。"""
    texts = []
    paths = [os.path.join(ROOT, "config", "llama-swap.yaml")]
    sd = os.path.join(ROOT, "scripts")
    if os.path.isdir(sd):
        paths += [os.path.join(sd, n) for n in os.listdir(sd) if n.startswith("run_") and n.endswith(".py")]
    for p in paths:
        try:
            with open(p, encoding="utf-8") as f:
                texts.append(f.read())
        except OSError:
            pass
    return "\n".join(texts).replace("\\", "/")


def scan():
    """各类的占用（会走一遍文件夹，几秒钟；结果留 60 秒）。"""
    tp = trash_parent()
    if _cache["data"] and time.time() - _cache["t"] < 60 and _cache["data"]["trash_parent"] == tp:
        return _cache["data"]
    yaml = _models_in_use()
    models = []
    for kind in ("llm", "ocr", "embed"):
        for it in _children(os.path.join(ROOT, "models", kind), lambda e: e.is_dir()):
            it["in_use"] = f"{kind}/{it['name']}/" in yaml or f"{{{kind}}}/{it['name']}/" in yaml or f"models/{kind}/{it['name']}" in yaml
            models.append(it)
    cats = [
        {"id": "models", "name": "本地模型", "desc": "对话、翻译、识字、知识库用的模型。“在用”= 正式调度配置里有它", "items": models},
        {"id": "comfy", "name": "图像模型（ComfyUI）", "desc": COMFY_MODELS, "items": _children(COMFY_MODELS, lambda e: e.is_dir())},
        {"id": "images", "name": "生成的图片和视频", "desc": "沙箱\\图片、沙箱\\视频，你的作品，只显示",
         "items": [_item(os.path.join(SANDBOX, "图片"), "图片"), _item(os.path.join(SANDBOX, "视频"), "视频")]},
        {"id": "shots", "name": "Agent 截图", "desc": "检查网页时拍的截图。旧对话里引用了它们，挪走后旧对话里的图会显示不出来，所以只显示",
         "items": [_item(os.path.join(SANDBOX, ".运行", "截图"), "截图")]},
        {"id": "browser", "name": "网页检查用的浏览器资料", "desc": "Agent 检查网页时临时建的浏览器配置，用完就没用了", "items": _browser(), "movable": True},
        {"id": "tests", "name": "测试副本", "desc": "测试时复制出来的服务、数据和工作区，测完就没用了", "items": _tests(), "movable": True},
        {"id": "recycle", "name": "回收站", "desc": "删掉、挪走的东西都先放这里，可以找回；清空后不能恢复", "deletable": True, "items": [
            _item(RECYCLE["sandbox"], "沙箱回收站"), _item(RECYCLE["kb"], "知识库回收站")] + _legacy(tp)},
        {"id": "logs", "name": "日志", "desc": "测试和运行记录，文档里引用了其中不少，只显示", "items": [_item(os.path.join(ROOT, "logs"), "logs")]},
        {"id": "data", "name": "数据", "desc": "对话、知识库索引、设置、主题原始素材", "items": _children(os.path.join(ROOT, "data"))},
    ]
    now = time.time()
    for c in cats:
        c["items"] = [i for i in c["items"] if i["files"] or c["id"] in ("images", "shots")]
        c["items"].sort(key=lambda i: -i["bytes"])
        c["bytes"] = sum(i["bytes"] for i in c["items"])
        c["movable"] = bool(c.get("movable"))
        c["deletable"] = bool(c.get("deletable"))
        for i in c["items"]:
            i["recent"] = now - i["newest"] < RECENT
            i["path_show"] = i["path"].replace(SANDBOX, "沙箱")
    disks = []
    for d in sorted({os.path.splitdrive(ROOT)[0] + "\\", "C:\\"}):
        try:
            u = shutil.disk_usage(d)
            disks.append({"drive": d.rstrip("\\"), "total": u.total, "used": u.used, "free": u.free})
        except OSError:
            pass
    data = {"cats": cats, "disks": disks, "trash_parent": tp, "trash_custom": tp != TRASH_DIR, "time": now}
    _cache.update(t=now, data=data)
    return data


def clean(cat):
    """把能挪的一类挪进回收站。返回 {moved: [...], skipped: [...], dest}。调用方先确认服务空闲。"""
    pick = {"tests": _tests, "browser": _browser}.get(cat)
    if not pick:
        raise ValueError("这一类不能挪")
    dest = os.path.join(RECYCLE["sandbox"], "存储清理_" + time.strftime("%Y%m%d"), cat)
    moved, skipped = [], []
    now = time.time()
    for it in pick():
        if now - it["newest"] < RECENT:
            skipped.append({"name": it["name"], "why": "30 分钟内改过，可能正在用"})
            continue
        os.makedirs(dest, exist_ok=True)
        target = os.path.join(dest, it["name"])
        n = 2
        while os.path.exists(target):
            target = os.path.join(dest, f"{it['name']}_{n}")
            n += 1
        try:
            shutil.move(it["path"], target)
            moved.append({"name": it["name"], "bytes": it["bytes"], "to": target})
        except OSError as e:
            skipped.append({"name": it["name"], "why": str(e)[:200]})
    _cache["data"] = None
    return {"moved": moved, "skipped": skipped, "dest": dest}


def delete(cat, path=None):
    """彻底删除（不能恢复）。cat 是 recycle（旧的 trash 也认）：path 是沙箱 / 知识库回收站，或者一个 _待删除_ 文件夹；
    不给 path 就全清（两个回收站里的东西 + 全部 _待删除_ 文件夹）。只认这些位置。
    返回 {deleted: [{name, bytes}], failed: [{name, why}]}。"""
    if cat not in ("recycle", "trash"):
        raise ValueError("这一类不能删")
    bases = [b for b in RECYCLE.values() if not path or os.path.normcase(b) == os.path.normcase(path)]
    targets = [(f"{os.path.basename(os.path.dirname(b))}\\{os.path.basename(b)}\\{e.name}", e.path)
               for b in bases if os.path.isdir(b) for e in os.scandir(b)]
    tp = trash_parent()
    old = [(e.name, e.path) for e in os.scandir(tp) if e.is_dir() and e.name.startswith(TRASH_PREFIX)
           and (not path or os.path.normcase(e.path) == os.path.normcase(path))] if os.path.isdir(tp) else []
    targets += old
    if path and not bases and not old:
        raise ValueError("不是这一类里的位置：" + str(path))
    deleted, failed = [], []
    for name, p in targets:
        try:
            deleted.append({"name": name, "bytes": remove(p)})
        except OSError as e:
            failed.append({"name": name, "why": str(e)[:200]})
    _cache["data"] = None
    return {"deleted": deleted, "failed": failed}

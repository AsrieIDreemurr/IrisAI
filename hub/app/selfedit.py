"""Agent 改进自己。

- 规范：update_norm 直接改 knowledge\\规范 里的文件，旧版本存进 knowledge\\.历史，知识库页面可以撤回。
- 界面和流程：不碰正式代码。Agent 在沙箱\\自改\\编号 里的副本上改，副本的测试实例（启动时挑空闲端口）验证，
  普通模式提交后由用户审核应用；高权限模式检查通过后直接应用。上线前备份，之后可以撤回。
- 两种模式：
  - 普通：只能改 hub\\static（界面）和 hub\\app（流程）；登录和路由（server.py）、路径和权限（paths.py、config.py、
    tools.py 里的 check）不能改。
  - 高权限（设置里打开）：副本包含 hub、config、scripts、knowledge、docs、README 和界面设置、生图预设，任何部分都能改，
    包括密钥配置和自改机制本身。普通模式仍保留文件和代码范围限制。
- 自改默认关闭；创建、测试、提交、应用和 Agent 修改规范都必须先开启。关闭后仍可查看记录、拒绝和撤回。
"""
import ast
import asyncio
import difflib
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import hmac
import threading

import httpx

from .config import AI_ROOT, DATA_DIR, SANDBOX, load_settings
from .paths import _within, display, resolve
from . import fileops

_record_lock = threading.RLock()

WORK = os.path.join(SANDBOX, "自改")
STORE = os.path.join(DATA_DIR, "selfedit.json")
BACKUPS = os.path.join(DATA_DIR, "selfedit")
PY = os.path.join(AI_ROOT, "hub", ".venv", "Scripts", "python.exe")

COPY_NORMAL = ["hub"]
COPY_FULL = ["hub", "config", "scripts", "knowledge", "docs", "README.md", "data/settings.json", "data/image_presets.json"]
SKIP_DIRS = {".venv", "__pycache__", ".回收站", ".历史", "node_modules", ".git"}
NORMAL_NEVER = {"config/api_key.txt", "hub/app/selfedit.py"}
GUARD = {}
GUARD_CALLS = {}
NORMAL_ALLOWED = ("hub/static/", "hub/app/")
NORMAL_LOCKED = {"hub/app/paths.py", "hub/app/config.py"}
NORMAL_GUARD = {"hub/app/tools.py": ["check", "_ABS"]}
RESTART_EXT = (".py", ".json", ".yaml", ".txt")    # 改了这些要重启服务；只改网页文件不用


def enabled():
    return load_settings().get("self_edit_enabled") is True


def full_mode():
    settings = load_settings()
    return settings.get("self_edit_enabled") is True and settings.get("self_edit_full") is True


def require_enabled(rec=None):
    if not enabled():
        raise ValueError("自改进已关闭，请先在设置里开启 Agent 自改进")
    if rec and rec["full"] and not full_mode():
        raise ValueError("这个副本使用高权限模式，请先在设置里开启高权限，或新建普通模式副本")


def check_tool_access(name, args):
    """常规文件工具也遵守自改开关；已有副本在关闭后不能继续修改或执行。"""
    if name in ("write_file", "edit_file", "make_dir", "delete_path"):
        targets = [args.get("path")]
    elif name == "move_path":
        targets = [args.get("src"), args.get("dst")]
    elif name == "run_python" and args.get("path"):
        targets = [args["path"]]
    elif name == "run_js" and args.get("page"):
        targets = [args["page"]]
    else:
        return
    for target in targets:
        path = resolve(target)
        if _within(path, WORK):
            require_enabled()
            for rec in listing():
                if _within(path, rec["copy"]):
                    require_enabled(rec)
                    break
        elif any(_within(path, os.path.join(AI_ROOT, root)) for root in (*COPY_FULL, "data")):
            require_enabled()
            raise ValueError("请先用 self_edit_start 建副本，只修改和运行副本中的代码，测试后用 self_edit_propose 提交；高权限直接应用，普通模式由用户审核")


def where_lines(rec, project=None):
    """告诉模型自改的文件在哪：副本路径和正式文件一一对应，项目文件夹里没有。"""
    copy = display(rec["copy"])
    out = [f"自改的文件都在副本 {copy} 里，不在项目文件夹{f'（{display(project)}）' if project else ''}里，不要去那里找。"
           f"副本里的路径就是正式文件的路径换个开头：正式的 hub\\static\\index.html 在副本里是 {copy}\\hub\\static\\index.html，"
           f"hub\\app\\agent.py 是 {copy}\\hub\\app\\agent.py。读、改、搜索都写这样的完整路径。"]
    try:
        ch = changes(rec)
    except OSError:
        ch = []
    if ch:
        kind = {"changed": "改过", "added": "新增", "deleted": "删除"}
        out.append("副本里已经动过的文件：" + "、".join(f"{copy}\\{rel.replace('/', chr(92))}（{kind[k]}）" for rel, k in ch[:12]))
    return out


def state_lines(st):
    rec = get(st.get("self_edit") or "")
    active = rec and rec["status"] in ("editing", "proposed")
    if not enabled():
        out = ["自改进：已关闭。不能修改规范或自身代码；需要用户在设置里开启后才能使用。"]
        if active:      # 关了也告诉它副本在哪：已经做的改动还在，读文件不受影响
            out.append(f"这个对话的自改副本还在：{display(rec['copy'])}，目标：{rec['goal']}。现在只能读，不能改、不能测试。")
            out += where_lines(rec, st.get("project"))
        return out
    mode = "高权限（副本中的所有内容都可改）" if full_mode() else "普通（只能改界面 hub\\static 和流程 hub\\app）"
    out = [f"自改进：已开启；模式：{mode}"]
    out.append("高权限副本通过检查后直接应用，无需审核；普通副本仍需用户审核应用。应用前自动备份，可以撤回。")
    if active:
        if rec["full"] and not full_mode():
            out.append("原高权限副本已暂停；需要用户重新开启高权限，或新建普通模式副本。")
        else:
            out.append(f"正在改的自改副本：{display(rec['copy'])}（{'高权限' if rec['full'] else '普通'}模式），目标：{rec['goal']}")
        out += where_lines(rec, st.get("project"))
    elif rec and rec["status"] == "applied":
        out.append(f"这个对话上次的自改副本（{display(rec['copy'])}）已经应用到正式文件。要接着改，先调用 self_edit_start 建新副本。")
    return out


# ---------------------------------------------------------------- 记录
def _load():
    try:
        return json.load(open(STORE, encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(d):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = STORE + "." + secrets.token_hex(8) + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STORE)


def get(eid):
    return _load().get(eid)


def put(rec):
    with _record_lock:
        d = _load()
        d[rec["id"]] = rec
        _save(d)
    return rec


def listing():
    return sorted(_load().values(), key=lambda r: -r.get("t", 0))


def _hash(p):
    try:
        return hashlib.sha1(open(p, "rb").read()).hexdigest()
    except OSError:
        return None


def _rel(p, base):
    return os.path.relpath(p, base).replace(os.sep, "/")


# ---------------------------------------------------------------- 建副本
def _files(base, roots, full=False):
    out = []
    for r in roots:
        src = os.path.join(base, *r.split("/"))
        if os.path.isfile(src):
            out.append(r)
            continue
        for d, dirs, fs in os.walk(src):
            dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
            for f in fs:
                rel = _rel(os.path.join(d, f), base)
                if (full or rel != "config/api_key.txt") and not f.endswith((".pyc", ".tmp")):
                    out.append(rel)
    return out


def start(goal, conv=None):
    require_enabled()
    full = full_mode()
    eid = time.strftime("%m%d-%H%M%S") + "-" + secrets.token_hex(3)
    copy = os.path.join(WORK, eid)
    base = {}
    for rel in _files(AI_ROOT, COPY_FULL if full else COPY_NORMAL, full):
        src = os.path.join(AI_ROOT, *rel.split("/"))
        dst = os.path.join(copy, *rel.split("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        base[rel] = _hash(src)
    rec = {"id": eid, "goal": goal[:500], "conv": conv, "full": full, "copy": copy, "base": base, "status": "editing", "t": time.time()}
    put(rec)
    return rec


def changes(rec):
    """副本相对建立时的改动：[(相对路径, 'changed'|'added'|'deleted')]。"""
    copy, base = rec["copy"], rec["base"]
    roots = [r for r in (COPY_FULL if rec["full"] else COPY_NORMAL) if os.path.exists(os.path.join(copy, *r.split("/")))]
    now = {r: _hash(os.path.join(copy, *r.split("/"))) for r in _files(copy, roots, rec["full"]) if not r.endswith(".check.mjs")}
    out = [(r, "changed") for r in base if r in now and now[r] != base[r]]
    out += [(r, "deleted") for r in base if r not in now]
    out += [(r, "added") for r in now if r not in base]
    return sorted(out)


def snapshot(rec):
    """记录接受检查的改动内容，防止测试后又改过的文件未经复验就应用。"""
    return {rel: [kind, _hash(os.path.join(rec["copy"], *rel.split("/")))] for rel, kind in changes(rec)}


# ---------------------------------------------------------------- 检查改动范围
def _symbols(src, names):
    """取出模块里这些名字（函数、类、顶层赋值）的源码，用来比较有没有被改。"""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None
    out = {}
    for node in tree.body:
        nm = getattr(node, "name", None)
        if nm is None and isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            nm = node.targets[0].id
        if nm in names:
            out[nm] = ast.get_source_segment(src, node)
    return out


def _read(p):
    try:
        return open(p, encoding="utf-8").read()
    except (OSError, UnicodeDecodeError):
        return None


def violations(rec):
    """不允许的改动和冲突。返回说明列表，空表示可以提交。"""
    try:
        require_enabled(rec)
    except ValueError as e:
        return [str(e)]
    full = rec["full"]
    out = []
    ch = changes(rec)
    for rel, kind in ch:
        if not full and rel in NORMAL_NEVER:
            out.append(f"{rel} 不能改")
            continue
        if not full and (not rel.startswith(NORMAL_ALLOWED) or rel in NORMAL_LOCKED or rel == "hub/server.py"):
            out.append(f"{rel}：普通模式只能改 hub/static 和 hub/app（paths.py、config.py 除外）；要改它得在设置里打开高权限")
            continue
        guards = {} if full else {**GUARD, **NORMAL_GUARD}
        if rel in guards:
            old = _read(os.path.join(AI_ROOT, *rel.split("/"))) or ""
            new = _read(os.path.join(rec["copy"], *rel.split("/"))) if kind != "deleted" else ""
            a, b = _symbols(old, guards[rel]), _symbols(new or "", guards[rel])
            if b is None:
                out.append(f"{rel} 有语法错误")
            elif a != b:
                which = "、".join(n for n in guards[rel] if (a or {}).get(n) != (b or {}).get(n))
                out.append(f"{rel} 里的 {which} 不能改" + ("（生图的未成年人保护检查）" if rel in GUARD else "（权限检查，高权限模式才能改）"))
            for call in GUARD_CALLS.get(rel, []):
                if (new or "").count(call) < old.count(call):
                    out.append(f"{rel} 里调用 {call.rstrip('(')} 的地方变少了（生图的未成年人保护检查不能绕过）")
        # 冲突：副本建立之后，正式文件又被改过
        prod = _hash(os.path.join(AI_ROOT, *rel.split("/")))
        if prod != rec["base"].get(rel):
            out.append(f"{rel} 在副本建立之后正式版本又改过，请重新建副本再改")
    return out


def diff_text(rec, limit=400_000):
    parts = []
    for rel, kind in changes(rec):
        old = _read(os.path.join(AI_ROOT, *rel.split("/"))) if kind != "added" else ""
        new = _read(os.path.join(rec["copy"], *rel.split("/"))) if kind != "deleted" else ""
        if old is None or new is None:
            parts.append(f"=== {rel}（二进制文件，{ {'changed': '改动', 'added': '新增', 'deleted': '删除'}[kind] }）\n")
            continue
        parts.append("".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"正式/{rel}", f"副本/{rel}", n=3)))
    return "\n".join(parts)[:limit]


# ---------------------------------------------------------------- 测试实例
_test = {"proc": None, "id": None, "pw": None, "port": None}


def test_base():
    return f"http://127.0.0.1:{_test['port']}" if _test["port"] else None


def _free_port():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_cookie():
    if not _test["pw"]:
        return None
    return hmac.new(_test["pw"].encode(), b"hub-login-v1", hashlib.sha256).hexdigest()


def stop_test():
    p = _test["proc"]
    if p and p.poll() is None:
        p.kill()
    # 服务重启过的话，之前起的测试实例没人管了：凡是从副本目录启动的 server.py 都停掉
    import psutil
    work = os.path.normcase(WORK) + os.sep
    for q in psutil.process_iter(["pid", "cmdline"]):
        try:
            if q.pid != os.getpid() and any(os.path.normcase(a).startswith(work) and a.endswith("server.py") for a in (q.info["cmdline"] or [])):
                q.kill()
        except (psutil.Error, TypeError):
            continue
    _test.update(proc=None, id=None, port=None)


async def run_checks(rec):
    """语法检查改过的文件，再用副本起一个测试实例，确认主要接口能用。返回 (是否通过, 说明列表)。"""
    errs = violations(rec)
    if errs:
        return False, errs
    copy, notes, ok = rec["copy"], [], True
    for rel, kind in changes(rec):
        if kind == "deleted":
            continue
        p = os.path.join(copy, *rel.split("/"))
        if rel.endswith(".py"):
            r = await asyncio.to_thread(subprocess.run, [PY, "-m", "py_compile", p], capture_output=True, text=True,
                                        encoding="utf-8", errors="replace", creationflags=0x08000000)
            if r.returncode:
                ok = False
                notes.append(f"{rel}：Python 语法错误：{r.stderr.strip()[-300:]}")
        elif rel.endswith((".js", ".mjs")):
            tmp = p + ".check.mjs"
            shutil.copyfile(p, tmp)
            r = await asyncio.to_thread(subprocess.run, ["node", "--check", tmp], capture_output=True, text=True,
                                        encoding="utf-8", errors="replace", creationflags=0x08000000)
            os.remove(tmp)
            if r.returncode:
                ok = False
                notes.append(f"{rel}：脚本语法错误：{r.stderr.strip()[-300:]}")
        elif rel.endswith(".json"):
            try:
                json.load(open(p, encoding="utf-8"))
            except ValueError as e:
                ok = False
                notes.append(f"{rel}：JSON 格式错误：{e}")
        elif rel.endswith((".yaml", ".yml")):
            try:
                import yaml
                with open(p, encoding="utf-8") as f:
                    yaml.safe_load(f)
            except Exception:
                ok = False
                notes.append(f"{rel}：YAML 检查未通过（诊断不回显配置内容）")
    if not ok:
        return False, notes
    errs = violations(rec)   # 语法检查期间用户可能关闭自改或撤销高权限
    if errs:
        return False, notes + errs
    # 起测试实例
    stop_test()
    pw = secrets.token_hex(12)
    port = _free_port()
    test_data = os.path.join(copy, ".测试数据", fileops.stamp())
    os.makedirs(test_data)
    settings_path = os.path.join(copy, "data", "settings.json")
    settings = json.load(open(settings_path, encoding="utf-8")) if os.path.isfile(settings_path) else load_settings()
    with open(os.path.join(test_data, "settings.json"), "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False)
    config_dir = os.path.join(copy, "config") if rec["full"] else os.path.join(AI_ROOT, "config")
    if rec["full"]:
        candidate = os.path.join(config_dir, "llama-swap.yaml")
        swap = os.path.join(AI_ROOT, "bin", "llama-swap-v265", "llama-swap.exe")
        if os.path.isfile(candidate):
            from .config import API_KEY
            try:
                result = await asyncio.to_thread(subprocess.run, [swap, "-config", candidate, "-validate"],
                    env={**os.environ, "LLAMA_SWAP_KEY": API_KEY}, capture_output=True, timeout=30, creationflags=0x08000000)
            except (OSError, subprocess.SubprocessError):
                return False, notes + ["候选调度配置无法验证"]
            if result.returncode:
                return False, notes + ["候选调度配置校验失败（不回显可能含密钥的诊断）"]
    env = {**os.environ, "HUB_PORT": str(port), "HUB_TEST_PASSWORD": pw, "HUB_AI_ROOT": AI_ROOT,
           "HUB_CONFIG_DIR": config_dir, "HUB_DATA_DIR": test_data,
           "HUB_SANDBOX": os.path.join(test_data, "sandbox"), "PYTHONIOENCODING": "utf-8"}
    log = open(os.path.join(copy, "测试实例.log"), "w", encoding="utf-8")
    proc = subprocess.Popen([PY, os.path.join(copy, "hub", "server.py")], cwd=copy, env=env, stdout=log, stderr=subprocess.STDOUT,
                            creationflags=0x08000000)
    _test.update(proc=proc, id=rec["id"], pw=pw, port=port)
    base = test_base()
    cookies = {"hub": test_cookie()}
    async with httpx.AsyncClient(timeout=10, cookies=cookies) as c:
        for _ in range(60):
            try:
                require_enabled(rec)
            except ValueError as e:
                stop_test()
                return False, notes + [str(e)]
            if proc.poll() is not None:
                log.flush()
                return False, notes + ["测试实例没有启动起来：" + (_read(os.path.join(copy, "测试实例.log")) or "")[-800:]]
            try:
                if (await c.get(base + "/login")).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.5)
        else:
            return False, notes + ["测试实例 30 秒内没有响应"]
        for path in ("/", "/api/config", "/api/convs", "/api/status", "/api/kb", "/api/img/info", "/api/tr/info"):
            try:
                require_enabled(rec)
            except ValueError as e:
                stop_test()
                return False, notes + [str(e)]
            try:
                r = await c.get(base + path)
                if r.status_code >= 400:
                    ok = False
                    notes.append(f"{path} 返回 {r.status_code}：{r.text[:200]}")
            except httpx.HTTPError as e:
                ok = False
                notes.append(f"{path} 请求失败：{e}")
    notes.append(f"测试实例在 {base} 运行中：{base}/app 是工作台（对话、翻译、识字、生图、知识库、设置），"
                 f"{base}/ 是入口页（左边功能入口，右边是随手问的小对话框，那里的联网开关不是工作台的）。"
                 "用 view_result 的 url 参数打开它截图、模拟操作，用 fetch_url 读页面或接口，都会自动登录。")
    return ok, notes


# ---------------------------------------------------------------- 提交、应用、撤回
def propose(rec, summary, tests):
    errs = violations(rec)
    if errs:
        return None, errs
    ch = changes(rec)
    if not ch:
        return None, ["副本没有任何改动"]
    d = diff_text(rec)
    diff_path = os.path.join(WORK, f"{rec['id']}.diff")
    open(diff_path, "w", encoding="utf-8").write(d)
    rec.update(status="proposed", summary=summary[:2000], files=ch, diff=diff_path, tests=tests, t_proposed=time.time(),
               restart=any(r.endswith(RESTART_EXT) and not r.startswith(("hub/static/", "knowledge/", "docs/")) for r, _ in ch))
    put(rec)
    return rec, []


def apply(eid):
    with _record_lock:
        return _apply(eid)


def _replace_copy(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + "." + secrets.token_hex(8) + ".tmp"
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)


def _apply(eid):
    require_enabled()
    rec = get(eid)
    if not rec or rec["status"] != "proposed":
        raise ValueError("这个改动现在不能应用")
    tests = rec.get("tests") or {}
    if (not tests.get("ok") or tests.get("snapshot") != snapshot(rec) or
            {tuple(x) for x in rec["files"]} != set(changes(rec))):
        raise ValueError("提交后的副本内容已改变或未通过检查，请重新测试并提交")
    errs = violations(rec)
    if errs:
        raise ValueError("；".join(errs))
    bdir = os.path.join(BACKUPS, eid, fileops.stamp())
    staged = os.path.join(bdir, "candidate")
    for rel, kind in rec["files"]:
        prod = os.path.join(AI_ROOT, *rel.split("/"))
        if kind != "added":
            _replace_copy(prod, os.path.join(bdir, "before", *rel.split("/")))
        if kind != "deleted":
            dst = os.path.join(staged, *rel.split("/"))
            _replace_copy(os.path.join(rec["copy"], *rel.split("/")), dst)
            if _hash(dst) != tests["snapshot"][rel][1]:
                raise ValueError("准备应用时副本又被修改，请重新测试")
    if tests["snapshot"] != snapshot(rec) or violations(rec):
        raise ValueError("准备应用时内容或权限发生变化，请重新测试")
    rec.update(status="applying", backup=bdir, touched=[])
    put(rec)
    after = {}
    try:
        for rel, kind in rec["files"]:
            prod = os.path.join(AI_ROOT, *rel.split("/"))
            if kind == "deleted":
                trash = os.path.join(bdir, "deleted", *rel.split("/"))
                os.makedirs(os.path.dirname(trash), exist_ok=True)
                os.replace(prod, trash)
            else:
                os.makedirs(os.path.dirname(prod), exist_ok=True)
                os.replace(os.path.join(staged, *rel.split("/")), prod)
            rec["touched"].append(rel)
            after[rel] = _hash(prod)
            rec["after"] = dict(after)
            put(rec)
        rec.update(status="applied", after=after, t_applied=time.time())
        put(rec)
    except Exception as exc:
        failed = []
        kinds = dict(rec["files"])
        for rel in reversed(rec["touched"]):
            prod = os.path.join(AI_ROOT, *rel.split("/"))
            try:
                if kinds[rel] == "added":
                    if os.path.exists(prod):
                        fileops.recycle(prod, os.path.join(bdir, "失败文件"))
                else:
                    _replace_copy(os.path.join(bdir, "before", *rel.split("/")), prod)
            except OSError:
                failed.append(rel)
        rec.update(status="apply_failed" if failed else "editing", rollback_failed=failed,
                   after={rel: _hash(os.path.join(AI_ROOT, *rel.split("/"))) for rel in rec["touched"]},
                   tests={**tests, "ok": False}, apply_error=type(exc).__name__)
        put(rec)
        raise ValueError("应用失败；" + ("部分文件恢复失败，可用撤回重试：" + "、".join(failed) if failed else "已恢复正式文件，请修正问题后重新测试")) from exc
    return rec


def revert(eid, force=False):
    rec = get(eid)
    if not rec or rec["status"] not in {"applied", "apply_failed", "applying"}:
        raise ValueError("只有已经应用或应用中断的改动才能撤回")
    bdir = rec.get("backup") or os.path.join(BACKUPS, eid)
    moved = [rel for rel, _ in rec["files"] if _hash(os.path.join(AI_ROOT, *rel.split("/"))) != rec.get("after", {}).get(rel, rec["base"].get(rel))]
    if moved and not force:
        raise ValueError("应用之后这些文件又被改过：" + "、".join(moved) + "。确定要撤回就点“仍然撤回”，这些后来的改动会丢失（会先备份）")
    keep = os.path.join(bdir, "撤回前", fileops.stamp())
    for rel, kind in rec["files"]:
        prod = os.path.join(AI_ROOT, *rel.split("/"))
        if os.path.exists(prod):
            k = os.path.join(keep, *rel.split("/"))
            os.makedirs(os.path.dirname(k), exist_ok=True)
            shutil.copy2(prod, k)
        if kind == "added":
            if os.path.exists(prod):
                fileops.recycle(prod, os.path.join(bdir, "撤回新增"))
        else:
            src = os.path.join(bdir, "before", *rel.split("/"))
            os.makedirs(os.path.dirname(prod), exist_ok=True)
            _replace_copy(src, prod)
    rec.update(status="reverted", t_reverted=time.time())
    put(rec)
    return rec


def reject(eid):
    rec = get(eid)
    if not rec or rec["status"] not in ("proposed", "editing"):
        raise ValueError("这个改动现在不能拒绝")
    rec.update(status="rejected", t_rejected=time.time())
    put(rec)
    if _test["id"] == eid:
        stop_test()
    return rec


def schedule_restart():
    """在后台起一个脚本：等没有任务在跑，再重启正式服务。"""
    script = os.path.join(AI_ROOT, "scripts", "restart_hub.py")
    subprocess.Popen([sys.executable if os.path.exists(sys.executable) else PY, script], cwd=AI_ROOT,
                     creationflags=0x08000000 | 0x00000008 | 0x00000200)   # 无窗口 + 脱离 + 新进程组


def summary_line(rec):
    files = "、".join(f"{r}（{ {'changed': '改', 'added': '新增', 'deleted': '删'}[k] }）" for r, k in rec.get("files", [])[:12])
    return f"{display(rec['copy'])}：{files}"

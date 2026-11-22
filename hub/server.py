"""本地助手网页服务：http://127.0.0.1:8088（局域网也能访问，需要登录）。

启动：hub\\.venv\\Scripts\\python.exe hub\\server.py
"""
import asyncio
import hashlib
import hmac
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import time

import psutil
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app import agent, image, jobs, kb, llm, ocr, petparts, selfedit, store, tools, translate  # noqa: E402
from app.config import API_KEY, CFG, KEY_PATH, MODELS, M, ROLES, SANDBOX, STATIC_DIR, THEMES_DIR, load_settings, save_settings  # noqa: E402
from app.paths import display, readable, resolve, roots, writable, zone
from app import activity, fileops, modelcfg, modeldl, storage, themes  # noqa: E402

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
TEST_MODE = bool(os.environ.get("HUB_PORT"))   # 测试实例和正式服务共用数据，下面两件事只有正式服务做
# 上次服务退出时没跑完的运行，补一条“被中断”记录
INTERRUPTED = 0 if TEST_MODE else store.mark_interrupted()
# 测试实例可以用环境变量换一个登录密码和端口，不影响正式服务
LOGIN_PW = os.environ.get("HUB_TEST_PASSWORD") or API_KEY
PORT = int(os.environ.get("HUB_PORT") or CFG["port"])
TOKEN = hmac.new(LOGIN_PW.encode(), b"hub-login-v1", hashlib.sha256).hexdigest()
# 测试实例也认正式服务的登录：浏览器的登录凭据不分端口，在同一个浏览器里登录过正式服务，打开测试实例不用再登录
MAIN_TOKEN = hmac.new(API_KEY.encode(), b"hub-login-v1", hashlib.sha256).hexdigest()
OPEN_PATHS = ("/login", "/api/login", "/s/")


def authed(req: Request):
    cookie = req.cookies.get("hub", "")
    if hmac.compare_digest(cookie, TOKEN) or hmac.compare_digest(cookie, MAIN_TOKEN):
        return True
    auth = req.headers.get("authorization", "")
    return auth.startswith("Bearer ") and hmac.compare_digest(auth[7:], API_KEY)


@app.middleware("http")
async def guard(req: Request, call_next):
    p = req.url.path
    if p != "/api/health" and not p.startswith(OPEN_PATHS) and not authed(req):
        if p.startswith("/api/"):
            return JSONResponse({"error": "需要登录"}, status_code=401)
        return RedirectResponse("/login")
    resp = await call_next(req)
    if not p.startswith("/s/vendor/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


# 主题在代码目录外（app/themes.py），地址照旧挂在 /s/skins 下，前端不用改；要先于 /s 挂上
app.mount("/s/skins", StaticFiles(directory=THEMES_DIR), name="themes")
app.mount("/s", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def portal():
    """入口页：功能入口、最近的对话、状态，和 9B 的随手问。"""
    return FileResponse(os.path.join(STATIC_DIR, "portal.html"))


@app.get("/petchat")
async def petchat_page():
    """桌面程序里点桌面桌宠弹出的聊天小窗（随手问，scripts/desktop_app.py 开的窗口）。"""
    return FileResponse(os.path.join(STATIC_DIR, "petchat.html"))


@app.get("/app")
async def index():
    """工作台。?mod=tr 直接打开某个页面，?conv=编号 直接打开某个对话。"""
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


SKIN_SLOTS = {"立绘": "art", "横幅": "banner", "看板": "mascot", "背景": "bg"}
IMG_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif")


# ---------------------------------------------------------------- 桌宠按部件拼的帧（app/petparts.py）
# 用户在入口页“调整布局 → 表情… → 桌宠 → 编辑表情”里改的配方存在设置的 pet_frames[主题][帧名]
def _pet_skin(skin: str):
    d = themes.theme_path(THEMES_DIR, skin)
    man = petparts.manifest(d) if d else None
    if not man:
        raise HTTPException(404, "这个主题的桌宠没有部件")
    return d, man


_pet_png = {}


@app.get("/api/pet/frame/{skin}/{name}.png")
async def pet_frame(skin: str, name: str):
    """按设置里的配方拼出这一帧（改过的、自己建的、跟着改过的底帧走的眨眼和说话）。同一个配方只拼一次。"""
    d, man = _pet_skin(skin)
    custom = (load_settings().get("pet_frames") or {}).get(skin) or {}
    r = petparts.recipe(man, custom, name)
    if not r:
        raise HTTPException(404, "没有这一帧")
    key = (skin, json.dumps(r, sort_keys=True, ensure_ascii=False))
    if key not in _pet_png:
        if len(_pet_png) > 200:
            _pet_png.clear()
        _pet_png[key] = await asyncio.to_thread(lambda: petparts.png(petparts.compose(d, r, man)))
    return Response(_pet_png[key], media_type="image/png")


@app.post("/api/pet/compose")
async def pet_compose(req: Request):
    """编辑表情时的预览：按传来的配方拼一张（不存）。"""
    body = await req.json()
    d, man = _pet_skin(str(body.get("skin", "")))
    r = petparts.clean(man, body.get("recipe"))
    return Response(await asyncio.to_thread(lambda: petparts.png(petparts.compose(d, r, man))), media_type="image/png")


@app.get("/api/skins")
async def skins():
    """主题列表：主题文件夹（app/themes.py）里的 <主题>/skin.json，素材文件夹里按文件名认出立绘、横幅、看板、背景。"""
    from urllib.parse import quote
    base = THEMES_DIR
    out = []
    for d in sorted(os.listdir(base)) if os.path.isdir(base) else []:
        try:
            meta = json.load(open(os.path.join(base, d, "skin.json"), encoding="utf-8"))
        except Exception:
            continue
        files, mdir = {}, os.path.join(base, d, "素材")
        for f in sorted(os.listdir(mdir)) if os.path.isdir(mdir) else []:
            stem, ext = os.path.splitext(f)
            slot = SKIN_SLOTS.get(stem)
            if slot and ext.lower() in IMG_EXT and slot not in files:
                v = int(os.path.getmtime(os.path.join(mdir, f)))
                files[slot] = f"/s/skins/{quote(d)}/{quote('素材')}/{quote(f)}?v={v}"
        out.append({"id": d, "name": meta.get("name") or d, "accent": meta.get("accent"), "persona": meta.get("persona"),   # persona：桌宠台词用哪个人格的
                    "css": f"/s/skins/{quote(d)}/skin.css", "files": files})
    return out


@app.get("/login")
async def login_page():
    return FileResponse(os.path.join(STATIC_DIR, "login.html"))


@app.post("/api/login")
async def login(req: Request):
    body = await req.json()
    pw = str(body.get("password", ""))
    # 测试实例除了自己的临时密码，也认 api_key.txt 里的密钥（登录页上写的就是它）
    if not (hmac.compare_digest(pw, LOGIN_PW) or hmac.compare_digest(pw, API_KEY)):
        await asyncio.sleep(1)
        return JSONResponse({"error": "密码不对"}, status_code=403)
    resp = JSONResponse({"ok": True})
    resp.set_cookie("hub", TOKEN, max_age=60 * 60 * 24 * 90, httponly=True, samesite="strict")
    return resp


# ---------------------------------------------------------------- 设置
def is_local(req: Request):
    return (req.client.host if req.client else "") in ("127.0.0.1", "::1")


@app.get("/api/config")
async def get_config(req: Request):
    s = load_settings()
    return {"chat_models": CFG["chat_models"], "settings": s, "sandbox": tools.SANDBOX, "local": is_local(req),
            "roles": ROLES, "models": [{"id": k, "name": e["name"], "device": e["device"]} for k, e in M.items()]}


@app.get("/api/models")
async def models_table():
    """模型表（设置页）：清单里每个模型的用途、能力、上下文、占用，模型文件在不在，现在有没有加载。"""
    try:
        running = {r.get("model"): r.get("state") for r in (await llm.get("/running", timeout=5)).json().get("running", [])}
    except Exception:
        running = None
    out = await asyncio.to_thread(modelcfg.public, MODELS)
    for m in out:
        m["state"] = None if running is None else running.get(m["id"])
    return {"models": out, "ok": running is not None}


@app.get("/api/models/downloads")
async def models_downloads():
    """下载区：每组的大小、缺多少（大小第一次要问 HuggingFace，之后有缓存），程序装了没有，正在下的进度。"""
    if not MODELS.get("file"):
        raise HTTPException(409, "没有模型清单 config/models.yaml")
    try:
        st = await asyncio.to_thread(modeldl.status, modelcfg.load(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    except modelcfg.ManifestError as e:
        raise HTTPException(409, str(e))
    return {**st, "progress": modeldl.read_progress()}


@app.post("/api/models/downloads")
async def models_download_start(req: Request):
    """按组下载（后台进程跑 scripts/download_models.py，进度写 data/download_progress.json）。同时只能有一个。"""
    groups = (await req.json()).get("groups")
    if not isinstance(groups, list) or not groups or not all(isinstance(g, str) for g in groups):
        raise HTTPException(400, "选几个组")
    p = modeldl.read_progress()
    if p and p.get("state") == "running":
        raise HTTPException(409, "已经在下载了")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        os.remove(modeldl.PROGRESS)
    except OSError:
        pass
    subprocess.Popen([modeldl.main_python(), os.path.join(root, "scripts", "download_models.py"), "--groups", ",".join(groups),
                      "--yes", "--progress", modeldl.PROGRESS], cwd=root, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=0x08000000 | 0x00000200)
    return {"ok": True}


@app.post("/api/models/downloads/stop")
async def models_download_stop():
    """停下载：结束下载进程和它的 curl。已下的部分留着，下次接着下。"""
    p = modeldl.read_progress()
    if not p or p.get("state") != "running":
        return {"ok": True}
    try:
        proc = psutil.Process(p["pid"])
        for c in proc.children(recursive=True) + [proc]:
            c.kill()
    except psutil.Error:
        pass
    return {"ok": True}


@app.put("/api/settings")
async def put_settings(req: Request):
    body = await req.json()
    for k in ("self_edit_enabled", "self_edit_full"):
        if k in body and not isinstance(body[k], bool):
            raise HTTPException(422, f"{k} 必须是布尔值")
    s = load_settings()
    was_full = selfedit.full_mode()
    for k in ("pet_faces", "pet_frames"):
        if k in body and not isinstance(body[k], dict):
            raise HTTPException(422, f"{k} 必须是对象")
    for k in ("look", "pet_faces", "pet_frames", "default_model", "think", "web", "allowed_dirs", "level", "self_edit_enabled", "self_edit_full"):
        if k in body:
            s[k] = body[k]
    if "trash_dir" in body:                 # 存储卡片里改的待删除位置；空的就回到 hub.json 的
        if body["trash_dir"]:
            try:
                s["trash_dir"] = storage.check_trash_dir(body["trash_dir"])
            except ValueError as e:
                raise HTTPException(422, str(e))
        else:
            s.pop("trash_dir", None)
    if s.get("self_edit_enabled") is not True:
        s["self_edit_full"] = False
    save_settings(s)
    if (body.get("self_edit_enabled") is False) or (was_full and not selfedit.full_mode()):
        await asyncio.to_thread(selfedit.stop_test)
    return s


# ---------------------------------------------------------------- 对话
@app.get("/api/convs")
async def convs(archived: int = 0):
    return store.list_convs(200 if archived else 100, 1 if archived else 0)


@app.post("/api/convs")
async def new_conv(req: Request):
    body = await req.json()
    return store.new_conv(body.get("model") or load_settings()["default_model"])


@app.get("/api/convs/{cid}")
async def conv(cid: str):
    c = store.get_conv(cid)
    if not c:
        raise HTTPException(404, "对话不存在")
    rid = agent.ACTIVE.get(cid)
    running = rid if rid and not agent.RUNS[rid].done else None
    st = store.get_state(cid)
    proj = st.get("project")
    return {"conv": c, "msgs": store.get_msgs(cid), "run": running,
            "project": proj if proj and os.path.isdir(proj) else None, "project_show": display(proj) if proj else "",
            "state": {k: st.get(k, d) for k, d in (("plan", []), ("criteria", []), ("unfinished", []), ("findings", []),
                                                   ("prev_criteria", []), ("level", "standard"), ("asks", []))}}


@app.patch("/api/convs/{cid}")
async def edit_conv(cid: str, req: Request):
    body = await req.json()
    kw = {k: body[k] for k in ("title", "archived", "folder") if k in body}
    if "archived" in kw and not kw["archived"]:
        kw["folder"] = ""          # 恢复时离开归档分组
    store.update_conv(cid, **kw)
    return store.get_conv(cid)


@app.delete("/api/convs/{cid}")
async def delete_conv(cid: str):
    rid = agent.ACTIVE.get(cid)
    if rid and rid in agent.RUNS and not agent.RUNS[rid].done:
        raise HTTPException(409, "这个对话还有任务在运行，先停止再删")
    store.delete_conv(cid)
    agent.prune_runs(cid)
    return {"ok": True}


@app.post("/api/convs/{cid}/send")
async def send(cid: str, req: Request):
    body = await req.json()
    if not store.get_conv(cid):
        raise HTTPException(404, "对话不存在")
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "消息是空的")
    s = load_settings()
    try:
        run = agent.start(cid, text, body.get("model") or s["default_model"], bool(body.get("think", s["think"])), bool(body.get("web", s["web"])),
                          body.get("level") or s["level"], body.get("profile"))
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"run": run.id}


@app.get("/api/runs/{rid}/events")
async def events(rid: str, req: Request, start: int = 0):
    job = agent.RUNS.get(rid)
    if not job:
        raise HTTPException(404, "运行记录不存在")
    return StreamingResponse(jobs.sse(job, jobs.cursor(req, start)), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})


@app.post("/api/runs/{rid}/confirm")
async def confirm(rid: str, req: Request):
    run = agent.RUNS.get(rid)
    body = await req.json()
    if not run or not run.answer(body.get("id"), body.get("decision"), (body.get("note") or "")[:1000]):
        raise HTTPException(409, "这个确认已经失效")
    return {"ok": True}


@app.post("/api/runs/{rid}/stop")
async def stop(rid: str):
    run = agent.RUNS.get(rid)
    if run and not run.done and run.task:
        run.task.cancel()
    return {"ok": True}


# ---------------------------------------------------------------- 文件（只能看沙箱和授权目录）
def _checked(path):
    p = resolve(path)
    if not readable(p):
        raise HTTPException(403, "只能查看沙箱和授权目录里的文件")
    if not os.path.exists(p):
        raise HTTPException(404, "文件不存在")
    return p


@app.get("/api/files/roots")
async def file_roots():
    return roots()


@app.get("/api/files/list")
async def file_list(path: str):
    p = _checked(path)
    out = []
    for e in os.scandir(p):
        if e.name.startswith(".") and e.name not in (".回收站",):
            continue
        try:
            st = e.stat()
        except OSError:
            continue
        out.append({"name": e.name, "path": e.path, "dir": e.is_dir(), "size": st.st_size, "mtime": st.st_mtime})
    out.sort(key=lambda x: (not x["dir"], x["name"].lower()))
    return out


@app.get("/api/files/find")
async def file_find(q: str, limit: int = 40):
    """按文件名找文件（标题栏搜索用）。名字包含所有关键词就算命中，跳过隐藏文件夹。"""
    words = [w.lower() for w in q.split() if w]
    if not words:
        return []

    def walk():
        out, seen = [], 0
        for r in roots():
            stack = [r["path"]]
            while stack and seen < 30000 and len(out) < limit:
                try:
                    it = list(os.scandir(stack.pop()))
                except OSError:
                    continue
                for e in it:
                    seen += 1
                    if e.name.startswith("."):
                        continue
                    if all(w in e.name.lower() for w in words):
                        out.append({"name": e.name, "path": e.path, "show": display(e.path), "dir": e.is_dir()})
                    if e.is_dir():
                        stack.append(e.path)
        return out[:limit]
    return await asyncio.to_thread(walk)


IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}


@app.get("/api/files/read")
async def file_read(path: str):
    p = _checked(path)
    ext = os.path.splitext(p)[1].lower()
    st = os.stat(p)
    show = display(p)
    if show == p and os.path.normcase(p).startswith(os.path.normcase(kb.ROOT + os.sep)):
        show = "知识库" + os.sep + os.path.relpath(p, kb.ROOT)      # 查看器的路径栏：知识库里的文件不显示整段磁盘路径
    base = {"path": p, "show": show, "name": os.path.basename(p) or p, "size": st.st_size, "mtime": st.st_mtime}
    if os.path.isdir(p):
        return {**base, "kind": "dir", "entries": await file_list(p)}
    if ext in IMAGE_EXT:
        return {**base, "kind": "image"}
    if ext in image.VIDEO_EXT:
        return {**base, "kind": "video"}
    try:
        text, enc = tools.read_text(p)
    except Exception as e:
        return {**base, "kind": "binary", "error": str(e)}
    return {**base, "kind": "markdown" if ext in (".md", ".markdown") else "text", "text": text, "encoding": enc,
            "editable": _edit_kind(p) is not None}


# ---------------------------------------------------------------- 在查看器里直接改文本（用户本人操作）
# 沙箱和可写目录里的文本文件直接写回，保持原来的编码和换行；规范层的文件走 kb.save_norm，改前的版本存进历史，可以撤回
_NORM_DIR = os.path.join(kb.ROOT, "规范")


def _edit_kind(p):
    if os.path.isdir(p) or os.path.splitext(p)[1].lower() in IMAGE_EXT | image.VIDEO_EXT:
        return None
    if os.path.normcase(os.path.dirname(p)) == os.path.normcase(_NORM_DIR) and p.lower().endswith((".md", ".markdown", ".txt")):
        return "norm"
    if (os.path.basename(p) == kb.PERSONA_FILE and
            os.path.normcase(os.path.dirname(os.path.dirname(p))) == os.path.normcase(kb.PERSONA_DIR)):
        return "persona"
    if (os.path.normcase(os.path.dirname(p)) == os.path.normcase(kb.SYSPROMPT_DIR) and
            os.path.splitext(os.path.basename(p))[0] in kb.SYSPROMPTS and p.lower().endswith(".md")):
        return "sysprompt"
    if writable(p) and not any(os.path.normcase(p) == os.path.normcase(os.path.realpath(r["path"])) for r in roots()):
        rel = os.path.relpath(p, SANDBOX) if zone(p) == "sandbox" else ""
        if rel.split(os.sep)[0] not in (".回收站", ".运行"):
            return "file"
    return None


@app.post("/api/files/stat")
async def file_stat(req: Request):
    """查看器核对打开着的文件有没有在别处被改过：返回每个路径的修改时间，不存在或读不了的给 null。"""
    out = {}
    for path in ((await req.json()).get("paths") or [])[:20]:
        try:
            p = resolve(path)
            out[path] = os.stat(p).st_mtime if readable(p) else None
        except (OSError, ValueError, HTTPException):
            out[path] = None
    return {"mtimes": out}


@app.post("/api/files/save")
async def file_save(req: Request):
    body = await req.json()
    p = _checked(body.get("path", ""))
    text = body.get("text")
    if not isinstance(text, str):
        raise HTTPException(400, "缺少文本内容")
    kind = _edit_kind(p)
    if not kind or not os.path.isfile(p):
        raise HTTPException(403, "这个位置只读，不能在这里改")
    mtime = body.get("mtime")
    if mtime is not None and not body.get("force") and abs(os.stat(p).st_mtime - float(mtime)) > 0.001:
        raise HTTPException(409, "文件在别处被改过了")
    try:
        _, enc = tools.read_text(p)
    except Exception as e:
        raise HTTPException(400, "不能按文本保存：" + str(e))
    text = text.replace("\r\n", "\n")
    if kind == "norm":
        kb.save_norm(os.path.basename(p), text, reason="在网页上直接编辑", who="你")
        asyncio.get_running_loop().create_task(kb.scan())
        return await file_read(p)
    if kind == "sysprompt":         # 系统提示词：旧版本存进 knowledge\.历史，下一条消息起生效
        kb.save_sysprompt(os.path.splitext(os.path.basename(p))[0], text)
        return await file_read(p)
    if kind == "persona":           # 人格卡：存历史，可撤回；有字数上限
        try:
            kb.save_persona(os.path.basename(os.path.dirname(p)), text, reason="在网页上直接编辑", who="你")
        except ValueError as e:
            raise HTTPException(400, str(e))
        return await file_read(p)
    with open(p, "rb") as f:
        crlf = b"\r\n" in f.read(1 << 20)
    try:
        data = (text.replace("\n", "\r\n") if crlf else text).encode(enc or "utf-8")
    except (UnicodeEncodeError, LookupError):
        raise HTTPException(400, f"有字符不能用原来的编码（{enc}）保存")
    tmp = p + ".保存中"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, p)
    return await file_read(p)


@app.get("/api/files/raw")
async def file_raw(path: str, request: Request):
    p = _checked(path)
    media = mimetypes.guess_type(p)[0] or "application/octet-stream"
    rng = request.headers.get("range", "")
    if rng.startswith("bytes=") and media.startswith(("video/", "audio/")):
        return _ranged(p, media, rng)            # 视频要能拖进度条：浏览器按段要
    return FileResponse(p, media_type=media, headers={
        "Content-Security-Policy": "sandbox; default-src 'none'; script-src 'none'",
        "X-Content-Type-Options": "nosniff"},
        filename=os.path.basename(p) if media in {"text/html", "image/svg+xml", "application/xhtml+xml"} else None)


def _ranged(p, media, rng):
    """只回答一段（bytes=开始-结束），够浏览器播视频、拖进度条用。这版 Starlette 的 FileResponse 不支持 Range。"""
    size = os.path.getsize(p)
    a, _, b = rng[6:].split(",")[0].strip().partition("-")
    try:
        start = int(a) if a else max(0, size - int(b))
        end = min(size - 1, int(b)) if a and b else size - 1
    except ValueError:
        start, end = 0, size - 1
    if start >= size or start > end:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})

    def body():
        with open(p, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(1 << 20, left))
                if not chunk:
                    break
                left -= len(chunk)
                yield chunk
    return StreamingResponse(body(), status_code=206, media_type=media, headers={
        "Content-Range": f"bytes {start}-{end}/{size}", "Accept-Ranges": "bytes", "Content-Length": str(end - start + 1),
        "Content-Security-Policy": "sandbox; default-src 'none'; script-src 'none'", "X-Content-Type-Options": "nosniff"})


@app.post("/api/files/reveal")
async def file_reveal(req: Request):
    """在这台电脑的资源管理器里显示文件。只接受本机请求。"""
    if not is_local(req):
        raise HTTPException(403, "只能在这台电脑上使用")
    p = _checked((await req.json()).get("path", ""))
    args = ["explorer", p] if os.path.isdir(p) else ["explorer", "/select," + p]
    subprocess.Popen(args, creationflags=0x08000000)
    return {"ok": True}


# ---------------------------------------------------------------- 整理文件（网页上用户自己的操作：新建文件夹、移动或改名、移到回收站）
# 只在沙箱和设成可写的授权目录里；根目录本身、回收站、运行目录不能动。用户本人操作，不弹确认卡片
_BAD_NAME = image.BAD_NAME


def _organizable(path, must_exist=True):
    p = resolve(path)
    if not writable(p):
        raise HTTPException(403, "只能在沙箱和设成可写的授权目录里整理文件")
    if any(os.path.normcase(p) == os.path.normcase(os.path.realpath(r["path"])) for r in roots()):
        raise HTTPException(403, "根目录本身不能改动")
    rel = os.path.relpath(p, SANDBOX) if zone(p) == "sandbox" else ""
    if rel.split(os.sep)[0] in (".回收站", ".运行"):
        raise HTTPException(403, "回收站和运行目录不能在这里整理")
    if must_exist and not os.path.exists(p):
        raise HTTPException(404, "不存在：" + display(p))
    return p


def _check_name(name):
    name = (name or "").strip()
    if not name or name in (".", "..") or _BAD_NAME.search(name) or name.endswith((" ", ".")):
        raise HTTPException(400, '名字不能为空，不能用 \\ / : * ? " < > |，也不能以空格或点结尾')
    return name


@app.post("/api/files/mkdir")
async def file_mkdir(req: Request):
    body = await req.json()
    parent = _checked(body.get("parent", ""))
    if not os.path.isdir(parent):
        raise HTTPException(400, "只能在文件夹里新建")
    p = _organizable(os.path.join(parent, _check_name(body.get("name"))), must_exist=False)
    if os.path.exists(p):
        raise HTTPException(409, "已经有同名的文件或文件夹")
    os.makedirs(p)
    return {"path": p, "show": display(p)}


@app.post("/api/files/move")
async def file_move(req: Request):
    """移动或改名。dst 是已有的文件夹就移进去；目标已存在时不覆盖。改名时传 {src, name}。"""
    body = await req.json()
    s = _organizable(body.get("src", ""))
    if body.get("name") is not None:
        d = os.path.join(os.path.dirname(s), _check_name(body["name"]))
    else:
        d = resolve(body.get("dst", ""))
        if os.path.isdir(d):
            d = os.path.join(d, os.path.basename(s))
    d = _organizable(d, must_exist=False)
    if os.path.normcase(d) == os.path.normcase(s):
        if d == s:
            return {"path": s, "show": display(s), "moved": False}
        try:
            store.move_tracked(s, d)
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        return {"path": d, "show": display(d), "moved": True}
    if os.path.normcase(d).startswith(os.path.normcase(s).rstrip(os.sep) + os.sep):
        raise HTTPException(400, "不能把文件夹移到它自己里面")
    if os.path.exists(d):
        raise HTTPException(409, "目标位置已经有同名的文件或文件夹：" + display(d))
    os.makedirs(os.path.dirname(d), exist_ok=True)
    try:
        store.move_tracked(s, d)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return {"path": d, "show": display(d), "moved": True}


def _deletable(path):
    """彻底删除前的检查：和整理文件一样只在沙箱、可写授权目录里，根目录、运行目录不行；回收站里面的东西可以删。
    密钥文件、正在跑的项目不能删。"""
    p = resolve(path)
    if not writable(p):
        raise HTTPException(403, "只能删沙箱和设成可写的授权目录里的东西")
    if any(os.path.normcase(p) == os.path.normcase(os.path.realpath(r["path"])) for r in roots()):
        raise HTTPException(403, "根目录本身不能删")
    rel = os.path.relpath(p, SANDBOX) if zone(p) == "sandbox" else ""
    if rel.split(os.sep)[0] == ".运行":
        raise HTTPException(403, "运行目录不能在这里删")
    if os.path.normcase(rel) == os.path.normcase(".回收站"):
        raise HTTPException(403, "回收站本身不能删，要清空请用“清空回收站”")
    if not os.path.lexists(p):
        raise HTTPException(404, "不存在：" + display(p))
    if fileops.sensitive(p) or fileops.inside(KEY_PATH, p):
        raise HTTPException(403, "密钥文件不能删")
    for run in agent.RUNS.values():
        project = run.st.get("project")
        if not run.done and project and (fileops.inside(p, project) or fileops.inside(project, p)):
            raise HTTPException(409, "这个项目正在运行，停止或等本次运行结束后再删")
    return p


@app.post("/api/files/delete")
async def file_delete(req: Request):
    """彻底删除（不进回收站，不能恢复）。网页上用户自己点、确认过才调；Agent 没有这个工具。"""
    p = _deletable((await req.json()).get("path", ""))
    try:
        n = await asyncio.to_thread(fileops.remove, p)
    except OSError as e:
        raise HTTPException(409, f"没删掉（可能有程序正打开着它）：{e}")
    return {"ok": True, "bytes": n}


@app.post("/api/files/empty-trash")
async def file_empty_trash():
    """清空沙箱回收站（彻底删除里面所有东西）。"""
    return await asyncio.to_thread(storage.delete, "recycle", tools.TRASH)


@app.post("/api/files/trash")
async def file_trash(req: Request):
    p = _organizable((await req.json()).get("path", ""))
    os.makedirs(tools.TRASH, exist_ok=True)
    d = fileops.recycle(p, tools.TRASH)
    return {"path": d, "show": display(d)}


# ---------------------------------------------------------------- 翻译
def _agent_busy():
    return any(not agent.RUNS[r].done for r in agent.ACTIVE.values() if r in agent.RUNS)


@app.get("/api/tr/info")
async def tr_info():
    names = {m: CFG["models"][k] for m, k in (("sakura", "ja_zh"), ("hy", "translate"))}
    return {"glossaries": translate.list_glossaries(), "ext": sorted(translate.FILE_EXT), "models": names,
            "glossary_dir": translate.GLOSSARY_DIR}


@app.put("/api/tr/glossaries")
async def tr_glossary_save(req: Request):
    body = await req.json()
    try:
        p = translate.glossary_path(body.get("name"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    os.makedirs(translate.GLOSSARY_DIR, exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write((body.get("text") or "").replace("\r\n", "\n"))
    return {"path": p, "count": len(translate.parse_glossary(body.get("text") or ""))}


@app.post("/api/tr/jobs")
async def tr_start(req: Request):
    body = await req.json()
    if body.get("path"):
        p = _checked(body["path"])
        if os.path.splitext(p)[1].lower() not in translate.FILE_EXT:
            raise HTTPException(400, "不支持这种文件。能翻译的有 " + "、".join(sorted(translate.FILE_EXT)))
        body["path"] = p
    elif not (body.get("text") or "").strip():
        raise HTTPException(400, "没有要翻译的文字")
    job = translate.start_job(body)
    return {"job": job.id, "agent_busy": _agent_busy()}


@app.get("/api/tr/jobs/{jid}/events")
async def tr_events(jid: str, req: Request, start: int = 0):
    job = translate.JOBS.get(jid)
    if not job:
        raise HTTPException(404, "翻译任务不存在（服务重启过）")
    return StreamingResponse(jobs.sse(job, jobs.cursor(req, start)), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})


@app.post("/api/tr/jobs/{jid}/stop")
async def tr_stop(jid: str):
    job = translate.JOBS.get(jid)
    if job:
        job.stopped = True
    return {"ok": True}


# ---------------------------------------------------------------- 知识库
@app.on_event("startup")
async def kb_startup():
    if not TEST_MODE:
        asyncio.get_running_loop().create_task(kb.scan())      # 启动时对一遍文件夹和索引
        asyncio.get_running_loop().create_task(_warm_lite())
        from app import memory
        asyncio.get_running_loop().create_task(memory.nightly(_idle_for_sleep))   # 人格记忆的睡眠整理（凌晨 3–6 点）


def _idle_for_sleep():
    """没有对话在跑，模型 20 分钟没用过，才做记忆整理。"""
    if any(not r.done for r in agent.RUNS.values()):
        return False
    return time.time() - max(llm.LAST_USED.values(), default=0) > 20 * 60


async def _warm_lite():
    """入口页的 9B 对话：服务启动后先把固定开头（系统提示词 + 工具说明）读一遍，第一次提问就能用上缓存。"""
    await asyncio.sleep(20)
    try:
        msgs = [{"role": "system", "content": kb.system_prompt("精简版")}, {"role": "user", "content": "你好"}]
        async for _ in llm.stream_chat(agent.LITE_MODEL, msgs, tools=agent.lite_schemas(), think=False, max_tokens=1):
            pass
    except Exception:
        pass


@app.get("/api/kb")
async def kb_info():
    kb.scan_soon()
    return {"libs": kb.libs(), "norms": kb.norms(), "status": kb.STATUS, "root": kb.ROOT, "sysprompts": kb.sysprompts()}


@app.post("/api/kb/sysprompt/reset")
async def kb_sysprompt_reset(req: Request):
    """系统提示词恢复默认（现在的版本存进 knowledge 的 .历史 文件夹）。"""
    try:
        kb.reset_sysprompt((await req.json()).get("name", ""))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"sysprompts": kb.sysprompts()}


@app.get("/api/kb/files")
async def kb_files(lib: str):
    try:
        return {"files": kb.files(lib), "dir": kb.lib_dir(lib), "sources": kb.sources(lib)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/kb/libs")
async def kb_new_lib(req: Request):
    body = await req.json()
    try:
        lib = kb.create_lib(body.get("layer"), body.get("name"), body.get("desc") or "")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"lib": lib}


@app.patch("/api/kb/libs")
async def kb_edit_lib(req: Request):
    body = await req.json()
    kb.set_desc(body.get("lib"), body.get("desc") or "")
    return {"ok": True}


@app.patch("/api/kb/norms")
async def kb_edit_norm(req: Request):
    body = await req.json()
    if not kb.find_norm(body.get("name")):
        raise HTTPException(404, "没有这份规范")
    kb.set_norm(kb.find_norm(body["name"])["name"], **{k: body[k] for k in ("desc", "default") if k in body})
    return {"norms": kb.norms()}


def _kb_target(lib, name):
    try:
        d = kb.lib_dir(lib)
    except ValueError as e:
        raise HTTPException(400, str(e))
    name = os.path.basename(name or "")
    if os.path.splitext(name)[1].lower() not in kb.EXTS:
        raise HTTPException(400, "不支持这种文件。能放进知识库的有 " + "、".join(sorted(kb.EXTS)))
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, name)


@app.post("/api/kb/upload")
async def kb_upload(req: Request):
    form = await req.form()
    lib, saved = form.get("lib"), []
    for f in form.getlist("files"):
        dst = _kb_target(lib, f.filename)
        with open(dst, "wb") as out:
            out.write(await f.read())
        saved.append(dst)
    asyncio.get_running_loop().create_task(kb.scan())
    return {"saved": saved}


@app.post("/api/kb/add")
async def kb_add(req: Request):
    """把沙箱或授权目录里的文件复制进某个库。"""
    body = await req.json()
    src = _checked(body.get("path", ""))
    if os.path.isdir(src):
        raise HTTPException(400, "只能添加文件，不能添加文件夹")
    dst = _kb_target(body.get("lib"), src)
    import shutil
    shutil.copy2(src, dst)
    asyncio.get_running_loop().create_task(kb.scan())
    return {"saved": dst}


@app.post("/api/kb/remove")
async def kb_remove(req: Request):
    """从知识库移走一个文件：移到 knowledge\\.回收站，不真删。关联的外部文件不动。"""
    p = os.path.realpath((await req.json()).get("path", ""))
    if not p.startswith(kb.ROOT + os.sep) or not os.path.isfile(p):
        raise HTTPException(400, "只能移走知识库文件夹里的文件；关联的外部文件夹在库说明里改")
    trash = os.path.join(kb.ROOT, ".回收站")
    os.makedirs(trash, exist_ok=True)
    dst = fileops.recycle(p, trash)
    asyncio.get_running_loop().create_task(kb.scan())
    return {"moved_to": dst}


@app.post("/api/kb/scan")
async def kb_scan():
    asyncio.get_running_loop().create_task(kb.scan())
    return {"ok": True}


@app.post("/api/kb/search")
async def kb_search(req: Request):
    body = await req.json()
    ids = body.get("libs") or [l["id"] for l in kb.libs()]
    t0 = time.time()
    hits = await kb.search(body.get("query") or "", ids, k=int(body.get("count") or 8))
    return {"hits": hits, "secs": round(time.time() - t0, 1)}


def _conv_state(cid):
    """正在运行的对话用内存里的状态（运行结束时会整体存回），否则读数据库。"""
    rid = agent.ACTIVE.get(cid)
    run = agent.RUNS.get(rid) if rid else None
    return (run.st if run and not run.done else store.get_state(cid)), run


@app.get("/api/convs/{cid}/kb")
async def conv_kb(cid: str):
    st, _ = _conv_state(cid)
    return {"norms": kb.conv_norms(st), "other": st.get("kb_other") or []}


@app.put("/api/convs/{cid}/kb")
async def conv_kb_set(cid: str, req: Request):
    body = await req.json()
    st, run = _conv_state(cid)
    if "norms" in body:
        st["norms"] = [n for n in body["norms"] if kb.find_norm(n)]
    if "other" in body:
        st["kb_other"] = [l for l in body["other"] if l.startswith("其他/")]
    if not run or run.done:
        store.set_state(cid, st)
    return {"norms": kb.conv_norms(st), "other": st.get("kb_other") or []}


# ---------------------------------------------------------------- 人格层（knowledge\人格）和对话的人格
@app.get("/api/convs/{cid}/persona")
async def conv_persona(cid: str):
    st, _ = _conv_state(cid)
    return {"persona": kb.conv_persona(st)}


@app.put("/api/convs/{cid}/persona")
async def conv_persona_set(cid: str, req: Request):
    name = (await req.json()).get("persona") or None
    if name and not kb.find_persona(name):
        raise HTTPException(404, "没有这个人格")
    st, run = _conv_state(cid)
    st["persona"] = name
    if not run or run.done:
        store.set_state(cid, st)
    return {"persona": kb.conv_persona(st)}


@app.get("/api/personas")
async def persona_list():
    return {"personas": kb.personas(), "dir": kb.PERSONA_DIR}


@app.post("/api/personas")
async def persona_new(req: Request):
    body = await req.json()
    try:
        if kb.find_persona(body.get("name")):
            raise ValueError("已经有这个名字的人格了")
        name, _ = kb.save_persona(body.get("name"), reason="新建", desc=body.get("desc") or "")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"name": name, "path": kb.find_persona(name)["path"]}


@app.patch("/api/personas")
async def persona_desc(req: Request):
    body = await req.json()
    if not kb.find_persona(body.get("name")):
        raise HTTPException(404, "没有这个人格")
    kb.set_persona_desc(body["name"], body.get("desc") or "")
    return {"personas": kb.personas()}


@app.get("/api/personas/petlines")
async def persona_petlines(name: str):
    """桌宠台词库（knowledge\\人格\\<名字>\\桌宠台词.json），没有时 lines 为 null，网页用通用台词。"""
    if not kb.find_persona(name):
        raise HTTPException(404, "没有这个人格")
    return {"lines": kb.pet_lines(name)}


PET_LINES_MODEL = ROLES.get("chat")     # 一次性的活，用显卡上的大模型写得更像（要切模型，约半分钟）
PET_LINES_PROMPT = """下面是一个角色的人格卡。她在用户电脑屏幕上是一个会动的小桌宠。按人格卡里的身份、性格和说话方式，给她写桌宠在各种情况下说的短台词。

要求：
- 每条一句，最多 16 个字，口语，有她自己的味道；不要所有角色都能说的空话。
- 每类写 4–6 条，彼此不重复。称呼用户按人格卡来。
- 严格按说话的那一刻写：tool 里是事情刚开始、还没结果的时候（不能说“找到了”“完成了”）；wait 是她要问用户、请用户确认（不是用户问她）；fail 是已经失败了。
- 只输出一个 JSON 对象，不要别的文字。键和含义：
  "greet": {"late": 凌晨还没睡时打招呼, "morning": 早上, "noon": 中午, "afternoon": 下午, "evening": 晚上}
  "poke": 被鼠标点了一下, "pout": 被连着戳好几下不高兴了, "wake": 睡着时被戳醒, "held": 被鼠标拎起来拖着走,
  "dizzy": 从高处摔下来晕了, "home": 走回原位, "idle": 闲着时自言自语, "sleepy": 半夜犯困, "stroll": 散步停下时,
  "hover": 鼠标停在她身上, "fail": 帮用户干活没做成,
  "tool": {"search": 正在查资料或找文件, "write": 正在写文件, "run": 正在运行代码检查结果, "draw": 正在画图,
           "translate": 正在翻译或识别文字, "delete": 要删东西, "error": 工具出错},
  "wait": {"ask": 有问题要问用户, "confirm": 要用户点确认}
  每一类的值都必须是字符串数组（4–6 条），不能只写一句字符串。格式像这样：
  {"greet": {"late": ["…", "…", "…", "…"], "morning": [...], ...}, "poke": ["…", "…", "…", "…"], "pout": [...], ...,
   "tool": {"search": ["…", "…", "…", "…"], ...}, "wait": {"ask": [...], "confirm": [...]}}

<人格卡>
{card}
</人格卡>"""


@app.post("/api/personas/petlines")
async def persona_petlines_make(req: Request):
    """按人格卡让模型重写桌宠台词库；旧的改名留在人格文件夹里。"""
    name = (await req.json()).get("name")
    card = kb.persona_text(name) if kb.find_persona(name) else None
    if not card:
        raise HTTPException(404, "没有这个人格")
    def count(d):
        return sum(len(v) if isinstance(v, list) else sum(map(len, v.values())) for v in d.values())
    lines = {}
    for _ in range(2):              # 偶尔写坏或写得太少，再写一次，取台词多的那份
        text, _t = await llm.complete(PET_LINES_MODEL, [{"role": "user", "content": PET_LINES_PROMPT.replace("{card}", card.strip())}],
                                      max_tokens=3000, temperature=0.8, top_p=0.9,
                                      chat_template_kwargs={"enable_thinking": False})
        m = re.search(r"\{.*\}", text, re.S)
        try:
            got = kb.clean_pet_lines(json.loads(m.group(0)) if m else {})
        except ValueError:
            got = {}
        if count(got) > count(lines):
            lines = got
        if len(lines) >= 14 and count(lines) >= 60:
            break
    if len(lines) < 8 or count(lines) < 30:
        raise HTTPException(502, "模型没写出能用的台词，再试一次")
    return {"lines": kb.save_pet_lines(name, lines)}


@app.get("/api/personas/history")
async def persona_history(name: str):
    return {"history": kb.persona_history(name)}


@app.post("/api/personas/restore")
async def persona_restore(req: Request):
    body = await req.json()
    try:
        kb.restore_persona(body.get("name"), body.get("stamp"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"history": kb.persona_history(body.get("name"))}


@app.post("/api/personas/remove")
async def persona_remove(req: Request):
    try:
        return {"moved_to": kb.remove_persona((await req.json()).get("name"))}
    except ValueError as e:
        raise HTTPException(400, str(e))


# ---------------------------------------------------------------- 人格的记忆（knowledge\记忆，hub\app\memory.py）
@app.get("/api/memory")
async def memory_info():
    """每个人格的记忆：笔记条数、淡忘条数、最近 20 条。"""
    from app import memory
    out = []
    for p in kb.personas():
        s = memory.stats(p["name"])
        notes_dir = os.path.join(s["dir"], "笔记")
        recent = sorted((f for f in os.listdir(notes_dir) if f.endswith(".md")), reverse=True)[:20]
        s["recent"] = []
        for f in recent:
            try:
                n = memory._parse_note(os.path.join(notes_dir, f))
                s["recent"].append({k: n[k] for k in ("id", "time", "kind", "imp", "text", "path")})
            except Exception:
                pass
        out.append(s)
    return {"memory": out, "root": memory.ROOT}


def _mem_persona(name):
    if not kb.find_persona(name):
        raise HTTPException(404, "没有这个人格")
    return name


@app.get("/api/memory/notes")
async def memory_notes(persona: str):
    """记忆页面：全部笔记（带强度、被想起次数）、已淡忘的、上次整理。"""
    from app import memory
    _mem_persona(persona)
    async with memory._lock(persona):
        net = await memory.open_net(persona)
        try:
            now = time.time()
            notes = [{**{k: n[k] for k in ("id", "time", "kind", "imp", "text", "quote", "pinned", "who", "conv", "path")},
                      "strength": round(net.strength(i, now), 3), "recalls": max(0, len(net.access.get(i) or []) - 1),
                      "last": (net.access.get(i) or [n["t"]])[-1]} for i, n in net.notes.items()]
            links = net.db.execute("SELECT a, b, w FROM links").fetchall()
        finally:
            net.close()
    gone_dir = memory._dirs(persona)[2]
    gone = []
    for f in sorted(os.listdir(gone_dir), reverse=True):
        if f.endswith(".md"):
            try:
                n = memory._parse_note(os.path.join(gone_dir, f))
                gone.append({"file": f, "time": n["time"], "text": n["text"], "merged": f.startswith("合并_")})
            except Exception:
                pass
    notes.sort(key=lambda n: n["time"], reverse=True)
    log_p = os.path.join(memory.mem_dir(persona), "整理记录.md")
    log = open(log_p, encoding="utf-8").read()[-4000:] if os.path.exists(log_p) else ""
    return {"notes": notes, "links": [{"a": a, "b": b, "w": round(w, 3)} for a, b, w in links], "gone": gone,
            "last_sleep": memory.last_sleep(persona).get("t"), "log": log, "dir": memory.mem_dir(persona)}


@app.post("/api/memory/pin")
async def memory_pin(req: Request):
    from app import memory
    body = await req.json()
    _mem_persona(body.get("persona"))
    try:
        memory.set_pinned(body["persona"], body.get("id") or "", bool(body.get("pinned")))
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"ok": True}


@app.post("/api/memory/restore")
async def memory_restore(req: Request):
    from app import memory
    body = await req.json()
    _mem_persona(body.get("persona"))
    try:
        return {"path": memory.restore(body["persona"], body.get("file") or "")}
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.post("/api/memory/sleep")
async def memory_sleep(req: Request):
    """手动整理一次（平时每晚自动）。用常驻 CPU 的小模型，可能要一两分钟。"""
    from app import memory
    body = await req.json()
    _mem_persona(body.get("persona"))
    return await memory.sleep(body["persona"])


@app.post("/api/memory/forget")
async def memory_forget(req: Request):
    from app import memory
    body = await req.json()
    if not kb.find_persona(body.get("persona")):
        raise HTTPException(404, "没有这个人格")
    async with memory._lock(body["persona"]):
        net = await memory.open_net(body["persona"])
        try:
            n = net.forget(body.get("id") or "")
        except ValueError as e:
            raise HTTPException(404, str(e))
        finally:
            net.close()
    return {"forgot": n["id"]}


# ---------------------------------------------------------------- 规范的历史
@app.get("/api/kb/norms/history")
async def kb_norm_history(name: str):
    return {"history": kb.norm_history(name)}


@app.post("/api/kb/norms/restore")
async def kb_norm_restore(req: Request):
    body = await req.json()
    try:
        kb.restore_norm(body.get("name"), body.get("stamp"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    asyncio.get_running_loop().create_task(kb.scan())
    return {"history": kb.norm_history(body.get("name"))}


# ---------------------------------------------------------------- 自改记录、应用和撤回；高权限副本由 Agent 提交时直接应用，普通副本由用户审核
def _se_view(r):
    keep = ("id", "goal", "status", "summary", "files", "tests", "full", "t", "t_proposed", "t_applied", "t_reverted", "restart", "conv")
    out = {k: r.get(k) for k in keep}
    out["copy"], out["diff"] = display(r["copy"]), r.get("diff")
    out["can_apply"] = selfedit.enabled() and (not r["full"] or selfedit.full_mode())
    return out


@app.get("/api/selfedit")
async def se_list():
    return {"items": [_se_view(r) for r in selfedit.listing()[:50]], "enabled": selfedit.enabled(), "full": selfedit.full_mode()}


@app.get("/api/selfedit/{eid}")
async def se_get(eid: str):
    r = selfedit.get(eid)
    if not r:
        raise HTTPException(404, "没有这个改动")
    return _se_view(r)


@app.post("/api/selfedit/{eid}/apply")
async def se_apply(eid: str):
    try:
        r = selfedit.apply(eid)
    except ValueError as e:
        raise HTTPException(409, str(e))
    if r.get("restart"):
        selfedit.schedule_restart()
    return _se_view(r)


@app.post("/api/selfedit/{eid}/reject")
async def se_reject(eid: str):
    try:
        return _se_view(selfedit.reject(eid))
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.post("/api/selfedit/{eid}/revert")
async def se_revert(eid: str, req: Request):
    body = await req.json() if (req.headers.get("content-length") or "0") != "0" else {}
    try:
        r = selfedit.revert(eid, bool(body.get("force")))
    except ValueError as e:
        raise HTTPException(409, str(e))
    if r.get("restart"):
        selfedit.schedule_restart()
    return _se_view(r)


# ---------------------------------------------------------------- 通用后台任务（生图等）
@app.get("/api/jobs/{jid}/events")
async def job_events(jid: str, req: Request, start: int = 0):
    job = jobs.JOBS.get(jid)
    if not job:
        raise HTTPException(404, "任务不存在（服务重启过）")
    return StreamingResponse(jobs.sse(job, jobs.cursor(req, start)), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})


@app.post("/api/jobs/{jid}/stop")
async def job_stop(jid: str):
    job = jobs.JOBS.get(jid)
    if job:
        job.stopped = True
    return {"ok": True}


# ---------------------------------------------------------------- 图像（图片、视频）
@app.get("/api/img/info")
async def img_info():
    ok, vok = image.available(), image.video_available()
    os.makedirs(image.VIDEO_DIR, exist_ok=True)           # 新建子文件夹要先有 沙箱\视频
    return {"models": [{"id": k, "name": m["name"], "ok": ok[k], "kind": m["kind"]} for k, m in image.MODELS.items()],
            "sizes": {k: f"{w}×{h}" for k, (w, h) in image.SIZES.items()}, "presets": image.load_presets(), "history": image.history(),
            "folders": image.folders(),
            "video_models": [{"id": k, "name": m["name"], "ok": vok[k], "secs": m["secs"]} for k, m in image.VIDEO_MODELS.items()],
            "video_folders": image.folders(image.VIDEO_DIR),
            "loras": image.loras(), "lora_dir": image.lora_dir(),
            "model_arch": {**{k: m["kind"] for k, m in image.MODELS.items()}, **{k: m["arch"] for k, m in image.VIDEO_MODELS.items()}},
            "video_steps": {k: m["steps"] for k, m in image.VIDEO_MODELS.items()},
            "image_defaults": {k: {"steps": m["steps"], "cfg": m["cfg"]} for k, m in image.MODELS.items()}}


@app.post("/api/img/loras/open")
async def img_lora_open(req: Request):
    """在资源管理器里打开放 LoRA 的文件夹。只接受本机请求。"""
    if not is_local(req):
        raise HTTPException(403, "只能在这台电脑上使用")
    d = image.lora_dir()
    os.makedirs(d, exist_ok=True)
    subprocess.Popen(["explorer", d], creationflags=0x08000000)
    return {"ok": True}


@app.post("/api/img/loras")
async def img_lora_upload(req: Request, name: str):
    """把网页拖进来的 LoRA 文件存进 LoRA 文件夹（请求体就是文件本身）。同名的不覆盖；存完检查是不是 safetensors。"""
    name = os.path.basename(name.strip())
    if not name.lower().endswith(image.LORA_EXT) or image.BAD_NAME.search(name) or name.startswith("."):
        raise HTTPException(400, "只收 .safetensors 文件")
    d = image.lora_dir()
    os.makedirs(d, exist_ok=True)
    dst = os.path.join(d, name)
    if os.path.exists(dst):
        raise HTTPException(409, f"LoRA 文件夹里已经有 {name} 了")
    tmp = dst + ".part"
    try:
        with open(tmp, "wb") as f:
            async for chunk in req.stream():
                f.write(chunk)
        if image.lora_info(tmp) is None:
            raise HTTPException(400, "这个文件不是 safetensors 格式（或者没传完）")
        os.replace(tmp, dst)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return {"name": name, "loras": image.loras()}


@app.put("/api/img/presets")
async def img_presets(req: Request):
    items = (await req.json()).get("presets")
    if not isinstance(items, list):
        raise HTTPException(400, "格式不对")
    image.save_presets(items)
    return {"presets": items}


@app.post("/api/img/tags")
async def img_tags(req: Request):
    body = await req.json()
    try:
        await image.check_prompt(body.get("text") or "")
        tags = await image.to_tags(body.get("text") or "", image.MODELS.get(body.get("model"), {}).get("kind", "sdxl"))
        await image.check_prompt(tags)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"tags": tags}


@app.post("/api/img/jobs")
async def img_start(req: Request):
    body = await req.json()
    data = None
    if body.get("kind") in ("edit", "upscale"):
        data, _ = _image(body)
        if body.get("kind") == "edit" and not (body.get("instruction") or "").strip():
            raise HTTPException(400, "写一句要怎么改")
    elif not (body.get("prompt") or "").strip():
        raise HTTPException(400, "提示词是空的")
    if body.get("kind") == "video":
        if body.get("path") or body.get("image"):           # 首帧图可以不给：不给就是文生视频
            data, _ = _image(body)
        if body.get("model") and body["model"] not in image.VIDEO_MODELS and body["model"] not in image.LEGACY_VIDEO:
            raise HTTPException(400, "没有这个视频模型")
    try:
        if body.get("loras") and body.get("kind") in (None, "t2i", "video"):     # LoRA 不在、和模型对不上：提交时就报错
            v = body.get("kind") == "video"
            mk = image.LEGACY_VIDEO.get(body.get("model"), (body.get("model"),))[0] or ("h3" if v else "noobai")
            m = (image.VIDEO_MODELS if v else image.MODELS).get(mk)
            if m:
                image.pick_loras(body["loras"], m["arch"] if v else m["kind"])
        image.out_dir(body.get("folder"), image.VIDEO_DIR if body.get("kind") == "video" else image.OUT_DIR)
    except ValueError as e:
        raise HTTPException(400, str(e))
    job = image.start_job(body, data)
    return {"job": job.id, "agent_busy": _agent_busy()}


# ---------------------------------------------------------------- 识字
def _image(body):
    """网页贴进来的图（data URL，先存到 沙箱\\识字）或者已有的图片文件。返回 (字节, 路径)。"""
    try:
        if body.get("path"):
            p = _checked(body["path"])
            if os.path.getsize(p) > ocr.MAX_BYTES:       # 先看大小再读，别把几百 MB 的文件整个读进内存
                raise ValueError(f"图片太大（{os.path.getsize(p) / 2 ** 20:.0f} MB），最大 {ocr.MAX_BYTES // 2 ** 20} MB")
            return ocr.prepare(open(p, "rb").read()), p
        data = ocr.from_data_url(body.get("image"))
        return ocr.prepare(data), (ocr.save(data) if body.get("save") else None)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/ocr")
async def ocr_run(req: Request):
    body = await req.json()
    data, p = _image(body)
    try:
        text, secs = await ocr.recognize(data, body.get("mode") or "text")
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    return {"text": text, "secs": secs, "path": p, "show": display(p) if p else "", "agent_busy": _agent_busy()}


@app.post("/api/ocr/ask")
async def ocr_ask(req: Request):
    body = await req.json()
    data, _ = _image(body)
    if not (body.get("question") or "").strip():
        raise HTTPException(400, "问题是空的")

    async def gen():
        try:
            async for piece in ocr.ask(data, body["question"], body.get("ocr_text") or ""):
                yield piece
        except Exception as e:
            yield f"\n\n（出错：{str(e)[:300]}）"
    return StreamingResponse(gen(), media_type="text/plain; charset=utf-8", headers={"X-Accel-Buffering": "no", "X-Model": ocr.ask_model()})


# ---------------------------------------------------------------- 状态
_gpu_cache = {"t": 0, "v": None}


def _gpu():
    if time.time() - _gpu_cache["t"] < 3:
        return _gpu_cache["v"]
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,memory.total,utilization.gpu", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5, creationflags=0x08000000).stdout.strip().split(",")
        v = {"used": int(out[0]) / 1024, "total": int(out[1]) / 1024, "util": int(out[2])}
    except Exception:
        v = None
    _gpu_cache.update(t=time.time(), v=v)
    return v


@app.get("/api/health")
async def health():
    """服务活着没有（不用登录，只回一句，不带任何数据）：桌面程序启动、看门狗用（scripts/desktop_app.py 的 hub_up）。
    以前看登录页能不能打开，登录页是静态文件，后端出了问题也能打开。"""
    return {"ok": True}


@app.get("/api/storage")
async def storage_scan():
    """设置页“存储”：各类东西占了多少（app/storage.py；走一遍文件夹，结果留 60 秒）。"""
    return await asyncio.to_thread(storage.scan)


@app.post("/api/storage/clean")
async def storage_clean(req: Request):
    """把测试副本 / 浏览器资料挪到 <待删除位置>\\_待删除_存储清理_<日期>（不删，位置见 app/storage.py 的 trash_parent）。要求服务空闲。"""
    body = await req.json()
    if not (await activity_status())["idle"]:
        raise HTTPException(409, "有任务在跑，等跑完再挪")
    try:
        return await asyncio.to_thread(storage.clean, str(body.get("cat", "")))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/storage/delete")
async def storage_delete(req: Request):
    """彻底删除：{cat: "recycle", path?} 清空回收站；{cat: "trash", path?} 删待删除文件夹。不能恢复。"""
    body = await req.json()
    try:
        return await asyncio.to_thread(storage.delete, str(body.get("cat", "")), body.get("path") or None)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/activity")
async def activity_status():
    agent.prune_runs()
    tasks = [{"kind": "对话", "id": r.id, "label": r.conv} for r in agent.RUNS.values() if not r.done]
    tasks += [{"kind": j.kind, "id": j.id, "label": j.kind} for j in jobs.JOBS.values() if not j.done]
    tasks += list(activity.ACTIVE.values())
    return {"idle": not tasks, "tasks": tasks}


@app.get("/api/status")
async def status():
    try:
        running = (await llm.get("/running", timeout=5)).json().get("running", [])
    except Exception:
        running = None
    vm = psutil.virtual_memory()
    names = {k: e["name"] for k, e in M.items()}
    models = []
    for r in running or []:
        mid = r.get("model")
        last = llm.LAST_USED.get(mid)
        e = M.get(mid, {})
        ttl = None if e.get("device") == "cpu" else (e.get("ttl") or CFG["gpu_ttl_seconds"])
        left = max(0, int(ttl - (time.time() - last))) if ttl and last else None
        models.append({"id": mid, "name": names.get(mid, mid), "state": r.get("state"), "gpu": ttl is not None, "unload_in": left})
    return {"ok": running is not None, "models": models, "gpu": _gpu(),
            "ram": {"used": vm.used / 2**30, "total": vm.total / 2**30}, "sandbox": tools.SANDBOX}


@app.post("/api/unload")
async def unload():
    r = await llm.post("/api/models/unload")
    llm.LAST_USED.clear()
    return {"ok": r.status_code == 200}


# ---------------------------------------------------------------- 运行记录
# 正式服务每次启动、正常退出各记一行；Python 本身崩溃时把调用栈记进 hub_crash.log。
# 服务停了却没有“正常退出”、也没有崩溃记录，就是被外部直接结束的（重启脚本停掉的会记在 restart_hub.log）
LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")


def _life(msg):
    if TEST_MODE:
        return
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, "hub_life.log"), "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + f"[{os.getpid()}] {msg}\n")
    except OSError:
        pass


@app.on_event("startup")
async def _life_start():
    _life(f"启动，端口 {PORT}")


@app.on_event("shutdown")
async def _life_stop():
    _life("正常退出")


if __name__ == "__main__":
    if not TEST_MODE:
        import faulthandler
        os.makedirs(LOG_DIR, exist_ok=True)
        _crash = open(os.path.join(LOG_DIR, "hub_crash.log"), "a", encoding="utf-8")
        faulthandler.enable(_crash)
    uvicorn.run(app, host="127.0.0.1" if os.environ.get("HUB_PORT") else CFG["host"], port=PORT, log_level="warning")

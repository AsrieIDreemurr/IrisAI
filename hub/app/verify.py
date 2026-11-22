"""验证工具：run_js（用 Node 运行真实脚本）和 view_result（用 Chrome 看实际效果）。

view_result 通过 Chrome 调试接口按真实时间操作页面。实测无界面模式直接截图时动画不会推进，所以不用 --screenshot。
"""
import asyncio
import base64
import datetime as dt
import json
import os
import pathlib
import shutil
import socket
import time
import uuid

import httpx
import websockets

from .config import SANDBOX
from . import processes
from .paths import display, resolve

RUN_DIR = os.path.join(SANDBOX, ".运行")
SHOT_DIR = os.path.join(RUN_DIR, "截图")
HARNESS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "js", "page_harness.js")
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
VIEW_W, VIEW_H = 1024, 640
CHROME = next((p for p in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                           r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                           r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe") if os.path.exists(p)), None)


class Out:
    """和 tools.Result 一样的形状，避免循环导入。"""
    def __init__(self, text, meta=None):
        self.text, self.meta = text, meta or {}


async def _proc(args, cwd, timeout):
    proc = await asyncio.create_subprocess_exec(*args, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                                                env={**os.environ, "NODE_NO_WARNINGS": "1"})
    try:
        out, _ = await processes.communicate(proc, timeout)
        return proc.returncode, out.decode("utf-8", "replace")
    except asyncio.TimeoutError:
        return None, f"超时（{timeout} 秒），已停止。"


# ---------------------------------------------------------------- run_js
async def t_run_js(a, ctx):
    node = shutil.which("node")
    if not node:
        return Out("本机没有安装 Node，不能运行 JavaScript。")
    os.makedirs(RUN_DIR, exist_ok=True)
    if a.get("page"):
        page = resolve(a["page"])
        if not os.path.isfile(page):
            return Out(f"网页不存在：{display(page)}")
        job = {"html": page, "frames": int(a.get("frames") or 120), "every": int(a.get("every") or 10),
               "watch": a.get("watch") or [], "actions": a.get("actions") or [], "width": VIEW_W, "height": VIEW_H}
        jf = os.path.join(RUN_DIR, f"job_{uuid.uuid4().hex[:8]}.json")
        with open(jf, "w", encoding="utf-8") as f:
            json.dump(job, f, ensure_ascii=False)
        try:
            code, out = await _proc([node, HARNESS, jf], SANDBOX, 60)
        finally:
            os.remove(jf)
        head = f"用 Node 运行 {display(page)} 里的真实脚本 {job['frames']} 帧（画布 {VIEW_W}×{VIEW_H}，60 帧/秒）。\n"
        return Out(head + (out.strip() or "运行成功，没有输出。"), {"exit": code, "verify": True})
    src = a.get("code", "")
    jf = os.path.join(RUN_DIR, f"run_{uuid.uuid4().hex[:8]}.js")
    with open(jf, "w", encoding="utf-8") as f:
        f.write(src)
    try:
        code, out = await _proc([node, jf], SANDBOX, 120)
    finally:
        os.remove(jf)
    out = out.replace(jf, "脚本")
    if len(out) > 6000:
        out = "……（前面省略）\n" + out[-6000:]
    ctx.refresh()
    return Out(f"退出码 {code}\n" + (out.strip() or "运行成功，没有输出。"), {"exit": code, "verify": True})


# ---------------------------------------------------------------- view_result
def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _point(p):
    if p in (None, "center", "中心"):
        return VIEW_W / 2, VIEW_H / 2
    if isinstance(p, dict):
        return float(p.get("x", 0)), float(p.get("y", 0))
    return float(p[0]), float(p[1])


class _CDP:
    def __init__(self, ws):
        self.ws, self.n, self.events = ws, 0, []

    async def call(self, method, **params):
        self.n += 1
        my = self.n
        await self.ws.send(json.dumps({"id": my, "method": method, "params": params}))
        while True:
            msg = json.loads(await asyncio.wait_for(self.ws.recv(), 20))
            if msg.get("id") == my:
                if "error" in msg:
                    raise RuntimeError(msg["error"].get("message"))
                return msg.get("result", {})
            self.events.append(msg)

    async def drain(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            try:
                self.events.append(json.loads(await asyncio.wait_for(self.ws.recv(), max(0.01, end - time.time()))))
            except asyncio.TimeoutError:
                break

    def errors(self):
        out = []
        for e in self.events:
            m, p = e.get("method"), e.get("params", {})
            if m == "Runtime.exceptionThrown":
                d = p.get("exceptionDetails", {})
                out.append(f"第 {d.get('lineNumber', 0) + 1} 行：{(d.get('exception') or {}).get('description') or d.get('text')}")
            elif m == "Runtime.consoleAPICalled" and p.get("type") == "error":
                out.append("console.error：" + " ".join(str(x.get("value", x.get("description", ""))) for x in p.get("args", [])))
            elif m == "Log.entryAdded" and p.get("entry", {}).get("level") == "error":
                out.append(p["entry"].get("text", ""))
        return list(dict.fromkeys(out))[:10]


async def t_view_result(a, ctx):
    url, cookie = (a.get("url") or "").strip(), None
    if url:
        # 只能打开自改副本的测试实例，自动带上登录
        from . import selfedit
        base = selfedit.test_base()
        cookie = selfedit.test_cookie()
        if not base or not cookie:
            return Out("自改副本的测试实例没有在运行，先调用 self_edit_test。")
        if not url.startswith(base):
            return Out(f"url 只能打开自改副本的测试实例 {base}/ 下的页面；本地文件请用 path。")
        p, ext = url, ".html"
    else:
        p = resolve(a.get("path"))
        if not os.path.isfile(p):
            return Out(f"文件不存在：{display(p)}")
        ext = os.path.splitext(p)[1].lower()
    if ext in IMAGE_EXT:
        return Out(f"已附上图片 {display(p)}，请看图核对。", {"images": [p], "verify": True})
    if ext not in (".html", ".htm"):
        return Out("view_result 只能看网页（.html）和图片。代码请用 run_js 或 run_python 运行。")
    if not CHROME:
        return Out("本机没有找到 Chrome 或 Edge，不能截图。")
    os.makedirs(SHOT_DIR, exist_ok=True)
    wait = min(max(int(a.get("wait_ms") or 1500), 0), 15000)
    frames = min(max(int(a.get("frames") or 1), 1), 4)
    interval = min(max(int(a.get("interval_ms") or 300), 50), 3000)
    actions = a.get("actions") or []
    port = _free_port()
    profile = os.path.join(RUN_DIR, "chrome-profile-" + uuid.uuid4().hex)
    proc = await asyncio.create_subprocess_exec(
        CHROME, "--headless=new", f"--remote-debugging-port={port}", f"--user-data-dir={profile}", f"--window-size={VIEW_W},{VIEW_H}",
        "--hide-scrollbars", "--mute-audio", "--no-first-run", "--no-default-browser-check", "--disable-extensions",
        "--disable-background-timer-throttling", "--disable-renderer-backgrounding", "about:blank",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    shots, log = [], []
    stamp = dt.datetime.now().strftime("%H%M%S") + "_" + uuid.uuid4().hex
    try:
        ws_url = None
        async with httpx.AsyncClient(timeout=2) as c:
            for _ in range(50):
                try:
                    pages = (await c.get(f"http://127.0.0.1:{port}/json")).json()
                    ws_url = next((x["webSocketDebuggerUrl"] for x in pages if x.get("type") == "page"), None)
                    if ws_url:
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.2)
        if not ws_url:
            return Out("Chrome 没有启动成功，不能截图。")
        async with websockets.connect(ws_url, max_size=50_000_000) as ws:
            cdp = _CDP(ws)
            for m in ("Page.enable", "Runtime.enable", "Log.enable"):
                await cdp.call(m)
            await cdp.call("Emulation.setDeviceMetricsOverride", width=VIEW_W, height=VIEW_H, deviceScaleFactor=1, mobile=False)
            if url:
                from . import selfedit
                await cdp.call("Network.enable")
                await cdp.call("Network.setCookie", name="hub", value=cookie, url=selfedit.test_base() + "/")
                await cdp.call("Page.navigate", url=url)
            else:
                await cdp.call("Page.navigate", url=pathlib.Path(p).as_uri())
            for _ in range(100):
                if any(e.get("method") == "Page.loadEventFired" for e in cdp.events):
                    break
                await cdp.drain(0.1)

            watch = [str(w) for w in (a.get("watch") or [])][:8]

            async def read_vars():
                """在真实页面里计算 watch 表达式（脚本顶层的 let/const 也能读到）。"""
                vals = []
                for w in watch:
                    try:
                        r = await cdp.call("Runtime.evaluate", expression=f"(() => {{ try {{ return JSON.stringify({w}); }} catch (e) {{ return '出错：' + e.message; }} }})()",
                                           returnByValue=True)
                        v = r.get("result", {}).get("value")
                        try:
                            v = json.dumps(json.loads(v), ensure_ascii=False)[:200] if v and not v.startswith("出错") else v
                        except (ValueError, AttributeError):
                            pass
                        vals.append(f"{w} = {v}")
                    except Exception as e:
                        vals.append(f"{w} = 读取失败：{e}")
                return "；".join(vals)

            async def shoot(label):
                r = await cdp.call("Page.captureScreenshot", format="jpeg", quality=80)
                f = os.path.join(SHOT_DIR, f"{stamp}_{len(shots) + 1}.jpg")
                with open(f, "wb") as fh:
                    fh.write(base64.b64decode(r["data"]))
                shots.append(f)
                log.append(f"第 {len(shots)} 张：{label}" + (f"。变量：{await read_vars()}" if watch else ""))

            await cdp.drain(wait / 1000)
            t0 = wait
            if not actions:
                for i in range(frames):
                    if i:
                        await cdp.drain(interval / 1000)
                        t0 += interval
                    await shoot(f"打开后 {t0 / 1000:.1f} 秒")
            else:
                await shoot(f"打开后 {wait / 1000:.1f} 秒，操作之前")
                async def locate(act):
                    """按 selector 或 text 找元素，返回中心坐标和说明；找不到返回 None。"""
                    sel, txt = act.get("selector"), act.get("text")
                    if not sel and not txt:
                        return None
                    js = ("(() => { const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };"
                          f" let e = {json.dumps(sel)} ? document.querySelector({json.dumps(sel)}) : null;"
                          f" if (!e && {json.dumps(txt)}) {{ const t = {json.dumps(txt or '')}.trim();"
                          "   const all = [...document.querySelectorAll('button, a, input, label, [role=button], [onclick], li, span, div, p, h1, h2, h3')].filter(vis);"
                          "   e = all.find(x => (x.value || x.textContent || '').trim() === t) || all.find(x => (x.value || x.textContent || '').trim().includes(t) && x.children.length === 0); }"
                          " if (!e) return null; e.scrollIntoView({block: 'center'}); const r = e.getBoundingClientRect();"
                          " return [r.left + r.width / 2, r.top + r.height / 2, e.tagName.toLowerCase() + (e.id ? '#' + e.id : '')]; })()")
                    r = await cdp.call("Runtime.evaluate", expression=js, returnByValue=True)
                    v = r.get("result", {}).get("value")
                    return v

                for act in actions:
                    kind = act.get("type")
                    found = await locate(act)
                    if (act.get("selector") or act.get("text")) and not found:
                        log.append(f"操作失败：找不到元素（selector={act.get('selector') or '无'}，text={act.get('text') or '无'}），这一步没有执行")
                        continue
                    if found:
                        act = {**act, ("from" if kind == "drag" else "at"): found[:2]}
                        if kind == "drag" and not act.get("to"):
                            act["to"] = [found[0] + 100, found[1]]
                    if kind in ("drag", "throw"):
                        x0, y0 = _point(act.get("from"))
                        x1, y1 = _point(act.get("to"))
                        ms = int(act.get("ms") or 200)
                        await cdp.call("Input.dispatchMouseEvent", type="mouseMoved", x=x0, y=y0)
                        await cdp.call("Input.dispatchMouseEvent", type="mousePressed", x=x0, y=y0, button="left", clickCount=1)
                        steps = 10
                        for k in range(1, steps + 1):
                            await cdp.call("Input.dispatchMouseEvent", type="mouseMoved", x=x0 + (x1 - x0) * k / steps,
                                           y=y0 + (y1 - y0) * k / steps, button="left", buttons=1)
                            await asyncio.sleep(ms / 1000 / steps)
                        await cdp.call("Input.dispatchMouseEvent", type="mouseReleased", x=x1, y=y1, button="left", clickCount=1)
                        log.append(f"操作：从 ({x0:.0f},{y0:.0f}) 拖到 ({x1:.0f},{y1:.0f})，用时 {ms} 毫秒后松开")
                    elif kind == "click":
                        x, y = _point(act.get("at"))
                        await cdp.call("Input.dispatchMouseEvent", type="mousePressed", x=x, y=y, button="left", clickCount=1)
                        await cdp.call("Input.dispatchMouseEvent", type="mouseReleased", x=x, y=y, button="left", clickCount=1)
                        log.append(f"操作：点击 ({x:.0f},{y:.0f})" + (f"，即 {found[2]}「{act.get('text') or act.get('selector')}」" if found else ""))
                    elif kind == "wait":
                        await cdp.drain(int(act.get("ms") or 500) / 1000)
                        log.append(f"操作：等待 {int(act.get('ms') or 500)} 毫秒")
                    elif kind == "shot":
                        if len(shots) < 6:
                            await shoot("操作中途")
                        continue
                    if watch and kind != "wait":
                        await cdp.drain(0.05)
                    if watch:
                        log.append(f"　这时的变量：{await read_vars()}")
                for i in range(frames):
                    await cdp.drain(interval / 1000)
                    await shoot(f"操作后 {(i + 1) * interval / 1000:.1f} 秒")
            errors = cdp.errors()
    finally:
        await processes.finish(proc)
    text = (f"在 {VIEW_W}×{VIEW_H} 的窗口里打开 {p if url else display(p)}，按真实时间截图 {len(shots)} 张，已附上请看图核对。\n" + "\n".join(log) +
            "\n页面报错：" + ("；".join(errors) if errors else "没有"))
    if any("is not defined" in l for l in log):
        text += ("\n提示：变量写在函数或模块内部，从外面读不到。在网页脚本里加一行把要看的对象挂到全局，"
                 "例如 window.debug = { ball, box }，再用 watch 读 debug.ball.position.y。")
    return Out(text, {"images": shots, "verify": True, "page_errors": len(errors)})

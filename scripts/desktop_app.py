"""IrisAI 的独立窗口：用 Windows 自带的 WebView2 打开网页服务（端口按 config\\hub.json，默认 8088），不经过浏览器。

- 自己的窗口、图标和任务栏位置；关掉窗口缩到右下角托盘，托盘菜单可以打开或退出。
- 只开一个：再次双击时把已有的窗口调到前面。
- 网页服务没在运行时，先运行开机启动脚本（会同时检查调度器），等它起来再打开。
- 登录状态存在 data\\desktop，登录一次 90 天有效。
- 网页里的外部链接用系统默认浏览器打开。
- 桌面桌宠（scripts/desktop_pet.py）：设置里“桌宠”选“桌面上”时，看板娘站在任务栏上，网页里不再放；单击她弹出聊天小窗
  （入口页的随手问对话框，/?petchat=1，同一个话题、同一套主题样式），双击打开主窗口；托盘菜单“桌宠在桌面上”也能切换（改的是同一个设置）。
  网页把 Agent 的动静通过 pet_event 转给她，她跟着换表情、说话。

用法：hub\\.venv\\Scripts\\pythonw.exe scripts\\desktop_app.py（桌面快捷方式就是这样启动的）
"""
import ctypes
import ctypes.wintypes
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import desktop_pet  # noqa: E402  桌面桌宠（同一个文件夹）

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _hub_cfg():
    try:
        with open(os.path.join(ROOT, "config", "hub.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


# 网页服务的端口跟着服务自己的配置（config\hub.json 的 port）走，以前写死 8088，改了端口桌面程序就连不上
HUB_CFG = _hub_cfg()
URL = f"http://127.0.0.1:{int(HUB_CFG.get('port') or 8088)}/"
APP_NAME = "IrisAI"      # 软件名字（2026-11-07 由“本地助手”改名，取自虹膜）；找窗口也按它找


def pick_icon():
    """按网页设置里的主题选图标：主题文件夹里有 icon.ico 就用它（古明地恋主题是恋之瞳），没有就用默认的 IrisAI 虹膜。
    和网页标签页的小图标同一个规则（hub/static/js/skin.js 的 setIcon）。换主题后重开窗口生效。"""
    default = os.path.join(ROOT, "hub", "static", "img", "app.ico")
    try:
        with open(os.path.join(ROOT, "data", "settings.json"), encoding="utf-8") as f:
            skin = (json.load(f).get("look") or {}).get("skin") or "default"
        p = os.path.join(desktop_pet.THEMES, skin, "icon.ico")
        if skin != "default" and os.path.isfile(p):
            return p
    except Exception:
        pass
    return default


ICON = pick_icon()
STORAGE = os.path.join(ROOT, "data", "desktop")
LOCK_PORT = int(HUB_CFG.get("desktop_lock_port") or 8087)   # 只开一个：第一个实例占住这个端口，后来的连上去让它显示窗口（hub.json 可改）
APP_ID = "LocalAI.Assistant"
LOG = os.path.join(ROOT, "logs", "desktop_app.log")


def log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%F %T ") + msg + "\n")
    except OSError:
        pass


def already_running():
    try:
        with socket.create_connection(("127.0.0.1", LOCK_PORT), timeout=1) as s:
            s.sendall(b"show")
        return True
    except OSError:
        return False


def _others():
    """别的桌面程序进程（不含自己和自己的上级：venv 的 pythonw 会再起一个真正的 Python）。"""
    import psutil
    me = psutil.Process()
    mine = {me.pid} | {p.pid for p in me.parents()}
    out = []
    for p in psutil.process_iter(["pid", "cmdline"]):
        if p.info["pid"] not in mine and any(a.endswith("desktop_app.py") for a in (p.info["cmdline"] or [])):
            out.append(p)
    return out


def _main_window_of(pids):
    """这些进程里有没有看得见的主窗口（标题就叫 APP_NAME）。"""
    u, found = ctypes.windll.user32, []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(h, _):
        pid = ctypes.c_ulong()
        u.GetWindowThreadProcessId(ctypes.c_void_p(h), ctypes.byref(pid))
        if pid.value in pids and u.IsWindowVisible(ctypes.c_void_p(h)):
            t = ctypes.create_unicode_buffer(64)
            u.GetWindowTextW(ctypes.c_void_p(h), t, 64)
            if t.value == APP_NAME:
                found.append(h)
        return True
    u.EnumWindows(cb, 0)
    return bool(found)


def take_over_if_stuck():
    """已经开着一个时，叫它显示窗口；6 秒内看不到它的主窗口，说明它卡住或窗口没了
    （2026-11-22：开了 8 天的实例主窗口没了、还占着锁端口，双击快捷方式没反应），就结束它、自己接手。
    返回 True 表示那边正常显示了，这边退出。"""
    others = _others()
    if any(time.time() - p.create_time() < 90 for p in others):
        return True                          # 刚打开的还在等网页服务（最多 60 秒才出窗口），别误杀
    pids = {p.pid for p in others}
    for _ in range(12):
        time.sleep(0.5)
        if _main_window_of(pids):
            return True
    import psutil
    procs = _others()
    log(f"已开着的实例 {[p.pid for p in procs]} 叫不出窗口，结束它重新打开")
    for p in procs:
        try:
            p.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(procs, timeout=5)
    for _ in range(20):                      # 等锁端口放出来
        try:
            with socket.socket() as s:
                s.bind(("127.0.0.1", LOCK_PORT))
            return False
        except OSError:
            time.sleep(0.25)
    return False


def hub_up():
    """网页服务活着没有：先问 /api/health（后端真的在回话）；旧版服务没有这个接口（要登录，回 401）时退回看登录页。"""
    try:
        with urllib.request.urlopen(URL + "api/health", timeout=2) as r:
            return json.load(r).get("ok") is True
    except urllib.error.HTTPError:
        pass                    # 服务在回话，只是不认这个接口（旧版）：看登录页
    except Exception:
        return False
    try:
        urllib.request.urlopen(URL + "login", timeout=2)
        return True
    except Exception:
        return False


def ensure_hub():
    if hub_up():
        return True
    log("网页服务没在运行，运行开机启动脚本")
    import shutil                       # 装了 PowerShell 7 用 pwsh，没装用 Windows 自带的 powershell（5.1）
    subprocess.Popen([shutil.which("pwsh") or "powershell", "-NoProfile", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass", "-File",
                      os.path.join(ROOT, "scripts", "start-llama-swap.ps1")], creationflags=0x08000000)
    for _ in range(60):
        time.sleep(1)
        if hub_up():
            return True
    return False


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", _RECT), ("rcWork", _RECT), ("dwFlags", ctypes.c_ulong)]


class Frame:
    """无边框窗口的标题栏操作，给网页调用（window.pywebview.api.xxx）。
    拖动交给 Windows 原生的“按住标题栏拖动”，所以拖到屏幕边缘照样能贴边分屏；最大化只铺满工作区，不盖住任务栏。"""

    def __init__(self):
        self._hwnd = None
        self._restore = None        # 最大化之前的位置和大小
        self.on_close = None
        self._bridge = None         # 桌面桌宠（PetBridge），下划线开头的不暴露给网页

    # ---- 桌面桌宠：网页（static/js/pet.js）把 Agent 的动静、桌宠放在哪转过来
    def pet_event(self, ev):
        if self._bridge:
            self._bridge.pet.feed(ev)

    def pet_mode(self, mode, skin):
        if self._bridge:
            self._bridge.mode(mode, skin)

    def pet_surfaces(self, rects, dpr):
        """页面上她能站的地方（对话页的输入框，网页坐标）和网页缩放；桌面模式下 static/js/pet.js 每有变化报一次。"""
        if self._bridge:
            self._bridge.set_surfaces(rects, dpr)

    def _h(self):
        if self._hwnd:
            return self._hwnd
        u = ctypes.windll.user32
        found = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def cb(h, _):
            pid = ctypes.c_ulong()
            u.GetWindowThreadProcessId(ctypes.c_void_p(h), ctypes.byref(pid))
            buf = ctypes.create_unicode_buffer(64)
            u.GetWindowTextW(ctypes.c_void_p(h), buf, 64)
            if pid.value == os.getpid() and buf.value == APP_NAME:
                found.append(h)
            return True
        u.EnumWindows(cb, 0)
        self._hwnd = found[0] if found else None
        return self._hwnd

    def setup(self):
        """窗口显示后调用一次：Windows 11 的圆角；补上最小化样式，点任务栏图标能最小化和还原。"""
        h = self._h()
        if not h:
            return
        u = ctypes.windll.user32
        GWL_STYLE, WS_MINIMIZEBOX, WS_MAXIMIZEBOX, WS_SYSMENU = -16, 0x00020000, 0x00010000, 0x00080000
        u.GetWindowLongW.restype = ctypes.c_long
        style = u.GetWindowLongW(ctypes.c_void_p(h), GWL_STYLE)
        u.SetWindowLongW(ctypes.c_void_p(h), GWL_STYLE, style | WS_MINIMIZEBOX | WS_MAXIMIZEBOX | WS_SYSMENU)
        pref = ctypes.c_int(2)      # DWMWA_WINDOW_CORNER_PREFERENCE = 33，DWMWCP_ROUND = 2（Windows 10 上没有效果，也不报错）
        ctypes.windll.dwmapi.DwmSetWindowAttribute(ctypes.c_void_p(h), 33, ctypes.byref(pref), 4)

    def _rect(self):
        r = _RECT()
        ctypes.windll.user32.GetWindowRect(ctypes.c_void_p(self._h()), ctypes.byref(r))
        return r

    def _work(self):
        u = ctypes.windll.user32
        u.MonitorFromWindow.restype = ctypes.c_void_p
        mon = u.MonitorFromWindow(ctypes.c_void_p(self._h()), 2)
        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(_MONITORINFO)
        u.GetMonitorInfoW(ctypes.c_void_p(mon), ctypes.byref(mi))
        return mi.rcWork

    def _place(self, x, y, w, h):
        ctypes.windll.user32.SetWindowPos(ctypes.c_void_p(self._h()), None, int(x), int(y), int(w), int(h), 0x0004 | 0x0010)  # 不改层次、不抢焦点

    # ---- 下面这些给网页调用
    def drag(self):
        """按住顶栏拖动窗口。最大化时先还原，再让窗口跟着鼠标走。"""
        h = self._h()
        if not h:
            return
        u = ctypes.windll.user32
        if self._restore:
            pt = ctypes.wintypes.POINT()
            u.GetCursorPos(ctypes.byref(pt))
            x, y, w, hh = self._restore
            self._restore = None
            self._place(pt.x - w // 2, pt.y - 16, w, hh)
        u.ReleaseCapture()
        u.PostMessageW(ctypes.c_void_p(h), 0x00A1, 2, 0)     # WM_NCLBUTTONDOWN, HTCAPTION

    def _check_max(self):
        """记着“最大化了”，但窗口其实已经不是铺满工作区（被系统、快捷键、换显示器挪过）：就当没最大化。"""
        r, w = self._rect(), self._work()
        full = (r.left, r.top, r.right, r.bottom) == (w.left, w.top, w.right, w.bottom)
        if self._restore and not full:
            self._restore = None
        elif full and not self._restore:     # 一打开就铺满了（或被系统铺满）：也算最大化，还原成居中的七成大小
            ww, wh = w.right - w.left, w.bottom - w.top
            self._restore = (w.left + ww * 3 // 20, w.top + wh * 3 // 20, ww * 7 // 10, wh * 7 // 10)
        return bool(self._restore)

    def toggle_max(self):
        if self._check_max():
            self._place(*self._restore)
            self._restore = None
            return False
        r = self._rect()
        self._restore = (r.left, r.top, r.right - r.left, r.bottom - r.top)
        w = self._work()
        self._place(w.left, w.top, w.right - w.left, w.bottom - w.top)
        return True

    def is_max(self):
        """网页每次加载、窗口大小变了都来问一次，按钮图标和双击才对得上（2026-11-22：页面一刷新就以为没最大化，
        再点“最大化”反而还原成左上角那块，看起来像只剩左边一半）。"""
        return self._check_max()

    def minimize(self):
        ctypes.windll.user32.ShowWindow(ctypes.c_void_p(self._h()), 6)     # SW_MINIMIZE

    def close(self):
        if self.on_close:
            self.on_close()

    def bounds(self):
        r = self._rect()
        return [r.left, r.top, r.right - r.left, r.bottom - r.top]

    def set_bounds(self, x, y, w, h):
        """拖窗口边缘调大小时，网页按鼠标位置算好新的位置和大小传过来。"""
        self._restore = None
        self._place(x, y, max(int(w), 900), max(int(h), 600))


CHAT_TITLE = APP_NAME + " 随手问"


class PetChatApi:
    """聊天小窗（入口页 /?petchat=1：只显示随手问对话框，见 static/js/portal.js 的 SOLO）调用的接口。"""

    def __init__(self, bridge):
        self._b = bridge

    def pet_event(self, ev):
        self._b.pet.feed(ev)

    def close_chat(self):
        self._b.close_chat()

    def open_main(self, path):
        self._b.open_main(path)


class PetBridge:
    """桌面桌宠（scripts/desktop_pet.py）和桌面程序之间：单击她弹出聊天小窗、她挪动时小窗跟着、按设置开关、换主题换角色。
    小窗是一个普通的 pywebview 窗口（WebView2 做不了透明，但中文输入、Markdown、确认卡片都现成），平时藏着，用 Win32 消息摆位置和显隐，
    不碰 WinForms 对象（跨线程改它会卡死，见 set_icon 的说明）。"""

    def __init__(self, show_main, load_main, main_hwnd=None, main_win=None):
        self.show_main, self.load_main = show_main, load_main
        self.main_hwnd = main_hwnd or (lambda: None)     # 主窗口的句柄（Frame._h）
        self.main_win = main_win                          # 主窗口（pywebview），告诉入口页她站在哪
        self.pet = desktop_pet.DesktopPet(on_open=show_main, on_click=self.open_chat, on_move=self.place_chat,
                                          surfaces=self.surfaces, on_pos=self.pet_moved)
        self.chat = None            # pywebview 窗口，main() 里建
        self._chat_hwnd = None
        self.skin = None
        self._rects, self._dpr = [], 1.0
        self._pos, self._pos_evt = None, threading.Event()
        threading.Thread(target=self._pos_loop, daemon=True, name="pet-pos").start()

    # ---- 她和主窗口页面的互动
    def set_surfaces(self, rects, dpr):
        self._rects = rects if isinstance(rects, list) else []
        self._dpr = float(dpr or 1)
        self._pos_evt.set()

    def _origin(self):
        """主窗口网页内容左上角的屏幕坐标；窗口藏着、最小化时返回 None。"""
        h = self.main_hwnd()
        u = ctypes.windll.user32
        if not h or not u.IsWindowVisible(ctypes.c_void_p(h)) or u.IsIconic(ctypes.c_void_p(h)):
            return None
        pt = ctypes.wintypes.POINT(0, 0)
        u.ClientToScreen(ctypes.c_void_p(h), ctypes.byref(pt))
        return h, pt.x, pt.y

    def surfaces(self):
        """桌宠线程每 0.1 秒问一次：输入框换成屏幕坐标，只给露在外面的（上沿取几个点，至少一个点上面是主窗口）。"""
        o = self._origin()
        if not o or not self._rects:
            return []
        h, ox, oy = o
        u = ctypes.windll.user32
        u.WindowFromPoint.argtypes = [ctypes.wintypes.POINT]
        u.WindowFromPoint.restype = ctypes.c_void_p
        u.GetAncestor.restype = ctypes.c_void_p
        pet_h, k, out = self.pet.hwnd, self._dpr, []
        for r in self._rects:
            try:
                left, right, top = ox + r["l"] * k, ox + r["r"] * k, oy + r["t"] * k
            except (KeyError, TypeError):
                continue
            seen = False
            for fx in (0.15, 0.5, 0.85):
                pt = ctypes.wintypes.POINT(int(left + (right - left) * fx), int(top + 6 * k))
                w = u.WindowFromPoint(pt)
                root = u.GetAncestor(ctypes.c_void_p(w), 2) if w else None      # GA_ROOT
                if root == h:
                    seen = True
                    break
            if seen:
                out.append({"id": str(r.get("id", "")), "left": left, "right": right, "top": top})
        return out

    def pet_moved(self, rect):
        self._pos = rect
        self._pos_evt.set()

    def _pos_loop(self):
        """把她站的位置（换成主窗口的网页坐标）告诉入口页，对话框按它让位（portal.js 的 irisDeskPet）。
        单独一个线程：evaluate_js 要等页面回话，不能卡住桌宠。窗口被拖动时她的网页坐标也会变，所以 0.3 秒也看一次"""
        last = None
        while True:
            self._pos_evt.wait(0.3)
            self._pos_evt.clear()
            try:
                o = self._origin()
                msg = None
                if o and self._pos and self.pet.visible:
                    _, ox, oy = o
                    x, y, S, _ = self._pos
                    k = self._dpr
                    msg = {"x": round((x - ox) / k), "y": round((y - oy) / k), "w": round(S / k), "h": round(S / k)}
                if msg != last and self.main_win:
                    last = msg
                    self.main_win.evaluate_js(f"window.irisDeskPet && irisDeskPet({json.dumps(msg)})")
            except Exception as e:
                log(f"告诉入口页桌宠位置出错：{e}")
                time.sleep(1)
            time.sleep(0.06)

    def _hwnd(self):
        if self._chat_hwnd:
            return self._chat_hwnd
        u = ctypes.windll.user32
        found = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def cb(h, _):
            pid = ctypes.c_ulong()
            u.GetWindowThreadProcessId(ctypes.c_void_p(h), ctypes.byref(pid))
            buf = ctypes.create_unicode_buffer(64)
            u.GetWindowTextW(ctypes.c_void_p(h), buf, 64)
            if pid.value == os.getpid() and buf.value == CHAT_TITLE:
                found.append(h)
            return True
        u.EnumWindows(cb, 0)
        if found:
            self._chat_hwnd = found[0]
            pref = ctypes.c_int(2)          # Windows 11 圆角
            ctypes.windll.dwmapi.DwmSetWindowAttribute(ctypes.c_void_p(found[0]), 33, ctypes.byref(pref), 4)
        return self._chat_hwnd

    def place_chat(self, rect, show=False):
        """小窗放在她身旁头部高度：右边放得下放右边，不然放左边；不出工作区。"""
        h = self._hwnd()
        if not h:
            return
        rx, ry, S, _ = rect
        s = self.pet.scale
        w, hh = int(360 * s), int(480 * s)
        work = self.pet._work()
        x = rx + int(S * 0.86)
        if x + w > work.right - 8:
            x = rx + int(S * 0.14) - w
        x = max(work.left + 8, x)
        y = min(max(ry + int(S * 0.4) - hh, work.top + 8), work.bottom - hh - 8)
        u = ctypes.windll.user32
        u.SetWindowPos(ctypes.c_void_p(h), ctypes.c_void_p(-1), int(x), int(y), w, hh, 0x4000 | 0x0010)   # 置顶、异步、不抢焦点
        if show:
            u.ShowWindowAsync(ctypes.c_void_p(h), 5)       # SW_SHOW
            u.SetForegroundWindow(ctypes.c_void_p(h))

    def open_chat(self, rect):
        if not self.chat:
            return
        self.pet.chat_open = True
        self.place_chat(rect, show=True)
        # 叫页面接上入口页那边可能换过的话题、把光标放进输入框（evaluate_js 会等结果，放到别的线程）。
        # 小窗不在聊天页（启动时还没登录被转去登录页、登录后跑去了入口页）就重新打开聊天页
        def wake():
            try:
                if "petchat=1" not in (self.chat.get_current_url() or ""):
                    self.chat.load_url(URL + "?petchat=1")
                else:
                    self.chat.evaluate_js("window.petChatOpened && petChatOpened()")
            except Exception as e:
                log(f"聊天小窗出错：{e}")
        threading.Thread(target=wake, daemon=True).start()

    def close_chat(self):
        h = self._hwnd()
        if h:
            ctypes.windll.user32.ShowWindowAsync(ctypes.c_void_p(h), 0)    # SW_HIDE
        self.pet.chat_open = False

    def open_main(self, path):
        self.close_chat()
        try:
            self.load_main(URL.rstrip("/") + path)
        except Exception as e:
            log(f"打开主窗口失败：{e}")
        self.show_main()

    def mode(self, mode, skin):
        """网页报来的设置：桌宠放在哪（web / desktop）、哪个主题。主题变了换角色。"""
        if self.skin is not None and skin != self.skin:
            self.pet.reload(skin)          # 直接按网页报来的主题换：设置文件要等网页存完（约 0.4 秒）才是新的
        self.pet.want_skin = skin or None
        self.skin = skin
        if mode == "desktop":
            self.pet.set_visible(True)
        else:
            self.pet.set_visible(False)
            self.close_chat()


def main():
    if already_running() and take_over_if_stuck():
        return
    # 任务栏按这个编号分组，显示自己的图标，不和 Python 或浏览器挤在一起
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    srv = socket.socket()
    srv.bind(("127.0.0.1", LOCK_PORT))
    srv.listen(4)

    import webview
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    ok = ensure_hub()
    start_url = URL if ok else "data:text/html;charset=utf-8," + urllib.request.quote(
        "<body style='font:15px Microsoft YaHei UI;background:#1c1d22;color:#ddd;padding:40px'>"
        "网页服务 60 秒内没有启动起来。看一下 " + os.path.join(ROOT, "logs", "hub.log") + "，然后重新打开 " + APP_NAME + "。</body>")
    frame = Frame()
    # 无边框：不要 Windows 自带的标题栏，网页顶栏当标题栏用（拖动、双击最大化、右上角三个按钮）；shadow 给无边框窗口加阴影
    win = webview.create_window(APP_NAME, start_url, width=1440, height=900, min_size=(900, 600), background_color="#1c1d22",
                                frameless=True, easy_drag=False, shadow=True, js_api=frame,
                                text_select=True)     # pywebview 默认禁止选中文字，打开才能复制
    state = {"quit": False}
    frame.on_close = lambda: threading.Timer(0.05, win.hide).start()     # 关闭按钮和点叉一样，缩到托盘

    def show():
        try:
            win.show()
            win.restore()
            win.native.Activate()
        except Exception as e:
            log(f"显示窗口失败：{e}")

    def on_closing():
        if state["quit"]:
            return True
        # 关窗口只是缩到托盘。这个事件在窗口线程里触发，直接隐藏会被吞掉，稍后在别的线程里隐藏
        threading.Timer(0.05, win.hide).start()
        return False

    def set_icon():
        """窗口图标默认是 Python 的，换成自己的。用 Windows 消息设置，不碰 WinForms 对象（跨线程改它会和窗口线程互相等待，卡死）。"""
        try:
            u = ctypes.windll.user32
            u.LoadImageW.restype = ctypes.c_void_p
            hwnds = []

            @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
            def cb(h, _):
                pid = ctypes.c_ulong()
                u.GetWindowThreadProcessId(ctypes.c_void_p(h), ctypes.byref(pid))
                if pid.value == os.getpid() and u.IsWindowVisible(ctypes.c_void_p(h)):
                    hwnds.append(h)
                return True
            u.EnumWindows(cb, 0)
            for size, which in ((32, 1), (16, 0)):      # ICON_BIG、ICON_SMALL
                hicon = u.LoadImageW(None, ICON, 1, size, size, 0x10)   # IMAGE_ICON, LR_LOADFROMFILE
                for h in hwnds:
                    u.SendMessageTimeoutW(ctypes.c_void_p(h), 0x0080, which, ctypes.c_void_p(hicon), 0x0002, 2000, None)
        except Exception as e:
            log(f"设置图标失败：{e}")

    def on_shown():
        threading.Timer(0.3, set_icon).start()
        threading.Timer(0.4, frame.setup).start()

    win.events.closing += on_closing
    win.events.shown += on_shown

    def listen():
        while True:
            try:
                conn, _ = srv.accept()
                conn.close()
                show()
            except OSError:
                return
    threading.Thread(target=listen, daemon=True).start()

    def watchdog():
        """窗口开着时每 30 秒看一次网页服务。连续两次没响应就运行开机启动脚本把它拉起来
        （正常重启只停几秒，碰不上连续两次）。以前服务被外部结束后一直停着，页面上的开关看起来正常，其实什么都存不进去"""
        miss = 0
        while not state["quit"]:
            time.sleep(30)
            if hub_up():
                miss = 0
                continue
            miss += 1
            if miss >= 2:
                log("网页服务连续一分钟没有响应，重新启动")
                ensure_hub()
                miss = 0
    threading.Thread(target=watchdog, daemon=True).start()

    import pystray
    from PIL import Image

    # 桌面桌宠：设置里“桌宠”选了“桌面上”才放出来；单击她弹出的聊天小窗平时藏着
    bridge = PetBridge(show, win.load_url, frame._h, win)
    frame._bridge = bridge
    pet = bridge.pet
    if ok:
        bridge.chat = webview.create_window(CHAT_TITLE, URL + "?petchat=1", width=360, height=480, frameless=True, easy_drag=False,
                                            on_top=True, hidden=True, js_api=PetChatApi(bridge), background_color="#ffffff",
                                            text_select=True)

        def chat_closing():
            if state["quit"]:
                return True
            threading.Timer(0.05, bridge.close_chat).start()     # Alt+F4 也只是收起来
            return False
        bridge.chat.events.closing += chat_closing
    if desktop_pet.enabled():
        pet.start()

    def toggle_pet():
        """托盘“桌宠在桌面上”：改设置里的“桌宠”（主窗口的页面去存），页面不在（比如登录页）就只临时开关。"""
        target = "web" if pet.visible else "desktop"
        try:
            done = win.evaluate_js(f"window.irisPetMode ? (irisPetMode('{target}'), 1) : 0")
        except Exception:
            done = 0
        if not done:
            pet.set_visible(target == "desktop")

    def quit_all(icon, item):
        state["quit"] = True
        pet.stop()
        icon.stop()
        win.destroy()

    tray = pystray.Icon(APP_NAME, Image.open(ICON), APP_NAME,
                        pystray.Menu(pystray.MenuItem("打开", lambda i, it: show(), default=True),
                                     pystray.MenuItem("刷新", lambda i, it: (show(), win.evaluate_js("location.reload()"))),
                                     pystray.MenuItem("桌宠在桌面上", lambda i, it: threading.Thread(target=toggle_pet, daemon=True).start(),
                                                      checked=lambda it: pet.visible),
                                     pystray.MenuItem("退出", quit_all)))
    tray.run_detached()
    os.makedirs(STORAGE, exist_ok=True)
    webview.start(gui="edgechromium", private_mode=False, storage_path=STORAGE)
    try:
        tray.stop()
    except Exception:
        pass
    srv.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"出错：{e!r}")
        raise

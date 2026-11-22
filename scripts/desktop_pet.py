"""桌面桌宠：IrisAI 的看板娘跳出网页，站在 Windows 任务栏上。

为什么不用网页做：WebView2 的窗口做不出透明（pywebview 的 transparent 露出灰底；颜色键分层窗口抠不掉 WebView2 画的东西，
还会整窗点击穿透，2026-11-09 实测）。所以这里用 Windows 的逐像素透明窗口（UpdateLayeredWindow）自己画：
帧图片和说话气泡用 PIL 拼好整张贴上去，透明的地方鼠标自动穿透，点到她身上才算点她。

素材和台词与网页桌宠同一套：主题（data/settings.json 的 look.skin）的 素材/桌宠.json 帧清单，
主题 skin.json 的 persona 对应的 knowledge/人格/<名字>/桌宠台词.json；缺的台词用下面 BASE 里的通用台词（和 static/js/pet.js 一致）。

会做的事（和网页桌宠 static/js/pet.js 对齐）：站在任务栏上；拖她、松手掉下来（摔得高会晕）；单击有反应（连戳会鼓腮）
并弹出聊天小窗（desktop_app.py 开的 petchat 页面）；双击打开 IrisAI；右键菜单；眨眼、随机小动作、闲时沿任务栏溜达
（往右走时左右翻转）；电脑闲置 5 分钟睡着；鼠标停到她身上偶尔眨眼。
跟着 Agent 干活：网页（工作台、入口页、聊天小窗）把动静通过 desktop_app.py 转进来（feed）：思考时换思考脸、
说话时动嘴、调工具时按工具换表情说一句、模型标的表情、等确认时期待、答完说回答第一句（出错说没做完）。

单独运行（测试用）：hub\\.venv\\Scripts\\python.exe scripts\\desktop_pet.py
桌面程序里由 desktop_app.py 启动，托盘菜单可以开关。
"""
import ctypes
import ctypes.wintypes as wt
import json
import math
import os
import random
import re
import threading
import time
from collections import deque

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
try:                                                       # 按部件拼帧（用户在入口页改过的表情），和网页服务用同一份代码
    import sys as _sys
    _sys.path.insert(0, os.path.join(ROOT, "hub"))
    from app import petparts
except Exception as _e:
    petparts = None
    print("没有拼帧模块，改过的表情显示原样：", repr(_e))
try:                                                       # 主题文件夹（不在代码目录里），和网页服务同一个规则
    from app.themes import themes_dir
    THEMES = themes_dir(ROOT)
except Exception as _e:
    THEMES = os.path.join(ROOT, "themes")
    print("没有找主题的模块，用默认的 themes 文件夹：", repr(_e))
STATE = os.path.join(ROOT, "data", "desktop", "pet.json")     # 站在哪（开不开看设置里的“桌宠”，见 enabled）
FONT = r"C:\Windows\Fonts\msyh.ttc"
HEART_FONT = r"C:\Windows\Fonts\seguisym.ttf"       # 戳她冒的 ♥
# 爱心的颜色：主题的 --deco（themes/<主题>/skin.css），其余主题用强调色
DECO = {"satori": (200, 32, 63), "koishi": (170, 120, 195)}

# 尺寸都是逻辑像素，按屏幕缩放乘上去
PET = 180          # 她的高度（帧是正方形）
CANVAS_W = 270     # 窗口比她宽，放得下头上的气泡
ABOVE = 92         # 她头顶上方给气泡留的高度
GRAVITY, WALK = 2600, 95

BASE = {
    "greet": {"late": ["还没睡呀？"], "morning": ["早上好～"], "noon": ["中午好～"], "afternoon": ["下午好～"], "evening": ["晚上好～"]},
    "poke": ["嗯？", "在呢～", "要问点什么吗？", "嘿嘿", "♡"], "pout": ["别戳啦～", "哼"], "wake": ["唔……醒了醒了"],
    "held": ["要带我去哪？"], "dizzy": ["晕～"], "home": ["回来啦"], "idle": ["嗨～", "♪～", "嘿嘿", "啊，想到了！"],
    "sleepy": ["好困……"], "stroll": ["♪～"], "hover": ["嗯？"], "fail": ["唔……这次没做完"],
    "hop": ["嘿咻～", "上去看看～"],
}
ACTS = ["wave", "hum", "wink", "idea", "shy", "cheer", "hope"]
POKE = ["surprise", "wink", "shy", "cheer", "wave", "hope"]
BASE.update({
    "tool": {"search": ["我找找……", "查一下～", "翻翻看"], "write": ["记下来～", "动笔了", "写好这个"], "run": ["跑一下看看", "会成功吗……"],
             "draw": ["画画中♪", "等我画完～"], "translate": ["翻译中～", "认真读着呢"], "delete": ["要删东西了"], "error": ["唔，出错了……"]},
    "wait": {"ask": ["问你个事～"], "confirm": ["这个要你点一下"]},
})
# 调工具时的表情和台词分类（和 pet.js 的 TOOL_ACT 一样）
TOOL_ACT = [
    (re.compile(r"^(web_search|fetch_url|search_files|read_file|list_dir|recall_memory)$"), "think", "search"),
    (re.compile(r"^(write_file|edit_file|make_dir|move_path|remember)$"), "idea", "write"),
    (re.compile(r"^(run_python|run_js|check_file|view_result)$"), "hope", "run"),
    (re.compile(r"^generate_image$"), "hum", "draw"),
    (re.compile(r"^(translate|ocr_image)$"), "hum", "translate"),
    (re.compile(r"^delete_path$"), "surprise", "delete"),
]


def gist(t):
    """回答的第一句：去掉代码块、标题、链接、列表等记号，超过 34 字截断（和 pet.js 的 gist 一样）。"""
    t = re.sub(r"```[\s\S]*?```", " ", t or "")
    t = re.sub(r"^\s*#{1,6}\s.*$", "", t, flags=re.M)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"^[\s#>*+|-]+|^\d+\.\s*", "", t, flags=re.M)
    t = re.sub(r"[*_~]", "", t).strip()
    m = re.match(r"[\s\S]*?[。！？!?\n]", t)
    s = (m.group(0) if m else t).strip()
    s = s[:-1] if s.endswith("。") else s
    return s[:33] + "…" if len(s) > 34 else s
STOP = ["hum", "wink", "idea", "shy", "wave"]

# ---------------------------------------------------------------- Win32
u32 = ctypes.WinDLL("user32", use_last_error=True)
g32 = ctypes.WinDLL("gdi32")
k32 = ctypes.WinDLL("kernel32")
LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON), ("hCursor", wt.HANDLE),
                ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR), ("hIconSm", wt.HICON)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte), ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG), ("biPlanes", wt.WORD), ("biBitCount", wt.WORD),
                ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG),
                ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]


class TRACKMOUSEEVENT(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("dwFlags", wt.DWORD), ("hwndTrack", wt.HWND), ("dwHoverTime", wt.DWORD)]


u32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
u32.DefWindowProcW.restype = LRESULT
u32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
u32.CreateWindowExW.restype = wt.HWND
u32.GetCapture.restype = wt.HWND
u32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
u32.UpdateLayeredWindow.argtypes = [wt.HWND, wt.HDC, ctypes.POINTER(wt.POINT), ctypes.POINTER(wt.SIZE), wt.HDC,
                                    ctypes.POINTER(wt.POINT), wt.COLORREF, ctypes.POINTER(BLENDFUNCTION), wt.DWORD]
u32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT]
u32.GetDC.restype = wt.HDC
u32.MonitorFromPoint.argtypes = [wt.POINT, wt.DWORD]
u32.MonitorFromPoint.restype = wt.HMONITOR
u32.GetMonitorInfoW.argtypes = [wt.HMONITOR, ctypes.POINTER(MONITORINFO)]
u32.SetTimer.argtypes = [wt.HWND, ctypes.c_size_t, wt.UINT, wt.LPVOID]
u32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
u32.TrackPopupMenu.argtypes = [wt.HMENU, wt.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HWND, wt.LPVOID]
u32.AppendMenuW.argtypes = [wt.HMENU, wt.UINT, ctypes.c_size_t, wt.LPCWSTR]
u32.CreatePopupMenu.restype = wt.HMENU
u32.LoadCursorW.argtypes = [wt.HINSTANCE, wt.LPVOID]
u32.LoadCursorW.restype = wt.HANDLE
g32.CreateCompatibleDC.argtypes = [wt.HDC]
g32.CreateCompatibleDC.restype = wt.HDC
g32.CreateDIBSection.argtypes = [wt.HDC, ctypes.c_void_p, wt.UINT, ctypes.POINTER(ctypes.c_void_p), wt.HANDLE, wt.DWORD]
g32.CreateDIBSection.restype = wt.HBITMAP
g32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
g32.SelectObject.restype = wt.HGDIOBJ
g32.DeleteObject.argtypes = [wt.HGDIOBJ]
g32.DeleteDC.argtypes = [wt.HDC]
u32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
u32.PeekMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT, wt.UINT]
u32.MsgWaitForMultipleObjects.argtypes = [wt.DWORD, ctypes.c_void_p, wt.BOOL, wt.DWORD, wt.DWORD]
k32.GetModuleHandleW.restype = wt.HMODULE
dwm = ctypes.WinDLL("dwmapi")
dwm.DwmFlush.restype = ctypes.c_long

WM_DESTROY, WM_TIMER, WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK = 0x0002, 0x0113, 0x0200, 0x0201, 0x0202, 0x0203
WM_RBUTTONUP, WM_MOUSELEAVE, WM_CLOSE, WM_APP, WM_CAPTURECHANGED = 0x0205, 0x02A3, 0x0010, 0x8000, 0x0215
WS_POPUP = 0x80000000
WS_EX_LAYERED, WS_EX_TOPMOST, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x80000, 0x8, 0x80, 0x08000000
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE = 0x1, 0x2, 0x4, 0x10
WM_QUIT, QS_ALLINPUT = 0x0012, 0x04FF
HOP = [(0, 0), (.35, 16), (.6, 0), (.78, 5), (1, 0)]
PHASES = 4         # 上下位置细到 1/4 像素（窗口只能整像素挪，余下的在图里错开），飘起来不一格一格跳
HWND_TOPMOST = wt.HWND(-1)


def _load_json(p):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _pet_faces(skin):
    """设置里这个主题的桌宠表情搭配（pet_faces[主题]：情况 -> 帧名），和网页桌宠 static/js/pet.js 的 PET_SITS 同一套情况。"""
    d = ((_load_json(os.path.join(ROOT, "data", "settings.json")) or {}).get("pet_faces") or {}).get(skin or "default")
    return d if isinstance(d, dict) else {}


def _theme(skin=None):
    """主题（不给就读设置）的帧清单和台词：返回 (帧名 -> 文件路径, 台词字典, 主题名)；主题没有桌宠帧时帧为 None。"""
    look = (_load_json(os.path.join(ROOT, "data", "settings.json")) or {}).get("look") or {}
    skin = skin or look.get("skin") or "default"
    sdir = os.path.join(THEMES, skin)
    m = _load_json(os.path.join(sdir, "素材", "桌宠.json"))
    frames = {k: os.path.join(sdir, "素材", v) for k, v in (m or {}).get("frames", {}).items()} or None
    persona = (_load_json(os.path.join(sdir, "skin.json")) or {}).get("persona")
    lines = _load_json(os.path.join(ROOT, "knowledge", "人格", persona, "桌宠台词.json")) if persona else None
    return frames, lines or {}, skin


class DesktopPet:
    def __init__(self, on_open=None, on_click=None, on_move=None, surfaces=None, on_pos=None):
        self.on_open = on_open or (lambda: None)       # 双击：打开 IrisAI
        self.on_click = on_click                       # 单击：弹出聊天小窗，传她的位置 (x, y, 宽, 高)
        self.on_move = on_move                         # 聊天小窗开着时她挪了位置
        # IrisAI 窗口里能站的地方（对话页的输入框），屏幕坐标 [{id, left, right, top}]，只给露在外面的（desktop_app.py 算）
        self.surfaces = surfaces or (lambda: [])
        self.on_pos = on_pos                           # 她站的位置变了（不算上下浮动）：入口页的对话框按它让位
        self.chat_open = False                         # 聊天小窗开着：不闲逛，答完不在头上重复说
        self.events = deque()                          # 网页转来的 Agent 动静，窗口线程每帧取
        self.hwnd = None
        self.thread = None
        self.visible = False
        self._want_visible = True
        self.pf = {}                                   # 表情搭配（_pet_faces）
        self.want_skin = None                          # 网页刚报来的主题：设置文件要过一会儿才写好，先按这个换

    # ---------------------------------------------------------------- 给外面调用（任意线程）
    def start(self, visible=True):
        if self.thread:
            return
        self._want_visible = visible
        self.thread = threading.Thread(target=self._run, daemon=True, name="desktop-pet")
        self.thread.start()

    def set_visible(self, on):
        on = bool(on)
        if on == self._want_visible and (self.hwnd or not on):
            return                                  # 页面每次加载都会报一次模式，没变就不重复（不然每次都打招呼）
        self._want_visible = on
        if self.hwnd:
            u32.PostMessageW(self.hwnd, WM_APP + 1, 1 if on else 0, 0)
        elif on:
            self.start(True)

    def stop(self):
        if self.hwnd:
            u32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)

    def feed(self, ev):
        """网页转来的一条动静（dict：type = mood / talk / round / final / step / wait / emote，src = 哪个页面）。"""
        if isinstance(ev, dict):
            self.events.append(ev)

    def reload(self, skin=None):
        """换了主题：重新读帧和台词。"""
        self.want_skin = skin or self.want_skin
        if self.hwnd:
            u32.PostMessageW(self.hwnd, WM_APP + 3, 0, 0)

    def rect(self):
        return int(self.x), int(self.y), self.S, self.S

    # ---------------------------------------------------------------- 窗口线程
    def _run(self):
        try:
            u32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))      # 每个显示器各自的缩放，图不糊
        except Exception:
            pass
        frames, lines, self.skin = _theme(self.want_skin)
        self.pf = _pet_faces(self.skin)
        if not frames:
            print("当前主题没有桌宠帧，桌面桌宠不显示")
            self.thread = None
            return
        self.frame_paths, self.lines = frames, lines
        self._custom(True)
        self._colors()
        self._proc = WNDPROC(self._wndproc)       # 留住引用，不然会被回收
        hinst = k32.GetModuleHandleW(None)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.style = 0x0008                         # CS_DBLCLKS：要双击消息
        wc.lpfnWndProc = self._proc
        wc.hInstance = hinst
        wc.hCursor = u32.LoadCursorW(None, ctypes.c_void_p(32649))   # 手形
        wc.lpszClassName = "IrisAIDesktopPet"
        u32.RegisterClassExW(ctypes.byref(wc))
        self.hwnd = u32.CreateWindowExW(WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
                                        "IrisAIDesktopPet", "IrisAI 桌宠", WS_POPUP, 0, 0, 10, 10, None, None, hinst, None)
        self.scale = (u32.GetDpiForWindow(self.hwnd) or 96) / 96
        self._init_state()
        self._render(force=True)
        self._place(time.monotonic())
        if self._want_visible:
            self._show(True)
        # 每帧跟着屏幕刷新走（DwmFlush 等到下一次合成）：以前用 16ms 的 SetTimer，Windows 计时精度 15.6ms，
        # 实际 1–48ms 不等、平均 40 帧，动起来一顿一顿（2026-11-09 实测）。藏起来时 0.1 秒一次，只处理动静
        msg = wt.MSG()
        while True:
            quit = False
            while u32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):        # PM_REMOVE
                if msg.message == WM_QUIT:
                    quit = True
                    break
                u32.TranslateMessage(ctypes.byref(msg))
                u32.DispatchMessageW(ctypes.byref(msg))
            if quit:
                break
            if self.visible:
                t0 = time.perf_counter()
                if dwm.DwmFlush() != 0 or time.perf_counter() - t0 < 0.002:  # 没等到合成（远程桌面等）：自己等一帧
                    u32.MsgWaitForMultipleObjects(0, None, False, 15, QS_ALLINPUT)
            else:
                u32.MsgWaitForMultipleObjects(0, None, False, 100, QS_ALLINPUT)
            try:
                self._tick()
            except Exception as e:
                print("桌宠出错：", repr(e))
        self._free_dibs()
        self.hwnd, self.thread = None, None

    def _show(self, on):
        u32.ShowWindow(self.hwnd, 4 if on else 0)     # SW_SHOWNOACTIVATE / SW_HIDE
        self.visible = on
        if on:
            self._greet()

    # ---------------------------------------------------------------- 状态
    def _init_state(self):
        s = self.scale
        self.S, self.CW, self.CH = int(PET * s), int(CANVAS_W * s), int((PET + ABOVE) * s)
        self.sx, self.sy = (self.CW - self.S) // 2, self.CH - self.S          # 她在窗口里的位置
        self.cache, self.font = {}, ImageFont.truetype(FONT, int(13 * s))
        work = self._work_at(0, 0, primary=True)
        saved = _load_state().get("x")
        self.x = work.left + saved * (work.right - work.left) if isinstance(saved, (int, float)) else work.right - self.S - int(160 * s)
        self.y = work.bottom - self.S
        self.y = self._floor()
        now = time.monotonic()
        self.vy, self.falling, self.bounced, self.drop = 0.0, False, 0, 0
        self.walk = None              # (目标 x, 走完后做什么)
        self.flip, self.hop_t = False, -9
        self.temp, self.temp_until, self.hold = "", 0, ""
        self.blink, self.next_blink = [], now + 3
        self.next_act, self.next_stroll = now + 25 + random.random() * 20, now + 14 + random.random() * 24
        self.text, self.text_until, self.pokes, self.hover, self.hover_cool = "", 0, [], False, 0
        self.drag, self.click_at = None, 0
        self.sleeping, self.zz, self.unflip_at = False, "sleep1", 0
        self.drawn, self.pos, self.shown, self.cur, self.store, self.top_at, self.gen = None, None, None, None, {}, 0, 0
        self.plats, self.plat_at, self.on, self.jump, self.last_pos, self.air = [], 0, None, None, None, {}
        self.squash_t, self.tilt, self.hearts, self.held_t, self.bases = -9, 0.0, [], 0, {}     # 落地压扁、走路前倾、爱心、开始被拎着的时间   # on：站在哪个台面上 (id, 当时的左边)
        self.last = now
        self.moods, self.seen, self.bufs = {}, {}, {}     # 每个页面各自的心情、最后一次动静、回答的字
        self.talk_until, self.said_at, self.pending = 0, 0, ""

    def _work_at(self, x, y, primary=False):
        pt = wt.POINT(int(x), int(y))
        mon = u32.MonitorFromPoint(pt, 1 if primary else 2)     # 1 主显示器 / 2 最近的显示器
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        u32.GetMonitorInfoW(mon, ctypes.byref(mi))
        return mi.rcWork

    def _work(self):
        return self._work_at(self.x + self.S / 2, self.y + self.S / 2)

    def _floor(self):
        return self._work().bottom - self.S + int(5 * self.scale)     # 脚踩在任务栏上沿

    def line(self, path):
        def dig(d):
            for k in path.split("."):
                d = d.get(k) if isinstance(d, dict) else None
            return d
        a = dig(self.lines)
        a = a if isinstance(a, list) and a else dig(BASE) or [""]
        return random.choice(a)

    def say(self, text, sec=2.2):
        self.text, self.text_until, self.said_at = text, time.monotonic() + sec, time.monotonic()

    def show(self, name, sec=1.8):
        self.temp, self.temp_until = name, time.monotonic() + sec

    def hop(self):
        self.hop_t = time.monotonic()

    def _greet(self):
        h = time.localtime().tm_hour
        k = "late" if h < 5 else "morning" if h < 11 else "noon" if h < 13 else "afternoon" if h < 18 else "evening"
        self.show(self.F("greet", "wave"), 2.4)
        self.say(self.line("greet." + k), 2.6)
        self.hop()

    def mood(self, now):
        """现在的心情：哪个页面在跑就是思考（10 分钟没动静的不算，页面关了不会一直思考）；否则最近一次完成 / 出错，过 6 / 8 秒消失。"""
        live = [(m, t) for src, (m, t) in self.moods.items() if m and now - self.seen.get(src, 0) < 600]
        if any(m == "think" for m, _ in live):
            return "think"
        live = [(m, t) for m, t in live if now - t < (6 if m == "done" else 8)]
        return max(live, key=lambda x: x[1])[0] if live else ""

    def _colors(self):
        """强调色（设置里的 look.accent，光斑和鼠标停上去的光用）和爱心的颜色（主题的 --deco）。"""
        look = (_load_json(os.path.join(ROOT, "data", "settings.json")) or {}).get("look") or {}
        h = str(look.get("accent") or "#5b8def").lstrip("#")
        try:
            acc = tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
        except ValueError:
            acc = (91, 141, 239)
        self.accent, self.deco = acc, DECO.get(self.skin, acc)

    def _custom(self, force=False):
        """设置里改过的帧（pet_frames[主题]）：要拼的帧在 frame_paths 里记成 custom:<名字>。配方变了就清掉画好的图。"""
        base = {k: v for k, v in self.frame_paths.items() if not v.startswith("custom:")}
        sd = os.path.join(THEMES, self.skin or "default")
        man = petparts.manifest(sd) if petparts else None
        custom = ((_load_json(os.path.join(ROOT, "data", "settings.json")) or {}).get("pet_frames") or {}).get(self.skin) or {}
        if not force and man is getattr(self, "pman", None) and custom == getattr(self, "pcustom", None):
            return
        self.pman, self.pcustom = man, custom if man else {}
        if man:
            for n in petparts.changed(man, custom):
                base[n] = "custom:" + n
        self.frame_paths = base
        if not force and self.hwnd:
            self.cache.clear()
            self.drawn = None
            self._free_dibs()

    def F(self, k, d):
        """这种情况用哪一帧：设置里改过（入口页“调整布局 → 表情… → 桌宠”）就用改的，不然用默认的 d。"""
        v = self.pf.get(k)
        return v if v and v in self.frame_paths else d

    def want(self, now):
        if self.hold:
            return self.hold
        if self.temp and now < self.temp_until:
            return self.temp
        m = self.mood(now)
        if m in ("think", "done", "error"):
            return self.F(m, m)
        if self.sleeping:
            return self.zz
        return "idle"

    def talking(self, now):
        return now < self.talk_until and self.mood(now) not in ("done", "error")

    # ---------------------------------------------------------------- 跟着 Agent 干活
    def _event(self, ev, now):
        t, src = ev.get("type"), str(ev.get("src") or "")
        self.seen[src] = now
        if t == "mood":
            m = ev.get("mood") or ""
            m = "" if m == "idle" else m          # 网页的“闲着”是网页自己的睡觉；桌面这边看电脑有没有人在用
            was = self.moods.get(src, ("", 0))[0]
            self.moods[src] = (m, now)
            if m == "think" and was != "think":
                self.bufs[src] = ["", False]
            if m in ("done", "error") and was == "think":
                self._finished(m, src)
        elif t == "talk":
            self.talk_until = now + 0.45
            b = self.bufs.setdefault(src, ["", False])
            if b[1]:                                # 新一轮开口：只留最后一轮说的话
                b[0], b[1] = "", False
            b[0] += ev.get("text") or ""
        elif t == "round":
            self.bufs.setdefault(src, ["", False])[1] = True
        elif t == "final":
            self.bufs[src] = [ev.get("text") or "", False]
        elif t == "step":
            quiet = now - self.said_at < 5         # 台词 5 秒内最多一句
            if ev.get("status") == "error":
                self.show(self.F("tool.error", "error"), 1.6)
                if not quiet:
                    self.say(self.line("tool.error"), 1.6)
            elif ev.get("status") == "running":
                hit = next((h for h in TOOL_ACT if h[0].match(ev.get("tool") or "")), None)
                if hit:
                    self.show(self.F("tool." + hit[2], hit[1]), 1.6)
                    if not quiet and random.random() < .6:
                        self.say(self.line("tool." + hit[2]), 1.7)
        elif t == "wait":
            self.show(self.F("wait", "hope"), 2.2)
            self.say(self.line("wait.ask" if ev.get("kind") == "ask" else "wait.confirm"), 2.4)
        elif t == "emote":
            f = ev.get("face")
            if f in self.frame_paths:
                self.show(self.F("emote." + f, f), 2.6)
        elif t == "preview":                      # 入口页“调整布局 → 表情… → 桌宠”里选帧时先在她身上看看
            f = ev.get("face")
            self._custom()                         # 刚改了这一帧的部件：网页存完设置才发预览，这里马上重读
            if f in self.frame_paths:
                self.show(f, 2.4)

    def _finished(self, mood, src):
        """答完：完成说第一句，出错说没做完。聊天小窗开着就不说（答案在小窗里）；人不在电脑前就等回来再说。"""
        if self.chat_open:
            return
        text = gist(self.bufs.get(src, [""])[0]) if mood == "done" else self.line("fail")
        if not text:
            return
        if self.sleeping:
            self.pending = text
        else:
            self.say(text, min(6.5, 2.5 + len(text) * 0.11) if mood == "done" else 3)

    # ---------------------------------------------------------------- 每帧
    def _tick(self):
        now = time.monotonic()
        dt, self.last = min(now - self.last, 0.05), now
        s = self.scale
        if now - getattr(self, "_skin_check", 0) > 2:     # 在普通浏览器里换主题时桌面程序收不到通知：看设置文件改没改
            self._skin_check = now
            try:
                mt = os.path.getmtime(os.path.join(ROOT, "data", "settings.json"))
            except OSError:
                mt = None
            if mt != getattr(self, "_skin_mtime", mt):
                look = (_load_json(os.path.join(ROOT, "data", "settings.json")) or {}).get("look") or {}
                if (look.get("skin") or "default") != self.skin:
                    self.reload(look.get("skin") or "default")
                self.pf = _pet_faces(self.skin)          # 表情搭配改了
                self._custom()                           # 部件配方改了
                self._colors()                           # 强调色改了
            self._skin_mtime = mt
        while self.events:
            try:
                self._event(self.events.popleft(), now)
            except Exception as e:
                print("桌宠处理动静出错：", repr(e))
        # 电脑闲置 5 分钟就睡着，一动鼠标键盘就醒（打个哈欠）
        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
        u32.GetLastInputInfo(ctypes.byref(lii))
        idle_sec = (k32.GetTickCount() - lii.dwTime) / 1000
        if idle_sec > 300 and not self.sleeping and not self.drag:
            self.sleeping = True
        elif idle_sec < 2 and self.sleeping:
            self.sleeping = False
            self.show(self.F("yawn", "yawn"), 2.6)
            if self.pending:                       # 人不在时答完的那句，回来再说
                self.say(self.pending, 5)
                self.pending = ""
        self.zz = "sleep1" if int(now / 1.8) % 2 == 0 else "sleep2"      # ZZZ 和鼻涕泡交替
        # 单击要等一下，确认不是双击
        if self.click_at and now >= self.click_at:
            self.click_at = 0
            if self.on_click:
                self.on_click(self.rect())         # 弹出聊天小窗
            self._poke()
        # IrisAI 窗口里能站的地方：0.1 秒问一次
        if now >= self.plat_at:
            self.plat_at = now + 0.1
            try:
                self.plats = self.surfaces() or []
            except Exception as e:
                self.plats = []
                print("桌宠读台面出错：", repr(e))
        # 跳：抛物线跳到目标（台面或地面），目标跟着窗口走
        if self.jump and not self.drag:
            j = self.jump
            k = min((now - j["t0"]) / j["dur"], 1)
            p = self._plat(j["to"]) if j["to"] else None
            if j["to"] and not p:                  # 跳的时候窗口没了：改成掉下去
                self.jump = None
                self._fall()
            else:
                y1 = self._stand(p) if p else self._floor()
                x1 = j["x1"] + (p["left"] - j["left0"] if p else 0)     # 跳的时候窗口挪了：落点跟着挪
                self.x = j["x0"] + (x1 - j["x0"]) * k
                self.y = j["y0"] + (y1 - j["y0"]) * k - j["h"] * 4 * k * (1 - k)
                if k >= 1:
                    self.jump, self.y = None, y1
                    self.on = (p["id"], p["left"]) if p else None
                    self.squash_t = now
                    self._landed()
                    self._remember()
                    j["then"] and j["then"]()
        # 站在台面上：窗口挪了跟着挪；台面没了（窗口关了、被挡住）或走出了边就掉下去
        elif self.on and not (self.drag or self.falling):
            p = self._plat(self.on[0])
            cx = self.x + self.S / 2
            if p and p["left"] + 12 * s <= cx + (p["left"] - self.on[1]) <= p["right"] - 12 * s:
                dx = p["left"] - self.on[1]
                self.x += dx
                if dx and self.walk:
                    self.walk = (self.walk[0] + dx, self.walk[1])
                self.y, self.on = self._stand(p), (p["id"], p["left"])
            else:
                self.on = None
                self._fall()
        elif not (self.drag or self.falling) and self.y < self._floor() - 3 * s:
            self._fall()                           # 悬在半空（脚下的窗口没了）
        # 掉落：落在脚下最近的台面上，没有就是任务栏
        if self.falling:
            floor = self._land_y(self.y)
            self.vy += GRAVITY * s * dt
            self.y += self.vy * dt
            if self.y >= floor:
                self.y = floor
                self.squash_t = now                    # 落地压扁回弹（每弹一下都有）
                if self.vy > 520 * s and self.bounced < 2:
                    self.vy, self.bounced = -self.vy * 0.28, self.bounced + 1
                else:
                    self.falling, self.hold = False, ""
                    p = self._plat_at(self.y)
                    self.on = (p["id"], p["left"]) if p else None
                    self._landed()
                    self._remember()
                    if self.drop > 320 * s:
                        self.show(self.F("dizzy", "dizzy"), 1.9)
                        self.say(self.line("dizzy"), 1.6)
        # 走路
        if self.walk and not self.drag and not self.falling and not self.jump:
            tx, then = self.walk
            step = WALK * s * dt
            if abs(tx - self.x) <= step:
                self.x, self.walk = tx, None
                self._remember()
                then and then()
            else:
                self.flip = tx > self.x                 # 脸朝走的方向（目标被挪过、中途换了方向也不会倒着走）
                self.x += step if tx > self.x else -step
        want_tilt = (2.5 if self.flip else -2.5) if self.walk and not self.drag else 0.0
        self.tilt += max(-12.5 * dt, min(12.5 * dt, want_tilt - self.tilt))
        if self.unflip_at and now >= self.unflip_at and not self.walk:
            self.flip, self.unflip_at = False, 0
        # 眨眼：普通表情时每 2.5–6.5 秒一次
        if self.blink and now >= self.blink[0][1]:
            self.blink.pop(0)
        if not self.blink and now >= self.next_blink:
            self.next_blink = now + 2.5 + random.random() * 4
            if self.want(now) == "idle":
                t = now
                for f, ms in (("blink1", .055), ("blink2", .085), ("blink1", .055)):
                    t += ms
                    self.blink.append((f, t))
        free = (not (self.drag or self.falling or self.walk or self.jump or self.sleeping or self.hover or self.chat_open)
                and self.want(now) == "idle" and not self.talking(now))
        # 随机小动作
        if now >= self.next_act:
            self.next_act = now + 20 + random.random() * 25
            if free:
                late = time.localtime().tm_hour < 6 and random.random() < .5
                f = self.F("yawn", "yawn") if late else (self.F("act", "") or random.choice(ACTS))
                self.show(f, 2.4)
                if random.random() < .45:
                    self.say(self.line("sleepy" if late else "idle"), 2.0)
                if f in ("wave", "cheer"):
                    self.hop()
        # 溜达：在她站的那一层走（任务栏，或 IrisAI 的输入框上）；有输入框时偶尔跳上去，站在上面时偶尔跳下来
        if now >= self.next_stroll:
            self.next_stroll = now + 14 + random.random() * 24
            if free:
                p = self._plat(self.on[0]) if self.on else None
                near = [q for q in self.plats if abs(q["left"] + (q["right"] - q["left"]) / 2 - self.x) < 900 * s]
                if not p and near and random.random() < .35:
                    self.jump_to(random.choice(near))
                elif p and random.random() < .2:
                    self.jump_to(None)
                else:
                    w = self._work()
                    lo, hi = (p["left"] + 12 * s - self.S / 2, p["right"] - 12 * s - self.S / 2) if p else (w.left, w.right - self.S)
                    d = (90 + random.random() * 260) * s * random.choice((-1, 1))
                    tx = self.x + d
                    if tx < lo or tx > hi:
                        tx = self.x - d
                    tx = min(max(tx, lo), hi)
                    if abs(tx - self.x) > 40 * s:
                        self.flip = tx > self.x
                        self.walk = (tx, self._stroll_end)
        if (round(self.x), round(self.y)) != self.last_pos:
            self.last_pos = (round(self.x), round(self.y))
            if self.on_pos:
                self.on_pos(self.rect())
            if self.chat_open and self.on_move:
                self.on_move(self.rect())          # 聊天小窗跟着她站的位置（浮动不算）
        self._render()
        self._place(now)

    # ---------------------------------------------------------------- 台面（IrisAI 的输入框）
    def _stand(self, p):
        return p["top"] - self.S + int(5 * self.scale)       # 脚踩在台面上沿（和任务栏一样）

    def _plat(self, pid):
        return next((p for p in self.plats if p["id"] == pid), None)

    def _plat_at(self, y):
        """她脚下、正好站在这个高度的台面。"""
        cx, s = self.x + self.S / 2, self.scale
        return next((p for p in self.plats if p["left"] + 12 * s <= cx <= p["right"] - 12 * s and abs(self._stand(p) - y) < 3 * s), None)

    def _land_y(self, from_y):
        """从 from_y 往下，她脚下最近的台面（没有就是任务栏）。和网页桌宠 pet.js 的 landAt 一样。"""
        cx, s, y = self.x + self.S / 2, self.scale, self._floor()
        for p in self.plats:
            st = self._stand(p)
            if p["left"] + 12 * s <= cx <= p["right"] - 12 * s and from_y - 2 * s <= st < y:
                y = st
        return y

    def _landed(self):
        """落地（跳完、掉完）：空中的表情和台词（被拎着说的话、起跳时那句）不再留着。"""
        now, a = time.monotonic(), self.air
        if a.get("face") and self.temp == a["face"]:
            self.temp_until = min(self.temp_until, now)
        if a.get("text") and self.text == a["text"]:
            self.text_until = min(self.text_until, now)
        self.air = {}

    def _fall(self):
        self.walk, self.on = None, None
        self.drop, self.vy, self.bounced = self._land_y(self.y) - self.y, 0.0, 0
        self.hold = self.F("fall", "surprise") if self.drop > 40 * self.scale else ""
        self.falling = True

    def jump_to(self, p, x=None, then=None):
        """跳到台面 p 上（落在离她近的那一头），p 为 None 就跳回任务栏（落在 x，不给就落在台面旁边）。"""
        s, S = self.scale, self.S
        if p:
            lo, hi = p["left"] + 30 * s - S / 2, p["right"] - 30 * s - S / 2
            x1 = min(max(self.x, lo), hi) if x is None else min(max(x, lo), hi)
            if abs(x1 - self.x) < 1 and lo < hi:   # 正好在台面下面：往里多跳一点
                x1 = min(max(self.x + random.choice((-1, 1)) * 120 * s, lo), hi)
            y1 = self._stand(p)
        else:
            w = self._work()
            if x is None:
                cur = self._plat(self.on[0]) if self.on else None
                x = (cur["left"] - S * 0.7 if random.random() < .5 else cur["right"] - S * 0.3) if cur else self.x
            x1 = min(max(x, w.left), w.right - S)
            y1 = self._floor()
        self.walk, self.on, self.falling = None, None, False
        self.flip = x1 > self.x + 2
        self.unflip_at = time.monotonic() + 2.2
        rise = self.y - y1
        self.jump = {"t0": time.monotonic(), "x0": self.x, "y0": self.y, "x1": x1, "to": p["id"] if p else None, "left0": p["left"] if p else 0,
                     "dur": 0.55 + min(abs(rise) + abs(x1 - self.x), 900 * s) / (1800 * s),
                     "h": max(rise, 0) * 0.35 + 46 * s, "then": then}
        self.show(self.F("hop", "cheer") if p else self.F("fall", "surprise"), 1.2)
        self.air = {"face": self.temp}
        if p and random.random() < .5:
            self.say(self.line("hop"), 1.4)
            self.air["text"] = self.text

    def _stroll_end(self):
        if random.random() < .3:
            self.show(random.choice(STOP), 2.0)
            if random.random() < .6:
                self.say(self.line("stroll"), 1.6)
        self.unflip_at = time.monotonic() + 1.6 + random.random() * 2

    def _place(self, now):
        s = self.scale
        # 上下浮动：平时 3.4 秒一个来回轻轻飘；走路时离地一点、1.2 秒一个来回；跳一下是 0.5 秒的抛物线
        if self.walk:
            off = -(4 + 2 * math.sin(now * 2 * math.pi / 1.2)) * s
        elif self.drag or self.falling:
            off = 0
        else:
            off = -(2 - 2 * math.cos(now * 2 * math.pi / 3.4)) * s
        k = (now - self.hop_t) / 0.5
        if 0 <= k < 1:                             # 小跳：和网页 p-pet-hop 的关键帧一样，跳 16px、落回、再弹 5px
            for (k0, v0), (k1, v1) in zip(HOP, HOP[1:]):
                if k0 <= k <= k1:
                    e = 1 - (1 - (k - k0) / (k1 - k0)) ** 2
                    off -= (v0 + (v1 - v0) * e) * s
                    break
        # 窗口放在往上取整的整像素处，余下不到 1 像素的部分让图往上错开（PHASES 档）
        yf = self.y - self.sy + off
        iy = math.ceil(yf)
        q = round((iy - yf) * PHASES)
        if q >= PHASES:
            iy, q = iy - 1, 0
        pos = (round(self.x - self.sx), iy)
        if (pos, q, self.cur["gen"]) != self.shown:
            self._present(pos, q)
        if pos != self.pos:
            self.pos = pos
        if now >= self.top_at:                     # 隔一会儿重新站到最上层（别的置顶窗口会盖过来）
            self.top_at = now + 2
            u32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)

    # ---------------------------------------------------------------- 画
    def _sprite(self, name):
        key = (name, self.flip)
        if key not in self.cache:
            p = self.frame_paths.get(name) or self.frame_paths["idle"]
            if p.startswith("custom:"):          # 改过的帧：按配方拼
                sd = os.path.join(THEMES, self.skin)
                im = petparts.compose(sd, petparts.recipe(self.pman, self.pcustom, p[7:]), self.pman)
            else:
                im = Image.open(p)
            im = im.convert("RGBA").resize((self.S, self.S), Image.LANCZOS)
            self.cache[key] = im.transpose(Image.FLIP_LEFT_RIGHT) if self.flip else im
        return self.cache[key]

    # 网页桌宠的动画（static/css/pet.css）在这里照着做：落地压扁回弹（p-pet-land）、拎着左右晃（p-pet-held）、
    # 走路往前倾（.walk img rotate）、戳她冒爱心（p-heart）、身后的光斑（.p-pet::before）、鼠标停上去发光（:hover drop-shadow）、
    # 说话气泡淡入淡出（.p-say transition）。一直在变的（压扁、爱心、气泡淡入淡出）按 60 帧画、画完就扔；拎着晃的几个角度留着重复用
    def _pose(self, now):
        """(旋转角度（CSS 的顺时针为正）, 横向缩放, 纵向缩放, 以脚为原点?)"""
        rot, kx, ky, foot = round(self.tilt * 4) / 4, 1.0, 1.0, False
        if self.drag and self.drag.get("moved"):
            ph = (now - self.held_t) / 0.5               # p-pet-held：.5s ease-in-out alternate，-7° 到 7°，放大 1.05
            i, u = int(ph), ph % 1
            e = (1 - math.cos(math.pi * u)) / 2
            rot, kx, ky = round(-7 + 14 * (1 - e if i % 2 else e)), 1.05, 1.05
        k = (now - self.squash_t) / 0.32                  # p-pet-land：.32s，(1.08, .86) → 55% (.96, 1.05) → 原样，原点在脚下
        if 0 <= k < 1:
            if k < .55:
                e = 1 - (1 - k / .55) ** 2
                qx, qy = 1.08 + (.96 - 1.08) * e, .86 + (1.05 - .86) * e
            else:
                e = 1 - (1 - (k - .55) / .45) ** 2
                qx, qy = .96 + .04 * e, 1.05 - .05 * e
            kx, ky, foot = kx * qx, ky * qy, True
        return rot, round(kx, 3), round(ky, 3), foot

    def _posed(self, im, rot, kx, ky, foot):
        """按姿势缩放、旋转，返回 (图, 相对原位置的偏移)。"""
        S, ox, oy = self.S, 0.0, 0.0
        if kx != 1 or ky != 1:
            w, h = max(1, round(S * kx)), max(1, round(S * ky))
            im = im.resize((w, h), Image.BICUBIC)
            ox, oy = (S - w) / 2, (S - h) if foot else (S - h) / 2
        if rot:
            r = im.rotate(-rot, resample=Image.BICUBIC, expand=True)      # PIL 逆时针为正
            ox, oy = ox + (im.width - r.width) / 2, oy + (im.height - r.height) / 2
            im = r
        return im, round(ox), round(oy)

    def _glow(self):
        """身后的光斑：主题强调色 40%，模糊 26px，透明度 .45，在她身体的位置（左右各缩 22%，上 40% 到下 6%）。"""
        key = ("glow", self.accent)
        if key not in self.cache:
            s, S = self.scale, self.S
            g = Image.new("RGBA", (self.CW, self.CH), (0, 0, 0, 0))
            d = ImageDraw.Draw(g)
            x0, y0 = self.sx + S * .22, self.sy + S * .40
            d.ellipse((x0, y0, self.sx + S * .78, self.sy + S * .94), fill=(*self.accent, round(255 * .4 * .45)))
            self.cache[key] = g.filter(ImageFilter.GaussianBlur(13 * s))
        return self.cache[key]

    def _hearts(self, now):
        """戳她冒出的爱心：1.1 秒往上飘 70px、左右偏一点，从 .6 倍放大到 1.1 倍，慢慢变淡（ease-out）。"""
        self.hearts = [h for h in self.hearts if now < h["t0"] + 1.1]
        return [h for h in self.hearts if now >= h["t0"]]

    def _render(self, force=False):
        now = time.monotonic()
        nq = round(now * 60) / 60                         # 一直在变的东西按 60 帧画
        name = self.want(now)
        if self.talking(now) and not self.hold and not (self.temp and now < self.temp_until):
            name = ("talk1", "idle", "talk2", "idle")[int(now / 0.11) % 4]     # 回答一个个字冒出来时嘴一张一合
        elif self.blink and name == "idle":
            name = self.blink[0][0]
        # 说话气泡：出现时 .2 秒淡入、从下面 6px 升上来，到时间后 .2 秒淡出
        text = self.text if now < self.text_until + 0.2 else ""
        ba = round(min(1, (nq - self.said_at) / 0.2, (self.text_until + 0.2 - nq) / 0.2) * 10) / 10 if text else 0
        if text and ba <= 0:
            text = ""
        pose = self._pose(nq)
        nh = round(now * 30) / 30                         # 爱心小、淡得快，30 帧就够
        hearts = self._hearts(nh)
        moving = bool(hearts) or (text and ba < 1) or pose[3]
        bkey = (name, self.flip, text, self.x // 40 if text else 0, pose, self.hover, ba)
        key = bkey + (nh if hearts else 0,)
        if key == self.drawn and not force:
            return
        self.drawn = key
        if force:
            self._free_dibs()
        e = self.store.pop(key, None)
        if e is None:
            img = self._base(bkey, name, text, pose, ba)
            if hearts:                                  # 爱心叠在留着的底图上，不用整张重画
                img = img.copy()
                for h in hearts:
                    self._draw_heart(img, h, nh)
            self.gen += 1
            e = {"img": img, "base": None, "dibs": {}, "gen": self.gen, "tmp": moving}
        old = self.cur
        if old is not None and old is not e and old.get("tmp"):
            self._free_entry(old)                       # 画完就扔的（压扁、爱心、气泡淡入淡出）
        if not e["tmp"]:
            self.store[key] = e                         # 拼好的图按帧留着：对口型、眨眼、拎着晃的几个角度来回用，不用每次重拼
            while len(self.store) > 24:
                self._free_entry(self.store.pop(next(iter(self.store))))
        self.cur = e                                    # 下一次 _place 时贴上去（和挪位置一起，一次更新）

    def _base(self, bkey, name, text, pose, ba):
        """不带爱心的整张图（光斑、她、鼠标停上去的光、说话气泡），最近几张留着。"""
        img = self.bases.pop(bkey, None)
        if img is not None:
            self.bases[bkey] = img
            return img
        img = Image.new("RGBA", (self.CW, self.CH), (0, 0, 0, 0))
        img.alpha_composite(self._glow())
        im, ox, oy = self._posed(self._sprite(name), *pose)
        layer = Image.new("RGBA", (self.CW, self.CH), (0, 0, 0, 0))
        layer.paste(im, (self.sx + ox, self.sy + oy))
        if self.hover:                              # 鼠标停在她身上：强调色 35% 的光，往下 4px、模糊 10px
            a = layer.getchannel("A").filter(ImageFilter.GaussianBlur(5 * self.scale)).point(lambda v: v * .35)
            sh = Image.new("RGBA", layer.size, (*self.accent, 0))
            sh.putalpha(a)
            img.alpha_composite(sh, (0, round(4 * self.scale)))
        img.alpha_composite(layer)
        if text:
            bub = Image.new("RGBA", (self.CW, self.CH), (0, 0, 0, 0))
            self._bubble(bub, text)
            if ba < 1:
                bub.putalpha(bub.getchannel("A").point(lambda v: v * ba))
                dy = round((1 - ba) * 6 * self.scale)
                b2 = Image.new("RGBA", bub.size, (0, 0, 0, 0))
                b2.paste(bub, (0, dy))
                bub = b2
            img.alpha_composite(bub)
        self.bases[bkey] = img
        while len(self.bases) > 6:
            self.bases.pop(next(iter(self.bases)))
        return img

    def _draw_heart(self, img, h, now):
        s, S = self.scale, self.S
        u = min(max((now - h["t0"]) / 1.1, 0), 1)
        e = 1 - (1 - u) ** 2
        size = h["size"] * (.6 + .5 * e)
        key = ("heart", round(size))
        if key not in self.cache:
            f = ImageFont.truetype(HEART_FONT, max(4, round(size)))
            bb = f.getbbox("♥")
            g = Image.new("RGBA", (bb[2] - bb[0] + 2, bb[3] - bb[1] + 2), (0, 0, 0, 0))
            ImageDraw.Draw(g).text((-bb[0] + 1, -bb[1] + 1), "♥", font=f, fill=(*self.deco, 255))
            self.cache[key] = g
        g = self.cache[key]
        op = .95 * (1 - e)
        if op <= 0.01:
            return
        g2 = g.copy()
        g2.putalpha(g.getchannel("A").point(lambda v: v * op))
        x = round(self.sx + S * h["x"] + h["dx"] * e - g.width / 2)
        y = round(self.sy + S * .30 - 70 * s * e - g.height / 2)
        cx, cy = max(0, -x), max(0, -y)
        w, hh = min(g2.width, img.width - x) - cx, min(g2.height, img.height - y) - cy
        if w > 0 and hh > 0:
            img.alpha_composite(g2, (x + cx, y + cy), (cx, cy, cx + w, cy + hh))

    def _bubble(self, img, text):
        s, d = self.scale, ImageDraw.Draw(img)
        rows, cur = [], ""
        for ch in text:                     # 按宽度折行，最宽约 11 个字
            if d.textlength(cur + ch, font=self.font) > 150 * s:
                rows.append(cur)
                cur = ""
            cur += ch
        rows.append(cur)
        lh = int(19 * s)
        tw = max(d.textlength(r, font=self.font) for r in rows)
        bw, bh = int(tw + 24 * s), int(lh * len(rows) + 14 * s)
        cx = self.CW / 2
        # 靠屏幕边时气泡往里挪，不出屏幕
        w = self._work()
        left_screen = self.x - self.sx + cx - bw / 2
        shift = max(0, w.left + 4 * s - left_screen) - max(0, left_screen + bw - (w.right - 4 * s))
        bx = int(min(max(cx - bw / 2 + shift, 0), self.CW - bw))
        by = int(self.sy + self.S * 0.06 - bh)
        ink, line_w = (17, 17, 17, 255), max(2, int(2.5 * s))
        d.rounded_rectangle((bx, by, bx + bw, by + bh), radius=int(11 * s), fill=(255, 255, 255, 255), outline=ink, width=line_w)
        tx = int(min(max(cx, bx + 18 * s), bx + bw - 18 * s))      # 尖角指向她
        tri = [(tx - 7 * s, by + bh - line_w), (tx + 7 * s, by + bh - line_w), (tx, by + bh + 9 * s)]
        d.polygon(tri, fill=(255, 255, 255, 255))
        d.line([tri[0], tri[2], tri[1]], fill=ink, width=line_w)
        for i, r in enumerate(rows):
            d.text((bx + 12 * s, by + 6 * s + i * lh), r, font=self.font, fill=(30, 30, 34, 255))

    def _dib(self, q):
        """当前这张图往上错开 q/PHASES 像素后的位图（预乘透明度的 BGRA，在预乘之后插值，边缘不出杂色）；同一张图各档只做一次。"""
        e = self.cur
        if q not in e["dibs"]:
            if e["base"] is None:                      # 整数运算（比浮点快几倍，戳她冒爱心时每帧都要做）
                a = np.asarray(e["img"], dtype=np.uint16)
                alpha = a[..., 3:4]
                e["base"] = np.concatenate([(a[..., 2::-1] * alpha + 127) // 255, alpha], axis=2)
            base = e["base"]
            if q:
                out = base * (PHASES - q)
                out[:-1] += base[1:] * q
                bgra = ((out + PHASES // 2) // PHASES).astype(np.uint8)
            else:
                bgra = base.astype(np.uint8)
            bgra = np.ascontiguousarray(bgra)
            w, h = e["img"].size
            bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
            mem = g32.CreateCompatibleDC(None)
            bits = ctypes.c_void_p()
            bmp = g32.CreateDIBSection(mem, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
            old = g32.SelectObject(mem, bmp)
            ctypes.memmove(bits, bgra.ctypes.data, w * h * 4)
            e["dibs"][q] = (mem, bmp, old)
            e["base"] = None if len(e["dibs"]) >= PHASES else base      # 各档都做好了就不用再留浮点数组
        return e["dibs"][q][0]

    @staticmethod
    def _free_entry(e):
        for mem, bmp, old in e["dibs"].values():
            g32.SelectObject(mem, old)
            g32.DeleteObject(bmp)
            g32.DeleteDC(mem)
        e["dibs"], e["base"] = {}, None

    def _free_dibs(self):
        """换主题、退出时：拼好的图全扔掉。"""
        getattr(self, "bases", {}).clear()
        for e in self.store.values():
            self._free_entry(e)
        self.store.clear()

    def _present(self, pos, q):
        """图和位置一起交给 UpdateLayeredWindow：换帧和挪窗口在同一次更新里，不会错开一帧。"""
        w, h = self.cur["img"].size
        size, src, dst = wt.SIZE(w, h), wt.POINT(0, 0), wt.POINT(*pos)
        blend = BLENDFUNCTION(0, 0, 255, 1)        # AC_SRC_OVER，整体不透明，按每个像素的透明度
        u32.UpdateLayeredWindow(self.hwnd, None, ctypes.byref(dst), ctypes.byref(size), self._dib(q), ctypes.byref(src), 0, ctypes.byref(blend), 2)
        self.shown = (pos, q, self.cur["gen"])

    # ---------------------------------------------------------------- 互动
    def _poke(self):
        now = time.monotonic()
        self.hop()
        self.pokes = [t for t in self.pokes if now - t < 2.5] + [now]
        if self.sleeping:
            self.sleeping = False
            self.show(self.F("wake", "surprise"), 1.6)
            self.say(self.line("wake"))
        elif self.temp == self.F("pout", "pout") and now < self.temp_until:
            self.show(self.F("pout", "pout"), 2.4)
            self.say(self.line("pout"))
        elif len(self.pokes) >= 4:
            self.pokes = []
            self.show(self.F("pout", "pout"), 2.4)
            self.say(self.line("pout"))
        else:
            f = self.F("poke", "") or random.choice(POKE)
            self.show(f, 1.8)
            self.say(self.line("poke"))
            if f != "surprise":
                s = self.scale
                self.hearts += [{"t0": now + i * .12, "x": .30 + random.random() * .40, "dx": (random.random() - .5) * 60 * s,
                                 "size": (11 + random.random() * 8) * s} for i in range(3)]

    def _remember(self):
        w = self._work()
        _save_state({"x": (self.x - w.left) / max(1, w.right - w.left)})

    def _cursor(self):
        p = wt.POINT()
        u32.GetCursorPos(ctypes.byref(p))
        return p.x, p.y

    def _menu(self):
        m = u32.CreatePopupMenu()
        u32.AppendMenuW(m, 0, 1, "打开 IrisAI")
        u32.AppendMenuW(m, 0, 2, "回到右下角")
        others = [p for p in self.plats if not (self.on and p["id"] == self.on[0])]
        if others:
            u32.AppendMenuW(m, 0, 4, "跳到 IrisAI 的输入框上")
        if self.on:
            u32.AppendMenuW(m, 0, 5, "跳下来")
        u32.AppendMenuW(m, 0x800, 0, None)          # 分隔线
        u32.AppendMenuW(m, 0, 3, "收起桌宠（托盘菜单可以再放出来）")
        x, y = self._cursor()
        u32.SetForegroundWindow(self.hwnd)
        cmd = u32.TrackPopupMenu(m, 0x0100 | 0x0080, x, y, 0, self.hwnd, None)   # TPM_RETURNCMD | TPM_NONOTIFY
        u32.DestroyMenu(m)
        if cmd == 1:
            self.on_open()
        elif cmd == 2:
            w = self._work()
            tx = w.right - self.S - int(160 * self.scale)
            home = lambda: (self.show(self.F("greet", "wave"), 1.8), self.say(self.line("home")), self._stroll_end())
            if self.on:                            # 站在输入框上：直接跳回去
                self.jump_to(None, tx, home)
            else:
                self.flip = tx > self.x
                self.walk = (tx, home)
        elif cmd == 4 and others:
            self.jump_to(min(others, key=lambda p: abs(p["left"] + (p["right"] - p["left"]) / 2 - self.x)))
        elif cmd == 5:
            self.jump_to(None)
        elif cmd == 3:
            self.set_visible(False)

    def _wndproc(self, hwnd, msg, wp, lp):
        try:
            if msg == WM_LBUTTONDOWN:
                x, y = self._cursor()
                self.walk, self.falling, self.jump = None, False, None
                self.drag = {"ox": x - self.x, "oy": y - self.y, "sx": x, "sy": y, "moved": False}
                u32.SetCapture(hwnd)
                return 0
            if msg == WM_MOUSEMOVE:
                if self.drag:
                    x, y = self._cursor()
                    if not self.drag["moved"] and math.hypot(x - self.drag["sx"], y - self.drag["sy"]) < 4:
                        return 0
                    if not self.drag["moved"]:
                        self.drag["moved"], self.hold, self.sleeping, self.on = True, self.F("held", "held"), False, None
                        self.held_t = time.monotonic()
                        self.say(self.line("held"), 1.6)
                        self.air = {"text": self.text}
                    self.x, self.y = x - self.drag["ox"], y - self.drag["oy"]
                elif not self.hover:
                    self.hover = True
                    tme = TRACKMOUSEEVENT(ctypes.sizeof(TRACKMOUSEEVENT), 0x2, hwnd, 0)     # TME_LEAVE
                    u32.TrackMouseEvent(ctypes.byref(tme))
                    now = time.monotonic()
                    if now > self.hover_cool and self.want(now) == "idle" and random.random() < .35:
                        self.hover_cool = now + 8
                        self.show(self.F("hover", "wink"), 1.3)
                        if random.random() < .5:
                            self.say(self.line("hover"), 1.4)
                return 0
            if msg == WM_MOUSELEAVE:
                self.hover = False
                return 0
            if msg == WM_CAPTURECHANGED and self.drag and lp != hwnd:
                msg = WM_LBUTTONUP                 # 拖着的时候鼠标被别的窗口抢走：当成松手，掉下去
            if msg == WM_LBUTTONUP:
                d, self.drag = self.drag, None
                if u32.GetCapture() == hwnd:
                    u32.ReleaseCapture()
                if d and d["moved"]:
                    self.y = min(self.y, self._floor())
                    self._fall()                   # 松手：掉到脚下最近的台面（IrisAI 的输入框）或任务栏
                elif d:
                    self.click_at = time.monotonic() + 0.22
                return 0
            if msg == WM_LBUTTONDBLCLK:
                self.click_at = 0
                self.on_open()
                return 0
            if msg == WM_RBUTTONUP:
                self._menu()
                return 0
            if msg == WM_APP + 1:
                self._show(bool(wp))
                return 0
            if msg == WM_APP + 3:                  # 换了主题：换角色
                frames, lines, skin = _theme(self.want_skin)
                if frames:
                    self.frame_paths, self.lines, self.skin = frames, lines, skin
                    self.pf = _pet_faces(skin)
                    self._custom(True)
                    self._colors()
                    self.cache.clear()
                    self._render(force=True)
                    self._place(time.monotonic())
                    if self._want_visible and not self.visible:      # 从没有桌宠帧的主题换回来
                        self._show(True)
                elif self.visible:
                    self._show(False)
                return 0
            if msg == WM_CLOSE:
                u32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                u32.PostQuitMessage(0)
                return 0
        except Exception as e:      # 窗口过程里出错不能让整个线程崩掉
            print("桌宠出错：", repr(e))
        return u32.DefWindowProcW(hwnd, msg, wp, lp)


def _load_state():
    return _load_json(STATE) or {}


def _save_state(p):
    try:
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        d = {**_load_state(), **p}
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump(d, f)
    except OSError:
        pass


def enabled():
    """桌面程序启动时要不要放出桌宠：设置里“桌宠”选了“桌面上”、看板图开着、没换成自己上传的图。"""
    look = (_load_json(os.path.join(ROOT, "data", "settings.json")) or {}).get("look") or {}
    return look.get("petMode") == "desktop" and look.get("mascot", "on") == "on" and not look.get("mascotSrc")


if __name__ == "__main__":
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
    pet = DesktopPet(on_open=lambda: print("双击：打开 IrisAI"))
    pet.start()
    try:
        while pet.thread and pet.thread.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pet.stop()

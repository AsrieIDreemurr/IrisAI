"""一键安装（2026-11-22）：根目录的 安装.bat 调它。可以重复运行：已经做好的步骤会跳过，下载会接着下。

1. 建 Python 虚拟环境 hub\\.venv，装 requirements.txt 里的依赖
2. 配置：没有的话从示例复制 config\\models.yaml、config\\hub.json，生成登录密码 config\\api_key.txt
3. 看看显卡、内存，和默认模型的要求对一下
4. 按功能分组选要下的模型；llama.cpp、llama-swap、ComfyUI 缺了一起装（scripts\\download_models.py）
5. 按模型清单生成调度配置 config\\llama-swap.yaml
6. 可选：桌面快捷方式、开机自启

这一步用系统的 Python 跑，只用标准库；第 4、5 步换成虚拟环境里的 Python。
"""
import ctypes
import os
import secrets
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENV_PY = os.path.join(ROOT, "hub", ".venv", "Scripts", "python.exe")
CFG = os.path.join(ROOT, "config")


def step(n, text):
    print(f"\n==== {n}. {text} ====", flush=True)


def ask(q, default=True):
    a = input(f"{q} [{'Y/n' if default else 'y/N'}] ").strip().lower()
    return default if not a else a in ("y", "yes", "是", "好")


def run(cmd, **kw):
    print("  >", " ".join(f'"{c}"' if " " in c else c for c in cmd), flush=True)
    return subprocess.run(cmd, **kw).returncode


def venv():
    step(1, "Python 环境")
    if sys.version_info < (3, 11):
        print(f"要 Python 3.11 以上，现在是 {sys.version.split()[0]}。到 python.org 装 3.12，或者运行：winget install -e --id Python.Python.3.12")
        return False
    if not os.path.isfile(VENV_PY):
        if run([sys.executable, "-m", "venv", os.path.join(ROOT, "hub", ".venv")]):
            print("建虚拟环境失败")
            return False
    else:
        print("  虚拟环境已有：hub\\.venv")
    if run([VENV_PY, "-m", "pip", "install", "--disable-pip-version-check", "-q", "-r", os.path.join(ROOT, "requirements.txt")]):
        print("装依赖失败（看上面的报错；网络不通的话可以换 pip 镜像再运行一次）")
        return False
    print("  依赖装好了")
    return True


def configs():
    step(2, "配置")
    for name in ("models.yaml", "hub.json"):
        dst = os.path.join(CFG, name)
        src = os.path.join(CFG, name.replace(".", ".example.", 1))
        if os.path.exists(dst):
            print(f"  已有 config\\{name}，不动它")
        else:
            shutil.copyfile(src, dst)
            print(f"  从示例复制了 config\\{name}（以后换模型、改路径都改它）")
    key = os.path.join(CFG, "api_key.txt")
    if os.path.exists(key):
        print("  已有登录密码 config\\api_key.txt")
    else:
        with open(key, "w", encoding="utf-8") as f:
            f.write(secrets.token_urlsafe(18))
        print("  生成了登录密码，存在 config\\api_key.txt（网页登录、局域网访问都用它，别发给别人）")
    return True


class MemStatus(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong), ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong), ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def hardware():
    step(3, "硬件")
    m = MemStatus()
    m.dwLength = ctypes.sizeof(m)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    ram = m.ullTotalPhys / 2**30
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=20).stdout.strip().splitlines()
        gpus = [(a.strip(), int(b) / 1024) for a, b in (x.rsplit(",", 1) for x in out if "," in x)]
    except (OSError, subprocess.SubprocessError, ValueError):
        gpus = []
    print(f"  内存 {ram:.0f} GB；显卡：" + ("、".join(f"{n}（{v:.0f} GB）" for n, v in gpus) or "没找到 NVIDIA 显卡"))
    vram = max((v for _, v in gpus), default=0)
    if not gpus:
        print("  ！没有 NVIDIA 显卡：llama.cpp 会装 CPU 版，对话会很慢，图像、视频用不了。")
    elif vram < 11.5:
        print("  ！默认模型按 12 GB 显存选的。显存小的话，下载前先把 config\\models.yaml 里的主力模型换成小一点的量化（看里面的说明）。")
    if ram < 31:
        print("  ！默认模型要 32 GB 以上内存（主力模型一部分放在内存里，还有常驻的 9B）。内存小的话同样先改清单。")
    return True


def downloads():
    step(4, "下载模型和程序")
    print("  按功能分组，只下用得到的。下载能中断：关掉窗口，以后再运行 安装.bat 会接着下。")
    print("  以后也可以在网页的 设置 → 模型 里下。")
    return run([VENV_PY, os.path.join(ROOT, "scripts", "download_models.py")]) == 0


def gen():
    step(5, "生成调度配置")
    return run([VENV_PY, os.path.join(ROOT, "scripts", "gen_models.py")]) == 0


def shortcut(path, target, args, icon, workdir):
    ps = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:LNK); $s.TargetPath = $env:TGT; $s.Arguments = $env:ARGS; "
          "$s.IconLocation = $env:ICO; $s.WorkingDirectory = $env:WD; $s.Save()")
    env = {**os.environ, "LNK": path, "TGT": target, "ARGS": args, "ICO": icon, "WD": workdir}
    return subprocess.run(["powershell", "-NoProfile", "-Command", ps], env=env).returncode == 0


def shortcuts():
    step(6, "快捷方式")
    shell = os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows")
    desktop = os.path.join(os.path.expanduser("~"), "Desktop")
    pyw = os.path.join(ROOT, "hub", ".venv", "Scripts", "pythonw.exe")
    icon = os.path.join(ROOT, "hub", "static", "img", "app.ico")
    if ask("在桌面放一个 IrisAI 快捷方式？（双击打开，服务没运行会自动启动）"):
        ok = shortcut(os.path.join(desktop, "IrisAI.lnk"), pyw, f'"{os.path.join(ROOT, "scripts", "desktop_app.py")}"', icon, ROOT)
        print("  放好了" if ok else "  没放成")
    if ask("开机自动在后台启动服务？（不开的话，第一次双击 IrisAI 时启动，要多等十几秒）", default=False):
        ps = shutil.which("pwsh") or "powershell"
        ok = shortcut(os.path.join(shell, "Start Menu", "Programs", "Startup", "IrisAI 后台服务.lnk"), ps,
                      f'-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "{os.path.join(ROOT, "scripts", "start-llama-swap.ps1")}"',
                      icon, ROOT)
        print("  设好了" if ok else "  没设成")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    print("IrisAI 安装。可以重复运行：做过的步骤会跳过。")
    for f in (venv, configs, hardware, downloads, gen):
        if f() is False:
            print("\n这一步没成功，解决后再运行 安装.bat。")
            return 1
    shortcuts()
    print("\n装好了。双击桌面的 IrisAI 打开（或者浏览器访问 http://127.0.0.1:8088），密码在 config\\api_key.txt。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断。再运行 安装.bat 会接着做。")
        sys.exit(1)

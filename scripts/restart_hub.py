"""重启正式的网页服务（8088）：等到没有对话在运行，停掉旧进程，按开机脚本的方式重新启动。

自改的“应用”和“撤回”改了 Python 代码时，由服务自己在后台起这个脚本。也可以手动运行：
  hub\\.venv\\Scripts\\python.exe scripts\\restart_hub.py
日志追加到 logs\\restart_hub.log。
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

import psutil
from hub_processes import hub_processes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "hub", "server.py")
PY = os.path.join(ROOT, "hub", ".venv", "Scripts", "python.exe")
KEY = open(os.path.join(ROOT, "config", "api_key.txt"), encoding="utf-8").read().strip()
LOG = os.path.join(ROOT, "logs", "restart_hub.log")
LEGACY_WINDOW = None


def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(time.strftime("%F %T ") + msg + "\n")


def spawn_outside_job(out_path, err_path):
    """让 Windows 的 WMI 服务启动网页服务：新进程的父进程是 WmiPrvSE，不在调用方的作业对象里。
    2026-11-14：在 Claude 桌面版的命令里重启，作业对象不让脱离，退回普通方式后服务挂在那个会话下，
    会话暂停时被一起清理掉（服务停了、没有退出记录）。日志照样追加到 hub.log / hub.out.log。"""
    import base64
    cl = f'cmd /d /c ""{PY}" "{SERVER}" >> "{out_path}" 2>> "{err_path}""'
    ps = ("$si = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ShowWindow=[uint16]0}; "
          "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments "
          "@{CommandLine=$env:HUB_SPAWN_CL; CurrentDirectory=$env:HUB_SPAWN_DIR; ProcessStartupInformation=$si}; "
          "exit [int]$r.ReturnValue")
    env = {**os.environ, "HUB_SPAWN_CL": cl, "HUB_SPAWN_DIR": ROOT}
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand",
                            base64.b64encode(ps.encode("utf-16-le")).decode()], env=env, timeout=60,
                           capture_output=True, creationflags=0x08000000)
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"WMI 启动出错：{e}")
        return False
    if r.returncode != 0:
        log(f"WMI 启动返回 {r.returncode}：{(r.stderr or b'').decode('gbk', 'replace')[:300]}")
    return r.returncode == 0


def get(path):
    req = urllib.request.Request("http://127.0.0.1:8088" + path, headers={"Authorization": "Bearer " + KEY})
    return json.load(urllib.request.urlopen(req, timeout=10))


def busy():
    try:
        state = get("/api/activity")
    except Exception as exc:
        if not LEGACY_WINDOW:
            raise
        import urllib.error
        if not isinstance(exc, urllib.error.HTTPError) or exc.code != 404:
            raise
        from hub_legacy_window import legacy_activity
        state = legacy_activity(ROOT, get, LEGACY_WINDOW)
    if not isinstance(state.get("idle"), bool) or not isinstance(state.get("tasks"), list):
        raise ValueError("活动接口返回格式不对")
    return [] if state["idle"] and not state["tasks"] else [t.get("label", t.get("kind", "任务")) for t in state["tasks"]] or ["活动状态不一致"]


def hub_procs():
    return hub_processes(ROOT)


def main():
    time.sleep(3)               # 让发起重启的请求先返回
    for i in range(120):        # 最多等 10 分钟
        try:
            b = busy()
        except Exception as exc:
            log("无法确认空闲，取消重启：" + type(exc).__name__)
            return 1
        if not b:
            break
        if i == 0:
            log("等待运行中的对话结束：" + "；".join(b))
        time.sleep(5)
    else:
        log("等了 10 分钟仍有对话在运行，这次不重启")
        return 1
    procs = hub_procs()
    if len(procs) != 1:
        log("无法唯一识别正式 Hub，取消重启")
        return 1
    for p in procs:
        try:
            p.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(procs, timeout=10)
    if any(p.is_running() for p in procs):
        log("旧 Hub 未完全退出，取消启动新实例")
        return 1
    out_path, err_path = os.path.join(ROOT, "logs", "hub.out.log"), os.path.join(ROOT, "logs", "hub.log")
    out = open(out_path, "a", encoding="utf-8")
    err = open(err_path, "a", encoding="utf-8")
    # 无窗口 + 脱离 + 新进程组 + 脱离作业对象：从命令行工具里运行这个脚本时，新服务不会随那条命令结束而被一起关掉
    flags = 0x08000000 | 0x00000008 | 0x00000200
    try:
        subprocess.Popen([PY, SERVER], cwd=ROOT, stdout=out, stderr=err, creationflags=flags | 0x01000000)
    except OSError:          # 所在的作业对象不允许脱离（例如 Claude 桌面版里跑的命令）：让 WMI 替我们启动
        out.close(); err.close()
        if not spawn_outside_job(out_path, err_path):
            log("WMI 启动失败，退回普通方式（这个命令的会话结束时新服务可能被一起关掉）")
            with open(out_path, "a", encoding="utf-8") as out, open(err_path, "a", encoding="utf-8") as err:
                subprocess.Popen([PY, SERVER], cwd=ROOT, stdout=out, stderr=err, creationflags=flags)
    for _ in range(30):
        time.sleep(1)
        try:
            state = get("/api/activity")
            new = hub_procs()
            if not isinstance(state.get("idle"), bool) or len(new) != 1 or new[0].pid == procs[0].pid:
                continue
            log(f"已重启（停掉 {len(procs)} 个旧进程）")
            return 0
        except Exception:
            continue
    log("重启后 30 秒内没有响应，请检查 logs\\hub.log")
    return 1


if __name__ == "__main__":
    if sys.stdout:              # 用 pythonw 运行时没有 stdout
        sys.stdout.reconfigure(encoding="utf-8")
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy-window")
    LEGACY_WINDOW = parser.parse_args().legacy_window
    raise SystemExit(main())

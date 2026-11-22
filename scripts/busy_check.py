"""检查全部活动；旧版本首次升级可显式传 --legacy-window 维护凭据。"""
import argparse
import json
from pathlib import Path
import sys
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")
KEY = (Path(__file__).resolve().parent.parent / "config" / "api_key.txt").read_text(encoding="utf-8").strip()
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("port", nargs="?", default="8088")
parser.add_argument("--legacy-window", help="用户已确认无人使用的限时、绑定旧进程的 JSON 记录")
args = parser.parse_args()
PORT = args.port


def get(path):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", headers={"Authorization": "Bearer " + KEY})
    return json.load(urllib.request.urlopen(req, timeout=10))


try:
    try:
        state = get("/api/activity")
    except urllib.error.HTTPError as exc:
        if exc.code != 404 or not args.legacy_window:
            raise
        from hub_legacy_window import legacy_activity
        state = legacy_activity(Path(__file__).resolve().parents[1], get, args.legacy_window, int(PORT))
    if not isinstance(state.get("idle"), bool) or not isinstance(state.get("tasks"), list):
        raise ValueError("活动接口返回格式不对")
except Exception as exc:
    print("无法确认空闲：" + type(exc).__name__ + "；未获空闲许可")
    sys.exit(2)
busy = state["tasks"]
print("空闲" if state["idle"] and not busy else "运行中：" + "；".join(t.get("label", t.get("kind", "任务")) for t in busy))
if state.get("legacy"):
    print(f"旧版首次升级：已查全部 {state['checked_conversations']} 个对话及索引/模型状态；非对话任务以用户确认的维护窗口为依据。")
sys.exit(0 if state["idle"] and not busy else 1)

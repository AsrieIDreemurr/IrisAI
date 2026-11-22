"""把启动拒绝同时写到本模型日志和调度器输出，供 Hub 读取本次失败原因。"""
import datetime as dt
import json
import sys
import time
from pathlib import Path

MARKER = "HUB_STARTUP_ERROR "


def refuse(root, model, log_name, reason):
    event = {"model": model, "time": time.time(), "reason": reason}
    # 先输出；即使本地日志不可写，调度器和网页仍能获得拒绝原因。
    print(MARKER + json.dumps(event, ensure_ascii=False), flush=True)
    try:
        path = Path(root) / "logs" / log_name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(f"{dt.datetime.now():%F %T} {reason}\n")
    except OSError as exc:
        print(f"启动拒绝日志写入失败：{exc}", file=sys.stderr, flush=True)
    return 1

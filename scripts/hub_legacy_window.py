"""仅供缺少 /api/activity 的旧 Hub 首次升级；需要用户确认的限时、绑定进程的维护记录。

旧服务不能列出所有非对话任务，因此此入口不是自动空闲检测的替代品。
日常重启不传维护记录，任何查询失败仍拒绝放行。
"""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import time

from hub_processes import hub_processes


def legacy_activity(root, get, permit_path, port=8088):
    root = Path(root).resolve()
    permit = json.loads(Path(permit_path).read_text(encoding="utf-8"))
    now = time.time()
    if (port != 8088 or permit.get("purpose") != "A09-first-upgrade" or
            permit.get("user_confirmed_idle") is not True or permit.get("used") or
            not permit.get("created_at", 0) <= now < permit.get("expires", 0) or
            not 0 < permit["expires"] - permit["created_at"] <= 7200):
        raise ValueError("旧版维护凭据缺失、过期或不适用")
    processes = hub_processes(str(root), port)
    if len(processes) != 1 or processes[0].pid != permit.get("pid") or abs(processes[0].create_time() - permit.get("process_created", 0)) > 0.01:
        raise ValueError("正式服务进程与维护凭据不匹配")
    with sqlite3.connect((root / "data/hub.db").as_uri() + "?mode=ro", uri=True) as db:
        ids = [r[0] for r in db.execute("SELECT id FROM convs")]
    def inspect(cid):
        response = get("/api/convs/" + cid)
        if "run" not in response:
            raise ValueError("旧版对话接口缺少运行状态")
        return {"kind": "对话", "id": response["run"], "label": "运行中的对话 " + cid} if response["run"] else None
    with ThreadPoolExecutor(max_workers=4) as pool:
        tasks = [r for r in pool.map(inspect, ids) if r]
    scan = get("/api/kb").get("status", {})
    if not isinstance(scan.get("running"), bool):
        raise ValueError("无法确认知识库扫描状态")
    if scan["running"]:
        tasks.append({"kind": "知识库", "label": "知识库扫描"})
    status = get("/api/status")
    if status.get("ok") is not True or not isinstance(status.get("models"), list):
        raise ValueError("无法确认模型调度器状态")
    for model in status["models"]:
        if model.get("state") not in ("ready", "stopped"):
            tasks.append({"kind": "模型", "label": "模型状态 " + str(model.get("state"))})
    # 读完后再次核验凭据和旧进程；不能拿一次旧检查授权另一个进程。
    if time.time() >= permit["expires"] or not processes[0].is_running() or abs(processes[0].create_time() - permit["process_created"]) > 0.01:
        raise ValueError("维护窗口或旧进程已经失效")
    return {"idle": not tasks, "tasks": tasks, "legacy": True, "checked_conversations": len(ids),
            "non_conversation_basis": "用户明确确认无人使用并留出本次维护窗口"}

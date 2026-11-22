"""用端口归属和完整脚本路径识别 Hub，不依赖 Windows 的连接状态标签。"""
import os
import psutil


def hub_processes(root, port=8088):
    target = os.path.normcase(os.path.abspath(os.path.join(root, "hub", "server.py")))
    owners = {c.pid for c in psutil.net_connections(kind="tcp") if c.laddr and c.laddr.port == port and c.pid}
    result = []
    for p in psutil.process_iter(["pid", "cmdline"]):
        try:
            if not any(os.path.normcase(os.path.abspath(a.strip('"'))) == target for a in (p.info["cmdline"] or [])):
                continue
            if p.pid in owners or any(c.laddr and c.laddr.port == port for c in p.net_connections(kind="tcp")):
                result.append(p)
        except (psutil.Error, TypeError, ValueError, OSError):
            continue
    return result

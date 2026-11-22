"""模型清单 config/models.yaml（2026-11-14，见 docs/开源分离方案.md）：读清单、补默认值、生成调度器配置。

网页服务（app/config.py）、scripts/gen_models.py、scripts/download_models.py 都用这里，所以不依赖 app.config（那里会读密钥）。
字段说明见 config/models.example.yaml。
"""
import os
import re

import yaml

ROLES = ("chat", "write", "fast", "ja_zh", "translate", "ocr", "embed", "rerank", "image")
SRV_FLAGS = "--host 127.0.0.1 --port ${PORT} --jinja --no-webui"     # 只监听本机；外部访问一律经过调度器（带密钥）
GPU_FLAGS = "--fit on -fa on"
CPU_FLAGS = "-ngl 0 -t {threads} --no-mmproj-auto"                   # 完全不碰显卡


class ManifestError(ValueError):
    pass


def path_of(config_dir):
    return os.path.join(config_dir, "models.yaml")


def _abs(root, p):
    return os.path.normpath(os.path.join(root, p)).replace("\\", "/")


def load(root, config_dir=None):
    """读清单，补默认值，路径换成绝对路径（正斜杠）。清单不对时抛 ManifestError，说清楚哪一项。"""
    p = path_of(config_dir or os.path.join(root, "config"))
    try:
        with open(p, encoding="utf-8") as f:
            m = yaml.safe_load(f) or {}
    except FileNotFoundError:
        raise ManifestError(f"没有模型清单 {p}：复制 config/models.example.yaml 改成自己的")
    except yaml.YAMLError as e:
        raise ManifestError(f"模型清单格式不对：{e}")
    paths = {k: _abs(root, v) for k, v in (m.get("paths") or {}).items() if v}
    paths.setdefault("models", _abs(root, "models"))
    paths.setdefault("python", _abs(root, "hub/.venv/Scripts/python.exe"))
    sched = {"idle_unload": 900, "health_timeout": 300, "cpu_threads": 8, **(m.get("scheduler") or {})}
    models = {}
    for mid, e in (m.get("models") or {}).items():
        if not re.fullmatch(r"[\w.\-]+", str(mid)):
            raise ManifestError(f"模型 id 只能用字母、数字、点、下划线、减号：{mid}")
        e = dict(e or {})
        kinds = [k for k in ("llama", "launcher", "comfyui") if e.get(k)]
        if len(kinds) != 1:
            raise ManifestError(f"{mid}：llama、launcher、comfyui 三种启动方式要有且只有一种")
        e["kind"] = kinds[0]
        e.setdefault("name", mid)
        e.setdefault("title", e["name"])
        e.setdefault("device", "gpu")
        if e["device"] not in ("gpu", "cpu"):
            raise ManifestError(f"{mid}：device 只能是 gpu 或 cpu")
        for k in ("vision", "big", "no_tokenize", "startup_check", "unlisted"):
            e[k] = bool(e.get(k))
        if e["kind"] == "llama":
            ll = e["llama"]
            if not ll.get("model"):
                raise ManifestError(f"{mid}：llama 下要写 model（模型文件，相对 paths.models）")
            ll["model_path"] = _abs(paths["models"], ll["model"])
            if ll.get("mmproj"):
                ll["mmproj_path"] = _abs(paths["models"], ll["mmproj"])
        if e["kind"] == "comfyui" and "comfy" not in paths:
            raise ManifestError(f"{mid}：用 ComfyUI 要在 paths 里写 comfy（ComfyUI 本体所在的文件夹）")
        models[str(mid)] = e
    roles = dict(m.get("roles") or {})
    for r, mid in roles.items():
        if mid not in models:
            raise ManifestError(f"roles.{r} 用的 {mid} 不在 models 里")
    for r in ("chat", "fast"):
        if r not in roles:
            raise ManifestError(f"roles 里至少要有 chat 和 fast（{r} 没写）")
    groups = m.get("download_groups") or {}
    if not isinstance(groups, dict):
        raise ManifestError("download_groups 要写成 组名: {desc, required} 的形式")
    return {"paths": paths, "scheduler": sched, "roles": roles, "models": models, "file": p,
            "download_groups": {str(k): dict(v or {}) for k, v in groups.items()}}


def from_legacy(cfg):
    """没有清单、hub.json 里还是旧写法（models / chat_models）时凑一份最小的清单：测试脚本临时建的配置是这种。
    只够网页服务用（不能生成调度配置）。"""
    models = {}
    for c in cfg.get("chat_models") or []:
        models[c["id"]] = {"name": c.get("name") or c["id"], "menu": c.get("note") or True}
    for mid in (cfg.get("models") or {}).values():
        models.setdefault(mid, {"name": mid})
    for mid, e in models.items():
        e.update(kind="legacy", title=e["name"], device="gpu", vision=True, big=False, no_tokenize=False,
                 startup_check=False, unlisted=False)
        e.setdefault("menu", None)
    return {"paths": {}, "scheduler": {"idle_unload": cfg.get("gpu_ttl_seconds", 900)}, "roles": dict(cfg.get("models") or {}),
            "models": models, "file": None}


def command(man, mid):
    """这个模型的启动命令（给调度器，${PORT} 由调度器填）。"""
    e, paths, root = man["models"][mid], man["paths"], None
    if e["kind"] == "llama":
        ll = e["llama"]
        dev = GPU_FLAGS if e["device"] == "gpu" else CPU_FLAGS.format(threads=man["scheduler"]["cpu_threads"])
        parts = [paths["llama_server"], SRV_FLAGS, dev, f"-c {int(ll.get('ctx') or 8192)}", "-m " + ll["model_path"]]
        if ll.get("mmproj_path"):
            parts.append("--mmproj " + ll["mmproj_path"])
        if ll.get("args"):
            parts.append(" ".join(str(ll["args"]).split()))
        if "llama_server" not in paths:
            raise ManifestError("paths 里没写 llama_server（llama.cpp 的 llama-server.exe）")
        return " ".join(parts)
    if e["kind"] == "launcher":
        root = os.path.dirname(os.path.dirname(os.path.abspath(man["file"])))
        py = _abs(root, e["python"]) if e.get("python") else paths["python"]
        return f"{py} {_abs(root, e['launcher'])} ${{PORT}}"
    py = paths.get("comfy_python") or _abs(paths["comfy"], "../python_embeded/python.exe")
    # --windows-standalone-build 会顺带启动后打开浏览器，--disable-auto-launch 关掉它，只在后台跑
    return (f"{py} -s {paths['comfy']}/main.py --windows-standalone-build --disable-auto-launch "
            f"--listen 127.0.0.1 --port ${{PORT}}")


def swap_config(man):
    """调度器（llama-swap）配置的内容（dict）。"""
    s = man["scheduler"]
    models, gpu, cpu = {}, [], []
    for mid, e in man["models"].items():
        d = {"name": e["title"], "cmd": command(man, mid)}
        if e["device"] == "cpu":
            d["env"] = ["CUDA_VISIBLE_DEVICES=-1"]
            d["ttl"] = 0
        elif e.get("ttl") is not None:
            d["ttl"] = int(e["ttl"])
        if e.get("check"):
            d["checkEndpoint"] = e["check"]
        if e["unlisted"]:
            d["unlisted"] = True
        models[mid] = d
        (cpu if e["device"] == "cpu" else gpu).append(mid)
    groups = {}
    if gpu:
        groups["gpu"] = {"swap": True, "exclusive": False, "members": gpu}
    if cpu:
        groups["cpu"] = {"persistent": True, "swap": False, "exclusive": False, "members": cpu}
    return {"apiKeys": ["${env.LLAMA_SWAP_KEY}"], "globalTTL": int(s["idle_unload"]), "healthCheckTimeout": int(s["health_timeout"]),
            "models": models, "routing": {"router": {"use": "group", "settings": {"groups": groups}}}}


def swap_yaml(man):
    """生成的配置文件全文：开头说明它是生成的，命令写成折行方便看。"""
    cfg = swap_config(man)
    head = ("# 由 scripts/gen_models.py 按 config/models.yaml 生成，别直接改（下次生成会覆盖）。\n"
            "# 显卡组同一时间只放一个模型，切换时自动卸载上一个；CPU 组常驻内存、不占显存，不会被显卡组挤掉。\n\n")
    out = [head, f"apiKeys:\n  - \"{cfg['apiKeys'][0]}\"\n\n",
           f"globalTTL: {cfg['globalTTL']}\nhealthCheckTimeout: {cfg['healthCheckTimeout']}\n\nmodels:\n"]
    for mid, d in cfg["models"].items():
        out.append(f"  {mid}:\n    name: {_q(d['name'])}\n")
        if "env" in d:
            out.append("    env:\n" + "".join(f"      - {_q(x)}\n" for x in d["env"]))
        out.append("    cmd: >-\n" +"".join(f"      {ln}\n" for ln in _wrap(d["cmd"])))
        for k in ("checkEndpoint", "ttl", "unlisted"):
            if k in d:
                out.append(f"    {k}: {_q(d[k]) if isinstance(d[k], str) else str(d[k]).lower()}\n")
        out.append("\n")
    out.append("routing:\n  router:\n    use: group\n    settings:\n      groups:\n")
    for g, v in cfg["routing"]["router"]["settings"]["groups"].items():
        out.append(f"        {g}:\n")
        for k, val in v.items():
            if k != "members":
                out.append(f"          {k}: {str(val).lower()}\n")
        out.append("          members:\n" + "".join(f"            - {x}\n" for x in v["members"]))
    text = "".join(out)
    if yaml.safe_load(text) != cfg:          # 手写的格式和内容对不上就说明这里有错，别写出坏配置
        raise ManifestError("生成的调度配置和清单对不上（gen 代码的问题）")
    return text


def _q(s):
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _wrap(cmd, width=100):
    lines, cur = [], ""
    for tok in cmd.split(" "):
        if cur and len(cur) + 1 + len(tok) > width and not cur.endswith(("-m", "--mmproj", "-c")):
            lines.append(cur)
            cur = tok
        else:
            cur = f"{cur} {tok}" if cur else tok
    return lines + ([cur] if cur else [])


def public(man):
    """给网页的模型信息（不含路径）：设置页的模型表、状态栏的短名字、哪些在 CPU 上。"""
    out = []
    for mid, e in man["models"].items():
        ll = e.get("llama") or {}
        files = [ll.get("model_path"), ll.get("mmproj_path")] if e["kind"] == "llama" else []
        out.append({"id": mid, "name": e["name"], "title": e["title"], "device": e["device"], "menu": e.get("menu"),
                    "roles": [r for r, v in man["roles"].items() if v == mid], "vision": e["vision"],
                    "ctx": ll.get("ctx"), "agent_ctx": e.get("agent_ctx"), "size": e.get("size"), "kind": e["kind"],
                    "files_ok": all(os.path.isfile(f) for f in files if f) if files else None, "unlisted": e["unlisted"]})
    return out

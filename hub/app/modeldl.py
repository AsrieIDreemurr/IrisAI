"""下载模型和程序（2026-11-22）：命令行 scripts/download_models.py、安装脚本 scripts/setup.py、网页设置页共用。

- 模型：按模型清单 config/models.yaml 里各模型的 download 下载，按 download_groups 分组（基础、翻译、识字……）。
  文件来自 HuggingFace（repo + file），或者任意地址（url，可附 sha256）。断点续传，下载到 *.part，校验 SHA-256 后改名。
- 程序：llama.cpp、llama-swap、ComfyUI 便携版，版本固定为作者测过的（PROGRAMS），从 GitHub 发布页下载。
  llama.cpp 按显卡驱动支持的 CUDA 版本选（nvidia-smi），没有 N 卡用 CPU 版。
- 进度写进一个 JSON 文件（网页读它显示进度条），日志写 logs/download.log。

不依赖 app.config（那里会读密钥），脚本直接 import 也能用。
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile

from . import modelcfg

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOG = os.path.join(ROOT, "logs", "download.log")
META_CACHE = os.path.join(ROOT, "data", "download_meta.json")
PROGRESS = os.path.join(ROOT, "data", "download_progress.json")
TOKEN_FILE = os.path.join(ROOT, "config", "hf_token.txt")
SPACE_MARGIN = 5 * 1024**3          # 下载完至少还留 5 GB
UA = {"User-Agent": "IrisAI-setup"}

# 程序：作者机器上测过的版本。换版本只改这里（发布页文件名会跟着版本变）
LLAMA_TAG = "b11988"
PROGRAMS = {
    "llama.cpp": {"repo": "ggml-org/llama.cpp", "tag": LLAMA_TAG, "assets": {
        "cuda13": [f"llama-{LLAMA_TAG}-bin-win-cuda-13.4-x64.zip", "cudart-llama-bin-win-cuda-13.4-x64.zip"],
        "cuda12": [f"llama-{LLAMA_TAG}-bin-win-cuda-12.4-x64.zip", "cudart-llama-bin-win-cuda-12.4-x64.zip"],
        "cpu": [f"llama-{LLAMA_TAG}-bin-win-cpu-x64.zip"]}},
    "llama-swap": {"repo": "mostlygeek/llama-swap", "tag": "v265", "assets": {"any": ["llama-swap_265_windows_amd64.zip"]}},
    "ComfyUI": {"repo": "comfyanonymous/ComfyUI", "tag": "v0.37.0", "assets": {
        "cuda13": ["ComfyUI_windows_portable_nvidia.7z"], "cuda12": ["ComfyUI_windows_portable_nvidia_cu126.7z"]}},
}


# ---------------------------------------------------------------- 小工具
def log(msg):
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def hf_token():
    """HuggingFace 令牌（只有要登录才能下的仓库用得上）：环境变量 HF_TOKEN，或 config/hf_token.txt。不打印。"""
    t = os.environ.get("HF_TOKEN", "").strip()
    if not t and os.path.isfile(TOKEN_FILE):
        t = open(TOKEN_FILE, encoding="utf-8").read().strip()
    return t


def _get_json(url, hf=False):
    h = dict(UA)
    if hf and hf_token():
        h["Authorization"] = "Bearer " + hf_token()
    return json.load(urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=60))


def gb(n):
    return f"{n / 1e9:.1f} GB"


def cuda_variant():
    """显卡驱动支持的 CUDA：cuda13 / cuda12 / cpu（没有 N 卡或驱动太旧）。"""
    try:
        out = subprocess.run(["nvidia-smi"], capture_output=True, text=True, timeout=20, creationflags=0x08000000).stdout
    except (OSError, subprocess.SubprocessError):
        return "cpu"
    m = re.search(r"CUDA (?:UMD )?Version:\s*(\d+)\.(\d+)", out)        # 新驱动写成 “CUDA UMD Version”
    if not m:
        return "cpu"
    v = (int(m.group(1)), int(m.group(2)))
    return "cuda13" if v >= (13, 4) else "cuda12" if v >= (12, 4) else "cpu"


def gpu_info():
    """[(显卡名, 显存 MB)]，没有 N 卡返回 []。"""
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=20, creationflags=0x08000000).stdout
        return [(a.strip(), int(b)) for a, b in (ln.rsplit(",", 1) for ln in out.strip().splitlines() if "," in ln)]
    except (OSError, subprocess.SubprocessError, ValueError):
        return []


# ---------------------------------------------------------------- ComfyUI 的模型文件夹
def comfy_dirs(comfy):
    """ComfyUI 找模型的文件夹：{类别: [文件夹...]}，自己的 models 在前，extra_model_paths.yaml 配的在后。"""
    import yaml
    out = {}
    base = os.path.join(comfy, "models")
    try:
        cats = [c for c in os.listdir(base) if os.path.isdir(os.path.join(base, c))]
    except OSError:
        cats = []
    for c in cats:
        out[c] = [os.path.join(base, c)]
    try:
        extra = yaml.safe_load(open(os.path.join(comfy, "extra_model_paths.yaml"), encoding="utf-8")) or {}
    except (OSError, ValueError):
        extra = {}
    for sec in extra.values():
        if isinstance(sec, dict):
            b = sec.get("base_path", "")
            for cat, sub in sec.items():
                if cat != "base_path" and isinstance(sub, str):
                    out.setdefault(cat, [os.path.join(base, cat)])
                    out[cat] += [os.path.join(b, x.strip()) for x in sub.splitlines() if x.strip()]
    return out


def _comfy_target(comfy, cat, dirs):
    """新文件下到哪：extra_model_paths.yaml 配了、存在的文件夹优先（大模型一般放那），否则 ComfyUI 自己的 models。"""
    ds = dirs.get(cat) or [os.path.join(comfy, "models", cat)]
    return next((d for d in ds[1:] if os.path.isdir(d)), ds[0])


# ---------------------------------------------------------------- 要下哪些文件
def groups_of(man):
    """分组：[(组名, {desc, required})]，按清单 download_groups 的顺序；清单没写的组排在后面。"""
    g = list((man.get("download_groups") or {}).items())
    seen = {k for k, _ in g}
    for it in plan(man):
        if it["group"] not in seen:
            seen.add(it["group"])
            g.append((it["group"], {}))
    return [(k, v or {}) for k, v in g]


def plan(man):
    """清单里所有要下的文件：[{group, model, repo, file, url, sha256, name, dest, final, present, comfy}]。"""
    paths, out, dirs = man["paths"], [], None
    for mid, e in man["models"].items():
        for d in e.get("download") or []:
            group = d.get("group") or e.get("group") or "其他"
            name = os.path.basename(d.get("file") or d.get("url", "").split("?")[0])
            to, comfy = d.get("to"), None
            found = None
            if to and to.startswith("comfy:"):
                if "comfy" not in paths:
                    raise modelcfg.ManifestError(f"{mid}：要下到 ComfyUI，但 paths 里没写 comfy")
                comfy = to[len("comfy:"):]
                dirs = dirs if dirs is not None else comfy_dirs(paths["comfy"])
                found = next((os.path.join(x, name) for x in dirs.get(comfy, []) if os.path.isfile(os.path.join(x, name))), None)
                dest = _comfy_target(paths["comfy"], comfy, dirs)
            elif to:
                dest = os.path.join(paths["models"], to)
            elif e["kind"] == "llama":
                dest = os.path.dirname(e["llama"]["model_path"])
            else:
                raise modelcfg.ManifestError(f"{mid}：download 里的 {name} 没写 to（不是 llama 模型，猜不出放哪）")
            final = found or os.path.normpath(os.path.join(dest, name))
            out.append({"group": group, "model": mid, "repo": d.get("repo"), "file": d.get("file"), "url": d.get("url"),
                        "sha256": d.get("sha256"), "name": name, "dest": os.path.normpath(dest), "final": final,
                        "present": os.path.isfile(final) and not os.path.isfile(final + ".part"), "comfy": comfy})
    return out


def _cache():
    try:
        return json.load(open(META_CACHE, encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def meta(it, cache=None):
    """(大小, sha256 或 None)。HuggingFace 的从 API 取（LFS 的 oid 就是 sha256）；url 的用 HEAD 取大小。结果缓存在 data/。"""
    key = it["url"] or f"{it['repo']}/{it['file']}"
    c = cache if cache is not None else _cache()
    if key in c:
        return c[key]["size"], c[key].get("sha256")
    if it["url"]:
        r = urllib.request.urlopen(urllib.request.Request(it["url"], headers=UA, method="HEAD"), timeout=60)
        size, sha = int(r.headers.get("Content-Length") or 0), it.get("sha256")
    else:
        d = os.path.dirname(it["file"])
        size = sha = None
        for x in _get_json(f"https://huggingface.co/api/models/{it['repo']}/tree/main" + (f"/{d}" if d else ""), hf=True):
            if x["path"] == it["file"]:
                size, sha = (x["lfs"]["size"], x["lfs"]["oid"]) if x.get("lfs") else (x["size"], None)
                break
        if size is None:
            raise RuntimeError(f"{it['repo']} 里没有 {it['file']}")
    c[key] = {"size": size, "sha256": sha}
    return size, sha


def fill_sizes(items):
    """给每项填 size（并行取，最多 8 个同时），取不到的 size 为 None。缓存写回 data/。"""
    cache = _cache()
    todo = [it for it in items if (it["url"] or f"{it['repo']}/{it['file']}") not in cache]

    def one(it):
        try:
            meta(it, cache)
        except Exception as e:           # 网络不通、仓库改名：大小显示“未知”，下载时再报错
            it["meta_error"] = str(e)[:200]
    ts = [threading.Thread(target=one, args=(it,)) for it in todo]
    for i in range(0, len(ts), 8):
        for t in ts[i:i + 8]:
            t.start()
        for t in ts[i:i + 8]:
            t.join()
    if todo:
        try:
            os.makedirs(os.path.dirname(META_CACHE), exist_ok=True)
            json.dump(cache, open(META_CACHE, "w", encoding="utf-8"), ensure_ascii=False)
        except OSError:
            pass
    for it in items:
        k = it["url"] or f"{it['repo']}/{it['file']}"
        it["size"] = cache.get(k, {}).get("size")
    return items


# ---------------------------------------------------------------- 程序
def program_targets(man, variant=None):
    """要装的程序：{名字: {dest, present, assets, needed}}。ComfyUI 只在清单里有 comfy 路径时才算。"""
    paths, out = man["paths"], {}
    variant = variant or cuda_variant()
    if paths.get("llama_server"):
        out["llama.cpp"] = {"dest": os.path.dirname(paths["llama_server"]), "present": os.path.isfile(paths["llama_server"])}
    swap = [d for d in os.listdir(os.path.join(ROOT, "bin")) if d.startswith("llama-swap")] if os.path.isdir(os.path.join(ROOT, "bin")) else []
    out["llama-swap"] = {"dest": os.path.join(ROOT, "bin", "llama-swap"),
                         "present": any(os.path.isfile(os.path.join(ROOT, "bin", d, "llama-swap.exe")) for d in swap)}
    if paths.get("comfy"):
        out["ComfyUI"] = {"dest": os.path.dirname(paths["comfy"]), "present": os.path.isfile(os.path.join(paths["comfy"], "main.py"))}
    for k, v in out.items():
        a = PROGRAMS[k]["assets"]
        v["assets"] = a.get(variant) or a.get("any") or a.get("cuda12") or []
        v["tag"] = PROGRAMS[k]["tag"]
    return out


def _release_assets(repo, tag):
    """{文件名: (下载地址, 大小, sha256 或 None)}。GitHub 新发布的文件带 digest。"""
    d = _get_json(f"https://api.github.com/repos/{repo}/releases/tags/{tag}")
    out = {}
    for a in d.get("assets", []):
        dg = a.get("digest") or ""
        out[a["name"]] = (a["browser_download_url"], a["size"], dg[7:] if dg.startswith("sha256:") else None)
    return out


def _extract(archive, dest):
    if archive.lower().endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            z.extractall(dest)
        return
    # 7z：Windows 自带的 tar（libarchive）能解；装了 7-Zip 的话也可以用它
    tar = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "tar.exe")
    seven = shutil.which("7z") or (r"C:\Program Files\7-Zip\7z.exe" if os.path.isfile(r"C:\Program Files\7-Zip\7z.exe") else None)
    os.makedirs(dest, exist_ok=True)
    cmd = [seven, "x", "-y", f"-o{dest}", archive] if seven else [tar, "-xf", archive, "-C", dest]
    r = subprocess.run(cmd, capture_output=True, text=True, creationflags=0x08000000)
    if r.returncode:
        raise RuntimeError(f"解压失败：{(r.stderr or r.stdout)[-300:]}")


# ---------------------------------------------------------------- 下载
class Progress:
    """写进度文件（网页读）。每秒最多写一次，状态变化时马上写。"""

    def __init__(self, path, total, count):
        self.path, self.last = path, 0
        self.d = {"state": "running", "pid": os.getpid(), "started": time.time(), "total": total, "count": count,
                  "index": 0, "done": 0, "file": "", "file_done": 0, "file_size": 0, "phase": "", "failed": [], "message": ""}
        self.write(True)

    def set(self, force=False, **kw):
        self.d.update(kw)
        self.write(force)

    def write(self, force=False):
        if not self.path or (not force and time.time() - self.last < 1):
            return
        self.last = time.time()
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            json.dump(self.d, open(tmp, "w", encoding="utf-8"), ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError:
            pass


def _fetch(url, part, size, prog, base, headers=None):
    """curl 断点续传下到 part，边下边报进度。成功返回 True。"""
    args = ["curl.exe", "-sSL", "--fail", "-C", "-", "--retry", "5", "--retry-delay", "5", "-o", part, url]
    for k, v in (headers or {}).items():
        args[1:1] = ["-H", f"{k}: {v}"]
    for attempt in range(5):
        p = subprocess.Popen(args, creationflags=0x08000000)
        while p.poll() is None:
            have = os.path.getsize(part) if os.path.exists(part) else 0
            prog.set(file_done=have, done=base + have)
            time.sleep(1)
        have = os.path.getsize(part) if os.path.exists(part) else 0
        if p.returncode == 0 and (not size or have == size):
            return True
        if size and have > size:            # 服务器上的文件变了：从头来
            os.remove(part)
        log(f"  第 {attempt + 1} 次没下完（curl={p.returncode}），重试")
        time.sleep(10)
    return False


def _sha256(p, prog):
    h, n, size = hashlib.sha256(), 0, os.path.getsize(p)
    with open(p, "rb") as f:
        while chunk := f.read(1 << 24):
            h.update(chunk)
            n += len(chunk)
            prog.set(phase=f"校验 {n * 100 // max(size, 1)}%")
    return h.hexdigest()


def check_space(jobs):
    """[(目标文件夹, 字节)] → 每个盘够不够。返回不够的说明列表（空表示够）。"""
    need = {}
    for dest, n in jobs:
        d = dest
        while not os.path.exists(d) and os.path.dirname(d) != d:
            d = os.path.dirname(d)
        drive = os.path.splitdrive(os.path.abspath(d))[0] or d
        need.setdefault(drive, [d, 0])[1] += n
    bad = []
    for drive, (d, n) in need.items():
        free = shutil.disk_usage(d).free
        if free < n + SPACE_MARGIN:
            bad.append(f"{drive} 盘要 {gb(n)}，只剩 {gb(free)}（还要留 {gb(SPACE_MARGIN)}）")
    return bad


def run(man, groups, progress_path=PROGRESS, programs=True, variant=None):
    """下载这些组的缺的文件；programs=True 时先装缺的程序（要 ComfyUI 的组才装 ComfyUI）。返回失败的文件名列表。"""
    variant = variant or cuda_variant()
    items = [it for it in plan(man) if it["group"] in groups and not it["present"]]
    progs = {}
    if programs:
        need_comfy = any(it["comfy"] for it in plan(man) if it["group"] in groups)
        progs = {k: v for k, v in program_targets(man, variant).items() if not v["present"] and (k != "ComfyUI" or need_comfy)}
    # 先把大小都取到，算总量、查空间
    prog_files = []
    for k, v in progs.items():
        rel = _release_assets(PROGRAMS[k]["repo"], v["tag"])
        for a in v["assets"]:
            if a not in rel:
                raise RuntimeError(f"{k} {v['tag']} 的发布页里没有 {a}")
            url, size, sha = rel[a]
            prog_files.append({"program": k, "name": a, "url": url, "size": size, "sha256": sha, "dest": v["dest"]})
    fill_sizes(items)
    for it in items:
        if it.get("size") is None:
            meta(it)                         # 再取一次，取不到就让异常说明原因
            it["size"] = meta(it)[0]
    bad = check_space([(f["dest"], f["size"] * 2 if f["name"].endswith(".7z") else f["size"]) for f in prog_files] +
                      [(it["dest"], it["size"]) for it in items])
    if bad:
        raise RuntimeError("磁盘空间不够：" + "；".join(bad))
    total = sum(f["size"] for f in prog_files) + sum(it["size"] for it in items)
    prog = Progress(progress_path, total, len(prog_files) + len(items))
    failed, base = [], 0
    try:
        # 程序：下到 bin/.download，校验后解压
        tmpdir = os.path.join(ROOT, "bin", ".download")
        for i, f in enumerate(prog_files):
            prog.set(True, index=i + 1, file=f"{f['program']}：{f['name']}", file_size=f["size"], file_done=0, phase="下载")
            os.makedirs(tmpdir, exist_ok=True)
            part = os.path.join(tmpdir, f["name"] + ".part")
            done_file = os.path.join(tmpdir, f["name"])
            try:
                if not (os.path.isfile(done_file) and os.path.getsize(done_file) == f["size"]):
                    log(f"下载 {f['program']} {f['name']}（{gb(f['size'])}）")
                    if not _fetch(f["url"], part, f["size"], prog, base):
                        raise RuntimeError("下载没完成")
                    if f["sha256"] and _sha256(part, prog) != f["sha256"]:
                        os.remove(part)
                        raise RuntimeError("SHA-256 不符，已删掉下坏的文件，重新运行会重下")
                    os.replace(part, done_file)
                prog.set(True, phase="解压")
                log(f"解压 {f['name']} → {f['dest']}")
                if f["program"] == "ComfyUI":          # 便携版里面是一层 ComfyUI_windows_portable\，挪成 paths.comfy 的上一级
                    tmp = os.path.join(tmpdir, "comfy_x")
                    shutil.rmtree(tmp, ignore_errors=True)
                    _extract(done_file, tmp)
                    inner = next((os.path.join(tmp, x) for x in os.listdir(tmp) if os.path.isdir(os.path.join(tmp, x))), None)
                    if not inner or os.path.exists(f["dest"]):
                        raise RuntimeError(f"ComfyUI 解压后的结构不对，或者 {f['dest']} 已经存在")
                    os.makedirs(os.path.dirname(f["dest"]), exist_ok=True)
                    shutil.move(inner, f["dest"])
                    shutil.rmtree(tmp, ignore_errors=True)
                else:
                    _extract(done_file, f["dest"])
                os.remove(done_file)
                log(f"装好 {f['program']}")
            except Exception as e:
                log(f"失败 {f['name']}：{e}")
                failed.append(f["name"])
            base += f["size"]
            prog.set(True, done=base)
        # 模型
        headers = {"Authorization": "Bearer " + hf_token()} if hf_token() else None
        for i, it in enumerate(items):
            n = len(prog_files) + i + 1
            prog.set(True, index=n, file=it["name"], file_size=it["size"], file_done=0, phase="下载")
            os.makedirs(it["dest"], exist_ok=True)
            final, part = os.path.join(it["dest"], it["name"]), os.path.join(it["dest"], it["name"] + ".part")
            try:
                size, sha = meta(it)
                if os.path.isfile(final) and os.path.getsize(final) == size and not os.path.exists(part):
                    log(f"跳过（已存在）{it['name']}")
                else:
                    url = it["url"] or f"https://huggingface.co/{it['repo']}/resolve/main/{it['file']}"
                    log(f"下载 {url if it['url'] else it['repo'] + '/' + it['file']}（{gb(size)}）→ {it['dest']}")
                    if not _fetch(url, part, size, prog, base, None if it["url"] else headers):
                        raise RuntimeError("下载没完成（再运行一次会接着下）")
                    if sha:
                        got = _sha256(part, prog)
                        if got != sha:
                            os.remove(part)          # 只删自己下坏的半截文件
                            raise RuntimeError("SHA-256 不符，已删掉下坏的文件，重新运行会重下")
                    os.replace(part, final)
                    log(f"完成并校验 {it['name']}")
            except Exception as e:
                log(f"失败 {it['name']}：{e}")
                failed.append(it["name"])
            base += it["size"]
            prog.set(True, done=base, phase="")
        prog.set(True, state="done" if not failed else "failed", failed=failed, file="", phase="",
                 message="全部完成" if not failed else f"{len(failed)} 个没下好，再点一次下载会接着下")
        log("全部结束" + (f"，失败 {len(failed)} 个：{failed}" if failed else "，全部成功"))
    except BaseException as e:
        prog.set(True, state="failed", message=str(e)[:300])
        raise
    return failed


def read_progress(path=PROGRESS):
    """进度文件 + 进程还在不在（被结束、崩溃时 state 还是 running，这里改成 stopped）。"""
    try:
        d = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if d.get("state") == "running":
        try:
            import psutil
            alive = psutil.pid_exists(d.get("pid", -1)) and "python" in psutil.Process(d["pid"]).name().lower()
        except Exception:
            alive = False
        if not alive:
            d["state"] = "stopped"
    return d


def status(man):
    """网页和安装脚本用：每组的文件数、缺几个、总大小、缺多少，要不要 ComfyUI；程序装了没有。"""
    items = fill_sizes(plan(man))
    out = []
    for g, info in groups_of(man):
        its = [it for it in items if it["group"] == g]
        if not its:
            continue
        miss = [it for it in its if not it["present"]]
        out.append({"id": g, "desc": info.get("desc", ""), "required": bool(info.get("required")),
                    "files": len(its), "missing": len(miss), "size": sum(it["size"] or 0 for it in its),
                    "missing_size": sum(it["size"] or 0 for it in miss), "unknown": any(it["size"] is None for it in miss),
                    "comfy": any(it["comfy"] for it in its), "models": sorted({it["model"] for it in its}),
                    "missing_files": [it["name"] for it in miss]})
    try:
        progs = {k: {"present": v["present"], "tag": v["tag"]} for k, v in program_targets(man).items()}
    except OSError:
        progs = {}
    return {"groups": out, "programs": progs}


def main_python():
    """跑下载用的 Python：项目的 venv，没有就用当前的。"""
    p = os.path.join(ROOT, "hub", ".venv", "Scripts", "python.exe")
    return p if os.path.isfile(p) else sys.executable

"""一键下载模型和程序（按模型清单 config/models.yaml 的 download 和 download_groups）。

用法: hub\\.venv\\Scripts\\python.exe scripts\\download_models.py [选项]
  不带选项      列出各组（大小、缺多少），问要下哪些组
  --list        只列出，不下载
  --groups 基础,翻译   下这几组缺的文件（逗号分隔）；--all 下全部
  --only 关键字  只下模型 id、仓库或文件名里有这个关键字的（旧用法）
  --no-programs 不装 llama.cpp、llama-swap、ComfyUI（默认缺了就装）
  --yes         不问，直接下
  --progress 文件  进度写到这个 JSON 文件（网页用）
断点续传 + SHA-256 校验：先下成 *.part，核对后才改名；中断了再运行会接着下。日志 logs/download.log。
核心代码在 hub/app/modeldl.py（网页设置页的“下载”也用它）。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "hub"))
from app import modelcfg, modeldl  # noqa: E402


def arg(name):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv and sys.argv.index(name) + 1 < len(sys.argv) else None


def show(man):
    st = modeldl.status(man)
    print("\n程序：")
    for k, v in st["programs"].items():
        print(f"  {k:<11} {v['tag']:<8} {'已装' if v['present'] else '没装（下载时自动装）'}")
    print("\n模型（按功能分组）：")
    for i, g in enumerate(st["groups"], 1):
        mark = "齐了" if not g["missing"] else f"缺 {g['missing']} 个，{modeldl.gb(g['missing_size'])}" + ("（部分大小未知）" if g["unknown"] else "")
        print(f"  {i}. {g['id']}{'（必装）' if g['required'] else ''}  {g['desc']}")
        print(f"     共 {modeldl.gb(g['size'])}，{mark}")
    return st


def ask(prompt="> "):
    try:
        return input(prompt).strip().lower()
    except EOFError:                   # 没有输入（被别的程序调用）：当成什么都不选
        return None


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        man = modelcfg.load(ROOT)
    except modelcfg.ManifestError as e:
        print("模型清单有问题：", e)
        return 1
    only = arg("--only")
    if only:                                   # 旧用法：按关键字挑文件，组照样走下面的流程
        hit = {it["group"] for it in modeldl.plan(man) if only.lower() in f"{it['model']} {it['repo']} {it['file']} {it['name']}".lower()}
        groups = sorted(hit)
        for mid, e in man["models"].items():   # 同组里别的模型不下：把它们的 download 先拿掉
            e["download"] = [d for d in e.get("download") or []
                             if only.lower() in f"{mid} {d.get('repo')} {d.get('file')} {d.get('url')}".lower()]
    elif "--all" in sys.argv:
        groups = [g for g, _ in modeldl.groups_of(man)]
    elif arg("--groups"):
        groups = [x.strip() for x in arg("--groups").replace("，", ",").split(",") if x.strip()]
    else:
        groups = None
    if "--list" in sys.argv:
        show(man)
        for it in modeldl.plan(man):
            if groups is None or it["group"] in groups:
                print(f"  [{it['group']}] {it['model']}: {it['repo'] or it['url']}/{it['file'] or ''} → {it['final']}"
                      f"{'（已有）' if it['present'] else ''}")
        return 0
    if groups is None:
        st = show(man)
        names = [g["id"] for g in st["groups"]]
        default = [g["id"] for g in st["groups"] if g["required"] and g["missing"]]
        print("\n输入要下载的组号，用空格隔开（例如 1 2 4），all 表示全部，直接回车 = "
              + (" ".join(str(names.index(x) + 1) for x in default) if default else "什么都不下"))
        ans = ask()
        if ans is None:
            print("没有输入，退出")
            return 0
        if ans in ("all", "a", "全部"):
            groups = names
        elif not ans:
            groups = default
        else:
            try:
                groups = [names[int(x) - 1] for x in ans.replace(",", " ").split()]
            except (ValueError, IndexError):
                print("没看懂，退出")
                return 1
        if not groups:
            print("什么都没选")
            return 0
    unknown = [g for g in groups if g not in {x for x, _ in modeldl.groups_of(man)}]
    if unknown:
        print("清单里没有这些组：", "、".join(unknown))
        return 1
    if "--yes" not in sys.argv and not sys.stdin.isatty():
        print("不是在窗口里运行：要直接下载，加 --yes")
        return 1
    if "--yes" not in sys.argv:
        miss = [it for it in modeldl.fill_sizes(modeldl.plan(man)) if it["group"] in groups and not it["present"]]
        print(f"\n要下：{'、'.join(groups)}，{len(miss)} 个文件，约 {modeldl.gb(sum(it['size'] or 0 for it in miss))}"
              "（另加缺的程序）。中途可以关掉，再运行会接着下。继续？[Y/n]")
        if ask() in (None, "n", "no", "否"):
            return 0
    try:
        failed = modeldl.run(man, groups, arg("--progress") or modeldl.PROGRESS, programs="--no-programs" not in sys.argv)
    except Exception as e:
        modeldl.log(f"没能开始：{e}")
        return 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

"""按模型清单 config/models.yaml 生成调度器配置 config/llama-swap.yaml（2026-11-14）。

用法：hub\\.venv\\Scripts\\python.exe scripts\\gen_models.py [--check] [--out 文件]
  内容没变就不写（调度器开着 -watch-config，写了就会重新加载）；写之前把旧文件备份成 llama-swap.yaml.bak。
  --check  只检查清单、列出缺的模型文件，不写。
开机脚本 start-llama-swap.ps1 启动调度器前会跑一次。清单有错时不写，旧配置照用，退出码 1。
"""
import argparse
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "hub"))
from app import modelcfg  # noqa: E402


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--out", default=os.path.join(ROOT, "config", "llama-swap.yaml"))
    args = ap.parse_args()
    try:
        man = modelcfg.load(ROOT)
        text = modelcfg.swap_yaml(man)
    except modelcfg.ManifestError as e:
        print("模型清单有问题，没有生成：", e)
        return 1
    for m in modelcfg.public(man):
        if m["files_ok"] is False:
            print(f"提醒：{m['id']} 的模型文件不在（scripts\\download_models.py --only {m['id']} 可以下载）")
    if args.check:
        print(f"清单没问题：{len(man['models'])} 个模型")
        return 0
    try:
        with open(args.out, encoding="utf-8") as f:
            if f.read() == text:
                print("调度配置没变")
                return 0
    except FileNotFoundError:
        pass
    if os.path.exists(args.out):
        shutil.copy2(args.out, args.out + ".bak")
    tmp = args.out + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, args.out)
    print("已生成", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())

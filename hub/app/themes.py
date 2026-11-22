"""主题文件夹在哪（2026-11-14 从 hub\\static\\skins 挪出来，见 docs\\开源分离方案.md）。

主题素材常有不许再分发的许可，所以和代码分开放；上传代码时整个主题文件夹不带，只带 themes\\README.md。
位置：环境变量 HUB_THEMES > config\\hub.json 的 themes_dir（相对路径按项目根目录算）> 项目根目录\\themes。
网页服务、桌面程序、桌宠都用这里的函数找主题。桌面脚本不 import app.config（那里会读密钥、建目录），所以这里不依赖它。
"""
import json
import os
import re


def themes_dir(root, cfg=None):
    """root：项目根目录；cfg：hub.json 的内容（不给就自己读 root\\config\\hub.json）。"""
    p = os.environ.get("HUB_THEMES")
    if not p:
        if cfg is None:
            try:
                with open(os.path.join(root, "config", "hub.json"), encoding="utf-8") as f:
                    cfg = json.load(f)
            except (OSError, ValueError):
                cfg = {}
        p = cfg.get("themes_dir") or "themes"
    return os.path.normpath(os.path.join(root, p))


def theme_path(base, skin):
    """某个主题的文件夹；名字不合法（防 ..\\ 之类）返回 None。不检查存不存在。"""
    if not re.fullmatch(r"[\w\-]+", skin or "") or skin == "default":
        return None
    return os.path.join(base, skin)

"""读取 config/hub.json 和访问密钥。settings.json 存界面设置和授权目录，可在网页里改。"""
import json
import os

from . import modelcfg
from .themes import themes_dir

HUB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 自改副本的测试实例：代码和网页用副本自己的，配置和数据用正式目录（HUB_AI_ROOT 指过去）
AI_ROOT = os.environ.get("HUB_AI_ROOT") or os.path.dirname(HUB_DIR)
CONFIG_DIR = os.environ.get("HUB_CONFIG_DIR") or os.path.join(AI_ROOT, "config")
CONFIG_PATH = os.path.join(CONFIG_DIR, "hub.json")
KEY_PATH = os.path.join(CONFIG_DIR, "api_key.txt")
DATA_DIR = os.environ.get("HUB_DATA_DIR") or os.path.join(AI_ROOT, "data")
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")
STATIC_DIR = os.path.join(HUB_DIR, "static")

os.makedirs(DATA_DIR, exist_ok=True)

with open(CONFIG_PATH, encoding="utf-8") as f:
    CFG = json.load(f)
with open(KEY_PATH, encoding="utf-8") as f:
    API_KEY = f.read().strip()

SANDBOX = os.path.normpath(os.path.join(AI_ROOT, os.environ.get("HUB_SANDBOX") or CFG["sandbox"]))   # 相对路径按项目根目录算
os.makedirs(SANDBOX, exist_ok=True)
# 主题文件夹（不在代码目录里，见 app/themes.py）；网页地址仍是 /s/skins/<主题>/...
THEMES_DIR = themes_dir(AI_ROOT, CFG)
os.makedirs(THEMES_DIR, exist_ok=True)


def cfg_path(key, default):
    """hub.json 里的路径：相对路径按项目根目录算（2026-11-14 起不在代码里写死本机路径，见 docs/开源分离方案.md）。"""
    return os.path.normpath(os.path.join(AI_ROOT, CFG.get(key) or default))


# 模型清单 config/models.yaml（app/modelcfg.py）：各功能用哪个模型、对话页列哪些、每个模型的能力。
# 以前写在 hub.json 的 models / chat_models / gpu_ttl_seconds，现在由清单填进 CFG，原来读 CFG 的地方不用改
if not os.path.exists(modelcfg.path_of(CONFIG_DIR)) and "models" in CFG:
    MODELS = modelcfg.from_legacy(CFG)          # 测试脚本临时建的旧写法配置
else:
    MODELS = modelcfg.load(AI_ROOT, CONFIG_DIR)
M = MODELS["models"]
ROLES = MODELS["roles"]
CFG["models"] = ROLES
CFG["chat_models"] = [{"id": k, "name": e["name"], "note": e["menu"] if e["menu"] is not True else ""}
                      for k, e in M.items() if e.get("menu")]
CFG["gpu_ttl_seconds"] = MODELS["scheduler"]["idle_unload"]

# ComfyUI 根目录（生图、存储页用它的 models）：清单的 paths.comfy；“挪到待删除”时在哪个文件夹下建 _待删除_<说明>
COMFY_DIR = os.path.normpath(MODELS["paths"]["comfy"]) if MODELS["paths"].get("comfy") else cfg_path("comfy_dir", "../ComfyUI/ComfyUI")
TRASH_DIR = cfg_path("trash_dir", ".")


# 测试实例（设了 HUB_PORT）在网页里改的设置只留在内存里，不写回正式的 settings.json。
# 以前模型在自改副本的测试实例里点了“自改权限”开关做点击测试，把正式服务的自改进关掉了
TEST_MODE = bool(os.environ.get("HUB_PORT"))
_test_settings = None


def load_settings():
    if TEST_MODE and _test_settings is not None:
        return json.loads(json.dumps(_test_settings))
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            s = json.load(f)
    except (OSError, ValueError):
        s = {}
    s.setdefault("look", {})
    s.setdefault("allowed_dirs", CFG.get("allowed_dirs", []))
    s.setdefault("default_model", CFG["models"]["chat"])
    s.setdefault("think", False)
    s.setdefault("web", True)
    s.setdefault("level", "standard")   # 检查力度：quick 快速 / standard 标准 / strict 严格
    s.setdefault("self_edit_enabled", False)
    s.setdefault("self_edit_full", False)
    if s["self_edit_enabled"] is not True:
        s["self_edit_full"] = False
    return s


def save_settings(s):
    global _test_settings
    if TEST_MODE:
        _test_settings = json.loads(json.dumps(s))
        return
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=1)
    os.replace(tmp, SETTINGS_PATH)

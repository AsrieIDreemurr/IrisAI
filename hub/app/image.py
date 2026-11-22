"""图像：ComfyUI 工作流（文生图、改图、放大、视频）、中文描述转标签、画风预设、提示词检查。
网页的生图模块和 Agent 的 generate_image 工具共用。ComfyUI 由调度器按需启动（/comfyui/ 转发）。
"""
import asyncio
import datetime as dt
import json
import os
import random
import re
import time
import uuid

from . import llm
from .config import CFG, COMFY_DIR, DATA_DIR, ROLES, SANDBOX
from .jobs import Job
from .paths import display

OUT_DIR = os.path.join(SANDBOX, "图片")
VIDEO_DIR = os.path.join(SANDBOX, "视频")
PRESETS_PATH = os.path.join(DATA_DIR, "image_presets.json")
HISTORY_PATH = os.path.join(DATA_DIR, "images.jsonl")

# 文生图模型。参数按各自作者的推荐：NoobAI 只能用 euler；Anima 官方模板 30 步、CFG 4、euler、simple
SDXL_NEG = ("worst quality, old, early, low quality, lowres, signature, username, logo, bad hands, mutated hands, "
            "extra digits, fewer digits, bad anatomy, jpeg artifacts, watermark, text")
ANIMA_NEG = "worst quality, low quality, score_1, score_2, score_3, blurry, jpeg artifacts, sepia"
MODELS = {
    "noobai": {"name": "NoobAI-XL Vpred 1.0", "kind": "sdxl", "file": "NoobAI-XL-Vpred-v1.0.safetensors",
               "steps": 28, "cfg": 4.5, "sampler": "euler", "scheduler": "normal",
               "quality": "masterpiece, best quality, newest, absurdres, highres, ", "neg": SDXL_NEG, "artist": "artist:{}"},
    "anima29": {"name": "Anima 2.9B（暗场景备选）", "kind": "anima", "file": "Anima-2.9B-preview-v1_int8_convrot.safetensors",
                "steps": 30, "cfg": 4.0, "sampler": "euler", "scheduler": "sgm_uniform",
                "quality": "masterpiece, best quality, score_7, ", "neg": ANIMA_NEG, "artist": "@{}"},
    # Qwen-Image 2.1 Turbo：和改图同一个模型，8 步、CFG 1。直接写自然语言（中文也行），不转标签；写实、画面里的中文字比较好
    "qwen21": {"name": "Qwen-Image 2.1", "kind": "qwen", "file": "qwen_image_2.1_turbo_int8_convrot.safetensors",
               "steps": 8, "cfg": 1.0, "sampler": "euler", "scheduler": "simple", "quality": "", "neg": "", "artist": "{} 的画风"},
}
QWEN_TE, QWEN_VAE = "qwen3vl_8b_int8_convrot.safetensors", "qwen_image_2.1_vae_bf16.safetensors"
SIZES = {"square": (1024, 1024), "portrait": (832, 1216), "landscape": (1216, 832), "tall": (768, 1344), "wide": (1344, 768)}
UPSCALER = "RealESRGAN_x4plus_anime_6B.pth"
COMFY_MODELS = os.path.join(COMFY_DIR, "models")      # hub.json 的 comfy_dir

# 视频：MiniMax H3（11-22 盲测胜出；自带声音）。两个模型共用文本编码器和两个 VAE。
# 用时是 832×576、5 秒的实测（RTX 4070 Ti 12 GB），10 秒大约 2.5 倍。加速 LoRA 不再算一个模型，和用户自己的 LoRA 一样在 LoRA 里选。
H3_UNET = "minimax_h3_fl2va_pruned_int8_convrot.safetensors"
H3_SHARED = [("text_encoders", "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"), ("vae", "minimax_h3_video_vae_fp16.safetensors"),
             ("vae", "minimax_h3_audio_vae_fp32.safetensors")]
H3_TURBO = "minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors"
VIDEO_MODELS = {
    "h3": {"name": "H3", "unet": H3_UNET, "arch": "h3", "steps": 20, "secs": 266},
    "fasth3": {"name": "FastH3", "unet": "fastvideo_fasth3_8step_v2_pruned_int8_convrot.safetensors",
               "arch": "h3", "steps": 8, "fast": True, "secs": 90},
}
LEGACY_VIDEO = {"h3turbo": ("h3", H3_TURBO)}         # 11-22 的“H3 加速”= H3 + 加速 LoRA（历史记录和旧请求还会用这个名字）
# 已知的 LoRA：选了它，默认步数跟着改（用户在步数里填了数就按填的）
KNOWN_LORAS = {H3_TURBO: {"title": "H3 加速", "steps": 6, "secs": 105}}
VIDEO_SIZES = {"landscape": (832, 576), "wide": (864, 480), "portrait": (576, 832), "tall": (480, 864), "square": (704, 704)}
VIDEO_EXT = {".mp4", ".webm", ".mov", ".mkv"}


def model_dirs():
    """ComfyUI 找模型的文件夹：自己的 models，加上 extra_model_paths.yaml 里的（例如 F 盘）。返回 {类别: [文件夹...]}。"""
    out = {cat: [os.path.join(COMFY_MODELS, cat)] for cat in ("checkpoints", "diffusion_models", "text_encoders", "vae", "loras")}
    try:
        import yaml
        extra = yaml.safe_load(open(os.path.join(COMFY_DIR, "extra_model_paths.yaml"), encoding="utf-8")) or {}
    except (OSError, ValueError, ImportError):
        extra = {}
    for sec in extra.values():
        if not isinstance(sec, dict):
            continue
        base = sec.get("base_path", "")
        for cat, sub in sec.items():
            if cat in out and isinstance(sub, str):
                out[cat] += [os.path.join(base, x.strip()) for x in sub.splitlines() if x.strip()]
    return out


def has_model(cat, name, dirs=None):
    return any(os.path.exists(os.path.join(d, name)) for d in (dirs or model_dirs()).get(cat, []))


def video_available():
    dirs = model_dirs()
    shared = all(has_model(c, n, dirs) for c, n in H3_SHARED)
    return {k: shared and has_model("diffusion_models", m["unet"], dirs) for k, m in VIDEO_MODELS.items()}


# ---------------------------------------------------------------- LoRA
# 用户把 LoRA 放进 ComfyUI 的 loras 文件夹（可以分子文件夹），这里列出来。名字用 ComfyUI 的写法：相对 loras 文件夹的路径（Windows 上是反斜杠）。
# 是给哪种模型的，看文件头里的键名和元数据：SDXL（NoobAI 能用）、Anima、H3；SD1.5 的用不了；看不出来的标成“未识别”，哪里都列出来。
LORA_EXT = (".safetensors", ".sft")
ARCH_NAME = {"sdxl": "SDXL", "anima": "Anima", "h3": "H3", "qwen": "Qwen-Image", "sd15": "SD1.5", "": "未识别"}
_lora_cache = {}       # 路径 → (修改时间, 大小, 信息)


def lora_dir():
    """让用户放 LoRA 的文件夹：extra_model_paths.yaml 里配的第一个（大模型都在那），没有就是 ComfyUI 自己的。"""
    ds = model_dirs()["loras"]
    return next((os.path.normpath(d) for d in ds[1:] if os.path.isdir(d)), os.path.normpath(ds[0]))


def _guess_arch(keys, meta):
    m = " ".join(str(meta.get(k, "")) for k in ("ss_base_model_version", "modelspec.architecture", "base_model", "ss_sd_model_name")).lower()
    ks = " ".join(keys[:400])
    if "minimax" in m or "h3" in m.split() or ".qkv_proj." in ks:
        return "h3"
    if "qwen" in m or "img_mlp" in ks or "txt_mlp" in ks or "img_mod" in ks:
        return "qwen"
    if "anima" in m or "cosmos" in m or "llm_adapter" in ks or ("self_attn" in ks and "cross_attn" in ks and "blocks" in ks):
        return "anima"
    if "sdxl" in m or "stable-diffusion-xl" in m or "lora_te2_" in ks or "lora_te1_" in ks or "text_encoder_2" in ks:
        return "sdxl"
    if "sd_v1" in m or "stable-diffusion-v1" in m or "lora_te_text_model" in ks:
        return "sd15"
    if "input_blocks" in ks or "down_blocks" in ks:
        return "sdxl"          # 没有文本编码器部分的 UNet LoRA，按 SDXL 算（SD1.5 的一般都带文本编码器）
    return ""


def _triggers(path, meta):
    """触发词：作者写的（modelspec.trigger_phrase、Civitai 的 .civitai.info / A1111 的 .json 旁边文件），没有就用训练标签里最常见的几个。"""
    out = []
    t = meta.get("modelspec.trigger_phrase")
    if t:
        out += [x.strip() for x in str(t).split(",")]
    stem = os.path.splitext(path)[0]
    for side, key in ((stem + ".civitai.info", "trainedWords"), (stem + ".json", "activation text")):
        try:
            v = json.load(open(side, encoding="utf-8")).get(key)
        except (OSError, ValueError, AttributeError):
            continue
        out += [x.strip() for w in (v if isinstance(v, list) else [v]) if isinstance(w, str) for x in w.split(",")]
    if not out and meta.get("ss_tag_frequency"):
        try:
            freq = {}
            for tags in json.loads(meta["ss_tag_frequency"]).values():
                for k, n in tags.items():
                    freq[k.strip()] = freq.get(k.strip(), 0) + n
            out = sorted(freq, key=lambda k: -freq[k])[:5]
        except (ValueError, AttributeError):
            pass
    seen = []
    for x in out:
        if x and x not in seen:
            seen.append(x)
    return seen[:8]


def lora_info(path):
    """读 safetensors 文件头（只读开头几十 KB），返回 {arch, triggers}。读不了返回 None。"""
    try:
        st = os.stat(path)
        c = _lora_cache.get(path)
        if c and c[0] == st.st_mtime and c[1] == st.st_size:
            return c[2]
        with open(path, "rb") as f:
            n = int.from_bytes(f.read(8), "little")
            if not 2 <= n < 64 * 2**20:
                return None
            head = json.loads(f.read(n))
    except (OSError, ValueError):
        return None
    meta = head.pop("__metadata__", None) or {}
    info = {"arch": _guess_arch(list(head), meta), "triggers": _triggers(path, meta)}
    _lora_cache[path] = (st.st_mtime, st.st_size, info)
    return info


def loras():
    """所有 LoRA：[{name, title, arch, arch_name, size_mb, triggers, steps?, note?}]。同名的只算一次（ComfyUI 也是用先找到的那个）。"""
    out, seen = [], set()
    for d in model_dirs()["loras"]:
        if not os.path.isdir(d):
            continue
        for cur, dirs, files in os.walk(d):
            dirs[:] = sorted(x for x in dirs if not x.startswith("."))
            for f in sorted(files):
                if not f.lower().endswith(LORA_EXT):
                    continue
                p = os.path.join(cur, f)
                name = os.path.relpath(p, d)
                if name in seen:
                    continue
                seen.add(name)
                inf = lora_info(p)
                if inf is None:
                    continue
                k = KNOWN_LORAS.get(name, {})
                out.append({"name": name, "title": k.get("title") or os.path.splitext(name)[0], "arch": inf["arch"],
                            "arch_name": ARCH_NAME.get(inf["arch"], inf["arch"]), "size_mb": round(os.path.getsize(p) / 2**20),
                            "triggers": inf["triggers"], **{x: k[x] for x in ("steps", "note") if x in k}})
    return out


def pick_loras(items, arch):
    """网页传来的 [{name, strength}] → 检查过的 [(名字, 强度)]。文件不在、类型对不上就报错（未识别的放行）。最多 6 个。"""
    if not items:
        return []
    have = {x["name"]: x for x in loras()}
    out = []
    for it in items[:6]:
        name = (it or {}).get("name") if isinstance(it, dict) else None
        if not name:
            continue
        x = have.get(name)
        if not x:
            raise ValueError(f"找不到这个 LoRA：{name}")
        if x["arch"] and x["arch"] != arch:
            raise ValueError(f"{x['title']} 是 {x['arch_name']} 的 LoRA，这个模型用不了")
        try:
            s = float(it.get("strength", 1.0))
        except (TypeError, ValueError):
            s = 1.0
        out.append((name, max(-2.0, min(2.0, s))))
    return out


def _num(v, lo, hi, kind=int):
    """网页填的步数、CFG：空的或不对就返回 None（用默认）。"""
    try:
        v = kind(v)
    except (TypeError, ValueError):
        return None
    return v if lo <= v <= hi else None


def parse_size(size, table, default, lo, hi, step):
    """尺寸：预设的名字（table 里的），或者自定义的“宽x高”（按 step 取整，限制在 lo～hi）。返回 (宽, 高)。"""
    if size in table:
        return table[size]
    m = re.fullmatch(r"\s*(\d+)\s*[x×*]\s*(\d+)\s*", str(size or ""))
    if not m:
        return table[default]
    r = lambda n: min(hi, max(lo, round(int(n) / step) * step))
    return r(m.group(1)), r(m.group(2))


def video_frames(sec):
    """H3 的帧数要是 17k+5（24 帧/秒）：5 秒 → 124 帧。"""
    a = max(5, round(sec * 24))
    return a + (5 - a % 17) % 17


def available():
    """下载好的模型。Anima 系还要文本编码器和 VAE。"""
    out = {}
    for k, m in MODELS.items():
        sub = "checkpoints" if m["kind"] == "sdxl" else "diffusion_models"
        ok = os.path.exists(os.path.join(COMFY_MODELS, sub, m["file"]))
        if m["kind"] == "qwen":
            dirs = model_dirs()
            ok = (has_model("diffusion_models", m["file"], dirs) and has_model("text_encoders", QWEN_TE, dirs)
                  and has_model("vae", QWEN_VAE, dirs))
        if m["kind"] == "anima":
            ok = ok and os.path.exists(os.path.join(COMFY_MODELS, "text_encoders", "qwen_3_06b_base.safetensors"))                 and os.path.exists(os.path.join(COMFY_MODELS, "vae", "qwen_image_vae.safetensors"))
        out[k] = ok
    return out


# ---------------------------------------------------------------- 提示词检查


async def check_edit(data, instruction):
    pass


async def check_prompt(text):
    pass


# ---------------------------------------------------------------- 中文描述转标签
TAG_SYS = """把用户对画面的描述改写成二次元生图模型的提示词。
- 输出英文 Danbooru 标签，小写，逗号分隔，按“人数（1girl、2girls、1boy）, 角色, 作品, 画面内容, 动作, 服装, 场景, 光影, 构图”的顺序。
- 只写描述里有的内容，可以补足画面需要的常见标签（例如 solo、looking at viewer、upper body），不要加画质词。
- 用户写的英文标签、画师名原样保留。
- 不要加任何性相关、裸露相关的标签，除非描述里明确要求，而且描述里的人物都是成年人。
- 只输出提示词本身，不要解释。"""


def has_chinese(text):
    return bool(re.search(r"[一-鿿]", text or ""))


async def to_tags(text, kind="sdxl"):
    extra = "\n- 最后可以加一句简短的英文自然语言描述整体画面。" if kind == "anima" else ""
    out, _ = await llm.complete(CFG["models"]["fast"], [{"role": "system", "content": TAG_SYS + extra}, {"role": "user", "content": text}],
                                max_tokens=400, temperature=0.3)
    return out.strip().strip("`").strip()


# ---------------------------------------------------------------- 画风预设
def load_presets():
    try:
        return json.load(open(PRESETS_PATH, encoding="utf-8"))
    except (OSError, ValueError):
        return []


def save_presets(items):
    tmp = PRESETS_PATH + ".tmp"
    json.dump(items, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    os.replace(tmp, PRESETS_PATH)


def artists(names, model):
    """画师名列表 → 各模型的写法。名字里的括号要转义，否则 ComfyUI 当成权重。"""
    fmt = MODELS[model]["artist"]
    return ", ".join(fmt.format(n.strip().replace("(", r"\(").replace(")", r"\)")) for n in names if n.strip())


def compose(prompt, model, preset=None):
    """拼出正面、负面提示词：画质词 + 预设的画风 + 用户的内容。"""
    m = MODELS[model]
    pos, neg = m["quality"], m["neg"]
    if preset:
        style = ", ".join(x for x in [artists(preset.get("artists") or [], model), preset.get("style", "")] if x)
        if style:
            pos += style + ", "
        if preset.get("negative"):
            neg += ", " + preset["negative"]
    pos += prompt
    return pos, neg


# ---------------------------------------------------------------- 工作流
def wf_t2i(model, pos, neg, w, h, seed, prefix, lora_list=(), steps=None, cfg=None):
    """lora_list：[(名字, 强度)]，模型和文本编码器都套上（LoraLoader）。steps、cfg 不给就按模型默认。"""
    m = MODELS[model]
    if m["kind"] == "qwen":         # Qwen-Image 2.1：不接参考图的 TextEncodeQwenImage21 + 空 latent（和 ComfyUI 的文生图模板一样）
        load = {"1": {"class_type": "UNETLoader", "inputs": {"unet_name": m["file"], "weight_dtype": "default"}},
                "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": QWEN_TE, "type": "qwen_image", "device": "default"}},
                "3": {"class_type": "VAELoader", "inputs": {"vae_name": QWEN_VAE}}}
        mdl, clip, vae = ["1", 0], ["2", 0], ["3", 0]
        for i, (name, st) in enumerate(lora_list):
            load[str(40 + i)] = {"class_type": "LoraLoader", "inputs": {"model": mdl, "clip": clip, "lora_name": name,
                                                                      "strength_model": st, "strength_clip": st}}
            mdl, clip = [str(40 + i), 0], [str(40 + i), 1]
        return {**load,
                "10": {"class_type": "TextEncodeQwenImage21", "inputs": {"clip": clip, "prompt": pos, "negative_prompt": neg, "resolution": 1024}},
                "12": {"class_type": "EmptyLatentImage", "inputs": {"width": w, "height": h, "batch_size": 1}},
                "13": {"class_type": "KSampler", "inputs": {"model": mdl, "positive": ["10", 0], "negative": ["10", 1], "latent_image": ["12", 0],
                                                            "seed": seed, "steps": steps or m["steps"], "cfg": cfg if cfg is not None else m["cfg"],
                                                            "sampler_name": m["sampler"], "scheduler": m["scheduler"], "denoise": 1.0}},
                "14": {"class_type": "VAEDecode", "inputs": {"samples": ["13", 0], "vae": vae}},
                "15": {"class_type": "SaveImage", "inputs": {"images": ["14", 0], "filename_prefix": prefix}}}
    if m["kind"] == "sdxl":
        load = {"1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": m["file"]}},
                "2": {"class_type": "ModelSamplingDiscrete", "inputs": {"model": ["1", 0], "sampling": "v_prediction", "zsnr": False}}}
        mdl, clip, vae = ["2", 0], ["1", 1], ["1", 2]
    else:
        load = {"1": {"class_type": "UNETLoader", "inputs": {"unet_name": m["file"], "weight_dtype": "default"}},
                "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen_3_06b_base.safetensors", "type": "stable_diffusion", "device": "default"}},
                "3": {"class_type": "VAELoader", "inputs": {"vae_name": "qwen_image_vae.safetensors"}}}
        mdl, clip, vae = ["1", 0], ["2", 0], ["3", 0]
    for i, (name, st) in enumerate(lora_list):
        load[str(40 + i)] = {"class_type": "LoraLoader", "inputs": {"model": mdl, "clip": clip, "lora_name": name,
                                                                  "strength_model": st, "strength_clip": st}}
        mdl, clip = [str(40 + i), 0], [str(40 + i), 1]
    return {**load,
            "10": {"class_type": "CLIPTextEncode", "inputs": {"clip": clip, "text": pos}},
            "11": {"class_type": "CLIPTextEncode", "inputs": {"clip": clip, "text": neg}},
            "12": {"class_type": "EmptyLatentImage", "inputs": {"width": w, "height": h, "batch_size": 1}},
            "13": {"class_type": "KSampler", "inputs": {"model": mdl, "positive": ["10", 0], "negative": ["11", 0], "latent_image": ["12", 0],
                                                        "seed": seed, "steps": steps or m["steps"], "cfg": cfg if cfg is not None else m["cfg"], "sampler_name": m["sampler"],
                                                        "scheduler": m["scheduler"], "denoise": 1.0}},
            "14": {"class_type": "VAEDecode", "inputs": {"samples": ["13", 0], "vae": vae}},
            "15": {"class_type": "SaveImage", "inputs": {"images": ["14", 0], "filename_prefix": prefix}}}


def wf_edit(image_name, instruction, seed, prefix):
    """Qwen-Image 2.1 Turbo 改图：8 步，CFG 1。指令里用 <image1> 指代原图。"""
    if "<image1>" not in instruction:
        instruction = "修改<image1>：" + instruction
    return {"1": {"class_type": "UNETLoader", "inputs": {"unet_name": "qwen_image_2.1_turbo_int8_convrot.safetensors", "weight_dtype": "default"}},
            "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen3vl_8b_int8_convrot.safetensors", "type": "qwen_image", "device": "default"}},
            "3": {"class_type": "VAELoader", "inputs": {"vae_name": "qwen_image_2.1_vae_bf16.safetensors"}},
            "9": {"class_type": "LoadImage", "inputs": {"image": image_name}},
            "4": {"class_type": "TextEncodeQwenImage21", "inputs": {"clip": ["2", 0], "vae": ["3", 0], "images.image_1": ["9", 0],
                                                                  "prompt": instruction, "negative_prompt": "", "resolution": 1024}},
            "6": {"class_type": "KSampler", "inputs": {"model": ["1", 0], "positive": ["4", 0], "negative": ["4", 1], "latent_image": ["4", 2],
                                                       "seed": seed, "steps": 8, "cfg": 1.0, "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0}},
            "7": {"class_type": "VAEDecode", "inputs": {"samples": ["6", 0], "vae": ["3", 0]}},
            "8": {"class_type": "SaveImage", "inputs": {"images": ["7", 0], "filename_prefix": prefix}}}


def wf_upscale(image_name, scale, prefix):
    """RealESRGAN 动漫模型放大 4 倍；要 2 倍时再缩一半（比直接 2 倍模型清楚）。"""
    wf = {"1": {"class_type": "LoadImage", "inputs": {"image": image_name}},
          "2": {"class_type": "UpscaleModelLoader", "inputs": {"model_name": UPSCALER}},
          "3": {"class_type": "ImageUpscaleWithModel", "inputs": {"upscale_model": ["2", 0], "image": ["1", 0]}}}
    last = ["3", 0]
    if scale != 4:
        wf["4"] = {"class_type": "ImageScaleBy", "inputs": {"image": ["3", 0], "upscale_method": "lanczos", "scale_by": scale / 4}}
        last = ["4", 0]
    wf["5"] = {"class_type": "SaveImage", "inputs": {"images": last, "filename_prefix": prefix}}
    return wf


def wf_video(model, prompt, w, h, frames, seed, prefix, image_name=None, lora_list=(), steps=None):
    """H3 文生视频 / 图生视频（给了首帧就是图生视频）。和 scripts\\video_bakeoff.py 的 wf_h3 一样。lora_list：[(名字, 强度)]，只套模型。"""
    m = VIDEO_MODELS[model]
    cond = {"clip": ["2", 0], "vae": ["3", 0], "prompt": prompt, "width": w, "height": h, "length": frames}
    n = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": m["unet"], "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": H3_SHARED[0][1], "type": "minimax", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": H3_SHARED[1][1]}},
        "4": {"class_type": "VAELoader", "inputs": {"vae_name": H3_SHARED[2][1]}},
        "6": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "7": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "res_multistep"}},
    }
    if image_name:
        n["20"] = {"class_type": "LoadImage", "inputs": {"image": image_name}}
        cond["first_frame"] = ["20", 0]
    n["5"] = {"class_type": "MiniMaxH3ImageToVideo", "inputs": cond}
    mdl = ["1", 0]
    for i, (name, st) in enumerate(lora_list):
        n[str(40 + i)] = {"class_type": "LoraLoaderModelOnly", "inputs": {"model": mdl, "lora_name": name, "strength_model": st}}
        mdl = [str(40 + i), 0]
    if m.get("fast"):                     # FastH3：模板里的 sigma 偏移 + 注意力后端 + 视频稀疏注意力（VSA）
        n["31"] = {"class_type": "MiniMaxH3SigmaShift", "inputs": {"model": mdl, "shift_video": 10.0, "shift_audio": 3.0}}
        n["32"] = {"class_type": "ModelAttentionBackend", "inputs": {"model": ["31", 0], "attention": "comfy kitchen attention"}}
        n["33"] = {"class_type": "BlockSparseAttention", "inputs": {"model": ["32", 0], "selection": "vsa", "selection.keep_percent": 10.0,
                                                                    "start_percent": 0.2, "end_percent": 1.0, "dense_blocks": "", "min_tokens": 12288,
                                                                    "extra_tokens": 256, "sink_conditioning": "exact_kv_and_rows", "verbose": False}}
        mdl = ["33", 0]
    n.update({
        "8": {"class_type": "BasicScheduler", "inputs": {"model": mdl, "scheduler": "simple", "steps": steps or m["steps"], "denoise": 1.0}},
        "9": {"class_type": "BasicGuider", "inputs": {"model": mdl, "conditioning": ["5", 0]}},
        "10": {"class_type": "SamplerCustomAdvanced", "inputs": {"noise": ["6", 0], "guider": ["9", 0], "sampler": ["7", 0], "sigmas": ["8", 0],
                                                                 "latent_image": ["5", 1]}},
        "11": {"class_type": "VAEDecode", "inputs": {"samples": ["10", 0], "vae": ["3", 0]}},
        "12": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["10", 0], "vae": ["4", 0]}},
        "13": {"class_type": "CreateVideo", "inputs": {"images": ["11", 0], "audio": ["12", 0], "fps": 24.0}},
        "14": {"class_type": "SaveVideo", "inputs": {"video": ["13", 0], "filename_prefix": prefix, "format": "auto", "codec": "auto"}},
    })
    return n


# ---------------------------------------------------------------- 和 ComfyUI 通信
def _touch():
    llm.LAST_USED[ROLES.get("image", "comfyui_auto")] = time.time()


async def start_comfy():
    _touch()
    await llm.get("/comfyui/", timeout=300)          # 调度器收到请求才启动 ComfyUI


async def upload(data, name):
    _touch()
    r = await llm.post("/comfyui/api/upload/image", files={"image": (name, data, "image/png")}, data={"overwrite": "true"})
    if r.status_code != 200:
        raise RuntimeError(f"图片传给 ComfyUI 失败：{r.text[:200]}")
    return r.json()["name"]


async def run_wf(wf, stop=None, limit=600):
    """提交工作流，等它跑完，返回生成的图片（或视频）字节列表。limit 是最多等几秒。"""
    await start_comfy()
    if stop and stop():
        return []
    r = await llm.post("/comfyui/api/prompt", json={"prompt": wf, "client_id": uuid.uuid4().hex})
    if r.status_code != 200:
        raise RuntimeError(f"ComfyUI 拒绝了这个任务：{r.text[:300]}")
    pid = r.json()["prompt_id"]
    cancel_sent = False
    for _ in range(limit * 2):
        h = (await llm.get(f"/comfyui/api/history/{pid}")).json()
        if pid in h and h[pid].get("status", {}).get("completed") is not None:
            break
        if stop and stop() and not cancel_sent:
            # interrupt 是全局操作，不能用来停止某个 Job。只删除自己的排队项；
            # 已在执行的单张等完成后丢弃，活动表在此期间仍显示忙。
            r = await llm.post("/comfyui/api/queue", json={"delete": [pid]})
            r.raise_for_status()
            cancel_sent = True
        if cancel_sent:
            queue = await llm.get("/comfyui/api/queue")
            queue.raise_for_status()
            active = queue.json()
            if not any(len(item) > 1 and item[1] == pid for key in ("queue_running", "queue_pending") for item in active.get(key, [])):
                return []
        await asyncio.sleep(0.5)
    else:
        raise RuntimeError(f"生成超时（{limit // 60} 分钟）")
    st = h[pid].get("status", {})
    if stop and stop():
        return []
    if st.get("status_str") == "error":
        msg = next((m[1].get("exception_message", "") for m in st.get("messages", []) if m[0] == "execution_error"), "")
        raise RuntimeError("ComfyUI 出错：" + (msg or "未知错误")[:300])
    out = []
    for o in h[pid]["outputs"].values():
        for img in o.get("images", []) + o.get("videos", []):
            data = (await llm.get("/comfyui/api/view", params={"filename": img["filename"], "subfolder": img["subfolder"], "type": "output"})).content
            out.append(data)
    _touch()
    return out


BAD_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def out_dir(folder=None, base=OUT_DIR):
    """生成结果存到哪：沙箱\\图片 下的子文件夹（相对路径，可以多层，不存在就建），不填就是 沙箱\\图片。视频的 base 是 沙箱\\视频。"""
    parts = [x.strip() for x in re.split(r"[\\/]+", (folder or "").strip()) if x.strip()]
    for x in parts:
        if x in (".", "..") or x.startswith(".") or BAD_NAME.search(x):
            raise ValueError(f"文件夹名不能用：{x}")
    d = os.path.join(base, *parts)
    os.makedirs(d, exist_ok=True)
    return d


def folders(base=OUT_DIR):
    """沙箱\\图片 下的所有子文件夹（相对路径），给网页的“存到”下拉用。"""
    out = []
    for cur, dirs, _ in os.walk(base):
        dirs[:] = sorted(x for x in dirs if not x.startswith("."))
        out += [os.path.relpath(os.path.join(cur, x), base) for x in dirs]
    return out


def relocate(src, dst):
    """文件或文件夹从 src 移到 dst 后，把历史记录里的路径跟着改掉（图库靠它显示）。"""
    try:
        lines = open(HISTORY_PATH, encoding="utf-8").read().splitlines()
    except OSError:
        return 0
    s, n, out = os.path.normcase(src), 0, []
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            out.append(line)
            continue
        p = r.get("path", "")
        if os.path.normcase(p) == s or os.path.normcase(p).startswith(s.rstrip(os.sep) + os.sep):
            r["path"] = dst + p[len(src):]
            r["show"] = display(r["path"])
            n += 1
        out.append(json.dumps(r, ensure_ascii=False))
    if n:
        tmp = HISTORY_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")
        os.replace(tmp, HISTORY_PATH)
    return n


def save_image(data, tag="", folder=None, base=OUT_DIR, ext=".png"):
    d = out_dir(folder, base)
    name = dt.datetime.now().strftime("%Y%m%d-%H%M%S") + (f"-{tag}" if tag else "")
    p = os.path.join(d, name + ext)
    k = 2
    while os.path.exists(p):
        p = os.path.join(d, f"{name}-{k}{ext}")
        k += 1
    with open(p, "wb") as f:
        f.write(data)
    return p


def log_history(rec):
    with open(HISTORY_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def history(n=60):
    try:
        lines = open(HISTORY_PATH, encoding="utf-8").read().splitlines()[-n * 2:]
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            rec = json.loads(line)
            if isinstance(rec, dict):
                out.append(rec)
        except ValueError:
            continue
    return [r for r in out if os.path.exists(r.get("path", ""))][-n:][::-1]


# ---------------------------------------------------------------- 对外：生成、改图、放大
async def generate(prompt, model=None, size="portrait", count=1, seed=None, preset_id=None, emit=None, stop=None, folder=None,
                   lora=None, steps=None, cfg=None):
    """返回 [(路径, 种子)]。prompt 有中文时先让 9B 转成标签。lora：[{name, strength}]；steps、cfg 空着就按模型默认。"""
    preset = next((p for p in load_presets() if p["id"] == preset_id), None) if preset_id else None
    model = model or (preset or {}).get("model") or "noobai"
    out_dir(folder)          # 文件夹名不对的话，开始前就报错
    if model not in MODELS:
        raise ValueError(f"没有这个模型：{model}")
    ll = pick_loras(lora, MODELS[model]["kind"])
    steps, cfg = _num(steps, 1, 100), _num(cfg, 0.0, 30.0, float)
    raw = prompt.strip()
    if not raw:
        raise ValueError("提示词是空的")
    await check_prompt(raw)
    tags = raw
    if has_chinese(raw) and MODELS[model]["kind"] != "qwen":     # Qwen-Image 直接读中文，不转标签
        if emit:
            emit({"type": "status", "text": "正在把描述转成标签（9B）"})
        tags = await to_tags(raw, MODELS[model]["kind"])
        await check_prompt(tags)
        if emit:
            emit({"type": "tags", "text": tags})
    pos, neg = compose(tags, model, preset)
    w, h = parse_size(size, SIZES, "portrait", 512, 2048, 64)
    base = seed if isinstance(seed, int) and seed >= 0 else random.randint(1, 2**31 - 1)
    if emit:
        emit({"type": "start", "n": count, "model": MODELS[model]["name"] + lora_note(ll), "positive": pos, "negative": neg, "size": f"{w}×{h}"})
    out = []
    for i in range(count):
        if stop and stop():
            break
        t0 = time.time()
        s = base + i
        imgs = await run_wf(wf_t2i(model, pos, neg, w, h, s, "hub/t2i", ll, steps, cfg), stop)
        if not imgs:
            break
        p = save_image(imgs[0], str(s), folder)
        rec = {"path": p, "show": display(p), "kind": "t2i", "model": model, "prompt": raw, "tags": tags, "positive": pos, "negative": neg,
               "seed": s, "size": f"{w}×{h}", "sizekey": size, "preset": preset["name"] if preset else "", "secs": round(time.time() - t0, 1), "time": time.time(),
               **lora_rec(ll, steps, cfg)}
        log_history(rec)
        out.append((p, s))
        if emit:
            emit({"type": "image", "i": i, **rec})
    return out


async def edit(data, instruction, seed=None, emit=None, stop=None, folder=None):
    out_dir(folder)
    await check_edit(data, instruction)
    t0 = time.time()
    name = await upload(data, f"hub_edit_{uuid.uuid4().hex[:8]}.png")
    s = seed if isinstance(seed, int) and seed >= 0 else random.randint(1, 2**31 - 1)
    if emit:
        emit({"type": "start", "n": 1, "model": "Qwen-Image 2.1 Turbo"})
    imgs = await run_wf(wf_edit(name, instruction, s, "hub/edit"), stop)
    if not imgs and stop and stop():
        return None
    if not imgs:
        raise RuntimeError("改图任务没有返回图片")
    p = save_image(imgs[0], "改图", folder)
    rec = {"path": p, "show": display(p), "kind": "edit", "model": "qwen-image", "prompt": instruction, "seed": s,
           "secs": round(time.time() - t0, 1), "time": time.time()}
    log_history(rec)
    if emit:
        emit({"type": "image", "i": 0, **rec})
    return p


async def upscale(data, scale=2, emit=None, stop=None, folder=None):
    out_dir(folder)
    t0 = time.time()
    name = await upload(data, f"hub_up_{uuid.uuid4().hex[:8]}.png")
    if emit:
        emit({"type": "start", "n": 1, "model": "RealESRGAN 动漫"})
    imgs = await run_wf(wf_upscale(name, scale, "hub/up"), stop)
    if not imgs and stop and stop():
        return None
    if not imgs:
        raise RuntimeError("放大任务没有返回图片")
    p = save_image(imgs[0], f"放大{scale}x", folder)
    rec = {"path": p, "show": display(p), "kind": "upscale", "model": "realesrgan", "prompt": f"放大 {scale} 倍",
           "secs": round(time.time() - t0, 1), "time": time.time()}
    log_history(rec)
    if emit:
        emit({"type": "image", "i": 0, **rec})
    return p


def lora_note(ll):
    """状态行里显示用：“ · LoRA：甲 0.8、乙”。"""
    if not ll:
        return ""
    t = {x["name"]: x["title"] for x in loras()}
    return " · LoRA：" + "、".join(t.get(n, n) + ("" if s == 1 else f" {s:g}") for n, s in ll)


def lora_rec(ll, steps, cfg=None):
    """历史记录里存的 LoRA、步数、CFG（“同参数”要用）。没用的不写。"""
    out = {}
    if ll:
        out["loras"] = [{"name": n, "strength": s} for n, s in ll]
    if steps:
        out["steps"] = steps
    if cfg is not None:
        out["cfg"] = cfg
    return out


async def video(prompt, model=None, size="landscape", secs=5, seed=None, data=None, emit=None, stop=None, folder=None, lora=None, steps=None):
    """H3 生成一段带声音的视频，给了 data（首帧图）就是图生视频。提示词直接用自然语言（中文也行），不转标签。返回路径。
    lora：[{name, strength}]；steps 空着就按模型默认，选了已知的加速 LoRA 就按它的步数。"""
    model = model or "h3"
    if model in LEGACY_VIDEO:              # 旧的“H3 加速”：H3 + 加速 LoRA
        model, extra = LEGACY_VIDEO[model]
        if not any(isinstance(x, dict) and x.get("name") == extra for x in (lora or [])):
            lora = [{"name": extra, "strength": 1.0}] + list(lora or [])
    if model not in VIDEO_MODELS:
        raise ValueError(f"没有这个视频模型：{model}")
    ll = pick_loras(lora, VIDEO_MODELS[model]["arch"])
    steps = _num(steps, 1, 100)
    known = [KNOWN_LORAS[n] for n, _ in ll if n in KNOWN_LORAS and KNOWN_LORAS[n].get("steps")]
    out_dir(folder, VIDEO_DIR)
    raw = prompt.strip()
    if not raw:
        raise ValueError("提示词是空的")
    await check_prompt(raw)
    secs = max(1, min(int(secs or 5), 20))
    w, h = parse_size(size, VIDEO_SIZES, "landscape", 256, 1280, 32)
    s = seed if isinstance(seed, int) and seed >= 0 else random.randint(1, 2**31 - 1)
    m = VIDEO_MODELS[model]
    t0 = time.time()
    name = None
    if data:
        await check_edit(data, raw)
        name = await upload(data, f"hub_vid_{uuid.uuid4().hex[:8]}.png")
    use = steps or min([k["steps"] for k in known] or [m["steps"]])
    # 预计用时：选了加速 LoRA、步数没改，用它的实测；否则按步数比例估
    base = known[0]["secs"] if known and not steps and model == "h3" else m["secs"] * use / m["steps"]
    est = round(base * (secs / 5) ** 1.32 * (w * h) / (832 * 576))      # 10 秒实测约 2.5 倍
    if emit:
        emit({"type": "start", "n": 1, "model": f"{m['name']}{lora_note(ll)} · {use} 步", "size": f"{w}×{h} · {secs} 秒"})
        emit({"type": "status", "text": f"生成中，约 {est} 秒"})
    vids = await run_wf(wf_video(model, raw, w, h, video_frames(secs), s, "hub/video", name, ll, use), stop, limit=3600)
    if not vids and stop and stop():
        return None
    if not vids:
        raise RuntimeError("视频任务没有返回文件")
    p = save_image(vids[0], str(s), folder, VIDEO_DIR, ".mp4")
    rec = {"path": p, "show": display(p), "kind": "video", "model": model, "prompt": raw, "seed": s, "size": f"{w}×{h}",
           "sizekey": size, "length": secs, "first_frame": bool(data), "secs": round(time.time() - t0, 1), "time": time.time(),
           **lora_rec(ll, steps)}
    log_history(rec)
    if emit:
        emit({"type": "image", "i": 0, **rec})
    return p


def start_job(body, data=None):
    kind = body.get("kind") or "t2i"

    async def go(job):
        kw = dict(emit=job.emit, stop=lambda: job.stopped, folder=body.get("folder") or None)
        if kind == "edit":
            await edit(data, body.get("instruction") or "", body.get("seed"), **kw)
        elif kind == "upscale":
            await upscale(data, int(body.get("scale") or 2), **kw)
        elif kind == "video":
            await video(body.get("prompt") or "", body.get("model"), body.get("size") or "landscape", int(body.get("secs") or 5),
                        body.get("seed"), data, lora=body.get("loras"), steps=body.get("steps"), **kw)
        else:
            await generate(body.get("prompt") or "", body.get("model"), body.get("size") or "portrait",
                           max(1, min(int(body.get("count") or 1), 16)), body.get("seed"), body.get("preset"),
                           lora=body.get("loras"), steps=body.get("steps"), cfg=body.get("cfg"), **kw)
        job.emit({"type": "done", "secs": round(time.time() - job.t0, 1), "stopped": job.stopped})
    return Job("image").run(go)

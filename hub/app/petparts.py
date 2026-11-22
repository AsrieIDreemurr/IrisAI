"""桌宠按部件拼帧（2026-11-09）：服务端给网页出图（/api/pet/frame、/api/pet/compose），桌面桌宠（scripts/desktop_pet.py）也直接用。

部件和清单由 scripts/export_pet_parts.py 从 PSD 导出到 素材/桌宠部件/。配方 = {类: 部件 id 或 [部件 id...]}，
类见清单的 cats（body 姿势、eye 眼睛、mouth 嘴、brow 眉毛 单选；eyeopt、mouthopt、cheek、opt 多选）。
用户改的配方存在设置的 pet_frames[主题][帧名]：改的是现有的帧就盖掉它，新名字就是自己建的表情。
眨眼、对口型、睡觉第二帧跟着“底帧”走（DERIVE）：只改了平常的脸时，眨眼和说话的帧也换成新的脸，只是眼睛 / 嘴照原来的变。
"""
import io
import json
import os

from PIL import Image, ImageFilter

DERIVE = {"blink1": "idle", "blink2": "idle", "talk1": "idle", "talk2": "idle", "sleep2": "sleep1"}
CATS = ("body", "eye", "eyeopt", "mouth", "mouthopt", "brow", "cheek", "opt")
_man, _imgs = {}, {}


def parts_dir(skin_dir):
    return os.path.join(skin_dir, "素材", "桌宠部件")


def manifest(skin_dir):
    """部件清单（文件改了自动重读）；主题没有部件时返回 None。"""
    p = os.path.join(parts_dir(skin_dir), "部件.json")
    try:
        mt = os.path.getmtime(p)
    except OSError:
        return None
    hit = _man.get(p)
    if not hit or hit[0] != mt:
        with open(p, encoding="utf-8") as f:
            hit = (mt, json.load(f))
        _man[p] = hit
    return hit[1]


def clean(man, r):
    """只留清单里有的类和部件：单选的类要一个存在的部件，多选的类是列表。"""
    out = {}
    if not isinstance(r, dict):
        return out
    for c in CATS:
        cat = man["cats"].get(c)
        if not cat:
            continue
        ids = {o["id"] for o in cat["options"]}
        v = r.get(c)
        if cat["multi"]:
            out[c] = [x for x in (v if isinstance(v, list) else []) if x in ids]
        elif isinstance(v, str) and v in ids:
            out[c] = v
    return out


def recipe(man, custom, name):
    """这一帧的配方：用户改过的 > 跟着改过的底帧（眨眼、说话） > 原来的。没有这一帧返回 None。"""
    custom = custom or {}
    pre = man["presets"]
    if name in custom:
        return {**pre.get(name, {}), **clean(man, custom[name])} if name in pre else clean(man, custom[name])
    base = DERIVE.get(name)
    if base and base in custom and name in pre and base in pre:
        r = {**pre[base], **clean(man, custom[base])}
        for c in CATS:                              # 原来这一帧和底帧不一样的地方（眨眼的眼睛、说话的嘴）照原来的
            if pre[name].get(c) != pre[base].get(c):
                r[c] = pre[name].get(c)
        return r
    return pre.get(name)


def changed(man, custom):
    """哪些帧要用拼出来的图（改过的、自己建的、跟着改过的底帧走的）。"""
    custom = custom or {}
    names = set(custom)
    names |= {n for n, b in DERIVE.items() if b in custom and n in man["presets"]}
    return sorted(names)


def _part(skin_dir, f):
    p = os.path.join(parts_dir(skin_dir), f)
    if p not in _imgs:
        _imgs[p] = Image.open(p).convert("RGBA")
    return _imgs[p]


def outline(im, r):
    """墨线描边（和 export_satori.py 的 outline 同样的做法，半径按 512 尺寸）。"""
    a = im.getchannel("A")
    grown = a.filter(ImageFilter.GaussianBlur(r * .55)).point(lambda v: 255 if v > 10 else v * 25)
    grown = grown.filter(ImageFilter.GaussianBlur(max(.8 * r / 9, .3)))     # 原图上是 0.8（半径 9），按比例缩
    ink = Image.new("RGBA", im.size, (17, 17, 17, 0))
    ink.putalpha(grown)
    ink.alpha_composite(im)
    return ink


def compose(skin_dir, r, man=None):
    """按配方叠出 512×512 的帧（RGBA）。"""
    man = man or manifest(skin_dir)
    ids = list(man["fixed"])
    for c in CATS:
        v = (r or {}).get(c)
        ids += v if isinstance(v, list) else [v] if v else []
    if (r or {}).get("body"):
        ids += man["links"].get(r["body"], [])
    P = man["parts"]
    im = Image.new("RGBA", (man["size"], man["size"]))
    for pid in sorted({i for i in ids if i in P}, key=lambda i: P[i]["z"]):
        p = P[pid]
        im.alpha_composite(_part(skin_dir, p["file"]), (p["x"], p["y"]))
    if man.get("outline"):
        im = outline(im, man["outline"])
    return im


def png(im):
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()

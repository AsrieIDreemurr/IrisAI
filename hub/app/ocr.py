"""识字：PaddleOCR-VL 识别图片里的文字；对图片提问交给能看图的对话模型。网页的识字模块和 Agent 的 ocr_image 工具共用。"""
import base64
import datetime as dt
import os
import re
import time

from . import llm, fileops
from .config import CFG, SANDBOX

OUT_DIR = os.path.join(SANDBOX, "识字")
# PaddleOCR-VL 官方的四种任务提示
PROMPTS = {"text": "OCR:", "table": "Table Recognition:", "formula": "Formula Recognition:", "chart": "Chart Recognition:"}


def _mime(data):
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:3] == b"GIF":
        return "image/gif"
    if data[:2] == b"BM":
        return "image/bmp"
    raise ValueError("不是能识别的图片格式（支持 PNG、JPG、WebP、GIF、BMP）")


MAX_BYTES = 40 * 2 ** 20       # 一张图最多 40 MB
MAX_PIXELS = 2_800_000         # 和 static/js/ocr.js 一样：PaddleOCR-VL 一张图最多按约 280 万像素处理，再大先缩小


def prepare(data):
    """进模型前统一检查（网页贴的图和沙箱里已有的图片文件都走这里；以前已有文件原样送进去，没有大小限制，后续改进建议 I14）：
    格式、大小、能不能打开；超过 280 万像素就按比例缩小成 PNG。返回要送进模型的字节。"""
    if not data:
        raise ValueError("图片是空的")
    if len(data) > MAX_BYTES:
        raise ValueError(f"图片太大（{len(data) / 2 ** 20:.0f} MB），最大 {MAX_BYTES // 2 ** 20} MB")
    _mime(data)
    import io
    import math
    from PIL import Image
    try:
        im = Image.open(io.BytesIO(data))
        w, h = im.size
    except Image.DecompressionBombError:
        raise ValueError("图片像素太多，打不开")
    except Exception:
        raise ValueError("图片打不开，可能已经损坏")
    if w * h <= MAX_PIXELS:
        return data
    k = math.sqrt(MAX_PIXELS / (w * h))
    im = im.convert("RGBA" if im.mode in ("RGBA", "LA", "P") else "RGB").resize((max(1, round(w * k)), max(1, round(h * k))), Image.LANCZOS)
    out = io.BytesIO()
    im.save(out, "PNG")
    return out.getvalue()


def data_url(data):
    return f"data:{_mime(data)};base64," + base64.b64encode(data).decode()


def from_data_url(url):
    m = re.match(r"data:image/[\w.+-]+;base64,(.*)$", url or "", re.S)
    if not m:
        raise ValueError("图片数据不对")
    return base64.b64decode(m.group(1))


def save(data):
    """网页贴进来的图存到 沙箱\\识字，文件管理器里能看到，Agent 也能用。"""
    os.makedirs(OUT_DIR, exist_ok=True)
    ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif", "image/bmp": ".bmp"}[_mime(data)]
    p = os.path.join(OUT_DIR, fileops.stamp() + ext)
    with open(p, "xb") as f:
        f.write(data)
    return p


def _clean(text):
    # 偶尔会包一层 ``` 代码块
    text = re.sub(r"^```\w*\n|\n```$", "", text.strip())
    return text.strip()


async def recognize(data, mode="text"):
    t0 = time.time()
    msgs = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": data_url(data)}},
                                         {"type": "text", "text": PROMPTS.get(mode, "OCR:")}]}]
    out, _ = await llm.complete(CFG["models"]["ocr"], msgs, max_tokens=4096, temperature=0)
    return _clean(out), round(time.time() - t0, 1)


def ask_model():
    # 固定用 3.6：盲测里看图题它赢，而且比 27B 快 5 倍以上。不跟对话的默认模型走
    return CFG["models"]["chat"]


async def ask(data, question, ocr_text=""):
    """对图片提问，逐块产出文字。"""
    text = question.strip()
    if ocr_text.strip():
        text = f"图里的文字（OCR 结果，可能有个别错字）：\n{ocr_text.strip()[:4000]}\n\n问题：{text}"
    msgs = [{"role": "system", "content": "用中文回答。回答直接、具体，不要复述问题。"},
            {"role": "user", "content": [{"type": "image_url", "image_url": {"url": data_url(data)}}, {"type": "text", "text": text}]}]
    async for kind, val in llm.stream_chat(ask_model(), msgs, think=False, max_tokens=2048,
                                           temperature=0.7, top_p=0.8, top_k=20, min_p=0):
        if kind == "content":
            yield val

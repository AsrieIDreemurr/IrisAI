"""调用 llama-swap：流式对话（带工具调用）、普通请求、ComfyUI 转发。"""
import asyncio
import json
import time
from urllib.parse import quote

import httpx

from .config import API_KEY, CFG, M
from . import activity

BASE = CFG["llama"]
HEADERS = {"Authorization": "Bearer " + API_KEY}
# 记录每个模型最近一次被调用的时间，用来估算多久后自动卸载
LAST_USED = {}

_client = httpx.AsyncClient(base_url=BASE, headers=HEADERS, timeout=httpx.Timeout(1800, connect=10))


async def _http_error(model, started, status, text):
    """只补充同一调度器、同一模型、本次请求之后的启动拒绝；无记录时保留原错误。"""
    fallback = f"模型返回错误 {status}：{text[:500]}"
    if (status < 500 or "upstream command exited prematurely" not in text or
            not M.get(model, {}).get("startup_check")):        # 启动器会自己检查并写拒绝原因的模型（模型清单）
        return fallback
    try:
        async with asyncio.timeout(2):
            async with _client.stream("GET", "/logs/stream/" + quote(model, safe="")) as logs:
                if logs.status_code != 200:
                    return fallback
                size = 0
                async for line in logs.aiter_lines():
                    size += len(line)
                    if size > 65536:
                        break
                    _, marker, payload = line.partition("HUB_STARTUP_ERROR ")
                    if not marker:
                        continue
                    try:
                        event = json.loads(payload)
                        when = float(event["time"])
                        reason = event["reason"]
                        if (event["model"] == model and started <= when <= time.time() + 1 and
                                isinstance(reason, str) and reason):
                            return f"模型启动失败：{reason[:500]}"
                    except (ValueError, TypeError, KeyError):
                        continue
    except (httpx.HTTPError, TimeoutError):
        pass
    return fallback


@activity.streaming("模型生成")
async def stream_chat(model, messages, tools=None, think=False, max_tokens=8192, **params):
    """逐块产出 ("content", 文本) / ("think", 文本)；最后产出 ("done", {message, finish, usage, timings})。"""
    body = {"model": model, "messages": messages, "stream": True, "max_tokens": max_tokens,
            "chat_template_kwargs": {"enable_thinking": bool(think)}, **params}
    if tools:
        body["tools"] = tools
    started = LAST_USED[model] = time.time()
    content, reasoning, calls, finish, usage, timings = [], [], {}, None, {}, {}
    ended = False
    async with _client.stream("POST", "/v1/chat/completions", json=body) as r:
        if r.status_code != 200:
            text = (await r.aread()).decode("utf-8", "replace")[:500]
            raise RuntimeError(await _http_error(model, started, r.status_code, text))
        async for line in r.aiter_lines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                ended = True
                break
            d = json.loads(data)
            if d.get("error"):        # 生成到一半出错（例如工具参数无法解析），服务端在流里发一条 error
                err = d["error"]
                raise RuntimeError(f"模型返回错误：{err.get('message', err) if isinstance(err, dict) else err}"[:500])
            if d.get("usage"):
                usage = d["usage"]
            if d.get("timings"):
                timings = d["timings"]
            if not d.get("choices"):
                continue
            ch = d["choices"][0]
            delta = ch.get("delta") or {}
            if delta.get("reasoning_content"):
                reasoning.append(delta["reasoning_content"])
                yield "think", delta["reasoning_content"]
            if delta.get("content"):
                content.append(delta["content"])
                yield "content", delta["content"]
            for tc in delta.get("tool_calls") or []:
                slot = calls.setdefault(tc.get("index", 0), {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                slot["function"]["name"] += fn.get("name") or ""
                slot["function"]["arguments"] += fn.get("arguments") or ""
                yield "tool", slot
            if ch.get("finish_reason"):
                finish = ch["finish_reason"]
    if not ended and finish is None:
        raise RuntimeError("模型响应提前结束，未收到完成标记；本次输出可能不完整，请重试。")
    LAST_USED[model] = time.time()
    msg = {"role": "assistant", "content": "".join(content)}
    if calls:
        msg["tool_calls"] = [calls[k] for k in sorted(calls)]
        for i, c in enumerate(msg["tool_calls"]):
            c["id"] = c["id"] or f"call_{int(time.time() * 1000)}_{i}"
    yield "done", {"message": msg, "reasoning": "".join(reasoning), "finish": finish, "usage": usage, "timings": timings}


@activity.tracked("模型请求")
async def complete(model, messages, max_tokens=4096, **params):
    """一次性请求，返回文本。翻译、取标题这类小任务用。"""
    body = {"model": model, "messages": messages, "max_tokens": max_tokens, **params}
    started = LAST_USED[model] = time.time()
    r = await _client.post("/v1/chat/completions", json=body)
    LAST_USED[model] = time.time()
    if r.status_code != 200:
        raise RuntimeError(await _http_error(model, started, r.status_code, r.text[:500]))
    d = r.json()
    return (d["choices"][0]["message"].get("content") or "").strip(), d.get("timings", {})


async def get(path, **kw):
    return await _client.get(path, **kw)


async def post(path, **kw):
    return await _client.post(path, **kw)

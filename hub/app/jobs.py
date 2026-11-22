"""后台任务：翻译、生图这类要跑一阵的事。事件先存起来，网页订阅；刷新页面后从头重放。"""
import asyncio
import json
import time
import uuid

JOBS = {}


class Job:
    def __init__(self, kind=""):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.events, self.done, self.stopped, self.task = [], False, False, None
        self.cond = asyncio.Condition()
        self.t0 = time.time()
        JOBS[self.id] = self
        for k in [k for k, j in JOBS.items() if j.done and time.time() - j.t0 > 3600]:
            JOBS.pop(k, None)

    def emit(self, ev):
        self.events.append(ev)

        async def notify():
            async with self.cond:
                self.cond.notify_all()
        asyncio.get_running_loop().create_task(notify())

    def run(self, coro_fn):
        """coro_fn(job) 正常结束时自己发 done；出错发 error。"""
        async def go():
            try:
                await coro_fn(self)
            except Exception as e:
                self.emit({"type": "error", "message": str(e)[:500] or type(e).__name__})
            finally:
                self.done = True
                async with self.cond:
                    self.cond.notify_all()
        self.task = asyncio.get_running_loop().create_task(go())
        return self


async def sse(job, start=0):
    i = max(0, start)
    while True:
        while i < len(job.events):
            yield f"id: {i}\ndata: {json.dumps(job.events[i], ensure_ascii=False)}\n\n"
            i += 1
        if job.done:
            return
        async with job.cond:
            try:
                await asyncio.wait_for(job.cond.wait(), 15)
            except asyncio.TimeoutError:
                yield ": ping\n\n"


def cursor(request, start=0):
    try:
        return max(0, start, int(request.headers.get("last-event-id", "-1")) + 1)
    except ValueError:
        return max(0, start)

"""供空闲检查使用的内存活动表；只记录任务类别，不记录输入内容。"""
import contextlib
import functools
import time
import uuid

ACTIVE = {}


@contextlib.contextmanager
def work(kind, label=""):
    ident = uuid.uuid4().hex
    ACTIVE[ident] = {"kind": kind, "label": label or kind, "started": time.time()}
    try:
        yield
    finally:
        ACTIVE.pop(ident, None)


def tracked(kind):
    def decorate(fn):
        @functools.wraps(fn)
        async def wrapped(*args, **kwargs):
            with work(kind):
                return await fn(*args, **kwargs)
        return wrapped
    return decorate


def streaming(kind):
    def decorate(fn):
        @functools.wraps(fn)
        async def wrapped(*args, **kwargs):
            with work(kind):
                async with contextlib.aclosing(fn(*args, **kwargs)) as stream:
                    async for value in stream:
                        yield value
        return wrapped
    return decorate

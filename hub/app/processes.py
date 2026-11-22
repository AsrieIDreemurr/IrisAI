"""短任务的进程树生命周期；取消和超时都等待回收。"""
import asyncio
import os
import psutil


async def finish(proc, job=None):
    if job is not None:
        job.close()
    else:
        try:
            parent = psutil.Process(proc.pid)
            children = parent.children(recursive=True)
            for p in reversed(children):
                try:
                    p.kill()
                except psutil.Error:
                    pass
            if proc.returncode is None:
                proc.kill()
        except (psutil.Error, ProcessLookupError):
            pass
    await proc.communicate()


async def communicate(proc, timeout):
    # Windows 作业对象还会在 Hub 意外退出时释放子进程。
    job = None
    try:
        if os.name == "nt":
            from .winjob import Job
            job = Job(proc.pid)
        return await asyncio.wait_for(proc.communicate(), timeout)
    finally:
        cleanup = asyncio.create_task(finish(proc, job))
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
            raise

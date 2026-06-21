"""任务生命周期与 SSE 进度推送。

从 main.py 抽离：任务表 / 进度队列 / 后台任务强引用 / 终态延时清理，
以及对外暴露的 create / progress / cancel 三个操作。路由层（main.py）只做
参数校验后转调这里。
"""
from __future__ import annotations

import asyncio
import json

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from . import pipeline
from .pipeline import TaskState

# 全局任务表：task_id -> TaskState（含一个进度队列供 SSE 订阅）
_tasks: dict[str, TaskState] = {}
_queues: dict[str, asyncio.Queue] = {}
# 后台任务引用集合：防止「发射后不管」的任务被 GC 回收导致中途消失
_background_tasks: set[asyncio.Task] = set()
# 任务终态后保留时长（秒），供 SSE 重连查看结果，之后清理释放内存
_TASK_RETAIN_SECONDS = 300


def _put(task_id: str, state: TaskState) -> None:
    """把当前状态推入对应 SSE 队列。"""
    q = _queues.get(task_id)
    if q is not None:
        q.put_nowait({
            "stage": state.stage,
            "percent": state.percent,
            "message": state.message,
            "error": state.error,
            "done": state.stage in ("done", "error"),
            "video_id": state.video_id,
            "title": state.title,
            "uploader": state.uploader,
        })


async def create(mode: str, url: str, voice: str = "") -> dict:
    """创建并启动一个后台处理任务，返回 {"task_id": ...}。

    mode / url 已由路由层校验；voice 仅 TTS 模式生效。
    """
    state = TaskState()
    _tasks[state.task_id] = state
    _queues[state.task_id] = asyncio.Queue()
    # 捕获当前 event loop：yt-dlp 下载进度 hook 在 worker 线程触发，
    # 需用 call_soon_threadsafe 把队列写入调度回 loop 线程，避免跨线程操作 asyncio.Queue
    loop = asyncio.get_running_loop()

    def progress(stage: str, percent: int, message: str) -> None:
        state.stage = stage
        state.percent = percent
        state.message = message
        loop.call_soon_threadsafe(_put, state.task_id, state)

    # 后台运行任务（voice 仅 TTS 模式生效）
    task = asyncio.create_task(pipeline.run(mode, url, state, progress, voice=voice))
    state.task = task  # 存引用供 /api/cancel 取消
    # 持有强引用防止 GC；任务结束延时清理任务表与队列
    _background_tasks.add(task)

    def _on_done(t: asyncio.Task) -> None:
        _background_tasks.discard(t)
        # 终态后延时清理，留窗口给 SSE 重连查看结果
        async def _cleanup():
            await asyncio.sleep(_TASK_RETAIN_SECONDS)
            _tasks.pop(state.task_id, None)
            _queues.pop(state.task_id, None)
        cleanup_task = asyncio.create_task(_cleanup())
        # 清理任务也纳入强引用集合，防止 fire-and-forget 被 GC 中途取消
        _background_tasks.add(cleanup_task)
        cleanup_task.add_done_callback(lambda ct: _background_tasks.discard(ct))

    task.add_done_callback(_on_done)

    return {"task_id": state.task_id}


async def cancel(task_id: str) -> dict:
    """取消正在运行的任务。"""
    state = _tasks.get(task_id)
    if not state:
        raise HTTPException(404, "任务不存在")
    task = state.task
    if task is None or task.done():
        return {"ok": True, "already_done": True}
    task.cancel()
    return {"ok": True, "cancelled": True}


def progress_stream(task_id: str) -> StreamingResponse:
    """构造 SSE 响应：实时推送任务进度，结束后发送最终状态并关闭。"""
    if task_id not in _tasks:
        raise HTTPException(404, "任务不存在")

    queue = _queues.setdefault(task_id, asyncio.Queue())
    state = _tasks[task_id]

    async def event_generator():
        # 先补发当前状态
        yield f"data: {json.dumps({'stage': state.stage, 'percent': state.percent, 'message': state.message, 'error': state.error, 'done': state.stage in ('done', 'error'), 'video_id': state.video_id, 'title': state.title, 'uploader': state.uploader})}\n\n"
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    # 心跳保活
                    yield ": keep-alive\n\n"
                    continue
                yield f"data: {json.dumps(msg)}\n\n"
                if msg.get("done"):
                    # 发送最终结果摘要
                    if state.result:
                        final = {
                            "done": True,
                            "stage": state.stage,
                            "error": state.error,
                            "result": {
                                "title": state.result.title,
                                "uploader": state.result.uploader,
                                "mode": state.result.mode,
                                "audio_url": state.result.audio_url,
                                "audio_name": state.result.audio_path.name,
                            },
                        }
                        yield f"data: {json.dumps(final)}\n\n"
                    break
        finally:
            _queues.pop(task_id, None)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )

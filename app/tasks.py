"""任务生命周期与 SSE 进度推送。

从 main.py 抽离:任务表 / 进度队列 / 后台任务强引用 / 终态延时清理,
以及对外暴露的 create / progress / cancel 三个操作。路由层(main.py)只做
参数校验后转调这里。

任务状态额外持久化到 data/tasks.json:进程重启后 SSE 重连能拿到终态;
进行中的任务在重启时标记为 error(无法恢复执行态)。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from . import config, pipeline
from .pipeline import TaskState

logger = logging.getLogger(__name__)

# 全局任务表:task_id -> TaskState(含一个进度队列供 SSE 订阅)
_tasks: dict[str, TaskState] = {}
_queues: dict[str, asyncio.Queue] = {}
# 后台任务引用集合:防止「发射后不管」的任务被 GC 回收导致中途消失
_background_tasks: set[asyncio.Task] = set()
# 任务终态后保留时长(秒),供 SSE 重连查看结果,之后清理释放内存
_TASK_RETAIN_SECONDS = 300
# 持久化节流间隔(秒):避免高频进度更新打满磁盘 IO
_PERSIST_INTERVAL = 1.0
_last_persist = 0.0


def _put(task_id: str, state: TaskState) -> None:
    """把当前状态推入对应 SSE 队列,并节流持久化到 tasks.json。"""
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
    _persist_throttled(task_id, state)


def _state_snapshot(state: TaskState) -> dict:
    """提取可持久化的状态摘要(不含 asyncio 对象)。"""
    return {
        "stage": state.stage,
        "percent": state.percent,
        "message": state.message,
        "error": state.error,
        "video_id": state.video_id,
        "title": state.title,
        "uploader": state.uploader,
        # 任务参数(供断点重试读取)
        "url": state.url,
        "mode": state.mode,
        "voice": state.voice,
        "updated_at": time.time(),
    }


def _persist_throttled(task_id: str, state: TaskState) -> None:
    """节流写回:距上次写入超过 _PERSIST_INTERVAL 才落盘。

    终态(done/error)立即写入,确保重启后能查到最终结果。
    """
    global _last_persist
    now = time.time()
    is_terminal = state.stage in ("done", "error")
    if not is_terminal and now - _last_persist < _PERSIST_INTERVAL:
        return
    _last_persist = now
    data = _load_persisted()
    data[task_id] = _state_snapshot(state)
    _save_persisted(data)


def _load_persisted() -> dict:
    """读取持久化的任务状态字典;文件不存在或损坏返回空字典。"""
    if not config.TASKS_FILE.exists():
        return {}
    try:
        data = json.loads(config.TASKS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("读取任务状态文件失败: %s", e)
        return {}


def _save_persisted(data: dict) -> None:
    """原子写入任务状态字典。"""
    config.DATA_DIR.mkdir(exist_ok=True)
    tmp = config.DATA_DIR / ".tasks.json.tmp"
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(config.TASKS_FILE)
    except OSError as e:
        logger.warning("写入任务状态文件失败: %s", e)


def _remove_persisted(task_id: str) -> None:
    """从持久化文件移除一条任务。"""
    data = _load_persisted()
    if task_id in data:
        del data[task_id]
        _save_persisted(data)


def init() -> None:
    """启动时恢复:把进行中的持久化任务标记为 error,保留供 SSE 查看。

    进行中的任务无法恢复执行态(协程已随进程退出),标记为中断错误,
    前端 SSE 重连时能拿到明确终态而非无限等待。终态任务保留 _TASK_RETAIN_SECONDS
    后由 _on_done 清理;重启时若已超时则直接清除。
    """
    data = _load_persisted()
    if not data:
        return
    now = time.time()
    survivors: dict[str, dict] = {}
    interrupted = 0
    for task_id, snap in data.items():
        if not isinstance(snap, dict):
            continue
        stage = snap.get("stage", "")
        # 非终态任务标记为中断
        if stage not in ("done", "error"):
            snap["stage"] = "error"
            snap["error"] = "服务重启,任务中断"
            snap["message"] = "服务重启,任务中断"
            snap["done"] = True
            interrupted += 1
        # 超过保留时长的终态任务清除
        updated = snap.get("updated_at", 0)
        if now - updated > _TASK_RETAIN_SECONDS and stage in ("done", "error"):
            continue
        survivors[task_id] = snap
    _save_persisted(survivors)
    if interrupted:
        logger.info("任务状态恢复:%d 个进行中任务标记为中断", interrupted)


async def create(mode: str, url: str, voice: str = "", resume: bool = False) -> dict:
    """创建并启动一个后台处理任务,返回 {"task_id": ...}。

    mode / url 已由路由层校验;voice 仅 TTS 模式生效。
    resume=True 时从资产包 progress.json 记录的断点继续。
    """
    state = TaskState()
    state.url = url
    state.mode = mode
    state.voice = voice
    _tasks[state.task_id] = state
    _queues[state.task_id] = asyncio.Queue()
    # 捕获当前 event loop:yt-dlp 下载进度 hook 在 worker 线程触发,
    # 需用 call_soon_threadsafe 把队列写入调度回 loop 线程,避免跨线程操作 asyncio.Queue
    loop = asyncio.get_running_loop()

    def progress(stage: str, percent: int, message: str) -> None:
        state.stage = stage
        state.percent = percent
        state.message = message
        loop.call_soon_threadsafe(_put, state.task_id, state)

    # 后台运行任务(voice 仅 TTS 模式生效)
    logger.info("创建任务 %s: mode=%s resume=%s", state.task_id, mode, resume)
    task = asyncio.create_task(pipeline.run(mode, url, state, progress, voice=voice, resume=resume))
    state.task = task  # 存引用供 /api/cancel 取消
    # 持有强引用防止 GC;任务结束延时清理任务表与队列
    _background_tasks.add(task)

    def _on_done(t: asyncio.Task) -> None:
        _background_tasks.discard(t)
        if t.cancelled():
            logger.info("任务 %s 已取消", state.task_id)
        elif t.exception():
            logger.info("任务 %s 终态: error", state.task_id)
        else:
            logger.info("任务 %s 终态: done", state.task_id)
        # 终态后延时清理,留窗口给 SSE 重连查看结果
        async def _cleanup():
            await asyncio.sleep(_TASK_RETAIN_SECONDS)
            _tasks.pop(state.task_id, None)
            _queues.pop(state.task_id, None)
            _remove_persisted(state.task_id)
        cleanup_task = asyncio.create_task(_cleanup())
        # 清理任务也纳入强引用集合,防止 fire-and-forget 被 GC 中途取消
        _background_tasks.add(cleanup_task)
        cleanup_task.add_done_callback(lambda ct: _background_tasks.discard(ct))

    task.add_done_callback(_on_done)

    return {"task_id": state.task_id}


async def retry(task_id: str) -> dict:
    """从断点重试一个失败的任务。

    从持久化的 tasks.json 读取原任务参数(mode/url/voice/video_id),
    用 resume=True 创建新任务。若原任务的资产包有 progress.json,
    则从断点继续;否则从头开始(等价于普通重跑)。
    """
    snap = _load_persisted().get(task_id)
    if not snap:
        raise HTTPException(404, "原任务不存在,无法重试")

    mode = snap.get("mode", "audio")
    url = snap.get("url", "")
    voice = snap.get("voice", "")
    if not url:
        raise HTTPException(400, "原任务缺少 URL,无法重试")

    logger.info("任务 %s 触发断点重试: mode=%s url=%s", task_id, mode, url)
    return await create(mode, url, voice=voice, resume=True)


async def cancel(task_id: str) -> dict:
    """取消正在运行的任务。"""
    state = _tasks.get(task_id)
    if not state:
        # 内存中已无,检查持久化(可能已终态)
        persisted = _load_persisted().get(task_id)
        if persisted:
            return {"ok": True, "already_done": True}
        raise HTTPException(404, "任务不存在")
    task = state.task
    if task is None or task.done():
        return {"ok": True, "already_done": True}
    task.cancel()
    return {"ok": True, "cancelled": True}


def progress_stream(task_id: str) -> StreamingResponse:
    """构造 SSE 响应:实时推送任务进度,结束后发送最终状态并关闭。

    任务不在内存中时,从持久化文件查终态补发后关闭(支持重启后重连)。
    """
    state = _tasks.get(task_id)

    # 内存无此任务:查持久化,有终态则补发
    if state is None:
        snap = _load_persisted().get(task_id)
        if snap:
            async def _replay():
                payload = {
                    "stage": snap.get("stage"),
                    "percent": snap.get("percent", 0),
                    "message": snap.get("message", ""),
                    "error": snap.get("error"),
                    "done": True,
                    "video_id": snap.get("video_id"),
                    "title": snap.get("title"),
                    "uploader": snap.get("uploader"),
                }
                yield f"data: {json.dumps(payload)}\n\n"
            return StreamingResponse(
                _replay(), media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
            )
        raise HTTPException(404, "任务不存在")

    queue = _queues.setdefault(task_id, asyncio.Queue())

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
                                "video_id": state.result.video_id,
                                "audio_url": state.result.audio_url,
                                "audio_name": state.result.audio_name,
                                "duration": state.result.duration,
                                "source_lang": state.result.source_lang,
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


def list_tasks() -> list[dict]:
    """获取所有进行中、排队中或失败的任务。"""
    mem_tasks = {}
    for task_id, state in _tasks.items():
        mem_tasks[task_id] = {
            "stage": state.stage,
            "percent": state.percent,
            "message": state.message,
            "error": state.error,
            "video_id": state.video_id,
            "title": state.title,
            "uploader": state.uploader,
            "url": state.url,
            "mode": state.mode,
            "voice": state.voice,
            "updated_at": time.time(),
        }
    
    persisted = _load_persisted()
    merged = {**persisted, **mem_tasks}
    
    res = []
    for task_id, snap in merged.items():
        if snap.get("stage") == "done":
            continue
        res.append({
            "task_id": task_id,
            **snap
        })
    return res


def delete_task(task_id: str) -> None:
    """取消运行中的任务并从内存和持久化数据中删除。"""
    state = _tasks.get(task_id)
    if state and state.task and not state.task.done():
        state.task.cancel()
    
    _tasks.pop(task_id, None)
    _queues.pop(task_id, None)
    _remove_persisted(task_id)

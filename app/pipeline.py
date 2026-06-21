"""两种模式的编排 + 进度回调。"""
from __future__ import annotations

import asyncio
import logging
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from . import assets, history_store

logger = logging.getLogger(__name__)

# 进度回调：(stage, percent, message) -> None
ProgressFn = Callable[[str, int, str], None]

MODE_AUDIO = "audio"          # 直接提取音频
MODE_SUBTITLE_TTS = "tts"     # 字幕翻译 → 中文 TTS

# 串行信号量：yt-dlp 下载、DeepSeek 翻译、edge-tts 合成均为重资源操作，
# 并发多个会耗尽带宽/内存，且字幕文件按 video_id 定位也需避免并发干扰。
# 同一时间只允许一个任务执行核心流程。
_sem = asyncio.Semaphore(1)


@dataclass
class TaskResult:
    task_id: str
    mode: str
    video_id: str
    title: str
    uploader: str
    bundle: "assets.AssetBundle"
    audio_ext: str = ".mp3"        # 实际音频扩展名(决定 /audio 返回的 Content-Type)
    duration: float = 0.0           # 源视频时长（秒）
    source_lang: str = ""           # TTS 模式翻译的源语言
    error: str | None = None

    @property
    def audio_url(self) -> str:
        return f"/audio/{self.video_id}"

    @property
    def audio_name(self) -> str:
        """前端展示用:音频文件名(资产包内为 audio.{ext})。"""
        return f"audio{self.audio_ext}"


@dataclass
class TaskState:
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    stage: str = "pending"      # pending / downloading / subtitling / translating / synthesizing / done / error
    percent: int = 0
    message: str = ""
    result: TaskResult | None = None
    error: str | None = None
    video_id: str | None = None
    title: str | None = None
    uploader: str | None = None
    task: object | None = None  # asyncio.Task 引用，供取消使用（避免循环引用仅存弱引用亦可，这里存强引用）
    # 任务参数(持久化供断点重试读取)
    url: str = ""
    mode: str = ""
    voice: str = ""


def _emit(state: TaskState, progress: ProgressFn | None) -> None:
    if progress:
        progress(state.stage, state.percent, state.message)


def _pending_video_id(state: TaskState) -> str:
    """fetch_info 前尚不知 video_id,用 task_id 作临时占位目录。

    fetch_info 返回真实 video_id 后会重建 bundle;临时目录若残留会在
    _cleanup_partial 中清理。这里用 task_id 保证不与真实 video_id 冲突。
    """
    return f".pending_{state.task_id}"


def _save_metadata(result: TaskResult) -> None:
    """保存任务的元数据到资产包 meta.json,并更新历史索引(data/history.json)。

    - output/{video_id}/meta.json:单条元数据(唯一来源)
    - data/history.json:集中式历史索引(按 video_id 去重、最新在前)
    """
    # 文件大小与生成时间
    audio_path = result.bundle.audio_path()
    try:
        size = audio_path.stat().st_size if audio_path else 0
    except OSError:
        size = 0
    data = {
        "task_id": result.task_id,
        "mode": result.mode,
        "video_id": result.video_id,
        "title": result.title,
        "uploader": result.uploader,
        "audio_url": result.audio_url,
        "audio_ext": result.audio_ext,
        "duration": result.duration,
        "size": size,
        "source_lang": result.source_lang,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        result.bundle.save_meta(data)
    except Exception as e:
        logger.warning("保存元数据失败: %s", e)
    # 同步更新历史索引(按 video_id 去重)
    try:
        history_store.upsert(data)
    except Exception as e:
        logger.warning("更新历史索引失败: %s", e)


async def run(mode: str, url: str, state: TaskState, progress: ProgressFn | None, voice: str = "", resume: bool = False) -> None:
    """根据 mode 调度对应流程,捕获异常写入 state。

    用信号量串行化:同一时间只跑一个重任务,避免资源耗尽与字幕串台。
    实际处理逻辑由 steps.py 的步骤链执行,这里负责调度、进度外推与异常兜底。

    resume=True 时,从资产包 progress.json 记录的断点继续(跳过已完成步骤)。
    """
    from . import steps  # 延迟导入(steps 依赖本模块的 TaskResult/TaskState)

    def emit() -> None:
        _emit(state, progress)

    logger.info("任务 %s 开始: mode=%s url=%s resume=%s", state.task_id, mode, url, resume)
    try:
        async with _sem:
            pipeline = steps.get_pipeline(mode)
            ctx = steps.Ctx(url=url, mode=mode, voice=voice, state=state)
            result = await pipeline.execute(ctx, emit=emit, resume=resume)
            state.result = result
            # 成功完成:推进到 100% 并保存元数据
            state.stage, state.percent, state.message = "done", 100, "处理完成"
            _emit(state, progress)
            _save_metadata(state.result)
            logger.info("任务 %s 成功完成: video_id=%s", state.task_id, result.video_id)
    except asyncio.CancelledError:
        # 用户取消:清理可能产生的半成品文件
        state.stage = "error"
        state.error = "任务已取消"
        state.message = "任务已取消"
        _emit(state, progress)
        _cleanup_partial(state)
        logger.info("任务 %s 已取消", state.task_id)
        raise
    except Exception as e:  # noqa: BLE001 - 顶层捕获,写入状态供前端展示
        state.stage = "error"
        state.error = str(e)
        state.message = f"处理失败: {e}"
        _emit(state, progress)
        logger.error("任务 %s 失败: %s", state.task_id, e, exc_info=True)


def _pending_video_id(state: TaskState) -> str:
    """fetch_info 前尚不知 video_id,用 task_id 作临时占位目录名。

    与 steps._fetch_info 中创建 pending 资产包的命名保持一致,
    供 _cleanup_partial 在取消时清理临时目录。
    """
    return f".pending_{state.task_id}"


def _cleanup_partial(state: TaskState) -> None:
    """取消任务时清理半成品:资产包目录或 pending 临时目录。

    清理失败不影响取消流程(最坏情况是残留目录,下次处理同视频会被覆盖)。
    """
    try:
        # 优先用真实 video_id,未取到则用 pending 占位目录
        vid = state.video_id or _pending_video_id(state)
        bundle = assets.AssetBundle(vid)
        if bundle.dir.exists():
            shutil.rmtree(bundle.dir, ignore_errors=True)
    except Exception as e:
        logger.warning("清理半成品文件失败: %s", e)


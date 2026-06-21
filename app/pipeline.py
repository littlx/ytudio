"""两种模式的编排 + 进度回调。"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import config, history_store, translate, tts, yt

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
    audio_path: Path
    audio_url: str
    duration: float = 0.0           # 源视频时长（秒）
    source_lang: str = ""           # TTS 模式翻译的源语言
    error: str | None = None


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


def _audio_filename(video_id: str, mode: str) -> str:
    suffix = "_zh" if mode == MODE_SUBTITLE_TTS else ""
    return f"{video_id}{suffix}.mp3"


def _emit(state: TaskState, progress: ProgressFn | None) -> None:
    if progress:
        progress(state.stage, state.percent, state.message)


async def run_audio_mode(url: str, state: TaskState, progress: ProgressFn | None) -> None:
    """模式 A：直接提取音频。"""
    state.stage, state.percent, state.message = "fetching", 5, "获取视频信息…"
    _emit(state, progress)
    info = await yt.fetch_info(url)

    # 提前提取元数据用于前端展示
    state.video_id = info.video_id
    state.title = info.title
    state.uploader = info.uploader

    state.stage, state.percent, state.message = "downloading", 20, f"下载音频: {info.title}"
    _emit(state, progress)

    base = config.OUTPUT_DIR / f"{info.video_id}_audio"
    # 下载进度映射到 20~95（音频模式下载是主要耗时阶段）
    def _on_dl(downloaded: float, total: float) -> None:
        if total > 0:
            state.percent = 20 + int(75 * min(downloaded / total, 1.0))
            state.message = f"下载音频: {int(downloaded / 1024 / 1024)}MB / {int(total / 1024 / 1024)}MB"
        else:
            state.message = f"下载音频: {int(downloaded / 1024 / 1024)}MB"
        _emit(state, progress)

    final = await yt.extract_audio(url, base, on_progress=_on_dl)
    if not final.exists():
        raise RuntimeError("音频提取失败，文件未生成。")

    state.stage, state.percent, state.message = "done", 100, "音频提取完成"
    _emit(state, progress)

    state.result = TaskResult(
        task_id=state.task_id,
        mode=MODE_AUDIO,
        video_id=info.video_id,
        title=info.title,
        uploader=info.uploader,
        audio_path=final,
        audio_url=f"/audio/{final.name}",
        duration=info.duration,
    )


async def run_tts_mode(url: str, state: TaskState, progress: ProgressFn | None, voice: str = "") -> None:
    """模式 B：字幕翻译 → 中文 TTS。"""
    state.stage, state.percent, state.message = "fetching", 5, "获取视频信息…"
    _emit(state, progress)
    info = await yt.fetch_info(url)

    # 提前提取元数据用于前端展示
    state.video_id = info.video_id
    state.title = info.title
    state.uploader = info.uploader

    state.stage, state.percent, state.message = "subtitling", 15, "提取字幕…"
    _emit(state, progress)
    sub_path, lang = await yt.extract_subtitle(
        url, config.OUTPUT_DIR, info.video_id, source_lang=info.subtitle_lang
    )

    state.stage, state.percent, state.message = "subtitling", 25, "解析字幕…"
    _emit(state, progress)
    raw_text = await asyncio.to_thread(yt.parse_subtitle_to_text, sub_path)
    if not raw_text.strip():
        raise RuntimeError("字幕解析后内容为空，无法继续。")

    state.stage, state.percent, state.message = "translating", 30, "开始翻译…"
    _emit(state, progress)

    # 翻译进度映射到 30~75
    def _on_trans(done: int, total: int, msg: str) -> None:
        state.stage = "translating"
        if total > 0:
            state.percent = 30 + int(45 * done / total)
        state.message = msg
        _emit(state, progress)

    # translate_text 返回按语义分段的段落列表，供 TTS 分片合成
    paragraphs = await translate.translate_text(raw_text, on_progress=_on_trans)

    # 保存译文文稿（供前端「查看文稿」展示）
    transcript_path = config.OUTPUT_DIR / f"{info.video_id}_zh.txt"
    try:
        transcript_path.write_text("\n\n".join(paragraphs), encoding="utf-8")
    except Exception as e:
        logger.warning("保存译文文稿失败: %s", e)

    state.stage, state.percent, state.message = "synthesizing", 80, "合成中文语音…"
    _emit(state, progress)

    # 合成进度映射到 80~98（逐段回报，解决长文本卡进度问题）
    def _on_tts(done: int, total: int, msg: str) -> None:
        state.stage = "synthesizing"
        if total > 0:
            state.percent = 80 + int(18 * done / total)
        state.message = msg
        _emit(state, progress)

    out_path = config.OUTPUT_DIR / _audio_filename(info.video_id, MODE_SUBTITLE_TTS)
    await tts.synthesize_speech(paragraphs, out_path, voice=voice or None, on_progress=_on_tts)

    state.stage, state.percent, state.message = "done", 100, "中文音频生成完成"
    _emit(state, progress)

    state.result = TaskResult(
        task_id=state.task_id,
        mode=MODE_SUBTITLE_TTS,
        video_id=info.video_id,
        title=info.title,
        uploader=info.uploader,
        audio_path=out_path,
        audio_url=f"/audio/{out_path.name}",
        duration=info.duration,
        source_lang=info.subtitle_lang,
    )


def _save_metadata(result: TaskResult) -> None:
    """保存任务的元数据到对应的 JSON 文件，并更新历史索引。

    - {audio_name}.json：单条元数据（兼容旧版读取逻辑）
    - data/history.json：集中式历史索引（新版前端读取源）
    """
    meta_path = result.audio_path.with_suffix(".json")
    # 文件大小与生成时间
    try:
        size = result.audio_path.stat().st_size
    except OSError:
        size = 0
    data = {
        "task_id": result.task_id,
        "mode": result.mode,
        "video_id": result.video_id,
        "title": result.title,
        "uploader": result.uploader,
        "audio_name": result.audio_path.name,
        "audio_url": result.audio_url,
        "duration": result.duration,
        "size": size,
        "source_lang": result.source_lang,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        meta_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning("保存元数据失败: %s", e)
    # 同步更新历史索引（data/history.json）
    try:
        history_store.upsert(data)
    except Exception as e:
        logger.warning("更新历史索引失败: %s", e)


async def run(mode: str, url: str, state: TaskState, progress: ProgressFn | None, voice: str = "") -> None:
    """根据 mode 调度对应流程，捕获异常写入 state。

    用信号量串行化：同一时间只跑一个重任务，避免资源耗尽与字幕串台。
    """
    try:
        async with _sem:
            if mode == MODE_AUDIO:
                await run_audio_mode(url, state, progress)
            elif mode == MODE_SUBTITLE_TTS:
                await run_tts_mode(url, state, progress, voice=voice)
            else:
                raise ValueError(f"未知模式: {mode}")

        # 成功完成后保存元数据
        if state.result:
            _save_metadata(state.result)
    except asyncio.CancelledError:
        # 用户取消：清理可能产生的半成品文件
        state.stage = "error"
        state.error = "任务已取消"
        state.message = "任务已取消"
        _emit(state, progress)
        if state.video_id:
            _cleanup_partial(state.video_id, mode)
        raise
    except Exception as e:  # noqa: BLE001 - 顶层捕获，写入状态供前端展示
        state.stage = "error"
        state.error = str(e)
        state.message = f"处理失败: {e}"
        _emit(state, progress)


def _cleanup_partial(video_id: str, mode: str) -> None:
    """取消任务时清理半成品：TTS 临时片段目录、半成品音频与译文/字幕文件。

    清理失败不影响取消流程（最坏情况是残留文件，下次处理同视频会被覆盖）。
    """
    try:
        # TTS 模式的临时片段目录（与 tts.py 中的命名一致）
        suffix = "_zh" if mode == MODE_SUBTITLE_TTS else "_audio"
        tmp_dir = config.OUTPUT_DIR / f".{video_id}{suffix}_tts_parts"
        if tmp_dir.exists():
            for p in tmp_dir.iterdir():
                p.unlink(missing_ok=True)
            tmp_dir.rmdir(missing_ok=True)
        # 半成品音频、译文 txt、字幕 json3/vtt（均以 video_id 为前缀落盘）
        for p in config.OUTPUT_DIR.glob(f"{video_id}*"):
            if p.is_file():
                p.unlink(missing_ok=True)
    except Exception as e:
        logger.warning("清理半成品文件失败: %s", e)


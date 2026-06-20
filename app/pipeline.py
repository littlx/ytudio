"""两种模式的编排 + 进度回调。"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from . import config, translate, tts, yt

# 进度回调：(stage, percent, message) -> None
ProgressFn = Callable[[str, int, str], None]

MODE_AUDIO = "audio"          # 直接提取音频
MODE_SUBTITLE_TTS = "tts"     # 字幕翻译 → 中文 TTS


@dataclass
class TaskResult:
    task_id: str
    mode: str
    video_id: str
    title: str
    uploader: str
    audio_path: Path
    audio_url: str
    error: str | None = None


@dataclass
class TaskState:
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    stage: str = "pending"      # pending / downloading / subtitling / translating / synthesizing / done / error
    percent: int = 0
    message: str = ""
    result: TaskResult | None = None
    error: str | None = None


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

    state.stage, state.percent, state.message = "downloading", 20, f"下载音频: {info.title}"
    _emit(state, progress)

    base = config.OUTPUT_DIR / f"{info.video_id}_audio"
    final = await yt.extract_audio(url, base)
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
    )


async def run_tts_mode(url: str, state: TaskState, progress: ProgressFn | None, voice: str = "") -> None:
    """模式 B：字幕翻译 → 中文 TTS。"""
    state.stage, state.percent, state.message = "fetching", 5, "获取视频信息…"
    _emit(state, progress)
    info = await yt.fetch_info(url)

    state.stage, state.percent, state.message = "subtitling", 15, "提取字幕…"
    _emit(state, progress)
    sub_path, lang = await yt.extract_subtitle(url, config.OUTPUT_DIR)

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

    chinese = await translate.translate_text(raw_text, on_progress=_on_trans)

    state.stage, state.percent, state.message = "synthesizing", 80, "合成中文语音…"
    _emit(state, progress)

    out_path = config.OUTPUT_DIR / _audio_filename(info.video_id, MODE_SUBTITLE_TTS)
    await tts.synthesize_speech(chinese, out_path, voice=voice or None)

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
    )


def _save_metadata(result: TaskResult) -> None:
    """保存任务的元数据到对应的 JSON 文件。"""
    import json
    meta_path = result.audio_path.with_suffix(".json")
    data = {
        "task_id": result.task_id,
        "mode": result.mode,
        "video_id": result.video_id,
        "title": result.title,
        "uploader": result.uploader,
        "audio_name": result.audio_path.name,
        "audio_url": result.audio_url,
    }
    try:
        meta_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        # 仅打印错误，不中断主流程
        print(f"Failed to save metadata: {e}")


async def run(mode: str, url: str, state: TaskState, progress: ProgressFn | None, voice: str = "") -> None:
    """根据 mode 调度对应流程，捕获异常写入 state。"""
    try:
        if mode == MODE_AUDIO:
            await run_audio_mode(url, state, progress)
        elif mode == MODE_SUBTITLE_TTS:
            await run_tts_mode(url, state, progress, voice=voice)
        else:
            raise ValueError(f"未知模式: {mode}")
        
        # 成功完成后保存元数据
        if state.result:
            _save_metadata(state.result)
    except Exception as e:  # noqa: BLE001 - 顶层捕获，写入状态供前端展示
        state.stage = "error"
        state.error = str(e)
        state.message = f"处理失败: {e}"
        _emit(state, progress)


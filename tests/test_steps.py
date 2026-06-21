"""steps.py 步骤链测试:进度区间、stage 顺序、取消清理。

用 mock 隔离 yt/translate/tts 网络调用,只验证步骤编排逻辑。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app import assets, steps
from app.pipeline import TaskState
from app.yt import VideoInfo


def _fake_info(vid: str = "testvid12345"):
    return VideoInfo(video_id=vid, title=f"Title {vid}", uploader="Chan",
                     url="https://youtube.com", duration=100.0, subtitle_lang="en")


@pytest.mark.asyncio
async def test_audio_pipeline_progress_monotonic(isolated_dirs):
    state = TaskState()
    ctx = steps.Ctx(url="https://youtube.com", mode="audio", state=state)
    log = []
    def emit(): log.append((state.stage, state.percent))

    audio_file = config.OUTPUT_DIR / "audio.mp3"
    audio_file.write_bytes(b"fake")
    with patch("app.steps.yt.fetch_info", new=AsyncMock(return_value=_fake_info())), \
         patch("app.steps.yt.extract_audio", new=AsyncMock(return_value=audio_file)):
        result = await steps.AUDIO_PIPELINE.execute(ctx, emit=emit)

    pcts = [p[1] for p in log]
    assert pcts == sorted(pcts), f"进度非单调: {pcts}"
    assert pcts[-1] == 100
    assert result.video_id == "testvid12345"
    assert result.audio_url == "/audio/testvid12345"


@pytest.mark.asyncio
async def test_audio_pipeline_stage_order(isolated_dirs):
    state = TaskState()
    ctx = steps.Ctx(url="https://youtube.com", mode="audio", state=state)
    stages = []
    def emit(): stages.append(state.stage)

    audio_file = config.OUTPUT_DIR / "audio.mp3"
    audio_file.write_bytes(b"fake")
    with patch("app.steps.yt.fetch_info", new=AsyncMock(return_value=_fake_info())), \
         patch("app.steps.yt.extract_audio", new=AsyncMock(return_value=audio_file)):
        await steps.AUDIO_PIPELINE.execute(ctx, emit=emit)

    order = [s for s in dict.fromkeys(stages)]
    assert "fetching" in order and "downloading" in order
    assert order.index("fetching") < order.index("downloading")


@pytest.mark.asyncio
async def test_tts_pipeline_stage_order(isolated_dirs):
    state = TaskState()
    ctx = steps.Ctx(url="https://youtube.com", mode="tts", voice="zh-CN-XiaoxiaoNeural", state=state)
    stages = []
    def emit(): stages.append(state.stage)

    audio_file = config.OUTPUT_DIR / "tts.mp3"
    audio_file.write_bytes(b"fake")

    async def fake_extract_sub(url, bundle, source_lang="en"):
        bundle.ensure_dir()
        (bundle.dir / f"subtitle.{bundle.video_id}.{source_lang}.json3").write_text("{}")
        return (str(bundle.dir / f"subtitle.{bundle.video_id}.{source_lang}.json3"), source_lang)

    with patch("app.steps.yt.fetch_info", new=AsyncMock(return_value=_fake_info())), \
         patch("app.steps.yt.extract_subtitle", new=AsyncMock(side_effect=fake_extract_sub)), \
         patch("app.steps.yt.parse_subtitle_to_text", return_value="Hello world."), \
         patch("app.steps.translate.translate_text", new=AsyncMock(return_value=["你好。"])), \
         patch("app.steps.tts.synthesize_speech", new=AsyncMock(return_value=audio_file)):
        result = await steps.TTS_PIPELINE.execute(ctx, emit=emit)

    order = [s for s in dict.fromkeys(stages)]
    expected = ["fetching", "subtitling", "translating", "synthesizing"]
    actual = [s for s in order if s in expected]
    assert actual == expected, f"stage 顺序错: {actual}"
    assert result.source_lang == "en"
    # 译文应保存到资产包
    assert ctx.bundle.transcript_path.exists()


@pytest.mark.asyncio
async def test_tts_pipeline_progress_monotonic(isolated_dirs):
    state = TaskState()
    ctx = steps.Ctx(url="https://youtube.com", mode="tts", state=state)
    pcts = []
    def emit(): pcts.append(state.percent)

    audio_file = config.OUTPUT_DIR / "tts.mp3"
    audio_file.write_bytes(b"fake")

    async def fake_extract_sub(url, bundle, source_lang="en"):
        bundle.ensure_dir()
        (bundle.dir / f"subtitle.{bundle.video_id}.{source_lang}.json3").write_text("{}")
        return ("sub", source_lang)

    with patch("app.steps.yt.fetch_info", new=AsyncMock(return_value=_fake_info())), \
         patch("app.steps.yt.extract_subtitle", new=AsyncMock(side_effect=fake_extract_sub)), \
         patch("app.steps.yt.parse_subtitle_to_text", return_value="Hello."), \
         patch("app.steps.translate.translate_text", new=AsyncMock(return_value=["你好。"])), \
         patch("app.steps.tts.synthesize_speech", new=AsyncMock(return_value=audio_file)):
        await steps.TTS_PIPELINE.execute(ctx, emit=emit)

    assert pcts == sorted(pcts), f"进度非单调: {pcts}"
    assert pcts[-1] == 100


@pytest.mark.asyncio
async def test_unknown_mode_raises(isolated_dirs):
    with pytest.raises(ValueError):
        steps.get_pipeline("unknown_mode")


@pytest.mark.asyncio
async def test_fetch_info_fills_bundle(isolated_dirs):
    """FetchInfo 步骤应填充 ctx.info 和 ctx.bundle(用真实 video_id)。"""
    state = TaskState()
    ctx = steps.Ctx(url="https://youtube.com", mode="audio", state=state)
    with patch("app.steps.yt.fetch_info", new=AsyncMock(return_value=_fake_info("realvid12345"))):
        await steps._fetch_info(ctx, lambda r, m: None)
    assert ctx.info is not None
    assert ctx.info.video_id == "realvid12345"
    assert ctx.bundle is not None
    assert ctx.bundle.video_id == "realvid12345"


# 导入 config 供上面使用
from app import config  # noqa: E402

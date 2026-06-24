"""断点续传测试:步骤级 resume。

验证:
- 步骤完成后写入 progress.json
- resume=True 时跳过已完成步骤,从断点继续
- 跳过的步骤产出到 ctx 被正确恢复(info/audio_ext/paragraphs)
- 全部完成后 progress.json 被清除
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from app import assets, config, steps
from app.pipeline import TaskState
from app.yt import VideoInfo


def _fake_info(vid: str = "resumevid1234"):
    return VideoInfo(video_id=vid, title=f"Title {vid}", uploader="Chan",
                     url="https://youtube.com", duration=100.0, subtitle_lang="en")


class TestProgressPersistence:
    def test_save_and_load_progress(self, isolated_dirs):
        b = assets.AssetBundle("vid123")
        b.save_progress({"completed_steps": ["fetching"], "info": {"video_id": "vid123"}})
        prog = b.load_progress()
        assert prog is not None
        assert prog["completed_steps"] == ["fetching"]
        assert prog["info"]["video_id"] == "vid123"

    def test_load_progress_none_when_missing(self, isolated_dirs):
        b = assets.AssetBundle("vid123")
        assert b.load_progress() is None

    def test_clear_progress(self, isolated_dirs):
        b = assets.AssetBundle("vid123")
        b.save_progress({"completed_steps": ["fetching"]})
        assert b.progress_path.exists()
        b.clear_progress()
        assert not b.progress_path.exists()

    def test_clear_progress_idempotent(self, isolated_dirs):
        b = assets.AssetBundle("vid123")
        b.clear_progress()  # 不存在不应报错
        assert not b.progress_path.exists()


@pytest.mark.asyncio
async def test_resume_skips_completed_steps(isolated_dirs):
    """模拟翻译步骤失败:前 3 步完成,resume 后跳过它们从翻译继续。"""
    state = TaskState()
    ctx = steps.Ctx(url="https://youtube.com", mode="tts", voice="zh-CN-XiaoxiaoNeural", state=state)
    audio_file = config.OUTPUT_DIR / "tts.mp3"
    audio_file.write_bytes(b"fake")

    # 第一轮:让 fetch_info + extract_subtitle + parse_subtitle 成功,翻译失败
    call_log = []

    async def failing_translate(raw_text, on_progress=None, **kwargs):
        call_log.append("translate_called")
        raise RuntimeError("翻译服务不可用")

    async def fake_extract_sub(url, bundle, source_lang="en", **kwargs):
        bundle.ensure_dir()
        (bundle.dir / f"subtitle.{bundle.video_id}.{source_lang}.json3").write_text("{}")
        return ("sub", source_lang)

    with patch("app.steps.yt.fetch_info", new=AsyncMock(return_value=_fake_info())), \
         patch("app.steps.yt.extract_subtitle", new=AsyncMock(side_effect=fake_extract_sub)), \
         patch("app.steps.yt.parse_subtitle_to_text", return_value="Hello world."), \
         patch("app.steps.translate.translate_text", new=AsyncMock(side_effect=failing_translate)):
        try:
            await steps.TTS_PIPELINE.execute(ctx, emit=None, resume=False)
            assert False, "翻译应失败"
        except RuntimeError as e:
            assert "翻译服务不可用" in str(e)

    # 验证 progress.json 记录了前 3 步完成(用步骤索引 0,1,2)
    prog = ctx.bundle.load_progress()
    assert prog is not None
    assert 0 in prog["completed_steps"]   # fetching
    assert 1 in prog["completed_steps"]   # subtitling (extract)
    assert 2 in prog["completed_steps"]   # subtitling (parse)
    assert "info" in prog  # FetchInfo 产出快照
    assert prog["info"]["video_id"] == "resumevid1234"
    print(f"  第一轮失败后 progress.json: completed_idx={prog['completed_steps']}")

    # 第二轮:resume=True,翻译成功,验证跳过前 3 步
    state2 = TaskState()
    ctx2 = steps.Ctx(url="https://youtube.com", mode="tts", voice="zh-CN-XiaoxiaoNeural", state=state2)
    # bundle 需指向同一资产包(resume 依赖 progress.json)
    ctx2.bundle = ctx.bundle

    fetch_call_count = 0
    async def counting_fetch(url, bundle=None, download_thumb=True, **kwargs):
        nonlocal fetch_call_count
        fetch_call_count += 1
        return _fake_info()

    with patch("app.steps.yt.fetch_info", new=AsyncMock(side_effect=counting_fetch)), \
         patch("app.steps.yt.extract_subtitle", new=AsyncMock(side_effect=fake_extract_sub)), \
         patch("app.steps.yt.parse_subtitle_to_text", return_value="Hello."), \
         patch("app.steps.translate.translate_text", new=AsyncMock(return_value=["你好。"])), \
         patch("app.steps.tts.synthesize_speech", new=AsyncMock(return_value=audio_file)):
        result = await steps.TTS_PIPELINE.execute(ctx2, emit=None, resume=True)

    # 验证:fetch_info 没被调用(被跳过)
    assert fetch_call_count == 0, "fetch_info 应被跳过,不应调用"
    # 验证:ctx.info 从 progress.json 正确恢复
    assert ctx2.info is not None
    assert ctx2.info.video_id == "resumevid1234"
    assert ctx2.info.title == "Title resumevid1234"
    # 验证:翻译被调用(resume 后从这里继续)
    assert "translate_called" in call_log
    # 验证:任务成功完成
    assert result.video_id == "resumevid1234"
    assert result.mode == "tts"
    # 验证:progress.json 被清除
    assert not ctx.bundle.progress_path.exists()
    print("  第二轮 resume 成功:跳过前 3 步,翻译+合成完成,progress.json 已清除")


@pytest.mark.asyncio
async def test_resume_restores_paragraphs_from_transcript(isolated_dirs):
    """翻译步骤已完成时,resume 应从 transcript_zh.txt 还原 paragraphs。"""
    state = TaskState()
    ctx = steps.Ctx(url="https://youtube.com", mode="tts", state=state)
    bundle = assets.AssetBundle("transcriptvid12")
    bundle.ensure_dir()
    # 模拟翻译步骤已完成:写 transcript + progress.json 记录到翻译完成
    bundle.transcript_path.write_text("第一段\n\n第二段\n\n第三段", encoding="utf-8")
    # completed_steps 用步骤索引:tts 模式前 3 步是 fetching/subtitling/subtitling
    bundle.save_progress({
        "completed_steps": [0, 1, 2, 3],  # fetching + 2×subtitling + translating
        "info": {"video_id": "transcriptvid12", "title": "T", "uploader": "C",
                 "url": "u", "duration": 50.0, "subtitle_lang": "en"},
        "audio_ext": ".mp3",
    })
    ctx.bundle = bundle

    audio_file = config.OUTPUT_DIR / "tts.mp3"
    audio_file.write_bytes(b"fake")

    # resume:应跳过前 3 步,直接到 synthesizing
    synthesize_call_args = {}
    async def capture_synthesize(paragraphs, b, voice=None, on_progress=None, **kwargs):
        synthesize_call_args["paragraphs"] = paragraphs
        synthesize_call_args["voice"] = voice
        return b.dir / "audio.mp3"

    with patch("app.steps.yt.fetch_info", new=AsyncMock()) as m_fetch, \
         patch("app.steps.tts.synthesize_speech", new=AsyncMock(side_effect=capture_synthesize)):
        # 确保 audio.mp3 存在(合成产物)
        (bundle.dir / "audio.mp3").write_bytes(b"fake")
        result = await steps.TTS_PIPELINE.execute(ctx, emit=None, resume=True)

    # fetch_info 不应被调用(已跳过)
    assert not m_fetch.called, "fetch_info 应被跳过"
    # paragraphs 从 transcript 还原,传给 synthesize
    assert synthesize_call_args["paragraphs"] == ["第一段", "第二段", "第三段"]
    assert result.video_id == "transcriptvid12"
    # progress.json 已清除
    assert not bundle.progress_path.exists()
    print("  resume 从 transcript 还原 paragraphs 成功,合成完成")


@pytest.mark.asyncio
async def test_resume_without_progress_starts_from_beginning(isolated_dirs):
    """resume=True 但无 progress.json 时,等价于从头开始。"""
    state = TaskState()
    ctx = steps.Ctx(url="https://youtube.com", mode="audio", state=state)
    audio_file = config.OUTPUT_DIR / "audio.mp3"
    audio_file.write_bytes(b"fake")

    fetch_called = False
    async def check_fetch(url, bundle=None, download_thumb=True, **kwargs):
        nonlocal fetch_called
        fetch_called = True
        return _fake_info()

    with patch("app.steps.yt.fetch_info", new=AsyncMock(side_effect=check_fetch)), \
         patch("app.steps.yt.extract_audio", new=AsyncMock(return_value=audio_file)):
        result = await steps.AUDIO_PIPELINE.execute(ctx, emit=None, resume=True)

    assert fetch_called, "无 progress.json 时 fetch_info 应被调用(从头开始)"
    assert result.video_id == "resumevid1234"
    print("  无 progress.json 的 resume 等价于从头开始")


@pytest.mark.asyncio
async def test_no_resume_always_runs_all_steps(isolated_dirs):
    """resume=False(默认)时,即使有 progress.json 也全部重跑。"""
    state = TaskState()
    ctx = steps.Ctx(url="https://youtube.com", mode="audio", state=state)
    # 预置一个 progress.json(模拟之前跑到一半)
    bundle = assets.AssetBundle("resumevid1234")
    bundle.ensure_dir()
    bundle.save_progress({
        "completed_steps": ["fetching"],
        "info": {"video_id": "resumevid1234"},
    })
    ctx.bundle = bundle

    audio_file = config.OUTPUT_DIR / "audio.mp3"
    audio_file.write_bytes(b"fake")

    fetch_called = False
    async def check_fetch(url, bundle=None, download_thumb=True, **kwargs):
        nonlocal fetch_called
        fetch_called = True
        return _fake_info()

    with patch("app.steps.yt.fetch_info", new=AsyncMock(side_effect=check_fetch)), \
         patch("app.steps.yt.extract_audio", new=AsyncMock(return_value=audio_file)):
        result = await steps.AUDIO_PIPELINE.execute(ctx, emit=None, resume=False)

    assert fetch_called, "resume=False 时 fetch_info 应被调用(全部重跑)"
    assert result.video_id == "resumevid1234"
    print("  resume=False 时全部重跑,忽略 progress.json")


@pytest.mark.asyncio
async def test_pipeline_run_with_resume(isolated_dirs):
    """pipeline.run 支持 resume 参数,透传给 Pipeline.execute。"""
    from app import pipeline

    state = TaskState()
    audio_file = config.OUTPUT_DIR / "audio.mp3"
    audio_file.write_bytes(b"fake")

    with patch("app.steps.yt.fetch_info", new=AsyncMock(return_value=_fake_info())), \
         patch("app.steps.yt.extract_audio", new=AsyncMock(return_value=audio_file)):
        await pipeline.run("audio", "https://youtube.com", state, None, resume=False)

    assert state.stage == "done"
    assert state.result is not None
    # progress.json 应在成功完成后被清除
    assert not state.result.bundle.progress_path.exists()
    print("  pipeline.run(resume=False) 成功完成,progress.json 已清除")


@pytest.mark.asyncio
async def test_tasks_retry_resumes_from_breakpoint(isolated_dirs):
    """验证 tasks.retry 重试时,能读取任务持久化记录中的 video_id 并在新任务中正确跳过已完成步骤。"""
    from app import tasks

    # 1. 模拟持久化的旧任务
    task_id = "old_failed_task"
    snap = {
        "mode": "tts",
        "url": "https://youtube.com/watch?v=123",
        "voice": "zh-CN-XiaoxiaoNeural",
        "video_id": "testvid123",
        "stage": "error",
    }

    # 将旧任务写入持久化存储
    tasks._save_persisted({task_id: snap})

    # 2. 预置 progress.json 到对应的 video_id 目录下,标记前3个步骤已完成
    bundle = assets.AssetBundle("testvid123")
    bundle.ensure_dir()
    (bundle.dir / "subtitle.en.json3").write_text("Hello")
    bundle.save_progress({
        "completed_steps": [0, 1, 2],  # fetching + 2×subtitling
        "info": {
            "video_id": "testvid123",
            "title": "Test Title",
            "uploader": "Test Uploader",
            "url": "https://youtube.com/watch?v=123",
            "duration": 60.0,
            "subtitle_lang": "en"
        }
    })

    # 模拟剩下的步骤(translating, synthesizing, done)所需的文件
    audio_file = config.OUTPUT_DIR / "tts.mp3"
    audio_file.write_bytes(b"fake")

    # 3. 运行 retry 并断言
    # 监控 fetch_info 和 extract_subtitle 确保被跳过
    fetch_calls = 0
    async def fake_fetch(*args, **kwargs):
        nonlocal fetch_calls
        fetch_calls += 1
        return _fake_info("testvid123")

    extract_calls = 0
    async def fake_extract(*args, **kwargs):
        nonlocal extract_calls
        extract_calls += 1

    with patch("app.steps.yt.fetch_info", new=AsyncMock(side_effect=fake_fetch)), \
         patch("app.steps.yt.extract_subtitle", new=AsyncMock(side_effect=fake_extract)), \
         patch("app.steps.yt.parse_subtitle_to_text", return_value="Hello"), \
         patch("app.steps.translate.translate_text", new=AsyncMock(return_value=["你好"])), \
         patch("app.steps.tts.synthesize_speech", new=AsyncMock(return_value=audio_file)):

        # 执行 retry(task_id)
        res = await tasks.retry(task_id)
        new_task_id = res["task_id"]

        # 等待后台任务执行完成
        state = tasks._tasks[new_task_id]
        await state.task

        # 验证结果
        assert state.stage == "done"
        assert fetch_calls == 0, "fetch_info 应被跳过"
        assert extract_calls == 0, "extract_subtitle 应被跳过"
        # 验证 progress.json 已被删除
        assert not bundle.progress_path.exists()


"""pipeline.run 调度测试:成功/异常/取消。

用 mock 隔离步骤链的下游网络调用,验证 run 的调度、进度外推、异常捕获、取消清理。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app import assets, config, pipeline
from app.pipeline import TaskState
from app.yt import VideoInfo


def _fake_info(vid: str = "runvid12345"):
    return VideoInfo(video_id=vid, title="Run", uploader="Chan",
                     url="url", duration=50.0, subtitle_lang="en")


@pytest.mark.asyncio
async def test_run_success_saves_metadata(isolated_dirs):
    state = TaskState()
    audio_file = config.OUTPUT_DIR / "audio.mp3"
    audio_file.write_bytes(b"x")
    progress_calls = []
    def progress(stage, pct, msg): progress_calls.append((stage, pct))

    with patch("app.steps.yt.fetch_info", new=AsyncMock(return_value=_fake_info())), \
         patch("app.steps.yt.extract_audio", new=AsyncMock(return_value=audio_file)):
        await pipeline.run("audio", "https://youtube.com", state, progress)

    assert state.stage == "done" and state.percent == 100
    assert state.result is not None
    assert state.result.video_id == "runvid12345"
    # 元数据已保存到资产包
    assert state.result.bundle.meta_path.exists()
    # 历史索引已更新
    from app import history_store
    assert len(history_store.load()) == 1


@pytest.mark.asyncio
async def test_run_exception_written_to_state(isolated_dirs):
    state = TaskState()
    def progress(stage, pct, msg): pass
    with patch("app.steps.yt.fetch_info", new=AsyncMock(side_effect=RuntimeError("网络错误"))):
        await pipeline.run("audio", "https://youtube.com", state, progress)
    assert state.stage == "error"
    assert "网络错误" in state.error


@pytest.mark.asyncio
async def test_run_unknown_mode_written_to_state(isolated_dirs):
    """未知模式:异常被 run 捕获写入 state(不应冒泡到任务调度之外)。"""
    state = TaskState()
    def progress(stage, pct, msg): pass
    await pipeline.run("unknown", "url", state, progress)
    assert state.stage == "error"
    assert "未知模式" in state.error


@pytest.mark.asyncio
async def test_run_cancel_cleans_up(isolated_dirs):
    state = TaskState()
    async def slow_download(url, bundle, on_progress=None):
        await asyncio.sleep(100)  # 模拟长时间下载

    with patch("app.steps.yt.fetch_info", new=AsyncMock(return_value=_fake_info())), \
         patch("app.steps.yt.extract_audio", new=AsyncMock(side_effect=slow_download)):
        task = asyncio.create_task(pipeline.run("audio", "url", state, None))
        await asyncio.sleep(0.1)  # 让任务跑到下载阶段
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert state.stage == "error"
    assert "取消" in state.error
    # 资产包目录应被清理
    bundle = assets.AssetBundle("runvid12345")
    assert not bundle.dir.exists()


@pytest.mark.asyncio
async def test_run_serialized_by_semaphore(isolated_dirs):
    """信号量保证同一时间只跑一个任务(串行化)。

    两个任务并发提交,记录各自的执行区间,验证区间不重叠。
    用单一共享 patch 包裹两个任务,避免并发 patch 的竞争。
    """
    state1, state2 = TaskState(), TaskState()
    audio_file = config.OUTPUT_DIR / "audio.mp3"
    audio_file.write_bytes(b"x")
    windows = {}  # tag -> (start_time, end_time)
    call_count = 0

    async def slow_fetch(url, bundle=None, download_thumb=True):
        nonlocal call_count
        call_count += 1
        tag = f"vid{call_count}"
        start = asyncio.get_event_loop().time()
        await asyncio.sleep(0.05)
        end = asyncio.get_event_loop().time()
        windows[tag] = (start, end)
        return _fake_info(tag)

    async def run_one(state):
        await pipeline.run("audio", "https://youtube.com/watch?v=dQw4w9WgXcQ", state, None)

    with patch("app.steps.yt.fetch_info", new=AsyncMock(side_effect=slow_fetch)), \
         patch("app.steps.yt.extract_audio", new=AsyncMock(return_value=audio_file)):
        await asyncio.gather(run_one(state1), run_one(state2))

    assert len(windows) == 2
    tags = sorted(windows.keys())
    s1, e1 = windows[tags[0]]
    s2, e2 = windows[tags[1]]
    # 串行:区间不重叠(一个的 end <= 另一个的 start)
    assert e1 <= s2 or e2 <= s1, f"任务区间重叠: {tags[0]}={s1}-{e1}, {tags[1]}={s2}-{e2}"

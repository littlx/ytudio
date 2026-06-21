"""pytest 共享 fixture。

关键设计:每个测试用独立的临时目录作为 OUTPUT_DIR / DATA_DIR,
避免测试污染真实的 output/ 和 data/。通过 monkeypatch config 模块
的路径常量实现隔离。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import config


@pytest.fixture
def isolated_dirs(tmp_path: Path, monkeypatch) -> Path:
    """提供隔离的 output/ 和 data/ 目录,返回临时根目录。

    测试中所有资产包、历史、任务状态操作都落到 tmp_path 下,
    不影响真实数据。
    """
    output = tmp_path / "output"
    data = tmp_path / "data"
    output.mkdir()
    data.mkdir()
    monkeypatch.setattr(config, "OUTPUT_DIR", output)
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "HISTORY_FILE", data / "history.json")
    monkeypatch.setattr(config, "TASKS_FILE", data / "tasks.json")
    return tmp_path


@pytest.fixture
def make_legacy_files(isolated_dirs: Path):
    """工厂:在隔离的 output/ 下构造旧版散落文件,供迁移测试使用。"""
    output = config.OUTPUT_DIR

    def _make(vid: str, mode: str = "tts", *, with_transcript: bool = True,
              with_thumb: bool = True, created_at: str = "2026-01-01T00:00:00+00:00"):
        """构造一个 video_id 的旧版散落文件。"""
        suffix = "_zh" if mode == "tts" else "_audio"
        ext = ".mp3" if mode == "tts" else ".m4a"
        # 音频
        (output / f"{vid}{suffix}{ext}").write_bytes(b"audio")
        # 元数据
        meta = {
            "task_id": "t_" + vid, "mode": mode, "video_id": vid,
            "title": f"Title {vid}", "uploader": "Chan",
            "audio_name": f"{vid}{suffix}{ext}",
            "audio_url": f"/audio/{vid}{suffix}{ext}",
            "duration": 300.0, "size": 100, "source_lang": "en",
            "created_at": created_at,
        }
        (output / f"{vid}{suffix}.json").write_text(json.dumps(meta))
        # 字幕(TTS 模式)
        if mode == "tts":
            (output / f"{vid}.en.json3").write_text("{}")
            if with_transcript:
                (output / f"{vid}_zh.txt").write_text("译文内容")
        # 缩略图
        if with_thumb:
            (output / f"{vid}.jpg").write_bytes(b"jpg")

    return _make

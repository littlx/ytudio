"""history_store.py 测试:upsert 去重、remove、重建。"""
from __future__ import annotations

import json

from app import assets, config, history_store


def _make_bundle(vid: str, created_at: str = "2026-01-01T00:00:00+00:00"):
    b = assets.AssetBundle(vid)
    b.save_meta({"video_id": vid, "title": f"Title {vid}", "created_at": created_at})
    return b


class TestUpsert:
    def test_add_new(self, isolated_dirs):
        history_store.upsert({"video_id": "vid1", "title": "T1"})
        hist = history_store.load()
        assert len(hist) == 1 and hist[0]["video_id"] == "vid1"

    def test_dedup_by_video_id(self, isolated_dirs):
        history_store.upsert({"video_id": "vid1", "title": "T1"})
        history_store.upsert({"video_id": "vid1", "title": "T1 Updated"})
        hist = history_store.load()
        assert len(hist) == 1
        assert hist[0]["title"] == "T1 Updated"

    def test_latest_first(self, isolated_dirs):
        history_store.upsert({"video_id": "vid1", "created_at": "2026-01-01T00:00:00+00:00"})
        history_store.upsert({"video_id": "vid2", "created_at": "2026-02-01T00:00:00+00:00"})
        hist = history_store.load()
        assert hist[0]["video_id"] == "vid2"

    def test_skip_without_video_id(self, isolated_dirs):
        history_store.upsert({"title": "no vid"})
        assert history_store.load() == []


class TestRemove:
    def test_remove_existing(self, isolated_dirs):
        history_store.upsert({"video_id": "vid1"})
        history_store.upsert({"video_id": "vid2"})
        history_store.remove("vid1")
        hist = history_store.load()
        assert len(hist) == 1 and hist[0]["video_id"] == "vid2"

    def test_remove_nonexistent_no_error(self, isolated_dirs):
        history_store.upsert({"video_id": "vid1"})
        history_store.remove("nonexistent")  # 不应抛异常
        assert len(history_store.load()) == 1


class TestClear:
    def test_clear_all(self, isolated_dirs):
        history_store.upsert({"video_id": "vid1"})
        history_store.upsert({"video_id": "vid2"})
        history_store.clear()
        assert history_store.load() == []


class TestRebuildFromBundles:
    def test_rebuild_from_meta_files(self, isolated_dirs):
        _make_bundle("vid1", "2026-01-01T00:00:00+00:00")
        _make_bundle("vid2", "2026-02-01T00:00:00+00:00")
        hist = history_store.rebuild_from_bundles()
        assert len(hist) == 2
        assert hist[0]["video_id"] == "vid2"  # 最新在前

    def test_rebuild_empty_when_no_bundles(self, isolated_dirs):
        assert history_store.rebuild_from_bundles() == []


class TestLoadRobustness:
    def test_corrupt_file_returns_empty(self, isolated_dirs):
        config.HISTORY_FILE.write_text("not json{{{")
        assert history_store.load() == []

    def test_non_list_returns_empty(self, isolated_dirs):
        config.HISTORY_FILE.write_text(json.dumps({"not": "a list"}))
        assert history_store.load() == []

    def test_old_format_triggers_rebuild(self, isolated_dirs):
        """旧格式(有 audio_name 无 video_id)应触发从资产包重建。"""
        _make_bundle("vid1")
        config.HISTORY_FILE.write_text(json.dumps([
            {"audio_name": "vid1_zh.mp3"}  # 旧格式无 video_id
        ]))
        hist = history_store.load()
        assert len(hist) == 1 and hist[0]["video_id"] == "vid1"

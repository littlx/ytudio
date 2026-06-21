"""资产包接口与旧版迁移测试。"""
from __future__ import annotations

import json

from app import assets, config, history_store


class TestAssetBundle:
    def test_ensure_dir_and_meta(self, isolated_dirs):
        b = assets.AssetBundle("vid123")
        assert not b.exists()
        b.save_meta({"video_id": "vid123", "title": "Test"})
        assert b.exists()
        meta = b.meta()
        assert meta["title"] == "Test"

    def test_audio_path_finds_by_extension(self, isolated_dirs):
        b = assets.AssetBundle("vid123")
        b.ensure_dir()
        (b.dir / "audio.m4a").write_bytes(b"data")
        assert b.audio_path().suffix == ".m4a"

    def test_audio_path_none_when_missing(self, isolated_dirs):
        b = assets.AssetBundle("vid123")
        b.ensure_dir()
        assert b.audio_path() is None

    def test_thumb_path(self, isolated_dirs):
        b = assets.AssetBundle("vid123")
        b.ensure_dir()
        (b.dir / "thumb.webp").write_bytes(b"data")
        assert b.thumb_path().suffix == ".webp"

    def test_subtitle_file_with_lang_suffix(self, isolated_dirs):
        b = assets.AssetBundle("vid123")
        b.ensure_dir()
        (b.dir / "subtitle.vid123.en.json3").write_text("{}")
        sub = b.subtitle_file()
        assert sub is not None and sub.suffix == ".json3"

    def test_remove_deletes_directory(self, isolated_dirs):
        b = assets.AssetBundle("vid123")
        b.ensure_dir()
        (b.dir / "audio.mp3").write_bytes(b"data")
        (b.dir / "meta.json").write_text("{}")
        deleted = b.remove()
        assert "audio.mp3" in deleted and "meta.json" in deleted
        assert not b.dir.exists()

    def test_meta_corrupt_returns_none(self, isolated_dirs):
        b = assets.AssetBundle("vid123")
        b.ensure_dir()
        b.meta_path.write_text("not json")
        assert b.meta() is None


class TestListBundles:
    def test_empty_when_no_bundles(self, isolated_dirs):
        assert assets.list_bundles() == []

    def test_sorted_by_created_at_desc(self, isolated_dirs):
        for vid, ts in [("old", "2026-01-01T00:00:00+00:00"),
                        ("new", "2026-06-01T00:00:00+00:00"),
                        ("mid", "2026-03-01T00:00:00+00:00")]:
            b = assets.AssetBundle(vid)
            b.save_meta({"video_id": vid, "created_at": ts})
        bundles = assets.list_bundles()
        assert [b.video_id for b in bundles] == ["new", "mid", "old"]

    def test_skips_directories_without_meta(self, isolated_dirs):
        (config.OUTPUT_DIR / "nometa").mkdir()
        b = assets.AssetBundle("withmeta")
        b.save_meta({"video_id": "withmeta"})
        bundles = assets.list_bundles()
        assert [b.video_id for b in bundles] == ["withmeta"]


class TestMigrateLegacy:
    def test_migrate_tts_files(self, make_legacy_files):
        make_legacy_files("ABC123xyz45", mode="tts")
        count = assets.migrate_legacy()
        assert count == 1

        b = assets.AssetBundle("ABC123xyz45")
        assert b.audio_path().suffix == ".mp3"
        assert b.thumb_path().suffix == ".jpg"
        assert b.transcript_path.exists()
        assert b.subtitle_file() is not None
        meta = b.meta()
        assert meta["audio_url"] == "/audio/ABC123xyz45"
        assert meta["audio_ext"] == ".mp3"

    def test_migrate_audio_mode_files(self, make_legacy_files):
        make_legacy_files("XYZ789abc01", mode="audio")
        count = assets.migrate_legacy()
        assert count == 1

        b = assets.AssetBundle("XYZ789abc01")
        assert b.audio_path().suffix == ".m4a"
        meta = b.meta()
        assert meta["audio_ext"] == ".m4a"

    def test_migrate_multiple(self, make_legacy_files):
        make_legacy_files("vid_aaaaaaa01", mode="tts", created_at="2026-01-01T00:00:00+00:00")
        make_legacy_files("vid_bbbbbbb02", mode="audio", created_at="2026-02-01T00:00:00+00:00")
        count = assets.migrate_legacy()
        assert count == 2

    def test_migrate_idempotent(self, make_legacy_files):
        make_legacy_files("ABC123xyz45", mode="tts")
        assert assets.migrate_legacy() == 1
        # 第二次应跳过(已有 .migrated 标记)
        assert assets.migrate_legacy() == 0

    def test_migrate_clears_root_files(self, make_legacy_files):
        make_legacy_files("ABC123xyz45", mode="tts")
        assets.migrate_legacy()
        # 根目录应只剩 .migrated 标记(非目录文件)
        root_files = [p.name for p in config.OUTPUT_DIR.iterdir() if not p.is_dir()]
        assert root_files == [".migrated"]

    def test_migrate_deletes_old_history_and_rebuilds(self, make_legacy_files):
        make_legacy_files("ABC123xyz45", mode="tts")
        # 预置旧格式 history.json
        config.HISTORY_FILE.write_text(json.dumps([
            {"audio_name": "ABC123xyz45_zh.mp3", "video_id": "ABC123xyz45",
             "audio_url": "/audio/ABC123xyz45_zh.mp3"}
        ]))
        assets.migrate_legacy()
        # 旧 history.json 应被删除,load 时从 meta.json 重建
        assert not config.HISTORY_FILE.exists()
        hist = history_store.load()
        assert len(hist) == 1
        assert hist[0]["audio_url"] == "/audio/ABC123xyz45"

    def test_migrate_partial_files_no_transcript(self, make_legacy_files):
        """部分缺失(无译文)的旧记录应正常迁移。"""
        make_legacy_files("PARTIALvid01", mode="tts", with_transcript=False)
        assets.migrate_legacy()
        b = assets.AssetBundle("PARTIALvid01")
        assert b.audio_path() is not None
        assert not b.transcript_path.exists()

    def test_migrate_skips_invalid_video_ids(self, isolated_dirs):
        """文件名无法识别为 video_id 的不应被迁移。"""
        (config.OUTPUT_DIR / "random_file.txt").write_text("x")
        count = assets.migrate_legacy()
        assert count == 0

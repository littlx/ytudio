"""yt.py 纯函数测试:字幕解析、语言选择。"""
from __future__ import annotations

import json

from app import yt


class TestParseSubtitleToText:
    def test_parse_json3_basic(self, tmp_path):
        data = {"events": [
            {"segs": [{"utf8": "Hello "}]},
            {"segs": [{"utf8": "world."}]},
        ]}
        p = tmp_path / "sub.json3"
        p.write_text(json.dumps(data))
        text = yt.parse_subtitle_to_text(str(p))
        assert "Hello" in text and "world" in text

    def test_parse_json3_dedup_adjacent(self, tmp_path):
        """自动字幕常连重复同句,应去重相邻重复。"""
        data = {"events": [
            {"segs": [{"utf8": "same sentence"}]},
            {"segs": [{"utf8": "same sentence"}]},
            {"segs": [{"utf8": "same sentence"}]},
            {"segs": [{"utf8": "different"}]},
        ]}
        p = tmp_path / "sub.json3"
        p.write_text(json.dumps(data))
        text = yt.parse_subtitle_to_text(str(p))
        # 相邻重复只保留一次
        assert text.count("same sentence") == 1
        assert "different" in text

    def test_parse_vtt_basic(self, tmp_path):
        vtt = """WEBVTT

00:00:00.000 --> 00:00:02.000
Hello world.

00:00:02.000 --> 00:00:04.000
This is a test.
"""
        p = tmp_path / "sub.vtt"
        p.write_text(vtt)
        text = yt.parse_subtitle_to_text(str(p))
        assert "Hello world" in text and "This is a test" in text

    def test_parse_vtt_strips_tags(self, tmp_path):
        vtt = """WEBVTT

00:00:00.000 --> 00:00:02.000
<c.Hello>Tagged text</c>
"""
        p = tmp_path / "sub.vtt"
        p.write_text(vtt)
        text = yt.parse_subtitle_to_text(str(p))
        assert "<" not in text and "Tagged text" in text

    def test_parse_empty_events(self, tmp_path):
        data = {"events": []}
        p = tmp_path / "sub.json3"
        p.write_text(json.dumps(data))
        assert yt.parse_subtitle_to_text(str(p)) == ""

    def test_parse_skips_events_without_segs(self, tmp_path):
        data = {"events": [{"segs": [{"utf8": "ok"}]}, {}]}
        p = tmp_path / "sub.json3"
        p.write_text(json.dumps(data))
        text = yt.parse_subtitle_to_text(str(p))
        assert "ok" in text


class TestPickSubtitleLang:
    def test_prefers_english(self):
        info = {"subtitles": {"en": [], "es": []}, "automatic_captions": {}}
        assert yt.pick_subtitle_lang(info) == "en"

    def test_prefers_en_in_auto_captions(self):
        info = {"subtitles": {}, "automatic_captions": {"en": [], "es": []}}
        assert yt.pick_subtitle_lang(info) == "en"

    def test_fallback_to_video_language(self):
        info = {"subtitles": {"ja": []}, "automatic_captions": {"ja": []},
                "language": "ja"}
        assert yt.pick_subtitle_lang(info) == "ja"

    def test_fallback_to_default_audio_language(self):
        info = {"subtitles": {"fr": []}, "automatic_captions": {},
                "default_audio_language": "fr"}
        assert yt.pick_subtitle_lang(info) == "fr"

    def test_no_subs_returns_en(self):
        assert yt.pick_subtitle_lang({}) == "en"

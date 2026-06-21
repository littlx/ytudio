"""cookies.py 校验函数测试。"""
from __future__ import annotations

from app import cookies


class TestValidate:
    def test_empty_content(self):
        ok, msg, count = cookies.validate("")
        assert not ok and count == 0

    def test_whitespace_only(self):
        ok, msg, count = cookies.validate("   \n  ")
        assert not ok

    def test_valid_with_header(self):
        content = """# Netscape HTTP Cookie File
.youtube.com\tTRUE\t/\tTRUE\t1234567890\tkey\tvalue
.youtube.com\tTRUE\t/\tTRUE\t1234567890\tkey2\tvalue2"""
        ok, msg, count = cookies.validate(content)
        assert ok and count == 2

    def test_valid_without_header(self):
        content = ".youtube.com\tTRUE\t/\tTRUE\t1234567890\tkey\tvalue"
        ok, msg, count = cookies.validate(content)
        assert ok and count == 1

    def test_no_cookie_lines(self):
        content = "# Just a comment\n# Another comment"
        ok, msg, count = cookies.validate(content)
        assert not ok and count == 0

    def test_malformed_lines_ignored(self):
        content = """# Netscape HTTP Cookie File
not a cookie line
.youtube.com\tTRUE\t/\tTRUE\t123\tkey\tvalue"""
        ok, msg, count = cookies.validate(content)
        assert ok and count == 1

    def test_message_includes_count(self):
        content = ".youtube.com\tTRUE\t/\tTRUE\t123\tk1\tv1\n.youtube.com\tTRUE\t/\tTRUE\t123\tk2\tv2"
        ok, msg, count = cookies.validate(content)
        assert "2" in msg


class TestSource:
    def test_env_file(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cookies.config, "COOKIES_FILE", "/path/cookies.txt")
        monkeypatch.setattr(cookies.config, "COOKIES_FROM_BROWSER", "")
        # RUNTIME_FILE 指向不存在路径,模拟「无页面上传」
        monkeypatch.setattr(cookies.config, "COOKIES_RUNTIME_FILE", tmp_path / "nonexistent.txt")
        assert cookies.source() == "env"

    def test_browser(self, monkeypatch):
        monkeypatch.setattr(cookies.config, "COOKIES_FILE", "")
        monkeypatch.setattr(cookies.config, "COOKIES_FROM_BROWSER", "chrome")
        assert cookies.source() == "chrome"

    def test_upload(self, monkeypatch, tmp_path):
        runtime = tmp_path / "cookies.txt"
        runtime.write_text("# Netscape")
        monkeypatch.setattr(cookies.config, "COOKIES_FILE", "")
        monkeypatch.setattr(cookies.config, "COOKIES_FROM_BROWSER", "")
        monkeypatch.setattr(cookies.config, "COOKIES_RUNTIME_FILE", runtime)
        assert cookies.source() == "upload"

    def test_none(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cookies.config, "COOKIES_FILE", "")
        monkeypatch.setattr(cookies.config, "COOKIES_FROM_BROWSER", "")
        runtime = tmp_path / "nonexistent.txt"
        monkeypatch.setattr(cookies.config, "COOKIES_RUNTIME_FILE", runtime)
        assert cookies.source() == ""

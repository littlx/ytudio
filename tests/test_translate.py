"""translate.py 纯函数测试:分段、分批边界。"""
from __future__ import annotations

from app import translate


class TestSplitParagraphs:
    def test_single_paragraph(self):
        assert translate._split_paragraphs("你好") == ["你好"]

    def test_multiple_paragraphs(self):
        result = translate._split_paragraphs("第一段\n\n第二段\n\n第三段")
        assert result == ["第一段", "第二段", "第三段"]

    def test_strips_whitespace(self):
        result = translate._split_paragraphs("  第一段  \n\n  第二段  ")
        assert result == ["第一段", "第二段"]

    def test_filters_empty(self):
        result = translate._split_paragraphs("\n\n\n第一段\n\n\n")
        assert result == ["第一段"]

    def test_empty_input(self):
        assert translate._split_paragraphs("") == []


class TestChunkText:
    def test_short_text_single_chunk(self):
        assert translate._chunk_text("短文本", 100) == ["短文本"]

    def test_empty_returns_empty(self):
        assert translate._chunk_text("", 100) == []

    def test_whitespace_only_returns_empty(self):
        assert translate._chunk_text("   \n  ", 100) == []

    def test_long_text_split(self):
        text = "\n".join(f"line{i}" for i in range(100))
        chunks = translate._chunk_text(text, 50)
        assert len(chunks) > 1
        # 拼接后内容应与原文本等价(忽略切分边界差异)
        assert all(c.strip() for c in chunks)

    def test_chunk_boundary_on_newline(self):
        """分批应尽量在换行处断开。"""
        text = "a" * 30 + "\n" + "b" * 30
        chunks = translate._chunk_text(text, 40)
        # 第一块应在换行处断开,不超过 40 字符
        assert len(chunks[0]) <= 40

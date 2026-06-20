"""yt-dlp 封装：提取音频 / 提取字幕 / 元数据。"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yt_dlp

from . import config


@dataclass
class VideoInfo:
    video_id: str
    title: str
    uploader: str
    url: str


def _ydl_base_opts() -> dict[str, Any]:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "no_color": True,
    }
    # 绕过 YouTube 机器人检测：优先用 cookies 文件（环境变量或页面上传），其次从浏览器读取
    cookie_file = config.cookies_file_to_use()
    if cookie_file:
        opts["cookiefile"] = cookie_file
    elif config.COOKIES_FROM_BROWSER:
        opts["cookiesfrombrowser"] = (config.COOKIES_FROM_BROWSER,)
    # 解决 YouTube n-challenge：现代 YouTube 需要远程 JS 组件才能拿到真实音视频流
    if config.REMOTE_COMPONENTS:
        opts["remote_components"] = [c.strip() for c in config.REMOTE_COMPONENTS.split(",") if c.strip()]
    return opts


async def fetch_info(url: str) -> VideoInfo:
    """获取视频元数据（标题、作者、ID）。"""
    def _extract() -> VideoInfo:
        opts = _ydl_base_opts()
        # process=False：只取元数据，不做格式选择，避免命中 iamf 等异常编码报错
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False, process=False)
        return VideoInfo(
            video_id=info.get("id", ""),
            title=info.get("title", "未知标题"),
            uploader=info.get("uploader") or info.get("channel") or "未知作者",
            url=url,
        )

    return await asyncio.to_thread(_extract)


async def extract_audio(url: str, out_path: Path) -> Path:
    """下载原始音频流，保留原有格式（浏览器原生可播 m4a/webm）。

    out_path 不含扩展名；yt-dlp 按实际容器写入 .%(ext)s。
    返回最终生成的文件路径。
    """
    opts = _ydl_base_opts()
    opts.update({
        # 优先纯音频；bestaudio 可能命中浏览器无法播放的格式（如 iamf），
        # 用 ba/b 排序并依靠下面 ext 过滤；不强制转码。
        "format": "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best",
        "outtmpl": str(out_path) + ".%(ext)s",
        # 保留原始格式，不做 FFmpegExtractAudio 转码
    })

    def _run() -> Path:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        # prepare_filename 给出最终落盘文件名
        return Path(ydl.prepare_filename(info))

    return await asyncio.to_thread(_run)


async def extract_subtitle(url: str, out_dir: Path) -> tuple[str, str]:
    """提取英语字幕（模式 B 第 1 步）。

    只请求英语字幕（手动 + 自动生成），单语言下载以规避 YouTube 429 限流。
    后续交由 DeepSeek 翻译整理成中文。
    返回 (字幕文件路径, 字幕语言)。无字幕时抛出 RuntimeError。
    """
    opts = _ydl_base_opts()
    opts.update({
        "skip_download": True,
        "writesubtitles": True,        # 手动字幕
        "writeautomaticsub": True,     # 自动生成字幕
        "subtitleslangs": ["en"],
        "subtitlesformat": "json3",
        "outtmpl": str(out_dir / "%(id)s.%(ext)s"),
    })

    def _run() -> None:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])

    await asyncio.to_thread(_run)

    # 找到刚下载的英语字幕文件：<id>.en.json3
    candidates = sorted(out_dir.glob("*.en.json3"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        # 兜底：任意 en 字幕文件
        candidates = sorted(out_dir.glob("*.en.*"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        raise RuntimeError("该视频没有可用的英语字幕，建议改用「直接提取音频」模式。")

    return str(candidates[0]), "en"


def parse_subtitle_to_text(sub_path: str) -> str:
    """将 json3/vtt 字幕合并为纯文本（去重、合并断句）。"""
    path = Path(sub_path)
    if path.suffix == ".json3":
        return _parse_json3(path)
    if path.suffix in (".vtt", ".srv3"):
        return _parse_vtt(path.read_text(encoding="utf-8", errors="ignore"))
    # 其它格式尝试按 vtt 解析
    return _parse_vtt(path.read_text(encoding="utf-8", errors="ignore"))


def _parse_json3(path: Path) -> str:
    data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    seen: set[str] = set()
    pieces: list[str] = []
    for event in data.get("events", []):
        segs = event.get("segs")
        if not segs:
            continue
        text = "".join(s.get("utf8", "") for s in segs)
        text = _clean_line(text)
        if not text:
            continue
        # 去重相邻重复（自动字幕常重复同句）
        if text in seen:
            continue
        seen.add(text)
        pieces.append(text)
    return _join_sentences(pieces)


def _parse_vtt(content: str) -> str:
    pieces: list[str] = []
    seen: set[str] = set()
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("WEBVTT") or "-->" in line or line.isdigit():
            continue
        # 去除 vtt 内联标签 <c>、<00:00:01.000> 等
        line = re.sub(r"<[^>]+>", "", line)
        line = _clean_line(line)
        if not line or line in seen:
            continue
        seen.add(line)
        pieces.append(line)
    return _join_sentences(pieces)


_TAG_RE = re.compile(r"<[^>]+>")


def _clean_line(text: str) -> str:
    text = _TAG_RE.sub("", text)
    text = text.replace("\n", " ").strip()
    return text


def _join_sentences(pieces: list[str]) -> str:
    """合并碎片为段落，按句末标点断句。"""
    if not pieces:
        return ""
    joined = " ".join(pieces)
    # 规范化多余空白
    joined = re.sub(r"\s+", " ", joined).strip()
    # 在句末标点后适当换行，便于阅读与分批
    joined = re.sub(r"([.!?。！？])\s+", r"\1\n", joined)
    return joined

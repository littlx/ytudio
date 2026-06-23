"""yt-dlp 封装：提取音频 / 提取字幕 / 元数据。"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import yt_dlp

from . import concurrency, config

logger = logging.getLogger(__name__)


@dataclass
class VideoInfo:
    video_id: str
    title: str
    uploader: str
    url: str
    duration: float = 0.0
    subtitle_lang: str = "en"  # 字幕翻译模式实际使用的源语言


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


async def fetch_info(
    url: str,
    bundle: "AssetBundle | None" = None,
    download_thumb: bool = True,
    on_wait: "Callable[[str], None] | None" = None,
) -> VideoInfo:
    """获取视频元数据(标题、作者、ID、时长、可用字幕语言)。

    传入 bundle 时缩略图下载到资产包目录 thumb.%(ext)s;否则下到 output/ 根目录
    (向后兼容,实际调用方均传 bundle)。download_thumb=True 才下载缩略图。
    on_wait 用于在 yt-dlp 全局信号量被占满时透传"等待下载源…"文案。
    """
    from . import assets  # 延迟导入避免循环

    def _extract() -> VideoInfo:
        opts = _ydl_base_opts()
        if download_thumb:
            opts["writethumbnail"] = True
            opts["skip_download"] = True
            # 缩略图落盘到资产包目录,统一命名为 thumb.%(ext)s
            if bundle is not None:
                bundle.ensure_dir()
                opts["outtmpl"] = str(bundle.dir / "thumb.%(ext)s")
            else:
                opts["outtmpl"] = str(config.OUTPUT_DIR / "%(id)s.%(ext)s")
        # process=False:只取元数据,不做格式选择,避免命中 iamf 等异常编码报错
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=download_thumb, process=not download_thumb)
        video_id = info.get("id", "")
        logger.info("获取视频信息成功: id=%s title=%s 字幕语言=%s",
                     video_id, info.get("title", ""), pick_subtitle_lang(info))
        return VideoInfo(
            video_id=video_id,
            title=info.get("title", "未知标题"),
            uploader=info.get("uploader") or info.get("channel") or "未知作者",
            url=url,
            duration=float(info.get("duration") or 0),
            subtitle_lang=pick_subtitle_lang(info),
        )

    async with concurrency.slot("yt", on_wait=on_wait):
        return await asyncio.to_thread(_extract)


async def extract_audio(
    url: str,
    bundle: "AssetBundle",
    on_progress: "Callable[[float, float], None] | None" = None,
    on_wait: "Callable[[str], None] | None" = None,
) -> Path:
    """下载原始音频流到资产包目录,保留原有格式(浏览器原生可播 m4a/webm)。

    最终文件为 bundle.dir/audio.{ext},ext 由 yt-dlp 按实际容器决定。
    on_progress(downloaded_bytes, total_bytes) 回报下载进度(total 为 0 时未知)。
    返回最终生成的文件路径。
    """
    opts = _ydl_base_opts()
    bundle.ensure_dir()
    opts.update({
        # 优先纯音频;bestaudio 可能命中浏览器无法播放的格式(如 iamf),
        # 用 ba/b 排序并依靠下面 ext 过滤;不强制转码。
        "format": "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best",
        "outtmpl": str(bundle.dir / "audio.%(ext)s"),
        # 保留原始格式,不做 FFmpegExtractAudio 转码
    })

    if on_progress is not None:
        def _hook(d):
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                downloaded = d.get("downloaded_bytes") or 0
                try:
                    on_progress(float(downloaded), float(total))
                except Exception:
                    pass
        opts["progress_hooks"] = [_hook]

    def _run() -> Path:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        # prepare_filename 给出最终落盘文件名
        final = Path(ydl.prepare_filename(info))
        logger.info("音频下载完成: id=%s 文件=%s", info.get("id", ""), final.name)
        return final

    async with concurrency.slot("yt", on_wait=on_wait):
        return await asyncio.to_thread(_run)


async def extract_subtitle(
    url: str,
    bundle: "AssetBundle",
    source_lang: str = "en",
    on_wait: "Callable[[str], None] | None" = None,
) -> tuple[str, str]:
    """提取指定语言的字幕到资产包目录(模式 B 第 1 步)。

    单语言下载以规避 YouTube 429 限流。后续交由 DeepSeek 翻译整理成中文。
    返回 (字幕文件路径, 字幕语言)。无字幕时抛出 RuntimeError。

    字幕落盘到 bundle.dir/subtitle.{lang}.{ext},按 video_id 精确定位,
    避免并发任务互相取到对方的字幕文件。
    """
    opts = _ydl_base_opts()
    bundle.ensure_dir()
    opts.update({
        "skip_download": True,
        "writesubtitles": True,        # 手动字幕
        "writeautomaticsub": True,     # 自动生成字幕
        "subtitleslangs": [source_lang, f"{source_lang}-*"],
        "subtitlesformat": "json3",
        "outtmpl": str(bundle.dir / f"subtitle.{bundle.video_id}.%(ext)s"),
    })

    def _run() -> None:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])

    async with concurrency.slot("yt", on_wait=on_wait):
        await asyncio.to_thread(_run)

    # 精确定位:在资产包目录内按语言前缀找字幕文件
    candidates = sorted(
        bundle.dir.glob(f"subtitle.{bundle.video_id}.{source_lang}.*"),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    if not candidates:
        # 兜底:任意 source_lang 字幕文件
        candidates = sorted(
            bundle.dir.glob(f"subtitle.{bundle.video_id}.*{source_lang}*"),
            key=lambda p: p.stat().st_mtime, reverse=True,
        )
    if not candidates:
        logger.warning("字幕提取失败: id=%s 无可用 %s 字幕", bundle.video_id, source_lang)
        raise RuntimeError(
            f"该视频没有可用的 {source_lang} 字幕,建议改用「直接提取音频」模式。"
        )

    logger.info("字幕提取完成: id=%s 文件=%s", bundle.video_id, candidates[0].name)
    return str(candidates[0]), source_lang


def pick_subtitle_lang(info: dict) -> str:
    """从 yt-dlp 元数据选择字幕源语言：优先英语，无则用视频原始语言。"""
    # 优先英语（覆盖面最广，翻译质量最稳）
    subs = info.get("subtitles", {}) or {}
    auto_subs = info.get("automatic_captions", {}) or {}
    available = set(subs.keys()) | set(auto_subs.keys())
    if "en" in available:
        return "en"
    # 回退到视频原始语言
    for key in ("language", "default_audio_language"):
        lang = info.get(key)
        if lang and lang in available:
            return lang
    return "en"


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
    pieces: list[str] = []
    prev: str = ""  # 仅去重「相邻」重复（自动字幕常连重复同句），保留合法的跨段重复
    for event in data.get("events", []):
        segs = event.get("segs")
        if not segs:
            continue
        text = "".join(s.get("utf8", "") for s in segs)
        text = _clean_line(text)
        if not text:
            continue
        if text == prev:
            continue
        prev = text
        pieces.append(text)
    return _join_sentences(pieces)


def _parse_vtt(content: str) -> str:
    pieces: list[str] = []
    prev: str = ""
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("WEBVTT") or "-->" in line or line.isdigit():
            continue
        # 去除 vtt 内联标签 <c>、<00:00:01.000> 等
        line = re.sub(r"<[^>]+>", "", line)
        line = _clean_line(line)
        if not line or line == prev:
            continue
        prev = line
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

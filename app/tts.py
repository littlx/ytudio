"""edge-tts 封装：中文段落列表 → mp3（分段合成 + ffmpeg 拼接）。

译文已由 DeepSeek 按语义分段，这里逐段用 edge-tts 流式合成，
再用 ffmpeg concat 拼接成单个 mp3（无重编码，速度快、无音质损失）。
分段合成避免长文本单次超时，且能逐段回报进度。
"""
from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path
from typing import Callable

import edge_tts

from . import config

logger = logging.getLogger(__name__)

ProgressFn = Callable[[int, int, str], None]


async def _synthesize_one(text: str, voice: str, out_path: Path) -> None:
    """流式合成单段语音到文件。"""
    communicate = edge_tts.Communicate(text, voice)
    with open(out_path, "wb") as f:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])


def _concat_mp3(parts: list[Path], out_path: Path) -> None:
    """用 ffmpeg concat 把多个 mp3 片段拼接成单个 mp3（直接流复制，不重编码）。"""
    # concat demuxer 需要一个文件列表
    with tempfile.NamedTemporaryFile(
        "w", suffix=".txt", delete=False, encoding="utf-8"
    ) as lst:
        for p in parts:
            # ffmpeg concat 列表要求对路径里的单引号转义
            safe = str(p).replace("'", r"'\''")
            lst.write(f"file '{safe}'\n")
        list_file = Path(lst.name)

    import subprocess
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "concat", "-safe", "0",
                "-i", str(list_file),
                "-c", "copy",  # 直接复制流，不重编码
                str(out_path),
            ],
            check=True,
            capture_output=True,
        )
    except FileNotFoundError:
        # ffmpeg 未安装：退化为直接拼接 mp3 字节（多数情况下可正常播放）
        logger.warning("未找到 ffmpeg，退化为字节拼接（可能影响末尾帧）")
        with open(out_path, "wb") as out:
            for p in parts:
                out.write(p.read_bytes())
    except subprocess.CalledProcessError as e:
        # concat 失败时退化为字节拼接，保证至少能产出音频
        logger.warning("ffmpeg concat 失败，退化为字节拼接: %s", e.stderr.decode(errors="ignore")[:200])
        with open(out_path, "wb") as out:
            for p in parts:
                out.write(p.read_bytes())
    finally:
        list_file.unlink(missing_ok=True)


async def synthesize_speech(
    paragraphs: list[str],
    bundle: "AssetBundle",
    voice: str | None = None,
    on_progress: "ProgressFn | None" = None,
) -> Path:
    """把中文段落列表合成为单个 mp3 写入资产包 audio.mp3。

    - 逐段用 edge-tts 流式合成到资产包内临时片段目录;
    - 再用 ffmpeg concat 拼接(无重编码);
    - on_progress(done, total, msg) 按段回报合成进度。
    - 返回最终音频路径(bundle.dir/audio.mp3)。

    单段过长仍可能慢,但段落由模型切分(单段约 ≤300 字),基本不会超时。
    """
    if isinstance(paragraphs, str):
        # 兼容旧调用:传入整串则当作单段
        paragraphs = [paragraphs]
    paragraphs = [p.strip() for p in paragraphs if p and p.strip()]
    if not paragraphs:
        raise RuntimeError("待合成的文本为空。")

    voice = voice or config.TTS_VOICE
    total = len(paragraphs)
    out_path = bundle.dir / "audio.mp3"
    bundle.ensure_dir()

    # 单段直接合成,无需拼接
    if total == 1:
        if on_progress:
            on_progress(0, 1, "合成中文语音…")
        await _synthesize_one(paragraphs[0], voice, out_path)
        if on_progress:
            on_progress(1, 1, "语音合成完成")
        return out_path

    # 多段:逐段合成到资产包内临时目录,再拼接
    tmp_dir = bundle.tts_parts_dir
    tmp_dir.mkdir(exist_ok=True)
    parts: list[Path] = []
    try:
        for i, para in enumerate(paragraphs, 1):
            if on_progress:
                on_progress(i - 1, total, f"合成第 {i}/{total} 段…")
            part = tmp_dir / f"part_{i:05d}.mp3"
            await _synthesize_one(para, voice, part)
            parts.append(part)
        if on_progress:
            on_progress(total, total, "拼接音频…")
        _concat_mp3(parts, out_path)
        if on_progress:
            on_progress(total, total, "语音合成完成")
    finally:
        # 清理临时片段
        for p in parts:
            p.unlink(missing_ok=True)
        tmp_dir.rmdir(missing_ok=True)
    return out_path

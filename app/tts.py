"""edge-tts 封装：中文文本 → mp3。"""
from __future__ import annotations

from pathlib import Path

import edge_tts

from . import config


async def synthesize_speech(
    text: str,
    out_path: Path,
    voice: str | None = None,
) -> None:
    """把中文文本合成为 mp3 写入 out_path。"""
    text = text.strip()
    if not text:
        raise RuntimeError("待合成的文本为空。")

    voice = voice or config.TTS_VOICE
    communicate = edge_tts.Communicate(text, voice)
    # 直接写入文件；edge-tts 会按句切分内部处理
    await communicate.save(str(out_path))

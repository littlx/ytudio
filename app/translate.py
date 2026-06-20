"""DeepSeek API 调用：字幕 → 通顺忠于原意的中文（整篇翻译）。"""
from __future__ import annotations

import httpx

from . import config

# 系统提示词：要求通顺、忠于原意、去口语化、保留结构
SYSTEM_PROMPT = (
    "你是一名专业的视频字幕翻译。请把用户提供的视频字幕翻译成简体中文。"
    "要求：\n"
    "1. 译文必须通顺自然，符合中文表达习惯；\n"
    "2. 忠于原意，不增删信息、不夹带个人观点；\n"
    "3. 去除语气词（如 um、uh、you know）和无意义的口头禅；\n"
    "4. 保留原文的段落与句子结构，按句号、问号、感叹号自然分段；\n"
    "5. 只输出译文本身，不要解释、不要标题、不要任何额外说明。"
)

# 整篇翻译的字符安全上限（来自 config，DeepSeek-V4-Flash 1M 上下文下取 80 万字符）。
# 超过此值才退回分批翻译。绝大多数视频都能整篇翻译。
WHOLE_TRANSLATE_LIMIT: int = config.WHOLE_TRANSLATE_LIMIT


def _chunk_text(text: str, size: int) -> list[str]:
    """超长字幕兜底分批：按字符数切分，尽量在换行/句末边界断开。"""
    if len(text) <= size:
        return [text] if text.strip() else []

    chunks: list[str] = []
    buf = ""
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 > size and buf:
            chunks.append(buf)
            buf = ""
        buf = (buf + "\n" + line) if buf else line
    if buf:
        chunks.append(buf)
    return [c for c in chunks if c.strip()]


async def _call_deepseek(
    client: httpx.AsyncClient,
    headers: dict,
    user_content: str,
) -> str:
    """单次调用 DeepSeek，返回译文文本。"""
    payload = {
        "model": config.DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.3,
        "stream": False,
    }
    resp = await client.post(
        f"{config.DEEPSEEK_BASE_URL}/chat/completions",
        headers=headers,
        json=payload,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"DeepSeek 调用失败 ({resp.status_code}): {resp.text[:300]}"
        )
    data = resp.json()
    content = (
        data.get("choices", [{}])[0]
        .get("message", {})
        .get("content", "")
        .strip()
    )
    if not content:
        raise RuntimeError("DeepSeek 返回为空。")
    return content


async def translate_text(
    text: str,
    on_progress: "callable[[int, int, str], None] | None" = None,
) -> str:
    """把整段字幕翻译成中文。

    默认整篇一次性翻译（上下文最完整、衔接最好）；
    仅当字幕超长（> WHOLE_TRANSLATE_LIMIT）时才分批，逐段调用。
    on_progress(done, total, message) 用于进度回调。
    """
    if not config.has_deepseek_key():
        raise RuntimeError(
            "未配置 DEEPSEEK_API_KEY，请在 .env 中填入后再使用字幕翻译模式。"
        )

    text = text.strip()
    if not text:
        raise RuntimeError("字幕内容为空，无法翻译。")

    headers = {
        "Authorization": f"Bearer {config.DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
        # 整篇翻译
        if len(text) <= WHOLE_TRANSLATE_LIMIT:
            if on_progress:
                on_progress(0, 1, "整篇翻译中…")
            result = await _call_deepseek(
                client, headers,
                "请翻译以下视频字幕，只输出译文：\n\n" + text,
            )
            if on_progress:
                on_progress(1, 1, "翻译完成")
            return result

        # 超长兜底：分批翻译
        chunks = _chunk_text(text, config.TRANSLATE_CHUNK_SIZE)
        total = len(chunks)
        results: list[str] = []
        for i, chunk in enumerate(chunks, 1):
            if on_progress:
                on_progress(i - 1, total, f"正在翻译第 {i}/{total} 段…")
            content = await _call_deepseek(
                client, headers,
                "请翻译以下字幕片段，注意与前后文衔接，只输出译文：\n\n" + chunk,
            )
            results.append(content)
            if on_progress:
                on_progress(i, total, f"已完成第 {i}/{total} 段")
        if on_progress:
            on_progress(total, total, "翻译完成")
        return "\n".join(results)

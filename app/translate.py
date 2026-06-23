"""DeepSeek API 调用：字幕 → 通顺忠于原意的中文（整篇翻译，按段落输出）。

核心思路：让模型在翻译时就按语义把译文分成自然段落，段间用双换行分隔。
这样下游 TTS 可直接按段分片合成——分段由模型语义判断，比机械切字准确得多，
既避免长文本单次合成超时，又保证每段语音连贯。
"""
from __future__ import annotations

import asyncio
import logging

import httpx

from . import concurrency, config

logger = logging.getLogger(__name__)

# 段落分隔符：要求模型在译文段与段之间输出双换行。双换行在自然中文里极少出现，
# 可作为可靠切分依据；同时 TTS 分段合成也以此为界。
PARA_SEP = "\n\n"

# 系统提示词：要求通顺、忠于原意、去口语化、按段落输出
SYSTEM_PROMPT = (
    "你是一名专业的视频字幕翻译。请把用户提供的视频字幕翻译成简体中文。"
    "要求：\n"
    "1. 译文必须通顺自然，符合中文表达习惯；\n"
    "2. 忠于原意，不增删信息、不夹带个人观点；\n"
    "3. 去除语气词（如 um、uh、you know）和无意义的口头禅；\n"
    "4. 按语义把译文分成若干自然段落，每段是一两个完整句子，段与段之间用空行隔开；"
    "段落划分以语义完整为准，不要过短（避免碎句）也不要过长（单段不超过约 300 字）；\n"
    "5. 只输出译文本身，不要解释、不要标题、不要任何额外说明。"
)

# 整篇翻译的字符安全上限（来自 config，DeepSeek-V4-Flash 1M 上下文下取 80 万字符）。
# 超过此值才退回分批翻译。绝大多数视频都能整篇翻译。
WHOLE_TRANSLATE_LIMIT: int = config.WHOLE_TRANSLATE_LIMIT

# 瞬态错误重试：429 限流 / 5xx 服务端错误
_RETRY_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRIES = 3


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


def _split_paragraphs(text: str) -> list[str]:
    """把模型译文按双换行切成段落，过滤空段。"""
    paras = [p.strip() for p in text.split(PARA_SEP)]
    return [p for p in paras if p]


async def _call_deepseek(
    client: httpx.AsyncClient,
    headers: dict,
    user_content: str,
) -> str:
    """单次调用 DeepSeek，返回译文文本。

    对 429/5xx 瞬态错误做指数退避重试（最多 3 次）。
    """
    payload = {
        "model": config.DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.3,
        "stream": False,
    }
    last_err: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            resp = await client.post(
                f"{config.DEEPSEEK_BASE_URL}/chat/completions",
                headers=headers,
                json=payload,
            )
            if resp.status_code in _RETRY_STATUS and attempt < _MAX_RETRIES - 1:
                wait = 2 ** attempt  # 1s, 2s, 4s
                logger.warning("DeepSeek 返回 %d，%ds 后重试（第 %d 次）", resp.status_code, wait, attempt + 1)
                await asyncio.sleep(wait)
                continue
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
        except httpx.HTTPError as e:
            # 网络层错误也重试
            last_err = e
            if attempt < _MAX_RETRIES - 1:
                await asyncio.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"DeepSeek 网络错误: {e}") from e
    raise RuntimeError(f"DeepSeek 调用失败（重试耗尽）: {last_err}")


async def translate_text(
    text: str,
    on_progress: "Callable[[int, int, str], None] | None" = None,
    on_wait: "Callable[[str], None] | None" = None,
) -> list[str]:
    """把整段字幕翻译成中文，返回**段落列表**（按语义分段，供 TTS 分片合成）。

    默认整篇一次性翻译（上下文最完整、衔接最好）；
    仅当字幕超长（> WHOLE_TRANSLATE_LIMIT）时才分批，逐段调用后合并段落。
    on_progress(done, total, message) 用于进度回调，按批数计。
    on_wait 用于在 DeepSeek 信号量被占满时透传"等待翻译资源…"文案。
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

    async with concurrency.slot("translate", on_wait=on_wait):
        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
            # 整篇翻译
            if len(text) <= WHOLE_TRANSLATE_LIMIT:
                if on_progress:
                    on_progress(0, 1, "整篇翻译中…")
                result = await _call_deepseek(
                    client, headers,
                    "请翻译以下视频字幕，只输出译文，并按段落用空行分隔：\n\n" + text,
                )
                if on_progress:
                    on_progress(1, 1, "翻译完成")
                paras = _split_paragraphs(result)
                return paras if paras else [result.strip()]

            # 超长兜底：分批翻译，合并所有段落
            chunks = _chunk_text(text, config.TRANSLATE_CHUNK_SIZE)
            total = len(chunks)
            all_paras: list[str] = []
            for i, chunk in enumerate(chunks, 1):
                if on_progress:
                    on_progress(i - 1, total, f"正在翻译第 {i}/{total} 段…")
                content = await _call_deepseek(
                    client, headers,
                    "请翻译以下字幕片段，注意与前后文衔接，只输出译文，并按段落用空行分隔：\n\n" + chunk,
                )
                all_paras.extend(_split_paragraphs(content))
                if on_progress:
                    on_progress(i, total, f"已完成第 {i}/{total} 段")
            if on_progress:
                on_progress(total, total, "翻译完成")
            return all_paras if all_paras else ["翻译结果为空"]

"""三类外部重资源的全局并发限流。

任务级仍是完全并发,这里只对调用 yt-dlp / DeepSeek / edge-tts 的关键步骤限流,
分别避免 YouTube IP 限流、DeepSeek 账号 RPM、Edge TTS 端点压力。

用法::

    from . import concurrency

    async with concurrency.slot("yt", on_wait=lambda res: report(0.0, "等待下载源…")):
        ...  # 实际的 yt-dlp 调用

`Semaphore` 必须绑定到当前 event loop,所以采用懒初始化:首次使用时按当前
running loop 创建。后续若 loop 变化(测试场景)会重建,避免「跨 loop 复用」报错。
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Callable

from . import config

logger = logging.getLogger(__name__)

# 资源名 -> 并发上限(从 config 取,启动后改 .env 不会热生效,需重启)
_LIMITS: dict[str, int] = {
    "yt": config.YTDLP_CONCURRENCY,
    "translate": config.TRANSLATE_CONCURRENCY,
    "tts": config.TTS_CONCURRENCY,
}

# 资源名 -> (Semaphore, 所绑定的 loop)。绑定 loop 后,
# 若发现当前 loop 不一致(测试/重启),自动重建避免 RuntimeError。
_sems: dict[str, tuple[asyncio.Semaphore, asyncio.AbstractEventLoop]] = {}


def _get_sem(resource: str) -> asyncio.Semaphore:
    if resource not in _LIMITS:
        raise ValueError(f"未知并发资源: {resource}")
    loop = asyncio.get_running_loop()
    pair = _sems.get(resource)
    if pair is None or pair[1] is not loop:
        sem = asyncio.Semaphore(_LIMITS[resource])
        _sems[resource] = (sem, loop)
        return sem
    return pair[0]


@asynccontextmanager
async def slot(resource: str, on_wait: Callable[[str], None] | None = None):
    """获取 resource 槽位。被阻塞时调用一次 on_wait(resource) 报告等待状态。

    on_wait 仅在「需要等待」时触发(Semaphore 已无空闲),便于上层把
    "等待 xxx 资源空闲…" 文案透到 SSE。等到槽位后正常进入临界区。
    """
    sem = _get_sem(resource)
    waited = False
    if sem.locked() and on_wait is not None:
        try:
            on_wait(resource)
        except Exception:  # noqa: BLE001 - 回调异常不应影响主流程
            logger.debug("on_wait 回调异常(已忽略)", exc_info=True)
        waited = True
    async with sem:
        if waited:
            logger.debug("资源 %s 获得槽位", resource)
        yield


def limits() -> dict[str, int]:
    """返回当前各资源的并发上限(供调试/状态接口展示)。"""
    return dict(_LIMITS)

"""可组合的处理步骤链:把两种模式(直接提取音频 / 字幕翻译→TTS)拆为
独立步骤,按权重自动归一化进度,公共步骤(FetchInfo/SaveMeta)复用。

取代 pipeline.py 里两个过程式大函数中硬编码的进度魔法数字:每步声明
``weight``,Pipeline 按总权重把进度映射到 [start_pct, end_pct] 区间,
步骤内部只需报告自身 0~1 的完成比例。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from . import assets, translate, tts, yt
from .pipeline import TaskResult, TaskState

logger = logging.getLogger(__name__)

# 进度区间:任务从 5% 起步(留 0~5 给「提交任务」阶段),到 100% 完成
_PROGRESS_START = 5
_PROGRESS_END = 100

# 步骤内进度报告:(本步完成比例 0~1, 消息) -> None
StepReport = Callable[[float, str], None]
# 状态变更通知:每次 report 后触发,供外部推送 SSE
Emit = Callable[[], None]


@dataclass
class Ctx:
    """步骤间共享的执行上下文,由 Pipeline 在步骤间传递。"""

    url: str
    mode: str
    voice: str = ""
    state: TaskState = field(default_factory=TaskState)
    info: yt.VideoInfo | None = None       # 由 FetchInfo 填充
    bundle: assets.AssetBundle | None = None  # 由 FetchInfo 填充(用真实 video_id)
    paragraphs: list[str] = field(default_factory=list)  # TTS 中间产物
    audio_ext: str = ".mp3"                # 最终音频扩展名


@dataclass
class Step:
    """单个处理步骤。

    weight: 该步占总进度的权重(相对值,与其它步骤的 weight 一起归一化)。
    run: async (ctx, report) -> None,report(ratio, msg) 报告本步内进度。
    """

    stage: str
    weight: int
    run: Callable[[Ctx, StepReport], Awaitable[None]]


class Pipeline:
    """步骤链执行器:按 weight 归一化进度,顺序执行各步骤。"""

    def __init__(self, steps: list[Step]):
        self.steps = steps
        self._total_weight = sum(s.weight for s in steps)

    async def execute(self, ctx: Ctx, emit: Emit | None = None) -> TaskResult:
        """顺序执行所有步骤,返回 TaskResult。

        进度按 weight 分配:每步占 [start, start+weight/total*range] 区间。
        步骤内 report(ratio, msg) 自动映射到该区间,并通过 emit 通知外部。
        """
        state = ctx.state
        weight_left = self._total_weight
        pct_cursor = _PROGRESS_START
        span = _PROGRESS_END - _PROGRESS_START

        for step in self.steps:
            step_span = span * step.weight / self._total_weight if self._total_weight else 0
            step_start = pct_cursor
            step_end = pct_cursor + step_span
            weight_left -= step.weight

            def report(ratio: float, msg: str, _start=step_start, _end=step_end,
                       _stage=step.stage, _state=state, _ctx=ctx, _emit=emit):
                ratio = max(0.0, min(1.0, ratio))
                _state.stage = _stage
                _state.percent = int(_start + (_end - _start) * ratio)
                _state.message = msg
                _maybe_emit_meta(_state, _ctx)
                if _emit is not None:
                    _emit()

            state.stage = step.stage
            state.message = "处理中…"
            if emit is not None:
                emit()
            await step.run(ctx, report)
            pct_cursor = step_end if weight_left > 0 else _PROGRESS_END

        return _build_result(ctx)


def _maybe_emit_meta(state: TaskState, ctx: Ctx) -> None:
    """FetchInfo 步骤填充 ctx.info 后,同步到 state 供前端尽早展示。"""
    if ctx.info is not None:
        state.video_id = ctx.info.video_id
        state.title = ctx.info.title
        state.uploader = ctx.info.uploader


def _build_result(ctx: Ctx) -> TaskResult:
    """从上下文构建最终结果。"""
    assert ctx.info is not None and ctx.bundle is not None, "FetchInfo 必须先执行"
    return TaskResult(
        task_id=ctx.state.task_id,
        mode=ctx.mode,
        video_id=ctx.info.video_id,
        title=ctx.info.title,
        uploader=ctx.info.uploader,
        bundle=ctx.bundle,
        audio_ext=ctx.audio_ext,
        duration=ctx.info.duration,
        source_lang=ctx.info.subtitle_lang if ctx.mode == "tts" else "",
    )


# ============================================================
# 公共步骤(两种模式共用)
# ============================================================

async def _fetch_info(ctx: Ctx, report: StepReport) -> None:
    """获取视频元数据 + 下载缩略图,填充 ctx.info / ctx.bundle。"""
    report(0.1, "获取视频信息…")
    # fetch_info 前尚不知 video_id,用 task_id 作临时占位目录
    pending = assets.AssetBundle(f".pending_{ctx.state.task_id}")
    info = await yt.fetch_info(ctx.url, bundle=pending)
    ctx.info = info
    # 用真实 video_id 重建 bundle;pending 目录若残留由取消清理
    ctx.bundle = assets.AssetBundle(info.video_id)
    # 若 pending 目录已创建(如下了缩略图),把内容搬到正式目录
    if pending.dir.exists():
        ctx.bundle.ensure_dir()
        for p in pending.dir.iterdir():
            if p.is_file():
                target = ctx.bundle.dir / p.name
                if not target.exists():
                    p.rename(target)
        try:
            pending.dir.rmdir()
        except OSError:
            pass
    report(1.0, f"已获取: {info.title}")


async def _save_meta(ctx: Ctx, report: StepReport) -> None:
    """保存元数据到资产包 meta.json + 更新历史索引。"""
    report(0.5, "保存元数据…")
    # 实际保存逻辑由 pipeline._save_metadata 完成(避免循环导入),
    # 这里通过 ctx.result 触发——但 result 在 execute 末尾构建。
    # 因此 SaveMeta 步骤只做进度报告,实际保存在 pipeline.run 中调用。
    report(1.0, "完成")


# ============================================================
# audio 模式步骤
# ============================================================

async def _download_audio(ctx: Ctx, report: StepReport) -> None:
    """下载原始音频流到资产包。"""
    assert ctx.info is not None and ctx.bundle is not None
    report(0.0, f"下载音频: {ctx.info.title}")

    def _on_dl(downloaded: float, total: float) -> None:
        if total > 0:
            report(min(downloaded / total, 1.0),
                   f"下载音频: {int(downloaded / 1024 / 1024)}MB / {int(total / 1024 / 1024)}MB")
        else:
            report(0.5, f"下载音频: {int(downloaded / 1024 / 1024)}MB")

    final = await yt.extract_audio(ctx.url, ctx.bundle, on_progress=_on_dl)
    if not final.exists():
        raise RuntimeError("音频提取失败,文件未生成。")
    ctx.audio_ext = final.suffix
    report(1.0, "音频下载完成")


# ============================================================
# tts 模式步骤
# ============================================================

async def _extract_subtitle(ctx: Ctx, report: StepReport) -> None:
    """提取字幕到资产包。"""
    assert ctx.info is not None and ctx.bundle is not None
    report(0.0, "提取字幕…")
    await yt.extract_subtitle(ctx.url, ctx.bundle, source_lang=ctx.info.subtitle_lang)
    report(1.0, "字幕提取完成")


async def _parse_subtitle(ctx: Ctx, report: StepReport) -> None:
    """解析字幕为纯文本。"""
    assert ctx.bundle is not None
    report(0.0, "解析字幕…")
    sub_file = ctx.bundle.subtitle_file()
    if sub_file is None:
        raise RuntimeError("字幕文件未找到,无法解析。")
    raw_text = await asyncio.to_thread(yt.parse_subtitle_to_text, str(sub_file))
    if not raw_text.strip():
        raise RuntimeError("字幕解析后内容为空,无法继续。")
    ctx._raw_text = raw_text  # type: ignore[attr-defined]  传递给翻译步骤
    report(1.0, "字幕解析完成")


async def _translate(ctx: Ctx, report: StepReport) -> None:
    """翻译字幕为中文段落列表。"""
    report(0.0, "开始翻译…")
    raw_text = getattr(ctx, "_raw_text", "")
    if not raw_text:
        raise RuntimeError("无字幕文本可供翻译。")

    def _on_trans(done: int, total: int, msg: str) -> None:
        report(done / total if total > 0 else 0.5, msg)

    paragraphs = await translate.translate_text(raw_text, on_progress=_on_trans)
    ctx.paragraphs = paragraphs

    # 保存译文文稿
    try:
        ctx.bundle.ensure_dir()
        ctx.bundle.transcript_path.write_text("\n\n".join(paragraphs), encoding="utf-8")
    except Exception as e:
        logger.warning("保存译文文稿失败: %s", e)
    report(1.0, "翻译完成")


async def _synthesize(ctx: Ctx, report: StepReport) -> None:
    """逐段合成中文语音并拼接。"""
    assert ctx.bundle is not None
    report(0.0, "合成中文语音…")

    def _on_tts(done: int, total: int, msg: str) -> None:
        report(done / total if total > 0 else 0.5, msg)

    final = await tts.synthesize_speech(
        ctx.paragraphs, ctx.bundle, voice=ctx.voice or None, on_progress=_on_tts
    )
    ctx.audio_ext = final.suffix
    report(1.0, "语音合成完成")


# ============================================================
# 两种模式的 Pipeline 组装
# ============================================================

AUDIO_PIPELINE = Pipeline([
    Step("fetching", 5, _fetch_info),
    Step("downloading", 90, _download_audio),
    Step("done", 5, _save_meta),
])

TTS_PIPELINE = Pipeline([
    Step("fetching", 5, _fetch_info),
    Step("subtitling", 10, _extract_subtitle),
    Step("subtitling", 5, _parse_subtitle),
    Step("translating", 45, _translate),
    Step("synthesizing", 30, _synthesize),
    Step("done", 5, _save_meta),
])

_PIPELINES = {
    "audio": AUDIO_PIPELINE,
    "tts": TTS_PIPELINE,
}


def get_pipeline(mode: str) -> Pipeline:
    """按模式返回对应的 Pipeline。"""
    if mode not in _PIPELINES:
        raise ValueError(f"未知模式: {mode}")
    return _PIPELINES[mode]

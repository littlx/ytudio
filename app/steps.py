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

# 三个外部资源等待文案:concurrency.slot 被阻塞时透出
_WAIT_LABEL = {
    "yt": "等待下载源空闲…",
    "translate": "等待翻译额度空闲…",
    "tts": "等待 TTS 通道空闲…",
}


def _make_on_wait(report: "StepReport") -> Callable[[str], None]:
    """构造 on_wait 回调:把"等待 xxx 资源"消息透到当前步骤的进度。

    ratio 保持当前步骤起点(0.0),只换 message,避免回退百分比。
    """
    def _on_wait(resource: str) -> None:
        report(0.0, _WAIT_LABEL.get(resource, f"等待 {resource} 空闲…"))
    return _on_wait


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

    async def execute(self, ctx: Ctx, emit: Emit | None = None, resume: bool = False) -> TaskResult:
        """顺序执行所有步骤,返回 TaskResult。

        resume=True 时,跳过 progress.json 中记录的已完成步骤,从断点继续。
        每步成功后向 progress.json 追加完成标记;全部完成后删除 progress.json。
        """
        state = ctx.state
        task_id = state.task_id
        # 断点续传:读取已完成步骤索引(用索引而非 stage 名,避免同 stage 步骤冲突)
        completed_idx: set[int] = set()
        if resume and ctx.bundle is not None:
            prog = ctx.bundle.load_progress()
            if prog:
                completed_idx = set(prog.get("completed_steps", []))
                logger.info("任务 %s 断点续传:已完成 %d 个步骤,将从断点继续", task_id, len(completed_idx))

        weight_left = self._total_weight
        pct_cursor = _PROGRESS_START
        span = _PROGRESS_END - _PROGRESS_START

        for idx, step in enumerate(self.steps):
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

            # 断点续传:跳过已完成步骤,恢复其产出到 ctx
            if idx in completed_idx:
                logger.info("任务 %s 跳过已完成步骤: %s(#%d)", task_id, step.stage, idx)
                _restore_step_output(ctx, step.stage)
                pct_cursor = step_end if weight_left > 0 else _PROGRESS_END
                continue

            logger.info("任务 %s 开始步骤: %s", task_id, step.stage)
            try:
                await step.run(ctx, report)
            except Exception as e:
                logger.error("任务 %s 步骤 %s 失败: %s", task_id, step.stage, e, exc_info=True)
                raise
            logger.info("任务 %s 完成步骤: %s", task_id, step.stage)

            # 记录步骤完成到 progress.json(供断点续传,用步骤索引)
            if ctx.bundle is not None:
                _mark_step_done(ctx, idx)

            pct_cursor = step_end if weight_left > 0 else _PROGRESS_END

        # 全部完成:清除进度文件
        if ctx.bundle is not None and ctx.bundle.progress_path.exists():
            ctx.bundle.clear_progress()
            logger.info("任务 %s 全部步骤完成,已清除进度文件", task_id)

        return _build_result(ctx)


def _maybe_emit_meta(state: TaskState, ctx: Ctx) -> None:
    """FetchInfo 步骤填充 ctx.info 后,同步到 state 供前端尽早展示。"""
    if ctx.info is not None:
        state.video_id = ctx.info.video_id
        state.title = ctx.info.title
        state.uploader = ctx.info.uploader


def _restore_step_output(ctx: Ctx, stage: str) -> None:
    """断点续传跳过已完成步骤时,从 progress.json / 资产包恢复该步产出到 ctx。

    各步骤产出恢复方式见计划文档表格。关键是 ctx.info / ctx.audio_ext
    / ctx.paragraphs 这三个跨步骤共享的内存态。
    """
    assert ctx.bundle is not None
    prog = ctx.bundle.load_progress() or {}

    if stage == "fetching":
        # 从 progress.json 恢复 info
        info_data = prog.get("info")
        if info_data:
            ctx.info = yt.VideoInfo(
                video_id=info_data.get("video_id", ""),
                title=info_data.get("title", "未知标题"),
                uploader=info_data.get("uploader", "未知作者"),
                url=info_data.get("url", ctx.url),
                duration=float(info_data.get("duration", 0)),
                subtitle_lang=info_data.get("subtitle_lang", "en"),
            )
            _maybe_emit_meta(ctx.state, ctx)

    elif stage == "downloading":
        # audio_ext 从 progress.json 恢复;音频文件已在 bundle
        ctx.audio_ext = prog.get("audio_ext", ".mp3")

    elif stage == "subtitling":
        # 字幕提取/解析步骤。提取步骤产物(字幕文件)已在 bundle 无需恢复;
        # 解析步骤产物(raw_text)在内存,跳过时从 subtitle 文件重新解析,
        # 供后续翻译步骤使用。两步同 stage,这里统一处理:确保 raw_text 可用。
        sub_file = ctx.bundle.subtitle_file()
        if sub_file is not None and not getattr(ctx, "_raw_text", ""):
            try:
                ctx._raw_text = yt.parse_subtitle_to_text(str(sub_file))  # type: ignore[attr-defined]
            except Exception:
                pass  # 解析失败不阻塞(后续翻译步骤会兜底报错)

    elif stage == "translating":
        # paragraphs 从 transcript_zh.txt 按 \\n\\n 还原
        if ctx.bundle.transcript_path.exists():
            text = ctx.bundle.transcript_path.read_text(encoding="utf-8")
            ctx.paragraphs = [p for p in text.split("\n\n") if p.strip()]

    elif stage == "synthesizing":
        # audio_ext 从 progress.json 恢复;音频文件已在 bundle
        ctx.audio_ext = prog.get("audio_ext", ".mp3")


def _mark_step_done(ctx: Ctx, idx: int) -> None:
    """把已完成的步骤索引 + 关键产出快照写入 progress.json,供断点续传恢复。

    每步完成后追加 idx 到 completed_steps,并刷新 info/audio_ext 快照
    (这两个是跨步骤共享、且无法从资产包文件直接恢复的内存态)。
    用步骤索引(而非 stage 名)作 key,避免同 stage 步骤(如 tts 模式两个
    subtitling 步骤)冲突。
    """
    assert ctx.bundle is not None
    prog = ctx.bundle.load_progress() or {}
    # 保留原有字段,补全任务参数(首次写入时)
    prog.setdefault("task_id", ctx.state.task_id)
    prog.setdefault("mode", ctx.mode)
    prog.setdefault("url", ctx.url)
    prog.setdefault("voice", ctx.voice)
    completed = list(prog.get("completed_steps", []))
    if idx not in completed:
        completed.append(idx)
    prog["completed_steps"] = completed
    # info 快照(FetchInfo 产出,重试时恢复 ctx.info)
    if ctx.info is not None:
        prog["info"] = {
            "video_id": ctx.info.video_id,
            "title": ctx.info.title,
            "uploader": ctx.info.uploader,
            "url": ctx.info.url,
            "duration": ctx.info.duration,
            "subtitle_lang": ctx.info.subtitle_lang,
        }
    # audio_ext 快照(下载/合成产出,重试时恢复 ctx.audio_ext)
    if ctx.audio_ext:
        prog["audio_ext"] = ctx.audio_ext
    prog["updated_at"] = __import__("time").time()
    ctx.bundle.save_progress(prog)


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
    info = await yt.fetch_info(ctx.url, bundle=pending, on_wait=_make_on_wait(report))
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

    final = await yt.extract_audio(ctx.url, ctx.bundle, on_progress=_on_dl, on_wait=_make_on_wait(report))
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
    await yt.extract_subtitle(ctx.url, ctx.bundle, source_lang=ctx.info.subtitle_lang, on_wait=_make_on_wait(report))
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

    paragraphs = await translate.translate_text(raw_text, on_progress=_on_trans, on_wait=_make_on_wait(report))
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
        ctx.paragraphs, ctx.bundle, voice=ctx.voice or None,
        on_progress=_on_tts, on_wait=_make_on_wait(report),
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

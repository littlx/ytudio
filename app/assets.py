"""资产包:把单个视频的所有产物(音频/缩略图/字幕/译文/元数据)收敛到
``output/{video_id}/`` 目录,统一寻址与生命周期管理。

取代旧版「散落文件 + 文件名约定」的方案:
- 旧版各产物平铺在 output/ 下,靠 ``{video_id}_zh.mp3`` / ``{video_id}_audio.m4a``
  等命名约定关联,删除时要从文件名反解 video_id,脆弱且易漏文件。
- 新版每个 video_id 一个目录,删除即 ``rmdir``,元数据集中在 meta.json,
  history.json 退化为纯索引。
"""
from __future__ import annotations

import json
import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import config

logger = logging.getLogger(__name__)

# 音频/缩略图的实际落盘扩展名(yt-dlp 决定,非固定)
_AUDIO_EXTS = (".mp3", ".m4a", ".webm", ".mp4", ".ogg", ".wav", ".opus")
_THUMB_EXTS = (".jpg", ".webp", ".png")
_SUBTITLE_EXTS = (".json3", ".vtt", ".srv3")
_MIGRATE_FLAG = ".migrated"  # 迁移完成标记,避免重复执行


@dataclass
class AssetBundle:
    """单个视频的资产包:output/{video_id}/ 下的全部产物。"""

    video_id: str

    @property
    def dir(self) -> Path:
        return config.OUTPUT_DIR / self.video_id

    # ---- 各产物路径(惰性计算,文件未必存在)----
    @property
    def meta_path(self) -> Path:
        return self.dir / "meta.json"

    @property
    def transcript_path(self) -> Path:
        return self.dir / "transcript_zh.txt"

    @property
    def progress_path(self) -> Path:
        """断点续传进度文件:记录已完成步骤 + 关键产出快照。

        与 meta.json 区别:meta.json 是任务成功完成后的终态元数据;
        progress.json 记录任务进行中的步骤进度,供失败后断点重试。
        任务成功后会被清除。
        """
        return self.dir / "progress.json"

    @property
    def subtitle_path(self) -> Path:
        """字幕实际文件名带语言后缀(如 subtitle.en.json3),这里做模式匹配。"""
        return self.dir / "subtitle.json3"

    @property
    def tts_parts_dir(self) -> Path:
        """TTS 逐段合成的临时片段目录(合成完成后删除)。"""
        return self.dir / ".tts_parts"

    def audio_path(self) -> Path | None:
        """返回资产包内实际存在的音频文件路径,无则 None。"""
        if not self.dir.exists():
            return None
        for ext in _AUDIO_EXTS:
            p = self.dir / f"audio{ext}"
            if p.exists():
                return p
        return None

    def thumb_path(self) -> Path | None:
        """返回缩略图路径,无则 None。"""
        if not self.dir.exists():
            return None
        for ext in _THUMB_EXTS:
            p = self.dir / f"thumb{ext}"
            if p.exists():
                return p
        return None

    def subtitle_file(self) -> Path | None:
        """返回字幕文件(允许带语言后缀,如 subtitle.en.json3),无则 None。"""
        if not self.dir.exists():
            return None
        # 优先规范名 subtitle.json3,其次 subtitle.*.json3/vtt
        for p in sorted(self.dir.glob("subtitle*")):
            if p.is_file() and p.suffix in _SUBTITLE_EXTS:
                return p
        return None

    # ---- 生命周期 ----
    def ensure_dir(self) -> Path:
        """创建资产包目录(幂等)。"""
        self.dir.mkdir(parents=True, exist_ok=True)
        return self.dir

    def exists(self) -> bool:
        """资产包目录存在且有元数据。"""
        return self.meta_path.exists()

    def meta(self) -> dict | None:
        """读取元数据,损坏或不存在返回 None。"""
        if not self.meta_path.exists():
            return None
        try:
            data = json.loads(self.meta_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("读取资产包元数据失败 %s: %s", self.video_id, e)
            return None

    def save_meta(self, data: dict) -> None:
        """原子写入元数据。"""
        self.ensure_dir()
        tmp = self.dir / ".meta.json.tmp"
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.meta_path)

    def load_progress(self) -> dict | None:
        """读取断点续传进度;文件不存在或损坏返回 None。"""
        if not self.progress_path.exists():
            return None
        try:
            data = json.loads(self.progress_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("读取进度文件失败 %s: %s", self.video_id, e)
            return None

    def save_progress(self, data: dict) -> None:
        """原子写入断点续传进度。"""
        self.ensure_dir()
        tmp = self.dir / ".progress.json.tmp"
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.progress_path)

    def clear_progress(self) -> None:
        """删除进度文件(任务成功完成后调用)。"""
        self.progress_path.unlink(missing_ok=True)

    def remove(self) -> list[str]:
        """删除整个资产包目录,返回被删文件名列表(用于前端反馈)。"""
        deleted: list[str] = []
        if not self.dir.exists():
            return deleted
        for p in self.dir.iterdir():
            if p.is_file():
                deleted.append(p.name)
        try:
            shutil.rmtree(self.dir)
        except OSError as e:
            logger.warning("删除资产包失败 %s: %s", self.video_id, e)
        return deleted


def list_bundles() -> list[AssetBundle]:
    """列出所有有效资产包(有 meta.json 的目录),按 created_at 降序。"""
    bundles: list[tuple[str, dict]] = []
    if not config.OUTPUT_DIR.exists():
        return []
    for d in config.OUTPUT_DIR.iterdir():
        if not d.is_dir() or d.name.startswith("."):
            continue
        b = AssetBundle(d.name)
        meta = b.meta()
        if meta is not None:
            bundles.append((d.name, meta))
    # 按 created_at 降序,无的排最后
    bundles.sort(key=lambda x: x[1].get("created_at") or "", reverse=True)
    return [AssetBundle(vid) for vid, _ in bundles]


# ============================================================
# 旧版散落文件迁移
# ============================================================

# 旧版音频命名:{video_id}_zh.mp3 (TTS) / {video_id}_audio.{ext} (audio 模式)
_ZH_AUDIO_RE = re.compile(r"^(.+?)_zh\.(mp3|m4a|webm|mp4|ogg|wav|opus)$")
_AUDIO_MODE_RE = re.compile(r"^(.+?)_audio\.(m4a|webm|mp4|ogg|wav|opus|mp3)$")
# 旧版译文:{video_id}_zh.txt
_ZH_TXT_RE = re.compile(r"^(.+?)_zh\.txt$")
# 旧版元数据:{video_id}_zh.json / {video_id}_audio.json
_ZH_META_RE = re.compile(r"^(.+?)_zh\.json$")
_AUDIO_META_RE = re.compile(r"^(.+?)_audio\.json$")
# 旧版字幕:{video_id}.{lang}.json3/vtt
_SUBTITLE_RE = re.compile(r"^(.+?)\.[a-zA-Z\-]{2,}\.(json3|vtt|srv3)$")
# 旧版缩略图:{video_id}.{ext}
_THUMB_RE = re.compile(r"^(.+?)\.(jpg|webp|png)$")


def _is_valid_video_id(name: str) -> bool:
    """YouTube video_id 通常是 11 位 [A-Za-z0-9_-],宽松校验避免误迁。"""
    return bool(re.fullmatch(r"[A-Za-z0-9_\-]{6,}", name))


def migrate_legacy() -> int:
    """把旧版散落在 output/ 根目录的文件归入 {video_id}/ 资产包目录。

    返回迁移的资产包数量。已迁移(存在 .migrated 标记)则跳过。
    迁移规则按文件名匹配 video_id,同 video_id 的文件归到同一目录。
    """
    if not config.OUTPUT_DIR.exists():
        return 0
    if (config.OUTPUT_DIR / _MIGRATE_FLAG).exists():
        return 0

    # 收集每个 video_id 对应的旧文件:{video_id: {类型: 路径}}
    groups: dict[str, dict[str, Path]] = {}
    # 记录已处理的文件,迁移后删除
    handled: list[Path] = []

    def _group(vid: str, kind: str, path: Path) -> None:
        if not _is_valid_video_id(vid):
            return
        groups.setdefault(vid, {})[kind] = path

    for p in sorted(config.OUTPUT_DIR.iterdir()):
        if not p.is_file():
            continue
        name = p.name
        # 元数据优先判定(避免 _zh.json 被当 thumb 等)
        if m := _ZH_META_RE.match(name):
            _group(m.group(1), "meta", p); handled.append(p); continue
        if m := _AUDIO_META_RE.match(name):
            _group(m.group(1), "meta", p); handled.append(p); continue
        if m := _ZH_AUDIO_RE.match(name):
            _group(m.group(1), "audio", p); handled.append(p); continue
        if m := _AUDIO_MODE_RE.match(name):
            _group(m.group(1), "audio", p); handled.append(p); continue
        if m := _ZH_TXT_RE.match(name):
            _group(m.group(1), "transcript", p); handled.append(p); continue
        if m := _SUBTITLE_RE.match(name):
            _group(m.group(1), "subtitle", p); handled.append(p); continue
        if m := _THUMB_RE.match(name):
            _group(m.group(1), "thumb", p); handled.append(p); continue

    count = 0
    for vid, files in groups.items():
        bundle = AssetBundle(vid)
        bundle.ensure_dir()
        # 音频:统一改名为 audio.{ext}
        if ap := files.get("audio"):
            target = bundle.dir / f"audio{ap.suffix}"
            _move_safe(ap, target)
        # 缩略图
        if tp := files.get("thumb"):
            target = bundle.dir / f"thumb{tp.suffix}"
            _move_safe(tp, target)
        # 译文
        if trp := files.get("transcript"):
            _move_safe(trp, bundle.transcript_path)
        # 字幕:保留原文件名(带语言后缀,parse 时需 glob)
        if sp := files.get("subtitle"):
            target = bundle.dir / f"subtitle{sp.suffix[len('.json3'):]}" if sp.suffix == ".json3" else bundle.dir / f"subtitle{sp.suffix}"
            # 简化:直接保留 subtitle.{原后缀},语言信息留在文件名里
            target = bundle.dir / f"subtitle.{sp.name.split('.', 1)[1]}" if "." in sp.name else bundle.dir / sp.name
            _move_safe(sp, target)
        # 元数据:改名为 meta.json,并补全 audio_name→video_id 等字段
        if mp := files.get("meta"):
            try:
                data = json.loads(mp.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    # 兼容旧字段:audio_name 保留但增加 audio_url 指向新路径
                    if "audio_name" in data and "audio_ext" not in data:
                        ext = Path(data["audio_name"]).suffix
                        data["audio_ext"] = ext
                    data["audio_url"] = f"/audio/{vid}"
                    bundle.save_meta(data)
                    mp.unlink()
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("迁移元数据失败 %s: %s", vid, e)
        count += 1

    # 标记迁移完成
    try:
        (config.OUTPUT_DIR / _MIGRATE_FLAG).write_text("1", encoding="utf-8")
    except OSError as e:
        logger.warning("写入迁移标记失败: %s", e)

    # 删除旧 history.json,强制下次读取时从资产包 meta.json 重建。
    # 旧 history.json 的 audio_url/audio_name 字段已过时(指向旧文件名),
    # meta.json 在迁移时已更新为新的 /audio/{video_id} 形式。
    if count and config.HISTORY_FILE.exists():
        try:
            config.HISTORY_FILE.unlink()
        except OSError as e:
            logger.warning("删除旧 history.json 失败: %s", e)

    if count:
        logger.info("资产包迁移完成,共 %d 个", count)
    return count


def _move_safe(src: Path, dst: Path) -> None:
    """移动文件,目标已存在则覆盖,失败仅警告不中断。"""
    try:
        if dst.exists():
            dst.unlink()
        shutil.move(str(src), str(dst))
    except OSError as e:
        logger.warning("迁移文件失败 %s -> %s: %s", src, dst, e)

"""历史记录持久化:data/history.json 单文件,按 video_id 去重、最新在前。

退化为纯索引(指向 video_id 列表);元数据唯一来源是各资产包的 meta.json。
首次启动若 history.json 不存在或为旧格式,从资产包 meta.json 重建索引。
"""
from __future__ import annotations

import json
import logging

from . import assets, config

logger = logging.getLogger(__name__)


def load() -> list[dict]:
    """读取历史列表;文件不存在或旧格式时从资产包重建,损坏则返回空列表。"""
    if not config.HISTORY_FILE.exists():
        return rebuild_from_bundles()
    try:
        data = json.loads(config.HISTORY_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return rebuild_from_bundles()
        # 旧格式条目可能用 audio_name 键而非 video_id,触发重建
        if data and any("video_id" not in h for h in data if isinstance(h, dict)):
            return rebuild_from_bundles()
        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("读取历史文件失败: %s", e)
        return rebuild_from_bundles()


def save(items: list[dict]) -> None:
    """原子写入历史列表（先写临时文件再 rename，避免半写损坏）。"""
    config.DATA_DIR.mkdir(exist_ok=True)
    tmp = config.DATA_DIR / ".history.json.tmp"
    try:
        tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(config.HISTORY_FILE)
    except OSError as e:
        logger.warning("写入历史文件失败: %s", e)


def upsert(item: dict) -> None:
    """新增或更新一条历史（按 video_id 去重，最新在前）。"""
    items = load()
    vid = item.get("video_id")
    if not vid:
        return
    items = [h for h in items if h.get("video_id") != vid]
    items.insert(0, item)
    save(items)


def remove(video_id: str) -> None:
    """按 video_id 移除一条历史。"""
    items = load()
    items = [h for h in items if h.get("video_id") != video_id]
    save(items)


def clear() -> None:
    """清空全部历史记录。"""
    save([])


def rebuild_from_bundles() -> list[dict]:
    """从所有资产包的 meta.json 重建历史索引。

    用于:首次运行(无 history.json)、旧格式迁移、文件损坏。
    返回按 created_at 降序的元数据列表。
    """
    bundles = assets.list_bundles()
    items: list[dict] = []
    for b in bundles:
        meta = b.meta()
        if meta is not None:
            items.append(meta)
    items.sort(key=lambda h: h.get("created_at") or "", reverse=True)
    if items:
        save(items)
        logger.info("历史索引重建完成,共 %d 条", len(items))
    return items

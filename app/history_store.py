"""历史记录持久化：data/history.json 单文件，按 audio_name 去重、最新在前。

替代每次全量扫描 output/ 目录的旧实现——历史条目积累多了 iterdir+stat 会有可感延迟。
单文件 JSON 读写简单、人类可读，本地单用户场景下足够。
"""
from __future__ import annotations

import json
import logging

from . import config

logger = logging.getLogger(__name__)


def load() -> list[dict]:
    """读取历史列表；文件不存在时触发一次性迁移，损坏则返回空列表。"""
    if not config.HISTORY_FILE.exists():
        _migrate_from_output()
    if not config.HISTORY_FILE.exists():
        return []
    try:
        data = json.loads(config.HISTORY_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("读取历史文件失败: %s", e)
        return []


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
    """新增或更新一条历史（按 audio_name 去重，最新在前）。"""
    items = load()
    name = item.get("audio_name")
    items = [h for h in items if h.get("audio_name") != name]
    items.insert(0, item)
    save(items)


def remove(audio_name: str) -> None:
    """按 audio_name 移除一条历史。"""
    items = load()
    items = [h for h in items if h.get("audio_name") != audio_name]
    save(items)


def _migrate_from_output() -> None:
    """首次运行：把 output/ 下的 *.json 元数据迁移到 data/history.json。

    旧版本每次扫描 output/ 读取 {audio_name}.json 元数据；新版改用集中式
    history.json。此函数把已有的散落元数据合并成单文件，仅执行一次。
    """
    if not config.OUTPUT_DIR.exists():
        return
    items: list[dict] = []
    for jp in config.OUTPUT_DIR.glob("*.json"):
        try:
            data = json.loads(jp.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("audio_name"):
                items.append(data)
        except (json.JSONDecodeError, OSError):
            continue
    # 按 created_at 降序，无 created_at 的旧条目排最后
    items.sort(key=lambda h: h.get("created_at") or "", reverse=True)
    if items:
        save(items)
        logger.info("历史记录迁移完成，共 %d 条", len(items))

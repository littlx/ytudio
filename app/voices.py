"""edge-tts 可用中文音色列表与音色名 → 标签转换。

音色数据集中在此，供 /api/voices 路由、index 模板渲染（默认音色徽章）共用，
避免散落在多处。
"""
from __future__ import annotations

# edge-tts 可用中文音色（名称/性别/地区/方言备注）
ZH_VOICES: list[dict] = [
    {"name": "zh-CN-XiaoxiaoNeural", "gender": "女", "label": "晓晓 · 普通话（自然，推荐）"},
    {"name": "zh-CN-XiaoyiNeural", "gender": "女", "label": "晓伊 · 普通话"},
    {"name": "zh-CN-YunxiNeural", "gender": "男", "label": "云希 · 普通话（自然）"},
    {"name": "zh-CN-YunyangNeural", "gender": "男", "label": "云扬 · 普通话（新闻播报）"},
    {"name": "zh-CN-YunjianNeural", "gender": "男", "label": "云健 · 普通话"},
    {"name": "zh-CN-YunxiaNeural", "gender": "男", "label": "云夏 · 普通话（童声）"},
    {"name": "zh-CN-liaoning-XiaobeiNeural", "gender": "女", "label": "晓贝 · 东北话"},
    {"name": "zh-CN-shaanxi-XiaoniNeural", "gender": "女", "label": "晓妮 · 陕西话"},
    {"name": "zh-HK-HiuGaaiNeural", "gender": "女", "label": "曉佳 · 粤语"},
    {"name": "zh-HK-HiuMaanNeural", "gender": "女", "label": "曉曼 · 粤语"},
    {"name": "zh-HK-WanLungNeural", "gender": "男", "label": "雲龍 · 粤语"},
    {"name": "zh-TW-HsiaoChenNeural", "gender": "女", "label": "曉臻 · 台湾国语"},
    {"name": "zh-TW-HsiaoYuNeural", "gender": "女", "label": "曉雨 · 台湾国语"},
    {"name": "zh-TW-YunJheNeural", "gender": "男", "label": "雲哲 · 台湾国语"},
]


def voice_label(name: str) -> str:
    """音色名 → 简短标签（用于前端徽章初始值，避免硬编码）。

    label 形如「晓晓 · 普通话（自然，推荐）」，取「 · 」前的短名。
    找不到时原样返回音色名。
    """
    for v in ZH_VOICES:
        if v["name"] == name:
            return v["label"].split(" · ")[0]
    return name

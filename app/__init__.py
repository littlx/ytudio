"""ytudio — YouTube → 中文音频 Web 应用."""

import logging

__version__ = "0.1.0"

# 全局日志配置：带时间戳，INFO 级别。各模块用 logging.getLogger(__name__)。
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

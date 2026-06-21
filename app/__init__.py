"""ytudio — YouTube → 中文音频 Web 应用."""

import logging
from logging.handlers import RotatingFileHandler

__version__ = "0.1.0"

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"

# 控制台输出(带时间戳,INFO 级别)
logging.basicConfig(
    level=logging.INFO,
    format=_LOG_FORMAT,
    datefmt=_LOG_DATEFMT,
)

# 文件输出(轮转 5MB × 3 份,DEBUG 级别,方便事后排查)
# 放在 data/ytudio.log,与 cookies/history 等运行时数据同级。
try:
    from . import config as _config  # noqa: 避免循环:config 不依赖本包
    _file_handler = RotatingFileHandler(
        _config.DATA_DIR / "ytudio.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    _file_handler.setLevel(logging.DEBUG)
    _file_handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT))
    logging.getLogger().addHandler(_file_handler)
except Exception:
    # 文件日志初始化失败不阻塞启动(如目录权限问题),控制台日志仍可用
    pass

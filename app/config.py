"""全局配置：读取 .env、定义路径常量、校验 API Key。"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# 项目根目录：app/config.py 上溯一层
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

# 生成音频的输出目录（运行时自动创建）
OUTPUT_DIR = BASE_DIR / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

# 运行时数据目录（cookies 等）
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

# 模板目录
TEMPLATES_DIR = BASE_DIR / "templates"

# DeepSeek 配置
DEEPSEEK_API_KEY: str = os.getenv("DEEPSEEK_API_KEY", "").strip()
DEEPSEEK_BASE_URL: str = os.getenv(
    "DEEPSEEK_BASE_URL", "https://api.deepseek.com"
).rstrip("/")
# 模型名：DeepSeek-V4-Flash 原生 1M tokens 上下文，可整篇翻译超长字幕
DEEPSEEK_MODEL: str = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

# edge-tts 语音
TTS_VOICE: str = os.getenv("TTS_VOICE", "zh-CN-XiaoxiaoNeural")

# 翻译分批大小（字符数）——仅当字幕超过 WHOLE_TRANSLATE_LIMIT 时才分批，每批大小
TRANSLATE_CHUNK_SIZE: int = int(os.getenv("TRANSLATE_CHUNK_SIZE", "4000"))

# 整篇翻译的字符安全上限。DeepSeek-V4-Flash 原生 1M tokens 上下文，
# 英文字幕约 4 字符≈1 token，留足输出+提示词余量取 800000 字符；
# 超过此值才退回分批。绝大多数视频（含数小时长演讲）都能整篇翻译。
WHOLE_TRANSLATE_LIMIT: int = int(os.getenv("WHOLE_TRANSLATE_LIMIT", "800000"))

# 服务端口与监听地址
PORT: int = int(os.getenv("PORT", "8200"))
# 监听地址：默认仅本地回环。如需手机/局域网访问，设为 0.0.0.0 并务必配置 AUTH_TOKEN，
# 否则 cookies（含 YouTube 登录凭证）等端点会暴露在局域网。
HOST: str = os.getenv("HOST", "127.0.0.1").strip()

# 访问令牌：仅当 HOST 非 127.0.0.1 时强校验。客户端需在请求头携带
# `Authorization: Bearer <token>` 或查询参数 `?token=<token>`。
# 本地访问（127.0.0.1）无需配置。
AUTH_TOKEN: str = os.getenv("AUTH_TOKEN", "").strip()

# yt-dlp cookies：从浏览器读取以绕过 YouTube 机器人检测。
# 值为浏览器名，如 chrome / safari / firefox / edge / brave；留空则不使用。
# 也可用 cookies 文件绝对路径。
COOKIES_FROM_BROWSER: str = os.getenv("COOKIES_FROM_BROWSER", "").strip()
COOKIES_FILE: str = os.getenv("COOKIES_FILE", "").strip()

# 运行时通过网页上传/粘贴保存的 cookies 文件（Netscape 格式）。
# 服务器部署无浏览器时，用页面里的 cookies 管理面板上传。
COOKIES_RUNTIME_FILE: Path = BASE_DIR / "data" / "cookies.txt"

# 历史记录持久化文件（JSON 数组，按 audio_name 去重、最新在前）。
# 替代每次全量扫描 output/ 目录，提升历史列表读取性能。
HISTORY_FILE: Path = DATA_DIR / "history.json"

# 任务状态持久化文件（JSON 字典，task_id -> 状态摘要）。
# 进程重启后供 SSE 重连查看终态;进行中的任务标记为 error。
TASKS_FILE: Path = DATA_DIR / "tasks.json"


def cookies_file_to_use() -> str:
    """返回实际生效的 cookies 文件绝对路径（优先环境变量，其次运行时上传），无则空串。"""
    if COOKIES_FILE:
        return COOKIES_FILE
    if COOKIES_RUNTIME_FILE.exists():
        return str(COOKIES_RUNTIME_FILE)
    return ""

# yt-dlp 远程组件：用于解决 YouTube 的 n-challenge（现代 YouTube 必需，
# 否则只能拿到 storyboard 图片，没有音频/视频流）。需要系统装有 deno 或 node。
# 留空则不启用（遇到「Only images are available」错误时开启）。
REMOTE_COMPONENTS: str = os.getenv("REMOTE_COMPONENTS", "ejs:github").strip()


def has_deepseek_key() -> bool:
    """是否已配置可用的 DeepSeek API Key（非空即视为可用）。"""
    return bool(DEEPSEEK_API_KEY)


def is_local_only() -> bool:
    """是否仅本地访问（回环地址），决定是否强制鉴权。"""
    return HOST in ("127.0.0.1", "localhost", "::1")

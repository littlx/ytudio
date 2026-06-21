"""cookies.txt（Netscape 格式）校验与来源描述。

供 /api/cookies 系列路由调用：上传保存前校验格式、向界面展示当前 cookies 来源。
"""
from __future__ import annotations

from . import config

# Netscape cookies.txt 头部标识
NETSCAPE_HEADER = "# Netscape HTTP Cookie File"


def source() -> str:
    """返回 cookies 来源描述，供界面展示。

    优先级与 yt.py 实际取用顺序一致：环境变量文件 > 浏览器读取 > 页面上传 > 无。
    """
    if config.COOKIES_FILE:
        return "env"
    if config.COOKIES_FROM_BROWSER:
        return config.COOKIES_FROM_BROWSER
    if config.COOKIES_RUNTIME_FILE.exists():
        return "upload"
    return ""


def validate(content: str) -> tuple[bool, str, int]:
    """校验 cookies 文本是否为合法 Netscape 格式。

    返回 (是否有效, 信息, cookie 行数)。
    """
    if not content.strip():
        return False, "内容为空", 0
    lines = content.splitlines()
    cookie_lines = 0
    has_header = False
    for line in lines:
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            if NETSCAPE_HEADER in s:
                has_header = True
            continue
        # 非注释行：应为 7 个 tab 分隔字段
        parts = line.split("\t")
        if len(parts) >= 7:
            cookie_lines += 1
    if cookie_lines == 0:
        return False, "未找到有效的 cookie 行（每行需 7 个 tab 分隔字段）", 0
    return True, (
        f"包含 {cookie_lines} 条 cookie" + ("（含 Netscape 头）" if has_header else "")
    ), cookie_lines

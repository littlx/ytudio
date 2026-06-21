"""静态资源服务：图标 / manifest / CSS / JS / Service Worker。

把原先散落的 6 个几乎一样的 FileResponse 路由合并为一个 StaticFiles 挂载到 `/`，
由 Starlette 统一按扩展名推断 Content-Type。唯一例外是 sw.js —— Service Worker
需要 `Service-Worker-Allowed: /` 响应头才能把作用域提升到根，故单独保留一条显式路由
（在挂载之前注册，优先级高于挂载）。

用法：在 main.py 注册完所有动态路由后调用 mount_static(app)。
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import config


async def _serve_sw() -> FileResponse:
    """sw.js：附带 Service-Worker-Allowed 头，允许作用域覆盖根路径。"""
    return FileResponse(
        config.TEMPLATES_DIR / "sw.js",
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/"},
    )


def mount_static(app: FastAPI) -> None:
    """注册 sw.js 专用路由，并挂载 StaticFiles 作为静态资源兜底。

    必须在所有动态路由注册之后调用——Starlette 按注册顺序匹配，先注册的显式路由
    优先于挂载；/thumb、/audio、/api/* 等动态路由因此不受影响。
    """
    # sw.js 需特殊响应头，先于挂载注册以抢占该路径
    app.add_api_route("/sw.js", _serve_sw, methods=["GET"])
    # 其余静态文件（icon.jpg / icon-*.png / manifest.json / static/css|js/*）交给挂载
    app.mount("/", StaticFiles(directory=str(config.TEMPLATES_DIR), html=False), name="static")

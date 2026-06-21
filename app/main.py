"""FastAPI 应用入口：路由 + 依赖校验。

路由按职责转调子模块：
- 任务/SSE 生命周期 → app.tasks
- cookies 校验与来源 → app.cookies
- 音色列表与标签 → app.voices
- 静态资源（图标/manifest/CSS/JS/SW）→ app.static（末尾挂载）
"""
from __future__ import annotations

import secrets
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

from . import assets, config, cookies, history_store, pipeline, static, tasks, voices

templates = Jinja2Templates(directory=str(config.TEMPLATES_DIR))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时:确保目录存在、迁移旧版散落文件到资产包结构、恢复任务状态
    config.OUTPUT_DIR.mkdir(exist_ok=True)
    config.DATA_DIR.mkdir(exist_ok=True)
    try:
        assets.migrate_legacy()
    except Exception as e:
        # 迁移失败不阻塞启动,旧文件仍可被新逻辑兜底访问
        import logging
        logging.getLogger(__name__).warning("资产包迁移失败: %s", e)
    try:
        tasks.init()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("任务状态恢复失败: %s", e)
    try:
        webbrowser.open(f"http://127.0.0.1:{config.PORT}")
    except Exception:
        pass
    yield


app = FastAPI(title="ytudio — YouTube → 中文音频", lifespan=lifespan)


async def verify_token(request: Request) -> None:
    """访问令牌校验：仅当配置了 AUTH_TOKEN 时生效。

    本地回环访问（127.0.0.1）即使配了 token 也放行，方便本机使用；
    局域网访问必须带正确的 token，否则 401。token 可通过
    `Authorization: Bearer <token>` 或 `?token=<token>` 传递。
    """
    if not config.AUTH_TOKEN:
        return
    # 本地回环直接放行
    client = request.client.host if request.client else ""
    if client in ("127.0.0.1", "::1", "localhost"):
        return
    auth = request.headers.get("authorization", "")
    bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    query_token = request.query_params.get("token", "")
    # 常量时间比较，避免时序侧信道（虽本地工具风险极低，但部署到局域网时更稳妥）
    if not (secrets.compare_digest(bearer, config.AUTH_TOKEN)
            or secrets.compare_digest(query_token, config.AUTH_TOKEN)):
        raise HTTPException(status_code=401, detail="未授权：请提供正确的 token")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "has_deepseek_key": config.has_deepseek_key(),
            "has_cookies": config.cookies_file_to_use() != "",
            "default_voice": config.TTS_VOICE,
            "default_voice_label": voices.voice_label(config.TTS_VOICE),
        },
    )


@app.get("/api/status")
async def api_status(_: None = Depends(verify_token)):
    return {
        "has_deepseek_key": config.has_deepseek_key(),
        "has_cookies": config.cookies_file_to_use() != "",
        "cookies_source": cookies.source(),
        "default_voice": config.TTS_VOICE,
    }


@app.get("/api/voices")
async def voices_list(_: None = Depends(verify_token)):
    """返回可用中文音色列表。"""
    return {"voices": voices.ZH_VOICES, "default": config.TTS_VOICE}


@app.get("/api/voice/preview/{voice}")
async def voice_preview(voice: str, _: None = Depends(verify_token)):
    """生成一句话试听音频（缓存到 data/previews/<voice>.mp3）。"""
    import edge_tts
    if Path(voice).name != voice or ".." in voice or not voice.startswith("zh-"):
        raise HTTPException(400, "非法音色名")
    preview_dir = config.DATA_DIR / "previews"
    preview_dir.mkdir(exist_ok=True)
    path = preview_dir / f"{voice}.mp3"
    if not path.exists():
        communicate = edge_tts.Communicate(
            "你好，这是语音试听。开车听音频，请注意安全。", voice
        )
        await communicate.save(str(path))
    return FileResponse(path, media_type="audio/mpeg", filename=f"{voice}.mp3")


@app.get("/api/cookies")
async def cookies_get(_: None = Depends(verify_token)):
    """返回 cookies 状态（不返回内容，安全考虑）。"""
    return {
        "has_cookies": config.cookies_file_to_use() != "",
        "source": cookies.source(),
    }


@app.post("/api/cookies")
async def cookies_save(content: str = Form(...), _: None = Depends(verify_token)):
    """保存用户粘贴/上传的 cookies.txt 内容（Netscape 格式）。"""
    content = content.strip()
    ok, msg, count = cookies.validate(content)
    if not ok:
        raise HTTPException(400, f"cookies 格式无效：{msg}")
    # 若未含 Netscape 头，补上
    if cookies.NETSCAPE_HEADER not in content:
        content = cookies.NETSCAPE_HEADER + "\n" + content
    config.COOKIES_RUNTIME_FILE.write_text(content, encoding="utf-8")
    return {"ok": True, "message": msg, "count": count, "source": "upload"}


@app.delete("/api/cookies")
async def cookies_clear(_: None = Depends(verify_token)):
    """清除页面上传的 cookies 文件（不影响环境变量配置）。"""
    if config.COOKIES_RUNTIME_FILE.exists():
        config.COOKIES_RUNTIME_FILE.unlink()
    return {"ok": True, "has_cookies": config.cookies_file_to_use() != ""}


@app.post("/api/process")
async def process(
    url: str = Form(...),
    mode: str = Form(...),
    voice: str = Form(default=""),
    _: None = Depends(verify_token),
):
    url = url.strip()
    mode = mode.strip()
    voice = voice.strip()
    if mode not in (pipeline.MODE_AUDIO, pipeline.MODE_SUBTITLE_TTS):
        raise HTTPException(400, "无效的模式")
    if not url:
        raise HTTPException(400, "URL 不能为空")
    if mode == pipeline.MODE_SUBTITLE_TTS and not config.has_deepseek_key():
        raise HTTPException(
            400, "字幕翻译模式需要 DEEPSEEK_API_KEY，请在 .env 中配置后重启。"
        )
    return await tasks.create(mode, url, voice=voice)


@app.post("/api/cancel/{task_id}")
async def cancel_task(task_id: str, _: None = Depends(verify_token)):
    """取消正在运行的任务。"""
    return await tasks.cancel(task_id)


@app.post("/api/retry/{task_id}")
async def retry_task(task_id: str, _: None = Depends(verify_token)):
    """从断点重试一个失败的任务。

    读取原任务参数(mode/url/voice),用 resume=True 创建新任务。
    若资产包有 progress.json,则从断点继续;否则从头开始。
    """
    return await tasks.retry(task_id)


@app.get("/api/progress/{task_id}")
async def progress_stream(task_id: str, _: None = Depends(verify_token)):
    """SSE：实时推送任务进度，结束后发送最终状态并关闭。"""
    return tasks.progress_stream(task_id)


@app.get("/thumb/{video_id}")
async def get_thumb(video_id: str, _: None = Depends(verify_token)):
    """提供视频缩略图(资产包内 thumb.{ext},替代 i.ytimg.com 外链,支持离线)。"""
    if Path(video_id).name != video_id:
        raise HTTPException(400, "非法 video_id")
    bundle = assets.AssetBundle(video_id)
    thumb = bundle.thumb_path()
    if thumb is not None and thumb.is_file():
        ext = thumb.suffix.lstrip(".")
        return FileResponse(thumb, media_type=f"image/{ext}")
    # 兜底:未找到缩略图,返回默认图标
    return FileResponse(config.TEMPLATES_DIR / "icon.jpg", media_type="image/jpeg")


_MIME_BY_EXT = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".webm": "audio/webm",
    ".ogg": "audio/ogg",
    ".wav": "audio/wav",
    ".opus": "audio/ogg",
}


def _audio_mime(ext: str) -> str:
    return _MIME_BY_EXT.get(ext.lower(), "audio/mpeg")


@app.get("/audio/{video_id}")
async def serve_audio(video_id: str, _: None = Depends(verify_token)):
    """提供生成的音频文件播放(从资产包 audio.{ext} 取)。"""
    if Path(video_id).name != video_id:
        raise HTTPException(400, "非法 video_id")
    bundle = assets.AssetBundle(video_id)
    path = bundle.audio_path()
    if path is None or not path.is_file():
        raise HTTPException(404, "音频文件不存在")
    return FileResponse(path, media_type=_audio_mime(path.suffix), filename=path.name)


@app.get("/api/download/{video_id}")
async def download_audio(video_id: str, _: None = Depends(verify_token)):
    """下载音频文件(从资产包 audio.{ext} 取)。"""
    if Path(video_id).name != video_id:
        raise HTTPException(400, "非法 video_id")
    bundle = assets.AssetBundle(video_id)
    path = bundle.audio_path()
    if path is None or not path.is_file():
        raise HTTPException(404, "音频文件不存在")
    return FileResponse(
        path, media_type=_audio_mime(path.suffix), filename=path.name,
        headers={"Content-Disposition": f'attachment; filename="{path.name}"'},
    )


@app.get("/api/transcript/{video_id}")
async def get_transcript(video_id: str, _: None = Depends(verify_token)):
    """返回 TTS 模式生成的中文译文文稿(资产包内 transcript_zh.txt)。"""
    if Path(video_id).name != video_id:
        raise HTTPException(400, "非法 video_id")
    bundle = assets.AssetBundle(video_id)
    if not bundle.transcript_path.exists():
        raise HTTPException(404, "未找到该视频的译文文稿")
    return {"video_id": video_id, "transcript": bundle.transcript_path.read_text(encoding="utf-8")}


@app.get("/api/history")
async def get_history(_: None = Depends(verify_token)):
    """获取已生成的音频历史列表（从 data/history.json 读取）。"""
    return {"history": history_store.load()}


@app.delete("/api/history")
async def clear_history(_: None = Depends(verify_token)):
    """清空全部历史:删除所有资产包目录,并清空 history.json。不可恢复。"""
    deleted: list[str] = []
    if config.OUTPUT_DIR.exists():
        for d in config.OUTPUT_DIR.iterdir():
            if d.is_dir() and not d.name.startswith("."):
                bundle = assets.AssetBundle(d.name)
                deleted.extend(bundle.remove())
    history_store.clear()
    return {"ok": True, "deleted": deleted, "count": len(deleted)}


@app.delete("/api/history/{video_id}")
async def delete_history_item(video_id: str, _: None = Depends(verify_token)):
    """删除资产包(音频/缩略图/字幕/译文/元数据)并从历史索引移除。"""
    if Path(video_id).name != video_id:
        raise HTTPException(400, "非法 video_id")

    bundle = assets.AssetBundle(video_id)
    deleted_files = bundle.remove()

    # 从历史索引中移除记录(即使资产包已不在,也要清理 history.json)
    history_store.remove(video_id)

    if not deleted_files:
        raise HTTPException(404, "未找到该视频的资产包")

    return {"ok": True, "deleted": deleted_files}


# 静态资源（图标/manifest/CSS/JS/SW）兜底挂载，必须放在所有动态路由之后
static.mount_static(app)

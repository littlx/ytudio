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

from . import config, cookies, history_store, pipeline, static, tasks, voices

templates = Jinja2Templates(directory=str(config.TEMPLATES_DIR))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时尝试打开浏览器（本地工具，方便使用）
    config.OUTPUT_DIR.mkdir(exist_ok=True)
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


@app.get("/api/progress/{task_id}")
async def progress_stream(task_id: str, _: None = Depends(verify_token)):
    """SSE：实时推送任务进度，结束后发送最终状态并关闭。"""
    return tasks.progress_stream(task_id)


# 缩略图支持的扩展名（yt-dlp 落盘的实际格式）
_THUMB_EXTS = (".jpg", ".webp", ".png")


@app.get("/thumb/{video_id}")
async def get_thumb(video_id: str, _: None = Depends(verify_token)):
    """提供视频缩略图（本地存储，替代 i.ytimg.com 外链，支持离线）。"""
    if Path(video_id).name != video_id:
        raise HTTPException(400, "非法 video_id")
    for ext in _THUMB_EXTS:
        path = config.OUTPUT_DIR / f"{video_id}{ext}"
        if path.exists() and path.is_file():
            return FileResponse(path, media_type=f"image/{ext.lstrip('.')}")
    # 兜底：未找到缩略图，返回默认图标
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


def _audio_mime(name: str) -> str:
    ext = Path(name).suffix.lower()
    return _MIME_BY_EXT.get(ext, "audio/mpeg")


@app.get("/audio/{name}")
async def serve_audio(name: str, _: None = Depends(verify_token)):
    """提供生成的音频文件播放。"""
    if Path(name).name != name:
        raise HTTPException(400, "非法文件名")
    path = config.OUTPUT_DIR / name
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "音频文件不存在")
    return FileResponse(path, media_type=_audio_mime(name), filename=name)


@app.get("/api/download/{name}")
async def download_audio(name: str, _: None = Depends(verify_token)):
    """下载音频文件。"""
    if Path(name).name != name:
        raise HTTPException(400, "非法文件名")
    path = config.OUTPUT_DIR / name
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "音频文件不存在")
    return FileResponse(
        path, media_type=_audio_mime(name), filename=name,
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@app.get("/api/transcript/{video_id}")
async def get_transcript(video_id: str, _: None = Depends(verify_token)):
    """返回 TTS 模式生成的中文译文文稿。"""
    if Path(video_id).name != video_id:
        raise HTTPException(400, "非法 video_id")
    path = config.OUTPUT_DIR / f"{video_id}_zh.txt"
    if not path.exists():
        raise HTTPException(404, "未找到该视频的译文文稿")
    return {"video_id": video_id, "transcript": path.read_text(encoding="utf-8")}


@app.get("/api/history")
async def get_history(_: None = Depends(verify_token)):
    """获取已生成的音频历史列表（从 data/history.json 读取）。"""
    return {"history": history_store.load()}


@app.delete("/api/history")
async def clear_history(_: None = Depends(verify_token)):
    """清空全部历史：删除 output/ 下所有文件，并清空 history.json。不可恢复。"""
    deleted: list[str] = []
    if config.OUTPUT_DIR.exists():
        for p in config.OUTPUT_DIR.iterdir():
            if p.is_file():
                try:
                    p.unlink()
                    deleted.append(p.name)
                except Exception:
                    pass
    history_store.clear()
    return {"ok": True, "deleted": deleted, "count": len(deleted)}


@app.delete("/api/history/{name}")
async def delete_history_item(name: str, _: None = Depends(verify_token)):
    """删除音频文件及其元数据、字幕文件。"""
    if Path(name).name != name:
        raise HTTPException(400, "非法文件名")

    audio_path = config.OUTPUT_DIR / name
    json_path = audio_path.with_suffix(".json")

    deleted_files = []
    if audio_path.exists() and audio_path.is_file():
        try:
            audio_path.unlink()
            deleted_files.append(name)
        except Exception as e:
            raise HTTPException(500, f"删除音频文件失败: {e}")

    if json_path.exists() and json_path.is_file():
        try:
            json_path.unlink()
            deleted_files.append(json_path.name)
        except Exception:
            pass  # 元数据删除失败不影响主要流程

    # 清理相关的字幕等临时文件 (如果有)
    video_id = None
    if name.endswith("_zh.mp3"):
        video_id = name[:-7]
    elif "_audio" in name:
        video_id = name.split("_audio")[0]

    if video_id:
        for p in config.OUTPUT_DIR.glob(f"{video_id}.*"):
            if p.is_file() and p.suffix in (".json3", ".vtt", ".txt"):
                try:
                    p.unlink()
                    deleted_files.append(p.name)
                except Exception:
                    pass

    # 从历史索引中移除记录（即使音频文件已不在，也要清理 history.json）
    history_store.remove(name)

    if not deleted_files:
        raise HTTPException(404, "未找到该音频文件")

    return {"ok": True, "deleted": deleted_files}


# 静态资源（图标/manifest/CSS/JS/SW）兜底挂载，必须放在所有动态路由之后
static.mount_static(app)

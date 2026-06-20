"""FastAPI 应用入口：路由 + SSE 进度推送。"""
from __future__ import annotations

import asyncio
import json
import secrets
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from . import config, history_store, pipeline
from .pipeline import TaskState

# 全局任务表：task_id -> TaskState（含一个进度队列供 SSE 订阅）
_tasks: dict[str, TaskState] = {}
_queues: dict[str, asyncio.Queue] = {}
# 后台任务引用集合：防止「发射后不管」的任务被 GC 回收导致中途消失
_background_tasks: set[asyncio.Task] = set()
# 任务终态后保留时长（秒），供 SSE 重连查看结果，之后清理释放内存
_TASK_RETAIN_SECONDS = 300


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
templates = Jinja2Templates(directory=str(config.TEMPLATES_DIR))


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
            "default_voice_label": _voice_label(config.TTS_VOICE),
        },
    )


def _voice_label(name: str) -> str:
    """音色名 → 简短标签（用于前端徽章初始值，避免硬编码）。"""
    for v in ZH_VOICES:
        if v["name"] == name:
            return v["label"].split(" · ")[0]
    return name


@app.get("/manifest.json")
async def get_manifest():
    return FileResponse(config.TEMPLATES_DIR / "manifest.json", media_type="application/json")


@app.get("/sw.js")
async def get_sw():
    return FileResponse(
        config.TEMPLATES_DIR / "sw.js",
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/"},
    )


@app.get("/icon.jpg")
async def get_icon():
    return FileResponse(config.TEMPLATES_DIR / "icon.jpg", media_type="image/jpeg")


@app.get("/icon-192.png")
async def get_icon_192():
    return FileResponse(config.TEMPLATES_DIR / "icon-192.png", media_type="image/png")


@app.get("/icon-512.png")
async def get_icon_512():
    return FileResponse(config.TEMPLATES_DIR / "icon-512.png", media_type="image/png")


@app.get("/icon-maskable-512.png")
async def get_icon_maskable():
    return FileResponse(config.TEMPLATES_DIR / "icon-maskable-512.png", media_type="image/png")


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


@app.get("/api/status")
async def api_status(_: None = Depends(verify_token)):
    return {
        "has_deepseek_key": config.has_deepseek_key(),
        "has_cookies": config.cookies_file_to_use() != "",
        "cookies_source": _cookies_source(),
        "default_voice": config.TTS_VOICE,
    }


# edge-tts 可用中文音色（名称/性别/地区/方言备注）
ZH_VOICES = [
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


@app.get("/api/voices")
async def voices_list(_: None = Depends(verify_token)):
    """返回可用中文音色列表。"""
    return {"voices": ZH_VOICES, "default": config.TTS_VOICE}


@app.get("/api/voice/preview/{voice}")
async def voice_preview(voice: str, _: None = Depends(verify_token)):
    """生成一句话试听音频（缓存到 data/preview_<voice>.mp3）。"""
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


def _cookies_source() -> str:
    """返回 cookies 来源描述，供界面展示。"""
    if config.COOKIES_FILE:
        return "env"
    if config.COOKIES_FROM_BROWSER:
        return config.COOKIES_FROM_BROWSER
    if config.COOKIES_RUNTIME_FILE.exists():
        return "upload"
    return ""


# Netscape cookies.txt 头部标识
_NETSCAPE_HEADER = "# Netscape HTTP Cookie File"


def _validate_netscape_cookies(content: str) -> tuple[bool, str, int]:
    """校验 cookies 文本是否为合法 Netscape 格式。返回 (是否有效, 信息, cookie 行数)。"""
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
            if _NETSCAPE_HEADER in s:
                has_header = True
            continue
        # 非注释行：应为 7 个 tab 分隔字段
        parts = line.split("\t")
        if len(parts) >= 7:
            cookie_lines += 1
    if cookie_lines == 0:
        return False, "未找到有效的 cookie 行（每行需 7 个 tab 分隔字段）", 0
    return True, (f"包含 {cookie_lines} 条 cookie" + ("（含 Netscape 头）" if has_header else "")), cookie_lines


@app.get("/api/cookies")
async def cookies_get(_: None = Depends(verify_token)):
    """返回 cookies 状态（不返回内容，安全考虑）。"""
    return {
        "has_cookies": config.cookies_file_to_use() != "",
        "source": _cookies_source(),
    }


@app.post("/api/cookies")
async def cookies_save(content: str = Form(...), _: None = Depends(verify_token)):
    """保存用户粘贴/上传的 cookies.txt 内容（Netscape 格式）。"""
    content = content.strip()
    ok, msg, count = _validate_netscape_cookies(content)
    if not ok:
        raise HTTPException(400, f"cookies 格式无效：{msg}")
    # 若未含 Netscape 头，补上
    if _NETSCAPE_HEADER not in content:
        content = _NETSCAPE_HEADER + "\n" + content
    config.COOKIES_RUNTIME_FILE.write_text(content, encoding="utf-8")
    return {"ok": True, "message": msg, "count": count, "source": "upload"}


@app.delete("/api/cookies")
async def cookies_clear(_: None = Depends(verify_token)):
    """清除页面上传的 cookies 文件（不影响环境变量配置）。"""
    if config.COOKIES_RUNTIME_FILE.exists():
        config.COOKIES_RUNTIME_FILE.unlink()
    return {"ok": True, "has_cookies": config.cookies_file_to_use() != ""}


def _put(task_id: str, state: TaskState) -> None:
    """把当前状态推入对应 SSE 队列。"""
    q = _queues.get(task_id)
    if q is not None:
        q.put_nowait({
            "stage": state.stage,
            "percent": state.percent,
            "message": state.message,
            "error": state.error,
            "done": state.stage in ("done", "error"),
            "video_id": state.video_id,
            "title": state.title,
            "uploader": state.uploader,
        })


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

    state = TaskState()
    _tasks[state.task_id] = state
    _queues[state.task_id] = asyncio.Queue()
    # 捕获当前 event loop：yt-dlp 下载进度 hook 在 worker 线程触发，
    # 需用 call_soon_threadsafe 把队列写入调度回 loop 线程，避免跨线程操作 asyncio.Queue
    loop = asyncio.get_running_loop()

    def progress(stage: str, percent: int, message: str) -> None:
        state.stage = stage
        state.percent = percent
        state.message = message
        loop.call_soon_threadsafe(_put, state.task_id, state)

    # 后台运行任务（voice 仅 TTS 模式生效）
    task = asyncio.create_task(pipeline.run(mode, url, state, progress, voice=voice))
    state.task = task  # 存引用供 /api/cancel 取消
    # 持有强引用防止 GC；任务结束延时清理任务表与队列
    _background_tasks.add(task)

    def _on_done(t: asyncio.Task) -> None:
        _background_tasks.discard(t)
        # 终态后延时清理，留窗口给 SSE 重连查看结果
        async def _cleanup():
            await asyncio.sleep(_TASK_RETAIN_SECONDS)
            _tasks.pop(state.task_id, None)
            _queues.pop(state.task_id, None)
        cleanup_task = asyncio.create_task(_cleanup())
        # 清理任务也纳入强引用集合，防止 fire-and-forget 被 GC 中途取消
        _background_tasks.add(cleanup_task)
        cleanup_task.add_done_callback(lambda ct: _background_tasks.discard(ct))

    task.add_done_callback(_on_done)

    return {"task_id": state.task_id}


@app.post("/api/cancel/{task_id}")
async def cancel_task(task_id: str, _: None = Depends(verify_token)):
    """取消正在运行的任务。"""
    state = _tasks.get(task_id)
    if not state:
        raise HTTPException(404, "任务不存在")
    task = state.task
    if task is None or task.done():
        return {"ok": True, "already_done": True}
    task.cancel()
    return {"ok": True, "cancelled": True}


@app.get("/api/progress/{task_id}")
async def progress_stream(task_id: str, _: None = Depends(verify_token)):
    """SSE：实时推送任务进度，结束后发送最终状态并关闭。"""
    if task_id not in _tasks:
        raise HTTPException(404, "任务不存在")

    queue = _queues.setdefault(task_id, asyncio.Queue())
    state = _tasks[task_id]

    async def event_generator():
        # 先补发当前状态
        yield f"data: {json.dumps({'stage': state.stage, 'percent': state.percent, 'message': state.message, 'error': state.error, 'done': state.stage in ('done', 'error'), 'video_id': state.video_id, 'title': state.title, 'uploader': state.uploader})}\n\n"
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    # 心跳保活
                    yield ": keep-alive\n\n"
                    continue
                yield f"data: {json.dumps(msg)}\n\n"
                if msg.get("done"):
                    # 发送最终结果摘要
                    if state.result:
                        final = {
                            "done": True,
                            "stage": state.stage,
                            "error": state.error,
                            "result": {
                                "title": state.result.title,
                                "uploader": state.result.uploader,
                                "mode": state.result.mode,
                                "audio_url": state.result.audio_url,
                                "audio_name": state.result.audio_path.name,
                            },
                        }
                        yield f"data: {json.dumps(final)}\n\n"
                    break
        finally:
            _queues.pop(task_id, None)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


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


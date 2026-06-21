// ytudio 前端逻辑：PWA / 任务进度 / 播放器 / 历史列表 / cookies 管理
//
// 服务端配置（has_deepseek_key / has_cookies / default_voice）由 index.html 的
// 内联引导脚本注入到 window.YTUDIO_CONFIG，这里只读取，不出现任何 Jinja 语法。
(() => {
"use strict";

// PWA Service Worker 注册 + 更新检测 + 安装提示
let _deferredPrompt = null;
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js')
      .then(reg => {
        console.log('Service Worker registered', reg);
        // 检测到新 SW 等待激活时提示刷新
        reg.addEventListener('updatefound', () => {
          const nw = reg.installing;
          if (!nw) return;
          nw.addEventListener('statechange', () => {
            if (nw.state === 'installed' && navigator.serviceWorker.controller) {
              toast('应用已更新，刷新生效', 'ok');
            }
          });
        });
      })
      .catch(err => console.error('Service worker registration failed', err));
  });
}

// PWA 安装提示
window.addEventListener('beforeinstallprompt', (e) => {
  e.preventDefault();
  _deferredPrompt = e;
  // 在顶部标题栏显示安装入口
  const container = document.querySelector('.header-actions');
  if (container && !document.getElementById('install-btn')) {
    const btn = document.createElement('button');
    btn.id = 'install-btn';
    btn.className = 'install-header-btn';
    btn.innerHTML = '<svg class="icon" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M10.5 1.5H8.25A2.25 2.25 0 0 0 6 3.75v16.5a2.25 2.25 0 0 0 2.25 2.25h7.5A2.25 2.25 0 0 0 18 20.25V3.75a2.25 2.25 0 0 0-2.25-2.25H13.5m-3 0V3h3V1.5m-3 0h3m-3 18.75h3" /></svg><span>安装应用</span>';
    btn.addEventListener('click', async () => {
      if (!_deferredPrompt) return;
      _deferredPrompt.prompt();
      const { outcome } = await _deferredPrompt.userChoice;
      if (outcome === 'accepted') toast('已添加到主屏', 'ok');
      _deferredPrompt = null;
      btn.remove();
    });
    // 插入到最前面，排在在线状态指示器左侧
    container.insertBefore(btn, container.firstChild);
  }
});

// PWA 页面视图切换 (Tab Switcher)
function showTab(tabId) {
  document.querySelectorAll(".tab-content").forEach(el => {
    el.style.display = el.id === `tab-${tabId}` ? "block" : "none";
  });
  document.querySelectorAll(".nav-item").forEach(el => {
    el.classList.toggle("active", el.dataset.tab === tabId);
  });
  localStorage.setItem("ytudio_tab", tabId);
}
document.querySelectorAll(".nav-item").forEach(el => {
  el.addEventListener("click", () => showTab(el.dataset.tab));
});
// 延迟初始化默认页：默认「下载音频」，仅当上次停在播放器且有历史时才恢复
setTimeout(() => {
  const lastTab = localStorage.getItem("ytudio_tab");
  const defaultTab = lastTab === "history" ? "history" : "download";
  showTab(defaultTab);
}, 50);

// AUTH_TOKEN 携带：局域网部署时从 URL ?token= 读取并存 localStorage，后续请求统一附加。
// 本地回环访问无 token，函数原样返回不附加。fetch 走 Authorization 头（不进 URL/日志），
// EventSource / audio / img / a 等无法设自定义头的走 ?token= 查询参数（后端两种都支持）。
const TOKEN = new URLSearchParams(location.search).get("token") || localStorage.getItem("ytudio_token");
if (TOKEN) localStorage.setItem("ytudio_token", TOKEN);
function authUrl(path) {
  if (!TOKEN) return path;
  return path + (path.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(TOKEN);
}
function authFetch(path, opts = {}) {
  if (TOKEN) {
    opts.headers = { ...(opts.headers || {}), Authorization: "Bearer " + TOKEN };
  }
  return fetch(path, opts);
}

const CFG = window.YTUDIO_CONFIG || {};
const HAS_KEY = !!CFG.has_deepseek_key;
const HAS_COOKIES = !!CFG.has_cookies;
const DEFAULT_VOICE = CFG.default_voice || "zh-CN-XiaoxiaoNeural";
let selectedMode = "audio";
let history = [];
let currentIndex = -1;          // 当前播放项在 history 中的索引
// 播放模式：seq（顺序）→ repeat-one（单曲循环）→ repeat-all（列表循环）→ seq
let playMode = localStorage.getItem("ytudio_mode") || "seq";
// 播放进度持久化：audio_name -> 秒数。存 localStorage 跨会话恢复。
let progressMap = {};
try { progressMap = JSON.parse(localStorage.getItem("ytudio_progress") || "{}") || {}; } catch (e) {}
let voices = [];
let currentTaskId = null;        // 当前进行中的任务 ID（供取消）
let currentES = null;            // 当前 SSE 连接（供取消时关闭）

const $ = (id) => document.getElementById(id);
const modeAudio = $("mode-audio"), modeTts = $("mode-tts");

function selectMode(m) {
  selectedMode = m;
  modeAudio.classList.toggle("selected", m === "audio");
  modeTts.classList.toggle("selected", m === "tts");
}
modeAudio.addEventListener("click", () => selectMode("audio"));
modeTts.addEventListener("click", () => {
  if (!HAS_KEY) { showErr("字幕翻译模式需要 DEEPSEEK_API_KEY，请先在 .env 配置并重启。"); return; }
  selectMode("tts");
});
if (!HAS_KEY) modeTts.classList.add("disabled");

let _toastTimer = null;
function toast(msg, type = "err") {
  const el = $("toast");
  el.textContent = msg;
  el.className = "show " + type;
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => { el.className = type; }, 3200);
}
function showErr(msg) { toast("错误：" + msg, "err"); }
function clearErr() { $("toast").className = ""; }

function setProgress(pct, msg) {
  $("p-fill").style.width = pct + "%";
  $("p-pct").textContent = pct + "%";
  if (msg) $("p-msg").textContent = msg;
}

const STAGE_LABEL = {
  fetching: "获取视频信息",
  downloading: "下载音频",
  subtitling: "提取字幕",
  translating: "翻译中",
  synthesizing: "合成中文语音",
  done: "完成",
  error: "出错",
  pending: "准备中",
};

/* ===== 音色选择 ===== */
function getSelectedVoice() {
  return localStorage.getItem("ytudio_voice") || DEFAULT_VOICE;
}
async function loadVoices() {
  try {
    const resp = await authFetch("/api/voices");
    const data = await resp.json();
    voices = data.voices;
    const sel = getSelectedVoice();
    renderVoices(sel);
  } catch (e) { console.error("load voices", e); }
}
// 音色预览 SVG 定义
const previewPlaySvg = `<svg class="icon" style="width: 14px; height: 14px; fill: currentColor;" viewBox="0 0 24 24"><path fill-rule="evenodd" d="M4.5 5.653c0-1.427 1.529-2.33 2.779-1.643l11.54 6.347c1.295.712 1.295 2.573 0 3.286L7.28 19.99c-1.25.687-2.779-.217-2.779-1.643V5.653z" clip-rule="evenodd" /></svg>`;
const previewPauseSvg = `<svg class="icon" style="width: 14px; height: 14px; fill: currentColor;" viewBox="0 0 24 24"><path fill-rule="evenodd" d="M6 5.25A2.25 2.25 0 018.25 3h1.5A2.25 2.25 0 0112 5.25v13.5A2.25 2.25 0 019.75 21h-1.5A2.25 2.25 0 016 18.75V5.25zM14.25 5.25A2.25 2.25 0 0116.5 3h1.5A2.25 2.25 0 0120.25 5.25v13.5A2.25 2.25 0 0118 21h-1.5a2.25 2.25 0 01-2.25-2.25V5.25z" clip-rule="evenodd" /></svg>`;

function renderVoices(selected) {
  const grid = $("voice-grid");
  const pa = previewAudio();
  grid.innerHTML = voices.map(v => {
    const isPlaying = pa.dataset.name === v.name && !pa.paused;
    return `
      <div class="voice-item ${v.name === selected ? 'selected' : ''}" data-name="${v.name}">
        <span class="v-radio"></span>
        <span class="v-label">
          <span class="v-name">${escapeHtml(v.label)}</span>
        </span>
        <span class="voice-preview" data-name="${v.name}" title="试听">${isPlaying ? previewPauseSvg : previewPlaySvg}</span>
      </div>
    `;
  }).join("");
  // 更新状态徽章
  const cur = voices.find(v => v.name === selected);
  $("voice-status-text").textContent = cur ? cur.label.split(" · ")[0] : selected;
  // 选中事件
  grid.querySelectorAll(".voice-item").forEach(el => {
    el.addEventListener("click", (e) => {
      if (e.target.classList.contains("voice-preview")) return;
      const name = el.dataset.name;
      localStorage.setItem("ytudio_voice", name);
      renderVoices(name);
    });
  });
  // 试听
  grid.querySelectorAll(".voice-preview").forEach(el => {
    el.addEventListener("click", (e) => {
      e.stopPropagation();
      previewVoice(el.dataset.name, el);
    });
  });
}
const previewAudio = () => $("preview-audio");
function previewVoice(name, btn) {
  const pa = previewAudio();
  if (pa.dataset.name === name && !pa.paused) {
    pa.pause();
    btn.classList.remove("playing"); btn.innerHTML = previewPlaySvg;
    return;
  }
  document.querySelectorAll(".voice-preview").forEach(b => { b.classList.remove("playing"); b.innerHTML = previewPlaySvg; });
  pa.src = authUrl(`/api/voice/preview/${name}`);
  pa.dataset.name = name;
  pa.play().catch(() => {});
  btn.classList.add("playing"); btn.innerHTML = previewPauseSvg;
  pa.onended = () => { btn.classList.remove("playing"); btn.innerHTML = previewPlaySvg; };
}
const voiceToggle = $("voice-toggle"), voiceBody = $("voice-body");
voiceToggle.addEventListener("click", () => {
  const open = voiceBody.classList.toggle("open");
  voiceToggle.classList.toggle("open", open);
});
loadVoices();
loadHistory();

/* ===== 处理任务 ===== */
$("btn-start").addEventListener("click", async () => {
  clearErr();
  const url = $("url").value.trim();
  if (!url) { showErr("请输入 YouTube 链接"); return; }
  if (selectedMode === "tts" && !HAS_KEY) {
    showErr("字幕翻译模式需要 DEEPSEEK_API_KEY，请先在 .env 配置并重启。"); return;
  }

  const btn = $("btn-start");
  btn.disabled = true;
  btn.textContent = "处理中…";
  $("progress").classList.add("active");
  setProgress(0, "提交任务…");

  // 显示并初始化下载预览卡片
  const previewCard = $("download-preview-card");
  const dThumb = $("d-thumb");
  const dTitle = $("d-title");
  const dChannel = $("d-channel");
  previewCard.style.display = "block";
  dTitle.textContent = "正在连接并获取视频元数据…";
  dChannel.textContent = "—";
  dThumb.src = "/icon.jpg";
  dThumb.classList.add("pulse");

  try {
    const form = new FormData();
    form.append("url", url);
    form.append("mode", selectedMode);
    if (selectedMode === "tts") form.append("voice", getSelectedVoice());
    const resp = await authFetch("/api/process", { method: "POST", body: form });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ detail: "请求失败" }));
      throw new Error(err.detail || "请求失败");
    }
    const { task_id } = await resp.json();
    currentTaskId = task_id;
    await subscribe(task_id);
  } catch (e) {
    showErr(e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "开始处理";
    currentTaskId = null;
  }
});

// 取消任务
$("p-cancel").addEventListener("click", async () => {
  if (!currentTaskId) return;
  try {
    await authFetch(`/api/cancel/${currentTaskId}`, { method: "POST" });
  } catch (e) {}
  if (currentES) { currentES.close(); currentES = null; }
  currentTaskId = null;
  $("progress").classList.remove("active");
  $("download-preview-card").style.display = "none";
  const btn = $("btn-start");
  btn.disabled = false;
  btn.textContent = "开始处理";
  toast("已取消任务", "warn");
});

function subscribe(taskId) {
  return new Promise((resolve) => {
    const es = new EventSource(authUrl(`/api/progress/${taskId}`));
    currentES = es;
    es.onmessage = (ev) => {
      let data;
      try { data = JSON.parse(ev.data); } catch { return; }

      // 如果获取到了元数据，实时更新下载卡片展示
      if (data.video_id) {
        $("d-title").textContent = data.title || "未知视频";
        $("d-channel").textContent = data.uploader || "未知作者";
        $("d-thumb").src = authUrl(`/thumb/${data.video_id}`);
        $("d-thumb").classList.remove("pulse");
      }

      if (data.error) {
        showErr(data.error);
        $("progress").classList.remove("active");
        $("download-preview-card").style.display = "none";
        currentES = null;
        es.close();
        resolve();
        return;
      }
      if (data.result) {
        setProgress(100, "完成");
        const r = data.result;
        addHistory(r);
        playIndex(0);  // 新生成的立即播放
        currentES = null;
        es.close();
        toast("处理完成，已开始播放", "ok");
        // 不再强制跳转：用户可能想继续处理下一个；播放器底栏已显示
        setTimeout(() => {
          $("progress").classList.remove("active");
          $("download-preview-card").style.display = "none";
        }, 1500);
        resolve();
        return;
      }
      const label = STAGE_LABEL[data.stage] || data.stage;
      setProgress(data.percent || 0, `${label} · ${data.message || ""}`);
    };
    es.onerror = () => { currentES = null; es.close(); resolve(); };
  });
}

/* ===== 播放器 ===== */
const audio = $("audio");
const playerBar = $("player-bar");

// ===== Media Session API：锁屏/耳机/车载控制 =====
function updateMediaSession(r) {
  if (!("mediaSession" in navigator)) return;
  navigator.mediaSession.metadata = new MediaMetadata({
    title: r.title || "未知",
    artist: r.uploader || "",
    album: "ytudio",
    artwork: [
      { src: authUrl(`/thumb/${r.video_id}`), sizes: "480x360", type: "image/jpeg" },
      { src: "/icon.jpg", sizes: "512x512", type: "image/jpeg" },
    ],
  });
}
if ("mediaSession" in navigator) {
  navigator.mediaSession.setActionHandler("play", () => audio.play());
  navigator.mediaSession.setActionHandler("pause", () => audio.pause());
  navigator.mediaSession.setActionHandler("previoustrack", () => playIndex(currentIndex - 1));
  navigator.mediaSession.setActionHandler("nexttrack", () => playIndex(currentIndex + 1));
  try {
    navigator.mediaSession.setActionHandler("seekto", (d) => {
      if (d.seekTime != null) audio.currentTime = d.seekTime;
    });
  } catch (e) {}
}

const speeds = [1.0, 1.25, 1.5, 1.75, 2.0];
let currentSpeedIndex = parseInt(localStorage.getItem("ytudio_speed") || "0", 10);
if (isNaN(currentSpeedIndex) || currentSpeedIndex < 0 || currentSpeedIndex >= speeds.length) currentSpeedIndex = 0;

function formatSpeed(s) { return s.toFixed(2).replace(/\.?0+$/, '') + "x"; }

// 播放模式图标：顺序 / 列表循环 / 单曲循环
const modeSeqSvg = `<svg class="icon" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M8.25 6.75h12M8.25 12h12m-12 5.25h12M3.75 6.75h.007v.008H3.75V6.75zm.375 0a.375.375 0 11-.75 0 .375.375 0 01.75 0zM3.75 12h.007v.008H3.75V12zm.375 0a.375.375 0 11-.75 0 .375.375 0 01.75 0zm-.375 5.25h.007v.008H3.75v-.008zm.375 0a.375.375 0 11-.75 0 .375.375 0 01.75 0z" /></svg>`;
const modeRepeatAllSvg = `<svg class="icon" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0l3.181 3.183a8.25 8.25 0 0013.803-3.7M4.031 9.865a8.25 8.25 0 0113.803-3.7l3.181 3.182m0-4.991v4.99" /></svg>`;
const modeRepeatOneSvg = `<svg class="icon" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0l3.181 3.183a8.25 8.25 0 0013.803-3.7M4.031 9.865a8.25 8.25 0 0113.803-3.7l3.181 3.182m0-4.991v4.99" /><text x="12" y="15" text-anchor="middle" font-size="9" fill="currentColor" stroke="none" font-weight="bold">1</text></svg>`;
const timerSvg = `<svg class="icon" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M12 6v6h4.5m4.5 0a9 9 0 11-18 0 9 9 0 0118 0z" /></svg>`;

function updateModeBtn() {
  const btn = $("pb-loop");
  if (playMode === "seq") {
    btn.innerHTML = modeSeqSvg; btn.title = "顺序播放"; btn.classList.remove("active");
  } else if (playMode === "repeat-all") {
    btn.innerHTML = modeRepeatAllSvg; btn.title = "列表循环"; btn.classList.add("active");
  } else {
    btn.innerHTML = modeRepeatOneSvg; btn.title = "单曲循环"; btn.classList.add("active");
  }
}

function fmtTime(s) {
  if (!s || !isFinite(s)) return "0:00";
  const m = Math.floor(s / 60), sec = Math.floor(s % 60);
  return m + ":" + (sec < 10 ? "0" : "") + sec;
}
function fmtDuration(sec) {
  if (!sec) return "";
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = Math.floor(sec % 60);
  if (h > 0) return `${h}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
  return `${m}:${String(s).padStart(2,'0')}`;
}
function fmtSize(bytes) {
  if (!bytes) return "";
  if (bytes < 1024) return bytes + "B";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(0) + "KB";
  return (bytes / 1024 / 1024).toFixed(1) + "MB";
}

function playIndex(i) {
  if (history.length === 0) return;
  // 顺序模式：越界钳制到边界（不回绕）；循环模式：回绕
  if (i < 0) i = playMode === "seq" ? 0 : history.length - 1;
  if (i >= history.length) i = playMode === "seq" ? history.length - 1 : 0;
  currentIndex = i;
  const r = history[i];
  // 记忆当前进度（切歌前）
  saveCurrentProgress();
  // 记录上次播放曲目，供刷新后恢复
  localStorage.setItem("ytudio_last_audio", r.audio_name);
  audio.src = authUrl(r.audio_url);
  $("pb-title").textContent = r.title;
  $("pb-meta").textContent = `${r.uploader || ""} · ${r.mode === "tts" ? "中文TTS" : "原音"}`;
  $("dl-link").href = authUrl("/api/download/" + r.audio_name);
  updateMediaSession(r);
  playerBar.classList.add("show");
  renderHistory();
  // 恢复记忆进度
  const resume = progressMap[r.audio_name] || 0;
  audio.play().then(() => {
    if (resume > 1 && resume < (audio.duration || Infinity) - 2) {
      audio.currentTime = resume;
    }
    // 恢复播放速度
    audio.playbackRate = speeds[currentSpeedIndex];
    updatePlayBtn(true);
  }).catch(() => updatePlayBtn(false));
}

function saveProgressToStorage() {
  try { localStorage.setItem("ytudio_progress", JSON.stringify(progressMap)); } catch (e) {}
}

function saveCurrentProgress() {
  if (currentIndex >= 0 && history[currentIndex] && audio.currentTime > 1) {
    progressMap[history[currentIndex].audio_name] = audio.currentTime;
    saveProgressToStorage();
  }
}

// 播放器 SVGs
const playSvg = `<svg class="icon" style="width: 20px; height: 20px; fill: currentColor; margin-left: 2px;" viewBox="0 0 24 24"><path fill-rule="evenodd" d="M4.5 5.653c0-1.427 1.529-2.33 2.779-1.643l11.54 6.347c1.295.712 1.295 2.573 0 3.286L7.28 19.99c-1.25.687-2.779-.217-2.779-1.643V5.653z" clip-rule="evenodd" /></svg>`;
const pauseSvg = `<svg class="icon" style="width: 20px; height: 20px; fill: currentColor;" viewBox="0 0 24 24"><path fill-rule="evenodd" d="M6 5.25A2.25 2.25 0 018.25 3h1.5A2.25 2.25 0 0112 5.25v13.5A2.25 2.25 0 019.75 21h-1.5A2.25 2.25 0 016 18.75V5.25zM14.25 5.25A2.25 2.25 0 0116.5 3h1.5A2.25 2.25 0 0120.25 5.25v13.5A2.25 2.25 0 0118 21h-1.5a2.25 2.25 0 01-2.25-2.25V5.25z" clip-rule="evenodd" /></svg>`;

function updatePlayBtn(playing) {
  $("pb-play").innerHTML = playing ? pauseSvg : playSvg;
}

$("pb-play").addEventListener("click", () => {
  if (audio.paused) { audio.play().then(() => updatePlayBtn(true)).catch(() => {}); }
  else { audio.pause(); updatePlayBtn(false); }
});
$("pb-prev").addEventListener("click", () => {
  // 若已播放超过 3 秒，先回到开头
  if (audio.currentTime > 3) { audio.currentTime = 0; }
  else { playIndex(currentIndex - 1); }
});
$("pb-next").addEventListener("click", () => playIndex(currentIndex + 1));
// 播放模式三态切换：seq → repeat-all → repeat-one → seq
$("pb-loop").addEventListener("click", () => {
  playMode = playMode === "seq" ? "repeat-all" : (playMode === "repeat-all" ? "repeat-one" : "seq");
  localStorage.setItem("ytudio_mode", playMode);
  updateModeBtn();
});
updateModeBtn();

// 播放速度控制（持久化到 localStorage）
$("pb-speed").textContent = formatSpeed(speeds[currentSpeedIndex]);
$("pb-speed").addEventListener("click", () => {
  currentSpeedIndex = (currentSpeedIndex + 1) % speeds.length;
  const speed = speeds[currentSpeedIndex];
  $("pb-speed").textContent = formatSpeed(speed);
  audio.playbackRate = speed;
  localStorage.setItem("ytudio_speed", String(currentSpeedIndex));
});

// 音频事件
let lastProgressSave = 0;
audio.addEventListener("timeupdate", () => {
  const dur = audio.duration || 0;
  $("pb-cur").textContent = fmtTime(audio.currentTime);
  $("pb-dur").textContent = fmtTime(dur);
  const pct = dur > 0 ? (audio.currentTime / dur * 100) : 0;
  $("pb-seek-fill").style.width = pct + "%";
  $("pb-seek-thumb").style.left = pct + "%";
  // 刷新当前播放项的内嵌进度条
  if (dur > 0 && currentIndex >= 0) {
    const curItem = document.querySelector(`.h-item[data-i="${currentIndex}"] .h-progress-fill`);
    if (curItem) curItem.style.width = pct + "%";
  }
  // 节流持久化播放进度（每 5 秒存一次 localStorage）
  const now = Date.now();
  if (now - lastProgressSave > 5000 && audio.currentTime > 1) {
    saveCurrentProgress();
    lastProgressSave = now;
  }
});
// 缓冲进度条更新
audio.addEventListener("progress", () => {
  const dur = audio.duration || 0;
  if (dur > 0 && audio.buffered.length > 0) {
    const buffered = audio.buffered.end(audio.buffered.length - 1);
    $("pb-seek-buffer").style.width = Math.min(100, buffered / dur * 100) + "%";
  }
});
audio.addEventListener("ended", () => {
  saveCurrentProgress();
  if (playMode === "repeat-one") {
    // 单曲循环：重播当前
    audio.currentTime = 0;
    audio.play().catch(() => {});
  } else if (currentIndex < history.length - 1) {
    playIndex(currentIndex + 1);
  } else if (playMode === "repeat-all") {
    playIndex(0);
  } else {
    updatePlayBtn(false);
  }
});
audio.addEventListener("play", () => {
  audio.playbackRate = speeds[currentSpeedIndex];
  updatePlayBtn(true);
  if ("mediaSession" in navigator) navigator.mediaSession.playbackState = "playing";
});
audio.addEventListener("pause", () => {
  updatePlayBtn(false);
  if ("mediaSession" in navigator) navigator.mediaSession.playbackState = "paused";
});

// 进度条拖动跳转（鼠标 + 触摸）
const seek = $("pb-seek");
let dragging = false;
function seekRatio(clientX) {
  const rect = seek.getBoundingClientRect();
  const ratio = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
  const dur = audio.duration || 0;
  $("pb-seek-fill").style.width = (ratio * 100) + "%";
  $("pb-seek-thumb").style.left = (ratio * 100) + "%";
  $("pb-cur").textContent = fmtTime(ratio * dur);
  return ratio;
}
seek.addEventListener("mousedown", (e) => {
  if (!audio.duration) return;
  dragging = true;
  seek.classList.add("dragging");
  seekRatio(e.clientX);
});
document.addEventListener("mousemove", (e) => {
  if (!dragging) return;
  seekRatio(e.clientX);
});
document.addEventListener("mouseup", (e) => {
  if (!dragging) return;
  dragging = false;
  seek.classList.remove("dragging");
  const ratio = seekRatio(e.clientX);
  audio.currentTime = ratio * (audio.duration || 0);
});
// 触摸拖动
seek.addEventListener("touchstart", (e) => {
  if (!audio.duration) return;
  dragging = true;
  seek.classList.add("dragging");
  seekRatio(e.touches[0].clientX);
}, { passive: true });
seek.addEventListener("touchmove", (e) => {
  if (!dragging) return;
  seekRatio(e.touches[0].clientX);
}, { passive: true });
seek.addEventListener("touchend", (e) => {
  if (!dragging) return;
  dragging = false;
  seek.classList.remove("dragging");
  const rect = seek.getBoundingClientRect();
  const ratio = Math.max(0, Math.min(1, (e.changedTouches[0].clientX - rect.left) / rect.width));
  audio.currentTime = ratio * (audio.duration || 0);
});

// 页面卸载时 flush 播放进度
window.addEventListener("pagehide", () => { saveCurrentProgress(); });

// 键盘快捷键：空格播放/暂停、←/→ 快退快进 10s、↑/↓ 上下首、L 循环
document.addEventListener("keydown", (e) => {
  // 输入框聚焦时不拦截
  const tag = (e.target.tagName || "").toLowerCase();
  if (tag === "input" || tag === "textarea" || e.target.isContentEditable) return;
  // 文稿弹层打开时仅响应 Esc
  if ($("transcript-overlay").classList.contains("show")) {
    if (e.key === "Escape") $("transcript-overlay").classList.remove("show");
    return;
  }
  switch (e.key) {
    case " ":
      e.preventDefault();
      if (audio.src) { audio.paused ? audio.play().catch(()=>{}) : audio.pause(); }
      break;
    case "ArrowLeft":
      if (audio.src) audio.currentTime = Math.max(0, audio.currentTime - 10);
      break;
    case "ArrowRight":
      if (audio.src) audio.currentTime = Math.min(audio.duration || 0, audio.currentTime + 10);
      break;
    case "ArrowUp":
      e.preventDefault();
      if (history.length) playIndex(currentIndex - 1);
      break;
    case "ArrowDown":
      e.preventDefault();
      if (history.length) playIndex(currentIndex + 1);
      break;
    case "l":
    case "L":
      $("pb-loop").click();
      break;
  }
});

function addHistory(r) {
  const idx = history.findIndex(h => h.audio_name === r.audio_name);
  if (idx >= 0) history.splice(idx, 1);
  history.unshift(r);
  renderHistory();
}

function renderHistory() {
  const box = $("history");
  // 控制清空按钮显示
  $("h-actions").style.display = history.length > 0 ? "flex" : "none";
  if (history.length === 0) {
    box.innerHTML = '<div class="empty">暂无记录。处理完成后会显示在这里。</div>';
    return;
  }
  const itemPlaySvg = `<svg class="icon" style="width: 14px; height: 14px; fill: var(--accent); margin-right: 6px;" viewBox="0 0 24 24"><path fill-rule="evenodd" d="M4.5 5.653c0-1.427 1.529-2.33 2.779-1.643l11.54 6.347c1.295.712 1.295 2.573 0 3.286L7.28 19.99c-1.25.687-2.779-.217-2.779-1.643V5.653z" clip-rule="evenodd" /></svg>`;
  const itemTrashSvg = `<svg class="icon" style="width: 14px; height: 14px;" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M14.74 9l-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 01-2.244 2.077H8.084a2.25 2.25 0 01-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 00-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 013.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 00-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 00-7.5 0" /></svg>`;
  const itemDocSvg = `<svg class="icon" style="width: 14px; height: 14px;" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m0 12.75h7.5m-7.5 3H12M10.5 2.25H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 00-9-9z" /></svg>`;

  box.innerHTML = history.map((r, i) => {
    const metaParts = [escapeHtml(r.uploader || "")];
    if (r.duration) metaParts.push(fmtDuration(r.duration));
    if (r.size) metaParts.push(fmtSize(r.size));
    const showDoc = r.mode === "tts" && r.video_id;
    // 内嵌进度条：已听比例（progressMap 秒数 / duration 秒数）
    const prog = progressMap[r.audio_name] || 0;
    const dur = r.duration || 0;
    const progressPct = (prog > 0 && dur > 0) ? Math.min(100, prog / dur * 100) : 0;
    return `
    <div class="h-item ${i === currentIndex ? 'active' : ''}" data-i="${i}">
      <span class="h-play-icon">${i === currentIndex ? itemPlaySvg : ''}</span>
      <div class="h-info">
        <div class="h-title">${escapeHtml(r.title)}</div>
        <div class="h-meta">${metaParts.filter(Boolean).join(" · ")}</div>
        <div class="h-progress"><div class="h-progress-fill" style="width: ${progressPct}%"></div></div>
      </div>
      ${showDoc ? `<button class="h-delete-btn h-doc-btn" data-vid="${escapeHtml(r.video_id)}" title="查看文稿" style="opacity:0.6">${itemDocSvg}</button>` : ''}
      <span class="badge" style="margin-right: 6px;">${r.mode === "tts" ? "中文TTS" : "原音"}</span>
      <button class="h-delete-btn" data-i="${i}" title="删除">${itemTrashSvg}</button>
    </div>`;
  }).join("");
  box.querySelectorAll(".h-item").forEach(el => {
    el.addEventListener("click", (e) => {
      if (e.target.closest(".h-delete-btn")) return;
      playIndex(parseInt(el.dataset.i, 10));
    });
  });
  box.querySelectorAll(".h-delete-btn[data-i]").forEach(el => {
    el.addEventListener("click", (e) => {
      e.stopPropagation();
      const idx = parseInt(el.dataset.i, 10);
      deleteHistoryItem(idx);
    });
  });
  box.querySelectorAll(".h-doc-btn").forEach(el => {
    el.addEventListener("click", async (e) => {
      e.stopPropagation();
      await showTranscript(el.dataset.vid);
    });
  });
}

async function showTranscript(videoId) {
  const overlay = $("transcript-overlay");
  const txt = $("transcript-text");
  $("transcript-title").textContent = "中文文稿";
  txt.textContent = "加载中…";
  overlay.classList.add("show");
  try {
    const resp = await authFetch(`/api/transcript/${videoId}`);
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ detail: "加载失败" }));
      throw new Error(err.detail || "加载失败");
    }
    const data = await resp.json();
    txt.textContent = data.transcript || "（文稿为空）";
  } catch (e) {
    txt.textContent = "加载失败：" + e.message;
  }
}
$("transcript-close").addEventListener("click", () => $("transcript-overlay").classList.remove("show"));
$("transcript-overlay").addEventListener("click", (e) => {
  if (e.target.id === "transcript-overlay") e.currentTarget.classList.remove("show");
});

async function deleteHistoryItem(idx) {
  const item = history[idx];
  if (!item) return;
  if (!confirm(`确定要删除「${item.title}」吗？`)) return;

  try {
    const resp = await authFetch(`/api/history/${item.audio_name}`, { method: "DELETE" });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ detail: "删除失败" }));
      throw new Error(err.detail || "删除失败");
    }

    history.splice(idx, 1);
    // 同步清除该条的记忆进度
    delete progressMap[item.audio_name];
    saveProgressToStorage();
    // 若删的是上次播放曲目，清除恢复标记
    if (localStorage.getItem("ytudio_last_audio") === item.audio_name) {
      localStorage.removeItem("ytudio_last_audio");
    }

    if (idx === currentIndex) {
      audio.pause();
      audio.src = "";
      currentIndex = -1;
      playerBar.classList.remove("show");
    } else if (idx < currentIndex) {
      currentIndex--;
    }

    renderHistory();
  } catch (e) {
    showErr(e.message);
  }
}

async function loadHistory() {
  try {
    const resp = await authFetch("/api/history");
    const data = await resp.json();
    history.length = 0;
    history.push(...data.history);
    renderHistory();
    // 恢复上次播放会话（设置 UI 与 audio.src，不自动播放——浏览器策略禁止）
    restoreLastSession();
  } catch (e) {
    console.error("加载历史失败", e);
  }
}

function restoreLastSession() {
  const lastName = localStorage.getItem("ytudio_last_audio");
  if (!lastName) return;
  const idx = history.findIndex(h => h.audio_name === lastName);
  if (idx < 0) return;
  currentIndex = idx;
  const r = history[idx];
  // 只设置 UI 与音频源，不自动播放（需用户点击）
  audio.src = authUrl(r.audio_url);
  $("pb-title").textContent = r.title;
  $("pb-meta").textContent = `${r.uploader || ""} · ${r.mode === "tts" ? "中文TTS" : "原音"}`;
  $("dl-link").href = authUrl("/api/download/" + r.audio_name);
  updateMediaSession(r);
  playerBar.classList.add("show");
  renderHistory();
  // 恢复播放进度（需等 metadata 加载后才能设 currentTime）
  const resume = progressMap[r.audio_name] || 0;
  const onMeta = () => {
    if (resume > 1 && resume < (audio.duration || Infinity) - 2) {
      audio.currentTime = resume;
      $("pb-cur").textContent = fmtTime(resume);
      const pct = audio.duration > 0 ? resume / audio.duration * 100 : 0;
      $("pb-seek-fill").style.width = pct + "%";
      $("pb-seek-thumb").style.left = pct + "%";
    }
  };
  audio.addEventListener("loadedmetadata", onMeta, { once: true });
  updatePlayBtn(false);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;" }[c]));
}

/* ===== 睡眠定时器 ===== */
let sleepTimer = null;
const sleepOptions = [0, 15, 30, 60, 90]; // 0 = 关闭
let sleepIndex = 0;

function updateTimerBtn() {
  const btn = $("pb-timer");
  const mins = sleepOptions[sleepIndex];
  if (mins > 0) {
    btn.classList.add("active");
    btn.textContent = mins + "′";
  } else {
    btn.classList.remove("active");
    btn.innerHTML = timerSvg;
  }
}
$("pb-timer").innerHTML = timerSvg;
$("pb-timer").addEventListener("click", () => {
  sleepIndex = (sleepIndex + 1) % sleepOptions.length;
  const mins = sleepOptions[sleepIndex];
  if (sleepTimer) { clearTimeout(sleepTimer); sleepTimer = null; }
  if (mins > 0) {
    sleepTimer = setTimeout(() => {
      audio.pause();
      toast("睡眠定时已到，播放已暂停", "warn");
      sleepIndex = 0;
      updateTimerBtn();
    }, mins * 60 * 1000);
    toast(`睡眠定时：${mins} 分钟后暂停`, "ok");
  } else {
    toast("已关闭睡眠定时", "warn");
  }
  updateTimerBtn();
});

/* ===== 清空全部历史 ===== */
$("btn-clear-all").addEventListener("click", async () => {
  if (!confirm(`确定清空全部 ${history.length} 条历史？所有音频文件将被删除，此操作不可恢复。`)) return;
  try {
    const resp = await authFetch("/api/history", { method: "DELETE" });
    if (!resp.ok) throw new Error("清空失败");
    const data = await resp.json();
    history.length = 0;
    currentIndex = -1;
    audio.pause();
    audio.src = "";
    playerBar.classList.remove("show");
    if (sleepTimer) { clearTimeout(sleepTimer); sleepTimer = null; sleepIndex = 0; updateTimerBtn(); }
    progressMap = {};
    localStorage.removeItem("ytudio_progress");
    localStorage.removeItem("ytudio_last_audio");
    renderHistory();
    toast(`已清空 ${data.count} 条历史`, "ok");
  } catch (e) {
    showErr(e.message);
  }
});

/* ===== cookies 管理面板 ===== */
const ckToggle = $("ck-toggle"), ckBody = $("ck-body");
ckToggle.addEventListener("click", () => {
  const open = ckBody.classList.toggle("open");
  ckToggle.classList.toggle("open", open);
});

function setCookieStatus(on) {
  const st = $("ck-status"), txt = $("ck-status-text");
  st.className = "ck-status " + (on ? "on" : "off");
  txt.textContent = on ? "已配置" : "未配置";
}
function ckMsg(msg, ok) {
  const el = $("ck-msg");
  el.textContent = msg || "";
  el.className = "ck-msg " + (ok ? "ok" : (msg ? "err" : ""));
}

// 上传文件 → 填入文本框
$("ck-upload-btn").addEventListener("click", () => $("ck-file").click());
$("ck-file").addEventListener("change", (e) => {
  const f = e.target.files[0];
  if (!f) return;
  const reader = new FileReader();
  reader.onload = () => { $("ck-textarea").value = reader.result; ckMsg("已载入文件，点「保存」生效", true); };
  reader.readAsText(f);
  e.target.value = "";
});

// 保存
$("ck-save").addEventListener("click", async () => {
  const content = $("ck-textarea").value.trim();
  if (!content) { ckMsg("请先粘贴或上传 cookies 内容", false); return; }
  ckMsg("保存中…", true);
  try {
    const form = new FormData();
    form.append("content", content);
    const resp = await authFetch("/api/cookies", { method: "POST", body: form });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || "保存失败");
    ckMsg("已保存: " + data.message, true);
    setCookieStatus(true);
    $("ck-textarea").value = "";
  } catch (e) {
    ckMsg(e.message, false);
  }
});

// 清除
$("ck-clear").addEventListener("click", async () => {
  if (!confirm("确定清除已保存的 cookies 吗？")) return;
  try {
    const resp = await authFetch("/api/cookies", { method: "DELETE" });
    const data = await resp.json();
    if (!resp.ok) throw new Error("清除失败");
    setCookieStatus(data.has_cookies);
    ckMsg(data.has_cookies ? "已清除页面上传的 cookies（环境变量配置仍生效）" : "成功：cookies 已清除", true);
    $("ck-textarea").value = "";
  } catch (e) {
    ckMsg(e.message, false);
  }
});

})();

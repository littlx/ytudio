// 播放器:播放/暂停/上下首/进度条/速度/循环/Media Session/进度持久化。
// 订阅 state.currentIndex 变化切换曲目;通过 window._playIndex 暴露给视图调用。

import { getState, setState, persistPlayMode, persistSpeed, loadSpeed, persistProgress, persistLastAudio } from "./state.js";
import { authUrl } from "./api.js";

const speeds = [1.0, 1.25, 1.5, 1.75, 2.0];
let currentSpeedIndex = loadSpeed();
if (isNaN(currentSpeedIndex) || currentSpeedIndex < 0 || currentSpeedIndex >= speeds.length) currentSpeedIndex = 0;

const audio = () => document.getElementById("audio");
const playerBar = () => document.getElementById("player-bar");

const playSvg = `<svg class="icon" style="width: 20px; height: 20px; fill: currentColor; margin-left: 2px;" viewBox="0 0 24 24"><path fill-rule="evenodd" d="M4.5 5.653c0-1.427 1.529-2.33 2.779-1.643l11.54 6.347c1.295.712 1.295 2.573 0 3.286L7.28 19.99c-1.25.687-2.779-.217-2.779-1.643V5.653z" clip-rule="evenodd" /></svg>`;
const pauseSvg = `<svg class="icon" style="width: 20px; height: 20px; fill: currentColor;" viewBox="0 0 24 24"><path fill-rule="evenodd" d="M6 5.25A2.25 2.25 0 018.25 3h1.5A2.25 2.25 0 0112 5.25v13.5A2.25 2.25 0 019.75 21h-1.5A2.25 2.25 0 016 18.75V5.25zM14.25 5.25A2.25 2.25 0 0116.5 3h1.5A2.25 2.25 0 0120.25 5.25v13.5A2.25 2.25 0 0118 21h-1.5a2.25 2.25 0 01-2.25-2.25V5.25z" clip-rule="evenodd" /></svg>`;
const modeSeqSvg = `<svg class="icon" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M8.25 6.75h12M8.25 12h12m-12 5.25h12M3.75 6.75h.007v.008H3.75V6.75zm.375 0a.375.375 0 11-.75 0 .375.375 0 01.75 0zM3.75 12h.007v.008H3.75V12zm.375 0a.375.375 0 11-.75 0 .375.375 0 01.75 0zm-.375 5.25h.007v.008H3.75v-.008zm.375 0a.375.375 0 11-.75 0 .375.375 0 01.75 0z" /></svg>`;
const modeRepeatAllSvg = `<svg class="icon" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0l3.181 3.183a8.25 8.25 0 0013.803-3.7M4.031 9.865a8.25 8.25 0 0113.803-3.7l3.181 3.182m0-4.991v4.99" /></svg>`;
const modeRepeatOneSvg = `<svg class="icon" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0l3.181 3.183a8.25 8.25 0 0013.803-3.7M4.031 9.865a8.25 8.25 0 0113.803-3.7l3.181 3.182m0-4.991v4.99" /><text x="12" y="15" text-anchor="middle" font-size="9" fill="currentColor" stroke="none" font-weight="bold">1</text></svg>`;
const timerSvg = `<svg class="icon" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M12 6v6h4.5m4.5 0a9 9 0 11-18 0 9 9 0 0118 0z" /></svg>`;

function fmtTime(s) {
  if (!s || !isFinite(s)) return "0:00";
  const m = Math.floor(s / 60), sec = Math.floor(s % 60);
  return m + ":" + (sec < 10 ? "0" : "") + sec;
}

function formatSpeed(s) { return s.toFixed(2).replace(/\.?0+$/, '') + "x"; }

function updatePlayBtn(playing) {
  const btn = document.getElementById("pb-play");
  if (btn) btn.innerHTML = playing ? pauseSvg : playSvg;
}

function updateModeBtn() {
  const { playMode } = getState();
  const btn = document.getElementById("pb-loop");
  if (!btn) return;
  if (playMode === "seq") {
    btn.innerHTML = modeSeqSvg; btn.title = "顺序播放"; btn.classList.remove("active");
  } else if (playMode === "repeat-all") {
    btn.innerHTML = modeRepeatAllSvg; btn.title = "列表循环"; btn.classList.add("active");
  } else {
    btn.innerHTML = modeRepeatOneSvg; btn.title = "单曲循环"; btn.classList.add("active");
  }
}

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

let lastProgressSave = 0;

export function playIndex(i) {
  const { history, playMode } = getState();
  if (history.length === 0) return;
  // 顺序模式:越界钳制到边界;循环模式:回绕
  if (i < 0) i = playMode === "seq" ? 0 : history.length - 1;
  if (i >= history.length) i = playMode === "seq" ? history.length - 1 : 0;

  // 记忆当前进度(切歌前)
  saveCurrentProgress();

  const r = history[i];
  persistLastAudio(r.video_id);
  setState({ currentIndex: i });

  const a = audio();
  a.src = authUrl(r.audio_url);
  document.getElementById("pb-title").textContent = r.title;
  document.getElementById("pb-meta").textContent = `${r.uploader || ""} · ${r.mode === "tts" ? "中文TTS" : "原音"}`;
  const dlLink = document.getElementById("dl-link");
  if (dlLink) dlLink.href = authUrl("/api/download/" + r.video_id);
  updateMediaSession(r);
  playerBar().classList.add("show");

  // 恢复记忆进度
  const resume = getState().progressMap[r.video_id] || 0;
  a.play().then(() => {
    if (resume > 1 && resume < (a.duration || Infinity) - 2) a.currentTime = resume;
    a.playbackRate = speeds[currentSpeedIndex];
    updatePlayBtn(true);
  }).catch(() => updatePlayBtn(false));
}

function saveCurrentProgress() {
  const { currentIndex, history } = getState();
  const a = audio();
  if (currentIndex >= 0 && history[currentIndex] && a.currentTime > 1) {
    persistProgress(history[currentIndex].video_id, a.currentTime);
  }
}

/** 刷新当前播放项的内嵌进度条(history.js renderHistory 不监听 timeupdate)。 */
function refreshItemProgress() {
  const a = audio();
  const dur = a.duration || 0;
  const { currentIndex } = getState();
  if (dur > 0 && currentIndex >= 0) {
    const curItem = document.querySelector(`.h-item[data-i="${currentIndex}"] .h-progress-fill`);
    if (curItem) {
      const pct = a.currentTime / dur * 100;
      curItem.style.width = pct + "%";
    }
  }
}

export function initPlayer(renderHistory) {
  const a = audio();

  // Media Session
  if ("mediaSession" in navigator) {
    navigator.mediaSession.setActionHandler("play", () => a.play());
    navigator.mediaSession.setActionHandler("pause", () => a.pause());
    navigator.mediaSession.setActionHandler("previoustrack", () => playIndex(getState().currentIndex - 1));
    navigator.mediaSession.setActionHandler("nexttrack", () => playIndex(getState().currentIndex + 1));
    try {
      navigator.mediaSession.setActionHandler("seekto", (d) => {
        if (d.seekTime != null) a.currentTime = d.seekTime;
      });
    } catch (e) {}
  }

  // 播放/暂停
  document.getElementById("pb-play").addEventListener("click", () => {
    if (a.paused) { a.play().then(() => updatePlayBtn(true)).catch(() => {}); }
    else { a.pause(); updatePlayBtn(false); }
  });
  // 上一首(已播超 3 秒先回开头)
  document.getElementById("pb-prev").addEventListener("click", () => {
    if (a.currentTime > 3) { a.currentTime = 0; }
    else { playIndex(getState().currentIndex - 1); }
  });
  document.getElementById("pb-next").addEventListener("click", () => playIndex(getState().currentIndex + 1));
  // 播放模式三态切换
  document.getElementById("pb-loop").addEventListener("click", () => {
    const cur = getState().playMode;
    const next = cur === "seq" ? "repeat-all" : (cur === "repeat-all" ? "repeat-one" : "seq");
    persistPlayMode(next);
    updateModeBtn();
  });
  updateModeBtn();

  // 速度
  document.getElementById("pb-speed").textContent = formatSpeed(speeds[currentSpeedIndex]);
  document.getElementById("pb-speed").addEventListener("click", () => {
    currentSpeedIndex = (currentSpeedIndex + 1) % speeds.length;
    const speed = speeds[currentSpeedIndex];
    document.getElementById("pb-speed").textContent = formatSpeed(speed);
    a.playbackRate = speed;
    persistSpeed(currentSpeedIndex);
  });

  // 音频事件
  a.addEventListener("timeupdate", () => {
    const dur = a.duration || 0;
    document.getElementById("pb-cur").textContent = fmtTime(a.currentTime);
    document.getElementById("pb-dur").textContent = fmtTime(dur);
    const pct = dur > 0 ? (a.currentTime / dur * 100) : 0;
    document.getElementById("pb-seek-fill").style.width = pct + "%";
    document.getElementById("pb-seek-thumb").style.left = pct + "%";
    refreshItemProgress();
    // 节流持久化(每 5 秒)
    const now = Date.now();
    if (now - lastProgressSave > 5000 && a.currentTime > 1) {
      saveCurrentProgress();
      lastProgressSave = now;
    }
  });
  a.addEventListener("progress", () => {
    const dur = a.duration || 0;
    if (dur > 0 && a.buffered.length > 0) {
      const buffered = a.buffered.end(a.buffered.length - 1);
      document.getElementById("pb-seek-buffer").style.width = Math.min(100, buffered / dur * 100) + "%";
    }
  });
  a.addEventListener("ended", () => {
    saveCurrentProgress();
    const { currentIndex, history, playMode } = getState();
    if (playMode === "repeat-one") {
      a.currentTime = 0; a.play().catch(() => {});
    } else if (currentIndex < history.length - 1) {
      playIndex(currentIndex + 1);
    } else if (playMode === "repeat-all") {
      playIndex(0);
    } else {
      updatePlayBtn(false);
    }
  });
  a.addEventListener("play", () => {
    a.playbackRate = speeds[currentSpeedIndex];
    updatePlayBtn(true);
    if ("mediaSession" in navigator) navigator.mediaSession.playbackState = "playing";
  });
  a.addEventListener("pause", () => {
    updatePlayBtn(false);
    if ("mediaSession" in navigator) navigator.mediaSession.playbackState = "paused";
  });

  // 进度条拖动(鼠标 + 触摸)
  const seek = document.getElementById("pb-seek");
  let dragging = false;
  function seekRatio(clientX) {
    const rect = seek.getBoundingClientRect();
    const ratio = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
    const dur = a.duration || 0;
    document.getElementById("pb-seek-fill").style.width = (ratio * 100) + "%";
    document.getElementById("pb-seek-thumb").style.left = (ratio * 100) + "%";
    document.getElementById("pb-cur").textContent = fmtTime(ratio * dur);
    return ratio;
  }
  seek.addEventListener("mousedown", (e) => {
    if (!a.duration) return;
    dragging = true; seek.classList.add("dragging"); seekRatio(e.clientX);
  });
  document.addEventListener("mousemove", (e) => { if (dragging) seekRatio(e.clientX); });
  document.addEventListener("mouseup", (e) => {
    if (!dragging) return;
    dragging = false; seek.classList.remove("dragging");
    a.currentTime = seekRatio(e.clientX) * (a.duration || 0);
  });
  seek.addEventListener("touchstart", (e) => {
    if (!a.duration) return;
    dragging = true; seek.classList.add("dragging"); seekRatio(e.touches[0].clientX);
  }, { passive: true });
  seek.addEventListener("touchmove", (e) => { if (dragging) seekRatio(e.touches[0].clientX); }, { passive: true });
  seek.addEventListener("touchend", (e) => {
    if (!dragging) return;
    dragging = false; seek.classList.remove("dragging");
    const rect = seek.getBoundingClientRect();
    const ratio = Math.max(0, Math.min(1, (e.changedTouches[0].clientX - rect.left) / rect.width));
    a.currentTime = ratio * (a.duration || 0);
  });

  // 卸载时 flush 进度
  window.addEventListener("pagehide", () => saveCurrentProgress());

  // 键盘快捷键
  document.addEventListener("keydown", (e) => {
    const tag = (e.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea" || e.target.isContentEditable) return;
    if (document.getElementById("transcript-overlay").classList.contains("show")) {
      if (e.key === "Escape") document.getElementById("transcript-overlay").classList.remove("show");
      return;
    }
    switch (e.key) {
      case " ":
        e.preventDefault();
        if (a.src) { a.paused ? a.play().catch(()=>{}) : a.pause(); }
        break;
      case "ArrowLeft":
        if (a.src) a.currentTime = Math.max(0, a.currentTime - 10);
        break;
      case "ArrowRight":
        if (a.src) a.currentTime = Math.min(a.duration || 0, a.currentTime + 10);
        break;
      case "ArrowUp":
        e.preventDefault();
        if (getState().history.length) playIndex(getState().currentIndex - 1);
        break;
      case "ArrowDown":
        e.preventDefault();
        if (getState().history.length) playIndex(getState().currentIndex + 1);
        break;
      case "l": case "L":
        document.getElementById("pb-loop").click();
        break;
    }
  });

  // 睡眠定时器
  let sleepTimer = null;
  const sleepOptions = [0, 15, 30, 60, 90];
  let sleepIndex = 0;
  function updateTimerBtn() {
    const btn = document.getElementById("pb-timer");
    const mins = sleepOptions[sleepIndex];
    if (mins > 0) { btn.classList.add("active"); btn.textContent = mins + "′"; }
    else { btn.classList.remove("active"); btn.innerHTML = timerSvg; }
  }
  document.getElementById("pb-timer").innerHTML = timerSvg;
  document.getElementById("pb-timer").addEventListener("click", () => {
    sleepIndex = (sleepIndex + 1) % sleepOptions.length;
    const mins = sleepOptions[sleepIndex];
    if (sleepTimer) { clearTimeout(sleepTimer); sleepTimer = null; }
    if (mins > 0) {
      sleepTimer = setTimeout(() => {
        a.pause();
        window._toast("睡眠定时已到,播放已暂停", "warn");
        sleepIndex = 0; updateTimerBtn();
      }, mins * 60 * 1000);
      window._toast(`睡眠定时:${mins} 分钟后暂停`, "ok");
    } else {
      window._toast("已关闭睡眠定时", "warn");
    }
    updateTimerBtn();
  });

  // 暴露给 history.js 调用的回调
  window._playIndex = playIndex;
  window._onHistoryDeleted = (deletedIdx, deletedVideoId) => {
    const state = getState();
    if (deletedIdx === state.currentIndex) {
      a.pause(); a.src = "";
      setState({ currentIndex: -1 });
      playerBar().classList.remove("show");
    } else if (deletedIdx < state.currentIndex) {
      setState({ currentIndex: state.currentIndex - 1 });
    }
  };
  window._onHistoryCleared = () => {
    a.pause(); a.src = "";
    setState({ currentIndex: -1 });
    playerBar().classList.remove("show");
    if (sleepTimer) { clearTimeout(sleepTimer); sleepTimer = null; sleepIndex = 0; updateTimerBtn(); }
  };
}

/** 恢复上次播放会话:设置 UI 与 audio.src,不自动播放(浏览器策略禁止)。 */
export function restoreLastSession() {
  const { history, lastAudio, progressMap } = getState();
  if (!lastAudio) return;
  const idx = history.findIndex(h => h.video_id === lastAudio);
  if (idx < 0) return;
  setState({ currentIndex: idx });
  const r = history[idx];
  const a = audio();
  a.src = authUrl(r.audio_url);
  document.getElementById("pb-title").textContent = r.title;
  document.getElementById("pb-meta").textContent = `${r.uploader || ""} · ${r.mode === "tts" ? "中文TTS" : "原音"}`;
  const dlLink = document.getElementById("dl-link");
  if (dlLink) dlLink.href = authUrl("/api/download/" + r.video_id);
  updateMediaSession(r);
  playerBar().classList.add("show");
  const resume = progressMap[r.video_id] || 0;
  const onMeta = () => {
    if (resume > 1 && resume < (a.duration || Infinity) - 2) {
      a.currentTime = resume;
      document.getElementById("pb-cur").textContent = fmtTime(resume);
      const pct = a.duration > 0 ? resume / a.duration * 100 : 0;
      document.getElementById("pb-seek-fill").style.width = pct + "%";
      document.getElementById("pb-seek-thumb").style.left = pct + "%";
    }
  };
  a.addEventListener("loadedmetadata", onMeta, { once: true });
  updatePlayBtn(false);
}

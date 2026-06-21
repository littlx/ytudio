// 唯一状态源:所有模块通过 subscribe/setState 读写状态,取代散落的全局变量。
// localStorage 持久化(playMode/progress/last_audio/voice/tab)统一在此管理。
//
// 状态字段:
//   history:      历史记录数组(每条含 video_id/title/uploader/mode/audio_url/duration...)
//   currentIndex: 当前播放项在 history 中的索引,-1 表示未选中
//   playMode:     播放模式 seq / repeat-all / repeat-one
//   progressMap:  { video_id: 秒数 } 播放进度,跨会话恢复
//   voices:       可用音色列表
//   selectedVoice: 当前选中音色
//   lastAudio:    上次播放的 video_id(刷新后恢复)

const _KEYS = {
  tab: "ytudio_tab",
  mode: "ytudio_mode",
  progress: "ytudio_progress",
  lastAudio: "ytudio_last_audio",  // 旧版存 audio_name,新版存 video_id
  voice: "ytudio_voice",
  speed: "ytudio_speed",
  token: "ytudio_token",
};

const state = {
  history: [],
  currentIndex: -1,
  playMode: "seq",
  progressMap: {},
  voices: [],
  selectedVoice: null,
  lastAudio: null,
};

const listeners = new Set();

// 初始化:从 localStorage 恢复持久化字段
try {
  state.playMode = localStorage.getItem(_KEYS.mode) || "seq";
  state.progressMap = JSON.parse(localStorage.getItem(_KEYS.progress) || "{}") || {};
  state.lastAudio = localStorage.getItem(_KEYS.lastAudio);
} catch (e) { /* 损坏的 localStorage 忽略 */ }

export function getState() { return state; }

export function subscribe(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export function setState(patch) {
  Object.assign(state, patch);
  listeners.forEach(fn => fn(state));
}

// ---- 持久化辅助 ----
export function persistPlayMode(mode) {
  state.playMode = mode;
  localStorage.setItem(_KEYS.mode, mode);
}

export function persistVoice(voice) {
  state.selectedVoice = voice;
  localStorage.setItem(_KEYS.voice, voice);
}

export function persistSpeed(idx) {
  localStorage.setItem(_KEYS.speed, String(idx));
}

export function persistProgress(videoId, seconds) {
  state.progressMap[videoId] = seconds;
  try { localStorage.setItem(_KEYS.progress, JSON.stringify(state.progressMap)); } catch (e) {}
}

export function clearProgress(videoId) {
  delete state.progressMap[videoId];
  try { localStorage.setItem(_KEYS.progress, JSON.stringify(state.progressMap)); } catch (e) {}
}

export function clearAllProgress() {
  state.progressMap = {};
  localStorage.removeItem(_KEYS.progress);
}

export function persistLastAudio(videoId) {
  state.lastAudio = videoId;
  localStorage.setItem(_KEYS.lastAudio, videoId);
}

export function clearLastAudio() {
  state.lastAudio = null;
  localStorage.removeItem(_KEYS.lastAudio);
}

export function persistTab(tab) {
  localStorage.setItem(_KEYS.tab, tab);
}

export function loadTab() {
  return localStorage.getItem(_KEYS.tab);
}

export function loadSpeed() {
  return parseInt(localStorage.getItem(_KEYS.speed) || "0", 10);
}

export { _KEYS as KEYS };

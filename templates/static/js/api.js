// API 封装:token 携带 + 各后端接口调用 + 统一错误处理。
// 后端主键已从 audio_name 切换为 video_id,所有寻址用 video_id。

const TOKEN = new URLSearchParams(location.search).get("token") || localStorage.getItem("ytudio_token");
if (TOKEN) localStorage.setItem("ytudio_token", TOKEN);

export function hasToken() { return !!TOKEN; }

/** 给无法设自定义头的资源 URL(audio/img/a/EventSource)附加 token。 */
export function authUrl(path) {
  if (!TOKEN) return path;
  return path + (path.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(TOKEN);
}

/** fetch 封装:走 Authorization 头(token 不进 URL/日志)。 */
export function authFetch(path, opts = {}) {
  if (TOKEN) {
    opts.headers = { ...(opts.headers || {}), Authorization: "Bearer " + TOKEN };
  }
  return fetch(path, opts);
}

/** 解析错误响应,抛出带 detail 信息的 Error。 */
async function _parseError(resp, fallback) {
  const err = await resp.json().catch(() => ({ detail: fallback }));
  throw new Error(err.detail || fallback);
}

// ---- 配置(由 index.html 内联脚本注入)----
export const CFG = window.YTUDIO_CONFIG || {};
export const HAS_KEY = !!CFG.has_deepseek_key;
export const HAS_COOKIES = !!CFG.has_cookies;
export const DEFAULT_VOICE = CFG.default_voice || "zh-CN-XiaoxiaoNeural";

// ---- 各 API ----
export async function fetchVoices() {
  const resp = await authFetch("/api/voices");
  return (await resp.json()).voices;
}

export async function fetchHistory() {
  const resp = await authFetch("/api/history");
  return (await resp.json()).history;
}

export async function deleteHistory(videoId) {
  const resp = await authFetch(`/api/history/${videoId}`, { method: "DELETE" });
  if (!resp.ok) await _parseError(resp, "删除失败");
  return resp.json();
}

export async function clearHistory() {
  const resp = await authFetch("/api/history", { method: "DELETE" });
  if (!resp.ok) throw new Error("清空失败");
  return resp.json();
}

export async function fetchTranscript(videoId) {
  const resp = await authFetch(`/api/transcript/${videoId}`);
  if (!resp.ok) await _parseError(resp, "加载失败");
  return (await resp.json()).transcript;
}

export async function saveCookies(content) {
  const form = new FormData();
  form.append("content", content);
  const resp = await authFetch("/api/cookies", { method: "POST", body: form });
  const data = await resp.json();
  if (!resp.ok) throw new Error(data.detail || "保存失败");
  return data;
}

export async function clearCookies() {
  const resp = await authFetch("/api/cookies", { method: "DELETE" });
  if (!resp.ok) throw new Error("清除失败");
  return resp.json();
}

export async function startTask(url, mode, voice) {
  const form = new FormData();
  form.append("url", url);
  form.append("mode", mode);
  if (mode === "tts") form.append("voice", voice);
  const resp = await authFetch("/api/process", { method: "POST", body: form });
  if (!resp.ok) await _parseError(resp, "请求失败");
  return (await resp.json()).task_id;
}

export async function cancelTask(taskId) {
  try { await authFetch(`/api/cancel/${taskId}`, { method: "POST" }); } catch (e) {}
}

/** 构造 SSE 进度流(需走 URL token,EventSource 不支持自定义头)。 */
export function progressStream(taskId) {
  return new EventSource(authUrl(`/api/progress/${taskId}`));
}

/** 从断点重试失败的任务,返回新 task_id。 */
export async function retryTask(taskId) {
  const resp = await authFetch(`/api/retry/${taskId}`, { method: "POST" });
  if (!resp.ok) await _parseError(resp, "重试失败");
  return (await resp.json()).task_id;
}

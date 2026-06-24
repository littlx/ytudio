// 历史列表视图:渲染、删除单条、清空全部、文稿弹层。
// 后端主键为 video_id,所有操作用 video_id 寻址。

import { getState, setState, persistLastAudio, clearLastAudio, persistProgress, clearProgress, clearAllProgress } from "../state.js";
import { deleteHistory, clearHistory, fetchTranscript, authUrl } from "../api.js";

const itemPlaySvg = `<svg class="icon" style="width: 14px; height: 14px; fill: var(--accent); margin-right: 6px;" viewBox="0 0 24 24"><path fill-rule="evenodd" d="M4.5 5.653c0-1.427 1.529-2.33 2.779-1.643l11.54 6.347c1.295.712 1.295 2.573 0 3.286L7.28 19.99c-1.25.687-2.779-.217-2.779-1.643V5.653z" clip-rule="evenodd" /></svg>`;
const itemTrashSvg = `<svg class="icon" style="width: 14px; height: 14px;" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M14.74 9l-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 01-2.244 2.077H8.084a2.25 2.25 0 01-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 00-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 013.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 00-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 00-7.5 0" /></svg>`;
const itemDocSvg = `<svg class="icon" style="width: 14px; height: 14px;" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m0 12.75h7.5m-7.5 3H12M10.5 2.25H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 00-9-9z" /></svg>`;

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;" }[c]));
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

let renderScheduled = false;

function doRenderHistory() {
  const { history, currentIndex, progressMap } = getState();
  const box = document.getElementById("history");
  if (!box) return;

  // 控制清空按钮显示
  const actions = document.getElementById("h-actions");
  if (actions) actions.style.display = history.length > 0 ? "flex" : "none";

  if (history.length === 0) {
    box.innerHTML = '<div class="empty">暂无记录。处理完成后会显示在这里。</div>';
    return;
  }

  box.innerHTML = history.map((r, i) => {
    const metaParts = [escapeHtml(r.uploader || "")];
    if (r.duration) metaParts.push(fmtDuration(r.duration));
    if (r.size) metaParts.push(fmtSize(r.size));
    const showDoc = r.mode === "tts" && r.video_id;
    // 内嵌进度条:已听比例
    const prog = progressMap[r.video_id] || 0;
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
      window._playIndex(parseInt(el.dataset.i, 10));
    });
  });
  box.querySelectorAll(".h-delete-btn[data-i]").forEach(el => {
    el.addEventListener("click", (e) => {
      e.stopPropagation();
      deleteHistoryItem(parseInt(el.dataset.i, 10));
    });
  });
  box.querySelectorAll(".h-doc-btn").forEach(el => {
    el.addEventListener("click", async (e) => {
      e.stopPropagation();
      await showTranscript(el.dataset.vid);
    });
  });
}

export function renderHistory() {
  if (renderScheduled) return;
  renderScheduled = true;
  queueMicrotask(() => {
    renderScheduled = false;
    doRenderHistory();
  });
}

async function deleteHistoryItem(idx) {
  const { history } = getState();
  const item = history[idx];
  if (!item) return;
  if (!confirm(`确定要删除「${item.title}」吗?`)) return;

  try {
    await deleteHistory(item.video_id);
    const newHistory = history.filter((_, i) => i !== idx);
    clearProgress(item.video_id);
    if (getState().lastAudio === item.video_id) clearLastAudio();
    setState({ history: newHistory });
    // 调整 currentIndex(由 player 模块处理播放状态)
    window._onHistoryDeleted(idx, item.video_id);
    renderHistory();
  } catch (e) {
    window._toast("错误:" + e.message, "err");
  }
}

async function showTranscript(videoId) {
  const overlay = document.getElementById("transcript-overlay");
  const txt = document.getElementById("transcript-text");
  const title = document.getElementById("transcript-title");
  if (!overlay) return;
  if (title) title.textContent = "中文文稿";
  if (txt) txt.textContent = "加载中…";
  overlay.classList.add("show");
  try {
    const transcript = await fetchTranscript(videoId);
    if (txt) txt.textContent = transcript || "(文稿为空)";
  } catch (e) {
    if (txt) txt.textContent = "加载失败:" + e.message;
  }
}

export function initHistoryActions() {
  // 清空全部
  const btn = document.getElementById("btn-clear-all");
  if (btn) {
    btn.addEventListener("click", async () => {
      const { history } = getState();
      if (!confirm(`确定清空全部 ${history.length} 条历史?所有音频文件将被删除,此操作不可恢复。`)) return;
      try {
        const data = await clearHistory();
        clearAllProgress();
        clearLastAudio();
        setState({ history: [], currentIndex: -1 });
        window._onHistoryCleared();
        renderHistory();
        window._toast(`已清空 ${data.count} 条历史`, "ok");
      } catch (e) {
        window._toast("错误:" + e.message, "err");
      }
    });
  }
  // 文稿弹层关闭
  const closeBtn = document.getElementById("transcript-close");
  if (closeBtn) {
    closeBtn.addEventListener("click", () => document.getElementById("transcript-overlay").classList.remove("show"));
  }
  const overlay = document.getElementById("transcript-overlay");
  if (overlay) {
    overlay.addEventListener("click", (e) => {
      if (e.target.id === "transcript-overlay") e.currentTarget.classList.remove("show");
    });
  }
}

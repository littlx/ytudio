// 任务处理:提交任务、SSE 进度订阅、取消、断点重试。

import { getState, setState } from "./state.js";
import { HAS_KEY, startTask, cancelTask, retryTask, progressStream, fetchTasks, authUrl } from "./api.js";
import { stageLabel } from "./views/progress.js";
import { renderHistory } from "./views/history.js";

let selectedMode = "audio";
let currentTaskId = null;
const activeES = new Map();

export function getSelectedMode() { return selectedMode; }

export function setMode(m) { selectedMode = m; }

export function initTask(toast) {
  const modeAudio = document.getElementById("mode-audio");
  const modeTts = document.getElementById("mode-tts");

  function selectMode(m) {
    setMode(m);
    modeAudio.classList.toggle("selected", m === "audio");
    modeTts.classList.toggle("selected", m === "tts");
    // 同步 radio 勾选状态
    modeAudio.querySelector('input[type="radio"]').checked = (m === "audio");
    modeTts.querySelector('input[type="radio"]').checked = (m === "tts");
  }

  modeAudio.addEventListener("click", () => selectMode("audio"));
  modeTts.addEventListener("click", () => {
    if (!HAS_KEY) {
      toast("字幕翻译模式需要 DEEPSEEK_API_KEY,请先在 .env 配置并重启。", "err");
      return;
    }
    selectMode("tts");
  });
  if (!HAS_KEY) modeTts.classList.add("disabled");

  // 初始选中 audio
  selectMode("audio");

  // 开始处理
  document.getElementById("btn-start").addEventListener("click", async () => {
    const url = document.getElementById("url").value.trim();
    if (!url) { toast("请输入 YouTube 链接", "err"); return; }
    if (selectedMode === "tts" && !HAS_KEY) {
      toast("字幕翻译模式需要 DEEPSEEK_API_KEY", "err"); return;
    }

    const btn = document.getElementById("btn-start");
    btn.disabled = true;
    btn.textContent = "提交中…";

    try {
      const voice = getState().selectedVoice || "";
      const taskId = await startTask(url, selectedMode, voice);
      
      // 清空输入框，方便用户继续添加新视频
      document.getElementById("url").value = "";
      
      trackTask(taskId, toast);
    } catch (e) {
      toast(e.message, "err");
    } finally {
      btn.disabled = false;
      btn.textContent = "开始处理";
    }
  });

  // 从断点重试失败任务
  async function retryFromCheckpoint(failedTaskId, toast) {
    const btn = document.getElementById("btn-start");
    btn.disabled = true;
    btn.textContent = "重试中…";
    try {
      const newTaskId = await retryTask(failedTaskId);
      trackTask(newTaskId, toast);
    } catch (e) {
      toast(e.message, "err");
    } finally {
      btn.disabled = false;
      btn.textContent = "开始处理";
    }
  }
  // 暴露给外部使用
  window._retryFromCheckpoint = retryFromCheckpoint;
}

function getOrCreateTaskCard(taskId, onCancel) {
  const container = document.getElementById("tasks-container");
  if (!container) return null;

  let card = document.getElementById(`task-card-${taskId}`);
  if (!card) {
    card = document.createElement("div");
    card.id = `task-card-${taskId}`;
    card.className = "task-card";
    card.style = "padding: 14px; background: var(--card-2); border: 1px solid var(--border); border-radius: 12px; animation: fadeIn 0.3s ease; display: flex; flex-direction: column; gap: 12px;";
    card.innerHTML = `
      <!-- Meta section -->
      <div style="display: flex; gap: 14px; align-items: center;">
        <img class="task-thumb" src="/icon.jpg" style="width: 80px; height: 45px; object-fit: cover; border-radius: 6px; background: var(--card); border: 1px solid var(--border); transition: all 0.3s;" />
        <div style="flex: 1; min-width: 0;">
          <div class="task-title" style="font-size: 13px; font-weight: 600; line-height: 1.4; color: var(--text); overflow: hidden; text-overflow: ellipsis; display: -webkit-box; -webkit-line-clamp: 1; -webkit-box-orient: vertical;">正在解析视频信息…</div>
          <div class="task-channel" style="font-size: 11px; color: var(--muted); margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">—</div>
        </div>
      </div>
      <!-- Progress section -->
      <div>
        <div class="p-stage" style="display: flex; justify-content: space-between; align-items: center; font-size: 12px; margin-bottom: 6px; gap: 12px;">
          <span class="msg" style="color: var(--text); font-weight: 500; flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">准备中…</span>
          <span class="pct" style="color: var(--muted); font-weight: 600; margin-right: 8px;">0%</span>
          <button class="p-cancel" style="background: transparent; border: 1px solid var(--border); color: var(--muted); border-radius: 6px; padding: 2px 8px; font-size: 11px; cursor: pointer; transition: all 0.2s;">取消</button>
        </div>
        <div class="p-bar" style="height: 4px; background: var(--border); border-radius: 2px; overflow: hidden; position: relative;">
          <div class="p-fill" style="height: 100%; width: 0%; background: var(--accent); transition: width 0.2s ease;"></div>
        </div>
      </div>
    `;
    container.appendChild(card);

    const cancelBtn = card.querySelector(".p-cancel");
    if (cancelBtn) {
      cancelBtn.addEventListener("click", () => {
        if (onCancel) onCancel(taskId);
      });
    }
  }
  return card;
}

function showCardRetry(card, onRetry) {
  const cancelBtn = card.querySelector(".p-cancel") || card.querySelector(".p-retry");
  if (cancelBtn) {
    cancelBtn.textContent = "重试";
    cancelBtn.style.color = "var(--accent)";
    cancelBtn.className = "p-retry";
    cancelBtn.style.display = "inline-block";
    const newBtn = cancelBtn.cloneNode(true);
    cancelBtn.parentNode.replaceChild(newBtn, cancelBtn);
    newBtn.addEventListener("click", onRetry);
  }
}

function updateTaskCard(taskId, data, onCancel, onRetry) {
  const card = getOrCreateTaskCard(taskId, onCancel);
  if (!card) return;

  const thumb = card.querySelector(".task-thumb");
  const title = card.querySelector(".task-title");
  const channel = card.querySelector(".task-channel");
  const msg = card.querySelector(".msg");
  const pct = card.querySelector(".pct");
  const fill = card.querySelector(".p-fill");

  if (data.video_id) {
    if (thumb && !thumb.src.includes(data.video_id)) {
      thumb.src = authUrl(`/thumb/${data.video_id}`);
    }
  }
  if (data.title && title) {
    title.textContent = data.title;
  }
  if (data.uploader && channel) {
    channel.textContent = data.uploader;
  }

  if (data.error) {
    if (msg) {
      msg.textContent = `失败: ${data.error}`;
      msg.style.color = "var(--err)";
    }
    if (pct) pct.textContent = "Error";
    if (fill) {
      fill.style.width = "100%";
      fill.style.background = "var(--err)";
    }
    if (onRetry) {
      showCardRetry(card, onRetry);
    }
  } else if (data.stage === "done") {
    if (msg) {
      msg.textContent = "完成";
      msg.style.color = "var(--accent)";
    }
    if (pct) pct.textContent = "100%";
    if (fill) {
      fill.style.width = "100%";
      fill.style.background = "var(--accent)";
    }
    const cancelBtn = card.querySelector(".p-cancel") || card.querySelector(".p-retry");
    if (cancelBtn) cancelBtn.style.display = "none";
    setTimeout(() => {
      card.style.opacity = 0;
      card.style.transition = "opacity 0.5s ease";
      setTimeout(() => card.remove(), 500);
    }, 2000);
  } else {
    const label = stageLabel(data.stage);
    if (msg) {
      msg.textContent = `${label} · ${data.message || ""}`;
      msg.style.color = "var(--text)";
    }
    const percent = data.percent || 0;
    if (pct) pct.textContent = `${percent}%`;
    if (fill) {
      fill.style.width = `${percent}%`;
      fill.style.background = "var(--accent)";
    }
  }
}

function trackTask(taskId, toast) {
  const es = progressStream(taskId);
  activeES.set(taskId, es);
  currentTaskId = taskId;

  const onCancel = async (tid) => {
    es.close();
    activeES.delete(tid);
    const card = document.getElementById(`task-card-${tid}`);
    if (card) card.remove();

    try {
      await cancelTask(tid);
      toast("已取消任务", "warn");
    } catch (e) {
      toast(`取消失败: ${e.message}`, "err");
    }
  };

  const onRetry = () => {
    activeES.delete(taskId);
    es.close();
    const card = document.getElementById(`task-card-${taskId}`);
    if (card) card.remove();
    window._retryFromCheckpoint(taskId, toast);
  };

  es.onmessage = (ev) => {
    let data;
    try { data = JSON.parse(ev.data); } catch { return; }

    updateTaskCard(taskId, data, onCancel, onRetry);

    if (data.error) {
      activeES.delete(taskId);
      es.close();
      return;
    }

    if (data.result) {
      const r = data.result;
      const { history, currentIndex } = getState();
      const idx = history.findIndex(h => h.video_id === r.video_id);
      const newHistory = idx >= 0 ? history.filter((_, i) => i !== idx) : history.slice();
      newHistory.unshift(r);

      let newCurrentIndex = currentIndex;
      let shouldLoadNewTrack = false;

      if (currentIndex >= 0 && history[currentIndex]) {
        const activeVideoId = history[currentIndex].video_id;
        if (activeVideoId === r.video_id) {
          newCurrentIndex = 0;
          shouldLoadNewTrack = true;
        } else {
          newCurrentIndex = newHistory.findIndex(h => h.video_id === activeVideoId);
          if (newCurrentIndex < 0) {
            newCurrentIndex = 0;
            shouldLoadNewTrack = true;
          }
        }
      } else {
        newCurrentIndex = 0;
        shouldLoadNewTrack = true;
      }

      setState({ history: newHistory, currentIndex: newCurrentIndex });
      renderHistory();

      if (shouldLoadNewTrack) {
        window._playIndex(0, false);
      }

      activeES.delete(taskId);
      es.close();

      updateTaskCard(taskId, { stage: "done", video_id: r.video_id, title: r.title, uploader: r.uploader }, onCancel, onRetry);
      toast(`任务「${r.title}」处理完成`, "ok");
      return;
    }
  };

  es.onerror = () => {
    activeES.delete(taskId);
    es.close();
    const card = document.getElementById(`task-card-${taskId}`);
    if (card) {
      const msg = card.querySelector(".msg");
      if (msg) msg.textContent = "连接中断，后台任务仍在排队或运行中。";
      const pct = card.querySelector(".pct");
      if (pct) pct.textContent = "Waiting";
    }
  };
}

export async function restoreActiveTasks(toast) {
  try {
    const tasks = await fetchTasks();
    for (const t of tasks) {
      if (t.stage === "error") {
        const onCancel = async (tid) => {
          const card = document.getElementById(`task-card-${tid}`);
          if (card) card.remove();
          await cancelTask(tid);
        };
        const onRetry = () => {
          const card = document.getElementById(`task-card-${t.task_id}`);
          if (card) card.remove();
          window._retryFromCheckpoint(t.task_id, toast);
        };
        updateTaskCard(t.task_id, t, onCancel, onRetry);
      } else {
        trackTask(t.task_id, toast);
      }
    }
  } catch (e) {
    console.error("恢复后台任务失败", e);
  }
}

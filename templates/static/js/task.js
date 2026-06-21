// 任务处理:提交任务、SSE 进度订阅、取消、断点重试。

import { getState, setState } from "./state.js";
import { HAS_KEY, startTask, cancelTask, retryTask, progressStream } from "./api.js";
import { setProgress, stageLabel, showPreviewCard, updatePreviewCard, hidePreviewCard, showRetryButton, hideRetryButton } from "./views/progress.js";
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
    document.getElementById("progress").classList.add("active");
    setProgress(0, "提交任务…");
    showPreviewCard();

    try {
      const voice = getState().selectedVoice || "";
      const taskId = await startTask(url, selectedMode, voice);
      
      // 清空输入框，方便用户继续添加新视频
      document.getElementById("url").value = "";
      
      trackTask(taskId, toast);
    } catch (e) {
      toast(e.message, "err");
      document.getElementById("progress").classList.remove("active");
      hidePreviewCard();
    } finally {
      btn.disabled = false;
      btn.textContent = "开始处理";
    }
  });

  // 取消
  document.getElementById("p-cancel").addEventListener("click", async () => {
    if (!currentTaskId) return;
    const cancelingId = currentTaskId;

    const es = activeES.get(cancelingId);
    if (es) {
      es.close();
      activeES.delete(cancelingId);
    }

    if (currentTaskId === cancelingId) {
      document.getElementById("progress").classList.remove("active");
      hidePreviewCard();
      hideRetryButton();
      currentTaskId = null;
    }

    try {
      await cancelTask(cancelingId);
      toast("已取消任务", "warn");
    } catch (e) {
      toast(`取消失败: ${e.message}`, "err");
    }
  });

  // 从断点重试失败任务
  async function retryFromCheckpoint(failedTaskId, toast) {
    hideRetryButton();
    const btn = document.getElementById("btn-start");
    btn.disabled = true;
    btn.textContent = "重试中…";
    document.getElementById("progress").classList.add("active");
    setProgress(0, "从断点重试…");
    showPreviewCard();
    try {
      const newTaskId = await retryTask(failedTaskId);
      trackTask(newTaskId, toast);
    } catch (e) {
      toast(e.message, "err");
      document.getElementById("progress").classList.remove("active");
      hidePreviewCard();
    } finally {
      btn.disabled = false;
      btn.textContent = "开始处理";
    }
  }
  // 暴露给外部使用
  window._retryFromCheckpoint = retryFromCheckpoint;
}

function trackTask(taskId, toast) {
  const es = progressStream(taskId);
  activeES.set(taskId, es);
  currentTaskId = taskId;

  let taskTitle = "视频";

  es.onmessage = (ev) => {
    let data;
    try { data = JSON.parse(ev.data); } catch { return; }

    if (data.title) {
      taskTitle = data.title;
    }

    if (data.video_id && taskId === currentTaskId) {
      updatePreviewCard(data);
    }

    if (data.error) {
      if (taskId === currentTaskId) {
        toast(data.error, "err");
        hideRetryButton();
        showRetryButton(() => {
          activeES.delete(taskId);
          es.close();
          window._retryFromCheckpoint(taskId, toast);
        });
      } else {
        toast(`任务「${taskTitle}」处理失败: ${data.error}`, "err");
      }
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

      if (taskId === currentTaskId) {
        setProgress(100, "完成");
        hideRetryButton();
        toast("处理完成", "ok");
        setTimeout(() => {
          if (currentTaskId === taskId) {
            document.getElementById("progress").classList.remove("active");
            hidePreviewCard();
            currentTaskId = null;
          }
        }, 1500);
      } else {
        toast(`任务「${r.title || taskTitle}」处理完成`, "ok");
      }
      return;
    }

    if (taskId === currentTaskId) {
      const label = stageLabel(data.stage);
      setProgress(data.percent || 0, `${label} · ${data.message || ""}`);
    }
  };

  es.onerror = () => {
    activeES.delete(taskId);
    es.close();
    if (taskId === currentTaskId) {
      toast(`连接中断，后台任务仍在排队或运行中。`, "warn");
      document.getElementById("progress").classList.remove("active");
      hidePreviewCard();
      currentTaskId = null;
    }
  };
}

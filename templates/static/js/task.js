// 任务处理:提交任务、SSE 进度订阅、取消、断点重试。

import { getState, setState } from "./state.js";
import { HAS_KEY, startTask, cancelTask, retryTask, progressStream } from "./api.js";
import { setProgress, stageLabel, showPreviewCard, updatePreviewCard, hidePreviewCard, showRetryButton, hideRetryButton } from "./views/progress.js";
import { renderHistory } from "./views/history.js";

let selectedMode = "audio";
let currentTaskId = null;
let currentES = null;

export function getSelectedMode() { return selectedMode; }

export function setMode(m) { selectedMode = m; }

export function initTask(toast) {
  const modeAudio = document.getElementById("mode-audio");
  const modeTts = document.getElementById("mode-tts");

  function selectMode(m) {
    setMode(m);
    modeAudio.classList.toggle("selected", m === "audio");
    modeTts.classList.toggle("selected", m === "tts");
    // 同步 radio 勾选状态(供表单语义与可访问性)
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
    btn.textContent = "处理中…";
    document.getElementById("progress").classList.add("active");
    setProgress(0, "提交任务…");
    showPreviewCard();

    try {
      const voice = getState().selectedVoice || "";
      const taskId = await startTask(url, selectedMode, voice);
      currentTaskId = taskId;
      await subscribe(taskId, toast);
    } catch (e) {
      toast(e.message, "err");
    } finally {
      // 仅当任务未在重试中(无 currentTaskId)时恢复按钮
      if (!currentTaskId) {
        btn.disabled = false;
        btn.textContent = "开始处理";
      }
    }
  });

  // 取消
  document.getElementById("p-cancel").addEventListener("click", async () => {
    if (!currentTaskId) return;
    await cancelTask(currentTaskId);
    if (currentES) { currentES.close(); currentES = null; }
    currentTaskId = null;
    document.getElementById("progress").classList.remove("active");
    hidePreviewCard();
    hideRetryButton();
    const btn = document.getElementById("btn-start");
    btn.disabled = false;
    btn.textContent = "开始处理";
    toast("已取消任务", "warn");
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
      currentTaskId = newTaskId;
      await subscribe(newTaskId, toast);
    } catch (e) {
      toast(e.message, "err");
    } finally {
      if (!currentTaskId) {
        btn.disabled = false;
        btn.textContent = "开始处理";
      }
    }
  }
  // 暴露给 subscribe 的 error 分支使用
  window._retryFromCheckpoint = retryFromCheckpoint;
}

function subscribe(taskId, toast) {
  return new Promise((resolve) => {
    const es = progressStream(taskId);
    currentES = es;
    es.onmessage = (ev) => {
      let data;
      try { data = JSON.parse(ev.data); } catch { return; }

      // 实时更新下载卡片元数据
      if (data.video_id) updatePreviewCard(data);

      if (data.error) {
        toast(data.error, "err");
        // 失败时保留进度卡片与当前 taskId(供重试),显示重试按钮
        hideRetryButton();
        showRetryButton(() => {
          const failedId = currentTaskId;
          currentTaskId = null;
          window._retryFromCheckpoint(failedId, toast);
        });
        currentES = null; es.close();
        resolve();
        return;
      }
      if (data.result) {
        setProgress(100, "完成");
        hideRetryButton();
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
          // 仅载入不自动播放
          window._playIndex(0, false);
        }

        currentES = null; es.close();
        currentTaskId = null;
        toast("处理完成", "ok");
        setTimeout(() => {
          document.getElementById("progress").classList.remove("active");
          hidePreviewCard();
        }, 1500);
        resolve();
        return;
      }
      const label = stageLabel(data.stage);
      setProgress(data.percent || 0, `${label} · ${data.message || ""}`);
    };
    es.onerror = () => { currentES = null; es.close(); resolve(); };
  });
}

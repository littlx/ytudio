// 进度条 + 下载预览卡片视图。

import { authUrl } from "../api.js";

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

export function setProgress(pct, msg) {
  const fill = document.getElementById("p-fill");
  const pctEl = document.getElementById("p-pct");
  const msgEl = document.getElementById("p-msg");
  if (fill) fill.style.width = pct + "%";
  if (pctEl) pctEl.textContent = pct + "%";
  if (msg && msgEl) msgEl.textContent = msg;
}

export function stageLabel(stage) {
  return STAGE_LABEL[stage] || stage;
}

/** 显示下载预览卡片并初始化为加载中状态。 */
export function showPreviewCard() {
  const card = document.getElementById("download-preview-card");
  const thumb = document.getElementById("d-thumb");
  const title = document.getElementById("d-title");
  const channel = document.getElementById("d-channel");
  if (!card) return;
  card.style.display = "block";
  if (title) title.textContent = "正在连接并获取视频元数据…";
  if (channel) channel.textContent = "—";
  if (thumb) {
    thumb.src = "/icon.jpg";
    thumb.classList.add("pulse");
  }
}

/** 用 SSE 推送的元数据更新预览卡片。 */
export function updatePreviewCard(data) {
  if (!data.video_id) return;
  const title = document.getElementById("d-title");
  const channel = document.getElementById("d-channel");
  const thumb = document.getElementById("d-thumb");
  if (title) title.textContent = data.title || "未知视频";
  if (channel) channel.textContent = data.uploader || "未知作者";
  if (thumb) {
    thumb.src = authUrl(`/thumb/${data.video_id}`);
    thumb.classList.remove("pulse");
  }
}

export function hidePreviewCard() {
  const card = document.getElementById("download-preview-card");
  if (card) card.style.display = "none";
}

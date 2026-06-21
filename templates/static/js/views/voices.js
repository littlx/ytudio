// 音色选择视图:渲染音色网格、试听、选中持久化。

import { getState, setState, persistVoice } from "../state.js";
import { authUrl } from "../api.js";

const previewPlaySvg = `<svg class="icon" style="width: 14px; height: 14px; fill: currentColor;" viewBox="0 0 24 24"><path fill-rule="evenodd" d="M4.5 5.653c0-1.427 1.529-2.33 2.779-1.643l11.54 6.347c1.295.712 1.295 2.573 0 3.286L7.28 19.99c-1.25.687-2.779-.217-2.779-1.643V5.653z" clip-rule="evenodd" /></svg>`;
const previewPauseSvg = `<svg class="icon" style="width: 14px; height: 14px; fill: currentColor;" viewBox="0 0 24 24"><path fill-rule="evenodd" d="M6 5.25A2.25 2.25 0 018.25 3h1.5A2.25 2.25 0 0112 5.25v13.5A2.25 2.25 0 019.75 21h-1.5A2.25 2.25 0 016 18.75V5.25zM14.25 5.25A2.25 2.25 0 0116.5 3h1.5A2.25 2.25 0 0120.25 5.25v13.5A2.25 2.25 0 0118 21h-1.5a2.25 2.25 0 01-2.25-2.25V5.25z" clip-rule="evenodd" /></svg>`;

const previewAudio = () => document.getElementById("preview-audio");

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({ "&":"&amp;","<":"&lt;","<":"&gt;",'"':"&quot;","'":"&#39;" }[c]));
}

export function renderVoices() {
  const { voices, selectedVoice } = getState();
  const grid = document.getElementById("voice-grid");
  if (!grid) return;
  const pa = previewAudio();
  grid.innerHTML = voices.map(v => {
    const isPlaying = pa.dataset.name === v.name && !pa.paused;
    return `
      <div class="voice-item ${v.name === selectedVoice ? 'selected' : ''}" data-name="${v.name}">
        <span class="v-radio"></span>
        <span class="v-label">
          <span class="v-name">${escapeHtml(v.label)}</span>
        </span>
        <span class="voice-preview" data-name="${v.name}" title="试听">${isPlaying ? previewPauseSvg : previewPlaySvg}</span>
      </div>
    `;
  }).join("");

  // 更新状态徽章
  const cur = voices.find(v => v.name === selectedVoice);
  const badge = document.getElementById("voice-status-text");
  if (badge) badge.textContent = cur ? cur.label.split(" · ")[0] : selectedVoice;

  // 选中事件
  grid.querySelectorAll(".voice-item").forEach(el => {
    el.addEventListener("click", (e) => {
      if (e.target.classList.contains("voice-preview")) return;
      const name = el.dataset.name;
      persistVoice(name);
      renderVoices();
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

export function initVoices() {
  const toggle = document.getElementById("voice-toggle");
  const body = document.getElementById("voice-body");
  if (toggle) {
    toggle.addEventListener("click", () => {
      const open = body.classList.toggle("open");
      toggle.classList.toggle("open", open);
    });
  }
}

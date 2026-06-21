// ytudio 前端入口:初始化各模块,编排事件绑定。
// 服务端配置(has_deepseek_key/has_cookies/default_voice)由 index.html 内联脚本
// 注入到 window.YTUDIO_CONFIG,api.js 读取。
import { setState, persistVoice } from "./state.js";
import { DEFAULT_VOICE, fetchVoices, fetchHistory } from "./api.js";
import { initPWA, initTabs } from "./pwa.js";
import { initVoices, renderVoices } from "./views/voices.js";
import { initCookies } from "./views/cookies.js";
import { initHistoryActions, renderHistory } from "./views/history.js";
import { initPlayer, restoreLastSession } from "./player.js";
import { initTask, restoreActiveTasks } from "./task.js";

// ---- Toast(全局提示,供各模块通过 window._toast 调用)----
let _toastTimer = null;
function toast(msg, type = "err") {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.className = "show " + type;
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => { el.className = type; }, 3200);
}
window._toast = toast;

// ---- 初始化序列 ----
initPWA(toast);
initTabs();

// 音色:从 localStorage 恢复选中,加载列表后渲染
const savedVoice = localStorage.getItem("ytudio_voice") || DEFAULT_VOICE;
persistVoice(savedVoice);

// 播放器(在历史加载前初始化,以便 restoreLastSession 可用)
initPlayer(renderHistory);

// 历史操作按钮 + 音色面板 + cookies 面板
initHistoryActions();
initVoices();
initCookies();

// 任务处理
initTask(toast);

// ---- 异步加载数据 ----
(async () => {
  // 音色列表
  try {
    const voices = await fetchVoices();
    setState({ voices, selectedVoice: savedVoice });
    renderVoices();
  } catch (e) { console.error("load voices", e); }

  // 历史列表
  try {
    const history = await fetchHistory();
    setState({ history });
    renderHistory();
    // 恢复上次播放会话(设置 UI 与 audio.src,不自动播放)
    restoreLastSession();
  } catch (e) {
    console.error("加载历史失败", e);
  }

  // 恢复正在运行或已失败的后台任务
  restoreActiveTasks(toast);
})();

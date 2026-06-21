// PWA:Service Worker 注册、更新检测、安装提示、Tab 视图切换。

import { persistTab, loadTab } from "./state.js";

let _deferredPrompt = null;

export function initPWA(toast) {
  // Service Worker 注册 + 更新检测
  if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => {
      navigator.serviceWorker.register("/sw.js")
        .then(reg => {
          reg.addEventListener("updatefound", () => {
            const nw = reg.installing;
            if (!nw) return;
            nw.addEventListener("statechange", () => {
              if (nw.state === "installed" && navigator.serviceWorker.controller) {
                toast("应用已更新,刷新生效", "ok");
              }
            });
          });
        })
        .catch(err => console.error("Service worker registration failed", err));
    });
  }

  // 安装提示
  window.addEventListener("beforeinstallprompt", (e) => {
    e.preventDefault();
    _deferredPrompt = e;
    const container = document.querySelector(".header-actions");
    if (container && !document.getElementById("install-btn")) {
      const btn = document.createElement("button");
      btn.id = "install-btn";
      btn.className = "install-header-btn";
      btn.innerHTML = '<svg class="icon" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M10.5 1.5H8.25A2.25 2.25 0 0 0 6 3.75v16.5a2.25 2.25 0 0 0 2.25 2.25h7.5A2.25 2.25 0 0 0 18 20.25V3.75a2.25 2.25 0 0 0-2.25-2.25H13.5m-3 0V3h3V1.5m-3 0h3m-3 18.75h3" /></svg><span>安装应用</span>';
      btn.addEventListener("click", async () => {
        if (!_deferredPrompt) return;
        _deferredPrompt.prompt();
        const { outcome } = await _deferredPrompt.userChoice;
        if (outcome === "accepted") toast("已添加到主屏", "ok");
        _deferredPrompt = null;
        btn.remove();
      });
      container.insertBefore(btn, container.firstChild);
    }
  });
}

/** Tab 视图切换:下载 / 播放列表。 */
export function showTab(tabId) {
  document.querySelectorAll(".tab-content").forEach(el => {
    el.style.display = el.id === `tab-${tabId}` ? "block" : "none";
  });
  document.querySelectorAll(".nav-item").forEach(el => {
    el.classList.toggle("active", el.dataset.tab === tabId);
  });
  persistTab(tabId);
}

export function initTabs() {
  document.querySelectorAll(".nav-item").forEach(el => {
    el.addEventListener("click", () => showTab(el.dataset.tab));
  });
  // 默认「下载音频」,仅当上次停在播放器时恢复
  const lastTab = loadTab();
  showTab(lastTab === "history" ? "history" : "download");
}

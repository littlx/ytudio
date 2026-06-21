// Cookies 管理面板交互:上传/粘贴/保存/清除。

import { saveCookies, clearCookies } from "../api.js";

function setCookieStatus(on) {
  const st = document.getElementById("ck-status");
  const txt = document.getElementById("ck-status-text");
  if (!st || !txt) return;
  st.className = "ck-status " + (on ? "on" : "off");
  txt.textContent = on ? "已配置" : "未配置";
}

function ckMsg(msg, ok) {
  const el = document.getElementById("ck-msg");
  if (!el) return;
  el.textContent = msg || "";
  el.className = "ck-msg " + (ok ? "ok" : (msg ? "err" : ""));
}

export function initCookies() {
  const toggle = document.getElementById("ck-toggle");
  const body = document.getElementById("ck-body");
  if (toggle) {
    toggle.addEventListener("click", () => {
      const open = body.classList.toggle("open");
      toggle.classList.toggle("open", open);
    });
  }

  // 上传文件 → 填入文本框
  const uploadBtn = document.getElementById("ck-upload-btn");
  const fileInput = document.getElementById("ck-file");
  if (uploadBtn && fileInput) {
    uploadBtn.addEventListener("click", () => fileInput.click());
    fileInput.addEventListener("change", (e) => {
      const f = e.target.files[0];
      if (!f) return;
      const reader = new FileReader();
      reader.onload = () => {
        document.getElementById("ck-textarea").value = reader.result;
        ckMsg("已载入文件,点「保存」生效", true);
      };
      reader.readAsText(f);
      e.target.value = "";
    });
  }

  // 保存
  const saveBtn = document.getElementById("ck-save");
  if (saveBtn) {
    saveBtn.addEventListener("click", async () => {
      const content = document.getElementById("ck-textarea").value.trim();
      if (!content) { ckMsg("请先粘贴或上传 cookies 内容", false); return; }
      ckMsg("保存中…", true);
      try {
        const data = await saveCookies(content);
        ckMsg("已保存: " + data.message, true);
        setCookieStatus(true);
        document.getElementById("ck-textarea").value = "";
      } catch (e) {
        ckMsg(e.message, false);
      }
    });
  }

  // 清除
  const clearBtn = document.getElementById("ck-clear");
  if (clearBtn) {
    clearBtn.addEventListener("click", async () => {
      if (!confirm("确定清除已保存的 cookies 吗?")) return;
      try {
        const data = await clearCookies();
        setCookieStatus(data.has_cookies);
        ckMsg(data.has_cookies ? "已清除页面上传的 cookies(环境变量配置仍生效)" : "成功:cookies 已清除", true);
        document.getElementById("ck-textarea").value = "";
      } catch (e) {
        ckMsg(e.message, false);
      }
    });
  }
}

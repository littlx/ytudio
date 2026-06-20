// ytudio Service Worker
// - 应用外壳（HTML/JS/CSS/图标/manifest）走 stale-while-revalidate
// - 已生成的音频与缩略图「请求即缓存」+ LRU 淘汰，支持断网离线播放
// - 版本号升级时清理旧缓存；新 SW 接管时通知前端刷新

const CACHE_NAME = "ytudio-cache-v2";
const APP_SHELL = ["/", "/manifest.json", "/icon.jpg"];

// 音频缓存独立分桶，便于单独 LRU 管理；上限 20 条
const AUDIO_CACHE = "ytudio-audio-v2";
const AUDIO_MAX_ENTRIES = 20;

// SW 安装：预缓存外壳
self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(APP_SHELL))
  );
  self.skipWaiting();
});

// 激活：清理旧版本缓存，接管所有客户端
self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys.map((key) => {
          // 仅保留当前版本的缓存，旧版（含 v1）一律删除
          if (key !== CACHE_NAME && key !== AUDIO_CACHE) {
            return caches.delete(key);
          }
        })
      )
    ).then(() => self.clients.claim())
  );
});

// LRU 淘汰：保持音频缓存条目数不超过上限
async function trimAudioCache() {
  const cache = await caches.open(AUDIO_CACHE);
  const keys = await cache.keys();
  if (keys.length <= AUDIO_MAX_ENTRIES) return;
  // 按请求时间排序较难，这里按 keys 顺序删除最旧的（FIFO 近似 LRU）
  const toRemove = keys.slice(0, keys.length - AUDIO_MAX_ENTRIES);
  await Promise.all(toRemove.map((k) => cache.delete(k)));
}

self.addEventListener("fetch", (event) => {
  const req = event.request;
  const url = new URL(req.url);

  // 非 GET（API 写操作、SSE 进度流）一律直连，不缓存
  if (req.method !== "GET") return;

  // 音频资源：请求即缓存 + 网络优先（首次需下载，离线时回退缓存）
  if (url.pathname.startsWith("/audio/")) {
    event.respondWith(
      fetch(req).then((resp) => {
        // 成功响应才缓存（克隆，因为响应体只能消费一次）
        if (resp.status === 200) {
          const clone = resp.clone();
          caches.open(AUDIO_CACHE).then((cache) => {
            cache.put(req, clone).then(trimAudioCache);
          });
        }
        return resp;
      }).catch(() => caches.match(req).then((c) => c || Response.error()))
    );
    return;
  }

  // 缩略图：请求即缓存，离线回退
  if (url.pathname.startsWith("/thumb/")) {
    event.respondWith(
      fetch(req).then((resp) => {
        if (resp.status === 200) {
          const clone = resp.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(req, clone));
        }
        return resp;
      }).catch(() => caches.match(req).then((c) => c || Response.error()))
    );
    return;
  }

  // API 等动态请求不缓存
  if (url.pathname.startsWith("/api/")) return;

  // 应用外壳：stale-while-revalidate
  event.respondWith(
    caches.match(req).then((cached) => {
      if (cached) {
        // 后台更新
        fetch(req).then((resp) => {
          if (resp.status === 200) {
            caches.open(CACHE_NAME).then((cache) => cache.put(req, resp));
          }
        }).catch(() => {});
        return cached;
      }
      return fetch(req);
    })
  );
});

// 新 SW 接管后通知前端可刷新
self.addEventListener("message", (event) => {
  if (event.data === "SKIP_WAITING") self.skipWaiting();
});

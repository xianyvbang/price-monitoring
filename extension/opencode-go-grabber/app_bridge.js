// app_bridge.js — 注入到本地 app 页面（localhost / 127.0.0.1）
// 把页面与扩展后台连起来：页面用 window.postMessage 发请求，这里转发给后台并回贴响应。
// 用 postMessage 桥接的好处是不需要硬编码扩展 ID（解包扩展的 ID 不稳定）。

(() => {
  "use strict";

  const SOURCE_PAGE = "opencode-go-grabber-page";

  window.addEventListener("message", async (event) => {
    if (event.source !== window) return;
    const data = event.data;
    if (!data || data.source !== SOURCE_PAGE || !data.type) return;

    const send = (payload) =>
      window.postMessage(
        { source: "opencode-go-grabber-ext", requestId: data.requestId, payload },
        event.origin
      );

    try {
      const resp = await new Promise((resolve) =>
        chrome.runtime.sendMessage({ type: extType(data.type) }, (r) => resolve(r))
      );
      if (chrome.runtime.lastError) {
        send({ ok: false, message: chrome.runtime.lastError.message || "扩展未就绪" });
        return;
      }
      send(resp || { ok: false, message: "空响应" });
    } catch {
      send({ ok: false, message: "扩展未就绪" });
    }
  });

  function extType(type) {
    switch (type) {
      case "grab": return "grab-from-page";
      case "push": return "push"; // 透传：app 端也可让扩展直接推送
      default: return type;
    }
  }

  // 通知页面扩展已就绪
  window.postMessage({ source: "opencode-go-grabber-ext-ready" }, "*");
})();
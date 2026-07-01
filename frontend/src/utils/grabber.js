// 与 OpenCode Go Grabber 浏览器扩展通信的小工具。
// 扩展通过 app_bridge.js content script 监听 window.postMessage 并转发给后台。
// 不需要扩展 ID（解包扩展 ID 不稳定），用 postMessage 桥接。

const SOURCE_PAGE = "opencode-go-grabber-page";
const SOURCE_EXT = "opencode-go-grabber-ext";

let ready = false;
let readyListener = null;

if (typeof window !== "undefined") {
  readyListener = (event) => {
    if (event.source === window && event.data && event.data.source === `${SOURCE_EXT}-ready`) {
      ready = true;
    }
  };
  window.addEventListener("message", readyListener);
}

// 扩展是否已注入并就绪
export function isGrabberReady() {
  return ready;
}

// 抓取：让扩展返回当前 opencode.ai 的 workspace_id + Cookie 头（含 auth）
export function grabFromExtension(timeoutMs = 5000) {
  return postToExtension({ type: "grab" }, timeoutMs);
}

// 让扩展直接推送（建号 + 导入登录态 + 刷新）。payload 同扩展 push 协议
export function pushViaExtension(payload, timeoutMs = 60000) {
  return postToExtension({ type: "push", payload }, timeoutMs);
}

function postToExtension(req, timeoutMs) {
  return new Promise((resolve) => {
    if (typeof window === "undefined") {
      resolve({ ok: false, message: "非浏览器环境" });
      return;
    }
    const requestId = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    let done = false;
    const timer = setTimeout(() => {
      if (done) return;
      done = true;
      window.removeEventListener("message", onResp);
      resolve({ ok: false, message: "未检测到扩展，请确认已安装并启用 OpenCode Go Grabber 扩展" });
    }, timeoutMs);

    function onResp(event) {
      if (event.source !== window) return;
      const d = event.data;
      if (!d || d.source !== SOURCE_EXT || d.requestId !== requestId) return;
      done = true;
      clearTimeout(timer);
      window.removeEventListener("message", onResp);
      resolve(d.payload || { ok: false, message: "空响应" });
    }
    window.addEventListener("message", onResp);
    window.postMessage({ source: SOURCE_PAGE, requestId, ...req }, "*");
  });
}

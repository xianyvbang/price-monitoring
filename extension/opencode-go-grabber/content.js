// content.js — 注入到 https://opencode.ai/*
// 职责：
//  1. 从当前页 URL 解析 workspace_id（wrk_<编码>）
//  2. 猴补丁 window.fetch，拦截 /_server 响应，解包出 OpenCode API key（与 app 端 _best_api_key 优先级一致）
//  3. 响应 popup / background 的取值请求

(() => {
  "use strict";

  // 与 app/services/opencode_go.py _best_api_key 字段优先级保持一致
  const KEY_FIELDS = ["key", "api_key", "apiKey", "token"];

  function isMasked(v) {
    if (typeof v !== "string" || !v) return true;
    // 与 app 端 _mask_opencode_api_key 思路一致：含较多 * 的视为已掩码
    return v.includes("*") && v.replace(/\*/g, "").length < v.length / 2;
  }

  function pickBestKey(obj) {
    if (!obj || typeof obj !== "object") return null;
    for (const f of KEY_FIELDS) {
      const v = obj[f];
      if (typeof v === "string" && v && !isMasked(v)) return v;
    }
    return null;
  }

  // 深度遍历，取出候选 key（key.list 响应里 keys 一般是数组，每个元素含 key 字段）
  function scanForKeys(data, depth = 0) {
    if (depth > 8 || data == null) return null;
    if (Array.isArray(data)) {
      // 优先逐元素尝试取 key（每个 key 对象）
      for (const item of data) {
        const k = pickBestKey(item);
        if (k) return k;
      }
      for (const item of data) {
        const k = scanForKeys(item, depth + 1);
        if (k) return k;
      }
      return null;
    }
    if (typeof data === "object") {
      const direct = pickBestKey(data);
      if (direct) return direct;
      for (const v of Object.values(data)) {
        const k = scanForKeys(v, depth + 1);
        if (k) return k;
      }
    }
    return null;
  }

  // opencode 的 _server 返回一般是被 server-fn wrapper 包过的 JSON；尝试常见包装字段
  function unwrapServerBody(json) {
    if (json == null) return json;
    if (Array.isArray(json)) return json;
    if (typeof json === "object") {
      // 常见：{ data: {...} } / { result: {...} } / { output: {...} } / { keys: [...] }
      for (const f of ["data", "result", "output", "keys", "items", "value"]) {
        if (f in json) return json[f];
      }
    }
    return json;
  }

  function parseWorkspaceId(pathname) {
    // /workspace/wrk_xxx 或 /workspace/wrk_xxx/keys
    const m = String(pathname || "").match(/wrk_[A-Za-z0-9]+/);
    return m ? m[0] : "";
  }

  function getWorkspaceId() {
    try {
      return parseWorkspaceId(location.pathname);
    } catch {
      return "";
    }
  }

  function storeCapture(payload) {
    // 存到 session storage 供 popup/background 取（不依赖激活 tab 是哪个）
    const ws = payload.workspaceId != null ? payload.workspaceId : getWorkspaceId();
    try {
      chrome.storage.session.set({
        capture: {
          workspaceId: ws,
          apiKey: payload.apiKey || "",
          capturedAt: payload.capturedAt || 0,
        },
      });
    } catch {
      // chrome.storage.session 在部分环境不可用，退化为 message 通道
    }
    // 同时广播给 background，便于其维护最近值
    try {
      chrome.runtime.sendMessage({ type: "capture-update", payload: { workspaceId: ws, apiKey: payload.apiKey, capturedAt: payload.capturedAt } });
    } catch {}
  }

  // SPA 内导航（/workspace/A → /workspace/B）不重载 content script，需要自己监听 URL 变化更新 workspace
  function watchUrl() {
    let last = location.pathname + location.search;
    const fire = () => {
      const cur = location.pathname + location.search;
      if (cur === last) return;
      last = cur;
      storeCapture({});
    };
    // popstate + history pushState/replaceState 猴补丁 + 轮询兜底
    window.addEventListener("popstate", fire);
    ["pushState", "replaceState"].forEach((m) => {
      const orig = history[m];
      history[m] = function (...args) {
        const r = orig.apply(this, args);
        setTimeout(fire, 0);
        return r;
      };
    });
    setInterval(fire, 1000);
  }

  // ---- fetch 猴补丁 ----
  const _fetch = window.fetch;
  window.fetch = function (...args) {
    const p = _fetch.apply(this, args);
    const url = typeof args[0] === "string" ? args[0] : args[0] && args[0].url;
    if (url && url.includes("/_server")) {
      p.then((resp) => {
        if (!resp.ok) return;
        const cloned = resp.clone();
        cloned.json().then((json) => {
          const body = unwrapServerBody(json);
          const key = scanForKeys(body);
          if (key) {
            storeCapture({ apiKey: key, capturedAt: Date.now() });
          }
        }).catch(() => {});
      }).catch(() => {});
    }
    return p;
  };

  // ---- XHR 猴补丁（部分接口走 XHR） ----
  const _open = XMLHttpRequest.prototype.open;
  const _send = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    this.__grabberUrl = url;
    return _open.call(this, method, url, ...rest);
  };
  XMLHttpRequest.prototype.send = function (...args) {
    this.addEventListener("load", () => {
      try {
        if (!this.__grabberUrl || !String(this.__grabberUrl).includes("/_server")) return;
        if (this.status < 200 || this.status >= 300) return;
        let json;
        try {
          json = JSON.parse(this.responseText);
        } catch {
          return;
        }
        const body = unwrapServerBody(json);
        const key = scanForKeys(body);
        if (key) {
          storeCapture({ apiKey: key, capturedAt: Date.now() });
        }
      } catch {}
    });
    return _send.apply(this, args);
  };

  // ---- 响应取值请求 ----
  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg && msg.type === "get-capture") {
      sendResponse({
        workspaceId: getWorkspaceId(),
        url: location.href,
      });
      return false;
    }
    return false;
  });

  // 启动时上报一次 workspace_id（cookie 由 background 的 webRequest 捕）
  storeCapture({});
  watchUrl();
})();
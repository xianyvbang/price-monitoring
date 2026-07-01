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

  // ---- 文本兜底：对整段响应文本跑正则提取，不依赖 JSON 能否解析 ----
  // 对齐 app/services/opencode_go.py parse_server_function_key_response 的正则思路
  // _server 响应常是 RSC/server-fn 流式分块，JSON.parse 可能失败或结构不符，
  // 直接对文本扫描最稳。
  const KEY_OBJ_RE = /\{([^{}]*(?:\b(?:key|apiKey|api_key|token)\b)\s*:[^{}]*)\}/g;
  const FIELD_RE = /([A-Za-z_$][\w$]*|"[^"]+"|'[^']+')\s*:\s*("(?:\\.|[^"])*"|'(?:\\.|[^'])*'|-?\d+(?:\.\d+)?|true|false|null)/g;
  function stripName(s) {
    if (!s) return s;
    if (s[0] === '"' || s[0] === "'") return s.slice(1, -1);
    return s;
  }
  function parseScalar(s) {
    if (!s) return s;
    if (s[0] === '"' || s[0] === "'") {
      try { return JSON.parse(s[0] === "'" ? `"${s.slice(1, -1).replace(/"/g, '\\"')}"` : s); } catch { return s.slice(1, -1); }
    }
    if (s === "true") return true;
    if (s === "false") return false;
    if (s === "null") return null;
    return s;
  }
  function extractKeyFromText(text) {
    const t = String(text || "");
    if (!/apiKey|api_key|\bkey\b|token/.test(t)) return null;
    const items = [];
    let m;
    KEY_OBJ_RE.lastIndex = 0;
    while ((m = KEY_OBJ_RE.exec(t)) !== null) {
      const body = m[1];
      const item = {};
      FIELD_RE.lastIndex = 0;
      let fm;
      while ((fm = FIELD_RE.exec(body)) !== null) {
        item[stripName(fm[1])] = parseScalar(fm[2].trim());
      }
      for (const f of KEY_FIELDS) {
        const v = item[f];
        if (typeof v === "string" && v && !isMasked(v)) return v;
      }
    }
    return null;
  }
  function extractWorkspaceFromText(text) {
    const t = String(text || "");
    const m = t.match(/wrk_[A-Za-z0-9]+/);
    return m ? m[0] : null;
  }

  // 统一处理 _server 响应文本：JSON 与文本两条路都走，取到即用
  function handleServerText(text) {
    const key = extractKeyFromText(text);
    const ws = extractWorkspaceFromText(text);
    try { console.log("%c[Grabber ISO]", "color:#16a34a;font-weight:bold", "handleServerText: key=", key, "ws=", ws, "len=", (text||"").length); } catch {}
    // 同时尝试 JSON 路径（结构化解析更准，比如带掩码判断）
    if (text) {
      try {
        const json = JSON.parse(text);
        const body = unwrapServerBody(json);
        if (!key) {
          const k2 = scanForKeys(body);
          if (k2) {
            storeCapture({ workspaceId: ws || undefined, apiKey: k2, capturedAt: Date.now() });
            return;
          }
        }
        const ws2 = scanForWorkspace(body);
        if (key || ws || ws2) {
          storeCapture({ workspaceId: ws || ws2 || undefined, apiKey: key || undefined, capturedAt: Date.now() });
          return;
        }
      } catch {}
    }
    if (key || ws) {
      storeCapture({ workspaceId: ws || undefined, apiKey: key || undefined, capturedAt: Date.now() });
    }
  }

  // 深度遍历，从 _server 响应（session.get 等）里解出 workspace_id
  // 与 app/services/opencode_go.py _workspace_id_from_session 字段/结构保持一致
  const WS_KEYS = ["workspaceID", "workspaceId", "workspace_id", "activeWorkspaceID", "activeWorkspaceId", "currentWorkspaceId"];
  const WS_OBJ_KEYS = ["workspace", "activeWorkspace", "currentWorkspace"];

  function looksLikeWorkspaceId(v) {
    return typeof v === "string" && /^wrk_[A-Za-z0-9]+$/.test(v);
  }

  function scanForWorkspace(data, depth = 0) {
    if (depth > 10 || data == null) return null;
    if (Array.isArray(data)) {
      for (const item of data) {
        const w = scanForWorkspace(item, depth + 1);
        if (w) return w;
      }
      return null;
    }
    if (typeof data === "object") {
      for (const k of WS_KEYS) {
        const v = data[k];
        if (looksLikeWorkspaceId(v)) return v;
      }
      for (const k of WS_OBJ_KEYS) {
        const obj = data[k];
        if (obj && typeof obj === "object") {
          const v = obj.id || obj.workspaceID || obj.workspaceId;
          if (looksLikeWorkspaceId(v)) return v;
        }
      }
      const list = data.workspaces;
      if (Array.isArray(list)) {
        for (const w of list) {
          if (w && typeof w === "object" && looksLikeWorkspaceId(w.id)) return w.id;
        }
      }
      for (const v of Object.values(data)) {
        const w = scanForWorkspace(v, depth + 1);
        if (w) return w;
      }
    }
    return null;
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

  // ---- 接收 MAIN world（content_main.js）拦到的 /_server 响应文本 ----
  // isolated world 自己 hook fetch 拦不到页面真实调用，hook 已移到 MAIN world 脚本。
  const dbgIso = (...a) => { try { console.log("%c[Grabber ISO]", "color:#16a34a;font-weight:bold", ...a); } catch {} };
  window.addEventListener("message", (event) => {
    if (event.source !== window) return;
    const d = event.data;
    if (!d || d.source !== "opencode-go-grabber-main") return;
    const p = d.payload || {};
    if (p.kind === "server-text" && typeof p.text === "string") {
      dbgIso("got server-text", p.url, "len", p.text.length);
      handleServerText(p.text);
    }
  });

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
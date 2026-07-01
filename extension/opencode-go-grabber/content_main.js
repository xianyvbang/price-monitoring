// content_main.js — 在 MAIN world 注入（与页面共享 window）
// 职责：真正拦到页面 fetch/XHR 对 /_server 的请求，把响应文本 postMessage 给
// isolated world 的 content.js（它负责写 storage / 广播给 background）。
//
// 为什么要 MAIN world：MV3 content script 默认在 isolated world，hook 的 window.fetch
// 是 isolated 的副本；opencode（Next.js/RSC）在 main world 调 window.fetch，isolated
// hook 拦不到，导致 api_key 始终「未捕获」。

(() => {
  "use strict";
  if (window.__opencodeGoGrabberMainHook) return;
  window.__opencodeGoGrabberMainHook = true;

  const post = (payload) => {
    try {
      window.postMessage({ source: "opencode-go-grabber-main", payload }, "*");
    } catch {}
  };

  // 临时诊断：把 hook 进度打到页面 console，方便 F12 排查
  const dbg = (...a) => {
    try { console.log("%c[Grabber MAIN]", "color:#2563eb;font-weight:bold", ...a); } catch {}
  };
  dbg("MAIN world hook installed at", location.href);

  const _fetch = window.fetch;
  if (_fetch) {
    window.fetch = function (...args) {
      const p = _fetch.apply(this, args);
      try {
        const url = typeof args[0] === "string" ? args[0] : args[0] && args[0].url;
        const u = String(url || "");
        if (u.includes("/_server")) {
          dbg("fetch /_server hit:", u);
          p.then((resp) => {
            dbg("fetch /_server resp:", u, "status", resp && resp.status);
            if (!resp || !resp.ok) return;
            const cloned = resp.clone();
            cloned.text().then((text) => {
              dbg("fetch /_server body len", (text || "").length, "preview:", String(text).slice(0, 120));
              post({ kind: "server-text", url: u, text });
            }).catch((e) => dbg("fetch text err", e));
          }).catch((e) => dbg("fetch resp err", e));
        }
      } catch {}
      return p;
    };
    dbg("fetch hooked");
  } else {
    dbg("window.fetch NOT found at install time");
  }

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
        post({ kind: "server-text", url: String(this.__grabberUrl), text: this.responseText });
      } catch {}
    });
    return _send.apply(this, args);
  };
})();
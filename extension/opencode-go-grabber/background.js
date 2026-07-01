// background.js — service worker (MV3)
// 职责：
//  1. webRequest 捕获 https://opencode.ai/_server 请求的 Cookie 头
//  2. 与 popup/content 通信，提供"当前捕获"快照
//  3. 推送到 python-get-price app：建/更新账号 → 导入登录态 → /refresh

const DEFAULT_SESSION_COOKIE = "balance_monitor_session";
const DEFAULT_APP_BASE = "http://localhost:8000";

// 内存中最近一次 /_server 的 cookie（service worker 可能被回收，配合 chrome.storage.session）
let latestCookieHeader = "";
let latestCookieAt = 0;

// 内存中最近一次 content.js 上报的 capture（api_key / workspace_id）。
// 必须保留内存副本：chrome.storage.session 在 content script 上下文不可用（抛
// "Access to storage is not allowed from this context"），content.js 的 storage 写入会失败，
// 只能靠 capture-update 消息把数据送到 background 这里缓存。
let latestCapture = null;

chrome.webRequest.onBeforeSendHeaders.addListener(
  (details) => {
    let cookie = "";
    for (const h of details.requestHeaders || []) {
      if (h.name && h.name.toLowerCase() === "cookie") {
        cookie = h.value || "";
        break;
      }
    }
    if (cookie) {
      latestCookieHeader = cookie;
      latestCookieAt = Date.now();
      chrome.storage.session.set({ opencodeCookie: cookie, opencodeCookieAt: latestCookieAt });
    }
  },
  { urls: ["https://opencode.ai/_server*", "https://opencode.ai/_server"] },
  ["requestHeaders", "extraHeaders"]
);

async function getStored() {
  const keys = ["appBase", "sessionCookieName", "adminUser", "adminPassword", "defaultRecoveryEmail"];
  const local = await chrome.storage.local.get(keys);
  return {
    appBase: (local.appBase || DEFAULT_APP_BASE).replace(/\/$/, ""),
    sessionCookie: local.sessionCookieName || DEFAULT_SESSION_COOKIE,
    adminUser: local.adminUser || "",
    adminPassword: local.adminPassword || "",
  };
}

// 取最近 /_server cookie；若太旧或没有，退化用 chrome.cookies.getAll 拼 opencode.ai 域
async function getOpencodeCookie() {
  const sess = await chrome.storage.session.get(["opencodeCookie", "opencodeCookieAt"]);
  const now = Date.now();
  if (sess.opencodeCookie && sess.opencodeCookieAt && now - sess.opencodeCookieAt < 5 * 60 * 1000) {
    return sess.opencodeCookie;
  }
  // 退化：拼 opencode.ai 域所有 cookie
  try {
    const all = await chrome.cookies.getAll({ domain: "opencode.ai" });
    if (all && all.length) {
      return all.map((c) => `${c.name}=${c.value}`).join("; ");
    }
  } catch {}
  // 内存兜底
  if (latestCookieHeader) return latestCookieHeader;
  return "";
}

function cookieHasAuth(cookieHeader) {
  return /(^|;\s*)auth=([^;]+)/.test(cookieHeader || "");
}

// 调 app 接口：自动带会话 cookie；遇 401 用 admin 凭据兜底登录后重试一次
async function appFetch(cfg, path, init = {}) {
  const url = cfg.appBase + path;
  const doFetch = () =>
    chrome.cookies.get({ url: cfg.appBase, name: cfg.sessionCookie }).then((ck) => {
      const headers = Object.assign({}, init.headers || {});
      if (ck && ck.value) headers["Cookie"] = `${cfg.sessionCookie}=${ck.value}`;
      if (init.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
      return fetch(url, Object.assign({}, init, { headers }));
    });

  let resp = await doFetch();
  if (resp.status === 401 && cfg.adminUser && cfg.adminPassword) {
    const ok = await loginToApp(cfg);
    if (ok) resp = await doFetch();
  }
  return resp;
}

async function loginToApp(cfg) {
  try {
    const resp = await fetch(cfg.appBase + "/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: cfg.adminUser, password: cfg.adminPassword }),
      credentials: "include",
    });
    return resp.ok;
  } catch {
    return false;
  }
}

async function pushToApp({ email, password, recoveryEmail, workspaceId, cookieHeader, enabled }) {
  const cfg = await getStored();
  const errs = [];
  if (!email) errs.push("邮箱必填");
  if (!password) errs.push("密码必填");
  if (!workspaceId) errs.push("workspace_id 必填（请在 opencode.ai/workspace 页打开）");
  if (!cookieHasAuth(cookieHeader)) errs.push("Cookie 必须包含 auth（请先登录 opencode.ai）");
  if (errs.length) return { ok: false, message: errs.join("；") };

  // 1. 创建/更新账号
  let resp = await appFetch(cfg, "/api/opencode-go/accounts", {
    method: "POST",
    body: JSON.stringify({
      email,
      password,
      recovery_email: recoveryEmail || undefined,
      workspace_id: workspaceId,
      is_enabled: !!enabled,
    }),
  });
  let data = await resp.json().catch(() => ({}));
  if (!resp.ok || !data.ok) {
    return { ok: false, message: (data && data.message) || `创建账号失败 (${resp.status})` };
  }
  const accountId = data.id;

  // 2. 导入登录态
  resp = await appFetch(cfg, `/api/opencode-go/accounts/${accountId}/session`, {
    method: "POST",
    body: JSON.stringify({ workspace_id: workspaceId, cookie: cookieHeader }),
  });
  data = await resp.json().catch(() => ({}));
  if (!resp.ok || !data.ok) {
    // api_key 刷新前若已有则会保留；导入失败直接返回
    return { ok: false, message: (data && data.message) || "导入登录态失败", accountId };
  }

  // 3. 触发刷新（服务端自行回填 api_key）
  resp = await appFetch(cfg, `/api/opencode-go/accounts/${accountId}/refresh`, { method: "POST" });
  data = await resp.json().catch(() => ({}));
  if (!resp.ok || !data.ok) {
    // refresh 失败仅作为提示，账号与登录态已就位
    const msg = data && (data.message || (data.result && data.result.invalid_message));
    return { ok: false, message: `已建号并导入登录态，但刷新失败：${msg || resp.status}`, accountId };
  }

  return {
    ok: true,
    accountId,
    account: data.account || null,
    maskedKey: data.account ? data.account.api_key_masked || data.account.apiKeyMasked : "",
  };
}

// 供 app 页面（通过 app_bridge.js 转发）调用：返回 workspace_id + cookie
async function grabFromPage() {
  // workspace_id：优先从 storage 取（content.js 在 opencode.ai 页持续写入，不依赖激活 tab 是哪个）
  let workspaceId = "";
  try {
    const sess = await chrome.storage.session.get(["capture"]);
    workspaceId = (sess.capture && sess.capture.workspaceId) || "";
  } catch {}
  // 兜底：若 storage 没有（content.js 还没注入过），再问激活 tab
  if (!workspaceId) {
    try {
      const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
      const tab = tabs[0];
      if (tab && /^https:\/\/opencode\.ai\//.test(tab.url || "")) {
        workspaceId = await new Promise((resolve) =>
          chrome.tabs.sendMessage(tab.id, { type: "get-capture" }, (r) => resolve((r && r.workspaceId) || ""))
        );
      }
    } catch {}
  }
  const cookieHeader = await getOpencodeCookie();
  return {
    ok: true,
    workspaceId,
    cookieHeader,
    hasAuth: cookieHasAuth(cookieHeader),
    cookieAt: latestCookieAt,
  };
}

function openExtensionOptionsPage() {
  return new Promise((resolve) => {
    try {
      if (chrome.runtime.openOptionsPage) {
        chrome.runtime.openOptionsPage(() => {
          const err = chrome.runtime.lastError;
          resolve(err ? { ok: false, message: err.message } : { ok: true });
        });
        return;
      }
      chrome.tabs.create({ url: chrome.runtime.getURL("options.html") }, () => {
        const err = chrome.runtime.lastError;
        resolve(err ? { ok: false, message: err.message } : { ok: true });
      });
    } catch (e) {
      resolve({ ok: false, message: e && e.message ? e.message : String(e) });
    }
  });
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg.type === "open-options") {
    openExtensionOptionsPage().then(sendResponse);
    return true;
  }
  if (msg.type === "get-snapshot") {
    (async () => {
      const cookieHeader = await getOpencodeCookie();
      const sess = await chrome.storage.session.get(["opencodeCookieAt", "capture"]);
      // content.js 的 storage 写入会失败（context 限制），优先用 background 缓存的内存副本
      const capture = latestCapture || sess.capture || {};
      sendResponse({
        cookieHeader,
        hasAuth: cookieHasAuth(cookieHeader),
        cookieAt: sess.opencodeCookieAt || latestCookieAt,
        capture,
      });
    })();
    return true; // async
  }
  if (msg.type === "capture-update") {
    // content.js（ISO world）抓到 api_key/workspace_id 后广播过来；background 这边
    // 缓存到内存，并尝试写入 session storage（SW 上下文允许，供 popup 同源读取）。
    // 注意：storeCapture() 在启动和 URL 变化时会用空 payload 调用一次，只为驱动
    // workspace 更新；这里按字段增量合并，避免空值把刚抓到的 api_key 覆盖清掉。
    const p = msg.payload || {};
    const merged = {
      workspaceId: p.workspaceId != null && p.workspaceId !== "" ? p.workspaceId : (latestCapture && latestCapture.workspaceId) || "",
      apiKey: p.apiKey != null && p.apiKey !== "" ? p.apiKey : (latestCapture && latestCapture.apiKey) || "",
      capturedAt: p.capturedAt || (latestCapture && latestCapture.capturedAt) || 0,
    };
    latestCapture = merged;
    try {
      chrome.storage.session.set({ capture: merged });
    } catch {}
    return false;
  }
  if (msg.type === "get-workspace") {
    // 优先从 storage 取（content.js 在 opencode.ai 页持续写入，不依赖激活 tab 是哪个）
    (async () => {
      let workspaceId = "";
      try {
        const sess = await chrome.storage.session.get(["capture"]);
        workspaceId = (sess.capture && sess.capture.workspaceId) || "";
      } catch {}
      // storage 写入失败时用 background 内存缓存（content.js 广播的 capture-update）
      if (!workspaceId && latestCapture && latestCapture.workspaceId) {
        workspaceId = latestCapture.workspaceId;
      }
      if (!workspaceId) {
        // 兜底：问当前激活 tab 的 content script
        const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
        const tab = tabs[0];
        if (tab && /^https:\/\/opencode\.ai\//.test(tab.url || "")) {
          workspaceId = await new Promise((resolve) =>
            chrome.tabs.sendMessage(tab.id, { type: "get-capture" }, (r) => resolve((r && r.workspaceId) || ""))
          );
        }
      }
      sendResponse({ workspaceId });
    })();
    return true;
  }
  if (msg.type === "push") {
    pushToApp(msg.payload).then(sendResponse);
    return true;
  }
  if (msg.type === "grab-from-page") {
    grabFromPage().then(sendResponse);
    return true;
  }
  return false;
});

(() => {
  "use strict";

  if (window.__accountGrabberContent) return;
  window.__accountGrabberContent = true;

  if (!/^https?:$/.test(location.protocol) || location.hostname === "opencode.ai") return;

  const SOURCE = "account-grabber-main";
  const CORE = window.AccountGrabberCore || {};
  const PREFIXES = new Set(["www", "api", "app", "admin", "panel", "console", "newapi", "sub2api"]);
  const SUFFIXES = new Set(["com", "net", "org", "io", "ai", "cc", "cn", "top", "xyz", "site", "app", "dev", "cloud", "co", "uk"]);
  const KEY_FIELDS = ["key", "api_key", "apiKey", "token"];
  const DIRECT_KEY_FIELDS = ["key", "api_key", "apiKey"];
  const ACCESS_FIELDS = ["access_token", "accessToken", "auth_token", "authToken", "system_access_token", "systemAccessToken", "token"];
  const REFRESH_FIELDS = ["refresh_token", "refreshToken"];
  const USER_ID_FIELDS = ["user_id", "userId", "userid", "id"];

  const state = {
    platform: "",
    name: inferName(location.hostname),
    baseUrl: location.origin,
    note: "1:1",
    rechargePaidAmount: 1,
    rechargeReceivedAmount: 1,
    threshold: 5,
    isVisible: true,
    isEnabled: true,
    accessTokenMode: "auto",
    accessToken: "",
    manualAccessToken: "",
    refreshToken: "",
    userId: "",
    apiKey: "",
    email: "",
    password: "",
  };

  let host = null;
  let root = null;
  let visible = false;
  let siteProbeStarted = false;

  function inferName(hostname) {
    if (CORE.inferName) return CORE.inferName(hostname);
    const parts = String(hostname || "")
      .toLowerCase()
      .split(".")
      .filter(Boolean)
      .filter((part) => !PREFIXES.has(part));
    while (parts.length > 1 && SUFFIXES.has(parts[parts.length - 1])) parts.pop();
    return parts[parts.length - 1] || String(hostname || "").split(".")[0] || "account";
  }

  function mask(value) {
    const text = String(value || "");
    if (!text) return "未获取";
    if (text.length <= 10) return `${text.slice(0, 3)}***`;
    return `${text.slice(0, 5)}...${text.slice(-5)}`;
  }

  function isMasked(value) {
    if (CORE.isMasked) return CORE.isMasked(value);
    const text = String(value || "");
    return !text || (text.includes("*") && text.replace(/\*/g, "").length < text.length / 2);
  }

  function safeJson(text) {
    if (CORE.safeJson) return CORE.safeJson(text);
    if (typeof text !== "string") return text;
    const trimmed = text.trim();
    if (!trimmed) return "";
    if (!/^[\[{"]/.test(trimmed) && !/^(true|false|null|-?\d)/.test(trimmed)) return text;
    try {
      return JSON.parse(trimmed);
    } catch {
      return text;
    }
  }

  function unwrap(payload) {
    if (CORE.unwrap) return CORE.unwrap(payload);
    let value = payload;
    for (let i = 0; i < 4; i += 1) {
      if (!value || typeof value !== "object" || Array.isArray(value)) return value;
      if ("data" in value) value = value.data;
      else if ("result" in value) value = value.result;
      else if ("payload" in value) value = value.payload;
      else return value;
    }
    return value;
  }

  function walk(value, visitor, depth = 0) {
    if (depth > 8 || value == null) return;
    if (Array.isArray(value)) {
      for (const item of value) walk(item, visitor, depth + 1);
      return;
    }
    if (typeof value === "object") {
      visitor(value);
      for (const item of Object.values(value)) walk(item, visitor, depth + 1);
    }
  }

  function pickField(obj, fields) {
    if (!obj || typeof obj !== "object") return "";
    for (const field of fields) {
      const value = obj[field];
      if (typeof value === "string" || typeof value === "number") {
        const text = String(value).trim();
        if (text && !isMasked(text)) return text;
      }
    }
    return "";
  }

  function extractAccessToken(payload) {
    if (CORE.extractAccessToken) return CORE.extractAccessToken(payload);
    const direct = pickField(unwrap(payload), ACCESS_FIELDS) || pickField(payload, ACCESS_FIELDS);
    if (direct) return direct;
    let found = "";
    walk(payload, (obj) => {
      if (!found) found = pickField(obj, ACCESS_FIELDS);
    });
    return found;
  }

  function extractGeneratedAccessToken(payload) {
    if (CORE.extractGeneratedAccessToken) return CORE.extractGeneratedAccessToken(payload);
    const access = extractAccessToken(payload);
    if (access) return access;
    const data = unwrap(payload);
    if (typeof data === "string" && looksLikeToken(data)) return data.trim();
    if (typeof payload === "string" && looksLikeToken(payload)) return payload.trim();
    return "";
  }

  function looksLikeToken(value) {
    const text = String(value || "").trim();
    return text.length >= 16 && !/\s/.test(text) && !isMasked(text);
  }

  function extractRefreshToken(payload) {
    if (CORE.extractRefreshToken) return CORE.extractRefreshToken(payload);
    const direct = pickField(unwrap(payload), REFRESH_FIELDS) || pickField(payload, REFRESH_FIELDS);
    if (direct) return direct;
    let found = "";
    walk(payload, (obj) => {
      if (!found) found = pickField(obj, REFRESH_FIELDS);
    });
    return found;
  }

  function extractUserId(payload) {
    if (CORE.extractUserId) return CORE.extractUserId(payload);
    const data = unwrap(payload);
    const direct = pickField(data, USER_ID_FIELDS);
    if (direct) return direct;
    let found = "";
    walk(payload, (obj) => {
      if (found) return;
      if (obj.user && typeof obj.user === "object") found = pickField(obj.user, USER_ID_FIELDS);
      if (!found && obj.self && typeof obj.self === "object") found = pickField(obj.self, USER_ID_FIELDS);
    });
    return found;
  }

  function extractApiKeys(payload) {
    if (CORE.extractApiKey) return CORE.extractApiKey(payload);
    const data = unwrap(payload);
    const arrays = [];
    if (Array.isArray(data)) arrays.push(data);
    if (data && typeof data === "object") {
      for (const field of ["items", "keys", "list", "records", "data"]) {
        if (Array.isArray(data[field])) arrays.push(data[field]);
      }
    }
    for (const list of arrays) {
      for (const item of list) {
        const key = pickField(item, KEY_FIELDS);
        if (key) return key;
      }
    }
    let found = "";
    walk(payload, (obj) => {
      if (!found) found = pickField(obj, DIRECT_KEY_FIELDS);
    });
    return found;
  }

  function bearerFromHeaders(headers) {
    if (CORE.bearerFromHeaders) return CORE.bearerFromHeaders(headers);
    const input = headers || {};
    for (const [key, value] of Object.entries(input)) {
      if (String(key).toLowerCase() !== "authorization") continue;
      const match = String(value || "").match(/^Bearer\s+(.+)$/i);
      if (match && !isMasked(match[1])) return match[1].trim();
    }
    return "";
  }

  function newApiUserFromHeaders(headers) {
    if (CORE.newApiUserFromHeaders) return CORE.newApiUserFromHeaders(headers);
    const input = headers || {};
    for (const [key, value] of Object.entries(input)) {
      if (String(key).toLowerCase() === "new-api-user") return String(value || "").trim();
    }
    return "";
  }

  function hasExplicitPlatformHint(value) {
    if (CORE.hasExplicitPlatformHint) return CORE.hasExplicitPlatformHint(value);
    return /(^|[._/-])(newapi|new-api|sub2api|sub-2-api)([._/-]|$)/i.test(String(value || ""));
  }

  function hasSub2ApiPath(value) {
    if (CORE.hasSub2ApiPath) return CORE.hasSub2ApiPath(value);
    const text = String(value || "");
    return (
      text.includes("/api/v1/auth/login") ||
      text.includes("/api/v1/auth/refresh") ||
      text.includes("/api/v1/keys") ||
      text.includes("/v1/usage")
    );
  }

  function hasNewApiPath(value) {
    if (CORE.hasNewApiPath) return CORE.hasNewApiPath(value);
    const text = String(value || "");
    return text.includes("/api/user/self") || text.includes("/api/user/security");
  }

  function sameOriginUrl(value) {
    if (CORE.sameOrigin) return CORE.sameOrigin(value, location.origin);
    try {
      return new URL(String(value || ""), location.href).origin === location.origin;
    } catch {
      return false;
    }
  }

  function detectProbeResponse(path, status, contentType, body) {
    if (CORE.detectProbeResponse) return CORE.detectProbeResponse(path, status, contentType, body);
    const code = Number(status || 0);
    const type = String(contentType || "").toLowerCase();
    const text = String(body || "").trim();
    const apiLike = code && code !== 404 && code < 500 && (type.includes("application/json") || (text && !/^<!doctype\s+html|<html[\s>]/i.test(text) && typeof safeJson(text) === "object"));
    if (!apiLike) return "";
    if (String(path || "").includes("/api/user/self")) return "newApi";
    if (String(path || "").includes("/api/v1/keys")) return "sub2Api";
    return "";
  }

  function keysPageUrl(baseUrl) {
    if (CORE.keysPageUrl) return CORE.keysPageUrl(baseUrl);
    try {
      return new URL("/keys", String(baseUrl || "").trim()).href;
    } catch {
      return "";
    }
  }

  function newApiSecurityPageUrl(baseUrl) {
    if (CORE.newApiSecurityPageUrl) return CORE.newApiSecurityPageUrl(baseUrl);
    try {
      return new URL("/user/security", String(baseUrl || "").trim()).href;
    } catch {
      return "";
    }
  }

  function effectiveAccessToken() {
    return state.platform === "newApi" && state.accessTokenMode === "input"
      ? state.manualAccessToken
      : state.accessToken;
  }

  async function probeSite() {
    if (siteProbeStarted || state.platform) return;
    siteProbeStarted = true;
    for (const item of [
      { platform: "sub2Api", path: "/api/v1/keys" },
      { platform: "newApi", path: "/api/user/self" },
    ]) {
      if (state.platform) return;
      try {
        const ctrl = new AbortController();
        const timer = setTimeout(() => ctrl.abort(), 3500);
        const response = await fetch(location.origin + item.path, {
          method: "GET",
          credentials: "include",
          cache: "no-store",
          headers: { Accept: "application/json" },
          signal: ctrl.signal,
        });
        clearTimeout(timer);
        const contentType = response.headers.get("content-type") || "";
        const text = await response.clone().text().catch(() => "");
        const platform = detectProbeResponse(item.path, response.status, contentType, text);
        if (platform === item.platform) {
          state.platform = platform;
          const data = safeJson(text);
          if (platform === "sub2Api") {
            state.apiKey ||= extractApiKeys(data);
          } else {
            state.userId ||= extractUserId(data);
            state.accessToken ||= extractAccessToken(data);
          }
          maybeShow();
          render();
          return;
        }
      } catch {}
    }
  }

  function scanStorage() {
    const explicitSiteHint = hasExplicitPlatformHint(location.hostname);
    for (const storage of [safeStorage("localStorage"), safeStorage("sessionStorage")]) {
      if (!storage) continue;
      try {
        for (let i = 0; i < storage.length; i += 1) {
          const key = storage.key(i) || "";
          const value = storage.getItem(key) || "";
          const lower = key.toLowerCase();
          const parsed = safeJson(value);
          if (!state.platform && explicitSiteHint && /sub2[_-]?api/.test(lower)) state.platform = "sub2Api";
          if (!state.platform && explicitSiteHint && /new[_-]?api|newapi/.test(lower)) state.platform = "newApi";
          if (!state.platform && explicitSiteHint && /refresh[_-]?token|auth[_-]?token/.test(lower)) state.platform = "sub2Api";
          if (!state.platform && explicitSiteHint && /user[_-]?id|userid/.test(lower)) state.platform = "newApi";
          if (!state.refreshToken && /refresh[_-]?token/.test(lower) && !isMasked(value)) state.refreshToken = value.trim();
          if (!state.accessToken && /(access[_-]?token|auth[_-]?token|^token$)/.test(lower) && !isMasked(value)) state.accessToken = value.trim();
          if (!state.userId && /^(user[_-]?id|userid)$/.test(lower)) state.userId = value.trim();
          if (!state.apiKey && /(api[_-]?key|apikey|^keys?$)/.test(lower) && !isMasked(value)) {
            state.apiKey = typeof parsed === "object" ? extractApiKeys(parsed) : value.trim();
          }
          if (parsed && typeof parsed === "object") {
            state.accessToken ||= extractAccessToken(parsed);
            state.refreshToken ||= extractRefreshToken(parsed);
            state.userId ||= extractUserId(parsed);
          }
        }
      } catch {}
    }
    maybeShow();
  }

  function safeStorage(name) {
    try {
      return window[name];
    } catch {
      return null;
    }
  }

  function handleHttp(payload) {
    const url = payload.url || "";
    const headers = payload.requestHeaders || {};
    const data = safeJson(payload.responseText || "");
    const bearer = bearerFromHeaders(headers);
    const headerUser = newApiUserFromHeaders(headers);
    const sameOrigin = sameOriginUrl(url);
    const explicitSiteHint = hasExplicitPlatformHint(location.hostname);

    if (sameOrigin && hasNewApiPath(url)) {
      if (url.includes("/api/user/security") || bearer || headerUser || extractUserId(data) || extractAccessToken(data)) {
        state.platform = "newApi";
      }
      if (bearer) state.accessToken = bearer;
      if (headerUser) state.userId = headerUser;
      state.userId ||= extractUserId(data);
      state.accessToken ||= extractAccessToken(data);
    } else if (sameOrigin && (url.includes("/api/v1/auth/login") || url.includes("/api/v1/auth/refresh"))) {
      const access = extractAccessToken(data);
      const refresh = extractRefreshToken(data);
      if (access || refresh) state.platform = "sub2Api";
      state.accessToken = access || state.accessToken;
      state.refreshToken = refresh || state.refreshToken;
    } else if (sameOrigin && url.includes("/api/v1/keys")) {
      const key = extractApiKeys(data);
      if (key) state.platform = "sub2Api";
      state.apiKey = key || state.apiKey;
      state.accessToken ||= bearer;
    } else if (sameOrigin && url.includes("/v1/usage") && state.platform === "sub2Api") {
      state.platform = "sub2Api";
    } else if (sameOrigin && /token|access|security/i.test(url) && (state.platform === "newApi" || explicitSiteHint)) {
      const access = extractGeneratedAccessToken(data);
      if (access) {
        state.platform ||= "newApi";
        state.accessToken = access;
      }
      state.refreshToken = extractRefreshToken(data) || state.refreshToken;
    } else if (sameOrigin && /key/i.test(url) && (state.platform === "sub2Api" || (explicitSiteHint && hasSub2ApiPath(url)))) {
      const key = extractApiKeys(data);
      if (key) {
        state.platform ||= "sub2Api";
        state.apiKey = key;
      }
    }
    maybeShow();
    render();
  }

  function maybeShow() {
    if (state.platform && !host) mount();
  }

  function bg(message) {
    return new Promise((resolve) => chrome.runtime.sendMessage(message, (resp) => resolve(resp || {})));
  }

  function mount() {
    host = document.createElement("div");
    host.id = "account-grabber-floating";
    host.style.cssText = "all:initial;position:fixed;top:112px;right:0;z-index:2147483647;";
    root = host.attachShadow({ mode: "open" });
    root.innerHTML = `
      <style>
        :host, * { box-sizing: border-box; font-family: system-ui, "Segoe UI", "Microsoft YaHei", sans-serif; }
        .fab { position:absolute; right:0; top:0; width:44px; height:44px; border:0; border-radius:8px 0 0 8px; background:#0f766e; color:#fff; cursor:pointer; font-size:18px; box-shadow:0 2px 8px rgba(0,0,0,.24); }
        .panel { position:absolute; right:52px; top:0; width:360px; max-height:calc(100vh - 128px); overflow:auto; display:none; padding:12px; background:#fff; color:#1f2937; border:1px solid #d1d5db; border-radius:8px; box-shadow:0 4px 18px rgba(0,0,0,.22); font-size:13px; }
        .panel.open { display:block; }
        h1 { margin:0 0 8px; font-size:15px; }
        label { display:block; margin:7px 0 3px; color:#4b5563; font-weight:600; }
        input, select { width:100%; padding:6px 8px; border:1px solid #cbd5e1; border-radius:4px; font-size:13px; }
        .grid { display:grid; grid-template-columns:1fr 1fr; gap:8px; }
        .row { display:flex; gap:8px; align-items:center; }
        .row input { width:auto; }
        .secret { display:flex; flex-wrap:wrap; gap:6px; align-items:center; padding:5px 0; color:#475569; word-break:break-all; font-family:ui-monospace, Consolas, monospace; }
        .secret span { flex:1 1 120px; min-width:0; }
        .secret button { flex:0 0 auto; }
        button.small { padding:4px 8px; border:1px solid #cbd5e1; background:#fff; border-radius:4px; cursor:pointer; }
        button.push { width:100%; margin-top:10px; padding:8px; border:0; border-radius:4px; background:#0f766e; color:#fff; cursor:pointer; font-size:13px; }
        button.secondary { background:#64748b; }
        button.danger { background:#dc2626; }
        button.inline-link { width:auto; margin:0; padding:0; border:0; background:transparent; color:#0f766e; cursor:pointer; font:inherit; text-decoration:underline; }
        button:disabled { opacity:.62; cursor:not-allowed; }
        #status { min-height:18px; margin-top:7px; font-size:12px; }
        .ok { color:#15803d; }
        .bad { color:#dc2626; }
        .muted { color:#64748b; font-size:11px; line-height:1.5; margin-top:6px; }
      </style>
      <button class="fab" id="fab" title="账号导入">导</button>
      <div class="panel" id="panel">
        <h1>NewAPI/Sub2API 账号导入</h1>
        <label>平台类型</label>
        <select id="platform"><option value="newApi">newApi</option><option value="sub2Api">sub2Api</option></select>
        <div class="grid">
          <div><label>名称</label><input id="name" /></div>
          <div><label>预警阈值</label><input id="threshold" type="number" min="0" step="0.01" /></div>
        </div>
        <label>Base URL</label><input id="baseUrl" />
        <label>备注</label><input id="note" />
        <div class="grid">
          <div><label>充值金额</label><input id="paid" type="number" min="0.000001" step="0.000001" /></div>
          <div><label>实际得到</label><input id="received" type="number" min="0.000001" step="0.000001" /></div>
        </div>
        <div id="accessModeWrap">
          <label>accessToken 类型</label>
          <select id="accessTokenMode"><option value="auto">自动获取</option><option value="input">手动输入</option></select>
        </div>
        <label>accessToken</label>
        <div class="secret" id="accessAutoRow"><span id="accessPreview">未获取</span><button class="small" id="copyAccess">复制</button><button class="small" id="openNewApiSecurity">生成系统访问令牌</button></div>
        <div id="accessManualRow"><input id="manualAccessToken" type="password" autocomplete="off" /></div>
        <label id="newUserLabel">newApi userId</label><input id="userId" />
        <div id="subFields">
          <label>sub2Api apiKey</label>
          <div class="secret"><span id="apiPreview">未获取</span><button class="small" id="copyApi">复制</button><button class="small" id="openKeys">跳转页面</button></div>
          <label>refreshToken</label>
          <div class="secret"><span id="refreshPreview">未获取</span><button class="small" id="copyRefresh">复制</button></div>
          <div class="grid">
            <div><label>账号</label><input id="email" autocomplete="username" /></div>
            <div><label>密码</label><input id="password" type="password" autocomplete="current-password" /></div>
          </div>
        </div>
        <div class="grid">
          <label class="row"><input id="visible" type="checkbox" />显示仪表盘</label>
          <label class="row"><input id="enabled" type="checkbox" />启用自动查询</label>
        </div>
        <button class="push" id="push">推送到 App</button>
        <button class="push danger" id="clear">清空抓取值</button>
        <div id="status"></div>
        <div class="muted">推送只保存账号，不会立即查询余额或分组。需在扩展<button class="inline-link" id="optionsLink" type="button">选项页</button>配置 App 地址。</div>
      </div>
    `;
    document.documentElement.appendChild(host);
    bindUi();
    render();
  }

  function $(id) {
    return root && root.getElementById(id);
  }

  function bindUi() {
    $("fab").addEventListener("click", (event) => {
      event.stopPropagation();
      visible = !visible;
      $("panel").classList.toggle("open", visible);
    });
    for (const id of ["platform", "accessTokenMode", "name", "threshold", "baseUrl", "note", "paid", "received", "manualAccessToken", "userId", "email", "password"]) {
      $(id).addEventListener("input", syncFromUi);
      $(id).addEventListener("change", syncFromUi);
    }
    $("visible").addEventListener("change", syncFromUi);
    $("enabled").addEventListener("change", syncFromUi);
    $("copyAccess").addEventListener("click", () => copySecret(effectiveAccessToken()));
    $("openNewApiSecurity").addEventListener("click", openNewApiSecurityPage);
    $("copyApi").addEventListener("click", () => copySecret(state.apiKey));
    $("openKeys").addEventListener("click", openKeysPage);
    $("copyRefresh").addEventListener("click", () => copySecret(state.refreshToken));
    $("optionsLink").addEventListener("click", async () => {
      const result = await bg({ type: "open-options" });
      if (!result.ok) setStatus(result.message || "无法打开选项页", "bad");
    });
    $("clear").addEventListener("click", () => {
      state.accessToken = "";
      state.manualAccessToken = "";
      state.refreshToken = "";
      state.apiKey = "";
      state.userId = "";
      render();
      setStatus("已清空抓取值", "ok");
    });
    $("push").addEventListener("click", push);
  }

  function syncFromUi() {
    state.platform = $("platform").value;
    state.accessTokenMode = $("accessTokenMode").value === "input" ? "input" : "auto";
    state.name = $("name").value.trim();
    state.baseUrl = $("baseUrl").value.trim();
    state.note = $("note").value.trim();
    state.rechargePaidAmount = $("paid").value || 1;
    state.rechargeReceivedAmount = $("received").value || 1;
    state.threshold = $("threshold").value === "" ? 5 : $("threshold").value;
    state.manualAccessToken = $("manualAccessToken").value.trim();
    state.userId = $("userId").value.trim();
    state.email = $("email").value.trim();
    state.password = $("password").value;
    state.isVisible = $("visible").checked;
    state.isEnabled = $("enabled").checked;
    renderPlatformFields();
  }

  function render() {
    if (!root) return;
    $("platform").value = state.platform || "newApi";
    $("accessTokenMode").value = state.accessTokenMode;
    $("name").value = state.name;
    $("baseUrl").value = state.baseUrl;
    $("note").value = state.note;
    $("paid").value = state.rechargePaidAmount;
    $("received").value = state.rechargeReceivedAmount;
    $("threshold").value = state.threshold;
    $("manualAccessToken").value = state.manualAccessToken;
    $("userId").value = state.userId;
    $("email").value = state.email;
    $("password").value = state.password;
    $("visible").checked = !!state.isVisible;
    $("enabled").checked = !!state.isEnabled;
    $("accessPreview").textContent = mask(effectiveAccessToken());
    $("apiPreview").textContent = mask(state.apiKey);
    $("refreshPreview").textContent = mask(state.refreshToken);
    renderPlatformFields();
  }

  function renderPlatformFields() {
    const platform = $("platform").value;
    const accessMode = $("accessTokenMode").value;
    $("subFields").style.display = platform === "sub2Api" ? "block" : "none";
    $("accessModeWrap").style.display = platform === "newApi" ? "block" : "none";
    $("accessAutoRow").style.display = platform === "newApi" && accessMode === "input" ? "none" : "flex";
    $("accessManualRow").style.display = platform === "newApi" && accessMode === "input" ? "block" : "none";
    $("openNewApiSecurity").style.display = platform === "newApi" && accessMode === "auto" ? "inline-block" : "none";
    $("newUserLabel").style.display = platform === "newApi" ? "block" : "none";
    $("userId").style.display = platform === "newApi" ? "block" : "none";
  }

  function setStatus(text, kind) {
    const el = $("status");
    el.textContent = text || "";
    el.className = kind || "";
  }

  async function copySecret(value) {
    if (!value) return;
    await navigator.clipboard.writeText(value);
    setStatus("已复制", "ok");
  }

  function openKeysPage() {
    syncFromUi();
    const url = keysPageUrl(state.baseUrl || location.origin);
    if (!url) {
      setStatus("Base URL 无效，无法跳转", "bad");
      return;
    }
    window.location.assign(url);
  }

  function openNewApiSecurityPage() {
    syncFromUi();
    const url = newApiSecurityPageUrl(state.baseUrl || location.origin);
    if (!url) {
      setStatus("Base URL 无效，无法跳转", "bad");
      return;
    }
    window.location.assign(url);
  }

  function payload() {
    return {
      platform: state.platform || $("platform").value,
      name: state.name,
      base_url: state.baseUrl,
      note: state.note || "1:1",
      recharge_paid_amount: Number(state.rechargePaidAmount) || 1,
      recharge_received_amount: Number(state.rechargeReceivedAmount) || 1,
      threshold: state.threshold === "" ? 5 : Number(state.threshold),
      is_visible: !!state.isVisible,
      is_enabled: !!state.isEnabled,
      access_token: effectiveAccessToken(),
      refresh_token: state.refreshToken,
      user_id: state.userId,
      api_key: state.apiKey,
      email: state.email,
      password: state.password,
    };
  }

  async function push() {
    syncFromUi();
    setStatus("推送中...");
    $("push").disabled = true;
    const result = await bg({ type: "push-account", payload: payload() });
    $("push").disabled = false;
    if (result.ok) {
      setStatus(`保存成功，账号 ID=${result.id}`, "ok");
    } else {
      setStatus(result.message || "保存失败", "bad");
    }
  }

  window.addEventListener("message", (event) => {
    if (event.source !== window) return;
    const data = event.data;
    if (!data || data.source !== SOURCE) return;
    handleHttp(data.payload || {});
  });

  setTimeout(scanStorage, 500);
  setTimeout(probeSite, 700);
  setInterval(scanStorage, 3000);
})();

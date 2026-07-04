const DEFAULT_SESSION_COOKIE = "balance_monitor_session";
const DEFAULT_APP_BASE = "__APP_BASE_URL__";
const DEFAULT_PUSH_TIMEOUT_MS = 20000;

function normalizeAppBase(value) {
  const text = String(value || "").trim().replace(/\/$/, "");
  if (!text || text === "__APP_BASE_URL__") return "";
  return text;
}

async function getStored() {
  const keys = ["appBase", "sessionCookieName", "adminUser", "adminPassword", "pushTimeoutMs"];
  const local = await chrome.storage.local.get(keys);
  return {
    appBase: normalizeAppBase(local.appBase) || normalizeAppBase(DEFAULT_APP_BASE),
    sessionCookie: local.sessionCookieName || DEFAULT_SESSION_COOKIE,
    adminUser: local.adminUser || "",
    adminPassword: local.adminPassword || "",
    pushTimeoutMs: local.pushTimeoutMs || DEFAULT_PUSH_TIMEOUT_MS,
  };
}

async function appFetch(cfg, path, init = {}) {
  const url = cfg.appBase + path;
  const timeoutMs = cfg.pushTimeoutMs || DEFAULT_PUSH_TIMEOUT_MS;
  const doFetch = async () => {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    try {
      const headers = Object.assign({}, init.headers || {});
      const ck = await chrome.cookies.get({ url: cfg.appBase, name: cfg.sessionCookie });
      if (ck && ck.value) headers.Cookie = `${cfg.sessionCookie}=${ck.value}`;
      if (init.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
      return await fetch(url, Object.assign({}, init, { headers, signal: ctrl.signal }));
    } finally {
      clearTimeout(timer);
    }
  };

  let resp;
  try {
    resp = await doFetch();
  } catch (e) {
    if (e && e.name === "AbortError") {
      return { ok: false, status: 0, _timeout: true, json: async () => ({ message: `请求超时（${timeoutMs / 1000}s）` }) };
    }
    throw e;
  }
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

function cleanPayload(payload) {
  const data = Object.assign({}, payload || {});
  data.platform = data.platform === "sub2Api" ? "sub2Api" : "newApi";
  data.name = String(data.name || "").trim();
  data.base_url = String(data.base_url || data.baseUrl || "").replace(/\/$/, "");
  data.note = String(data.note || "1:1").trim() || "1:1";
  data.recharge_paid_amount = data.recharge_paid_amount || data.rechargePaidAmount || 1;
  data.recharge_received_amount = data.recharge_received_amount || data.rechargeReceivedAmount || 1;
  data.threshold = data.threshold === "" || data.threshold == null ? 5 : data.threshold;
  data.is_visible = data.is_visible == null ? true : !!data.is_visible;
  data.is_enabled = data.is_enabled == null ? true : !!data.is_enabled;
  if (data.platform === "newApi") {
    delete data.api_key;
    delete data.refresh_token;
    delete data.email;
    delete data.password;
  }
  return data;
}

async function pushAccount(payload) {
  const cfg = await getStored();
  const data = cleanPayload(payload);
  const errors = [];
  if (!cfg.appBase) errors.push("App Base URL 未配置或仍是占位符，请重新下载插件，或在扩展选项页保存余额监控 App 地址");
  if (!data.name) errors.push("名称必填");
  if (!data.base_url) errors.push("Base URL 必填");
  if (data.platform === "newApi") {
    if (!data.access_token) errors.push("newApi 缺少 accessToken");
    if (!data.user_id) errors.push("newApi 缺少 userId");
  }
  if (data.platform === "sub2Api" && !data.api_key) {
    errors.push("sub2Api 缺少 apiKey");
  }
  if (errors.length) return { ok: false, message: errors.join("；") };

  const resp = await appFetch(cfg, "/api/accounts/import-by-base-url", {
    method: "POST",
    body: JSON.stringify(data),
  });
  const result = await resp.json().catch(() => ({}));
  if (!resp.ok || !result.ok) {
    if (resp.status === 405) {
      return { ok: false, message: `App Base URL 可能配置错误：${cfg.appBase} 不接受账号导入接口 POST，请在扩展选项页改为余额监控 App 地址` };
    }
    return { ok: false, message: result.detail || result.message || `保存失败 (${resp.status})` };
  }
  return { ok: true, id: result.id, account: result.account };
}

function openOptionsPage() {
  return new Promise((resolve) => {
    chrome.runtime.openOptionsPage(() => {
      const err = chrome.runtime.lastError;
      resolve(err ? { ok: false, message: err.message } : { ok: true });
    });
  });
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg && msg.type === "push-account") {
    pushAccount(msg.payload).then(
      (r) => sendResponse(r),
      (e) => sendResponse({ ok: false, message: (e && e.message) || "推送异常" })
    );
    return true;
  }
  if (msg && msg.type === "open-options") {
    openOptionsPage().then(sendResponse);
    return true;
  }
  return false;
});

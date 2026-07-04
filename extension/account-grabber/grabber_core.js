(() => {
  "use strict";

  const PREFIXES = new Set(["www", "api", "app", "admin", "panel", "console", "newapi", "sub2api"]);
  const SUFFIXES = new Set(["com", "net", "org", "io", "ai", "cc", "cn", "top", "xyz", "site", "app", "dev", "cloud", "co", "uk"]);
  const KEY_FIELDS = ["key", "api_key", "apiKey", "token"];
  const DIRECT_KEY_FIELDS = ["key", "api_key", "apiKey"];
  const ACCESS_FIELDS = ["access_token", "accessToken", "auth_token", "authToken", "system_access_token", "systemAccessToken", "token"];
  const REFRESH_FIELDS = ["refresh_token", "refreshToken"];
  const USER_ID_FIELDS = ["user_id", "userId", "userid", "id"];

  function inferName(hostname) {
    const parts = String(hostname || "")
      .toLowerCase()
      .split(".")
      .filter(Boolean)
      .filter((part) => !PREFIXES.has(part));
    while (parts.length > 1 && SUFFIXES.has(parts[parts.length - 1])) parts.pop();
    return parts[parts.length - 1] || String(hostname || "").split(".")[0] || "account";
  }

  function isMasked(value) {
    const text = String(value || "");
    return !text || (text.includes("*") && text.replace(/\*/g, "").length < text.length / 2);
  }

  function looksLikeToken(value) {
    const text = String(value || "").trim();
    return text.length >= 16 && !/\s/.test(text) && !isMasked(text);
  }

  function safeJson(text) {
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
    const direct = pickField(unwrap(payload), ACCESS_FIELDS) || pickField(payload, ACCESS_FIELDS);
    if (direct) return direct;
    let found = "";
    walk(payload, (obj) => {
      if (!found) found = pickField(obj, ACCESS_FIELDS);
    });
    return found;
  }

  function extractGeneratedAccessToken(payload) {
    const access = extractAccessToken(payload);
    if (access) return access;
    const data = unwrap(payload);
    if (typeof data === "string" && looksLikeToken(data)) return data.trim();
    if (typeof payload === "string" && looksLikeToken(payload)) return payload.trim();
    return "";
  }

  function extractRefreshToken(payload) {
    const direct = pickField(unwrap(payload), REFRESH_FIELDS) || pickField(payload, REFRESH_FIELDS);
    if (direct) return direct;
    let found = "";
    walk(payload, (obj) => {
      if (!found) found = pickField(obj, REFRESH_FIELDS);
    });
    return found;
  }

  function extractUserId(payload) {
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

  function extractApiKey(payload) {
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
    const input = headers || {};
    for (const [key, value] of Object.entries(input)) {
      if (String(key).toLowerCase() !== "authorization") continue;
      const match = String(value || "").match(/^Bearer\s+(.+)$/i);
      if (match && !isMasked(match[1])) return match[1].trim();
    }
    return "";
  }

  function newApiUserFromHeaders(headers) {
    const input = headers || {};
    for (const [key, value] of Object.entries(input)) {
      if (String(key).toLowerCase() === "new-api-user") return String(value || "").trim();
    }
    return "";
  }

  function hasExplicitPlatformHint(value) {
    return /(^|[._/-])(newapi|new-api|sub2api|sub-2-api)([._/-]|$)/i.test(String(value || ""));
  }

  function hasSub2ApiPath(value) {
    const text = String(value || "");
    return (
      text.includes("/api/v1/auth/login") ||
      text.includes("/api/v1/auth/refresh") ||
      text.includes("/api/v1/keys") ||
      text.includes("/v1/usage")
    );
  }

  function hasNewApiPath(value) {
    const text = String(value || "");
    return text.includes("/api/user/self") || text.includes("/api/user/security");
  }

  function sameOrigin(url, pageOrigin) {
    if (!pageOrigin) return true;
    try {
      return new URL(String(url || ""), pageOrigin).origin === pageOrigin;
    } catch {
      return false;
    }
  }

  function keysPageUrl(baseUrl) {
    try {
      return new URL("/keys", String(baseUrl || "").trim()).href;
    } catch {
      return "";
    }
  }

  function newApiSecurityPageUrl(baseUrl) {
    try {
      return new URL("/user/security", String(baseUrl || "").trim()).href;
    } catch {
      return "";
    }
  }

  function looksLikeApiResponse(status, contentType, body) {
    const code = Number(status || 0);
    if (!code || code === 404 || code >= 500) return false;
    const type = String(contentType || "").toLowerCase();
    if (type.includes("application/json")) return true;
    const text = String(body || "").trim();
    if (!text || /^<!doctype\s+html|<html[\s>]/i.test(text)) return false;
    const parsed = safeJson(text);
    return parsed && typeof parsed === "object";
  }

  function detectProbeResponse(path, status, contentType, body) {
    const text = String(path || "");
    if (!looksLikeApiResponse(status, contentType, body)) return "";
    if (text.includes("/api/user/self") || text.includes("/api/user/security")) return "newApi";
    if (
      text.includes("/api/v1/keys") ||
      text.includes("/api/v1/auth/login") ||
      text.includes("/api/v1/auth/refresh") ||
      text.includes("/v1/usage")
    ) {
      return "sub2Api";
    }
    return "";
  }

  function detectHttp(url, headers, payload, pageOrigin, pageHostname) {
    const text = String(url || "");
    const data = safeJson(payload);
    const siteHint = hasExplicitPlatformHint(pageHostname || "");
    if (!sameOrigin(text, pageOrigin)) return "";
    if (hasNewApiPath(text) || newApiUserFromHeaders(headers)) {
      if (text.includes("/api/user/security")) return "newApi";
      if (newApiUserFromHeaders(headers) || bearerFromHeaders(headers) || extractUserId(data) || extractAccessToken(data)) return "newApi";
      return "";
    }
    if (text.includes("/api/v1/auth/login") || text.includes("/api/v1/auth/refresh")) {
      return extractAccessToken(data) || extractRefreshToken(data) ? "sub2Api" : "";
    }
    if (text.includes("/api/v1/keys")) return extractApiKey(data) ? "sub2Api" : "";
    if (siteHint && /token|access|security/i.test(text) && extractGeneratedAccessToken(data)) return "newApi";
    if (siteHint && extractRefreshToken(data)) return "sub2Api";
    if (siteHint && extractUserId(data) && extractAccessToken(data)) return "newApi";
    if (siteHint && extractApiKey(data)) return "sub2Api";
    return "";
  }

  const api = {
    inferName,
    isMasked,
    looksLikeToken,
    safeJson,
    unwrap,
    walk,
    pickField,
    extractAccessToken,
    extractGeneratedAccessToken,
    extractRefreshToken,
    extractUserId,
    extractApiKey,
    bearerFromHeaders,
    newApiUserFromHeaders,
    hasExplicitPlatformHint,
    hasSub2ApiPath,
    hasNewApiPath,
    sameOrigin,
    keysPageUrl,
    newApiSecurityPageUrl,
    looksLikeApiResponse,
    detectProbeResponse,
    detectHttp,
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  } else {
    window.AccountGrabberCore = api;
  }
})();

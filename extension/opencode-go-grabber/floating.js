// floating.js — 注入到 https://opencode.ai/workspace/*
// 在页面右侧显示一个悬浮按钮 + 面板：自动抓取 workspace/cookie/api_key，并可一键推送到 app。
// 用 Shadow DOM 隔离样式。推送复用 background 的 push 处理器（含 401 兜底登录）。

(() => {
  "use strict";

  // 避免重复注入
  if (window.__opencodeGoGrabberFloating) return;
  window.__opencodeGoGrabberFloating = true;

  const MASK = (v) => {
    if (!v) return "";
    if (v.length <= 8) return v.slice(0, 2) + "***";
    return v.slice(0, 4) + "…" + v.slice(-4);
  };

  // ---- UI 构建 ----
  const host = document.createElement("div");
  host.id = "opencode-go-grabber-floating";
  host.style.cssText = "all:initial;position:fixed;top:120px;right:0;z-index:2147483647;";
  const root = host.attachShadow({ mode: "open" });
  root.innerHTML = `
    <style>
      :host, * { box-sizing: border-box; font-family: system-ui, "Segoe UI", "Microsoft YaHei", sans-serif; }
      .fab {
        position: absolute; right: 0; top: 0;
        width: 44px; height: 44px; border-radius: 8px 0 0 8px;
        background: #2563eb; color: #fff; border: none; cursor: pointer;
        font-size: 20px; line-height: 1; box-shadow: 0 2px 8px rgba(0,0,0,.25);
        display: flex; align-items: center; justify-content: center;
      }
      .fab:hover { background: #1d4ed8; }
      .panel {
        position: absolute; right: 52px; top: 0; width: 320px;
        background: #fff; border: 1px solid #ddd; border-radius: 8px;
        box-shadow: 0 4px 16px rgba(0,0,0,.2); padding: 12px;
        color: #222; font-size: 13px; display: none;
      }
      .panel.open { display: block; }
      h1 { font-size: 14px; margin: 0 0 8px; }
      .row { display: flex; align-items: center; gap: 8px; margin: 4px 0; }
      .row > label { width: 70px; color: #555; font-weight: 600; flex-shrink: 0; }
      .row > .val { flex: 1; font-family: ui-monospace, Consolas, monospace; color: #111; word-break: break-all; }
      .ok { color: #16a34a; }
      .bad { color: #dc2626; }
      textarea {
        width: 100%; padding: 6px 8px; border: 1px solid #ccc; border-radius: 4px;
        font-size: 12px; font-family: ui-monospace, Consolas, monospace; resize: vertical;
        margin-top: 4px;
      }
      button.push {
        width: 100%; padding: 8px; margin-top: 10px; font-size: 13px;
        border: none; background: #2563eb; color: #fff; border-radius: 4px; cursor: pointer;
      }
      button.push:hover { background: #1d4ed8; }
      button.push:disabled { background: #93a3ef; cursor: not-allowed; }
      button.small { padding: 3px 8px; font-size: 11px; border: 1px solid #ccc; background: #fff; border-radius: 3px; cursor: pointer; }
      #status { margin-top: 6px; min-height: 16px; font-size: 12px; }
      .muted { color: #888; font-size: 11px; margin-top: 4px; }
      .chk { display: flex; align-items: center; gap: 6px; margin-top: 8px; }
      a { color: #2563eb; cursor: pointer; text-decoration: underline; }
    </style>
    <button class="fab" id="fab" title="OpenCode Go Grabber">G</button>
    <div class="panel" id="panel">
      <h1>OpenCode Go Grabber</h1>
      <div class="row"><label>Workspace</label><span class="val" id="workspaceId">读取中…</span></div>
      <div class="row"><label>Cookie</label><span class="val" id="cookieState">读取中…</span></div>
      <div class="row"><label>API Key</label><span class="val" id="apiKey">—</span><button class="small" id="copyKey">复制</button></div>
      <div class="muted">账号信息（邮箱|密码|恢复邮箱，恢复邮箱可空）</div>
      <textarea id="accountLine" rows="2" placeholder="谷歌邮箱|谷歌密码|恢复邮箱"></textarea>
      <div class="chk"><input type="checkbox" id="enabled" /><label for="enabled">启用自动刷新</label></div>
      <button class="push" id="push">推送到 App</button>
      <button class="push" id="navClick" style="background:#64748b;margin-top:6px;">模拟点击 workspace nav</button>
      <button class="push" id="clearBtn" style="background:#dc2626;margin-top:6px;">清空已抓取值</button>
      <div id="status"></div>
      <div class="muted">需先在 <a id="optionsLink">选项页</a> 配置 app 地址与账号。</div>
    </div>
  `;
  document.documentElement.appendChild(host);

  const $ = (id) => root.getElementById(id);

  function setStatus(text, kind) {
    const s = $("status");
    s.textContent = text || "";
    s.className = kind || "";
  }

  // ---- toggle（只手动开关，不点外面自动关）----
  $("fab").addEventListener("click", (e) => {
    e.stopPropagation();
    $("panel").classList.toggle("open");
  });

  // 选项页跳转
  $("optionsLink").addEventListener("click", (e) => {
    e.preventDefault();
    e.stopPropagation();
    bg({ type: "open-options" }).then((r) => {
      if (!r.ok) setStatus(r.message || "无法打开选项页", "bad");
    });
  });

  // ---- 取值 ----
  function bg(msg) {
    return new Promise((resolve) => chrome.runtime.sendMessage(msg, (r) => resolve(r || {})));
  }

  async function refresh() {
    // workspace: 走 background 的 get-workspace（与 popup 同通道，已验证能取到）
    const ws = await bg({ type: "get-workspace" });
    const workspaceId = ws.workspaceId || "";
    $("workspaceId").textContent = workspaceId || "（未识别，稍候自动更新）";

    // cookie / api_key 从 background 快照
    const snap = await bg({ type: "get-snapshot" });
    const cs = $("cookieState");
    if (snap.hasAuth) {
      cs.textContent = "已捕获（含 auth）";
      cs.className = "val ok";
    } else {
      cs.textContent = "缺少 auth，请先登录";
      cs.className = "val bad";
    }
    const key = snap.capture && snap.capture.apiKey;
    $("apiKey").textContent = key ? MASK(key) : "尚未抓到";
    $("apiKey").dataset.raw = key || "";
    return { workspaceId, cookieHeader: snap.cookieHeader || "" };
  }

  // 定期兜底刷新（workspace/cookie 都可能由 background/content.js 更新，storage.onChanged
  // 在 content script 上下文监听不可靠，故用轮询）
  refresh();
  setInterval(refresh, 2000);

  // ---- 复制 api key ----
  $("copyKey").addEventListener("click", async () => {
    const raw = $("apiKey").dataset.raw;
    if (!raw) return;
    await navigator.clipboard.writeText(raw);
    setStatus("API key 已复制", "ok");
  });

  // 解析「邮箱|密码|恢复邮箱」（恢复邮箱可空）。分隔符支持 | 或制表符。
  function parseAccountLine(line) {
    const raw = String(line || "").trim();
    if (!raw) return { email: "", password: "", recoveryEmail: "" };
    const segs = raw.split(/[|\t]/).map((s) => s.trim());
    return { email: segs[0] || "", password: segs[1] || "", recoveryEmail: segs[2] || "" };
  }

// ---- 模拟点击 workspace nav 下指定 a 标签 ----
  // 只点中文名称为「go」或「API 密钥」的两个链接。
  // 不用真路由跳转（会重载页面、打断自身）。改派发完整真实鼠标事件序列
  // （pointerdown→mousedown→pointerup→mouseup→click），让 React 的 onClick 被触发，
  // 客户端路由自行导航、加载对应数据（发出 /_server 请求，便于抓取 api_key）。
  // 仍不生效时兜底用 history.pushState 通知路由。
  const NAV_TARGETS = ["go", "API 密钥", "API密钥", "API Keys", "API keys"];
  function navLinkText(a) {
    return (a.textContent || "").trim();
  }
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  function realClick(a) {
    const opts = { bubbles: true, cancelable: true, view: window, button: 0, isTrusted: false };
    try {
      a.dispatchEvent(new PointerEvent("pointerdown", opts));
      a.dispatchEvent(new MouseEvent("mousedown", opts));
      a.dispatchEvent(new PointerEvent("pointerup", opts));
      a.dispatchEvent(new MouseEvent("mouseup", opts));
      a.dispatchEvent(new MouseEvent("click", opts));
      return true;
    } catch {
      try { a.click(); return true; } catch { return false; }
    }
  }

  $("navClick").addEventListener("click", async () => {
    const nav = document.querySelector('div[data-component="workspace-nav-items"]');
    if (!nav) { setStatus("未找到 workspace-nav-items", "bad"); return; }
    const links = Array.from(nav.querySelectorAll("a")).filter((a) =>
      NAV_TARGETS.includes(navLinkText(a))
    );
    if (!links.length) { setStatus("未找到 go / API 密钥 链接", "bad"); return; }
    $("navClick").disabled = true;

    const done = [];
    for (const a of links) {
      const text = navLinkText(a);
      setStatus(`模拟点击：${text}…`);
      const ok = realClick(a);
      if (ok) done.push(text);
      // 给客户端路由 + _server 请求留出时间
      await sleep(1200);
    }

    $("navClick").disabled = false;
    setStatus(`完成，已点击 ${done.length} 个：${done.join("、") || "无"}`, "ok");
  });

  // ---- 推送 ----
  // 推送涉及 app 端建号+导入+刷新三步，可能较慢；这里加 60s 兜底超时，避免 SW 休眠
  // 导致 channel 关闭、按钮卡在「推送中…」拿不到回调。
  function bgWithTimeout(msg, timeoutMs = 60000) {
    return new Promise((resolve) => {
      let done = false;
      const timer = setTimeout(() => {
        if (done) return;
        done = true;
        resolve({ ok: false, message: "推送超时（60s），请稍后在 app 端确认结果" });
      }, timeoutMs);
      chrome.runtime.sendMessage(msg, (r) => {
        if (done) return;
        done = true;
        clearTimeout(timer);
        if (chrome.runtime.lastError) {
          resolve({ ok: false, message: (chrome.runtime.lastError.message || "扩展通信中断") });
          return;
        }
        resolve(r || {});
      });
    });
  }

  $("push").addEventListener("click", async () => {
    const acct = parseAccountLine($("accountLine").value);
    const { workspaceId, cookieHeader } = await refresh();
    const payload = {
      email: acct.email,
      password: acct.password,
      recoveryEmail: acct.recoveryEmail,
      workspaceId: workspaceId || "",
      cookieHeader,
      enabled: $("enabled").checked,
    };
    if (!payload.email || !payload.password) {
      setStatus("请按 邮箱|密码|恢复邮箱 粘贴", "bad");
      return;
    }
    setStatus("推送中…");
    $("push").disabled = true;
    const r = await bgWithTimeout({ type: "push", payload });
    $("push").disabled = false;
    if (r.ok) {
      const masked = r.maskedKey || (r.account && (r.account.api_key_masked || r.account.apiKeyMasked)) || "";
      setStatus(`成功，账号 ID=${r.accountId}${masked ? "，key：" + masked : ""}`, "ok");
    } else {
      setStatus(r.message || "推送失败", "bad");
    }
  });

  // ---- 清空已抓取的 workspace / cookie / api_key ----
  $("clearBtn").addEventListener("click", async () => {
    const r = await bg({ type: "clear-capture" });
    if (r.ok) {
      $("apiKey").textContent = "尚未抓到";
      $("apiKey").dataset.raw = "";
      $("workspaceId").textContent = "（已清空，稍候自动更新）";
      $("cookieState").textContent = "已清空";
      $("cookieState").className = "val";
      setStatus("已清空抓取值", "ok");
    } else {
      setStatus("清空失败", "bad");
    }
  });
})();

const $ = (id) => document.getElementById(id);
const MASK = (v) => {
  if (!v) return "";
  if (v.length <= 8) return v.slice(0, 2) + "***";
  return v.slice(0, 4) + "…" + v.slice(-4);
};

function setStatus(text, kind) {
  const s = $("status");
  s.textContent = text || "";
  s.className = kind || "";
}

// 解析「邮箱|密码|恢复邮箱」格式（恢复邮箱可空）。分隔符支持 | 或制表符。
// 若只粘贴了纯邮箱（无分隔符），则视为 email=该串，其余为空。
function parseAccountLine(line) {
  const raw = String(line || "").trim();
  if (!raw) return { email: "", password: "", recoveryEmail: "" };
  // 分隔符支持 | 或制表符；空段保留以判断密码显空的情况
  const segs = raw.split(/[|\t]/).map((s) => s.trim());
  return {
    email: segs[0] || "",
    password: segs[1] || "",
    recoveryEmail: segs[2] || "",
  };
}

async function refresh() {
  // workspace_id 从 content script
  const ws = await new Promise((resolve) =>
    chrome.runtime.sendMessage({ type: "get-workspace" }, (r) => resolve(r || {}))
  );
  $("workspaceId").textContent = ws.workspaceId || "（请在 opencode.ai/workspace 页打开）";

  // cookie / api_key 从 background 快照
  const snap = await new Promise((resolve) =>
    chrome.runtime.sendMessage({ type: "get-snapshot" }, (r) => resolve(r || {}))
  );
  const cs = $("cookieState");
  if (snap.hasAuth) {
    cs.textContent = `已捕获（含 auth）`;
    cs.className = "val ok";
  } else {
    cs.textContent = `缺少 auth —— 请先登录 opencode.ai 并触发一次请求`;
    cs.className = "val bad";
  }
  const key = snap.capture && snap.capture.apiKey;
  $("apiKey").textContent = key ? MASK(key) : "尚未抓到";
  $("apiKey").dataset.raw = key || "";
}

document.getElementById("copyKey").addEventListener("click", async () => {
  const raw = $("apiKey").dataset.raw;
  if (!raw) return;
  await navigator.clipboard.writeText(raw);
  setStatus("API key 已复制", "ok");
});

document.getElementById("optionsLink").addEventListener("click", (e) => {
  e.preventDefault();
  chrome.runtime.openOptionsPage();
});

document.getElementById("push").addEventListener("click", async () => {
  const acct = parseAccountLine($("accountLine").value);
  const payload = {
    email: acct.email,
    password: acct.password,
    recoveryEmail: acct.recoveryEmail,
    workspaceId: $("workspaceId").textContent.startsWith("（") ? "" : $("workspaceId").textContent,
    cookieHeader: (await new Promise((res) =>
      chrome.runtime.sendMessage({ type: "get-snapshot" }, (r) => res((r && r.cookieHeader) || ""))
    )),
    enabled: $("enabled").checked,
  };
  if (!payload.email || !payload.password) {
    setStatus("请按 邮箱|密码|恢复邮箱 粘贴（恢复邮箱可空）", "bad");
    return;
  }
  setStatus("推送中…");
  document.getElementById("push").disabled = true;
  const r = await new Promise((res) =>
    chrome.runtime.sendMessage({ type: "push", payload }, (resp) => res(resp || {}))
  );
  document.getElementById("push").disabled = false;
  if (r.ok) {
    const masked = r.maskedKey || (r.account && (r.account.api_key_masked || r.account.apiKeyMasked)) || "";
    setStatus(`成功，账号 ID=${r.accountId}${masked ? "，已回填 key：" + masked : ""}`, "ok");
  } else {
    setStatus(r.message || "推送失败", "bad");
  }
});

refresh();
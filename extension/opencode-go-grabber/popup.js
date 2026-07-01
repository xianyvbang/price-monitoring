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

async function loadDefaults() {
  const data = await chrome.storage.local.get(["defaultRecoveryEmail"]);
  if (data.defaultRecoveryEmail && !$("recoveryEmail").value) $("recoveryEmail").value = data.defaultRecoveryEmail;
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
  const payload = {
    email: $("email").value.trim(),
    password: $("password").value,
    recoveryEmail: $("recoveryEmail").value.trim(),
    workspaceId: $("workspaceId").textContent.startsWith("（") ? "" : $("workspaceId").textContent,
    cookieHeader: (await new Promise((res) =>
      chrome.runtime.sendMessage({ type: "get-snapshot" }, (r) => res((r && r.cookieHeader) || ""))
    )),
    enabled: $("enabled").checked,
  };
  if (!payload.email || !payload.password) {
    setStatus("请填写邮箱与密码", "bad");
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
loadDefaults();
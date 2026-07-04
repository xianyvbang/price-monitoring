const KEYS = ["appBase", "sessionCookieName", "adminUser", "adminPassword"];
const DEFAULT_APP_BASE = "__APP_BASE_URL__";
const statusEl = document.getElementById("status");

function normalizeAppBase(value) {
  const text = String(value || "").trim().replace(/\/$/, "");
  if (!text || text === "__APP_BASE_URL__") return "";
  return text;
}

function setStatus(text, isErr) {
  statusEl.textContent = text;
  statusEl.classList.toggle("err", !!isErr);
  setTimeout(() => (statusEl.textContent = ""), 3000);
}

async function load() {
  const data = await chrome.storage.local.get(KEYS);
  document.getElementById("appBase").value = normalizeAppBase(data.appBase) || normalizeAppBase(DEFAULT_APP_BASE);
  document.getElementById("sessionCookieName").value = data.sessionCookieName || "balance_monitor_session";
  document.getElementById("adminUser").value = data.adminUser || "";
  document.getElementById("adminPassword").value = data.adminPassword || "";
}

document.getElementById("save").addEventListener("click", async () => {
  const obj = {};
  for (const key of KEYS) obj[key] = document.getElementById(key).value.trim();
  obj.appBase = normalizeAppBase(obj.appBase) || normalizeAppBase(DEFAULT_APP_BASE);
  await chrome.storage.local.set(obj);
  setStatus("已保存");
});

document.getElementById("export").addEventListener("click", async () => {
  const data = await chrome.storage.local.get(KEYS);
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "account-grabber-config.json";
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
  setStatus("已导出");
});

const importFile = document.getElementById("import-file");
document.getElementById("import-btn").addEventListener("click", () => importFile.click());
importFile.addEventListener("change", () => {
  const file = importFile.files && importFile.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => {
    try {
      const obj = JSON.parse(reader.result);
      let filled = 0;
      for (const key of KEYS) {
        if (typeof obj[key] === "string" && obj[key] !== "") {
          document.getElementById(key).value = obj[key];
          filled += 1;
        }
      }
      importFile.value = "";
      setStatus(filled ? `已回填 ${filled} 项，请点击保存` : "文件中无可识别配置", !filled);
    } catch (e) {
      setStatus("解析失败：" + e.message, true);
    }
  };
  reader.onerror = () => setStatus("读取文件失败", true);
  reader.readAsText(file);
});

load();

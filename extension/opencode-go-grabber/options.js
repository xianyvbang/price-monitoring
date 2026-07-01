const KEYS = ["appBase", "sessionCookieName", "adminUser", "adminPassword", "defaultRecoveryEmail"];

async function load() {
  const data = await chrome.storage.local.get(KEYS);
  document.getElementById("appBase").value = data.appBase || "__APP_BASE_URL__";
  document.getElementById("sessionCookieName").value = data.sessionCookieName || "balance_monitor_session";
  document.getElementById("adminUser").value = data.adminUser || "";
  document.getElementById("adminPassword").value = data.adminPassword || "";
  document.getElementById("defaultRecoveryEmail").value = data.defaultRecoveryEmail || "";
}

document.getElementById("save").addEventListener("click", async () => {
  const obj = {};
  for (const k of KEYS) obj[k] = document.getElementById(k).value.trim();
  await chrome.storage.local.set(obj);
  const s = document.getElementById("status");
  s.textContent = "已保存";
  setTimeout(() => (s.textContent = ""), 2000);
});

load();
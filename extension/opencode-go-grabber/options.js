const KEYS = ["appBase", "sessionCookieName", "adminUser", "adminPassword"];

const statusEl = document.getElementById("status");

function setStatus(text, isErr) {
  statusEl.textContent = text;
  statusEl.classList.toggle("err", !!isErr);
  setTimeout(() => (statusEl.textContent = ""), 3000);
}

async function load() {
  const data = await chrome.storage.local.get(KEYS);
  document.getElementById("appBase").value = data.appBase || "__APP_BASE_URL__";
  document.getElementById("sessionCookieName").value = data.sessionCookieName || "balance_monitor_session";
  document.getElementById("adminUser").value = data.adminUser || "";
  document.getElementById("adminPassword").value = data.adminPassword || "";
}

document.getElementById("save").addEventListener("click", async () => {
  const obj = {};
  for (const k of KEYS) obj[k] = document.getElementById(k).value.trim();
  await chrome.storage.local.set(obj);
  setStatus("已保存");
});

// 导出：把当前 4 项配置打包成 JSON 下载（页内 Blob + <a download>，无需 downloads 权限）。
document.getElementById("export").addEventListener("click", async () => {
  const data = await chrome.storage.local.get(KEYS);
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "opencode-go-grabber-config.json";
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
  setStatus("已导出");
});

// 导入：读 JSON 文件、回填到表单（不直接落库，留给「保存」按钮确认，符合现有交互）。
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
      for (const k of KEYS) {
        if (typeof obj[k] === "string" && obj[k] !== "") {
          document.getElementById(k).value = obj[k];
          filled++;
        }
      }
      importFile.value = "";
      if (filled === 0) {
        setStatus("文件中无可识别配置", true);
      } else {
        setStatus(`已回填 ${filled} 项，请点击「保存」`);
      }
    } catch (e) {
      setStatus("解析失败：" + e.message, true);
    }
  };
  reader.onerror = () => setStatus("读取文件失败", true);
  reader.readAsText(file);
});

load();
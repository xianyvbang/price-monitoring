<script setup>
import { computed, onMounted, reactive, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { CopyDocument, Delete, Edit, Link, Plus, Refresh, Setting, Timer, Upload } from "@element-plus/icons-vue";
import { api } from "../api";
import { useViewport } from "../composables/useViewport";
import { boolValue, formatTime } from "../utils";

const loading = ref(false);
const refreshingAll = ref(false);
const accounts = ref([]);
const summary = ref({ account_count: 0, last_success_at: null });
const dialogVisible = ref(false);
const dialogMode = ref("create");
const saving = ref(false);
const bulkDialogVisible = ref(false);
const bulkText = ref("");
const importingBulk = ref(false);
const settingsDialogVisible = ref(false);
const savingSettings = ref(false);
const opencodeSettings = ref({
  lite_subscription_js_url: "",
  lite_subscription_server_id: "",
  default_server_id: "",
  server_instance: "server-fn:3",
  key_list_js_url: "",
  key_list_server_id: "",
  default_key_list_js_url: "https://opencode.ai/_build/assets/index-PbCOrg8_.js",
  default_key_list_server_id: "",
  key_list_server_instance: "server-fn:2"
});
const settingsForm = reactive({ lite_subscription_js_url: "", key_list_js_url: "" });
const sessionDialogVisible = ref(false);
const importingSession = ref(false);
const sessionAccount = ref(null);
const sessionPassword = ref("");
const sessionPasswordLoading = ref(false);
const sessionStateLoading = ref(false);
const sessionForm = reactive({ workspace_id: "", storage_state: "" });
const historyVisible = ref(false);
const historyLoading = ref(false);
const historyAccount = ref(null);
const historyRecords = ref([]);
const formRef = ref(null);
const form = reactive(defaultForm());
const { isMobile } = useViewport();

const accountCount = computed(() => summary.value.account_count ?? summary.value.accountCount ?? accounts.value.length);
const lastSuccessAt = computed(() => summary.value.last_success_at ?? summary.value.lastSuccessAt);
const bulkPreviewCount = computed(() => bulkText.value.split(/\r?\n/).filter((line) => line.trim()).length);
const liteSubscriptionJsUrl = computed(() => opencodeSettings.value.lite_subscription_js_url || opencodeSettings.value.liteSubscriptionJsUrl || "");
const liteSubscriptionServerId = computed(() => opencodeSettings.value.lite_subscription_server_id || opencodeSettings.value.liteSubscriptionServerId || "");
const defaultServerId = computed(() => opencodeSettings.value.default_server_id || opencodeSettings.value.defaultServerId || "");
const serverInstance = computed(() => opencodeSettings.value.server_instance || opencodeSettings.value.serverInstance || "server-fn:3");
const defaultKeyListJsUrl = computed(() => opencodeSettings.value.default_key_list_js_url || opencodeSettings.value.defaultKeyListJsUrl || "https://opencode.ai/_build/assets/index-PbCOrg8_.js");
const defaultKeyListServerId = computed(() => opencodeSettings.value.default_key_list_server_id || opencodeSettings.value.defaultKeyListServerId || "");
const keyListJsUrl = computed(() => opencodeSettings.value.key_list_js_url || opencodeSettings.value.keyListJsUrl || defaultKeyListJsUrl.value);
const keyListServerId = computed(() => opencodeSettings.value.key_list_server_id || opencodeSettings.value.keyListServerId || "");
const keyListServerInstance = computed(() => opencodeSettings.value.key_list_server_instance || opencodeSettings.value.keyListServerInstance || "server-fn:2");
const storageStateConsoleCommand = String.raw`(async () => {
  const write = (value) => typeof copy === "function" ? copy(value) : navigator.clipboard.writeText(value);
  const fetchText = async (url) => {
    try {
      const response = await fetch(url, { credentials: "include" });
      return response.ok ? await response.text() : "";
    } catch (_error) {
      return "";
    }
  };
  const assetUrls = new Set(
    Array.from(document.querySelectorAll("script[src],link[href]"))
      .map((element) => element.src || element.href)
      .filter((url) => url && url.includes("/_build/assets/") && url.endsWith(".js"))
  );
  const pageHtml = await fetchText(location.href);
  for (const match of pageHtml.matchAll(/(?:src|href)=["']([^"']*\/_build\/assets\/[^"']+\.js)["']/g)) {
    assetUrls.add(new URL(match[1], location.origin).href);
  }
  const loadedUrls = new Set();
  const sources = [];
  for (let round = 0; round < 2; round += 1) {
    for (const url of Array.from(assetUrls)) {
      if (loadedUrls.has(url)) continue;
      loadedUrls.add(url);
      const source = await fetchText(url);
      sources.push(source);
      for (const match of source.matchAll(/["'](_build\/assets\/[^"']+\.js)["']/g)) {
        assetUrls.add(new URL("/" + match[1], location.origin).href);
      }
    }
  }
  const findServerId = (patterns) => {
    for (const source of sources) {
      for (const pattern of patterns) {
        const match = source.match(pattern);
        if (match) return match[1];
      }
    }
    return "";
  };
  const serverIds = {
    "session.get": findServerId([
      /querySessionInfo_query\s*=\s*createServerReference\("([0-9a-f]{64})"\)/,
      /querySessionInfo\s*=\s*createServerReference\("([0-9a-f]{64})"\)/
    ]),
    "lite.subscription.get": findServerId([
      /queryLiteSubscription_query\s*=\s*createServerReference\("([0-9a-f]{64})"\)/,
      /queryLiteSubscription\s*=\s*createServerReference\("([0-9a-f]{64})"\)/
    ]),
    "key.list": findServerId([
      /listKeys_query\s*=\s*createServerReference\("([0-9a-f]{64})"\)/,
      /listKeys\s*=\s*createServerReference\("([0-9a-f]{64})"\)/
    ])
  };
  const storageAuthPair = () => {
    for (const storage of [localStorage, sessionStorage]) {
      for (let index = 0; index < storage.length; index += 1) {
        const key = storage.key(index);
        if (!key || key.toLowerCase() !== "auth") continue;
        const value = storage.getItem(key) || "";
        if (value) return "auth=" + value;
      }
    }
    for (const storage of [localStorage, sessionStorage]) {
      for (let index = 0; index < storage.length; index += 1) {
        const key = storage.key(index);
        if (!key || !key.toLowerCase().includes("auth")) continue;
        const value = storage.getItem(key) || "";
        if (value) return key + "=" + value;
      }
    }
    return "";
  };
  const cookiePairs = document.cookie
    .split(";")
    .map((item) => item.trim())
    .filter(Boolean);
  const authPair = cookiePairs.find((item) => item.split("=")[0] === "auth") || storageAuthPair();
  if (!authPair) {
    throw new Error("没有读取到 auth。auth 是 HttpOnly 时控制台无法读取，请在 DevTools 的 Application > Cookies 或 Network 请求头里手动复制 auth=...");
  }
  const authName = authPair.split("=")[0];
  const authCookiePairs = [authPair, ...cookiePairs.filter((item) => item.split("=")[0] !== authName && item.split("=")[0] !== "oc_locale")];
  if (!authCookiePairs.some((item) => item.split("=")[0] === "auth")) {
    throw new Error("没有读取到名为 auth 的键值，请手动复制 auth=...");
  }
  const cookies = authCookiePairs
    .map((item) => {
      const [name, ...valueParts] = item.split("=");
      return {
        name,
        value: valueParts.join("="),
        domain: location.hostname,
        path: "/",
        secure: location.protocol === "https:",
        httpOnly: false,
        sameSite: "Lax"
      };
    });
  const localStorageItems = Object.keys(localStorage).map((name) => ({
    name,
    value: localStorage.getItem(name) || ""
  }));
  const storageState = {
    cookies,
    origins: [{ origin: location.origin, localStorage: localStorageItems }],
    serverIds,
    workspace_id: (location.pathname.match(/\/workspace\/([^/]+)/) || [])[1] || ""
  };
  await write(JSON.stringify(storageState, null, 2));
  return storageState;
})()`;
const cookieConsoleCommand = `(() => {
  const pairs = document.cookie.split(";").map((item) => item.trim()).filter(Boolean);
  const authPair = pairs.find((item) => item.split("=")[0] === "auth");
  const storageAuthPair = () => {
    for (const storage of [localStorage, sessionStorage]) {
      for (let index = 0; index < storage.length; index += 1) {
        const key = storage.key(index);
        if (!key || key.toLowerCase() !== "auth") continue;
        const value = storage.getItem(key) || "";
        if (value) return "auth=" + value;
      }
    }
    for (const storage of [localStorage, sessionStorage]) {
      for (let index = 0; index < storage.length; index += 1) {
        const key = storage.key(index);
        if (!key || !key.toLowerCase().includes("auth")) continue;
        const value = storage.getItem(key) || "";
        if (value) return key + "=" + value;
      }
    }
    return "";
  };
  const value = authPair || storageAuthPair();
  if (!value) throw new Error("没有读取到 auth。若 Application/Cookies 里能看到 auth 但这里读不到，说明它是 HttpOnly，只能在开发者工具 Application 或 Network 请求头里手动复制 auth=...");
  const write = (text) => typeof copy === "function" ? copy(text) : navigator.clipboard.writeText(text);
  write(value);
  return value;
})()`;

const rules = {
  name: [{ required: true, message: "请输入名称", trigger: "blur" }],
  email: [{ required: true, message: "请输入 Google 邮箱", trigger: "blur" }],
  password: [
    {
      validator: (_rule, value, callback) => {
        if (dialogMode.value === "create" && !value) {
          callback(new Error("请输入 Google 密码"));
          return;
        }
        callback();
      },
      trigger: "blur"
    }
  ]
};

async function loadAccounts() {
  loading.value = true;
  try {
    const payload = await api.opencodeGoAccounts();
    accounts.value = (payload.accounts || []).map(normalizeAccountUsage);
    summary.value = payload.summary || {};
  } catch (error) {
    ElMessage.error(error.message || "加载 OpenCode Go 账号失败");
  } finally {
    loading.value = false;
  }
}

async function loadOpenCodeSettings() {
  try {
    const payload = await api.opencodeGoSettings();
    opencodeSettings.value = payload.settings || {};
  } catch (error) {
    ElMessage.error(error.message || "加载 OpenCode Go 配置失败");
  }
}

function defaultForm() {
  return {
    id: "",
    name: "",
    email: "",
    password: "",
    workspace_id: "",
    is_enabled: false
  };
}

function resetForm() {
  Object.assign(form, defaultForm());
}

function openCreate() {
  dialogMode.value = "create";
  resetForm();
  dialogVisible.value = true;
}

function openBulkImport() {
  bulkText.value = "";
  bulkDialogVisible.value = true;
}

function openSettingsDialog() {
  settingsForm.lite_subscription_js_url = liteSubscriptionJsUrl.value;
  settingsForm.key_list_js_url = keyListJsUrl.value;
  settingsDialogVisible.value = true;
}

async function saveSettings() {
  savingSettings.value = true;
  try {
    const response = await api.saveOpencodeGoSettings({
      lite_subscription_js_url: settingsForm.lite_subscription_js_url,
      key_list_js_url: settingsForm.key_list_js_url
    });
    opencodeSettings.value = response.settings || {};
    settingsDialogVisible.value = false;
    ElMessage.success("OpenCode Go JS 文件配置已保存");
  } catch (error) {
    ElMessage.error(error.message || "保存配置失败");
  } finally {
    savingSettings.value = false;
  }
}

function openEdit(account) {
  dialogMode.value = "edit";
  Object.assign(form, {
    id: account.id,
    name: account.name || "",
    email: account.email || "",
    password: "",
    workspace_id: account.workspace_id || account.workspaceId || "",
    is_enabled: boolValue(account.is_enabled)
  });
  dialogVisible.value = true;
}

async function submitAccount() {
  if (formRef.value) {
    await formRef.value.validate();
  }
  saving.value = true;
  try {
    const payload = { ...form };
    if (!payload.password) {
      delete payload.password;
    }
    const response = dialogMode.value === "create" ? await api.createOpencodeGoAccount(payload) : await api.updateOpencodeGoAccount(form.id, payload);
    upsertLocal(response.account);
    dialogVisible.value = false;
    ElMessage.success(dialogMode.value === "create" ? "OpenCode Go 账号已保存" : "OpenCode Go 账号已更新");
  } catch (error) {
    ElMessage.error(error.message || "保存失败");
  } finally {
    saving.value = false;
  }
}

async function submitBulkImport() {
  importingBulk.value = true;
  try {
    const response = await api.bulkOpencodeGoAccounts({ bulk_text: bulkText.value });
    bulkDialogVisible.value = false;
    await loadAccounts();
    ElMessage.success(`已导入或更新 ${response.count || 0} 个 OpenCode Go 账号`);
  } catch (error) {
    ElMessage.error(error.message || "批量导入失败");
  } finally {
    importingBulk.value = false;
  }
}

function openSessionImport(account) {
  sessionAccount.value = account;
  sessionPassword.value = "";
  sessionPasswordLoading.value = false;
  sessionForm.workspace_id = account.workspace_id || account.workspaceId || "";
  sessionForm.storage_state = "";
  sessionDialogVisible.value = true;
  loadSessionPassword(account);
  loadSessionState(account);
}

async function loadSessionPassword(account) {
  const hasPassword = account?.has_password || account?.hasPassword;
  if (!hasPassword) {
    return;
  }
  sessionPasswordLoading.value = true;
  try {
    const payload = await api.opencodeGoPassword(account.id);
    if (String(sessionAccount.value?.id) === String(account.id)) {
      sessionPassword.value = payload.password || "";
    }
  } catch (error) {
    if (String(sessionAccount.value?.id) === String(account.id)) {
      ElMessage.error(error.message || "读取密码失败");
    }
  } finally {
    if (String(sessionAccount.value?.id) === String(account.id)) {
      sessionPasswordLoading.value = false;
    }
  }
}

async function loadSessionState(account) {
  const hasSession = account?.has_session || account?.hasSession;
  if (!hasSession) {
    return;
  }
  sessionStateLoading.value = true;
  try {
    const payload = await api.opencodeGoSession(account.id);
    if (String(sessionAccount.value?.id) === String(account.id)) {
      sessionForm.workspace_id = payload.workspace_id || payload.workspaceId || sessionForm.workspace_id;
      sessionForm.storage_state = payload.storage_state || payload.storageState || "";
    }
  } catch (error) {
    if (String(sessionAccount.value?.id) === String(account.id)) {
      ElMessage.error(error.message || "读取登录态失败");
    }
  } finally {
    if (String(sessionAccount.value?.id) === String(account.id)) {
      sessionStateLoading.value = false;
    }
  }
}

async function submitSessionImport() {
  if (!sessionAccount.value) {
    return;
  }
  if (!String(sessionForm.workspace_id || "").trim()) {
    ElMessage.error("请填写 Workspace ID");
    return;
  }
  if (!String(sessionForm.storage_state || "").trim()) {
    ElMessage.error("请填写 auth Cookie 或登录态 JSON");
    return;
  }
  importingSession.value = true;
  try {
    const response = await api.importOpencodeGoSession(sessionAccount.value.id, {
      workspace_id: sessionForm.workspace_id,
      storage_state: sessionForm.storage_state
    });
    upsertLocal(response.account);
    sessionDialogVisible.value = false;
    ElMessage.success("登录态已导入");
  } catch (error) {
    ElMessage.error(error.message || "导入登录态失败");
  } finally {
    importingSession.value = false;
  }
}

function openLocalOpencodeLogin() {
  const url = new URL("https://auth.opencode.ai/google/authorize");
  const email = String(sessionAccount.value?.email || "").trim();
  if (email) {
    url.searchParams.set("login_hint", email);
  }
  window.open(url.toString(), "_blank", "noopener,noreferrer");
}

function upsertLocal(account) {
  if (!account) {
    loadAccounts();
    return;
  }
  account = normalizeAccountUsage(account);
  const index = accounts.value.findIndex((item) => String(item.id) === String(account.id));
  if (index >= 0) {
    accounts.value[index] = account;
  } else {
    accounts.value.unshift(account);
  }
  summary.value = { ...summary.value, account_count: accounts.value.length, accountCount: accounts.value.length };
}

async function deleteAccount(account) {
  await ElMessageBox.confirm(`确定删除 ${account.name} 吗？`, "删除 OpenCode Go 账号", { type: "warning" });
  await api.deleteOpencodeGoAccount(account.id);
  accounts.value = accounts.value.filter((item) => item.id !== account.id);
  summary.value = { ...summary.value, account_count: accounts.value.length, accountCount: accounts.value.length };
  ElMessage.success("账号已删除");
}

async function toggleEnabled(account) {
  try {
    const response = await api.setOpencodeGoEnabled(account.id, !boolValue(account.is_enabled));
    upsertLocal(response.account);
    ElMessage.success("自动刷新状态已保存");
  } catch (error) {
    ElMessage.error(error.message || "保存失败");
  }
}

async function refreshAccount(account) {
  account._refreshing = true;
  try {
    const response = await api.refreshOpencodeGo(account.id);
    upsertLocal(response.account);
    ElMessage.success("刷新完成");
  } catch (error) {
    if (error.payload?.account) {
      upsertLocal(error.payload.account);
    }
    ElMessage.error(error.message || "刷新失败");
  } finally {
    account._refreshing = false;
  }
}

async function refreshAll() {
  refreshingAll.value = true;
  try {
    await api.refreshAllOpencodeGo();
    await loadAccounts();
    ElMessage.success("OpenCode Go 全部刷新完成");
  } catch (error) {
    ElMessage.error(error.message || "刷新全部失败");
  } finally {
    refreshingAll.value = false;
  }
}

async function copyApiKey(account) {
  try {
    const payload = await api.opencodeGoApiKey(account.id);
    const keyValue = payload.api_key || payload.apiKey;
    await copyText(keyValue, "API key 已复制");
  } catch (error) {
    ElMessage.error(error.message || "复制失败");
  }
}

function legacyCopyText(text) {
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "");
  textarea.style.left = "-9999px";
  textarea.style.opacity = "0";
  textarea.style.position = "fixed";
  textarea.style.top = "-9999px";
  document.body.appendChild(textarea);
  textarea.focus();
  textarea.select();
  textarea.setSelectionRange(0, textarea.value.length);
  const copied = document.execCommand("copy");
  document.body.removeChild(textarea);
  if (!copied) {
    throw new Error("浏览器拒绝复制，请手动选中文字复制");
  }
}

async function copyText(value, successMessage) {
  const text = value === null || value === undefined ? "" : String(value);
  if (!text) {
    throw new Error("没有可复制的数据");
  }
  if (navigator.clipboard?.writeText && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      ElMessage.success(successMessage);
      return;
    } catch (_error) {
      legacyCopyText(text);
      ElMessage.success(successMessage);
      return;
    }
  }
  legacyCopyText(text);
  ElMessage.success(successMessage);
}

async function copySessionEmail() {
  const email = String(sessionAccount.value?.email || "").trim();
  if (!email) {
    ElMessage.error("当前账号没有邮箱");
    return;
  }
  try {
    await copyText(email, "邮箱已复制");
  } catch (error) {
    ElMessage.error(error.message || "复制邮箱失败");
  }
}

async function copySessionPassword() {
  if (!sessionAccount.value) {
    return;
  }
  try {
    await copyText(sessionPassword.value, "密码已复制");
  } catch (error) {
    ElMessage.error(error.message || "复制密码失败");
  }
}

async function copyConsoleCommand(command, label) {
  try {
    await copyText(command, `${label}已复制`);
  } catch (error) {
    ElMessage.error(error.message || "复制命令失败");
  }
}

async function openHistory(account) {
  historyVisible.value = true;
  historyLoading.value = true;
  historyAccount.value = account;
  historyRecords.value = [];
  try {
    const payload = await api.opencodeGoHistory(account.id, { limit: 100 });
    historyRecords.value = (payload.records || []).map(normalizeAccountUsage);
  } catch (error) {
    ElMessage.error(error.message || "获取历史失败");
  } finally {
    historyLoading.value = false;
  }
}

function statusType(account) {
  if (account.last_status === "valid" || account.last_status === "logged_in") return "success";
  if (account.last_status === "invalid") return "danger";
  return "info";
}

function statusText(account) {
  if (account.last_status === "logged_in") return "已登录";
  if (account.last_status === "valid") return "正常";
  if (account.last_status === "invalid") return "失败";
  return "未查询";
}

function usageWindow(account, key) {
  return normalizeUsageWindow(account?.[key] ?? account?.[toCamel(key)] ?? {});
}

function toCamel(value) {
  return value.replace(/_([a-z])/g, (_, letter) => letter.toUpperCase());
}

function usagePercent(window) {
  const value = window?.usage_percent ?? window?.usagePercent;
  const number = Number(value);
  return Number.isFinite(number) ? Math.max(0, Math.min(100, number)) : 0;
}

function usageLabel(window) {
  const value = window?.usage_percent ?? window?.usagePercent;
  return value === null || value === undefined ? "-" : `${Number(value).toFixed(1)}%`;
}

function resetText(window) {
  const value = Number(window?.reset_in_sec ?? window?.resetInSec);
  if (!Number.isFinite(value) || value <= 0) {
    return "-";
  }
  const hours = Math.floor(value / 3600);
  const minutes = Math.floor((value % 3600) / 60);
  if (hours >= 24) {
    const days = Math.floor(hours / 24);
    return `${days}天${hours % 24}小时`;
  }
  if (hours > 0) {
    return `${hours}小时${minutes}分`;
  }
  return `${minutes}分`;
}

function usageColor(window) {
  const percent = usagePercent(window);
  if (percent >= 90) return "#d92d20";
  if (percent >= 70) return "#b54708";
  return "#07845f";
}

function usageCell(account, key) {
  return usageWindow(account, key);
}

function historyUsage(record, key) {
  return usageWindow(record, key);
}

function normalizeAccountUsage(account) {
  if (!account || typeof account !== "object") {
    return account;
  }
  return {
    ...account,
    rolling_usage: normalizeUsageWindow(account.rolling_usage ?? account.rollingUsage),
    weekly_usage: normalizeUsageWindow(account.weekly_usage ?? account.weeklyUsage),
    monthly_usage: normalizeUsageWindow(account.monthly_usage ?? account.monthlyUsage)
  };
}

function normalizeUsageWindow(value) {
  let window = value;
  if (typeof window === "string") {
    try {
      window = JSON.parse(window);
    } catch (_error) {
      window = {};
    }
  }
  const found = findUsageWindow(window);
  const usagePercentValue = found?.usage_percent ?? found?.usagePercent;
  const resetInSecValue = found?.reset_in_sec ?? found?.resetInSec;
  const usagePercentNumber = Number(usagePercentValue);
  const resetInSecNumber = Number(resetInSecValue);
  return {
    usage_percent: Number.isFinite(usagePercentNumber) ? usagePercentNumber : null,
    reset_in_sec: Number.isFinite(resetInSecNumber) ? resetInSecNumber : null
  };
}

function findUsageWindow(value) {
  if (!value || typeof value !== "object") {
    return {};
  }
  if ("usage_percent" in value || "usagePercent" in value || "reset_in_sec" in value || "resetInSec" in value) {
    return value;
  }
  for (const item of Object.values(value)) {
    const found = findUsageWindow(item);
    if (found && ("usage_percent" in found || "usagePercent" in found || "reset_in_sec" in found || "resetInSec" in found)) {
      return found;
    }
  }
  return {};
}

onMounted(() => {
  loadOpenCodeSettings();
  loadAccounts();
});
</script>

<template>
  <section v-loading="loading">
    <div class="page-head">
      <div>
        <h1>OpenCode Go</h1>
        <p>{{ accountCount }} 个账号，最近成功 {{ formatTime(lastSuccessAt) }}</p>
      </div>
      <div class="page-actions">
        <el-button :icon="Setting" @click="openSettingsDialog">配置用量 JS</el-button>
        <el-button type="primary" :icon="Plus" @click="openCreate">添加账号</el-button>
        <el-button :icon="Upload" @click="openBulkImport">批量导入</el-button>
        <el-button :icon="Refresh" :loading="refreshingAll" @click="refreshAll">刷新全部</el-button>
      </div>
    </div>

    <div class="opencode-config-strip">
      <div>
        <span>用量 JS 文件</span>
        <strong>{{ liteSubscriptionJsUrl || "未配置，暂用内置默认 server id" }}</strong>
      </div>
      <div>
        <span>X-Server-Instance</span>
        <strong>{{ serverInstance }}</strong>
      </div>
      <div>
        <span>当前 X-Server-Id</span>
        <strong>{{ liteSubscriptionServerId || defaultServerId || "-" }}</strong>
      </div>
      <div>
        <span>API key JS 文件</span>
        <strong>{{ keyListJsUrl || defaultKeyListJsUrl }}</strong>
      </div>
      <div>
        <span>API key X-Server-Instance</span>
        <strong>{{ keyListServerInstance }}</strong>
      </div>
      <div>
        <span>API key X-Server-Id</span>
        <strong>{{ keyListServerId || defaultKeyListServerId || "-" }}</strong>
      </div>
    </div>

    <div class="panel table-card">
      <div class="panel-head">
        <h2>Go 用量</h2>
        <el-tag>{{ accounts.length }} 个账号</el-tag>
      </div>
      <template v-if="!isMobile">
        <el-table :data="accounts" border stripe row-key="id" style="width: 100%">
          <el-table-column label="名称" min-width="150" fixed>
            <template #default="{ row }"><strong>{{ row.name }}</strong></template>
          </el-table-column>
          <el-table-column label="Google 邮箱" min-width="220">
            <template #default="{ row }"><span class="credentials-text">{{ row.email }}</span></template>
          </el-table-column>
          <el-table-column label="状态" width="105">
            <template #default="{ row }"><el-tag :type="statusType(row)">{{ statusText(row) }}</el-tag></template>
          </el-table-column>
          <el-table-column label="5h 用量" min-width="160">
            <template #default="{ row }">
              <div class="usage-bar">
                <el-progress :percentage="usagePercent(usageCell(row, 'rolling_usage'))" :stroke-width="8" :color="usageColor(usageCell(row, 'rolling_usage'))" :show-text="false" />
                <span>{{ usageLabel(usageCell(row, 'rolling_usage')) }}</span>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="7d 用量" min-width="160">
            <template #default="{ row }">
              <div class="usage-bar">
                <el-progress :percentage="usagePercent(usageCell(row, 'weekly_usage'))" :stroke-width="8" :color="usageColor(usageCell(row, 'weekly_usage'))" :show-text="false" />
                <span>{{ usageLabel(usageCell(row, 'weekly_usage')) }}</span>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="30d 用量" min-width="160">
            <template #default="{ row }">
              <div class="usage-bar">
                <el-progress :percentage="usagePercent(usageCell(row, 'monthly_usage'))" :stroke-width="8" :color="usageColor(usageCell(row, 'monthly_usage'))" :show-text="false" />
                <span>{{ usageLabel(usageCell(row, 'monthly_usage')) }}</span>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="重置倒计时" min-width="150">
            <template #default="{ row }">
              <div class="reset-list">
                <span>5h {{ resetText(usageCell(row, 'rolling_usage')) }}</span>
                <span>7d {{ resetText(usageCell(row, 'weekly_usage')) }}</span>
                <span>30d {{ resetText(usageCell(row, 'monthly_usage')) }}</span>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="API key" min-width="170">
            <template #default="{ row }">
              <span class="credentials-text">{{ row.api_key_masked || row.apiKeyMasked || "未找到" }}</span>
            </template>
          </el-table-column>
          <el-table-column label="最近刷新" width="165">
            <template #default="{ row }">{{ formatTime(row.last_checked_at) }}</template>
          </el-table-column>
          <el-table-column label="自动刷新" width="105">
            <template #default="{ row }"><el-switch :model-value="boolValue(row.is_enabled)" @change="toggleEnabled(row)" /></template>
          </el-table-column>
          <el-table-column label="操作" min-width="380" fixed="right">
            <template #default="{ row }">
              <div class="table-actions">
                <el-button size="small" :icon="Edit" @click="openEdit(row)">编辑</el-button>
                <el-button size="small" :icon="Upload" @click="openSessionImport(row)">导入登录态</el-button>
                <el-button size="small" :icon="Refresh" :loading="row._refreshing" @click="refreshAccount(row)">刷新</el-button>
                <el-button size="small" :icon="CopyDocument" :disabled="!row.has_api_key && !row.hasApiKey" @click="copyApiKey(row)">复制 Key</el-button>
                <el-button size="small" :icon="Timer" @click="openHistory(row)">历史</el-button>
                <el-button size="small" type="danger" :icon="Delete" @click="deleteAccount(row)">删除</el-button>
              </div>
            </template>
          </el-table-column>
        </el-table>
      </template>
      <div v-else class="mobile-stack">
        <article v-for="row in accounts" :key="row.id" class="mobile-card">
          <div class="mobile-card-head">
            <div class="mobile-card-title">
              <strong>{{ row.name }}</strong>
              <div class="mobile-card-meta">
                <span>{{ row.email }}</span>
                <span>API key: {{ row.api_key_masked || row.apiKeyMasked || "未找到" }}</span>
              </div>
            </div>
            <el-tag :type="statusType(row)">{{ statusText(row) }}</el-tag>
          </div>
          <div class="mobile-field-list">
            <div class="mobile-field">
              <span>5h 用量</span>
              <strong>{{ usageLabel(usageCell(row, 'rolling_usage')) }} · {{ resetText(usageCell(row, 'rolling_usage')) }}</strong>
            </div>
            <div class="mobile-field">
              <span>7d 用量</span>
              <strong>{{ usageLabel(usageCell(row, 'weekly_usage')) }} · {{ resetText(usageCell(row, 'weekly_usage')) }}</strong>
            </div>
            <div class="mobile-field">
              <span>30d 用量</span>
              <strong>{{ usageLabel(usageCell(row, 'monthly_usage')) }} · {{ resetText(usageCell(row, 'monthly_usage')) }}</strong>
            </div>
            <div class="mobile-field">
              <span>最近刷新</span>
              <strong>{{ formatTime(row.last_checked_at) }}</strong>
            </div>
          </div>
          <div class="mobile-switches">
            <div class="mobile-switch-row">
              <span>自动刷新</span>
              <el-switch :model-value="boolValue(row.is_enabled)" @change="toggleEnabled(row)" />
            </div>
          </div>
          <div class="mobile-actions">
            <el-button size="small" :icon="Edit" @click="openEdit(row)">编辑</el-button>
            <el-button size="small" :icon="Upload" @click="openSessionImport(row)">导入登录态</el-button>
            <el-button size="small" :icon="Refresh" :loading="row._refreshing" @click="refreshAccount(row)">刷新</el-button>
            <el-button size="small" :icon="CopyDocument" :disabled="!row.has_api_key && !row.hasApiKey" @click="copyApiKey(row)">复制 Key</el-button>
            <el-button size="small" :icon="Timer" @click="openHistory(row)">历史</el-button>
            <el-button size="small" type="danger" :icon="Delete" @click="deleteAccount(row)">删除</el-button>
          </div>
        </article>
      </div>
    </div>

    <el-dialog v-model="dialogVisible" :title="dialogMode === 'create' ? '添加 OpenCode Go 账号' : '编辑 OpenCode Go 账号'" width="560px">
      <el-form ref="formRef" :model="form" :rules="rules" label-position="top" @submit.prevent="submitAccount">
        <el-form-item label="名称" prop="name">
          <el-input v-model="form.name" placeholder="例如 opencode-main" />
        </el-form-item>
        <el-form-item label="Google 邮箱" prop="email">
          <el-input v-model="form.email" autocomplete="username" />
        </el-form-item>
        <el-form-item label="Google 密码" prop="password">
          <el-input v-model="form.password" type="password" show-password autocomplete="current-password" :placeholder="dialogMode === 'edit' ? '留空表示不修改' : ''" />
        </el-form-item>
        <el-form-item label="Workspace ID">
          <el-input v-model="form.workspace_id" placeholder="可留空，登录后自动识别" />
        </el-form-item>
        <el-form-item label="自动刷新">
          <el-switch v-model="form.is_enabled" />
        </el-form-item>
      </el-form>
      <template #footer>
        <div class="dialog-footer">
          <el-button @click="dialogVisible = false">取消</el-button>
          <el-button type="primary" :loading="saving" @click="submitAccount">保存</el-button>
        </div>
      </template>
    </el-dialog>

    <el-dialog v-model="bulkDialogVisible" title="批量导入 OpenCode Go 账号" width="680px">
      <el-form label-position="top" @submit.prevent="submitBulkImport">
        <el-form-item label="账号列表">
          <el-input
            v-model="bulkText"
            type="textarea"
            :rows="12"
            resize="vertical"
            placeholder="user1@example.com|password1&#10;user2@example.com|password2"
            autocomplete="off"
          />
        </el-form-item>
        <div class="import-helper">
          <span>格式：邮箱|邮箱密码，一行一条</span>
          <span>名称会自动使用邮箱，空行会跳过，重复名称会更新原账号</span>
          <span>待导入 {{ bulkPreviewCount }} 行</span>
        </div>
      </el-form>
      <template #footer>
        <div class="dialog-footer">
          <el-button @click="bulkDialogVisible = false">取消</el-button>
          <el-button type="primary" :loading="importingBulk" @click="submitBulkImport">导入</el-button>
        </div>
      </template>
    </el-dialog>

    <el-dialog v-model="settingsDialogVisible" title="配置 OpenCode Go JS 文件" width="720px">
      <el-form label-position="top" @submit.prevent="saveSettings">
        <el-form-item label="用量 JS 文件地址">
          <el-input
            v-model="settingsForm.lite_subscription_js_url"
            placeholder="https://opencode.ai/_build/assets/index-DtPYjwk4.js"
            autocomplete="off"
          />
        </el-form-item>
        <el-form-item label="API key JS 文件地址">
          <el-input
            v-model="settingsForm.key_list_js_url"
            placeholder="https://opencode.ai/_build/assets/index-PbCOrg8_.js"
            autocomplete="off"
          />
        </el-form-item>
        <div class="import-helper">
          <span>系统会下载这个 JS 文件，并从 const queryLiteSubscription_query = createServerReference(&quot;...&quot;) 中解析 X-Server-Id。</span>
          <span>用量请求固定为 /_server?id=解析到的 server id&amp;args=序列化后的 Workspace ID，X-Server-Instance 固定为 server-fn:3。</span>
          <span>当前解析到的 server id：{{ liteSubscriptionServerId || "未配置" }}；留空时会使用内置默认 server id：{{ defaultServerId || "-" }}</span>
          <span>API key 会从 listKeys_query 解析 X-Server-Id，请求固定使用 X-Server-Instance: {{ keyListServerInstance }}。</span>
          <span>当前 API key server id：{{ keyListServerId || "未配置" }}；默认 JS：{{ defaultKeyListJsUrl }}。</span>
        </div>
      </el-form>
      <template #footer>
        <div class="dialog-footer">
          <el-button @click="settingsDialogVisible = false">取消</el-button>
          <el-button type="primary" :loading="savingSettings" @click="saveSettings">保存</el-button>
        </div>
      </template>
    </el-dialog>

    <el-dialog v-model="sessionDialogVisible" title="导入 OpenCode Go 登录态" width="760px">
      <el-form label-position="top" @submit.prevent="submitSessionImport">
        <el-form-item label="账号">
          <div class="session-credential-list">
            <button class="session-copy-row" type="button" @click="copySessionEmail">
              <span>
                <small>邮箱</small>
                <strong>{{ sessionAccount?.email || "-" }}</strong>
              </span>
              <el-icon><CopyDocument /></el-icon>
            </button>
            <button class="session-copy-row" type="button" :disabled="sessionPasswordLoading || !sessionPassword" @click="copySessionPassword">
              <span>
                <small>密码</small>
                <strong>{{ sessionPasswordLoading ? "读取中..." : sessionPassword || "未保存密码" }}</strong>
              </span>
              <el-icon><CopyDocument /></el-icon>
            </button>
          </div>
        </el-form-item>
        <div class="manual-session-panel">
          <el-button type="primary" :icon="Link" @click="openLocalOpencodeLogin">打开本地浏览器登录页</el-button>
          <span>会在你当前设备的默认浏览器打开 OpenCode 登录页，并把当前邮箱作为登录提示带过去。密码不会写入链接；登录后复制登录态再粘贴到下方。</span>
        </div>
        <el-form-item label="Workspace ID（必填）">
          <el-input v-model="sessionForm.workspace_id" placeholder="wrk_01KW01D1MG4VHNMJWA2KSH83CQ" />
        </el-form-item>
        <div class="session-command-panel">
          <div>
            <strong>浏览器控制台获取命令</strong>
            <span>登录 OpenCode 后，在页面按 F12 打开控制台，复制下面命令执行。若 auth 是 HttpOnly，控制台无法读取，请在 DevTools 的 Application > Cookies 或 Network 请求头里手动复制 auth=...。</span>
          </div>
          <button class="session-command-copy" type="button" @click="copyConsoleCommand(storageStateConsoleCommand, '登录态 JSON 命令')">
            <span>
              <small>登录态 JSON + ServerId 命令</small>
              <code>{{ storageStateConsoleCommand }}</code>
            </span>
            <el-icon><CopyDocument /></el-icon>
          </button>
          <button class="session-command-copy" type="button" @click="copyConsoleCommand(cookieConsoleCommand, 'Auth 值命令')">
            <span>
              <small>Auth 值命令</small>
              <code>{{ cookieConsoleCommand }}</code>
            </span>
            <el-icon><CopyDocument /></el-icon>
          </button>
        </div>
        <el-form-item label="登录态 JSON 或 Cookie（必填，必须包含 auth）">
          <el-input
            v-loading="sessionStateLoading"
            v-model="sessionForm.storage_state"
            type="textarea"
            :rows="12"
            resize="vertical"
            placeholder="auth=你的值 或 { &quot;cookies&quot;: [{ &quot;name&quot;: &quot;auth&quot;, &quot;value&quot;: &quot;你的值&quot;, &quot;domain&quot;: &quot;.opencode.ai&quot;, &quot;path&quot;: &quot;/&quot; }], &quot;origins&quot;: [] }"
            autocomplete="off"
          />
        </el-form-item>
        <div class="import-helper">
          <span>遇到 Google 验证码或 2FA 时，可在本地浏览器完成人工登录后导入登录态。</span>
          <span>如果 auth Cookie 勾选了 HttpOnly，控制台命令读不到它；请从 DevTools 的 Application > Cookies 复制 auth 的值，粘贴为 auth=你的值。</span>
          <span>导入后系统会加密保存登录态，后续刷新优先复用该会话。</span>
        </div>
      </el-form>
      <template #footer>
        <div class="dialog-footer">
          <el-button :icon="Link" @click="openLocalOpencodeLogin">打开本地浏览器登录页</el-button>
          <el-button @click="sessionDialogVisible = false">取消</el-button>
          <el-button type="primary" :loading="importingSession" @click="submitSessionImport">导入</el-button>
        </div>
      </template>
    </el-dialog>

    <el-dialog v-model="historyVisible" title="OpenCode Go 历史" width="900px">
      <p class="muted">{{ historyAccount?.name }} · {{ historyAccount?.email }}</p>
      <el-table v-loading="historyLoading" :data="historyRecords" border stripe>
        <el-table-column label="时间" width="165">
          <template #default="{ row }">{{ formatTime(row.checked_at) }}</template>
        </el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }"><el-tag :type="row.is_valid ? 'success' : 'danger'">{{ row.is_valid ? "成功" : "失败" }}</el-tag></template>
        </el-table-column>
        <el-table-column label="5h" min-width="120">
          <template #default="{ row }">{{ usageLabel(historyUsage(row, 'rolling_usage')) }}</template>
        </el-table-column>
        <el-table-column label="7d" min-width="120">
          <template #default="{ row }">{{ usageLabel(historyUsage(row, 'weekly_usage')) }}</template>
        </el-table-column>
        <el-table-column label="30d" min-width="120">
          <template #default="{ row }">{{ usageLabel(historyUsage(row, 'monthly_usage')) }}</template>
        </el-table-column>
        <el-table-column label="API key" min-width="150">
          <template #default="{ row }">{{ row.api_key_masked || "-" }}</template>
        </el-table-column>
        <el-table-column label="错误" min-width="220">
          <template #default="{ row }"><span class="danger-text">{{ row.error || "-" }}</span></template>
        </el-table-column>
      </el-table>
      <template #footer>
        <div class="dialog-footer">
          <el-button @click="historyVisible = false">关闭</el-button>
        </div>
      </template>
    </el-dialog>
  </section>
</template>

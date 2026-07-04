<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { CopyDocument, Delete, Document, Download, Edit, Link, Plus, Refresh, Setting, Timer, Upload } from "@element-plus/icons-vue";
import { api } from "../api";
import { useViewport } from "../composables/useViewport";
import { boolValue, formatTime } from "../utils";
import { grabFromExtension as extGrab, isGrabberReady } from "../utils/grabber";

const loading = ref(false);
const refreshingAll = ref(false);
const accounts = ref([]);
const pagination = reactive({ page: 1, page_size: 20, total: 0, total_pages: 1 });
const selectedAccountIds = ref([]);
const summary = ref({ account_count: 0, last_success_at: null });
const refreshRemaining = ref(300);
const refreshTimer = ref(null);
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
  key_list_server_instance: "server-fn:2",
  query_interval: 300,
  monitor_paused: false
});
const settingsForm = reactive({ lite_subscription_js_url: "", key_list_js_url: "" });
const sessionDialogVisible = ref(false);
const importingSession = ref(false);
const grabberReady = ref(isGrabberReady());
const grabbingFromExt = ref(false);
const sessionAccount = ref(null);
const sessionPassword = ref("");
const sessionPasswordLoading = ref(false);
const sessionStateLoading = ref(false);
const sessionForm = reactive({ workspace_id: "", storage_state: "" });
const historyVisible = ref(false);
const historyLoading = ref(false);
const historyAccount = ref(null);
const historyRecords = ref([]);
const importLogsVisible = ref(false);
const importLogsLoading = ref(false);
const importLogs = ref([]);
const importLogsPagination = reactive({ page: 1, page_size: 50, total: 0, total_pages: 1 });
const sub2ApiImportVisible = ref(false);
const sub2ApiImportLoading = ref(false);
const sub2ApiImporting = ref(false);
const sub2ApiImportAccount = ref(null);
const sub2ApiGroups = ref([]);
const sub2ApiSelectedGroupIds = ref([]);
const cpaImportingId = ref(null);
const cpaBulkImporting = ref(false);
const formRef = ref(null);
const form = reactive(defaultForm());
const { isMobile } = useViewport();

const accountCount = computed(() => summary.value.account_count ?? summary.value.accountCount ?? accounts.value.length);
const lastSuccessAt = computed(() => summary.value.last_success_at ?? summary.value.lastSuccessAt);
const eligibleAccountCount = computed(() => summary.value.eligible_account_count ?? summary.value.eligibleAccountCount ?? 0);
const overallRollingUsage = computed(() => usagePercentWindow(summary.value.overall_rolling_usage_percent ?? summary.value.overallRollingUsagePercent));
const overallWeeklyUsage = computed(() => usagePercentWindow(summary.value.overall_weekly_usage_percent ?? summary.value.overallWeeklyUsagePercent));
const selectedAccounts = computed(() => accounts.value.filter((account) => selectedAccountIds.value.includes(account.id)));
const selectedImportableAccounts = computed(() => selectedAccounts.value.filter(hasApiKey));
const selectedImportCount = computed(() => selectedImportableAccounts.value.length);
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
const queryInterval = computed(() => normalizedQueryInterval(opencodeSettings.value.query_interval ?? opencodeSettings.value.queryInterval));
const monitorPaused = computed(() => boolValue(opencodeSettings.value.monitor_paused ?? opencodeSettings.value.monitorPaused));
const sub2ApiImportName = computed(() => {
  const email = String(sub2ApiImportAccount.value?.email || "").trim();
  return email ? `opencode-${email}` : "-";
});

const rules = {
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
    const payload = await api.opencodeGoAccounts({
      page: pagination.page,
      page_size: pagination.page_size,
      sort_by: "created_at",
      sort_order: "desc"
    });
    accounts.value = (payload.accounts || []).map(normalizeAccountUsage);
    const page = payload.pagination || {};
    pagination.page = page.page || pagination.page;
    pagination.page_size = page.page_size || page.pageSize || pagination.page_size;
    pagination.total = page.total ?? accounts.value.length;
    pagination.total_pages = page.total_pages || page.totalPages || 1;
    summary.value = payload.summary || {};
    selectedAccountIds.value = [];
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
    resetRefreshCountdown();
  } catch (error) {
    ElMessage.error(error.message || "加载 OpenCode Go 配置失败");
  }
}

function resetRefreshCountdown() {
  refreshRemaining.value = queryInterval.value;
}

function normalizedQueryInterval(value) {
  const number = Number(value ?? 300);
  return Number.isFinite(number) ? Math.max(300, number) : 300;
}

function defaultForm() {
  return {
    id: "",
    email: "",
    password: "",
    recovery_email: "",
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
    resetRefreshCountdown();
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
    email: account.email || "",
    password: "",
    recovery_email: account.recovery_email || account.recoveryEmail || "",
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
    await (dialogMode.value === "create" ? api.createOpencodeGoAccount(payload) : api.updateOpencodeGoAccount(form.id, payload));
    dialogVisible.value = false;
    await loadAccounts();
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
    pagination.page = 1;
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
  window.open("https://opencode.ai/zen", "_blank", "noopener,noreferrer");
}

// 下载定制后的浏览器扩展（同源，自动带会话 cookie）。zip 内 manifest 已按当前部署域名烘焙好。
async function downloadExtension() {
  try {
    const resp = await fetch("/api/opencode-go-grabber/extension.zip", { credentials: "same-origin" });
    if (!resp.ok) {
      const text = await resp.text().catch(() => "");
      ElMessage.error(`下载失败 (${resp.status})${text ? "：" + text : ""}`);
      return;
    }
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "opencode-go-grabber.zip";
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    ElMessage.success("已下载扩展，请解压后在 chrome://extensions 加载");
  } catch (e) {
    ElMessage.error(e?.message || "下载失败");
  }
}

// 从浏览器扩展抓取 workspace_id + 登录态 Cookie，并填入 sessionForm（或 createForm）
async function grabFromExtension(target = "session") {
  grabberReady.value = isGrabberReady();
  grabbingFromExt.value = true;
  try {
    const res = await extGrab(5000);
    if (!res.ok) {
      ElMessage.warning(res.message || "扩展未就绪，请确认已安装并启用 OpenCode Go Grabber 扩展");
      return false;
    }
    if (target === "session") {
      if (res.workspaceId) sessionForm.workspace_id = res.workspaceId;
      if (res.cookieHeader) {
        sessionForm.storage_state = res.cookieHeader;
        ElMessage.success(res.hasAuth
          ? `已从扩展抓取（workspace: ${res.workspaceId || "未识别"}，含 auth）`
          : "已抓取 Cookie，但未检测到 auth，请先在 opencode.ai 登录");
      } else {
        ElMessage.warning("未抓到 Cookie，请先在 opencode.ai 触发一次请求");
      }
    } else {
      if (res.workspaceId) form.workspace_id = res.workspaceId;
      ElMessage.success(res.workspaceId ? `已填入 workspace: ${res.workspaceId}` : "未识别到 workspace");
    }
    return !!res.cookieHeader;
  } finally {
    grabbingFromExt.value = false;
  }
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
}

async function deleteAccount(account) {
  await ElMessageBox.confirm(`确定删除 ${account.email} 吗？`, "删除 OpenCode Go 账号", { type: "warning" });
  await api.deleteOpencodeGoAccount(account.id);
  await loadAccounts();
  ElMessage.success("账号已删除");
}

function handleSelectionChange(rows) {
  selectedAccountIds.value = rows.filter(hasApiKey).map((row) => row.id);
}

function handlePageChange(page) {
  pagination.page = page;
  loadAccounts();
}

function handlePageSizeChange(pageSize) {
  pagination.page_size = pageSize;
  pagination.page = 1;
  loadAccounts();
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
  if (refreshingAll.value) {
    return;
  }
  refreshingAll.value = true;
  try {
    await api.refreshAllOpencodeGo();
    await loadAccounts();
    ElMessage.success("OpenCode Go 全部刷新完成");
  } catch (error) {
    ElMessage.error(error.message || "刷新全部失败");
  } finally {
    refreshingAll.value = false;
    resetRefreshCountdown();
  }
}

async function openSub2ApiImport(account) {
  sub2ApiImportAccount.value = account;
  sub2ApiGroups.value = [];
  sub2ApiSelectedGroupIds.value = [];
  sub2ApiImportVisible.value = true;
  sub2ApiImportLoading.value = true;
  try {
    const payload = await api.opencodeGoSub2ApiGroups();
    sub2ApiGroups.value = payload.groups || [];
  } catch (error) {
    ElMessage.error(error.message || "加载 Sub2API 分组失败");
  } finally {
    sub2ApiImportLoading.value = false;
  }
}

function sub2ApiGroupId(group) {
  return Number(group.id ?? group.group_id ?? group.groupId);
}

function sub2ApiGroupLabel(group) {
  return group.name || group.plan_name || group.planName || group.description || `分组 ${sub2ApiGroupId(group)}`;
}

function sub2ApiGroupMeta(group) {
  const parts = [`ID: ${sub2ApiGroupId(group)}`];
  const platform = group.platform || "openai";
  if (platform) {
    parts.push(`平台 ${platform}`);
  }
  if (group.status) {
    parts.push(group.status);
  }
  return parts.join(" · ");
}

async function submitSub2ApiImport() {
  if (!sub2ApiImportAccount.value) {
    return;
  }
  if (!sub2ApiSelectedGroupIds.value.length) {
    ElMessage.error("请选择至少一个 Sub2API 分组");
    return;
  }
  sub2ApiImporting.value = true;
  try {
    const payload = await api.importOpencodeGoToSub2Api(sub2ApiImportAccount.value.id, {
      group_ids: sub2ApiSelectedGroupIds.value
    });
    sub2ApiImportVisible.value = false;
    const modelCount = payload.model_count ?? payload.modelCount ?? (payload.models || []).length;
    ElMessage.success(`已导入 Sub2API，模型 ${modelCount} 个`);
  } catch (error) {
    ElMessage.error(error.message || "导入 Sub2API 失败");
  } finally {
    sub2ApiImporting.value = false;
  }
}

async function importToCpa(account) {
  try {
    await ElMessageBox.confirm(`确定将 ${account.email} 导入 CPA 的 OpenAI 提供商吗？`, "导入 CPA", { type: "warning" });
  } catch {
    return;
  }
  cpaImportingId.value = account.id;
  try {
    const payload = await api.importOpencodeGoToCpa(account.id);
    const modelCount = payload.model_count ?? payload.modelCount ?? (payload.models || []).length;
    ElMessage.success(`已导入 CPA，模型 ${modelCount} 个`);
  } catch (error) {
    ElMessage.error(error.message || "导入 CPA 失败");
  } finally {
    cpaImportingId.value = null;
  }
}

async function bulkImportToCpa() {
  const targets = selectedImportableAccounts.value;
  if (!targets.length) {
    ElMessage.error("请选择至少一个已获取 API key 的账号");
    return;
  }
  try {
    await ElMessageBox.confirm(`确定将已选 ${targets.length} 个账号批量导入 CPA 的 OpenAI 提供商吗？`, "批量导入 CPA", { type: "warning" });
  } catch {
    return;
  }
  cpaBulkImporting.value = true;
  try {
    const payload = await api.bulkImportOpencodeGoToCpa({ account_ids: targets.map((account) => account.id) });
    const count = payload.count || 0;
    const failedCount = payload.failed_count ?? payload.failedCount ?? (payload.failed || []).length;
    selectedAccountIds.value = [];
    if (failedCount > 0) {
      ElMessage.warning(`已导入 CPA ${count} 个，失败 ${failedCount} 个`);
    } else {
      ElMessage.success(`已批量导入 CPA ${count} 个账号`);
    }
  } catch (error) {
    ElMessage.error(error.message || "批量导入 CPA 失败");
  } finally {
    cpaBulkImporting.value = false;
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

async function openImportLogs(page = 1) {
  importLogsVisible.value = true;
  importLogsLoading.value = true;
  try {
    const payload = await api.opencodeGoImportLogs({ page, page_size: importLogsPagination.page_size });
    importLogs.value = payload.logs || [];
    Object.assign(importLogsPagination, payload.pagination || {});
  } catch (error) {
    ElMessage.error(error.message || "加载导入日志失败");
  } finally {
    importLogsLoading.value = false;
  }
}

function importLogLevelType(level) {
  if (level === "error") return "danger";
  if (level === "warning") return "warning";
  return "info";
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

function usagePercentWindow(value) {
  const number = Number(value);
  return {
    usage_percent: Number.isFinite(number) ? number : null,
    reset_in_sec: null
  };
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

function hasApiKey(account) {
  return boolValue(account?.has_api_key ?? account?.hasApiKey);
}

function canSelectForCpa(row) {
  return hasApiKey(row) && !cpaBulkImporting.value;
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

onMounted(async () => {
  await loadOpenCodeSettings();
  await loadAccounts();
  refreshTimer.value = window.setInterval(() => {
    if (refreshingAll.value || monitorPaused.value) {
      return;
    }
    refreshRemaining.value -= 1;
    if (refreshRemaining.value <= 0) {
      refreshAll();
    }
  }, 1000);
});

onBeforeUnmount(() => {
  if (refreshTimer.value) {
    window.clearInterval(refreshTimer.value);
  }
});
</script>

<template>
  <section v-loading="loading">
    <div class="page-head">
      <div>
        <h1>OpenCode Go</h1>
        <p>
          {{ accountCount }} 个账号，最近成功 {{ formatTime(lastSuccessAt) }}。
          自动刷新间隔 {{ queryInterval }} 秒，
          <span>{{ monitorPaused ? "自动监控已暂停" : `下次自动刷新 ${refreshRemaining} 秒` }}</span>
        </p>
      </div>
      <div class="page-actions">
        <el-button :icon="Setting" @click="openSettingsDialog">配置用量 JS</el-button>
        <el-button :icon="Download" @click="downloadExtension">下载浏览器插件</el-button>
        <el-button :icon="Document" @click="openImportLogs()">导入日志</el-button>
        <el-button type="primary" :icon="Plus" @click="openCreate">添加账号</el-button>
        <el-button :icon="Upload" @click="openBulkImport">批量导入</el-button>
        <el-button :icon="Refresh" :loading="refreshingAll" @click="refreshAll">刷新全部</el-button>
      </div>
    </div>

    <div class="opencode-overall-strip">
      <div class="overall-usage-card">
        <div>
          <span>整体5h</span>
          <strong>{{ usageLabel(overallRollingUsage) }}</strong>
        </div>
        <el-progress :percentage="usagePercent(overallRollingUsage)" :stroke-width="8" :color="usageColor(overallRollingUsage)" :show-text="false" />
      </div>
      <div class="overall-usage-card">
        <div>
          <span>整体7d</span>
          <strong>{{ usageLabel(overallWeeklyUsage) }}</strong>
        </div>
        <el-progress :percentage="usagePercent(overallWeeklyUsage)" :stroke-width="8" :color="usageColor(overallWeeklyUsage)" :show-text="false" />
      </div>
      <div class="overall-usage-note">按 {{ eligibleAccountCount }} 个 7d&lt;99% 账号统计</div>
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
        <div class="panel-actions">
          <el-button size="small" :icon="Upload" :loading="cpaBulkImporting" :disabled="!selectedImportCount" @click="bulkImportToCpa">批量导入 CPA</el-button>
          <el-tag>{{ selectedImportCount }} 已选</el-tag>
          <el-tag>{{ pagination.total }} 个账号</el-tag>
        </div>
      </div>
      <template v-if="!isMobile">
        <el-table :data="accounts" border stripe row-key="id" style="width: 100%" @selection-change="handleSelectionChange">
          <el-table-column type="selection" width="48" :selectable="canSelectForCpa" fixed />
          <el-table-column label="Google 邮箱" min-width="220" fixed>
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
          <el-table-column label="导入时间" width="165">
            <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
          </el-table-column>
          <el-table-column label="自动刷新" width="105">
            <template #default="{ row }"><el-switch :model-value="boolValue(row.is_enabled)" @change="toggleEnabled(row)" /></template>
          </el-table-column>
          <el-table-column label="操作" min-width="440" fixed="right">
            <template #default="{ row }">
              <div class="table-actions">
                <el-button size="small" :icon="Edit" @click="openEdit(row)">编辑</el-button>
                <el-button size="small" :icon="Upload" @click="openSessionImport(row)">导入登录态</el-button>
                <el-button size="small" :icon="Upload" :disabled="!hasApiKey(row)" @click="openSub2ApiImport(row)">导入 Sub2API</el-button>
                <el-button size="small" :icon="Upload" :loading="cpaImportingId === row.id" :disabled="!hasApiKey(row) || cpaBulkImporting" @click="importToCpa(row)">导入 CPA</el-button>
                <el-button size="small" :icon="Refresh" :loading="row._refreshing" @click="refreshAccount(row)">刷新</el-button>
                <el-button size="small" :icon="CopyDocument" :disabled="!hasApiKey(row)" @click="copyApiKey(row)">复制 Key</el-button>
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
            <el-checkbox v-model="selectedAccountIds" :value="row.id" :disabled="!hasApiKey(row) || cpaBulkImporting" />
            <div class="mobile-card-title">
              <strong>{{ row.email }}</strong>
              <div class="mobile-card-meta">
                <span>API key: {{ row.api_key_masked || row.apiKeyMasked || "未找到" }}</span>
                <span>导入时间: {{ formatTime(row.created_at) }}</span>
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
            <div class="mobile-field">
              <span>导入时间</span>
              <strong>{{ formatTime(row.created_at) }}</strong>
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
            <el-button size="small" :icon="Upload" :disabled="!hasApiKey(row)" @click="openSub2ApiImport(row)">导入 Sub2API</el-button>
            <el-button size="small" :icon="Upload" :loading="cpaImportingId === row.id" :disabled="!hasApiKey(row) || cpaBulkImporting" @click="importToCpa(row)">导入 CPA</el-button>
            <el-button size="small" :icon="Refresh" :loading="row._refreshing" @click="refreshAccount(row)">刷新</el-button>
            <el-button size="small" :icon="CopyDocument" :disabled="!hasApiKey(row)" @click="copyApiKey(row)">复制 Key</el-button>
            <el-button size="small" :icon="Timer" @click="openHistory(row)">历史</el-button>
            <el-button size="small" type="danger" :icon="Delete" @click="deleteAccount(row)">删除</el-button>
          </div>
        </article>
      </div>
      <div class="table-footer">
        <el-pagination
          background
          layout="total, sizes, prev, pager, next, jumper"
          :current-page="pagination.page"
          :page-size="pagination.page_size"
          :page-sizes="[20, 50, 100, 200]"
          :total="pagination.total"
          @current-change="handlePageChange"
          @size-change="handlePageSizeChange"
        />
      </div>
    </div>

    <el-dialog v-model="dialogVisible" :title="dialogMode === 'create' ? '添加 OpenCode Go 账号' : '编辑 OpenCode Go 账号'" width="560px">
      <el-form ref="formRef" :model="form" :rules="rules" label-position="top" @submit.prevent="submitAccount">
        <el-form-item label="Google 邮箱" prop="email">
          <el-input v-model="form.email" autocomplete="username" />
        </el-form-item>
        <el-form-item label="Google 密码" prop="password">
          <el-input v-model="form.password" type="password" show-password autocomplete="current-password" :placeholder="dialogMode === 'edit' ? '留空表示不修改' : ''" />
        </el-form-item>
        <el-form-item label="恢复电子邮件">
          <el-input v-model="form.recovery_email" autocomplete="email" />
        </el-form-item>
        <el-form-item label="Workspace ID">
          <el-input v-model="form.workspace_id" placeholder="可留空，登录后自动识别">
            <template #append>
              <el-button :icon="Download" :loading="grabbingFromExt" @click="grabFromExtension('create')">从插件抓取</el-button>
            </template>
          </el-input>
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
            placeholder="user1@example.com|password1&#10;user2@example.com|password2|recover@example.com"
            autocomplete="off"
          />
        </el-form-item>
        <div class="import-helper">
          <span>格式：账号|密码 或 账号|密码|恢复电子邮件，一行一条</span>
          <span>账号会作为 Google 邮箱使用，空行会跳过，重复账号会更新原账号</span>
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

    <el-dialog v-model="sub2ApiImportVisible" title="导入 Sub2API" width="680px">
      <el-form label-position="top" @submit.prevent="submitSub2ApiImport">
        <el-form-item label="导入账号">
          <el-input :model-value="sub2ApiImportName" readonly />
        </el-form-item>
        <el-form-item label="Sub2API 分组">
          <div v-loading="sub2ApiImportLoading" class="sub2api-group-picker">
            <el-empty v-if="!sub2ApiImportLoading && !sub2ApiGroups.length" description="暂无可选 OpenAI 分组" />
            <el-checkbox-group v-else v-model="sub2ApiSelectedGroupIds" class="sub2api-group-list">
              <el-checkbox v-for="group in sub2ApiGroups" :key="sub2ApiGroupId(group)" :value="sub2ApiGroupId(group)" border class="sub2api-group-option">
                <div class="sub2api-group-copy">
                  <span>{{ sub2ApiGroupLabel(group) }}</span>
                  <small>{{ sub2ApiGroupMeta(group) }}</small>
                </div>
              </el-checkbox>
            </el-checkbox-group>
          </div>
        </el-form-item>
        <div class="import-helper">
          <span>导入会在已配置的 Sub2API 站点创建 OpenAI APIkey 账号，Base URL 固定为 https://opencode.ai/zen/go。</span>
          <span>创建前会同步上游模型列表，开启池模式，并强制关闭 Codex 图片生成桥接。</span>
        </div>
      </el-form>
      <template #footer>
        <div class="dialog-footer">
          <el-button @click="sub2ApiImportVisible = false">取消</el-button>
          <el-button type="primary" :loading="sub2ApiImporting" :disabled="sub2ApiImportLoading || !sub2ApiSelectedGroupIds.length" @click="submitSub2ApiImport">导入</el-button>
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
          <el-button type="primary" :icon="Link" @click="openLocalOpencodeLogin">打开 OpenCode Zen</el-button>
          <el-button type="success" :icon="Download" :loading="grabbingFromExt" @click="grabFromExtension('session')">从浏览器插件抓取</el-button>
          <span>已安装插件时可一键填入 workspace 与 auth Cookie；否则打开 OpenCode Zen 登录后复制登录态再粘贴到下方。</span>
        </div>
        <el-form-item label="Workspace ID（必填）">
          <el-input v-model="sessionForm.workspace_id" placeholder="wrk_01KW01D1MG4VHNMJWA2KSH83CQ" />
        </el-form-item>
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
          <span>如果 auth Cookie 勾选了 HttpOnly，控制台命令读不到它；请从 DevTools 的 Application > Cookies 复制 auth 的值，粘贴为 auth=你的值。</span>
          <span>导入后系统会加密保存登录态，后续刷新优先复用该会话。</span>
        </div>
      </el-form>
      <template #footer>
        <div class="dialog-footer">
          <el-button :icon="Link" @click="openLocalOpencodeLogin">打开 OpenCode Zen</el-button>
          <el-button @click="sessionDialogVisible = false">取消</el-button>
          <el-button type="primary" :loading="importingSession" @click="submitSessionImport">导入</el-button>
        </div>
      </template>
    </el-dialog>

    <el-dialog v-model="historyVisible" title="OpenCode Go 历史" width="900px">
      <p class="muted">{{ historyAccount?.email }}</p>
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

    <el-dialog v-model="importLogsVisible" title="OpenCode Go 导入日志" width="960px">
      <template v-if="!isMobile">
        <el-table v-loading="importLogsLoading" :data="importLogs" border stripe row-key="id" style="width: 100%">
          <el-table-column label="时间" width="180">
            <template #default="{ row }">{{ row.created_at_formatted || formatTime(row.created_at) }}</template>
          </el-table-column>
          <el-table-column label="级别" width="100">
            <template #default="{ row }"><el-tag :type="importLogLevelType(row.level)">{{ row.level }}</el-tag></template>
          </el-table-column>
          <el-table-column label="内容" min-width="520">
            <template #default="{ row }"><span class="note-text">{{ row.message }}</span></template>
          </el-table-column>
        </el-table>
      </template>
      <div v-else v-loading="importLogsLoading" class="mobile-stack">
        <article v-for="row in importLogs" :key="row.id" class="mobile-card">
          <div class="mobile-card-head">
            <div class="mobile-card-title">
              <strong>{{ row.created_at_formatted || formatTime(row.created_at) }}</strong>
            </div>
            <el-tag :type="importLogLevelType(row.level)">{{ row.level }}</el-tag>
          </div>
          <div class="mobile-field">
            <span>内容</span>
            <strong class="note-text">{{ row.message }}</strong>
          </div>
        </article>
      </div>
      <div class="table-footer">
        <el-pagination
          background
          layout="prev, pager, next, total"
          :current-page="importLogsPagination.page"
          :page-size="importLogsPagination.page_size"
          :total="importLogsPagination.total"
          @current-change="openImportLogs"
        />
      </div>
      <template #footer>
        <div class="dialog-footer">
          <el-button :icon="Refresh" :loading="importLogsLoading" @click="openImportLogs(importLogsPagination.page)">刷新</el-button>
          <el-button @click="importLogsVisible = false">关闭</el-button>
        </div>
      </template>
    </el-dialog>
  </section>
</template>

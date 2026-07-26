<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { ArrowDown, ArrowRight, Check, CircleClose, Hide, Refresh, RefreshLeft, Search, Timer, VideoPlay } from "@element-plus/icons-vue";
import { api } from "../api";
import { formatTime } from "../utils";

const PLATFORM_FILTER_OPTIONS = [
  { value: "anthropic", label: "Anthropic" },
  { value: "openai", label: "OpenAI" },
  { value: "gemini", label: "Gemini" },
  { value: "antigravity", label: "Antigravity" },
  { value: "grok", label: "Grok" }
];
const ACCOUNT_TYPE_FILTER_OPTIONS = [
  { value: "oauth", label: "OAuth" },
  { value: "setup-token", label: "Setup Token" },
  { value: "apikey", label: "API Key" },
  { value: "upstream", label: "上游透传" },
  { value: "bedrock", label: "AWS Bedrock" },
  { value: "service_account", label: "Google Service Account" }
];
const STATUS_FILTER_OPTIONS = [
  { value: "active", label: "正常" },
  { value: "inactive", label: "停用" },
  { value: "error", label: "错误" },
  { value: "rate_limited", label: "限流中" },
  { value: "temp_unschedulable", label: "临时不可调度" },
  { value: "unschedulable", label: "不可调度" }
];

const loading = ref(false);
const loaded = ref(false);
const startingJob = ref(false);
const syncDialog = ref(false);
const syncDialogMode = ref("form");
const hasCache = ref(false);
const accounts = ref([]);
const groups = ref([]);
const excludedGroups = ref([]);
const warnings = ref([]);
const siteUrl = ref("");
const refreshedAt = ref("");
const activitiesRefreshedAt = ref("");
const job = ref(null);
const collapsedGroups = ref(new Set());
const updatingIds = ref(new Set());
const updatingGroupIds = ref(new Set());
const filters = reactive({
  search: "",
  platform: "",
  type: "",
  status: "",
  group_id: ""
});
const appliedRefreshFilter = reactive({ platform: "", type: "", status: "", include_ungrouped: true });
const refreshForm = reactive({ platform: "", type: "", status: "", include_ungrouped: true });
const policyLoading = ref(false);
const policySaving = ref(false);
const policyRunning = ref(false);
const policyRuntime = ref({});
const policyActions = ref([]);
const excludedAccountText = ref("1430, 1431");
const policyConfig = reactive({
  enabled: false,
  return_pool_enabled: false,
  smart_expand_enabled: false,
  load_factor_enabled: false,
  price_protection_enabled: false,
  probe_interval_seconds: 60,
  health_threshold: 75,
  evidence_ttl_multiplier: 3,
  minimum_available_accounts: 1,
  healthy_target_accounts: 3,
  total_concurrency: 900,
  account_min_concurrency: 20,
  account_max_concurrency: 250,
  expand_trigger_percent: 80,
  expand_step_percent: 10,
  load_factor_total: 400,
  account_min_load_factor: 20,
  account_max_load_factor: 500,
  rate_weight_exponent: 1,
  load_change_threshold_percent: 10,
  load_change_cooldown_seconds: 60,
  failure_window: 5,
  failure_threshold: 3,
  failure_health_threshold: 60,
  slow_window: 10,
  slow_first_token_ms: 15000,
  slow_threshold: 5,
  excluded_account_ids: [1430, 1431]
});
let jobPollTimer = null;
let disposed = false;

const jobActive = computed(() => isActiveJobStatus(job.value?.status));
const jobFailed = computed(() => job.value?.status === "failed");
const controlsDisabled = computed(() => loading.value || startingJob.value || jobActive.value);
const jobPercent = computed(() => {
  const direct = Number(job.value?.percent);
  if (Number.isFinite(direct)) return Math.min(100, Math.max(0, direct));
  const processed = Number(job.value?.processed);
  const total = Number(job.value?.total);
  if (Number.isFinite(processed) && Number.isFinite(total) && total > 0) {
    return Math.min(100, Math.max(0, Math.round((processed / total) * 100)));
  }
  return 0;
});
const jobTitle = computed(() => job.value?.kind === "activity_refresh" ? "刷新运行情况" : "同步账号");
const jobProgressText = computed(() => {
  if (!job.value) return "";
  if (job.value.message) return job.value.message;
  if (job.value.kind === "accounts_sync" && job.value.totalPages) {
    return `正在获取第 ${job.value.currentPage || 0} / ${job.value.totalPages} 页`;
  }
  if (job.value.total) return `已处理 ${job.value.processed || 0} / ${job.value.total} 个账号`;
  return jobActive.value ? "任务正在准备" : "任务已结束";
});

const platformOptions = computed(() => {
  return mergeFilterOptions(PLATFORM_FILTER_OPTIONS, accounts.value.map((account) => account.platform), appliedRefreshFilter.platform);
});

const typeOptions = computed(() => {
  return mergeFilterOptions(ACCOUNT_TYPE_FILTER_OPTIONS, accounts.value.map((account) => account.type), appliedRefreshFilter.type);
});

const groupOptions = computed(() => {
  return [...groups.value].sort(compareGroups);
});

const filteredAccounts = computed(() => {
  const search = filters.search.trim().toLowerCase();
  return accounts.value.filter((account) => {
    if (search) {
      const searchable = [account.id, account.name, account.notes, account.platform, account.type]
        .map((value) => String(value || "").toLowerCase())
        .join(" ");
      if (!searchable.includes(search)) return false;
    }
    if (filters.platform && account.platform !== filters.platform) return false;
    if (filters.type && account.type !== filters.type) return false;
    if (filters.status && accountFilterStatus(account) !== filters.status) return false;
    const groupIds = accountGroupIds(account);
    if (filters.group_id === "ungrouped" && groupIds.length) return false;
    if (filters.group_id && filters.group_id !== "ungrouped" && !groupIds.includes(Number(filters.group_id))) return false;
    return true;
  });
});

const groupSections = computed(() => {
  const knownGroups = new Map(groups.value.map((group) => [Number(group.id), group]));
  const allGroupIds = new Set();
  filteredAccounts.value.forEach((account) => accountGroupIds(account).forEach((id) => allGroupIds.add(id)));
  const sections = [];

  allGroupIds.forEach((id) => {
    const group = knownGroups.get(id) || { id, name: `分组 ${id}`, platform: "", status: "" };
    sections.push({
      key: `group-${id}`,
      group,
      accounts: filteredAccounts.value.filter((account) => accountGroupIds(account).includes(id))
    });
  });

  const ungrouped = filteredAccounts.value.filter((account) => accountGroupIds(account).length === 0);
  if (ungrouped.length) {
    sections.push({
      key: "ungrouped",
      group: { id: null, name: "未分组", platform: "", status: "" },
      accounts: ungrouped
    });
  }
  return sections.sort((left, right) => compareGroups(left.group, right.group));
});

const activeCount = computed(() => accounts.value.filter((account) => account.status === "active").length);
const errorCount = computed(() => accounts.value.filter((account) => account.status === "error").length);
const policySummary = computed(() => policyRuntime.value?.summary || {});
const policyStatusText = computed(() => {
  if (!policyConfig.enabled) return "仅评分";
  if (policyRuntime.value?.status === "running") return "执行中";
  if (policyRuntime.value?.status === "failed") return "异常";
  return "已接管";
});

async function loadPolicy() {
  policyLoading.value = true;
  try {
    applyPolicyPayload(await api.platformDispatchPolicy());
  } catch (error) {
    ElMessage.error(error.message || "加载自动调度策略失败");
  } finally {
    policyLoading.value = false;
  }
}

function applyPolicyPayload(payload) {
  Object.assign(policyConfig, payload.config || {});
  policyRuntime.value = payload.runtime || {};
  policyActions.value = payload.actions || [];
  excludedAccountText.value = (policyConfig.excluded_account_ids || []).join(", ");
  const states = new Map((payload.accounts || []).map((item) => [Number(item.account_id), item]));
  accounts.value.forEach((account) => {
    const state = states.get(Number(account.id));
    if (!state) return;
    Object.assign(account, {
      health_score: state.health_score,
      health_short_score: state.short_score,
      health_long_score: state.long_score,
      health_evidence_at: state.evidence_at,
      health_evidence_fresh: Boolean(state.evidence_fresh),
      decision_reason: state.decision_reason || "",
      target_concurrency: state.target_concurrency,
      target_load_factor: state.target_load_factor,
      last_policy_action_at: state.last_action_at
    });
  });
}

function parseExcludedAccountIds() {
  const values = excludedAccountText.value.split(/[，,\s]+/).filter(Boolean).map(Number);
  if (values.some((value) => !Number.isInteger(value) || value <= 0)) throw new Error("排除账号 ID 必须是正整数");
  return [...new Set(values)];
}

async function savePolicy() {
  policySaving.value = true;
  try {
    const payload = await api.savePlatformDispatchPolicy({
      ...policyConfig,
      excluded_account_ids: parseExcludedAccountIds()
    });
    applyPolicyPayload(payload);
    ElMessage.success("自动调度策略已保存");
  } catch (error) {
    ElMessage.error(error.message || "保存自动调度策略失败");
  } finally {
    policySaving.value = false;
  }
}

async function runPolicyNow() {
  policyRunning.value = true;
  try {
    const payload = await api.runPlatformDispatchPolicy();
    applyPolicyPayload(payload);
    await loadDispatch();
    ElMessage.success(payload.summary?.status_action || payload.summary?.message || "自动调度轮次已完成");
  } catch (error) {
    ElMessage.error(error.message || "执行自动调度失败");
  } finally {
    policyRunning.value = false;
  }
}

async function loadDispatch() {
  loading.value = true;
  try {
    const payload = await api.platformDispatch();
    applyDispatchPayload(payload);
  } catch (error) {
    ElMessage.error(error.message || "加载平台调度缓存失败");
  } finally {
    loading.value = false;
  }
}

function applyDispatchPayload(payload) {
  accounts.value = (payload.accounts || []).map((account) => ({
    ...account,
    is_enabled: account.is_enabled ?? account.isEnabled ?? account.status === "active",
    recent_activity: account.recent_activity || account.recentActivity || []
  }));
  groups.value = payload.groups || [];
  excludedGroups.value = payload.excluded_groups || payload.excludedGroups || [];
  warnings.value = payload.warnings || [];
  siteUrl.value = payload.site_url || payload.siteUrl || "";
  refreshedAt.value = payload.refreshed_at || payload.refreshedAt || "";
  activitiesRefreshedAt.value = payload.activities_refreshed_at || payload.activitiesRefreshedAt || "";
  hasCache.value = payload.has_cache ?? payload.hasCache ?? false;
  const refreshFilter = payload.refresh_filter || payload.refreshFilter || {};
  Object.assign(appliedRefreshFilter, {
    platform: refreshFilter.platform || "",
    type: refreshFilter.type || "",
    status: refreshFilter.status || "",
    include_ungrouped: refreshFilter.include_ungrouped ?? refreshFilter.includeUngrouped ?? true
  });
  loaded.value = true;
}

function openRefreshDialog() {
  Object.assign(refreshForm, appliedRefreshFilter);
  syncDialogMode.value = "form";
  syncDialog.value = true;
}

async function startAccountSync() {
  startingJob.value = true;
  try {
    const payload = await api.syncPlatformDispatch({ ...refreshForm });
    if (!setJobFromPayload(payload)) throw new Error("服务器未返回任务信息");
    syncDialogMode.value = "progress";
    if (jobActive.value) scheduleJobPoll();
    else await finishStartedJob();
  } catch (error) {
    ElMessage.error(error.message || "启动账号同步失败");
  } finally {
    startingJob.value = false;
  }
}

async function startActivityRefresh() {
  try {
    await ElMessageBox.confirm(
      `将为全部 ${accounts.value.length} 个已同步账号获取最近 6 条运行情况。`,
      "刷新运行情况",
      { confirmButtonText: "开始刷新", cancelButtonText: "取消", type: "info" }
    );
  } catch {
    return;
  }

  startingJob.value = true;
  try {
    const payload = await api.refreshPlatformDispatchActivities();
    if (!setJobFromPayload(payload)) throw new Error("服务器未返回任务信息");
    if (jobActive.value) scheduleJobPoll();
    else await finishStartedJob();
  } catch (error) {
    ElMessage.error(error.message || "启动运行情况刷新失败");
  } finally {
    startingJob.value = false;
  }
}

async function excludeGroup(group) {
  const groupId = excludedGroupId(group);
  if (!groupId) return;
  try {
    await ElMessageBox.confirm(
      `排除后，“${group.name || `分组 ${groupId}`}”及其独占账号会从缓存移除，多分组账号仍保留在其他分组。`,
      "排除分组",
      { confirmButtonText: "确认排除", cancelButtonText: "取消", type: "warning" }
    );
  } catch {
    return;
  }

  setGroupUpdating(groupId, true);
  try {
    const payload = await api.excludePlatformDispatchGroup(groupId);
    applyDispatchPayload(payload);
    ElMessage.success(`已排除分组“${group.name || groupId}”`);
  } catch (error) {
    ElMessage.error(error.message || "排除分组失败");
  } finally {
    setGroupUpdating(groupId, false);
  }
}

async function restoreExcludedGroup(group) {
  const groupId = excludedGroupId(group);
  if (!groupId) return;
  setGroupUpdating(groupId, true);
  try {
    const payload = await api.restorePlatformDispatchGroup(groupId);
    applyDispatchPayload(payload);
    ElMessage.success("已取消排除，请重新同步账号以加载该分组数据");
  } catch (error) {
    ElMessage.error(error.message || "取消排除分组失败");
  } finally {
    setGroupUpdating(groupId, false);
  }
}

function excludedGroupId(group) {
  const value = Number(group?.id ?? group?.group_id ?? group?.groupId);
  return Number.isInteger(value) && value > 0 ? value : 0;
}

function excludedGroupName(group) {
  const id = excludedGroupId(group);
  return group?.name || group?.group_name || group?.groupName || `分组 ${id}`;
}

function excludedGroupPlatform(group) {
  return group?.platform || group?.group_platform || group?.groupPlatform || "";
}

function setGroupUpdating(groupId, updating) {
  const next = new Set(updatingGroupIds.value);
  if (updating) next.add(groupId);
  else next.delete(groupId);
  updatingGroupIds.value = next;
}

function isUpdatingGroup(group) {
  return updatingGroupIds.value.has(excludedGroupId(group));
}

function normalizeJob(value) {
  if (!value || typeof value !== "object") return null;
  return {
    ...value,
    id: value.job_id || value.jobId || value.id || "",
    kind: value.kind || value.job_kind || value.jobKind || "",
    status: value.status || "",
    phase: value.phase || "",
    currentPage: value.current_page ?? value.currentPage ?? 0,
    totalPages: value.total_pages ?? value.totalPages ?? 0,
    processed: value.processed ?? 0,
    total: value.total ?? 0,
    percent: value.percent,
    message: value.message || "",
    error: value.error || "",
    filter: value.filter || value.filters || value.refresh_filter || value.refreshFilter || {}
  };
}

function setJobFromPayload(payload) {
  job.value = normalizeJob(payload?.job ?? null);
  return job.value;
}

function isActiveJobStatus(status) {
  return ["queued", "pending", "running"].includes(status);
}

function isSuccessfulJobStatus(status) {
  return ["completed", "succeeded", "success"].includes(status);
}

function stopJobPolling() {
  if (jobPollTimer !== null) {
    window.clearTimeout(jobPollTimer);
    jobPollTimer = null;
  }
}

function scheduleJobPoll() {
  stopJobPolling();
  if (!disposed && jobActive.value) jobPollTimer = window.setTimeout(pollJob, 1000);
}

async function finishStartedJob() {
  await loadDispatch();
  if (isSuccessfulJobStatus(job.value?.status)) {
    ElMessage.success(job.value?.kind === "activity_refresh" ? "运行情况刷新完成" : `账号同步完成，共 ${accounts.value.length} 个账号`);
  } else if (jobFailed.value) {
    ElMessage.error(job.value.error || "平台调度任务失败");
  }
}

async function pollJob() {
  jobPollTimer = null;
  const wasActive = jobActive.value;
  try {
    const payload = await api.platformDispatchJob();
    setJobFromPayload(payload);
    if (jobActive.value) {
      scheduleJobPoll();
      return;
    }
    if (wasActive) {
      await finishStartedJob();
    }
  } catch (error) {
    if (wasActive) {
      scheduleJobPoll();
    } else if (error.status !== 404) {
      ElMessage.error(error.message || "读取平台调度任务状态失败");
    }
  }
}

async function resumeJob() {
  try {
    const payload = await api.platformDispatchJob();
    setJobFromPayload(payload);
    if (jobActive.value) {
      if (job.value.kind === "accounts_sync") {
        const savedFilter = job.value.filter || {};
        Object.assign(refreshForm, {
          platform: savedFilter.platform || "",
          type: savedFilter.type || "",
          status: savedFilter.status || "",
          include_ungrouped: savedFilter.include_ungrouped ?? savedFilter.includeUngrouped ?? true
        });
        syncDialogMode.value = "progress";
        syncDialog.value = true;
      }
      scheduleJobPoll();
    }
  } catch (error) {
    if (error.status !== 404) ElMessage.error(error.message || "读取平台调度任务状态失败");
  }
}

async function toggleAccount(account, enabled) {
  updatingIds.value = new Set([...updatingIds.value, account.id]);
  try {
    const payload = await api.setPlatformDispatchEnabled(account.id, enabled);
    const updated = payload.account || {};
    account.status = updated.status || (enabled ? "active" : "inactive");
    account.is_enabled = updated.is_enabled ?? updated.isEnabled ?? enabled;
    account.error_message = updated.error_message || updated.errorMessage || "";
    ElMessage.success(`${account.name} 已${enabled ? "启用" : "停用"}`);
  } catch (error) {
    account.is_enabled = !enabled;
    ElMessage.error(error.message || "更新账号状态失败");
  } finally {
    const next = new Set(updatingIds.value);
    next.delete(account.id);
    updatingIds.value = next;
  }
}

function resetFilters() {
  Object.assign(filters, { search: "", platform: "", type: "", status: "", group_id: "" });
}

function mergeFilterOptions(knownOptions, values, selected = "") {
  const options = knownOptions.map((option) => ({ ...option }));
  const knownValues = new Set(options.map((option) => option.value));
  const extras = [...values, selected]
    .map((value) => String(value || "").trim())
    .filter((value) => value && !knownValues.has(value));
  [...new Set(extras)].sort().forEach((value) => options.push({ value, label: value }));
  return options;
}

function accountGroupIds(account) {
  const values = account.group_ids || account.groupIds || [];
  return Array.isArray(values) ? values.map(Number).filter((value) => Number.isInteger(value) && value > 0) : [];
}

function compareGroups(left, right) {
  const platformCompare = String(left.platform || "").localeCompare(String(right.platform || ""), "zh-CN");
  if (platformCompare) return platformCompare;
  return String(left.name || "").localeCompare(String(right.name || ""), "zh-CN");
}

function statusType(status) {
  if (status === "active") return "success";
  if (status === "error") return "danger";
  if (status === "rate_limited" || status === "temp_unschedulable") return "warning";
  return "info";
}

function groupOAuthCount(accounts) {
  return accounts.filter((account) => String(account.type || "").trim().toLowerCase() === "oauth").length;
}

function statusText(status) {
  return STATUS_FILTER_OPTIONS.find((option) => option.value === status)?.label || status || "-";
}

function accountFilterStatus(account) {
  return account.filter_status || account.filterStatus || account.status || "inactive";
}

function userText(activity) {
  const email = activity.user_email || activity.userEmail || "未知用户";
  const id = activity.user_id || activity.userId;
  return id ? `${email} #${id}` : email;
}

function tokenText(activity) {
  const value = activity.total_tokens ?? activity.totalTokens;
  return value === null || value === undefined ? "-" : Number(value).toLocaleString("zh-CN");
}

function tokenDetail(activity) {
  const input = activity.input_tokens ?? activity.inputTokens;
  if (input === null || input === undefined) return "无 token 数据";
  const output = activity.output_tokens ?? activity.outputTokens ?? 0;
  const cache = activity.cache_tokens ?? activity.cacheTokens ?? 0;
  return `输入 ${Number(input).toLocaleString("zh-CN")} / 输出 ${Number(output).toLocaleString("zh-CN")} / 缓存 ${Number(cache).toLocaleString("zh-CN")}`;
}

function costText(activity) {
  const value = activity.cost ?? activity.actual_cost ?? activity.actualCost;
  return value === null || value === undefined ? "-" : `$${Number(value).toFixed(6)}`;
}

function durationText(activity) {
  const first = activity.first_token_ms ?? activity.firstTokenMs;
  const total = activity.duration_ms ?? activity.durationMs;
  return `${formatDuration(first)} / ${formatDuration(total)}`;
}

function formatDuration(value) {
  if (value === null || value === undefined || value === "") return "-";
  const milliseconds = Number(value);
  if (!Number.isFinite(milliseconds)) return "-";
  if (milliseconds < 1000) return `${Math.round(milliseconds)} ms`;
  return `${(milliseconds / 1000).toFixed(milliseconds < 10000 ? 2 : 1)} s`;
}

function activityTime(activity) {
  return formatTime(activity.created_at || activity.createdAt);
}

function isUpdating(accountId) {
  return updatingIds.value.has(accountId);
}

function healthType(score) {
  const value = Number(score);
  if (!Number.isFinite(value)) return "info";
  if (value >= Number(policyConfig.health_threshold)) return "success";
  if (value >= Number(policyConfig.failure_health_threshold)) return "warning";
  return "danger";
}

function healthText(account) {
  const value = Number(account.health_score ?? account.healthScore);
  return Number.isFinite(value) ? value.toFixed(1) : "待采集";
}

function evidenceText(account) {
  const at = account.health_evidence_at || account.healthEvidenceAt;
  if (!at) return "无证据";
  return `${account.health_evidence_fresh ?? account.healthEvidenceFresh ? "有效" : "已过期"} · ${formatTime(at)}`;
}

function metricText(value) {
  return value === null || value === undefined || value === "" ? "-" : Number(value).toLocaleString("zh-CN");
}

function actionText(action) {
  const name = action.account_name || (action.account_id ? `账号 #${action.account_id}` : "系统");
  const result = action.status === "failed" ? `失败：${action.error || action.reason}` : action.reason;
  return `${name} · ${result || action.action}`;
}

function isCollapsed(key) {
  return collapsedGroups.value.has(key);
}

function toggleCollapsed(key) {
  const next = new Set(collapsedGroups.value);
  if (next.has(key)) next.delete(key);
  else next.add(key);
  collapsedGroups.value = next;
}

onMounted(async () => {
  disposed = false;
  await loadDispatch();
  await loadPolicy();
  resumeJob();
});

onBeforeUnmount(() => {
  disposed = true;
  stopJobPolling();
});
</script>

<template>
  <section v-loading="loading" class="dispatch-page">
    <div class="page-head">
      <div>
        <h1>平台调度</h1>
        <p>Sub2API 账号与最近调度记录<span v-if="siteUrl"> · {{ siteUrl }}</span></p>
      </div>
      <div class="page-actions">
        <el-button :icon="Refresh" :disabled="controlsDisabled" @click="openRefreshDialog">同步账号</el-button>
        <el-button
          :icon="Timer"
          :disabled="controlsDisabled || !hasCache || accounts.length === 0"
          @click="startActivityRefresh"
        >
          刷新运行情况
        </el-button>
      </div>
    </div>

    <section v-loading="policyLoading" class="policy-panel">
      <header class="policy-head">
        <div class="policy-master">
          <el-switch v-model="policyConfig.enabled" size="large" aria-label="自动调度总开关" />
          <div>
            <div class="policy-title-line">
              <h2>自动调度</h2>
              <el-tag :type="policyConfig.enabled ? (policyRuntime.status === 'failed' ? 'danger' : 'success') : 'info'" size="small">
                {{ policyStatusText }}
              </el-tag>
            </div>
            <p>{{ policyConfig.enabled ? "异常停用和最低池保护始终生效" : "仅读取请求记录并计算健康评分" }}</p>
          </div>
        </div>
        <div class="policy-actions">
          <el-button :loading="policySaving" :disabled="controlsDisabled || policyRunning" @click="savePolicy">保存策略</el-button>
          <el-tooltip content="立即执行一轮">
            <el-button
              circle
              type="primary"
              :icon="VideoPlay"
              :loading="policyRunning"
              :disabled="controlsDisabled || policySaving"
              aria-label="立即执行一轮"
              @click="runPolicyNow"
            />
          </el-tooltip>
        </div>
      </header>

      <div class="policy-behavior" :class="{ 'is-active': policyConfig.enabled }">
        <strong>当前行为</strong>
        <span v-if="!policyConfig.enabled">总开关关闭：不探活、不写远端；四项策略保留现有远端值。</span>
        <span v-else-if="!policyConfig.return_pool_enabled && !policyConfig.smart_expand_enabled && !policyConfig.load_factor_enabled && !policyConfig.price_protection_enabled">
          四项策略均关闭；仍会自动停用异常账号，并在可用池低于 {{ policyConfig.minimum_available_accounts }} 个时回池。
        </span>
        <span v-else>每 60 秒评估托管账号；每轮最多切换 1 个账号状态，其余策略独立执行。</span>
      </div>

      <div class="policy-strategies">
        <label class="policy-strategy">
          <el-switch v-model="policyConfig.return_pool_enabled" />
          <span><strong>健康回池</strong><small>将可用池恢复到 {{ policyConfig.healthy_target_accounts }} 个</small></span>
        </label>
        <label class="policy-strategy">
          <el-switch v-model="policyConfig.smart_expand_enabled" />
          <span><strong>智能扩容</strong><small>高负载时按 10% 增加并发</small></span>
        </label>
        <label class="policy-strategy">
          <el-switch v-model="policyConfig.load_factor_enabled" />
          <span><strong>负载因子</strong><small>按健康分与成本倍率分配水位</small></span>
        </label>
        <label class="policy-strategy">
          <el-switch v-model="policyConfig.price_protection_enabled" />
          <span><strong>价格保护</strong><small>按最低有效分组倍率限制权重</small></span>
        </label>
      </div>

      <div class="policy-runtime">
        <div><span>托管账号</span><strong>{{ policySummary.managed_accounts ?? accounts.length }}</strong></div>
        <div><span>可用池</span><strong>{{ policySummary.available_accounts ?? "-" }} / {{ policyConfig.healthy_target_accounts }}</strong></div>
        <div><span>实时并发</span><strong>{{ metricText(policySummary.current_concurrency) }} / {{ metricText(policySummary.capacity) }}</strong></div>
        <div><span>最近轮次</span><strong>{{ policyRuntime.last_finished_at ? formatTime(policyRuntime.last_finished_at) : "尚未执行" }}</strong></div>
      </div>

      <details class="policy-settings">
        <summary>规则参数</summary>
        <div class="policy-setting-groups">
          <section>
            <h3>池与健康</h3>
            <div class="policy-input-grid">
              <label><span>健康门槛</span><el-input-number v-model="policyConfig.health_threshold" :min="0" :max="100" /></label>
              <label><span>最低可用账号</span><el-input-number v-model="policyConfig.minimum_available_accounts" :min="1" /></label>
              <label><span>健康目标账号</span><el-input-number v-model="policyConfig.healthy_target_accounts" :min="1" /></label>
              <label><span>证据有效倍数</span><el-input-number v-model="policyConfig.evidence_ttl_multiplier" :min="1" /></label>
            </div>
          </section>
          <section>
            <h3>智能扩容</h3>
            <div class="policy-input-grid">
              <label><span>总并发</span><el-input-number v-model="policyConfig.total_concurrency" :min="1" /></label>
              <label><span>账号下限</span><el-input-number v-model="policyConfig.account_min_concurrency" :min="1" /></label>
              <label><span>扩容上限</span><el-input-number v-model="policyConfig.account_max_concurrency" :min="1" /></label>
              <label><span>触发负载 %</span><el-input-number v-model="policyConfig.expand_trigger_percent" :min="0" :max="100" /></label>
            </div>
          </section>
          <section>
            <h3>负载因子</h3>
            <div class="policy-input-grid">
              <label><span>总点数</span><el-input-number v-model="policyConfig.load_factor_total" :min="1" /></label>
              <label><span>账号下限</span><el-input-number v-model="policyConfig.account_min_load_factor" :min="1" /></label>
              <label><span>账号上限</span><el-input-number v-model="policyConfig.account_max_load_factor" :min="1" /></label>
              <label><span>写入死区 %</span><el-input-number v-model="policyConfig.load_change_threshold_percent" :min="0" :max="100" /></label>
            </div>
          </section>
          <section>
            <h3>异常判定</h3>
            <div class="policy-input-grid">
              <label><span>异常窗口</span><el-input-number v-model="policyConfig.failure_window" :min="1" /></label>
              <label><span>异常次数</span><el-input-number v-model="policyConfig.failure_threshold" :min="1" /></label>
              <label><span>慢首字窗口</span><el-input-number v-model="policyConfig.slow_window" :min="1" /></label>
              <label><span>慢首字次数</span><el-input-number v-model="policyConfig.slow_threshold" :min="1" /></label>
            </div>
          </section>
        </div>
        <label class="policy-excluded"><span>排除账号 ID</span><el-input v-model="excludedAccountText" placeholder="1430, 1431" /></label>
      </details>

      <div class="policy-rules">
        <p><strong>默认生效</strong><span>认证、余额和用量上限立即停用；3/5 异常且评分低于 60，或 5/10 首字超过 15 秒时停用。</span></p>
        <p><strong>系统计算</strong><span>短期为最新证据与前 9 次均值各 50%，最终评分为短期 70% + 最近 60 次均值 30%。</span></p>
      </div>

      <div v-if="policyActions.length" class="policy-recent-actions">
        <strong>最近动作</strong>
        <span v-for="action in policyActions.slice(0, 3)" :key="action.id" :class="{ 'is-error': action.status === 'failed' }">
          {{ actionText(action) }} · {{ formatTime(action.created_at) }}
        </span>
      </div>
      <el-alert v-if="policyRuntime.last_error" :title="policyRuntime.last_error" type="error" :closable="false" show-icon />
    </section>

    <el-alert
      v-for="warning in warnings"
      :key="warning"
      :title="warning"
      type="warning"
      :closable="false"
      show-icon
      class="dispatch-alert"
    />

    <div v-if="job && (jobActive || jobFailed)" class="dispatch-job" :class="{ 'is-error': jobFailed }">
      <div class="dispatch-job-head">
        <div>
          <strong>{{ jobTitle }}</strong>
          <el-tag v-if="jobActive" size="small" type="primary" effect="plain">运行中</el-tag>
          <el-tag v-else size="small" type="danger" effect="plain">失败</el-tag>
        </div>
        <span>{{ jobProgressText }}</span>
      </div>
      <el-progress
        :percentage="jobPercent"
        :status="jobFailed ? 'exception' : undefined"
        :striped="jobActive"
        :striped-flow="jobActive"
      />
      <div class="dispatch-job-detail">
        <span v-if="job.kind === 'accounts_sync' && job.totalPages">
          页码 {{ job.currentPage || 0 }} / {{ job.totalPages }}
        </span>
        <span v-if="job.total">账号 {{ job.processed || 0 }} / {{ job.total }}</span>
        <span v-if="job.phase">阶段：{{ job.phase }}</span>
      </div>
      <p v-if="jobFailed" class="dispatch-job-error">{{ job.error || "任务执行失败，请重试" }}</p>
    </div>

    <div v-if="hasCache" class="dispatch-cache-meta">
      <div class="dispatch-cache-times">
        <span>账号同步：{{ formatTime(refreshedAt) }}</span>
        <span>运行情况刷新：{{ activitiesRefreshedAt ? formatTime(activitiesRefreshedAt) : "尚未刷新" }}</span>
      </div>
      <div>
        <el-tag size="small" effect="plain">平台：{{ appliedRefreshFilter.platform || "全部" }}</el-tag>
        <el-tag size="small" effect="plain">类型：{{ appliedRefreshFilter.type || "全部" }}</el-tag>
        <el-tag size="small" effect="plain">状态：{{ appliedRefreshFilter.status ? statusText(appliedRefreshFilter.status) : "全部" }}</el-tag>
        <el-tag size="small" effect="plain">未分组：{{ appliedRefreshFilter.include_ungrouped ? "包含" : "不包含" }}</el-tag>
      </div>
    </div>

    <div class="dispatch-summary" v-if="hasCache">
      <div><span>账号</span><strong>{{ accounts.length }}</strong></div>
      <div><span>启用</span><strong class="summary-success">{{ activeCount }}</strong></div>
      <div><span>异常</span><strong class="summary-danger">{{ errorCount }}</strong></div>
      <div><span>当前筛选</span><strong>{{ filteredAccounts.length }}</strong></div>
    </div>

    <el-form class="toolbar dispatch-toolbar" inline label-position="top">
      <el-form-item label="账号">
        <el-input v-model="filters.search" :prefix-icon="Search" :disabled="controlsDisabled" clearable placeholder="名称或 ID" />
      </el-form-item>
      <el-form-item label="平台">
        <el-select v-model="filters.platform" :disabled="controlsDisabled" clearable placeholder="全部平台">
          <el-option v-for="option in platformOptions" :key="option.value" :label="option.label" :value="option.value" />
        </el-select>
      </el-form-item>
      <el-form-item label="类型">
        <el-select v-model="filters.type" :disabled="controlsDisabled" clearable placeholder="全部类型">
          <el-option v-for="option in typeOptions" :key="option.value" :label="option.label" :value="option.value" />
        </el-select>
      </el-form-item>
      <el-form-item label="状态">
        <el-select v-model="filters.status" :disabled="controlsDisabled" clearable placeholder="全部状态">
          <el-option v-for="option in STATUS_FILTER_OPTIONS" :key="option.value" :label="option.label" :value="option.value" />
        </el-select>
      </el-form-item>
      <el-form-item label="分组">
        <el-select v-model="filters.group_id" :disabled="controlsDisabled" clearable filterable placeholder="全部分组">
          <el-option v-for="group in groupOptions" :key="group.id" :label="group.name" :value="String(group.id)" />
          <el-option label="未分组" value="ungrouped" />
        </el-select>
      </el-form-item>
      <el-form-item class="dispatch-reset-item">
        <el-button :icon="RefreshLeft" :disabled="controlsDisabled" @click="resetFilters">重置</el-button>
      </el-form-item>
    </el-form>

    <div v-if="groupSections.length" class="dispatch-groups">
      <section v-for="section in groupSections" :key="section.key" class="dispatch-group">
        <header class="dispatch-group-head">
          <div class="dispatch-group-title">
            <h2>{{ section.group.name }}</h2>
            <el-tag v-if="section.group.platform" size="small" effect="plain">{{ section.group.platform }}</el-tag>
            <el-tag v-if="section.group.status" size="small" :type="section.group.status === 'active' ? 'success' : 'info'">
              {{ section.group.status === "active" ? "启用" : "停用" }}
            </el-tag>
            <span>{{ section.accounts.length }} 个账号</span>
            <span>OAuth {{ groupOAuthCount(section.accounts) }} 个</span>
          </div>
          <div class="dispatch-group-actions">
            <el-button
              v-if="section.group.id"
              text
              type="danger"
              size="small"
              :icon="Hide"
              :loading="isUpdatingGroup(section.group)"
              :disabled="controlsDisabled"
              @click="excludeGroup(section.group)"
            >
              排除
            </el-button>
            <el-tooltip :content="isCollapsed(section.key) ? '展开分组' : '收起分组'">
              <el-button
                circle
                text
                :icon="isCollapsed(section.key) ? ArrowRight : ArrowDown"
                @click="toggleCollapsed(section.key)"
              />
            </el-tooltip>
          </div>
        </header>

        <div v-show="!isCollapsed(section.key)" class="dispatch-account-grid">
          <article v-for="account in section.accounts" :key="`${section.key}-${account.id}`" class="dispatch-account-card">
            <header class="dispatch-account-head">
              <div class="dispatch-account-identity">
                <div class="dispatch-account-name">
                  <h3>{{ account.name }}</h3>
                  <span>#{{ account.id }}</span>
                </div>
                <div class="dispatch-account-meta">
                  <el-tag size="small" effect="plain">{{ account.platform || "-" }}</el-tag>
                  <span>{{ account.type || "-" }}</span>
                </div>
              </div>
              <div class="dispatch-account-state">
                <el-tag :type="statusType(accountFilterStatus(account))" size="small">
                  {{ statusText(accountFilterStatus(account)) }}
                </el-tag>
                <el-switch
                  v-model="account.is_enabled"
                  :loading="isUpdating(account.id)"
                  :disabled="controlsDisabled || isUpdating(account.id)"
                  :aria-label="`${account.name} 启用状态`"
                  @change="toggleAccount(account, $event)"
                />
              </div>
            </header>

            <p v-if="account.status === 'error' && account.error_message" class="dispatch-account-error">
              {{ account.error_message }}
            </p>

            <div class="account-policy-metrics">
              <div>
                <span>健康分</span>
                <el-tag :type="healthType(account.health_score ?? account.healthScore)" size="small">
                  {{ healthText(account) }}
                </el-tag>
                <small>{{ evidenceText(account) }}</small>
              </div>
              <div>
                <span>并发</span>
                <strong>{{ metricText(account.current_concurrency ?? account.currentConcurrency) }} / {{ metricText(account.concurrency) }}</strong>
                <small>目标 {{ metricText(account.target_concurrency ?? account.targetConcurrency) }}</small>
              </div>
              <div>
                <span>负载因子</span>
                <strong>{{ metricText(account.load_factor ?? account.loadFactor) }}</strong>
                <small>目标 {{ metricText(account.target_load_factor ?? account.targetLoadFactor) }}</small>
              </div>
              <div>
                <span>成本倍率</span>
                <strong>{{ account.rate_multiplier ?? account.rateMultiplier ?? "-" }}</strong>
                <small>{{ account.schedulable === false ? "不可调度" : "可调度" }}</small>
              </div>
              <p v-if="account.decision_reason || account.decisionReason">
                {{ account.decision_reason || account.decisionReason }}
                <span v-if="account.last_policy_action_at || account.lastPolicyActionAt"> · {{ formatTime(account.last_policy_action_at || account.lastPolicyActionAt) }}</span>
              </p>
            </div>

            <div v-if="account.recent_activity.length" class="activity-table">
              <div class="activity-table-head" aria-hidden="true">
                <span>结果</span><span>用户</span><span>模型</span><span>Token</span><span>费用</span><span>首字 / 总耗时</span><span>时间</span>
              </div>
              <div
                v-for="activity in account.recent_activity"
                :key="activity.id"
                class="activity-row"
                :class="{ 'is-error': activity.is_error || activity.isError }"
              >
                <div class="activity-cell activity-status">
                  <span class="activity-label">结果</span>
                  <el-tag :type="activity.is_error || activity.isError ? 'danger' : 'success'" size="small">
                    {{ activity.is_error || activity.isError ? `错误 ${activity.status_code || activity.statusCode || ''}` : "正常" }}
                  </el-tag>
                </div>
                <div class="activity-cell activity-user">
                  <span class="activity-label">用户</span>
                  <strong :title="userText(activity)">{{ userText(activity) }}</strong>
                </div>
                <div class="activity-cell activity-model">
                  <span class="activity-label">模型</span>
                  <strong :title="activity.model || '-'">{{ activity.model || "-" }}</strong>
                </div>
                <div class="activity-cell activity-number">
                  <span class="activity-label">Token</span>
                  <el-tooltip :content="tokenDetail(activity)"><strong>{{ tokenText(activity) }}</strong></el-tooltip>
                </div>
                <div class="activity-cell activity-number">
                  <span class="activity-label">费用</span>
                  <strong>{{ costText(activity) }}</strong>
                </div>
                <div class="activity-cell activity-duration">
                  <span class="activity-label">首字 / 总耗时</span>
                  <strong>{{ durationText(activity) }}</strong>
                </div>
                <div class="activity-cell activity-time">
                  <span class="activity-label">时间</span>
                  <strong>{{ activityTime(activity) }}</strong>
                </div>
                <p v-if="activity.is_error || activity.isError" class="activity-error-message" :title="activity.message">
                  {{ activity.message || "请求失败" }}
                </p>
              </div>
            </div>
            <el-empty v-else :image-size="42" description="暂无使用记录" />
          </article>
        </div>
      </section>
    </div>

    <el-empty
      v-else-if="loaded && !loading"
      :description="hasCache ? '没有符合条件的账号' : '暂无本地缓存数据'"
    />

    <el-dialog
      v-model="syncDialog"
      title="同步账号"
      width="min(520px, calc(100vw - 24px))"
      :close-on-click-modal="!jobActive"
      :close-on-press-escape="!jobActive"
      :show-close="!jobActive"
    >
      <el-form v-if="syncDialogMode === 'form'" :model="refreshForm" label-position="top" @submit.prevent="startAccountSync">
        <el-form-item label="平台类型">
          <el-select
            v-model="refreshForm.platform"
            clearable
            filterable
            :disabled="startingJob"
            placeholder="全部平台"
          >
            <el-option v-for="option in platformOptions" :key="option.value" :label="option.label" :value="option.value" />
          </el-select>
        </el-form-item>
        <el-form-item label="类型">
          <el-select
            v-model="refreshForm.type"
            clearable
            filterable
            :disabled="startingJob"
            placeholder="全部类型"
          >
            <el-option v-for="option in typeOptions" :key="option.value" :label="option.label" :value="option.value" />
          </el-select>
        </el-form-item>
        <el-form-item label="状态">
          <el-select v-model="refreshForm.status" clearable :disabled="startingJob" placeholder="全部状态">
            <el-option v-for="option in STATUS_FILTER_OPTIONS" :key="option.value" :label="option.label" :value="option.value" />
          </el-select>
        </el-form-item>
        <el-form-item label="未分组账号">
          <el-switch
            v-model="refreshForm.include_ungrouped"
            :disabled="startingJob"
            active-text="包含"
            inactive-text="不包含"
          />
        </el-form-item>
        <div v-if="excludedGroups.length" class="dispatch-excluded-groups">
          <div class="dispatch-excluded-title">
            <span>已排除分组</span>
            <el-tag size="small" type="info" effect="plain">{{ excludedGroups.length }}</el-tag>
          </div>
          <div
            v-for="excludedGroup in excludedGroups"
            :key="excludedGroupId(excludedGroup)"
            class="dispatch-excluded-row"
          >
            <div>
              <strong>{{ excludedGroupName(excludedGroup) }}</strong>
              <span>#{{ excludedGroupId(excludedGroup) }}</span>
              <el-tag v-if="excludedGroupPlatform(excludedGroup)" size="small" effect="plain">
                {{ excludedGroupPlatform(excludedGroup) }}
              </el-tag>
            </div>
            <el-tooltip content="取消排除">
              <el-button
                circle
                text
                :icon="CircleClose"
                :loading="isUpdatingGroup(excludedGroup)"
                :disabled="controlsDisabled"
                :aria-label="`取消排除 ${excludedGroupName(excludedGroup)}`"
                @click="restoreExcludedGroup(excludedGroup)"
              />
            </el-tooltip>
          </div>
        </div>
      </el-form>
      <div v-else class="dispatch-dialog-progress">
        <div class="dispatch-dialog-progress-head">
          <strong>{{ jobFailed ? "同步失败" : isSuccessfulJobStatus(job?.status) ? "同步完成" : "正在同步账号" }}</strong>
          <span>{{ jobProgressText }}</span>
        </div>
        <el-progress
          :percentage="jobPercent"
          :status="jobFailed ? 'exception' : isSuccessfulJobStatus(job?.status) ? 'success' : undefined"
          :striped="jobActive"
          :striped-flow="jobActive"
        />
        <div class="dispatch-job-detail">
          <span v-if="job?.totalPages">页码 {{ job.currentPage || 0 }} / {{ job.totalPages }}</span>
          <span v-if="job?.total">账号 {{ job.processed || 0 }} / {{ job.total }}</span>
          <span v-if="job?.phase">阶段：{{ job.phase }}</span>
        </div>
        <el-alert v-if="jobFailed" :title="job.error || '任务执行失败，请重试'" type="error" :closable="false" show-icon />
      </div>
      <template #footer>
        <div v-if="syncDialogMode === 'form'" class="dialog-footer">
          <el-button :disabled="startingJob" @click="syncDialog = false">取消</el-button>
          <el-button type="primary" :icon="Check" :loading="startingJob" @click="startAccountSync">开始同步</el-button>
        </div>
        <div v-else-if="!jobActive" class="dialog-footer">
          <el-button v-if="jobFailed" @click="syncDialogMode = 'form'">修改条件</el-button>
          <el-button v-if="jobFailed" type="primary" :icon="Refresh" :loading="startingJob" @click="startAccountSync">重新同步</el-button>
          <el-button v-else type="primary" @click="syncDialog = false">完成</el-button>
        </div>
      </template>
    </el-dialog>
  </section>
</template>

<style scoped>
.policy-panel {
  background: var(--panel);
  border: 1px solid var(--line-strong);
  border-radius: 8px;
  margin-bottom: 18px;
  overflow: hidden;
}

.policy-head,
.policy-master,
.policy-title-line,
.policy-actions {
  align-items: center;
  display: flex;
}

.policy-head {
  gap: 16px;
  justify-content: space-between;
  padding: 16px 18px;
}

.policy-master {
  gap: 13px;
  min-width: 0;
}

.policy-master > div {
  min-width: 0;
}

.policy-title-line {
  flex-wrap: wrap;
  gap: 8px;
}

.policy-title-line h2 {
  font-size: 17px;
  margin: 0;
}

.policy-master p {
  color: var(--muted);
  font-size: 12px;
  margin: 3px 0 0;
}

.policy-actions {
  flex: none;
  gap: 8px;
}

.policy-behavior {
  align-items: baseline;
  background: #f5f7fa;
  border-bottom: 1px solid var(--line);
  border-top: 1px solid var(--line);
  display: flex;
  font-size: 13px;
  gap: 10px;
  padding: 9px 18px;
}

.policy-behavior.is-active {
  background: #f0f9f4;
  color: #245b3d;
}

.policy-strategies {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
}

.policy-strategy {
  align-items: center;
  border-bottom: 1px solid var(--line);
  border-right: 1px solid var(--line);
  display: flex;
  gap: 10px;
  min-width: 0;
  padding: 13px 16px;
}

.policy-strategy:last-child {
  border-right: 0;
}

.policy-strategy > span {
  display: grid;
  gap: 2px;
  min-width: 0;
}

.policy-strategy strong {
  font-size: 13px;
}

.policy-strategy small,
.policy-input-grid label > span,
.policy-excluded > span {
  color: var(--muted);
  font-size: 11px;
}

.policy-runtime {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
}

.policy-runtime > div {
  border-bottom: 1px solid var(--line);
  border-right: 1px solid var(--line);
  display: grid;
  gap: 3px;
  padding: 11px 16px;
}

.policy-runtime > div:last-child {
  border-right: 0;
}

.policy-runtime span {
  color: var(--muted);
  font-size: 11px;
}

.policy-runtime strong {
  font-size: 14px;
  overflow-wrap: anywhere;
}

.policy-settings {
  border-bottom: 1px solid var(--line);
}

.policy-settings summary {
  cursor: pointer;
  font-size: 13px;
  font-weight: 700;
  padding: 10px 18px;
}

.policy-setting-groups {
  border-top: 1px solid var(--line);
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
}

.policy-setting-groups > section {
  border-bottom: 1px solid var(--line);
  min-width: 0;
  padding: 12px 16px 14px;
}

.policy-setting-groups > section:nth-child(odd) {
  border-right: 1px solid var(--line);
}

.policy-setting-groups h3 {
  font-size: 12px;
  margin: 0 0 9px;
}

.policy-input-grid {
  display: grid;
  gap: 9px 12px;
  grid-template-columns: repeat(2, minmax(0, 1fr));
}

.policy-input-grid label,
.policy-excluded {
  display: grid;
  gap: 4px;
  min-width: 0;
}

.policy-input-grid :deep(.el-input-number),
.policy-input-grid :deep(.el-input),
.policy-excluded :deep(.el-input) {
  width: 100%;
}

.policy-excluded {
  padding: 0 16px 14px;
}

.policy-rules {
  display: grid;
  gap: 7px;
  padding: 12px 18px;
}

.policy-rules p {
  display: grid;
  font-size: 12px;
  gap: 2px;
  grid-template-columns: 64px minmax(0, 1fr);
  margin: 0;
}

.policy-rules span,
.policy-recent-actions span {
  color: var(--muted);
}

.policy-recent-actions {
  border-top: 1px solid var(--line);
  display: grid;
  font-size: 12px;
  gap: 5px;
  padding: 11px 18px;
}

.policy-recent-actions span.is-error {
  color: var(--danger);
}

.dispatch-alert {
  margin-bottom: 12px;
}

.dispatch-job {
  background: var(--panel);
  border: 1px solid var(--line-strong);
  border-radius: 8px;
  margin-bottom: 14px;
  padding: 14px 16px;
}

.dispatch-job.is-error {
  border-color: #fecdca;
}

.dispatch-job-head,
.dispatch-dialog-progress-head {
  align-items: center;
  display: flex;
  gap: 8px 16px;
  justify-content: space-between;
  margin-bottom: 10px;
}

.dispatch-job-head > div {
  align-items: center;
  display: flex;
  gap: 8px;
}

.dispatch-job-head > span,
.dispatch-dialog-progress-head > span,
.dispatch-job-detail {
  color: var(--muted);
  font-size: 12px;
}

.dispatch-job-detail {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 18px;
  margin-top: 8px;
}

.dispatch-job-error {
  color: var(--danger);
  font-size: 13px;
  margin: 8px 0 0;
  overflow-wrap: anywhere;
}

.dispatch-dialog-progress {
  display: grid;
  gap: 4px;
  min-height: 130px;
}

.dispatch-dialog-progress .el-alert {
  margin-top: 10px;
}

.dispatch-cache-meta {
  align-items: center;
  color: var(--muted);
  display: flex;
  flex-wrap: wrap;
  font-size: 13px;
  gap: 8px 16px;
  justify-content: space-between;
  margin-bottom: 12px;
}

.dispatch-cache-meta > div {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.dispatch-cache-meta .dispatch-cache-times {
  gap: 6px 18px;
}

.dispatch-summary {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  margin-bottom: 16px;
}

.dispatch-summary > div {
  align-items: center;
  border-right: 1px solid var(--line);
  display: flex;
  justify-content: space-between;
  min-width: 0;
  padding: 12px 16px;
}

.dispatch-summary > div:last-child {
  border-right: 0;
}

.dispatch-summary span,
.dispatch-group-title > span,
.dispatch-account-name span,
.dispatch-account-meta span {
  color: var(--muted);
}

.dispatch-summary strong {
  font-size: 20px;
}

.summary-success {
  color: var(--success);
}

.summary-danger {
  color: var(--danger);
}

.dispatch-toolbar .el-form-item {
  min-width: 160px;
}

.dispatch-toolbar .el-form-item:first-child {
  flex: 1 1 240px;
}

.dispatch-toolbar :deep(.el-select),
.dispatch-toolbar :deep(.el-input) {
  width: 100%;
}

.dispatch-reset-item {
  min-width: auto !important;
}

.dispatch-groups {
  display: grid;
  gap: 24px;
}

.dispatch-group-head {
  align-items: center;
  border-bottom: 1px solid var(--line-strong);
  display: flex;
  justify-content: space-between;
  margin-bottom: 12px;
  padding: 0 2px 10px;
}

.dispatch-group-title {
  align-items: center;
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  min-width: 0;
}

.dispatch-group-actions {
  align-items: center;
  display: flex;
  flex: none;
  gap: 2px;
}

.dispatch-group-title h2 {
  font-size: 18px;
  margin: 0 4px 0 0;
  overflow-wrap: anywhere;
}

.dispatch-account-grid {
  display: grid;
  gap: 14px;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 720px), 1fr));
}

.dispatch-account-card {
  background: var(--panel);
  border: 1px solid var(--line-strong);
  border-radius: 8px;
  min-width: 0;
  overflow: hidden;
}

.dispatch-account-head {
  align-items: flex-start;
  border-bottom: 1px solid var(--line);
  display: flex;
  gap: 12px;
  justify-content: space-between;
  padding: 14px 16px;
}

.dispatch-account-identity {
  display: grid;
  gap: 6px;
  min-width: 0;
}

.dispatch-account-name,
.dispatch-account-meta,
.dispatch-account-state {
  align-items: center;
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.dispatch-account-name h3 {
  font-size: 16px;
  margin: 0;
  overflow-wrap: anywhere;
}

.dispatch-account-state {
  flex: none;
}

.dispatch-account-error {
  background: #fff5f4;
  border-bottom: 1px solid #fecdca;
  color: var(--danger);
  margin: 0;
  overflow-wrap: anywhere;
  padding: 9px 16px;
}

.activity-table {
  overflow-x: auto;
}

.activity-table-head,
.activity-row {
  display: grid;
  gap: 8px;
  grid-template-columns: 72px minmax(130px, 1.35fr) minmax(105px, 1fr) 86px 90px 118px 144px;
}

.activity-table-head {
  background: var(--panel-soft);
  border-bottom: 1px solid var(--line);
  color: var(--muted);
  font-size: 12px;
  font-weight: 700;
  min-width: 790px;
  padding: 8px 12px;
}

.activity-row {
  align-items: center;
  border-bottom: 1px solid var(--line);
  min-width: 790px;
  padding: 9px 12px;
}

.activity-row:last-child {
  border-bottom: 0;
}

.activity-row.is-error {
  background: #fffafa;
}

.activity-cell {
  min-width: 0;
}

.activity-cell strong {
  display: block;
  font-size: 12px;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.activity-number strong,
.activity-duration strong,
.activity-time strong {
  font-variant-numeric: tabular-nums;
}

.activity-error-message {
  color: var(--danger);
  font-size: 12px;
  grid-column: 2 / -1;
  margin: -2px 0 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.activity-label {
  display: none;
}

.dispatch-account-card :deep(.el-empty) {
  padding: 18px 0;
}

.account-policy-metrics {
  background: var(--panel-soft);
  border-bottom: 1px solid var(--line);
  display: grid;
  grid-template-columns: 1.3fr repeat(3, minmax(100px, 1fr));
  padding: 10px 16px;
}

.account-policy-metrics > div {
  align-content: start;
  border-right: 1px solid var(--line);
  display: grid;
  gap: 3px;
  min-width: 0;
  padding: 0 12px;
}

.account-policy-metrics > div:first-child {
  padding-left: 0;
}

.account-policy-metrics > div:nth-child(4) {
  border-right: 0;
  padding-right: 0;
}

.account-policy-metrics span,
.account-policy-metrics small {
  color: var(--muted);
  font-size: 11px;
  overflow-wrap: anywhere;
}

.account-policy-metrics strong {
  font-size: 13px;
}

.account-policy-metrics > p {
  color: var(--muted);
  font-size: 11px;
  grid-column: 1 / -1;
  margin: 8px 0 0;
  overflow-wrap: anywhere;
}

.dispatch-excluded-groups {
  border: 1px solid var(--line);
  border-radius: 6px;
  margin-top: 4px;
  overflow: hidden;
}

.dispatch-excluded-title,
.dispatch-excluded-row {
  align-items: center;
  display: flex;
  justify-content: space-between;
}

.dispatch-excluded-title {
  background: var(--panel-soft);
  color: var(--muted);
  font-size: 12px;
  font-weight: 700;
  padding: 7px 10px;
}

.dispatch-excluded-row {
  border-top: 1px solid var(--line);
  gap: 10px;
  min-height: 40px;
  padding: 5px 6px 5px 10px;
}

.dispatch-excluded-row > div {
  align-items: center;
  display: flex;
  flex-wrap: wrap;
  gap: 5px 8px;
  min-width: 0;
}

.dispatch-excluded-row strong {
  font-size: 13px;
  overflow-wrap: anywhere;
}

.dispatch-excluded-row span {
  color: var(--muted);
  font-size: 12px;
}

@media (max-width: 760px) {
  .policy-head,
  .policy-behavior {
    align-items: flex-start;
    flex-direction: column;
  }

  .policy-head {
    padding: 14px;
  }

  .policy-actions {
    justify-content: flex-end;
    width: 100%;
  }

  .policy-strategies,
  .policy-runtime,
  .policy-setting-groups,
  .policy-input-grid {
    grid-template-columns: 1fr;
  }

  .policy-strategy,
  .policy-runtime > div,
  .policy-setting-groups > section:nth-child(odd) {
    border-right: 0;
  }

  .policy-rules p {
    grid-template-columns: 1fr;
  }

  .dispatch-toolbar .el-form-item,
  .dispatch-toolbar .el-form-item:first-child {
    flex: none;
    min-width: 0;
  }

  .dispatch-job-head,
  .dispatch-dialog-progress-head {
    align-items: flex-start;
    flex-direction: column;
  }

  .dispatch-cache-meta {
    align-items: flex-start;
    flex-direction: column;
  }

  .dispatch-summary {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .dispatch-summary > div:nth-child(2) {
    border-right: 0;
  }

  .dispatch-summary > div:nth-child(-n + 2) {
    border-bottom: 1px solid var(--line);
  }

  .dispatch-account-head {
    padding: 12px;
  }

  .dispatch-group-head {
    align-items: flex-start;
    gap: 8px;
  }

  .dispatch-account-state {
    align-items: flex-end;
    flex-direction: column;
  }

  .account-policy-metrics {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    padding: 10px 12px;
  }

  .account-policy-metrics > div {
    border-bottom: 1px solid var(--line);
    padding: 8px;
  }

  .account-policy-metrics > div:nth-child(2),
  .account-policy-metrics > div:nth-child(4) {
    border-right: 0;
  }

  .activity-table {
    overflow: visible;
  }

  .activity-table-head {
    display: none;
  }

  .activity-row {
    align-items: start;
    gap: 10px 12px;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    min-width: 0;
    padding: 12px;
  }

  .activity-cell strong {
    font-size: 13px;
    overflow: visible;
    text-overflow: clip;
    white-space: normal;
    word-break: break-word;
  }

  .activity-status,
  .activity-user,
  .activity-model,
  .activity-time,
  .activity-error-message {
    grid-column: 1 / -1;
  }

  .activity-label {
    color: var(--muted);
    display: block;
    font-size: 11px;
    margin-bottom: 3px;
  }

  .activity-error-message {
    margin: 0;
    overflow: visible;
    text-overflow: clip;
    white-space: normal;
  }
}
</style>

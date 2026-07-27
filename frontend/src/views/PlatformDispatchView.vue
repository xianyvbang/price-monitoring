<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { ArrowDown, ArrowRight, Check, CircleClose, Delete, Hide, Link, Refresh, RefreshLeft, Search, Setting, Timer, VideoPause, VideoPlay } from "@element-plus/icons-vue";
import { api } from "../api";
import { boolValue, formatTime } from "../utils";

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
const DEFAULT_PROBE_MODELS_BY_GROUP_PLATFORM = Object.freeze({
  openai: "gpt-5.5",
  anthropic: "claude-sonnet-4-6"
});
const GROUP_PLATFORM_LABELS = Object.freeze({
  openai: "OpenAI",
  anthropic: "Anthropic"
});

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
const evidenceRefreshedAt = ref("");
const job = ref(null);
const collapsedGroups = ref(new Set());
const updatingIds = ref(new Set());
const updatingGroupIds = ref(new Set());
const updatingExcludedAccountIds = ref(new Set());
const probingAccountIds = ref(new Set());
const updatingProbeModelIds = ref(new Set());
const updatingGroupProbeModelIds = ref(new Set());
const excludingUngrouped = ref(false);
const costBindingDialog = ref(false);
const costBindingAccount = ref(null);
const costSourceOptions = ref([]);
const costSourceVisibilityFilter = ref("visible");
const costSourceLoading = ref(false);
const costBindingSaving = ref(false);
const selectedMonitorGroupId = ref(null);
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
const autoScoringSaving = ref(false);
const policyRunning = ref(false);
const policyStopping = ref(false);
const policyRuntime = ref({});
const policyActions = ref([]);
const excludedAccountText = ref("");
const policyConfig = reactive({
  enabled: false,
  auto_scoring_enabled: true,
  return_pool_enabled: false,
  smart_expand_enabled: false,
  load_factor_enabled: false,
  price_protection_enabled: false,
  probe_interval_seconds: 60,
  health_threshold: 75,
  evidence_ttl_multiplier: 3,
  minimum_available_accounts: 1,
  healthy_target_accounts: 3,
  oauth_account_threshold: 3,
  total_concurrency: 900,
  account_min_concurrency: 20,
  account_max_concurrency: 250,
  expand_trigger_percent: 80,
  expand_step_percent: 10,
  load_factor_total: 400,
  account_min_load_factor: 20,
  account_max_load_factor: 500,
  rate_weight_exponent: 1,
  minimum_profit_margin_percent: 10,
  load_change_threshold_percent: 10,
  load_change_cooldown_seconds: 60,
  failure_window: 5,
  failure_threshold: 3,
  failure_health_threshold: 60,
  slow_window: 10,
  slow_first_token_ms: 15000,
  slow_threshold: 5,
  default_probe_model: "",
  group_probe_models: {},
  account_probe_models: {},
  excluded_account_ids: []
});
let jobPollTimer = null;
let policyPollTimer = null;
let disposed = false;

const jobActive = computed(() => isActiveJobStatus(job.value?.status));
const jobFailed = computed(() => job.value?.status === "failed");
const groupedCostSourceOptions = computed(() => {
  const groupsByAccount = new Map();
  for (const option of costSourceOptions.value) {
    const visible = boolValue(option.is_visible ?? option.isVisible);
    if (costSourceVisibilityFilter.value === "visible" && !visible) continue;
    if (costSourceVisibilityFilter.value === "hidden" && visible) continue;
    const accountId = Number(option.balance_account_id);
    if (!groupsByAccount.has(accountId)) {
      groupsByAccount.set(accountId, {
        accountId,
        label: `${option.balance_platform || "-"} / ${option.balance_account_name || accountId}`,
        options: []
      });
    }
    groupsByAccount.get(accountId).options.push(option);
  }
  return [...groupsByAccount.values()];
});
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
const evidenceJob = computed(() => ["evidence_refresh", "activity_refresh"].includes(job.value?.kind));
const jobTitle = computed(() => evidenceJob.value ? "重新获取健康证据" : "同步账号");
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

const excludedAccountIdSet = computed(() => {
  return new Set(
    (policyConfig.excluded_account_ids || [])
      .map(Number)
      .filter((value) => Number.isInteger(value) && value > 0)
  );
});

const includedAccounts = computed(() => {
  return accounts.value.filter((account) => !excludedAccountIdSet.value.has(Number(account.id)));
});

const excludedAccounts = computed(() => {
  const accountsById = new Map(accounts.value.map((account) => [Number(account.id), account]));
  return [...excludedAccountIdSet.value]
    .sort((left, right) => left - right)
    .map((id) => accountsById.get(id) || { id, name: `账号 ${id}`, platform: "", type: "" });
});

const filteredAccounts = computed(() => {
  const search = filters.search.trim().toLowerCase();
  return includedAccounts.value.filter((account) => {
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

const activeCount = computed(() => includedAccounts.value.filter((account) => account.status === "active").length);
const errorCount = computed(() => includedAccounts.value.filter((account) => account.status === "error").length);
const policySummary = computed(() => policyRuntime.value?.summary || {});
const groupAvailabilityByKey = computed(() => {
  const entries = Array.isArray(policySummary.value.group_availability)
    ? policySummary.value.group_availability
    : [];
  return new Map(entries.map((item) => [item.pool_key, item]));
});
const oauthGroupStatisticsById = computed(() => {
  const entries = Array.isArray(policySummary.value.oauth_group_statistics)
    ? policySummary.value.oauth_group_statistics
    : [];
  return new Map(entries.map((item) => [Number(item.group_id), item]));
});
function groupAvailabilityTarget(item) {
  const available = Number(item?.available_accounts) || 0;
  const minimum = Number(item?.minimum_target ?? policyConfig.minimum_available_accounts) || 0;
  const healthy = Number(item?.healthy_target ?? policyConfig.healthy_target_accounts) || minimum;
  return available < minimum || !policyConfig.return_pool_enabled ? minimum : healthy;
}
const policyProgressPercent = computed(() => {
  const value = Number(policySummary.value.percent);
  return Number.isFinite(value) ? Math.min(100, Math.max(0, Math.round(value))) : 0;
});
const policyProgressMessage = computed(() => policySummary.value.message || "正在准备评分");
const policyProgressDetail = computed(() => {
  const processed = Number(policySummary.value.processed);
  const total = Number(policySummary.value.total);
  if (Number.isFinite(processed) && Number.isFinite(total) && total > 0) {
    return `${processed} / ${total} 个账号`;
  }
  return "";
});
const policyAutoRunning = computed(() => {
  return Boolean(policyRuntime.value?.is_running ?? policyRuntime.value?.isRunning ?? policyRuntime.value?.status === "running");
});
const policyAutomaticRunning = computed(() => {
  return Boolean(policyRuntime.value?.automatic_running ?? policyRuntime.value?.automaticRunning);
});
const dispatchMutationDisabled = computed(() => controlsDisabled.value || policyAutoRunning.value || probingAccountIds.value.size > 0);
const policyStatusText = computed(() => {
  if (policyAutoRunning.value) return policyConfig.enabled ? "正在自动调度" : "正在评分";
  if (policyRuntime.value?.status === "failed" && (policyConfig.enabled || policyConfig.auto_scoring_enabled)) return "执行异常";
  if (policyConfig.enabled) return "等待自动调度";
  if (policyConfig.auto_scoring_enabled) return "等待自动评分";
  return "自动执行已关闭";
});
const policyStatusType = computed(() => {
  if (
    policyRuntime.value?.status === "failed" &&
    !policyAutoRunning.value &&
    (policyConfig.enabled || policyConfig.auto_scoring_enabled)
  ) return "danger";
  if (policyAutoRunning.value) return "warning";
  return policyConfig.enabled || policyConfig.auto_scoring_enabled ? "success" : "info";
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
  applyPolicyRuntimePayload(payload);
  excludedAccountText.value = (policyConfig.excluded_account_ids || []).join(", ");
}

function applyAccountExclusionPayload(payload) {
  const excludedIds = payload.config?.excluded_account_ids || payload.config?.excludedAccountIds || [];
  policyConfig.excluded_account_ids = [...excludedIds];
  excludedAccountText.value = excludedIds.join(", ");
  applyPolicyRuntimePayload(payload);
}

function applyProbeModelPayload(payload) {
  const groupModels = payload.config?.group_probe_models || payload.config?.groupProbeModels || {};
  const accountModels = payload.config?.account_probe_models || payload.config?.accountProbeModels || {};
  policyConfig.group_probe_models = { ...groupModels };
  policyConfig.account_probe_models = { ...accountModels };
  applyPolicyRuntimePayload(payload);
}

function applyPolicyRuntimePayload(payload) {
  policyRuntime.value = payload.runtime || {};
  policyActions.value = payload.actions || [];
  const states = new Map((payload.accounts || []).map((item) => [Number(item.account_id), item]));
  accounts.value.forEach((account) => {
    const state = states.get(Number(account.id));
    if (!state) return;
    Object.assign(account, {
      health_score: state.health_score,
      health_short_score: state.short_score,
      health_long_score: state.long_score,
      health_evidence_count: state.evidence_count,
      health_evidence_at: state.evidence_at,
      health_evidence_fresh: Boolean(state.evidence_fresh),
      probe_records: state.probe_records || state.probeRecords || [],
      short_evidence_records: state.short_evidence_records || state.shortEvidenceRecords || [],
      decision_reason: state.decision_reason || "",
      target_concurrency: state.target_concurrency,
      target_load_factor: state.target_load_factor,
      last_policy_action_at: state.last_action_at
    });
  });
}

function stopPolicyPolling() {
  if (policyPollTimer !== null) {
    window.clearTimeout(policyPollTimer);
    policyPollTimer = null;
  }
}

function schedulePolicyPoll() {
  stopPolicyPolling();
  if (!disposed) policyPollTimer = window.setTimeout(pollPolicyRuntime, policyAutoRunning.value ? 1000 : 3000);
}

async function pollPolicyRuntime() {
  policyPollTimer = null;
  try {
    await refreshPolicyRuntime();
  } catch {
    // Keep the last known state; the next poll will retry.
  } finally {
    schedulePolicyPoll();
  }
}

async function refreshPolicyRuntime() {
  applyPolicyRuntimePayload(await api.platformDispatchPolicy());
}

function parseExcludedAccountIds() {
  const values = excludedAccountText.value.split(/[，,\s]+/).filter(Boolean).map(Number);
  if (values.some((value) => !Number.isInteger(value) || value <= 0)) throw new Error("排除账号 ID 必须是正整数");
  return [...new Set(values)];
}

function toggleAutomaticDispatch(enabled) {
  if (enabled) policyConfig.auto_scoring_enabled = true;
}

async function toggleAutomaticScoring(enabled) {
  const previousAutoScoringEnabled = !enabled;
  const previousDispatchEnabled = policyConfig.enabled;
  if (!enabled) policyConfig.enabled = false;
  autoScoringSaving.value = true;
  try {
    const payload = await api.savePlatformDispatchPolicy({
      auto_scoring_enabled: Boolean(enabled),
      enabled: Boolean(policyConfig.enabled)
    });
    policyConfig.auto_scoring_enabled = Boolean(payload.config?.auto_scoring_enabled);
    policyConfig.enabled = Boolean(payload.config?.enabled);
    applyPolicyRuntimePayload(payload);
    ElMessage.success(enabled ? "自动评分已开启" : "自动评分已关闭");
  } catch (error) {
    policyConfig.auto_scoring_enabled = previousAutoScoringEnabled;
    policyConfig.enabled = previousDispatchEnabled;
    ElMessage.error(error.message || "更新自动评分开关失败");
  } finally {
    autoScoringSaving.value = false;
  }
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
    ElMessage.success(payload.summary?.scheduling_action || payload.summary?.status_action || payload.summary?.message || "自动调度轮次已完成");
  } catch (error) {
    ElMessage.error(error.message || "执行自动调度失败");
  } finally {
    policyRunning.value = false;
  }
}

async function stopAutomaticPolicyRound() {
  policyStopping.value = true;
  try {
    const payload = await api.stopPlatformDispatchPolicy();
    applyPolicyRuntimePayload(payload);
    if (payload.stopped) ElMessage.success(payload.message || "本轮自动执行已停止");
    else ElMessage.info(payload.message || "当前没有正在执行的自动轮次");
  } catch (error) {
    ElMessage.error(error.message || "停止本轮自动执行失败");
  } finally {
    policyStopping.value = false;
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
    schedulable: account.schedulable !== false,
    probe_records: account.probe_records || account.probeRecords || [],
    short_evidence_records: account.short_evidence_records || account.shortEvidenceRecords || []
  }));
  groups.value = payload.groups || [];
  excludedGroups.value = payload.excluded_groups || payload.excludedGroups || [];
  warnings.value = payload.warnings || [];
  siteUrl.value = payload.site_url || payload.siteUrl || "";
  refreshedAt.value = payload.refreshed_at || payload.refreshedAt || "";
  evidenceRefreshedAt.value = payload.activities_refreshed_at || payload.activitiesRefreshedAt || "";
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

function costStatus(account) {
  return account.price_protection_status || account.priceProtectionStatus || "unbound";
}

function costStatusText(account) {
  return {
    safe: "价格安全",
    unsafe: "价格过低",
    rate_expired: "倍率过期",
    upstream_unknown: "等待上游倍率",
    downstream_unknown: "下游倍率未知",
    unbound: "未绑定"
  }[costStatus(account)] || "未知";
}

function costStatusType(account) {
  return {
    safe: "success",
    unsafe: "danger",
    rate_expired: "danger",
    upstream_unknown: "warning",
    downstream_unknown: "warning",
    unbound: "info"
  }[costStatus(account)] || "info";
}

function costBindingName(account) {
  const binding = account.cost_binding || account.costBinding;
  if (!binding) return "未选择余额监控分组";
  return `${binding.balance_account_name || "余额账号"} / ${binding.group_name || binding.monitor_group_id}`;
}

function costOptionLabel(option) {
  const platform = option.balance_platform || "-";
  const accountName = option.balance_account_name || `账号 ${option.balance_account_id}`;
  const groupName = option.group_plan_name || option.group_name || `分组 ${option.monitor_group_id}`;
  const rate = metricText(option.effective_rate_multiplier);
  const cost = metricText(option.upstream_cost_multiplier ?? option.upstreamCostMultiplier);
  return `${platform} / ${accountName} / ${groupName} · 分组倍率 ${rate} · 成本 ${cost}`;
}

async function openCostBindingDialog(account) {
  costBindingAccount.value = account;
  selectedMonitorGroupId.value = account.cost_binding?.monitor_group_id ?? account.costBinding?.monitor_group_id ?? null;
  costSourceVisibilityFilter.value = "visible";
  costBindingDialog.value = true;
  costSourceLoading.value = true;
  try {
    const payload = await api.platformDispatchCostSourceOptions();
    costSourceOptions.value = payload.items || [];
  } catch (error) {
    ElMessage.error(error.message || "加载余额监控分组失败");
  } finally {
    costSourceLoading.value = false;
  }
}

async function saveCostBinding() {
  if (!costBindingAccount.value || !selectedMonitorGroupId.value) {
    ElMessage.warning("请选择余额监控分组");
    return;
  }
  costBindingSaving.value = true;
  try {
    const payload = await api.setPlatformDispatchCostBinding(costBindingAccount.value.id, selectedMonitorGroupId.value);
    applyDispatchPayload(payload);
    costBindingDialog.value = false;
    ElMessage.success("上游成本绑定已保存");
  } catch (error) {
    ElMessage.error(error.message || "保存上游成本绑定失败");
  } finally {
    costBindingSaving.value = false;
  }
}

async function deleteCostBinding() {
  if (!costBindingAccount.value?.cost_binding && !costBindingAccount.value?.costBinding) return;
  try {
    await ElMessageBox.confirm("解除后，该账号将不参与成本调权和价格保护。", "解除上游成本绑定", { type: "warning" });
  } catch {
    return;
  }
  costBindingSaving.value = true;
  try {
    const payload = await api.deletePlatformDispatchCostBinding(costBindingAccount.value.id);
    applyDispatchPayload(payload);
    costBindingDialog.value = false;
    ElMessage.success("上游成本绑定已解除");
  } catch (error) {
    ElMessage.error(error.message || "解除上游成本绑定失败");
  } finally {
    costBindingSaving.value = false;
  }
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

async function startEvidenceRefresh() {
  try {
    await ElMessageBox.confirm(
      `将为全部 ${accounts.value.length} 个已同步账号重新拉取最近的使用和错误证据，逐个执行探活，并重新计算短期分、长期分和健康分。`,
      "重新获取健康证据",
      { confirmButtonText: "开始获取", cancelButtonText: "取消", type: "info" }
    );
  } catch {
    return;
  }

  startingJob.value = true;
  try {
    const payload = await api.refreshPlatformDispatchEvidence();
    if (!setJobFromPayload(payload)) throw new Error("服务器未返回任务信息");
    if (jobActive.value) scheduleJobPoll();
    else await finishStartedJob();
  } catch (error) {
    ElMessage.error(error.message || "启动健康证据获取失败");
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
    if (error.status === 409) {
      await refreshPolicyRuntime().catch(() => {});
      schedulePolicyPoll();
    }
    ElMessage.error(error.message || "排除分组失败");
  } finally {
    setGroupUpdating(groupId, false);
  }
}

async function excludeUngroupedAccounts() {
  try {
    await ElMessageBox.confirm(
      "屏蔽后，当前未分组账号会从平台调度缓存移除，后续同步也将默认不同步未分组账号。需要恢复时，可在同步账号时重新开启该选项。",
      "屏蔽未分组账号",
      { confirmButtonText: "确认屏蔽", cancelButtonText: "取消", type: "warning" }
    );
  } catch {
    return;
  }

  excludingUngrouped.value = true;
  try {
    const payload = await api.excludePlatformDispatchUngrouped();
    applyDispatchPayload(payload);
    ElMessage.success("已屏蔽未分组账号");
  } catch (error) {
    if (error.status === 409) {
      await refreshPolicyRuntime().catch(() => {});
      schedulePolicyPoll();
    }
    ElMessage.error(error.message || "屏蔽未分组账号失败");
  } finally {
    excludingUngrouped.value = false;
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
    if (error.status === 409) {
      await refreshPolicyRuntime().catch(() => {});
      schedulePolicyPoll();
    }
    ElMessage.error(error.message || "取消排除分组失败");
  } finally {
    setGroupUpdating(groupId, false);
  }
}

async function excludeAccount(account) {
  const accountId = Number(account?.id);
  if (!Number.isInteger(accountId) || accountId <= 0) return;
  try {
    await ElMessageBox.confirm(
      `排除后，“${account.name || `账号 ${accountId}`}”将退出自动评分和调度，并从当前账号列表隐藏。不会删除或停用远端账号。`,
      "排除账号",
      { confirmButtonText: "确认排除", cancelButtonText: "取消", type: "warning" }
    );
  } catch {
    return;
  }

  setExcludedAccountUpdating(accountId, true);
  try {
    applyAccountExclusionPayload(await api.excludePlatformDispatchAccount(accountId));
    ElMessage.success(`已排除账号“${account.name || accountId}”`);
  } catch (error) {
    if (error.status === 409) {
      await refreshPolicyRuntime().catch(() => {});
      schedulePolicyPoll();
    }
    ElMessage.error(error.message || "排除账号失败");
  } finally {
    setExcludedAccountUpdating(accountId, false);
  }
}

async function restoreExcludedAccount(account) {
  const accountId = Number(account?.id);
  if (!Number.isInteger(accountId) || accountId <= 0) return;
  setExcludedAccountUpdating(accountId, true);
  try {
    applyAccountExclusionPayload(await api.restorePlatformDispatchAccount(accountId));
    ElMessage.success(`已恢复账号“${account.name || accountId}”`);
  } catch (error) {
    if (error.status === 409) {
      await refreshPolicyRuntime().catch(() => {});
      schedulePolicyPoll();
    }
    ElMessage.error(error.message || "恢复账号失败");
  } finally {
    setExcludedAccountUpdating(accountId, false);
  }
}

function setExcludedAccountUpdating(accountId, updating) {
  const next = new Set(updatingExcludedAccountIds.value);
  if (updating) next.add(accountId);
  else next.delete(accountId);
  updatingExcludedAccountIds.value = next;
}

function isExcludedAccountUpdating(account) {
  return updatingExcludedAccountIds.value.has(Number(account?.id));
}

function accountProbeModel(account) {
  const accountId = String(Number(account?.id));
  return String(policyConfig.account_probe_models?.[accountId] || "").trim();
}

function groupProbeModel(group) {
  const groupId = String(Number(group?.id));
  return String(policyConfig.group_probe_models?.[groupId] || "").trim();
}

function accountGroupProbeModel(account) {
  const groupModels = policyConfig.group_probe_models || {};
  const groupIds = [...new Set(accountGroupIds(account))].sort((left, right) => left - right);
  for (const groupId of groupIds) {
    const model = String(groupModels[String(groupId)] || "").trim();
    if (!model) continue;
    const group = groups.value.find((item) => Number(item.id) === groupId);
    return { model, groupId, groupName: group?.name || `分组 ${groupId}` };
  }
  return null;
}

function groupPlatformDefaultProbeModel(group) {
  const platform = String(group?.platform || "").trim().toLowerCase();
  const model = DEFAULT_PROBE_MODELS_BY_GROUP_PLATFORM[platform] || "";
  return model ? { model, platform, platformLabel: GROUP_PLATFORM_LABELS[platform] || platform } : null;
}

function accountGroupPlatformDefaultProbeModel(account) {
  const groupIds = [...new Set(accountGroupIds(account))].sort((left, right) => left - right);
  for (const groupId of groupIds) {
    const group = groups.value.find((item) => Number(item.id) === groupId);
    const platformDefault = groupPlatformDefaultProbeModel(group);
    if (!platformDefault) continue;
    return {
      ...platformDefault,
      groupId,
      groupName: group?.name || `分组 ${groupId}`
    };
  }
  return null;
}

function effectiveProbeModel(account) {
  return accountProbeModel(account)
    || accountGroupProbeModel(account)?.model
    || String(policyConfig.default_probe_model || "").trim()
    || accountGroupPlatformDefaultProbeModel(account)?.model;
}

function probeModelText(account) {
  const accountModel = accountProbeModel(account);
  if (accountModel) return `${accountModel}（账号）`;
  const groupModel = accountGroupProbeModel(account);
  if (groupModel) return `${groupModel.model}（${groupModel.groupName}）`;
  const defaultModel = String(policyConfig.default_probe_model || "").trim();
  if (defaultModel) return `${defaultModel}（默认）`;
  const platformDefault = accountGroupPlatformDefaultProbeModel(account);
  return platformDefault
    ? `${platformDefault.model}（${platformDefault.platformLabel} 类型默认）`
    : "Sub2API 默认";
}

function groupProbeModelText(group) {
  const model = groupProbeModel(group);
  if (model) return `${model}（分组）`;
  const defaultModel = String(policyConfig.default_probe_model || "").trim();
  if (defaultModel) return `${defaultModel}（默认）`;
  const platformDefault = groupPlatformDefaultProbeModel(group);
  return platformDefault
    ? `${platformDefault.model}（${platformDefault.platformLabel} 类型默认）`
    : "Sub2API 默认";
}

async function configureProbeModel(account) {
  const accountId = Number(account?.id);
  if (!Number.isInteger(accountId) || accountId <= 0) return;
  const defaultModel = String(policyConfig.default_probe_model || "").trim();
  const inheritedGroup = accountGroupProbeModel(account);
  const inheritedPlatform = accountGroupPlatformDefaultProbeModel(account);
  const inheritedModel = inheritedGroup?.model || defaultModel || inheritedPlatform?.model;
  const inheritedText = inheritedGroup
    ? `留空将使用分组“${inheritedGroup.groupName}”的模型：${inheritedGroup.model}`
    : defaultModel
      ? `留空将使用默认模型：${defaultModel}`
      : inheritedPlatform
        ? `留空将使用 ${inheritedPlatform.platformLabel} 类型默认模型：${inheritedPlatform.model}`
        : "留空将使用 Sub2API 默认模型";
  let value;
  try {
    const result = await ElMessageBox.prompt(
      inheritedText,
      `设置探活模型 - ${account.name || accountId}`,
      {
        confirmButtonText: "保存",
        cancelButtonText: "取消",
        inputValue: accountProbeModel(account),
        inputPlaceholder: inheritedModel || "例如 gpt-5-mini",
        inputValidator: (input) => String(input || "").trim().length <= 200 || "模型名不能超过 200 个字符"
      }
    );
    value = String(result.value || "").trim();
  } catch {
    return;
  }

  setProbeModelUpdating(accountId, true);
  try {
    applyProbeModelPayload(await api.setPlatformDispatchProbeModel(accountId, value));
    ElMessage.success(value ? `探活模型已设置为 ${value}` : "已恢复继承的探活模型");
  } catch (error) {
    if (error.status === 409) {
      await refreshPolicyRuntime().catch(() => {});
      schedulePolicyPoll();
    }
    ElMessage.error(error.message || "保存探活模型失败");
  } finally {
    setProbeModelUpdating(accountId, false);
  }
}

function setProbeModelUpdating(accountId, updating) {
  const next = new Set(updatingProbeModelIds.value);
  if (updating) next.add(accountId);
  else next.delete(accountId);
  updatingProbeModelIds.value = next;
}

function isProbeModelUpdating(account) {
  return updatingProbeModelIds.value.has(Number(account?.id));
}

function setAccountProbing(accountId, probing) {
  const next = new Set(probingAccountIds.value);
  if (probing) next.add(accountId);
  else next.delete(accountId);
  probingAccountIds.value = next;
}

function isAccountProbing(account) {
  return probingAccountIds.value.has(Number(account?.id));
}

async function probeAccount(account) {
  const accountId = Number(account?.id);
  if (!Number.isInteger(accountId) || accountId <= 0) return;
  setAccountProbing(accountId, true);
  try {
    const payload = await api.probePlatformDispatchAccount(accountId);
    applyPolicyPayload(payload);
    const probe = payload.probe || {};
    const model = probe.model || "Sub2API 默认模型";
    if (probe.success) {
      ElMessage.success(`${account.name || `账号 ${accountId}`} 探活成功 · ${model}`);
    } else {
      ElMessage.error(probe.message || `${account.name || `账号 ${accountId}`} 探活失败`);
    }
  } catch (error) {
    if (error.status === 409) {
      await refreshPolicyRuntime().catch(() => {});
      schedulePolicyPoll();
    }
    ElMessage.error(error.message || "账号探活失败");
  } finally {
    setAccountProbing(accountId, false);
  }
}

async function configureGroupProbeModel(group) {
  const groupId = Number(group?.id);
  if (!Number.isInteger(groupId) || groupId <= 0) return;
  const defaultModel = String(policyConfig.default_probe_model || "").trim();
  const platformDefault = groupPlatformDefaultProbeModel(group);
  const inheritedModel = defaultModel || platformDefault?.model || "";
  const inheritedText = defaultModel
    ? `留空将使用默认模型：${defaultModel}`
    : platformDefault
      ? `留空将使用 ${platformDefault.platformLabel} 类型默认模型：${platformDefault.model}`
      : "留空将使用 Sub2API 默认模型";
  let value;
  try {
    const result = await ElMessageBox.prompt(
      inheritedText,
      `设置分组探活模型 - ${group.name || groupId}`,
      {
        confirmButtonText: "保存",
        cancelButtonText: "取消",
        inputValue: groupProbeModel(group),
        inputPlaceholder: inheritedModel || "例如 gpt-5-mini",
        inputValidator: (input) => String(input || "").trim().length <= 200 || "模型名不能超过 200 个字符"
      }
    );
    value = String(result.value || "").trim();
  } catch {
    return;
  }

  setGroupProbeModelUpdating(groupId, true);
  try {
    applyProbeModelPayload(await api.setPlatformDispatchGroupProbeModel(groupId, value));
    ElMessage.success(value ? `分组探活模型已设置为 ${value}` : "分组已恢复默认探活模型");
  } catch (error) {
    if (error.status === 409) {
      await refreshPolicyRuntime().catch(() => {});
      schedulePolicyPoll();
    }
    ElMessage.error(error.message || "保存分组探活模型失败");
  } finally {
    setGroupProbeModelUpdating(groupId, false);
  }
}

function setGroupProbeModelUpdating(groupId, updating) {
  const next = new Set(updatingGroupProbeModelIds.value);
  if (updating) next.add(groupId);
  else next.delete(groupId);
  updatingGroupProbeModelIds.value = next;
}

function isGroupProbeModelUpdating(group) {
  return updatingGroupProbeModelIds.value.has(Number(group?.id));
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
  if (evidenceJob.value) await refreshPolicyRuntime().catch(() => {});
  if (isSuccessfulJobStatus(job.value?.status)) {
    ElMessage.success(evidenceJob.value ? "健康证据已重新获取并完成评分" : `账号同步完成，共 ${accounts.value.length} 个账号`);
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

async function toggleAccountSchedulable(account, schedulable) {
  updatingIds.value = new Set([...updatingIds.value, account.id]);
  try {
    const payload = await api.setPlatformDispatchSchedulable(account.id, schedulable);
    const updated = payload.account || {};
    account.status = updated.status || account.status;
    account.filter_status = updated.filter_status || updated.filterStatus || account.status;
    account.filterStatus = account.filter_status;
    account.is_enabled = updated.is_enabled ?? updated.isEnabled ?? account.is_enabled;
    account.schedulable = updated.schedulable ?? schedulable;
    account.error_message = updated.error_message || updated.errorMessage || "";
    ElMessage.success(`${account.name} 已在 Sub2API ${schedulable ? "开启" : "关闭"}调度`);
  } catch (error) {
    account.schedulable = !schedulable;
    ElMessage.error(error.message || "更新 Sub2API 账号调度开关失败");
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

function groupOAuthStatistic(group) {
  const groupId = Number(group?.id);
  return Number.isInteger(groupId) && groupId > 0
    ? oauthGroupStatisticsById.value.get(groupId) || null
    : null;
}

function groupOAuthStatusText(group) {
  const statistic = groupOAuthStatistic(group);
  if (!statistic) return "正常 OAuth 尚无实时数据";
  const count = Number(statistic.normal_oauth_accounts) || 0;
  const threshold = Number(statistic.oauth_account_threshold ?? policyConfig.oauth_account_threshold) || 0;
  return `正常 OAuth ${count} 个 / 阈值 ${threshold}`;
}

function groupOAuthThresholdText(group) {
  const affected = Number(groupOAuthStatistic(group)?.affected_apikey_accounts) || 0;
  return affected > 0 ? `APIKey 停调 ${affected} 个` : "OAuth 已超阈值";
}

function statusText(status) {
  return STATUS_FILTER_OPTIONS.find((option) => option.value === status)?.label || status || "-";
}

function accountFilterStatus(account) {
  return account.filter_status || account.filterStatus || account.status || "inactive";
}

function accountSwitchText(account, schedulable) {
  const status = accountFilterStatus(account);
  if (!schedulable) return "调度关闭";
  if (status === "inactive") return "账号停用";
  if (status === "error") return "账号错误";
  return {
    active: "调度中",
    rate_limited: "限流中",
    temp_unschedulable: "暂不可用",
    unschedulable: "不可调度"
  }[status] || "已启用";
}

function accountSwitchColor(account, schedulable) {
  const status = accountFilterStatus(account);
  if (!schedulable) return "#909399";
  if (status === "inactive" || status === "error") return "var(--danger)";
  if (status === "active") return "var(--success)";
  if (status === "rate_limited" || status === "temp_unschedulable") return "var(--warning)";
  if (status === "unschedulable") return "var(--muted)";
  return "var(--primary)";
}

function formatDuration(value) {
  if (value === null || value === undefined || value === "") return "-";
  const milliseconds = Number(value);
  if (!Number.isFinite(milliseconds)) return "-";
  if (milliseconds < 1000) return `${Math.round(milliseconds)} ms`;
  return `${(milliseconds / 1000).toFixed(milliseconds < 10000 ? 2 : 1)} s`;
}

function isUpdating(accountId) {
  return updatingIds.value.has(accountId);
}

function healthType(score) {
  if (score === null || score === undefined || score === "") return "info";
  const value = Number(score);
  if (!Number.isFinite(value)) return "info";
  if (value >= Number(policyConfig.health_threshold)) return "success";
  if (value >= Number(policyConfig.failure_health_threshold)) return "warning";
  return "danger";
}

function healthText(account) {
  const rawValue = account.health_score ?? account.healthScore;
  if (rawValue === null || rawValue === undefined || rawValue === "") return "待采集";
  const value = Number(rawValue);
  return Number.isFinite(value) ? value.toFixed(1) : "待采集";
}

function hasHealthComposition(account) {
  const shortValue = account.health_short_score ?? account.healthShortScore;
  const longValue = account.health_long_score ?? account.healthLongScore;
  if (shortValue === null || shortValue === undefined || shortValue === "") return false;
  if (longValue === null || longValue === undefined || longValue === "") return false;
  const shortScore = Number(shortValue);
  const longScore = Number(longValue);
  return Number.isFinite(shortScore) && Number.isFinite(longScore);
}

function healthPartText(value, weight) {
  if (value === null || value === undefined || value === "") return "-";
  const score = Number(value);
  if (!Number.isFinite(score)) return "-";
  return `${score.toFixed(1)} × ${weight}% = ${(score * weight / 100).toFixed(1)}`;
}

function evidenceText(account) {
  const at = account.health_evidence_at || account.healthEvidenceAt;
  if (!at) return "无证据";
  const count = Number(account.health_evidence_count ?? account.healthEvidenceCount);
  const countText = Number.isFinite(count) ? `${count} 条证据 · ` : "";
  return `${countText}${account.health_evidence_fresh ?? account.healthEvidenceFresh ? "有效" : "已过期"} · ${formatTime(at)}`;
}

function probeRecords(account) {
  const records = account.probe_records || account.probeRecords || [];
  if (!Array.isArray(records)) return [];
  return records
    .map((record, index) => ({
      record,
      index,
      occurredAt: Date.parse(record.occurred_at || record.occurredAt || "")
    }))
    .sort((left, right) => {
      const leftValid = Number.isFinite(left.occurredAt);
      const rightValid = Number.isFinite(right.occurredAt);
      if (leftValid && rightValid && left.occurredAt !== right.occurredAt) return right.occurredAt - left.occurredAt;
      if (leftValid !== rightValid) return leftValid ? -1 : 1;
      return left.index - right.index;
    })
    .slice(0, 15)
    .map(({ record }) => record);
}

function probeTimelineRecords(account) {
  return probeRecords(account).reverse();
}

function shortEvidenceRecords(account) {
  const records = account.short_evidence_records || account.shortEvidenceRecords || [];
  if (!Array.isArray(records)) return [];
  return records
    .filter((record) => ["usage", "error"].includes(record.source_kind || record.sourceKind))
    .map((record, index) => ({
      record,
      index,
      occurredAt: Date.parse(record.occurred_at || record.occurredAt || "")
    }))
    .sort((left, right) => {
      const leftValid = Number.isFinite(left.occurredAt);
      const rightValid = Number.isFinite(right.occurredAt);
      if (leftValid && rightValid && left.occurredAt !== right.occurredAt) return right.occurredAt - left.occurredAt;
      if (leftValid !== rightValid) return leftValid ? -1 : 1;
      return left.index - right.index;
    })
    .slice(0, 10)
    .map(({ record }) => record);
}

function probeSucceeded(record) {
  return Boolean(record.is_probe_success ?? record.isProbeSuccess);
}

function evidenceCategoryText(category) {
  const labels = {
    healthy: "正常",
    slow: "慢响应",
    fatal_auth: "认证失败",
    fatal_balance: "余额不足",
    fatal_usage: "用量耗尽",
    probe_failure: "探活失败",
    timeout: "超时",
    upstream_error: "上游错误"
  };
  if (labels[category]) return labels[category];
  return String(category || "未知").startsWith("http_") ? `HTTP ${String(category).slice(5)}` : category || "未知";
}

function evidenceDetail(record) {
  const statusCode = record.status_code ?? record.statusCode;
  const firstTokenMs = record.first_token_ms ?? record.firstTokenMs;
  const timeout = record.is_timeout ?? record.isTimeout;
  const message = String(record.message || "").trim();
  return [
    statusCode ? `HTTP ${statusCode}` : "",
    firstTokenMs === null || firstTokenMs === undefined ? "" : `首字 ${formatDuration(firstTokenMs)}`,
    timeout ? "超时" : "",
    message
  ].filter(Boolean).join(" · ") || "-";
}

function evidenceTime(record) {
  return formatTime(record.occurred_at || record.occurredAt);
}

function probeTimelineDetail(record) {
  const time = evidenceTime(record);
  if (probeSucceeded(record)) return `探活成功 · ${time}`;
  const category = evidenceCategoryText(record.category);
  const detail = evidenceDetail(record);
  return [`探活失败（${category}）`, detail === "-" ? "无错误详情" : detail, time].filter(Boolean).join(" · ");
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
  schedulePolicyPoll();
  resumeJob();
});

onBeforeUnmount(() => {
  disposed = true;
  stopJobPolling();
  stopPolicyPolling();
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
        <el-button :icon="Refresh" :disabled="dispatchMutationDisabled" @click="openRefreshDialog">同步账号</el-button>
        <el-button
          :icon="Timer"
          :disabled="dispatchMutationDisabled || !hasCache || accounts.length === 0"
          @click="startEvidenceRefresh"
        >
          重新获取短期证据和长期证据
        </el-button>
      </div>
    </div>

    <section v-loading="policyLoading" class="policy-panel">
      <header class="policy-head">
        <div class="policy-master">
          <el-switch
            v-model="policyConfig.enabled"
            size="large"
            aria-label="自动调度总开关"
            @change="toggleAutomaticDispatch"
          />
          <div>
            <div class="policy-title-line">
              <h2>自动调度</h2>
              <el-tag :type="policyStatusType" size="small">
                {{ policyStatusText }}
              </el-tag>
            </div>
            <p v-if="policyAutoRunning && policyConfig.enabled">正在评估并更新账号，排除、同步和账号调度开关暂不可用</p>
            <p v-else-if="policyAutoRunning">正在读取请求记录并计算健康评分，排除、同步和账号调度开关暂不可用</p>
            <p v-else-if="policyConfig.enabled">自动调度已开启，等待后台执行下一轮</p>
            <p v-else-if="policyConfig.auto_scoring_enabled">自动评分已开启，后台仅获取证据并计算健康分</p>
            <p v-else>自动评分和自动调度均已关闭</p>
          </div>
        </div>
        <div class="policy-actions">
          <el-button :loading="policySaving" :disabled="controlsDisabled || policyRunning || autoScoringSaving" @click="savePolicy">保存策略</el-button>
          <el-tooltip v-if="policyAutomaticRunning" content="停止本轮，下次仍按原计划执行">
            <el-button
              circle
              type="danger"
              :icon="VideoPause"
              :loading="policyStopping"
              :disabled="policyStopping"
              aria-label="停止本轮自动执行"
              @click="stopAutomaticPolicyRound"
            />
          </el-tooltip>
          <el-tooltip v-else content="立即执行一轮">
            <el-button
              circle
              type="primary"
              :icon="VideoPlay"
              :loading="policyRunning"
              :disabled="controlsDisabled || policySaving || policyAutoRunning"
              aria-label="立即执行一轮"
              @click="runPolicyNow"
            />
          </el-tooltip>
        </div>
      </header>

      <div v-if="policyAutoRunning" class="policy-progress">
        <div class="policy-progress-head">
          <strong>{{ policyProgressMessage }}</strong>
          <span v-if="policyProgressDetail">{{ policyProgressDetail }}</span>
        </div>
        <el-progress
          :percentage="policyProgressPercent"
          :status="policyRuntime.status === 'failed' ? 'exception' : undefined"
          striped
          striped-flow
        />
      </div>

      <div class="policy-behavior" :class="{ 'is-active': policyConfig.enabled || policyConfig.auto_scoring_enabled }">
        <strong>当前行为</strong>
        <span v-if="!policyConfig.auto_scoring_enabled">自动评分和自动调度均关闭；后台不再读取证据或重算健康分。</span>
        <span v-else-if="!policyConfig.enabled">自动评分开启：后台增量读取请求证据、探活并统计各分组正常 OAuth 数量，不修改 Sub2API 账号状态或调度参数。</span>
        <span v-else-if="!policyConfig.return_pool_enabled && !policyConfig.smart_expand_enabled && !policyConfig.load_factor_enabled && !policyConfig.price_protection_enabled">
          四项可选策略均关闭；仍会关闭异常账号，并在账号所属全部分组的正常 OAuth 都超过 {{ policyConfig.oauth_account_threshold }} 个时逐个停止 APIKey 调度。任一分组低于每组最低保障 {{ policyConfig.minimum_available_accounts }} 个时，会将符合条件且未受 OAuth 规则限制的账号逐个重新开启。
        </span>
        <span v-else>每 {{ policyConfig.probe_interval_seconds }} 秒评估托管账号并实时统计正常 OAuth；每个分组独立执行阈值限制、最低保障与健康回池，每轮最多在 Sub2API 关闭或开启 1 个账号的调度。</span>
      </div>

      <div class="policy-strategies">
        <label class="policy-strategy">
          <el-switch
            v-model="policyConfig.auto_scoring_enabled"
            :loading="autoScoringSaving"
            :disabled="policySaving || autoScoringSaving"
            @change="toggleAutomaticScoring"
          />
          <span><strong>自动评分</strong><small>定时获取证据、探活并计算健康分</small></span>
        </label>
        <label class="policy-strategy">
          <el-switch v-model="policyConfig.return_pool_enabled" />
          <span><strong>健康回池</strong><small>每个分组健康可用账号不足时，逐步重新开启该分组的 Sub2API 账号调度，直至每组达到 {{ policyConfig.healthy_target_accounts }} 个</small></span>
        </label>
        <label class="policy-strategy">
          <el-switch v-model="policyConfig.smart_expand_enabled" />
          <span><strong>智能扩容</strong><small>高负载时按 10% 增加并发</small></span>
        </label>
        <label class="policy-strategy">
          <el-switch v-model="policyConfig.load_factor_enabled" />
          <span><strong>负载因子</strong><small>按健康分与上游成本反向分配负载，并同步调整账号优先级</small></span>
        </label>
        <label class="policy-strategy">
          <el-switch v-model="policyConfig.price_protection_enabled" />
          <span><strong>价格保护</strong><small>低于成本与最低利润线时关闭账号调度</small></span>
        </label>
      </div>

      <div class="policy-runtime">
        <div><span>托管账号</span><strong>{{ policySummary.managed_accounts ?? accounts.length }}</strong></div>
        <div>
          <span>健康可用总数（账号去重）</span>
          <strong>{{ policySummary.available_accounts == null ? "尚无数据" : `${policySummary.available_accounts} 个` }}</strong>
          <small>每组最低保障 {{ policyConfig.minimum_available_accounts }} 个 · 每组健康回池目标 {{ policyConfig.healthy_target_accounts }} 个</small>
        </div>
        <div><span>实时并发</span><strong>{{ metricText(policySummary.current_concurrency) }} / {{ metricText(policySummary.capacity) }}</strong></div>
        <div><span>成本绑定</span><strong>{{ policySummary.cost_bound_accounts ?? 0 }} / {{ policySummary.managed_accounts ?? accounts.length }}</strong><small>未绑定 {{ policySummary.cost_unbound_accounts ?? 0 }}</small></div>
        <div><span>价格风险</span><strong>{{ policySummary.price_unsafe_accounts ?? 0 }}</strong><small>过期 {{ policySummary.cost_expired_accounts ?? 0 }} · 下游未知 {{ policySummary.downstream_unknown_accounts ?? 0 }}</small></div>
        <div><span>最近轮次</span><strong>{{ policyRuntime.last_finished_at ? formatTime(policyRuntime.last_finished_at) : "尚未执行" }}</strong></div>
      </div>

      <details class="policy-settings">
        <summary>规则参数</summary>
        <div class="policy-setting-groups">
          <section>
            <h3>池与健康</h3>
            <div class="policy-input-grid">
              <label><span>调度间隔（秒）</span><el-input-number v-model="policyConfig.probe_interval_seconds" :min="5" :step="5" /></label>
              <label><span>健康门槛</span><el-input-number v-model="policyConfig.health_threshold" :min="0" :max="100" /></label>
              <label><span>每组最低保障数</span><el-input-number v-model="policyConfig.minimum_available_accounts" :min="1" /></label>
              <label><span>每组健康回池目标数</span><el-input-number v-model="policyConfig.healthy_target_accounts" :min="1" /></label>
              <label><span>正常 OAuth 停调阈值</span><el-input-number v-model="policyConfig.oauth_account_threshold" :min="1" /></label>
              <label><span>证据有效倍数</span><el-input-number v-model="policyConfig.evidence_ttl_multiplier" :min="1" /></label>
              <label><span>默认探活模型</span><el-input v-model="policyConfig.default_probe_model" clearable placeholder="可选的全局默认模型" /></label>
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
              <label><span>成本权重指数</span><el-input-number v-model="policyConfig.rate_weight_exponent" :min="0" :step="0.1" /></label>
              <label><span>最低利润率 %</span><el-input-number v-model="policyConfig.minimum_profit_margin_percent" :min="0" :max="100" /></label>
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
        <label class="policy-excluded"><span>排除账号 ID</span><el-input v-model="excludedAccountText" placeholder="多个 ID 用英文逗号分隔" /></label>
      </details>

      <div class="policy-rules">
        <p><strong>Sub2API 调度开关</strong><span>认证、余额和用量上限异常时立即关闭账号调度；价格安全且健康达标时，可重新开启系统或人员手动关闭的 active 账号。</span></p>
        <p><strong>OAuth 容量</strong><span>每轮实时统计可调度 OAuth；APIKey 所属全部分组均严格超过阈值时逐个停调，阈值解除后按健康与最低保障规则恢复。</span></p>
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
        <span>健康证据刷新：{{ evidenceRefreshedAt ? formatTime(evidenceRefreshedAt) : "尚未刷新" }}</span>
      </div>
      <div>
        <el-tag size="small" effect="plain">平台：{{ appliedRefreshFilter.platform || "全部" }}</el-tag>
        <el-tag size="small" effect="plain">类型：{{ appliedRefreshFilter.type || "全部" }}</el-tag>
        <el-tag size="small" effect="plain">状态：{{ appliedRefreshFilter.status ? statusText(appliedRefreshFilter.status) : "全部" }}</el-tag>
        <el-tag size="small" effect="plain">未分组：{{ appliedRefreshFilter.include_ungrouped ? "同步" : "不同步" }}</el-tag>
      </div>
    </div>

    <div class="dispatch-summary" v-if="hasCache">
      <div><span>调度账号</span><strong>{{ includedAccounts.length }}</strong></div>
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

    <section v-if="hasCache && excludedAccounts.length" class="dispatch-excluded-accounts">
      <header>
        <strong>已排除账号</strong>
        <el-tag size="small" type="info" effect="plain">{{ excludedAccounts.length }}</el-tag>
      </header>
      <div class="dispatch-excluded-account-grid">
        <div v-for="account in excludedAccounts" :key="account.id" class="dispatch-excluded-account-row">
          <div>
            <strong>{{ account.name || `账号 ${account.id}` }}</strong>
            <span>#{{ account.id }}</span>
            <el-tag v-if="account.platform" size="small" effect="plain">{{ account.platform }}</el-tag>
            <span v-if="account.type">{{ account.type }}</span>
          </div>
          <el-tooltip content="取消排除">
            <el-button
              circle
              text
              :icon="CircleClose"
              :loading="isExcludedAccountUpdating(account)"
              :disabled="dispatchMutationDisabled || policySaving"
              :aria-label="`取消排除 ${account.name || account.id}`"
              @click="restoreExcludedAccount(account)"
            />
          </el-tooltip>
        </div>
      </div>
    </section>

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
            <span v-if="section.group.id">{{ groupOAuthStatusText(section.group) }}</span>
            <el-tag
              v-if="groupOAuthStatistic(section.group)?.threshold_exceeded"
              size="small"
              type="warning"
              effect="plain"
            >
              {{ groupOAuthThresholdText(section.group) }}
            </el-tag>
            <span v-if="groupAvailabilityByKey.get(section.key)" class="group-availability">
              健康可用 {{ groupAvailabilityByKey.get(section.key).available_accounts }} / 每组目标 {{ groupAvailabilityTarget(groupAvailabilityByKey.get(section.key)) }}
            </span>
            <span v-if="section.group.id" class="group-probe-model" :title="groupProbeModel(section.group) || policyConfig.default_probe_model || 'Sub2API 默认'">
              探活：{{ groupProbeModelText(section.group) }}
            </span>
          </div>
          <div class="dispatch-group-actions">
            <el-tooltip v-if="section.group.id" content="设置分组探活模型">
              <el-button
                circle
                text
                :icon="Setting"
                :loading="isGroupProbeModelUpdating(section.group)"
                :disabled="dispatchMutationDisabled || policySaving || isGroupProbeModelUpdating(section.group)"
                :aria-label="`设置分组探活模型 ${section.group.name}`"
                @click="configureGroupProbeModel(section.group)"
              />
            </el-tooltip>
            <el-button
              v-if="section.group.id"
              text
              type="danger"
              size="small"
              :icon="Hide"
              :loading="isUpdatingGroup(section.group)"
              :disabled="dispatchMutationDisabled"
              @click="excludeGroup(section.group)"
            >
              排除
            </el-button>
            <el-button
              v-if="section.key === 'ungrouped'"
              text
              type="danger"
              size="small"
              :icon="Hide"
              :loading="excludingUngrouped"
              :disabled="dispatchMutationDisabled"
              @click="excludeUngroupedAccounts"
            >
              屏蔽
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
                  <span class="probe-model-meta" :title="effectiveProbeModel(account) || 'Sub2API 默认'">
                    探活：{{ probeModelText(account) }}
                  </span>
                </div>
              </div>
              <el-tag :type="statusType(accountFilterStatus(account))" size="small" class="dispatch-account-status">
                {{ statusText(accountFilterStatus(account)) }}
              </el-tag>
            </header>

            <div class="dispatch-account-tools">
              <div v-if="probeTimelineRecords(account).length" class="probe-timeline" aria-label="最近探活时间轴">
                <div class="probe-timeline-bars">
                  <el-tooltip
                    v-for="(record, index) in probeTimelineRecords(account)"
                    :key="record.id || `${evidenceTime(record)}-${index}`"
                    :content="probeTimelineDetail(record)"
                    placement="top"
                    popper-class="probe-timeline-tooltip"
                  >
                    <button
                      type="button"
                      class="probe-timeline-bar"
                      :class="probeSucceeded(record) ? 'is-success' : 'is-failure'"
                      :aria-label="probeTimelineDetail(record)"
                    />
                  </el-tooltip>
                </div>
                <div class="probe-timeline-labels" aria-hidden="true">
                  <span>PAST</span>
                  <span>NOW</span>
                </div>
              </div>
              <div class="dispatch-account-actions">
                <el-tooltip content="单独探活">
                  <el-button
                    circle
                    text
                    :icon="VideoPlay"
                    :loading="isAccountProbing(account)"
                    :disabled="dispatchMutationDisabled"
                    :aria-label="`单独探活 ${account.name}`"
                    @click="probeAccount(account)"
                  />
                </el-tooltip>
                <el-tooltip content="设置探活模型">
                  <el-button
                    circle
                    text
                    :icon="Setting"
                    :loading="isProbeModelUpdating(account)"
                    :disabled="dispatchMutationDisabled || policySaving || isProbeModelUpdating(account)"
                    :aria-label="`设置探活模型 ${account.name}`"
                    @click="configureProbeModel(account)"
                  />
                </el-tooltip>
                <el-tooltip content="排除账号">
                  <el-button
                    circle
                    text
                    type="danger"
                    :icon="Hide"
                    :loading="isExcludedAccountUpdating(account)"
                    :disabled="dispatchMutationDisabled || policySaving || isExcludedAccountUpdating(account)"
                    :aria-label="`排除账号 ${account.name}`"
                    @click="excludeAccount(account)"
                  />
                </el-tooltip>
                <el-tooltip :content="account.cost_binding || account.costBinding ? '修改上游成本绑定' : '绑定上游成本分组'">
                  <el-button
                    circle
                    text
                    :icon="Link"
                    :disabled="dispatchMutationDisabled || costBindingSaving"
                    :aria-label="`设置 ${account.name} 的上游成本绑定`"
                    @click="openCostBindingDialog(account)"
                  />
                </el-tooltip>
                <el-tooltip content="自动调度开启后，价格安全且健康达标的 active 账号可能被重新开启">
                  <el-switch
                    v-model="account.schedulable"
                    inline-prompt
                    :width="78"
                    :active-text="accountSwitchText(account, true)"
                    :inactive-text="accountSwitchText(account, false)"
                    :active-color="accountSwitchColor(account, true)"
                    :inactive-color="accountSwitchColor(account, false)"
                    :loading="isUpdating(account.id)"
                    :disabled="dispatchMutationDisabled || isUpdating(account.id)"
                    :aria-label="`${account.name} 的 Sub2API 调度开关：${accountSwitchText(account, account.schedulable)}`"
                    @change="toggleAccountSchedulable(account, $event)"
                  />
                </el-tooltip>
              </div>
            </div>

            <p
              v-if="account.status === 'error' && account.error_message"
              class="dispatch-account-error"
              :title="account.error_message"
            >
              {{ account.error_message }}
            </p>

            <div class="account-policy-metrics">
              <div>
                <span>健康分</span>
                <el-tag :type="healthType(account.health_score ?? account.healthScore)" size="small">
                  {{ healthText(account) }}
                </el-tag>
                <div v-if="hasHealthComposition(account)" class="health-score-breakdown">
                  <small><strong>短期</strong>{{ healthPartText(account.health_short_score ?? account.healthShortScore, 70) }}</small>
                  <small><strong>长期</strong>{{ healthPartText(account.health_long_score ?? account.healthLongScore, 30) }}</small>
                </div>
                <small :title="evidenceText(account)">{{ evidenceText(account) }}</small>
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
                <span>上游成本倍率</span>
                <strong>{{ metricText(account.upstream_cost_multiplier ?? account.upstreamCostMultiplier) }}</strong>
                <small :title="costBindingName(account)">
                  分组 {{ metricText(account.upstream_group_rate_multiplier ?? account.upstreamGroupRateMultiplier) }} ·
                  {{ account.cost_binding?.recharge_paid_amount ?? account.costBinding?.recharge_paid_amount ?? 1 }}:
                  {{ account.cost_binding?.recharge_received_amount ?? account.costBinding?.recharge_received_amount ?? 1 }}
                </small>
                <small>倍率时间 {{ account.upstream_cost_checked_at || account.upstreamCostCheckedAt ? formatTime(account.upstream_cost_checked_at || account.upstreamCostCheckedAt) : "-" }}</small>
              </div>
              <div>
                <span>本地销售倍率</span>
                <strong>{{ metricText(account.local_min_rate_multiplier ?? account.localMinRateMultiplier) }}</strong>
                <small>安全线 {{ metricText(account.minimum_safe_rate_multiplier ?? account.minimumSafeRateMultiplier) }}</small>
                <el-tag :type="costStatusType(account)" size="small" effect="plain">{{ costStatusText(account) }}</el-tag>
              </div>
              <div>
                <span>优先级</span>
                <strong>{{ metricText(account.priority) }}</strong>
                <small>数值越小越优先</small>
              </div>
              <p
                v-if="account.decision_reason || account.decisionReason"
                :title="`${account.decision_reason || account.decisionReason}${account.last_policy_action_at || account.lastPolicyActionAt ? ` · ${formatTime(account.last_policy_action_at || account.lastPolicyActionAt)}` : ''}`"
              >
                {{ account.decision_reason || account.decisionReason }}
                <span v-if="account.last_policy_action_at || account.lastPolicyActionAt"> · {{ formatTime(account.last_policy_action_at || account.lastPolicyActionAt) }}</span>
              </p>
            </div>

            <details class="short-evidence">
              <summary>
                <span class="short-evidence-summary">
                  <strong>短期健康证据</strong>
                  <template v-if="shortEvidenceRecords(account).length">
                    <el-tag
                      :type="(shortEvidenceRecords(account)[0].source_kind || shortEvidenceRecords(account)[0].sourceKind) === 'error' ? 'danger' : 'success'"
                      size="small"
                    >
                      {{ (shortEvidenceRecords(account)[0].source_kind || shortEvidenceRecords(account)[0].sourceKind) === "error" ? "错误" : "成功" }}
                    </el-tag>
                    <span>{{ evidenceCategoryText(shortEvidenceRecords(account)[0].category) }}</span>
                    <small>
                      {{ Number(shortEvidenceRecords(account)[0].score).toFixed(1) }} · {{ evidenceTime(shortEvidenceRecords(account)[0]) }}
                    </small>
                  </template>
                  <span v-else>暂无记录</span>
                </span>
                <span class="short-evidence-count">{{ shortEvidenceRecords(account).length }} 条</span>
              </summary>
              <div v-if="shortEvidenceRecords(account).length" class="short-evidence-table">
                <div class="short-evidence-head" aria-hidden="true">
                  <span>结果</span><span>分数</span><span>类型</span><span>状态与信息</span><span>时间</span>
                </div>
                <div
                  v-for="record in shortEvidenceRecords(account)"
                  :key="record.id"
                  class="short-evidence-row"
                  :class="{ 'is-error': (record.source_kind || record.sourceKind) === 'error' }"
                >
                  <el-tag :type="(record.source_kind || record.sourceKind) === 'error' ? 'danger' : 'success'" size="small">
                    {{ (record.source_kind || record.sourceKind) === "error" ? "错误" : "使用成功" }}
                  </el-tag>
                  <strong>{{ Number(record.score).toFixed(1) }}</strong>
                  <span>{{ evidenceCategoryText(record.category) }}</span>
                  <span class="short-evidence-message" :title="evidenceDetail(record)">{{ evidenceDetail(record) }}</span>
                  <span>{{ evidenceTime(record) }}</span>
                </div>
              </div>
              <div v-else class="short-evidence-empty">短期证据中暂无使用成功或错误记录</div>
            </details>
          </article>
        </div>
      </section>
    </div>

    <el-empty
      v-else-if="loaded && !loading"
      :description="hasCache ? '没有符合条件的账号' : '暂无本地缓存数据'"
    />

    <el-dialog
      v-model="costBindingDialog"
      :title="`上游成本绑定 · ${costBindingAccount?.name || ''}`"
      width="min(560px, calc(100vw - 24px))"
      destroy-on-close
    >
      <el-form label-position="top">
        <el-form-item label="仪表盘显示">
          <el-select
            v-model="costSourceVisibilityFilter"
            clearable
            :disabled="costSourceLoading || costBindingSaving"
            placeholder="全部"
            style="width: 100%"
          >
            <el-option label="显示在仪表盘" value="visible" />
            <el-option label="未显示在仪表盘" value="hidden" />
          </el-select>
        </el-form-item>
        <el-form-item label="余额监控分组">
          <el-select
            v-model="selectedMonitorGroupId"
            filterable
            :loading="costSourceLoading"
            :disabled="costBindingSaving"
            no-data-text="没有符合筛选条件的分组"
            placeholder="选择账号及监控分组"
            style="width: 100%"
          >
            <el-option-group v-for="group in groupedCostSourceOptions" :key="group.accountId" :label="group.label">
              <el-option
                v-for="option in group.options"
                :key="option.monitor_group_id"
                :label="costOptionLabel(option)"
                :value="option.monitor_group_id"
              />
            </el-option-group>
          </el-select>
        </el-form-item>
        <div v-if="costBindingAccount?.cost_binding || costBindingAccount?.costBinding" class="cost-binding-current">
          <span>当前绑定</span>
          <strong>{{ costBindingName(costBindingAccount) }}</strong>
          <small>最近倍率 {{ costBindingAccount.upstream_cost_checked_at || costBindingAccount.upstreamCostCheckedAt ? formatTime(costBindingAccount.upstream_cost_checked_at || costBindingAccount.upstreamCostCheckedAt) : "-" }}</small>
        </div>
      </el-form>
      <template #footer>
        <div class="dialog-footer cost-binding-footer">
          <el-button
            v-if="costBindingAccount?.cost_binding || costBindingAccount?.costBinding"
            type="danger"
            text
            :icon="Delete"
            :loading="costBindingSaving"
            @click="deleteCostBinding"
          >解除绑定</el-button>
          <span />
          <el-button :disabled="costBindingSaving" @click="costBindingDialog = false">取消</el-button>
          <el-button type="primary" :icon="Check" :loading="costBindingSaving" @click="saveCostBinding">保存</el-button>
        </div>
      </template>
    </el-dialog>

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
        <el-form-item label="同步未分组账号">
          <el-switch
            v-model="refreshForm.include_ungrouped"
            :disabled="startingJob"
            active-text="同步"
            inactive-text="不同步"
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
                :disabled="dispatchMutationDisabled"
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

.policy-progress {
  background: var(--panel-soft);
  border-top: 1px solid var(--line);
  display: grid;
  gap: 8px;
  padding: 11px 18px;
}

.policy-progress-head {
  align-items: center;
  display: flex;
  font-size: 12px;
  gap: 12px;
  justify-content: space-between;
}

.policy-progress-head span {
  color: var(--muted);
  font-variant-numeric: tabular-nums;
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
  grid-template-columns: repeat(5, minmax(0, 1fr));
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
  grid-template-columns: repeat(3, minmax(0, 1fr));
}

.policy-runtime > div {
  border-bottom: 1px solid var(--line);
  border-right: 1px solid var(--line);
  display: grid;
  gap: 3px;
  padding: 11px 16px;
}

.policy-runtime > div:nth-child(3n) {
  border-right: 0;
}

.policy-runtime span {
  color: var(--muted);
  font-size: 11px;
}

.policy-runtime small {
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

.dispatch-excluded-accounts {
  border: 1px solid var(--line);
  border-radius: 8px;
  margin-bottom: 16px;
  overflow: hidden;
}

.dispatch-excluded-accounts > header,
.dispatch-excluded-account-row {
  align-items: center;
  display: flex;
  justify-content: space-between;
}

.dispatch-excluded-accounts > header {
  background: var(--panel-soft);
  padding: 8px 12px;
}

.dispatch-excluded-accounts > header strong {
  font-size: 12px;
}

.dispatch-excluded-account-grid {
  background: var(--panel-soft);
  display: grid;
  gap: 8px;
  grid-template-columns: minmax(0, 1fr);
  padding: 8px;
}

.dispatch-excluded-account-row {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 6px;
  gap: 10px;
  min-height: 44px;
  padding: 6px 8px 6px 12px;
}

.dispatch-excluded-account-row > div {
  align-items: center;
  display: flex;
  flex-wrap: wrap;
  gap: 5px 8px;
  min-width: 0;
}

.dispatch-excluded-account-row strong {
  font-size: 13px;
  overflow-wrap: anywhere;
}

.dispatch-excluded-account-row span {
  color: var(--muted);
  font-size: 12px;
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

.group-probe-model {
  color: var(--muted);
  max-width: min(360px, 100%);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.dispatch-account-grid {
  align-items: start;
  display: grid;
  gap: 12px;
  grid-template-columns: minmax(0, 1fr);
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
  gap: 8px;
  justify-content: space-between;
  padding: 10px 12px 9px;
}

.dispatch-account-identity {
  display: grid;
  gap: 4px;
  min-width: 0;
}

.dispatch-account-name,
.dispatch-account-meta,
.dispatch-account-actions {
  align-items: center;
  display: flex;
  flex-wrap: wrap;
  gap: 5px 7px;
}

.dispatch-account-name h3 {
  display: -webkit-box;
  font-size: 15px;
  line-height: 1.25;
  margin: 0;
  overflow-wrap: anywhere;
  overflow: hidden;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}

.dispatch-account-name span,
.dispatch-account-meta span {
  font-size: 11px;
}

.dispatch-account-status {
  flex: none;
}

.probe-model-meta {
  max-width: min(200px, 100%);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.dispatch-account-tools {
  align-items: center;
  border-bottom: 1px solid var(--line);
  display: flex;
  flex-wrap: wrap;
  gap: 5px 8px;
  justify-content: space-between;
  min-height: 39px;
  padding: 5px 8px 5px 12px;
}

.probe-timeline {
  display: grid;
  flex: none;
  gap: 3px;
  max-width: 100%;
}

.probe-timeline-bars {
  align-items: center;
  display: flex;
  gap: 2px;
  height: 22px;
}

.probe-timeline-bar {
  appearance: none;
  border: 0;
  border-radius: 1px;
  cursor: help;
  flex: 0 0 4px;
  height: 20px;
  margin: 0;
  padding: 0;
  transition: height 120ms ease, filter 120ms ease;
  width: 4px;
}

.probe-timeline-bar.is-success {
  background: var(--success);
}

.probe-timeline-bar.is-failure {
  background: var(--danger);
}

.probe-timeline-bar:hover,
.probe-timeline-bar:focus-visible {
  filter: brightness(0.86);
  height: 22px;
  outline: 2px solid var(--text);
  outline-offset: 1px;
}

.probe-timeline-labels {
  color: var(--muted);
  display: flex;
  font-size: 8px;
  justify-content: space-between;
  letter-spacing: 0;
  line-height: 1;
}

:global(.probe-timeline-tooltip) {
  max-width: min(420px, calc(100vw - 24px));
  overflow-wrap: anywhere;
}

.dispatch-account-actions {
  flex: none;
  margin-left: auto;
}

.dispatch-account-actions :deep(.el-button.is-circle) {
  height: 28px;
  padding: 6px;
  width: 28px;
}

.dispatch-account-error {
  background: #fff5f4;
  border-bottom: 1px solid #fecdca;
  color: var(--danger);
  display: -webkit-box;
  font-size: 12px;
  line-height: 1.4;
  margin: 0;
  overflow: hidden;
  overflow-wrap: anywhere;
  padding: 7px 12px;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}

.short-evidence {
  background: var(--panel);
}

.short-evidence > summary {
  align-items: center;
  cursor: pointer;
  display: grid;
  gap: 8px;
  grid-template-columns: 10px minmax(0, 1fr) auto;
  list-style: none;
  min-height: 36px;
  padding: 6px 10px;
}

.short-evidence > summary::-webkit-details-marker {
  display: none;
}

.short-evidence > summary::before {
  border-bottom: 1.5px solid var(--muted);
  border-right: 1.5px solid var(--muted);
  content: "";
  height: 5px;
  transform: rotate(-45deg);
  transition: transform 120ms ease;
  width: 5px;
}

.short-evidence[open] > summary::before {
  transform: rotate(45deg) translate(-1px, -1px);
}

.short-evidence[open] > summary {
  border-bottom: 1px solid var(--line);
}

.short-evidence-summary {
  align-items: center;
  display: flex;
  gap: 5px 7px;
  min-width: 0;
  overflow: hidden;
}

.short-evidence-summary strong {
  flex: none;
  font-size: 12px;
}

.short-evidence-summary > span,
.short-evidence-summary > small,
.short-evidence-count,
.short-evidence-empty {
  color: var(--muted);
  font-size: 11px;
}

.short-evidence-summary > span,
.short-evidence-summary > small {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.short-evidence-summary > small {
  min-width: 0;
}

.short-evidence-count {
  white-space: nowrap;
}

.short-evidence-table {
  max-height: 260px;
  overflow: auto;
}

.short-evidence-head,
.short-evidence-row {
  align-items: center;
  display: grid;
  gap: 8px;
  grid-template-columns: 68px 58px 90px minmax(180px, 1fr) 145px;
  min-width: 620px;
  padding: 6px 10px;
}

.short-evidence-head {
  background: var(--panel-soft);
  border-bottom: 1px solid var(--line);
  color: var(--muted);
  font-size: 11px;
  font-weight: 700;
  position: sticky;
  top: 0;
  z-index: 1;
}

.short-evidence-row {
  border-bottom: 1px solid var(--line);
  font-size: 12px;
}

.short-evidence-row:last-child {
  border-bottom: 0;
}

.short-evidence-row > strong {
  font-variant-numeric: tabular-nums;
}

.short-evidence-row > span {
  min-width: 0;
}

.short-evidence-row.is-error {
  background: #fffafa;
}

.short-evidence-message {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.short-evidence-empty {
  background: var(--panel-soft);
  padding: 10px 12px;
}

.account-policy-metrics {
  background: var(--panel-soft);
  border-bottom: 1px solid var(--line);
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
}

.account-policy-metrics > div {
  align-content: start;
  display: grid;
  gap: 2px;
  min-width: 0;
  padding: 8px 10px;
}

.account-policy-metrics > div:nth-child(odd) {
  border-right: 1px solid var(--line);
}

.account-policy-metrics > div:not(:nth-last-child(-n + 2)) {
  border-bottom: 1px solid var(--line);
}

.account-policy-metrics span,
.account-policy-metrics small {
  color: var(--muted);
  font-size: 10px;
  line-height: 1.35;
  min-width: 0;
  overflow: hidden;
  overflow-wrap: anywhere;
  text-overflow: ellipsis;
}

.account-policy-metrics strong {
  font-size: 13px;
}

.health-score-breakdown {
  display: flex;
  flex-wrap: wrap;
  gap: 1px 7px;
  margin-top: 1px;
}

.health-score-breakdown small {
  font-variant-numeric: tabular-nums;
}

.health-score-breakdown strong {
  color: var(--text);
  display: inline-block;
  font-size: 11px;
  margin-right: 4px;
  min-width: 24px;
}

.account-policy-metrics > p {
  color: var(--muted);
  display: -webkit-box;
  font-size: 11px;
  grid-column: 1 / -1;
  line-height: 1.4;
  margin: 0;
  overflow: hidden;
  overflow-wrap: anywhere;
  padding: 7px 10px;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
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

.cost-binding-current {
  background: var(--panel-soft);
  border: 1px solid var(--line);
  border-radius: 6px;
  display: grid;
  gap: 3px;
  padding: 10px 12px;
}

.cost-binding-current span,
.cost-binding-current small {
  color: var(--muted);
  font-size: 12px;
}

.cost-binding-current strong {
  font-size: 13px;
  overflow-wrap: anywhere;
}

.cost-binding-footer {
  align-items: center;
  display: grid;
  gap: 8px;
  grid-template-columns: auto 1fr auto auto;
}

@media (min-width: 768px) {
  .dispatch-excluded-account-grid,
  .dispatch-account-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (min-width: 1100px) {
  .dispatch-excluded-account-grid,
  .dispatch-account-grid {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
}

@media (min-width: 1500px) {
  .dispatch-excluded-account-grid,
  .dispatch-account-grid {
    grid-template-columns: repeat(4, minmax(0, 1fr));
  }
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
    padding: 10px 12px 9px;
  }

  .dispatch-group-head {
    align-items: flex-start;
    gap: 8px;
  }

  .short-evidence-summary > small {
    display: none;
  }

  .cost-binding-footer {
    grid-template-columns: 1fr 1fr;
  }

  .cost-binding-footer > span {
    display: none;
  }

  .cost-binding-footer :deep(.el-button) {
    margin: 0;
    width: 100%;
  }

}
</style>

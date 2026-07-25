<script setup>
import { computed, onMounted, reactive, ref } from "vue";
import { ElMessage } from "element-plus";
import { ArrowDown, ArrowRight, Refresh, RefreshLeft, Search } from "@element-plus/icons-vue";
import { api } from "../api";
import { formatTime } from "../utils";

const loading = ref(false);
const loaded = ref(false);
const accounts = ref([]);
const groups = ref([]);
const warnings = ref([]);
const siteUrl = ref("");
const collapsedGroups = ref(new Set());
const updatingIds = ref(new Set());
const filters = reactive({
  search: "",
  platform: "",
  status: "",
  group_id: ""
});

const platformOptions = computed(() => {
  return [...new Set(accounts.value.map((account) => account.platform).filter(Boolean))].sort();
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
    if (filters.status && account.status !== filters.status) return false;
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

async function loadDispatch() {
  loading.value = true;
  try {
    const payload = await api.platformDispatch();
    accounts.value = (payload.accounts || []).map((account) => ({
      ...account,
      is_enabled: account.is_enabled ?? account.isEnabled ?? account.status === "active",
      recent_activity: account.recent_activity || account.recentActivity || []
    }));
    groups.value = payload.groups || [];
    warnings.value = payload.warnings || [];
    siteUrl.value = payload.site_url || payload.siteUrl || "";
    loaded.value = true;
  } catch (error) {
    ElMessage.error(error.message || "加载平台调度数据失败");
  } finally {
    loading.value = false;
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
  Object.assign(filters, { search: "", platform: "", status: "", group_id: "" });
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
  return "info";
}

function statusText(status) {
  if (status === "active") return "已启用";
  if (status === "error") return "异常";
  return "已停用";
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

function isCollapsed(key) {
  return collapsedGroups.value.has(key);
}

function toggleCollapsed(key) {
  const next = new Set(collapsedGroups.value);
  if (next.has(key)) next.delete(key);
  else next.add(key);
  collapsedGroups.value = next;
}

onMounted(loadDispatch);
</script>

<template>
  <section v-loading="loading" class="dispatch-page">
    <div class="page-head">
      <div>
        <h1>平台调度</h1>
        <p>Sub2API 账号与最近调度记录<span v-if="siteUrl"> · {{ siteUrl }}</span></p>
      </div>
      <div class="page-actions">
        <el-button :icon="Refresh" :loading="loading" @click="loadDispatch">刷新</el-button>
      </div>
    </div>

    <el-alert
      v-for="warning in warnings"
      :key="warning"
      :title="warning"
      type="warning"
      :closable="false"
      show-icon
      class="dispatch-alert"
    />

    <div class="dispatch-summary" v-if="loaded">
      <div><span>账号</span><strong>{{ accounts.length }}</strong></div>
      <div><span>启用</span><strong class="summary-success">{{ activeCount }}</strong></div>
      <div><span>异常</span><strong class="summary-danger">{{ errorCount }}</strong></div>
      <div><span>当前筛选</span><strong>{{ filteredAccounts.length }}</strong></div>
    </div>

    <el-form class="toolbar dispatch-toolbar" inline label-position="top">
      <el-form-item label="账号">
        <el-input v-model="filters.search" :prefix-icon="Search" clearable placeholder="名称或 ID" />
      </el-form-item>
      <el-form-item label="平台">
        <el-select v-model="filters.platform" clearable placeholder="全部平台">
          <el-option v-for="platform in platformOptions" :key="platform" :label="platform" :value="platform" />
        </el-select>
      </el-form-item>
      <el-form-item label="状态">
        <el-select v-model="filters.status" clearable placeholder="全部状态">
          <el-option label="已启用" value="active" />
          <el-option label="已停用" value="inactive" />
          <el-option label="异常" value="error" />
        </el-select>
      </el-form-item>
      <el-form-item label="分组">
        <el-select v-model="filters.group_id" clearable filterable placeholder="全部分组">
          <el-option v-for="group in groupOptions" :key="group.id" :label="group.name" :value="String(group.id)" />
          <el-option label="未分组" value="ungrouped" />
        </el-select>
      </el-form-item>
      <el-form-item class="dispatch-reset-item">
        <el-button :icon="RefreshLeft" @click="resetFilters">重置</el-button>
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
          </div>
          <el-tooltip :content="isCollapsed(section.key) ? '展开分组' : '收起分组'">
            <el-button
              circle
              text
              :icon="isCollapsed(section.key) ? ArrowRight : ArrowDown"
              @click="toggleCollapsed(section.key)"
            />
          </el-tooltip>
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
                <el-tag :type="statusType(account.status)" size="small">{{ statusText(account.status) }}</el-tag>
                <el-switch
                  v-model="account.is_enabled"
                  :loading="isUpdating(account.id)"
                  :disabled="isUpdating(account.id)"
                  :aria-label="`${account.name} 启用状态`"
                  @change="toggleAccount(account, $event)"
                />
              </div>
            </header>

            <p v-if="account.status === 'error' && account.error_message" class="dispatch-account-error">
              {{ account.error_message }}
            </p>

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

    <el-empty v-else-if="loaded && !loading" description="没有符合条件的账号" />
  </section>
</template>

<style scoped>
.dispatch-alert {
  margin-bottom: 12px;
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

@media (max-width: 760px) {
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

  .dispatch-account-state {
    align-items: flex-end;
    flex-direction: column;
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

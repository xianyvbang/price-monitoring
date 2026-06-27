<script setup>
import { computed, onMounted, reactive, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { CopyDocument, Delete, Edit, Key, Plus, Refresh, Timer } from "@element-plus/icons-vue";
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
const historyVisible = ref(false);
const historyLoading = ref(false);
const historyAccount = ref(null);
const historyRecords = ref([]);
const formRef = ref(null);
const form = reactive(defaultForm());
const { isMobile } = useViewport();

const accountCount = computed(() => summary.value.account_count ?? summary.value.accountCount ?? accounts.value.length);
const lastSuccessAt = computed(() => summary.value.last_success_at ?? summary.value.lastSuccessAt);

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
    accounts.value = payload.accounts || [];
    summary.value = payload.summary || {};
  } catch (error) {
    ElMessage.error(error.message || "加载 OpenCode Go 账号失败");
  } finally {
    loading.value = false;
  }
}

function defaultForm() {
  return {
    id: "",
    name: "",
    email: "",
    password: "",
    workspace_id: "",
    is_enabled: true
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

function upsertLocal(account) {
  if (!account) {
    loadAccounts();
    return;
  }
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

async function loginAccount(account) {
  account._loggingIn = true;
  try {
    const response = await api.loginOpencodeGo(account.id);
    upsertLocal(response.account);
    ElMessage.success("登录成功");
  } catch (error) {
    ElMessage.error(error.message || "登录失败");
  } finally {
    account._loggingIn = false;
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
    await navigator.clipboard.writeText(keyValue);
    ElMessage.success("API key 已复制");
  } catch (error) {
    ElMessage.error(error.message || "复制失败");
  }
}

async function openHistory(account) {
  historyVisible.value = true;
  historyLoading.value = true;
  historyAccount.value = account;
  historyRecords.value = [];
  try {
    const payload = await api.opencodeGoHistory(account.id, { limit: 100 });
    historyRecords.value = payload.records || [];
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
  return account[key] || account[toCamel(key)] || {};
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

onMounted(loadAccounts);
</script>

<template>
  <section v-loading="loading">
    <div class="page-head">
      <div>
        <h1>OpenCode Go</h1>
        <p>{{ accountCount }} 个账号，最近成功 {{ formatTime(lastSuccessAt) }}</p>
      </div>
      <div class="page-actions">
        <el-button type="primary" :icon="Plus" @click="openCreate">添加账号</el-button>
        <el-button :icon="Refresh" :loading="refreshingAll" @click="refreshAll">刷新全部</el-button>
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
            <template #default="{ row }"><UsageBar :value="usageCell(row, 'rolling_usage')" /></template>
          </el-table-column>
          <el-table-column label="7d 用量" min-width="160">
            <template #default="{ row }"><UsageBar :value="usageCell(row, 'weekly_usage')" /></template>
          </el-table-column>
          <el-table-column label="30d 用量" min-width="160">
            <template #default="{ row }"><UsageBar :value="usageCell(row, 'monthly_usage')" /></template>
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
          <el-table-column label="操作" min-width="360" fixed="right">
            <template #default="{ row }">
              <div class="table-actions">
                <el-button size="small" :icon="Edit" @click="openEdit(row)">编辑</el-button>
                <el-button size="small" :icon="Key" :loading="row._loggingIn" @click="loginAccount(row)">登录</el-button>
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
            <el-button size="small" :icon="Key" :loading="row._loggingIn" @click="loginAccount(row)">登录</el-button>
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

<script>
export default {
  components: {
    UsageBar: {
      props: {
        value: {
          type: Object,
          default: () => ({})
        }
      },
      methods: {
        percent() {
          const raw = this.value?.usage_percent ?? this.value?.usagePercent;
          const number = Number(raw);
          return Number.isFinite(number) ? Math.max(0, Math.min(100, number)) : 0;
        },
        label() {
          const raw = this.value?.usage_percent ?? this.value?.usagePercent;
          return raw === null || raw === undefined ? "-" : `${Number(raw).toFixed(1)}%`;
        },
        color() {
          const percent = this.percent();
          if (percent >= 90) return "#d92d20";
          if (percent >= 70) return "#b54708";
          return "#07845f";
        }
      },
      template: `
        <div class="usage-bar">
          <el-progress :percentage="percent()" :stroke-width="8" :color="color()" :show-text="false" />
          <span>{{ label() }}</span>
        </div>
      `
    }
  }
};
</script>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";
import { Money, Refresh, VideoPause, VideoPlay } from "@element-plus/icons-vue";
import { api } from "../api";
import GroupPickerDialog from "../components/GroupPickerDialog.vue";
import { amountWithUnit, boolValue, displayValue, formatTime } from "../utils";

const route = useRoute();
const router = useRouter();
const loading = ref(false);
const grouped = ref({ newApi: [], sub2Api: [] });
const settings = ref({ query_interval: 300, request_timeout: 10, default_threshold: 5, monitor_paused: false });
const summaries = ref([]);
const filter = reactive({
  name: String(route.query.name || ""),
  platform: String(route.query.platform || "")
});
const queryAllLoading = ref(false);
const refreshRemaining = ref(300);
const timer = ref(null);
const groupPicker = ref(null);
const balanceDialogVisible = ref(false);
const balanceLoading = ref(false);
const balanceAccount = ref(null);
const balanceRecords = ref([]);
const chartCanvas = ref(null);

const platformEntries = computed(() => Object.entries(grouped.value).filter(([, rows]) => Array.isArray(rows)));
const monitorPaused = computed(() => boolValue(settings.value.monitor_paused));

async function loadDashboard() {
  loading.value = true;
  try {
    const payload = await api.dashboard(filter);
    grouped.value = payload.grouped || {};
    settings.value = payload.settings || settings.value;
    summaries.value = payload.consumption_summaries || [];
    refreshRemaining.value = Math.max(300, Number(settings.value.query_interval || 300));
  } catch (error) {
    if (error.status === 401) {
      await router.replace({ name: "login", query: { redirect: route.fullPath } });
      return;
    }
    ElMessage.error(error.message || "加载仪表盘失败");
  } finally {
    loading.value = false;
  }
}

async function applyFilter() {
  await router.replace({ path: "/", query: { ...filter } });
  await loadDashboard();
}

async function resetFilter() {
  filter.name = "";
  filter.platform = "";
  await applyFilter();
}

function allRowsForAccount(accountId) {
  return Object.values(grouped.value).flatMap((rows) => rows.filter((row) => String(row.id) === String(accountId)));
}

function replaceAccountRows(account) {
  Object.entries(grouped.value).forEach(([platform, rows]) => {
    grouped.value[platform] = rows.map((row) => (String(row.id) === String(account.id) ? { ...row, ...account } : row));
  });
}

function accountCount(rows) {
  return rows.filter((row) => row.dashboard_is_first_row).length;
}

function groupRateText(row) {
  const rates = row.group_rates || [];
  if (!rates.length) {
    return ["-"];
  }
  return rates.map((rate) => `${rate.plan_name || rate.group_id || "-"}: ${displayValue(rate.rate_multiplier)}`);
}

function lowBalanceText(row) {
  if (row.is_eliminated) {
    return "不提醒";
  }
  const threshold = row.threshold ?? settings.value.default_threshold;
  return row.last_remaining !== null && row.last_remaining !== undefined && Number(row.last_remaining) < Number(threshold) ? "是" : "否";
}

async function runQueryAll(trigger = "manual") {
  if (queryAllLoading.value || (trigger === "auto" && monitorPaused.value)) {
    return;
  }
  queryAllLoading.value = true;
  try {
    const payload = await api.queryAll();
    const results = payload.results || [];
    results.forEach((result) => {
      allRowsForAccount(result.account_id || result.accountId).forEach((row) => {
        row.last_status = result.is_valid ? "valid" : "invalid";
        row.last_remaining = result.remaining;
        row.last_unit = result.unit || row.last_unit;
        row.last_used = result.used;
        row.last_total = result.total;
        row.today_consumption = result.consumption_stats?.today ?? result.today_consumption ?? row.today_consumption;
        row.actual_today_consumption = result.actual_consumption_stats?.today ?? result.actual_today_consumption ?? row.actual_today_consumption;
        row.last_checked_at = result.checked_at || result.checkedAt || row.last_checked_at;
      });
    });
    ElMessage.success(trigger === "auto" ? "自动刷新完成" : "查询全部完成");
    refreshRemaining.value = Math.max(300, Number(settings.value.query_interval || 300));
  } catch (error) {
    ElMessage.error(error.message || "查询全部失败");
  } finally {
    queryAllLoading.value = false;
  }
}

async function queryOne(row) {
  row._querying = true;
  try {
    const result = await api.queryAccount(row.id);
    allRowsForAccount(row.id).forEach((target) => {
      target.last_status = result.is_valid ? "valid" : "invalid";
      target.last_remaining = result.remaining;
      target.last_unit = result.unit || target.last_unit;
      target.last_used = result.used;
      target.last_total = result.total;
      target.today_consumption = result.consumption_stats?.today ?? result.today_consumption ?? target.today_consumption;
      target.actual_today_consumption = result.actual_consumption_stats?.today ?? result.actual_today_consumption ?? target.actual_today_consumption;
      target.last_checked_at = result.checked_at || result.checkedAt || target.last_checked_at;
    });
    ElMessage.success("查询完成");
  } catch (error) {
    ElMessage.error(error.message || "查询失败");
  } finally {
    row._querying = false;
  }
}

async function queryGroup(row) {
  row._groupQuerying = true;
  try {
    const result = await api.queryGroup(row.id);
    if (result.extra) {
      await loadDashboard();
    }
    ElMessage.success(result.is_valid === false ? "查组失败" : "查组完成");
  } catch (error) {
    ElMessage.error(error.message || "查组失败");
  } finally {
    row._groupQuerying = false;
  }
}

async function toggleEliminated(row) {
  const next = !boolValue(row.is_eliminated);
  try {
    const payload = await api.setEliminated(row.id, next);
    replaceAccountRows(payload.account);
    ElMessage.success("淘汰状态已保存");
  } catch (error) {
    ElMessage.error(error.message || "保存失败");
  }
}

async function toggleEnabled(row) {
  const next = !boolValue(row.is_enabled);
  try {
    const payload = await api.setEnabled(row.id, next);
    replaceAccountRows(payload.account);
    ElMessage.success("自动查询状态已保存");
  } catch (error) {
    ElMessage.error(error.message || "保存失败");
  }
}

async function toggleMonitor() {
  try {
    const payload = await api.pauseMonitor(!monitorPaused.value);
    settings.value = payload.settings;
    refreshRemaining.value = Math.max(300, Number(settings.value.query_interval || 300));
  } catch (error) {
    ElMessage.error(error.message || "操作失败");
  }
}

async function resetGroupRate(row) {
  try {
    await api.setGroupRateChange(row.id, {
      changed: false,
      monitor_group_id: row.current_monitor_group_id
    });
    await loadDashboard();
    ElMessage.success("倍率变化状态已重置");
  } catch (error) {
    ElMessage.error(error.message || "重置失败");
  }
}

async function fetchGroups(row) {
  row._fetchingGroups = true;
  try {
    const payload = row.platform === "sub2Api" ? await api.sub2ApiGroups(row.id) : await api.newApiGroups(row.id);
    groupPicker.value.open({
      accountId: row.id,
      platform: row.platform,
      groups: payload.groups || [],
      selected: payload.selected_group_ids ?? payload.selectedGroupIds ?? payload.selected_group_id ?? payload.selectedGroupId
    });
  } catch (error) {
    ElMessage.error(error.message || "获取分组失败");
  } finally {
    row._fetchingGroups = false;
  }
}

async function openBalanceHistory(row) {
  balanceDialogVisible.value = true;
  balanceLoading.value = true;
  balanceAccount.value = row;
  balanceRecords.value = [];
  try {
    const payload = await api.balanceHistory(row.id);
    balanceRecords.value = payload.records || [];
    await nextTick();
    drawChart();
  } catch (error) {
    ElMessage.error(error.message || "获取余额趋势失败");
  } finally {
    balanceLoading.value = false;
  }
}

async function clearBalanceHistory() {
  if (!balanceAccount.value) {
    return;
  }
  await ElMessageBox.confirm("确定清除这条平台的余额历史吗？", "清除余额历史", { type: "warning" });
  await api.clearBalanceHistory(balanceAccount.value.id);
  balanceRecords.value = [];
  drawChart();
  ElMessage.success("余额历史已清除");
}

function drawChart() {
  const canvas = chartCanvas.value;
  if (!canvas) {
    return;
  }
  const rect = canvas.getBoundingClientRect();
  const scale = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.floor(rect.width * scale));
  canvas.height = Math.max(1, Math.floor(rect.height * scale));
  const ctx = canvas.getContext("2d");
  ctx.scale(scale, scale);
  ctx.clearRect(0, 0, rect.width, rect.height);
  const records = balanceRecords.value
    .map((record) => ({ ...record, remaining: Number(record.remaining), time: new Date(record.checked_at).getTime() }))
    .filter((record) => Number.isFinite(record.remaining) && Number.isFinite(record.time))
    .sort((a, b) => a.time - b.time);
  if (!records.length) {
    return;
  }
  const padding = { top: 24, right: 20, bottom: 34, left: 54 };
  const width = rect.width - padding.left - padding.right;
  const height = rect.height - padding.top - padding.bottom;
  const minValue = Math.min(...records.map((record) => record.remaining));
  const maxValue = Math.max(...records.map((record) => record.remaining));
  const minTime = records[0].time;
  const maxTime = records[records.length - 1].time;
  const valueSpan = maxValue - minValue || 1;
  const timeSpan = maxTime - minTime || 1;
  const x = (record) => padding.left + ((record.time - minTime) / timeSpan) * width;
  const y = (record) => padding.top + (1 - (record.remaining - minValue) / valueSpan) * height;
  ctx.strokeStyle = "#d0d7e2";
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(padding.left, padding.top);
  ctx.lineTo(padding.left, padding.top + height);
  ctx.lineTo(padding.left + width, padding.top + height);
  ctx.stroke();
  ctx.strokeStyle = "#2563eb";
  ctx.lineWidth = 2;
  ctx.beginPath();
  records.forEach((record, index) => {
    if (index === 0) ctx.moveTo(x(record), y(record));
    else ctx.lineTo(x(record), y(record));
  });
  ctx.stroke();
  ctx.fillStyle = "#2563eb";
  records.forEach((record) => {
    ctx.beginPath();
    ctx.arc(x(record), y(record), 3, 0, Math.PI * 2);
    ctx.fill();
  });
  ctx.fillStyle = "#667085";
  ctx.font = "12px sans-serif";
  ctx.fillText(String(maxValue), 8, padding.top + 4);
  ctx.fillText(String(minValue), 8, padding.top + height);
}

onMounted(async () => {
  await loadDashboard();
  timer.value = window.setInterval(() => {
    if (queryAllLoading.value || monitorPaused.value) {
      return;
    }
    refreshRemaining.value -= 1;
    if (refreshRemaining.value <= 0) {
      runQueryAll("auto");
    }
  }, 1000);
  window.addEventListener("resize", drawChart);
});

onBeforeUnmount(() => {
  if (timer.value) {
    window.clearInterval(timer.value);
  }
  window.removeEventListener("resize", drawChart);
});
</script>

<template>
  <section v-loading="loading">
    <div class="page-head">
      <div>
        <h1>余额仪表盘</h1>
        <p>
          自动查询间隔 {{ settings.query_interval }} 秒，请求超时 {{ settings.request_timeout }} 秒。
          <span>{{ monitorPaused ? "自动监控已暂停" : `下次自动刷新 ${refreshRemaining} 秒` }}</span>
        </p>
      </div>
      <div class="page-actions">
        <el-button type="primary" :icon="Refresh" :loading="queryAllLoading" @click="runQueryAll('manual')">立即查询全部</el-button>
        <el-button :type="monitorPaused ? 'warning' : 'default'" :icon="monitorPaused ? VideoPlay : VideoPause" @click="toggleMonitor">
          {{ monitorPaused ? "恢复监控" : "暂停监控" }}
        </el-button>
      </div>
    </div>

    <el-form class="toolbar" :inline="true" @submit.prevent="applyFilter">
      <el-form-item label="名称">
        <el-input v-model="filter.name" placeholder="模糊搜索" clearable />
      </el-form-item>
      <el-form-item label="平台">
        <el-select v-model="filter.platform" placeholder="全部平台" clearable style="width: 160px">
          <el-option label="newApi" value="newApi" />
          <el-option label="sub2Api" value="sub2Api" />
        </el-select>
      </el-form-item>
      <el-form-item>
        <el-button type="primary" native-type="submit">筛选</el-button>
        <el-button @click="resetFilter">清除</el-button>
      </el-form-item>
    </el-form>

    <div class="stat-grid">
      <div v-for="summary in summaries" :key="summary.key" class="stat-card">
        <span>{{ summary.label }}</span>
        <strong>
          <template v-if="summary.totals?.length">
            {{ summary.totals.map((item) => `${item.amount}${item.unit ? ` ${item.unit}` : ''}`).join(" / ") }}
          </template>
          <template v-else>-</template>
        </strong>
        <small>{{ summary.account_count }} {{ summary.count_label }}</small>
      </div>
    </div>

    <div v-for="[platform, rows] in platformEntries" :key="platform" class="panel table-card">
      <div class="panel-head">
        <h2>{{ platform }}</h2>
        <el-tag>{{ accountCount(rows) }} 个账号<span v-if="rows.length !== accountCount(rows)"> / {{ rows.length }} 个分组</span></el-tag>
      </div>
      <el-table :data="rows" border stripe row-key="dashboard_row_id" style="width: 100%">
        <el-table-column label="名称" min-width="150" fixed>
          <template #default="{ row }">
            <strong v-if="row.dashboard_is_first_row">{{ row.name }}</strong>
          </template>
        </el-table-column>
        <el-table-column label="备注" min-width="170">
          <template #default="{ row }">
            <span v-if="row.dashboard_is_first_row" class="note-text">{{ row.note || "-" }}</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="105">
          <template #default="{ row }">
            <el-tag v-if="row.dashboard_is_first_row" class="status-tag" :type="row.last_status === 'valid' ? 'success' : row.last_status === 'invalid' ? 'danger' : 'info'">
              {{ row.last_status }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="分组倍率" min-width="190">
          <template #default="{ row }">
            <div class="group-rate-list">
              <span v-for="text in groupRateText(row)" :key="text">{{ text }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column v-if="platform === 'newApi' || platform === 'sub2Api'" label="倍率变化" width="130">
          <template #default="{ row }">
            <el-tag :type="row.last_group_rate_changed ? 'danger' : 'info'">{{ row.last_group_rate_changed ? "变化" : "未变化" }}</el-tag>
            <el-button v-if="row.last_group_rate_changed" link type="primary" @click="resetGroupRate(row)">重置</el-button>
          </template>
        </el-table-column>
        <el-table-column label="剩余" width="125">
          <template #default="{ row }">
            <span v-if="row.dashboard_is_first_row">{{ amountWithUnit(row.last_remaining, row.last_unit || 'USD') }}</span>
          </template>
        </el-table-column>
        <el-table-column label="今日消耗" width="125">
          <template #default="{ row }">
            <span v-if="row.dashboard_is_first_row">{{ amountWithUnit(row.today_consumption, row.last_unit || 'USD') }}</span>
          </template>
        </el-table-column>
        <el-table-column label="今日实际消耗" width="140">
          <template #default="{ row }">
            <span v-if="row.dashboard_is_first_row">{{ amountWithUnit(row.actual_today_consumption, row.last_unit || 'USD') }}</span>
          </template>
        </el-table-column>
        <el-table-column v-if="platform !== 'sub2Api'" label="已用" width="105">
          <template #default="{ row }">
            <span v-if="row.dashboard_is_first_row">{{ displayValue(row.last_used) }}</span>
          </template>
        </el-table-column>
        <el-table-column v-if="platform !== 'sub2Api'" label="总额" width="105">
          <template #default="{ row }">
            <span v-if="row.dashboard_is_first_row">{{ displayValue(row.last_total) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="阈值" width="90">
          <template #default="{ row }">
            <span v-if="row.dashboard_is_first_row">{{ row.threshold ?? settings.default_threshold }}</span>
          </template>
        </el-table-column>
        <el-table-column label="低于阈值" width="105">
          <template #default="{ row }">
            <span v-if="row.dashboard_is_first_row" :class="{ 'low-balance': lowBalanceText(row) === '是' }">{{ lowBalanceText(row) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="自动查询" width="105">
          <template #default="{ row }">
            <el-switch v-if="row.dashboard_is_first_row" :model-value="boolValue(row.is_enabled)" @change="toggleEnabled(row)" />
          </template>
        </el-table-column>
        <el-table-column label="是否淘汰" width="105">
          <template #default="{ row }">
            <el-switch v-if="row.dashboard_is_first_row" :model-value="boolValue(row.is_eliminated)" @change="toggleEliminated(row)" />
          </template>
        </el-table-column>
        <el-table-column label="最近查询" width="165">
          <template #default="{ row }">
            <span v-if="row.dashboard_is_first_row">{{ formatTime(row.last_checked_at) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" min-width="300" fixed="right">
          <template #default="{ row }">
            <div class="table-actions">
              <template v-if="row.dashboard_is_first_row">
                <el-button size="small" tag="a" :href="row.base_url" target="_blank">打开</el-button>
                <el-button v-if="row.recharge_url" size="small" tag="a" :href="row.recharge_url" target="_blank">充值</el-button>
                <el-button size="small" :loading="row._querying" @click="queryOne(row)">查询</el-button>
                <el-button size="small" @click="router.push({ path: '/accounts', query: { edit_id: row.id } })">修改</el-button>
                <el-button size="small" :icon="Money" @click="openBalanceHistory(row)">余额趋势</el-button>
                <el-button v-if="row.platform === 'newApi' || row.platform === 'sub2Api'" size="small" :loading="row._fetchingGroups" @click="fetchGroups(row)">
                  {{ row.platform === "newApi" ? "重新获取分组" : "选择分组" }}
                </el-button>
              </template>
              <el-button v-if="row.platform === 'newApi' || row.platform === 'sub2Api'" size="small" :loading="row._groupQuerying" @click="queryGroup(row)">查组</el-button>
              <el-button v-if="row.platform === 'newApi' || row.platform === 'sub2Api'" size="small" @click="router.push({ name: 'group-rates', params: { id: row.id }, query: row.current_monitor_group_id ? { monitor_group_id: row.current_monitor_group_id } : {} })">分组变化</el-button>
            </div>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <GroupPickerDialog ref="groupPicker" @saved="loadDashboard" />

    <el-dialog v-model="balanceDialogVisible" title="余额趋势" width="860px" @opened="drawChart">
      <p class="muted">{{ balanceAccount?.platform }} / {{ balanceAccount?.name }} · 最近 3 天</p>
      <div v-loading="balanceLoading" class="chart-box">
        <canvas ref="chartCanvas"></canvas>
        <div v-if="!balanceRecords.length" class="empty-chart">暂无余额数据</div>
      </div>
      <template #footer>
        <div class="dialog-footer">
          <el-button type="danger" :disabled="!balanceRecords.length" @click="clearBalanceHistory">清除</el-button>
          <el-button @click="balanceDialogVisible = false">关闭</el-button>
        </div>
      </template>
    </el-dialog>
  </section>
</template>

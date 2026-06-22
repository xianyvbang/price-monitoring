<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";
import { Money, Operation, Refresh, VideoPause, VideoPlay } from "@element-plus/icons-vue";
import { api } from "../api";
import GroupPickerDialog from "../components/GroupPickerDialog.vue";
import { useViewport } from "../composables/useViewport";
import { boolValue, displayValue, formatTime } from "../utils";

const route = useRoute();
const router = useRouter();
const loading = ref(false);
const grouped = ref({ newApi: [], sub2Api: [] });
const settings = ref({ query_interval: 300, request_timeout: 10, default_threshold: 5, monitor_paused: false });
const summaries = ref([]);
const filter = reactive({
  name: String(route.query.name || ""),
  platform: String(route.query.platform || ""),
  low_balance: String(route.query.low_balance || route.query.lowBalance || "")
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
const chartPoints = ref([]);
const chartBounds = ref(null);
const chartHover = ref(null);
const dashboardPlatforms = ["newApi", "sub2Api"];
const columnConfigStorageKey = "dashboard-column-config-v4";
const { isMobile } = useViewport();

const columnDefs = [
  { key: "name", label: "名称", defaultVisible: true },
  { key: "note", label: "备注", defaultVisible: true, defaultVisibleByPlatform: { newApi: false, sub2Api: false } },
  { key: "status", label: "状态", defaultVisible: true, defaultVisibleByPlatform: { newApi: false, sub2Api: false } },
  { key: "group_rates", label: "分组倍率", defaultVisible: true },
  { key: "group_rate_changed", label: "倍率变化", defaultVisible: true },
  { key: "remaining", label: "剩余", defaultVisible: true },
  { key: "today_consumption", label: "今日消耗", defaultVisible: false },
  { key: "actual_today_consumption", label: "今日实际消耗", defaultVisible: true },
  { key: "used", label: "已用", defaultVisible: true, platforms: ["newApi"], defaultVisibleByPlatform: { newApi: false } },
  { key: "total", label: "总额", defaultVisible: false, platforms: ["newApi"] },
  { key: "threshold", label: "阈值", defaultVisible: true, defaultVisibleByPlatform: { newApi: false, sub2Api: false } },
  { key: "low_balance", label: "低于阈值", defaultVisible: true },
  { key: "enabled", label: "自动查询", defaultVisible: true },
  { key: "eliminated", label: "是否淘汰", defaultVisible: true },
  { key: "checked_at", label: "最近查询", defaultVisible: true, defaultVisibleByPlatform: { newApi: false, sub2Api: false } },
  { key: "group_actions", label: "分组操作", defaultVisible: true },
  { key: "account_actions", label: "账号操作", defaultVisible: true }
];

const platformEntries = computed(() => Object.entries(grouped.value).filter(([, rows]) => Array.isArray(rows)));
const monitorPaused = computed(() => boolValue(settings.value.monitor_paused));
const columnConfig = reactive(loadColumnConfig());

function columnDefsForPlatform(platform) {
  return columnDefs.filter((column) => !column.platforms || column.platforms.includes(platform));
}

function columnDefaultVisible(column, platform) {
  if (typeof column.defaultVisibleByPlatform?.[platform] === "boolean") {
    return column.defaultVisibleByPlatform[platform];
  }
  return column.defaultVisible;
}

function defaultColumnConfigForPlatform(platform) {
  return Object.fromEntries(columnDefsForPlatform(platform).map((column) => [column.key, columnDefaultVisible(column, platform)]));
}

function columnControlOptions(platform) {
  return columnDefsForPlatform(platform).map((column) => ({ label: column.label, value: column.key }));
}

function columnControlValues(platform) {
  return columnDefsForPlatform(platform)
    .filter((column) => columnConfig[platform]?.[column.key] !== false)
    .map((column) => column.key);
}

function setPlatformColumnVisibility(platform, values) {
  const visibleKeys = new Set(values);
  columnDefsForPlatform(platform).forEach((column) => {
    columnConfig[platform][column.key] = visibleKeys.has(column.key);
  });
}

function resetColumnVisibility(platform) {
  setPlatformColumnVisibility(
    platform,
    columnDefsForPlatform(platform)
      .filter((column) => columnDefaultVisible(column, platform))
      .map((column) => column.key)
  );
}

function loadColumnConfig() {
  const defaults = Object.fromEntries(dashboardPlatforms.map((platform) => [platform, defaultColumnConfigForPlatform(platform)]));
  if (typeof window === "undefined") {
    return defaults;
  }
  try {
    const raw = window.localStorage.getItem(columnConfigStorageKey);
    if (!raw) {
      return defaults;
    }
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") {
      return defaults;
    }
    const legacyConfig = !parsed.newApi && !parsed.sub2Api ? parsed : null;
    return Object.fromEntries(
      dashboardPlatforms.map((platform) => {
        const source = parsed[platform] && typeof parsed[platform] === "object" ? parsed[platform] : legacyConfig;
        return [
          platform,
          {
            ...defaults[platform],
            ...Object.fromEntries(
              columnDefsForPlatform(platform)
                .filter((column) => typeof source?.[column.key] === "boolean")
                .map((column) => [column.key, source[column.key]])
            )
          }
        ];
      })
    );
  } catch {
    return defaults;
  }
}

function showColumn(platform, key) {
  return columnConfig[platform]?.[key] !== false;
}

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
  filter.low_balance = "";
  await applyFilter();
}

function allRowsForAccount(accountId) {
  return Object.values(grouped.value).flatMap((rows) => rows.filter((row) => String(row.id) === String(accountId)));
}

function replaceAccountRows(account) {
  Object.entries(grouped.value).forEach(([platform, rows]) => {
    grouped.value[platform] = rows.map((row) => {
      if (String(row.id) !== String(account.id)) {
        return row;
      }
      const nextRow = { ...row, ...account };
      if (row.dashboard_row_id) {
        nextRow.group_rates = row.group_rates;
        nextRow.last_group_rate_changed = row.last_group_rate_changed;
        nextRow.monitor_group = row.monitor_group;
        nextRow.current_group_id = row.current_group_id;
        nextRow.current_monitor_group_id = row.current_monitor_group_id;
        nextRow.dashboard_row_id = row.dashboard_row_id;
        nextRow.dashboard_rowspan = row.dashboard_rowspan;
        nextRow.dashboard_is_first_row = row.dashboard_is_first_row;
        nextRow.dashboard_is_last_row = row.dashboard_is_last_row;
      }
      return nextRow;
    });
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
  return boolValue(row.is_low_balance) ? "是" : "否";
}

function lowBalanceBadgeType(row) {
  return lowBalanceText(row) === "是" ? "danger" : "info";
}

function syncFilterFromRoute() {
  filter.name = String(route.query.name || "");
  filter.platform = String(route.query.platform || "");
  filter.low_balance = String(route.query.low_balance || route.query.lowBalance || "");
}

const accountMergedColumns = new Set([
  "name",
  "note",
  "status",
  "remaining",
  "today_consumption",
  "actual_today_consumption",
  "used",
  "total",
  "threshold",
  "low_balance",
  "enabled",
  "eliminated",
  "checked_at",
  "account_actions"
]);

function dashboardSpanMethod({ row, column }) {
  if (!accountMergedColumns.has(column.property)) {
    return { rowspan: 1, colspan: 1 };
  }
  if (row.dashboard_is_first_row) {
    return { rowspan: row.dashboard_rowspan || 1, colspan: 1 };
  }
  return { rowspan: 0, colspan: 0 };
}

function dashboardRowClassName({ row }) {
  return row.dashboard_is_first_row ? "account-block-start" : "account-block-child";
}

function dashboardCards(rows, platform) {
  return rows.filter((row) => row.dashboard_is_first_row);
}

function queryFailureMessage(result, fallback) {
  return result?.invalid_message || result?.invalidMessage || result?.message || fallback;
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
    const failedResults = results.filter((result) => result.is_valid === false);
    if (failedResults.length) {
      ElMessage.error(`查询失败 ${failedResults.length}/${results.length || failedResults.length}：${queryFailureMessage(failedResults[0], "查询失败")}`);
    } else {
      ElMessage.success(trigger === "auto" ? "自动刷新完成" : "查询全部完成");
    }
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
    if (result.is_valid === false) {
      ElMessage.error(queryFailureMessage(result, "查询失败"));
    } else {
      ElMessage.success("查询完成");
    }
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
    if (result.is_valid === false) {
      ElMessage.error(queryFailureMessage(result, "查组失败"));
    } else {
      ElMessage.success("查组完成");
    }
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
      selected: payload.selected_group_ids ?? payload.selectedGroupIds ?? payload.selected_group_id ?? payload.selectedGroupId,
      originalSelected: payload.stored_selected_group_ids ?? payload.storedSelectedGroupIds
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
  chartPoints.value = [];
  chartBounds.value = null;
  chartHover.value = null;
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
  chartHover.value = null;
  drawChart();
  ElMessage.success("余额历史已清除");
}

function normalizedBalanceRecords() {
  return balanceRecords.value
    .map((record) => ({ ...record, remaining: Number(record.remaining), time: new Date(record.checked_at).getTime() }))
    .filter((record) => Number.isFinite(record.remaining) && Number.isFinite(record.time))
    .sort((a, b) => a.time - b.time);
}

function clearChartHover() {
  if (!chartHover.value) {
    return;
  }
  chartHover.value = null;
  drawChart();
}

function handleChartMouseMove(event) {
  const canvas = chartCanvas.value;
  const points = chartPoints.value;
  const bounds = chartBounds.value;
  if (!canvas || !points.length || !bounds) {
    clearChartHover();
    return;
  }

  const rect = canvas.getBoundingClientRect();
  const mouseX = event.clientX - rect.left;
  const mouseY = event.clientY - rect.top;
  if (mouseX < bounds.left - 12 || mouseX > bounds.right + 12 || mouseY < bounds.top - 18 || mouseY > bounds.bottom + 18) {
    clearChartHover();
    return;
  }

  const nearest = points.reduce((best, point) => {
    const xDistance = Math.abs(point.x - mouseX);
    const bestXDistance = Math.abs(best.x - mouseX);
    if (xDistance !== bestXDistance) {
      return xDistance < bestXDistance ? point : best;
    }
    return Math.abs(point.y - mouseY) < Math.abs(best.y - mouseY) ? point : best;
  }, points[0]);
  const maxDistance = Math.max(24, (bounds.right - bounds.left) / Math.max(points.length - 1, 1) / 2);
  if (Math.abs(nearest.x - mouseX) > maxDistance) {
    clearChartHover();
    return;
  }

  if (chartHover.value?.index !== nearest.index) {
    chartHover.value = { index: nearest.index };
    drawChart();
  }
}

function drawRoundedRect(ctx, x, y, width, height, radius) {
  ctx.beginPath();
  ctx.moveTo(x + radius, y);
  ctx.lineTo(x + width - radius, y);
  ctx.quadraticCurveTo(x + width, y, x + width, y + radius);
  ctx.lineTo(x + width, y + height - radius);
  ctx.quadraticCurveTo(x + width, y + height, x + width - radius, y + height);
  ctx.lineTo(x + radius, y + height);
  ctx.quadraticCurveTo(x, y + height, x, y + height - radius);
  ctx.lineTo(x, y + radius);
  ctx.quadraticCurveTo(x, y, x + radius, y);
  ctx.closePath();
}

function drawChartHover(ctx, point, rect, bounds) {
  const valueText = `Y: ${displayValue(point.record.remaining)}`;
  const timeText = formatTime(point.record.checked_at);
  ctx.save();
  ctx.strokeStyle = "#94a3b8";
  ctx.lineWidth = 1;
  ctx.setLineDash([4, 4]);
  ctx.beginPath();
  ctx.moveTo(point.x, bounds.top);
  ctx.lineTo(point.x, bounds.bottom);
  ctx.moveTo(bounds.left, point.y);
  ctx.lineTo(bounds.right, point.y);
  ctx.stroke();
  ctx.setLineDash([]);

  ctx.fillStyle = "#ffffff";
  ctx.strokeStyle = "#2563eb";
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.arc(point.x, point.y, 5, 0, Math.PI * 2);
  ctx.fill();
  ctx.stroke();

  ctx.font = "600 12px sans-serif";
  const valueWidth = ctx.measureText(valueText).width;
  ctx.font = "12px sans-serif";
  const timeWidth = ctx.measureText(timeText).width;
  const boxWidth = Math.max(valueWidth, timeWidth) + 20;
  const boxHeight = 48;
  let boxX = point.x + 12;
  let boxY = point.y - boxHeight - 12;
  if (boxX + boxWidth > rect.width - 8) {
    boxX = point.x - boxWidth - 12;
  }
  if (boxY < 8) {
    boxY = point.y + 12;
  }
  boxX = Math.max(8, Math.min(boxX, rect.width - boxWidth - 8));
  boxY = Math.max(8, Math.min(boxY, rect.height - boxHeight - 8));

  ctx.fillStyle = "rgba(15, 23, 42, 0.92)";
  drawRoundedRect(ctx, boxX, boxY, boxWidth, boxHeight, 6);
  ctx.fill();
  ctx.fillStyle = "#ffffff";
  ctx.font = "600 12px sans-serif";
  ctx.fillText(valueText, boxX + 10, boxY + 18);
  ctx.fillStyle = "#cbd5e1";
  ctx.font = "12px sans-serif";
  ctx.fillText(timeText, boxX + 10, boxY + 36);
  ctx.restore();
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
  const records = normalizedBalanceRecords();
  if (!records.length) {
    chartPoints.value = [];
    chartBounds.value = null;
    chartHover.value = null;
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
  const points = records.map((record, index) => ({ index, record, x: x(record), y: y(record) }));
  const bounds = { left: padding.left, right: padding.left + width, top: padding.top, bottom: padding.top + height };
  chartPoints.value = points;
  chartBounds.value = bounds;

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
  points.forEach((point, index) => {
    if (index === 0) ctx.moveTo(point.x, point.y);
    else ctx.lineTo(point.x, point.y);
  });
  ctx.stroke();
  ctx.fillStyle = "#2563eb";
  points.forEach((point) => {
    ctx.beginPath();
    ctx.arc(point.x, point.y, 3, 0, Math.PI * 2);
    ctx.fill();
  });
  ctx.fillStyle = "#667085";
  ctx.font = "12px sans-serif";
  ctx.fillText(String(maxValue), 8, padding.top + 4);
  ctx.fillText(String(minValue), 8, padding.top + height);

  const activePoint = Number.isInteger(chartHover.value?.index) ? points[chartHover.value.index] : null;
  if (activePoint) {
    drawChartHover(ctx, activePoint, rect, bounds);
  }
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

watch(
  () => [route.query.name, route.query.platform, route.query.low_balance, route.query.lowBalance],
  () => {
    syncFilterFromRoute();
  }
);

watch(
  columnConfig,
  (value) => {
    if (typeof window === "undefined") {
      return;
    }
    window.localStorage.setItem(columnConfigStorageKey, JSON.stringify(value));
  },
  { deep: true }
);

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
      <el-form-item label="低于阈值">
        <el-select v-model="filter.low_balance" placeholder="全部状态" clearable style="width: 160px">
          <el-option label="全部" value="" />
          <el-option label="仅低于阈值" value="low" />
          <el-option label="仅未低于阈值" value="normal" />
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

    <div v-for="[platform, rows] in platformEntries" :key="platform" :class="['panel', 'table-card', 'platform-table', `platform-table--${platform}`]">
      <div class="panel-head platform-panel-head">
        <div class="platform-title">
          <h2>{{ platform }}</h2>
          <p>主体网站</p>
        </div>
        <div class="platform-head-actions">
          <el-popover placement="bottom-end" trigger="click" width="320">
            <template #reference>
              <el-button :icon="Operation">列显示</el-button>
            </template>
            <div class="column-visibility-panel">
              <div class="column-visibility-head">
                <strong>列显示</strong>
                <el-button link type="primary" @click="resetColumnVisibility(platform)">默认</el-button>
              </div>
              <el-checkbox-group :model-value="columnControlValues(platform)" class="column-visibility-list" @update:model-value="(values) => setPlatformColumnVisibility(platform, values)">
                <el-checkbox v-for="option in columnControlOptions(platform)" :key="option.value" :label="option.value">
                  {{ option.label }}
                </el-checkbox>
              </el-checkbox-group>
            </div>
          </el-popover>
          <el-tag>{{ accountCount(rows) }} 个账号<span v-if="rows.length !== accountCount(rows)"> / {{ rows.length }} 个分组</span></el-tag>
        </div>
      </div>
      <template v-if="!isMobile">
        <el-table
          :data="rows"
          border
          stripe
          row-key="dashboard_row_id"
          :span-method="dashboardSpanMethod"
          :row-class-name="dashboardRowClassName"
          style="width: 100%"
        >
          <el-table-column v-if="showColumn(platform, 'name')" prop="name" label="名称" min-width="150">
            <template #default="{ row }">
              <strong>{{ row.name }}</strong>
            </template>
          </el-table-column>
          <el-table-column v-if="showColumn(platform, 'note')" prop="note" label="备注" min-width="110">
            <template #default="{ row }">
              <span class="note-text">{{ row.note || "-" }}</span>
            </template>
          </el-table-column>
          <el-table-column v-if="showColumn(platform, 'status')" prop="status" label="状态" width="105">
            <template #default="{ row }">
              <el-tag class="status-tag" :type="row.last_status === 'valid' ? 'success' : row.last_status === 'invalid' ? 'danger' : 'info'">
                {{ row.last_status }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column v-if="showColumn(platform, 'group_rates')" label="分组倍率" min-width="190">
            <template #default="{ row }">
              <div class="group-rate-list">
                <span v-for="text in groupRateText(row)" :key="text">{{ text }}</span>
              </div>
            </template>
          </el-table-column>
          <el-table-column v-if="showColumn(platform, 'group_rate_changed') && (platform === 'newApi' || platform === 'sub2Api')" label="倍率变化" width="170">
            <template #default="{ row }">
              <div class="change-status-actions">
                <el-tag :type="row.last_group_rate_changed ? 'danger' : 'info'">{{ row.last_group_rate_changed ? "变化" : "未变化" }}</el-tag>
                <el-button v-if="row.last_group_rate_changed" size="small" link type="primary" @click="resetGroupRate(row)">重置</el-button>
              </div>
            </template>
          </el-table-column>
          <el-table-column v-if="showColumn(platform, 'remaining')" prop="remaining" label="剩余" width="125">
            <template #default="{ row }">
              <span class="metric-value">{{ displayValue(row.last_remaining) }}</span>
            </template>
          </el-table-column>
          <el-table-column v-if="showColumn(platform, 'today_consumption')" prop="today_consumption" label="今日消耗" width="125">
            <template #default="{ row }">
              <span>{{ displayValue(row.today_consumption) }}</span>
            </template>
          </el-table-column>
          <el-table-column v-if="showColumn(platform, 'actual_today_consumption')" prop="actual_today_consumption" label="今日实际消耗" width="140">
            <template #default="{ row }">
              <span class="metric-value">{{ displayValue(row.actual_today_consumption) }}</span>
            </template>
          </el-table-column>
          <el-table-column v-if="showColumn(platform, 'used') && platform !== 'sub2Api'" prop="used" label="已用" width="105">
            <template #default="{ row }">
              <span>{{ displayValue(row.last_used) }}</span>
            </template>
          </el-table-column>
          <el-table-column v-if="showColumn(platform, 'total') && platform !== 'sub2Api'" prop="total" label="总额" width="105">
            <template #default="{ row }">
              <span>{{ displayValue(row.last_total) }}</span>
            </template>
          </el-table-column>
          <el-table-column v-if="showColumn(platform, 'threshold')" prop="threshold" label="阈值" width="90">
            <template #default="{ row }">
              <span>{{ row.threshold ?? settings.default_threshold }}</span>
            </template>
          </el-table-column>
          <el-table-column v-if="showColumn(platform, 'low_balance')" prop="low_balance" label="低于阈值" width="105">
            <template #default="{ row }">
              <el-tag :type="lowBalanceBadgeType(row)" :class="{ 'low-balance': lowBalanceText(row) === '是' }">{{ lowBalanceText(row) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column v-if="showColumn(platform, 'enabled')" prop="enabled" label="自动查询" width="105">
            <template #default="{ row }">
              <el-switch :model-value="boolValue(row.is_enabled)" @change="toggleEnabled(row)" />
            </template>
          </el-table-column>
          <el-table-column v-if="showColumn(platform, 'eliminated')" prop="eliminated" label="是否淘汰" width="105">
            <template #default="{ row }">
              <el-switch :model-value="boolValue(row.is_eliminated)" @change="toggleEliminated(row)" />
            </template>
          </el-table-column>
          <el-table-column v-if="showColumn(platform, 'checked_at')" prop="checked_at" label="最近查询" width="165">
            <template #default="{ row }">
              <span>{{ formatTime(row.last_checked_at) }}</span>
            </template>
          </el-table-column>
          <el-table-column v-if="showColumn(platform, 'group_actions') && (platform === 'newApi' || platform === 'sub2Api')" label="分组操作" min-width="145" fixed="right">
            <template #default="{ row }">
              <div class="table-actions group-actions">
                <el-button v-if="row.platform === 'newApi' || row.platform === 'sub2Api'" size="small" :loading="row._groupQuerying" @click="queryGroup(row)">查组</el-button>
                <el-button v-if="row.platform === 'newApi' || row.platform === 'sub2Api'" size="small" @click="router.push({ name: 'group-rates', params: { id: row.id }, query: row.current_monitor_group_id ? { monitor_group_id: row.current_monitor_group_id } : {} })">分组变化</el-button>
              </div>
            </template>
          </el-table-column>
          <el-table-column v-if="showColumn(platform, 'account_actions')" prop="account_actions" label="账号操作" min-width="170" fixed="right">
            <template #default="{ row }">
              <div class="table-actions">
                <el-button size="small" tag="a" :href="row.base_url" target="_blank">打开</el-button>
                <el-button v-if="row.recharge_url" size="small" type="success" tag="a" :href="row.recharge_url" target="_blank">充值</el-button>
                <el-button size="small" :loading="row._querying" @click="queryOne(row)">查询</el-button>
                <el-button v-if="row.dashboard_is_first_row" size="small" :loading="row._fetchingGroups" @click="fetchGroups(row)">
                  {{ row.platform === "newApi" ? "获取分组" : "选择分组" }}
                </el-button>
                <el-button size="small" @click="router.push({ path: '/accounts', query: { edit_id: row.id } })">修改</el-button>
                <el-button size="small" :icon="Money" @click="openBalanceHistory(row)">余额趋势</el-button>
              </div>
            </template>
          </el-table-column>
        </el-table>
      </template>
      <div v-else class="mobile-stack">
        <article v-for="row in dashboardCards(rows, platform)" :key="row.dashboard_row_id" class="mobile-card">
          <div class="mobile-card-head">
            <div class="mobile-card-title">
              <strong>{{ row.name }}</strong>
              <div class="mobile-card-meta">
                <span>{{ row.note || "-" }}</span>
                <span>{{ row.base_url }}</span>
              </div>
            </div>
            <el-tag class="status-tag" :type="row.last_status === 'valid' ? 'success' : row.last_status === 'invalid' ? 'danger' : 'info'">
              {{ row.last_status || "未查询" }}
            </el-tag>
          </div>

          <div class="mobile-metrics">
            <div class="mobile-metric">
              <span>剩余</span>
              <strong class="metric-value">{{ displayValue(row.last_remaining) }}</strong>
            </div>
            <div class="mobile-metric">
              <span>今日消耗</span>
              <strong>{{ displayValue(row.today_consumption) }}</strong>
            </div>
            <div class="mobile-metric">
              <span>今日实际消耗</span>
              <strong class="metric-value">{{ displayValue(row.actual_today_consumption) }}</strong>
            </div>
            <div v-if="platform !== 'sub2Api'" class="mobile-metric">
              <span>已用 / 总额</span>
              <strong>{{ displayValue(row.last_used) }} / {{ displayValue(row.last_total) }}</strong>
            </div>
          </div>

          <div class="mobile-field-list">
            <div class="mobile-field">
              <span>分组倍率</span>
              <strong class="group-rate-list">
                <span v-for="text in groupRateText(row)" :key="text">{{ text }}</span>
              </strong>
            </div>
            <div class="mobile-field">
              <span>阈值</span>
              <strong>{{ row.threshold ?? settings.default_threshold }}</strong>
            </div>
            <div class="mobile-field">
              <span>低于阈值</span>
              <strong :class="{ 'low-balance': lowBalanceText(row) === '是' }">{{ lowBalanceText(row) }}</strong>
            </div>
            <div class="mobile-field">
              <span>最近查询</span>
              <strong>{{ formatTime(row.last_checked_at) }}</strong>
            </div>
          </div>

          <div class="mobile-switches">
            <div class="mobile-switch-row">
              <span>自动查询</span>
              <el-switch :model-value="boolValue(row.is_enabled)" @change="toggleEnabled(row)" />
            </div>
            <div class="mobile-switch-row">
              <span>是否淘汰</span>
              <el-switch :model-value="boolValue(row.is_eliminated)" @change="toggleEliminated(row)" />
            </div>
          </div>

          <div class="mobile-actions">
            <el-button size="small" tag="a" :href="row.base_url" target="_blank">打开</el-button>
            <el-button v-if="row.recharge_url" size="small" type="success" tag="a" :href="row.recharge_url" target="_blank">充值</el-button>
            <el-button size="small" :loading="row._querying" @click="queryOne(row)">查询</el-button>
            <el-button size="small" @click="router.push({ path: '/accounts', query: { edit_id: row.id } })">修改</el-button>
            <el-button size="small" :icon="Money" @click="openBalanceHistory(row)">余额趋势</el-button>
          </div>

          <div v-if="platform === 'newApi' || platform === 'sub2Api'" class="mobile-divider">
            <div class="mobile-actions">
              <el-button v-if="row.platform === 'newApi' || row.platform === 'sub2Api'" size="small" :loading="row._groupQuerying" @click="queryGroup(row)">查组</el-button>
              <el-button v-if="row.platform === 'newApi' || row.platform === 'sub2Api'" size="small" @click="router.push({ name: 'group-rates', params: { id: row.id }, query: row.current_monitor_group_id ? { monitor_group_id: row.current_monitor_group_id } : {} })">分组变化</el-button>
              <el-button v-if="row.dashboard_is_first_row" size="small" :loading="row._fetchingGroups" @click="fetchGroups(row)">
                {{ row.platform === "newApi" ? "获取分组" : "选择分组" }}
              </el-button>
              <el-button v-if="row.last_group_rate_changed" size="small" link type="primary" @click="resetGroupRate(row)">重置</el-button>
            </div>
          </div>
        </article>
      </div>
    </div>

    <GroupPickerDialog ref="groupPicker" @saved="loadDashboard" />

    <el-dialog v-model="balanceDialogVisible" title="余额趋势" width="860px" @opened="drawChart">
      <p class="muted">{{ balanceAccount?.platform }} / {{ balanceAccount?.name }} · 最近 3 天</p>
      <div v-loading="balanceLoading" class="chart-box">
        <canvas ref="chartCanvas" @mousemove="handleChartMouseMove" @mouseleave="clearChartHover"></canvas>
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

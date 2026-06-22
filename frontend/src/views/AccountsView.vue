<script setup>
import { onMounted, reactive, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";
import { CopyDocument, Delete, Edit, Plus, Upload } from "@element-plus/icons-vue";
import { api } from "../api";
import AccountDialog from "../components/AccountDialog.vue";
import GroupPickerDialog from "../components/GroupPickerDialog.vue";
import { useViewport } from "../composables/useViewport";
import { accountCredentialsText, boolValue, displayValue, platforms } from "../utils";

const route = useRoute();
const router = useRouter();
const loading = ref(false);
const grouped = ref({ newApi: [], sub2Api: [] });
const accountDialog = ref(null);
const groupPicker = ref(null);
const bulkDialogVisible = ref(false);
const bulkLoading = ref(false);
const filter = reactive({
  name: String(route.query.name || ""),
  platform: String(route.query.platform || "")
});
const bulkForm = reactive({
  platform: "newApi",
  bulk_text: ""
});
const { isMobile } = useViewport();

async function loadAccounts() {
  loading.value = true;
  try {
    grouped.value = await api.accounts(filter);
  } catch (error) {
    if (error.status === 401) {
      await router.replace({ name: "login", query: { redirect: route.fullPath } });
      return;
    }
    ElMessage.error(error.message || "加载账号失败");
  } finally {
    loading.value = false;
  }
}

async function applyFilter() {
  await router.replace({ path: "/accounts", query: { ...filter } });
  await loadAccounts();
}

async function resetFilter() {
  filter.name = "";
  filter.platform = "";
  await applyFilter();
}

function rows(platform) {
  return grouped.value[platform] || [];
}

function upsertLocal(account) {
  if (!account?.platform) {
    loadAccounts();
    return;
  }
  const target = grouped.value[account.platform] || [];
  const index = target.findIndex((item) => String(item.id) === String(account.id));
  if (index >= 0) {
    target[index] = account;
  } else {
    target.unshift(account);
  }
  grouped.value[account.platform] = target;
}

function openCreate() {
  accountDialog.value.open(null, "create");
}

async function openEdit(account) {
  try {
    const payload = await api.account(account.id);
    accountDialog.value.open(payload.account, "edit");
  } catch (error) {
    ElMessage.error(error.message || "获取账号失败");
  }
}

async function openCopy(account) {
  try {
    const payload = await api.account(account.id);
    accountDialog.value.open(payload.account, "copy");
  } catch (error) {
    ElMessage.error(error.message || "复制失败");
  }
}

async function deleteAccount(account) {
  await ElMessageBox.confirm(`确定删除 ${account.platform} / ${account.name} 吗？`, "删除账号", { type: "warning" });
  await api.deleteAccount(account.id);
  grouped.value[account.platform] = rows(account.platform).filter((item) => item.id !== account.id);
  ElMessage.success("账号已删除");
}

async function toggleVisible(account) {
  try {
    const payload = await api.setVisible(account.id, !boolValue(account.is_visible));
    upsertLocal(payload.account);
    ElMessage.success("仪表盘显示状态已保存");
  } catch (error) {
    ElMessage.error(error.message || "保存失败");
  }
}

async function toggleEnabled(account) {
  try {
    const payload = await api.setEnabled(account.id, !boolValue(account.is_enabled));
    upsertLocal(payload.account);
    ElMessage.success("自动查询状态已保存");
  } catch (error) {
    ElMessage.error(error.message || "保存失败");
  }
}

async function queryGroup(account) {
  account._groupQuerying = true;
  try {
    const result = await api.queryGroup(account.id);
    if (result.is_valid === false) {
      ElMessage.error(result.invalid_message || result.invalidMessage || "查组失败");
    } else {
      ElMessage.success("查组完成");
    }
  } catch (error) {
    ElMessage.error(error.message || "查组失败");
  } finally {
    account._groupQuerying = false;
  }
}

async function fetchGroups(account) {
  account._fetchingGroups = true;
  try {
    const payload = account.platform === "sub2Api" ? await api.sub2ApiGroups(account.id) : await api.newApiGroups(account.id);
    groupPicker.value.open({
      accountId: account.id,
      platform: account.platform,
      groups: payload.groups || [],
      selected: payload.selected_group_ids ?? payload.selectedGroupIds ?? payload.selected_group_id ?? payload.selectedGroupId,
      originalSelected: payload.stored_selected_group_ids ?? payload.storedSelectedGroupIds
    });
  } catch (error) {
    ElMessage.error(error.message || "获取分组失败");
  } finally {
    account._fetchingGroups = false;
  }
}

function openBulk() {
  bulkForm.platform = "newApi";
  bulkForm.bulk_text = "";
  bulkDialogVisible.value = true;
}

async function submitBulk() {
  bulkLoading.value = true;
  try {
    const payload = await api.bulkAccounts(bulkForm);
    ElMessage.success(`已导入或更新 ${payload.count || 0} 个账号`);
    bulkDialogVisible.value = false;
    await loadAccounts();
  } catch (error) {
    ElMessage.error(error.message || "导入失败");
  } finally {
    bulkLoading.value = false;
  }
}

function bulkPlaceholder(platform) {
  if (platform === "sub2Api") {
    return "JSON 数组，或 CSV 行：name,baseUrl,email,password,apiKey,threshold,note,rechargeUrl";
  }
  return "JSON 数组，或 CSV 行：name,baseUrl,accessToken,userId,threshold,note,rechargeUrl";
}

function mobileAccounts(platform) {
  return rows(platform);
}

onMounted(async () => {
  await loadAccounts();
  if (route.query.edit_id) {
    try {
      const payload = await api.account(route.query.edit_id);
      accountDialog.value.open(payload.account, "edit");
    } catch (error) {
      ElMessage.error(error.message || "获取账号失败");
    }
  }
});
</script>

<template>
  <section v-loading="loading">
    <div class="page-head">
      <div>
        <h1>平台配置</h1>
        <p>按平台新增或批量导入账号，名称相同会更新已有配置。</p>
      </div>
      <div class="page-actions">
        <el-button type="primary" :icon="Plus" @click="openCreate">添加账号</el-button>
        <el-button :icon="Upload" @click="openBulk">批量导入</el-button>
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

    <div v-for="platform in platforms.filter((item) => grouped[item])" :key="platform" class="panel table-card">
      <div class="panel-head">
        <h2>{{ platform }}</h2>
        <el-tag>{{ rows(platform).length }} 个账号</el-tag>
      </div>
      <template v-if="!isMobile">
        <el-table :data="rows(platform)" border stripe row-key="id" style="width: 100%">
          <el-table-column label="名称" min-width="150" fixed>
            <template #default="{ row }"><strong>{{ row.name }}</strong></template>
          </el-table-column>
          <el-table-column label="备注" min-width="160">
            <template #default="{ row }"><span class="note-text">{{ row.note || "-" }}</span></template>
          </el-table-column>
          <el-table-column label="Base URL" min-width="210">
            <template #default="{ row }"><span class="url-text">{{ row.base_url }}</span></template>
          </el-table-column>
          <el-table-column label="充值路径" width="105">
            <template #default="{ row }">
              <el-button v-if="row.recharge_url" link type="success" tag="a" :href="row.recharge_url" target="_blank">充值</el-button>
              <span v-else>-</span>
            </template>
          </el-table-column>
          <el-table-column label="充值比例" width="110">
            <template #default="{ row }">{{ row.recharge_paid_amount || 1 }}:{{ row.recharge_received_amount || 1 }}</template>
          </el-table-column>
          <el-table-column label="凭据" min-width="330">
            <template #default="{ row }"><span class="credentials-text">{{ accountCredentialsText(row) }}</span></template>
          </el-table-column>
          <el-table-column label="阈值" width="90">
            <template #default="{ row }">{{ displayValue(row.threshold) }}</template>
          </el-table-column>
          <el-table-column label="仪表盘显示" width="120">
            <template #default="{ row }">
              <el-switch :model-value="boolValue(row.is_visible)" @change="toggleVisible(row)" />
            </template>
          </el-table-column>
          <el-table-column label="自动查询" width="105">
            <template #default="{ row }">
              <el-switch :model-value="boolValue(row.is_enabled)" :disabled="!boolValue(row.is_visible)" @change="toggleEnabled(row)" />
            </template>
          </el-table-column>
          <el-table-column label="操作" min-width="280" fixed="right">
            <template #default="{ row }">
              <div class="table-actions">
                <el-button size="small" :icon="Edit" @click="openEdit(row)">编辑</el-button>
                <el-button size="small" :icon="CopyDocument" @click="openCopy(row)">复制</el-button>
                <el-button v-if="row.platform === 'sub2Api'" size="small" :loading="row._groupQuerying" @click="queryGroup(row)">查组</el-button>
                <el-button v-if="row.platform === 'newApi' || row.platform === 'sub2Api'" size="small" :loading="row._fetchingGroups" @click="fetchGroups(row)">
                  {{ row.platform === "newApi" ? "重新获取分组" : "选择分组" }}
                </el-button>
                <el-button size="small" type="danger" :icon="Delete" @click="deleteAccount(row)">删除</el-button>
              </div>
            </template>
          </el-table-column>
        </el-table>
      </template>
      <div v-else class="mobile-stack">
        <article v-for="row in mobileAccounts(platform)" :key="row.id" class="mobile-card">
          <div class="mobile-card-head">
            <div class="mobile-card-title">
              <strong>{{ row.name }}</strong>
              <div class="mobile-card-meta">
                <span>{{ row.note || "-" }}</span>
                <span>{{ row.base_url }}</span>
              </div>
            </div>
            <el-tag>{{ row.platform }}</el-tag>
          </div>

          <div class="mobile-metrics">
            <div class="mobile-metric">
              <span>充值比例</span>
              <strong>{{ row.recharge_paid_amount || 1 }} : {{ row.recharge_received_amount || 1 }}</strong>
            </div>
            <div class="mobile-metric">
              <span>阈值</span>
              <strong>{{ displayValue(row.threshold) }}</strong>
            </div>
          </div>

          <div class="mobile-field-list">
            <div class="mobile-field">
              <span>凭据</span>
              <strong class="credentials-text">{{ accountCredentialsText(row) }}</strong>
            </div>
            <div class="mobile-field">
              <span>充值路径</span>
              <strong>
                <el-button v-if="row.recharge_url" link type="success" tag="a" :href="row.recharge_url" target="_blank">打开</el-button>
                <span v-else>-</span>
              </strong>
            </div>
          </div>

          <div class="mobile-switches">
            <div class="mobile-switch-row">
              <span>仪表盘显示</span>
              <el-switch :model-value="boolValue(row.is_visible)" @change="toggleVisible(row)" />
            </div>
            <div class="mobile-switch-row">
              <span>自动查询</span>
              <el-switch :model-value="boolValue(row.is_enabled)" :disabled="!boolValue(row.is_visible)" @change="toggleEnabled(row)" />
            </div>
          </div>

          <div class="mobile-actions">
            <el-button size="small" :icon="Edit" @click="openEdit(row)">编辑</el-button>
            <el-button size="small" :icon="CopyDocument" @click="openCopy(row)">复制</el-button>
            <el-button v-if="row.platform === 'sub2Api'" size="small" :loading="row._groupQuerying" @click="queryGroup(row)">查组</el-button>
            <el-button v-if="row.platform === 'newApi' || row.platform === 'sub2Api'" size="small" :loading="row._fetchingGroups" @click="fetchGroups(row)">
              {{ row.platform === "newApi" ? "重新获取分组" : "选择分组" }}
            </el-button>
            <el-button size="small" type="danger" :icon="Delete" @click="deleteAccount(row)">删除</el-button>
          </div>
        </article>
      </div>
    </div>

    <AccountDialog ref="accountDialog" @saved="upsertLocal" @pick-groups="groupPicker.open($event)" />
    <GroupPickerDialog ref="groupPicker" @saved="upsertLocal" />

    <el-dialog v-model="bulkDialogVisible" title="批量导入" width="720px">
      <el-form label-position="top" @submit.prevent="submitBulk">
        <el-form-item label="平台">
          <el-select v-model="bulkForm.platform" style="width: 180px">
            <el-option label="newApi" value="newApi" />
            <el-option label="sub2Api" value="sub2Api" />
          </el-select>
        </el-form-item>
        <el-form-item label="内容">
          <el-input v-model="bulkForm.bulk_text" type="textarea" :rows="12" :placeholder="bulkPlaceholder(bulkForm.platform)" />
        </el-form-item>
      </el-form>
      <template #footer>
        <div class="dialog-footer">
          <el-button @click="bulkDialogVisible = false">取消</el-button>
          <el-button type="primary" :loading="bulkLoading" @click="submitBulk">导入</el-button>
        </div>
      </template>
    </el-dialog>
  </section>
</template>

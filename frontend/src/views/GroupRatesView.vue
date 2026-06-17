<script setup>
import { onMounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage } from "element-plus";
import { ArrowLeft, View } from "@element-plus/icons-vue";
import { api } from "../api";
import { useViewport } from "../composables/useViewport";
import { displayValue, formatTime, parseJsonLoose } from "../utils";

const route = useRoute();
const router = useRouter();
const loading = ref(false);
const account = ref(null);
const monitorGroup = ref(null);
const records = ref([]);
const jsonDialogVisible = ref(false);
const activeJson = ref("");
const { isMobile } = useViewport();

async function loadRecords() {
  loading.value = true;
  try {
    const payload = await api.groupRates(route.params.id, route.query);
    account.value = payload.account;
    monitorGroup.value = payload.monitor_group;
    records.value = payload.records || [];
  } catch (error) {
    ElMessage.error(error.message || "加载分组变化失败");
  } finally {
    loading.value = false;
  }
}

function showJson(record) {
  const parsed = parseJsonLoose(record.raw_json);
  activeJson.value = typeof parsed === "string" ? parsed : JSON.stringify(parsed || {}, null, 2);
  jsonDialogVisible.value = true;
}

onMounted(loadRecords);
</script>

<template>
  <section v-loading="loading">
    <div class="page-head">
      <div>
        <h1>分组变化</h1>
        <p v-if="account">
          {{ account.platform }} / {{ account.name }} / {{ account.base_url }}
          <span v-if="monitorGroup"> / 分组 {{ monitorGroup.display_name }}</span>
        </p>
      </div>
      <el-button :icon="ArrowLeft" @click="router.push('/')">返回仪表盘</el-button>
    </div>

    <div class="panel table-card">
      <template v-if="!isMobile">
        <el-table :data="records" border stripe style="width: 100%">
          <el-table-column label="名称" prop="plan_name" min-width="220" />
          <el-table-column label="分组倍率" width="140">
            <template #default="{ row }">{{ displayValue(row.rate_multiplier) }}</template>
          </el-table-column>
          <el-table-column label="查询时间" width="180">
            <template #default="{ row }">{{ formatTime(row.checked_at) }}</template>
          </el-table-column>
          <el-table-column label="操作" width="130">
            <template #default="{ row }">
              <el-button size="small" :icon="View" @click="showJson(row)">查看 JSON</el-button>
            </template>
          </el-table-column>
        </el-table>
      </template>
      <div v-else class="mobile-stack">
        <article v-for="row in records" :key="`${row.plan_name}-${row.checked_at}`" class="mobile-card">
          <div class="mobile-card-head">
            <div class="mobile-card-title">
              <strong>{{ row.plan_name }}</strong>
              <div class="mobile-card-meta">
                <span>{{ formatTime(row.checked_at) }}</span>
              </div>
            </div>
            <el-tag>倍率 {{ displayValue(row.rate_multiplier) }}</el-tag>
          </div>
          <div class="mobile-actions">
            <el-button size="small" :icon="View" @click="showJson(row)">查看 JSON</el-button>
          </div>
        </article>
      </div>
    </div>

    <el-dialog v-model="jsonDialogVisible" title="接口摘要 JSON" width="760px">
      <pre class="json-pre">{{ activeJson }}</pre>
    </el-dialog>
  </section>
</template>

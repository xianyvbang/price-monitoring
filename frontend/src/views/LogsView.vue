<script setup>
import { onMounted, reactive, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { Delete, Refresh } from "@element-plus/icons-vue";
import { api } from "../api";

const loading = ref(false);
const logs = ref([]);
const pagination = reactive({
  page: 1,
  page_size: 50,
  total: 0,
  total_pages: 1
});

async function loadLogs(page = pagination.page) {
  loading.value = true;
  try {
    const payload = await api.logs({ page, page_size: pagination.page_size });
    logs.value = payload.logs || [];
    Object.assign(pagination, payload.pagination || {});
  } catch (error) {
    ElMessage.error(error.message || "加载日志失败");
  } finally {
    loading.value = false;
  }
}

async function clearLogs() {
  await ElMessageBox.confirm("确定清空全部日志？", "清空日志", { type: "warning" });
  await api.clearLogs();
  ElMessage.success("日志已清空");
  await loadLogs(1);
}

function levelType(level) {
  if (level === "error") return "danger";
  if (level === "warning") return "warning";
  return "info";
}

onMounted(() => loadLogs(1));
</script>

<template>
  <section>
    <div class="page-head">
      <div>
        <h1>日志</h1>
        <p>显示最近 7 天日志。</p>
      </div>
      <div class="page-actions">
        <el-button :icon="Refresh" @click="loadLogs()">刷新</el-button>
        <el-button type="danger" :icon="Delete" @click="clearLogs">清空日志</el-button>
      </div>
    </div>

    <div class="panel table-card">
      <el-table v-loading="loading" :data="logs" border stripe row-key="id" style="width: 100%">
        <el-table-column label="时间" prop="created_at_formatted" width="180" />
        <el-table-column label="级别" width="100">
          <template #default="{ row }"><el-tag :type="levelType(row.level)">{{ row.level }}</el-tag></template>
        </el-table-column>
        <el-table-column label="分类" prop="category" width="140" />
        <el-table-column label="内容" min-width="420">
          <template #default="{ row }"><span class="note-text">{{ row.message }}</span></template>
        </el-table-column>
      </el-table>
      <el-pagination
        v-model:current-page="pagination.page"
        :page-size="pagination.page_size"
        :total="pagination.total"
        layout="prev, pager, next, total"
        style="justify-content: flex-end; margin-top: 14px"
        @current-change="loadLogs"
      />
    </div>
  </section>
</template>

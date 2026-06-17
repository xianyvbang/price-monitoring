<script setup>
import { computed, ref } from "vue";
import { ElMessage } from "element-plus";
import { api } from "../api";

const emit = defineEmits(["saved"]);

const visible = ref(false);
const loading = ref(false);
const accountId = ref(null);
const platform = ref("");
const groups = ref([]);
const selected = ref([]);

const title = computed(() => (platform.value === "sub2Api" ? "选择 sub2Api 监控分组" : "选择 newApi 监控分组"));

function open(options) {
  accountId.value = options.accountId;
  platform.value = options.platform || "";
  groups.value = options.groups || [];
  selected.value = normalizeSelected(options.selected);
  visible.value = true;
}

function normalizeSelected(value) {
  if (Array.isArray(value)) {
    return value.map(String);
  }
  if (value === null || value === undefined || value === "") {
    return [];
  }
  return String(value)
    .split(/[|;\n]/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function groupId(group) {
  return String(group.id ?? group.group_id ?? group.groupId ?? group.name ?? "");
}

function groupLabel(group) {
  return group.plan_name || group.planName || group.name || group.desc || group.description || groupId(group);
}

function rateLabel(group) {
  const rate = group.effective_rate_multiplier ?? group.user_rate_multiplier ?? group.default_rate_multiplier ?? group.rate ?? group.ratio;
  return rate === null || rate === undefined || rate === "" ? "-" : rate;
}

async function save() {
  if (!selected.value.length) {
    ElMessage.warning("请选择分组");
    return;
  }
  const selectedGroups = groups.value.filter((group) => selected.value.includes(groupId(group)));
  loading.value = true;
  try {
    const payload = await api.selectGroup(accountId.value, {
      group_ids: selected.value,
      groups: selectedGroups
    });
    ElMessage.success("分组已保存");
    visible.value = false;
    emit("saved", payload.account);
  } catch (error) {
    ElMessage.error(error.message || "保存分组失败");
  } finally {
    loading.value = false;
  }
}

defineExpose({ open });
</script>

<template>
  <el-dialog v-model="visible" :title="title" width="680px">
    <el-empty v-if="!groups.length" description="暂无可选分组" />
    <el-checkbox-group v-else v-model="selected" class="group-option-list">
      <el-checkbox v-for="group in groups" :key="groupId(group)" :label="groupId(group)" border class="group-option">
        <div class="group-option-copy">
          <span class="group-option-title">{{ groupLabel(group) }}</span>
          <small class="muted">ID: {{ groupId(group) }} · 倍率 {{ rateLabel(group) }}</small>
        </div>
      </el-checkbox>
    </el-checkbox-group>
    <template #footer>
      <div class="dialog-footer">
        <el-button @click="visible = false">取消</el-button>
        <el-button type="primary" :loading="loading" @click="save">保存分组</el-button>
      </div>
    </template>
  </el-dialog>
</template>

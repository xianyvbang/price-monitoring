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
const originalSelected = ref([]);

const title = computed(() => (platform.value === "sub2Api" ? "选择 sub2Api 监控分组" : "选择 newApi 监控分组"));

function open(options) {
  accountId.value = options.accountId;
  platform.value = options.platform || "";
  groups.value = options.groups || [];
  const availableGroupIds = groupIdsFromGroups(groups.value);
  const availableGroupIdSet = new Set(availableGroupIds);
  const storedSelected = normalizeSelected(
    options.originalSelected ??
      options.original_selected ??
      options.storedSelected ??
      options.stored_selected ??
      options.storedSelectedGroupIds ??
      options.stored_selected_group_ids ??
      options.selected
  );
  selected.value = normalizeSelected(options.selected).filter((groupIdValue) => availableGroupIdSet.has(groupIdValue));
  originalSelected.value = storedSelected;
  visible.value = true;
}

function normalizeSelected(value) {
  const values = [];
  if (Array.isArray(value)) {
    values.push(...value.map(String));
  } else if (value !== null && value !== undefined && value !== "") {
    values.push(
      ...String(value)
        .split(/[|;\n]/)
        .map((item) => item.trim())
    );
  }
  const seen = new Set();
  return values
    .map((item) => String(item).trim())
    .filter((item) => {
      if (!item || seen.has(item)) {
        return false;
      }
      seen.add(item);
      return true;
    });
}

function groupId(group) {
  return String(group.id ?? group.group_id ?? group.groupId ?? group.name ?? "");
}

function groupIdsFromGroups(value) {
  return normalizeSelected(value.map((group) => groupId(group)));
}

function groupLabel(group) {
  return group.plan_name || group.planName || group.name || group.desc || group.description || groupId(group);
}

function rateLabel(group) {
  const rate = group.effective_rate_multiplier ?? group.user_rate_multiplier ?? group.default_rate_multiplier ?? group.rate ?? group.ratio;
  return rate === null || rate === undefined || rate === "" ? "-" : rate;
}

async function save() {
  const availableGroupIdSet = new Set(groupIdsFromGroups(groups.value));
  const nextSelected = normalizeSelected(selected.value).filter((groupIdValue) => availableGroupIdSet.has(groupIdValue));
  const original = normalizeSelected(originalSelected.value);
  const originalSet = new Set(original);
  const nextSet = new Set(nextSelected);
  const added = nextSelected.filter((groupIdValue) => !originalSet.has(groupIdValue));
  const removed = original.filter((groupIdValue) => !nextSet.has(groupIdValue));
  if (!added.length && !removed.length) {
    ElMessage.info("分组未变化");
    visible.value = false;
    return;
  }
  selected.value = nextSelected;
  const selectedGroups = groups.value.filter((group) => nextSet.has(groupId(group)));
  loading.value = true;
  try {
    const payload = await api.selectGroup(accountId.value, {
      group_ids: nextSelected,
      groups: selectedGroups,
      original_group_ids: original,
      added_group_ids: added,
      removed_group_ids: removed
    });
    ElMessage.success(nextSelected.length ? "分组已保存" : "分组已清空");
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
      <el-checkbox v-for="group in groups" :key="groupId(group)" :value="groupId(group)" border class="group-option">
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

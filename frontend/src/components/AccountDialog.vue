<script setup>
import { reactive, ref } from "vue";
import { ElMessage } from "element-plus";
import { api } from "../api";
import { clone, normalizeAccountForm, selectedGroupIds } from "../utils";

const emit = defineEmits(["saved", "pick-groups"]);

const visible = ref(false);
const saving = ref(false);
const mode = ref("create");
const formRef = ref(null);
const form = reactive(normalizeAccountForm());

const rules = {
  platform: [{ required: true, message: "请选择平台", trigger: "change" }],
  name: [{ required: true, message: "请输入名称", trigger: "blur" }],
  base_url: [{ required: true, message: "请输入 Base URL", trigger: "blur" }]
};

function open(account = null, nextMode = "create") {
  Object.assign(form, normalizeAccountForm(account || {}));
  mode.value = nextMode;
  if (nextMode === "copy") {
    form.id = "";
    form.name = form.name ? `${form.name} 副本` : "";
  }
  syncEnabled();
  visible.value = true;
}

function syncEnabled() {
  if (!form.is_visible) {
    form.is_enabled = false;
  }
}

function payload() {
  const data = clone(form);
  if (data.platform === "sub2Api") {
    data.key_id = data.key_id || "";
  }
  data.monitor_group_ids = selectedGroupIds({ selected_group_ids: data.monitor_group_ids?.length ? data.monitor_group_ids : data.key_id });
  if (!data.api_key) delete data.api_key;
  if (!data.password) delete data.password;
  if (!data.access_token) delete data.access_token;
  if (!data.refresh_token) delete data.refresh_token;
  delete data.id;
  return data;
}

async function submit() {
  await formRef.value?.validate();
  saving.value = true;
  try {
    const isEdit = Boolean(form.id);
    const result = isEdit ? await api.updateAccount(form.id, payload()) : await api.createAccount(payload());
    const account = result.account;
    emit("saved", account);
    ElMessage.success("账号已保存");
    visible.value = false;
    if (account?.is_visible && ["newApi", "sub2Api"].includes(account.platform)) {
      try {
        const groups = account.platform === "sub2Api" ? await api.sub2ApiGroups(account.id) : await api.newApiGroups(account.id);
        emit("pick-groups", {
          accountId: account.id,
          platform: account.platform,
          groups: groups.groups || [],
          selected: groups.selected_group_ids ?? groups.selectedGroupIds ?? groups.selected_group_id ?? groups.selectedGroupId
        });
      } catch (error) {
        ElMessage.warning(`账号已保存，获取分组失败：${error.message || "未知错误"}`);
      }
    }
  } catch (error) {
    if (error?.message) {
      ElMessage.error(error.message);
    }
  } finally {
    saving.value = false;
  }
}

defineExpose({ open });
</script>

<template>
  <el-dialog v-model="visible" :title="form.id ? '编辑账号' : mode === 'copy' ? '复制账号' : '添加账号'" width="760px">
    <el-form ref="formRef" :model="form" :rules="rules" label-position="top">
      <el-row :gutter="12">
        <el-col :span="8">
          <el-form-item label="平台" prop="platform">
            <el-select v-model="form.platform" style="width: 100%">
              <el-option label="newApi" value="newApi" />
              <el-option label="sub2Api" value="sub2Api" />
            </el-select>
          </el-form-item>
        </el-col>
        <el-col :span="16">
          <el-form-item label="名称" prop="name">
            <el-input v-model="form.name" />
          </el-form-item>
        </el-col>
      </el-row>
      <el-form-item label="Base URL" prop="base_url">
        <el-input v-model="form.base_url" />
      </el-form-item>
      <el-form-item label="充值路径">
        <el-input v-model="form.recharge_url" />
      </el-form-item>
      <el-row :gutter="12">
        <el-col :span="12">
          <el-form-item label="充值金额">
            <el-input-number v-model="form.recharge_paid_amount" :min="0.000001" :step="0.000001" :precision="6" style="width: 100%" />
          </el-form-item>
        </el-col>
        <el-col :span="12">
          <el-form-item label="实际得到金额">
            <el-input-number v-model="form.recharge_received_amount" :min="0.000001" :step="0.000001" :precision="6" style="width: 100%" />
          </el-form-item>
        </el-col>
      </el-row>
      <el-form-item label="备注">
        <el-input v-model="form.note" type="textarea" :rows="3" />
      </el-form-item>
      <el-form-item v-if="form.platform === 'sub2Api'" label="sub2Api groupId（可选筛选）">
        <el-input v-model="form.key_id" />
      </el-form-item>
      <el-form-item v-if="form.platform === 'sub2Api'" label="sub2Api apiKey">
        <el-input v-model="form.api_key" type="password" show-password autocomplete="off" placeholder="留空则不修改已保存密钥" />
      </el-form-item>
      <el-row v-if="form.platform === 'sub2Api'" :gutter="12">
        <el-col :span="12">
          <el-form-item label="sub2Api email">
            <el-input v-model="form.email" autocomplete="username" />
          </el-form-item>
        </el-col>
        <el-col :span="12">
          <el-form-item label="sub2Api password">
            <el-input v-model="form.password" type="password" show-password autocomplete="current-password" placeholder="留空则不修改" />
          </el-form-item>
        </el-col>
      </el-row>
      <el-form-item label="accessToken">
        <el-input v-model="form.access_token" type="password" show-password autocomplete="off" :placeholder="form.platform === 'newApi' ? 'newApi 必填，编辑时留空不修改' : 'sub2Api 可选，编辑时留空不修改'" />
      </el-form-item>
      <el-form-item v-if="form.platform === 'sub2Api'" label="refreshToken">
        <el-input v-model="form.refresh_token" type="password" show-password autocomplete="off" placeholder="可选，编辑时留空不修改" />
      </el-form-item>
      <el-form-item v-if="form.platform === 'newApi'" label="newApi userId">
        <el-input v-model="form.user_id" />
      </el-form-item>
      <el-row :gutter="12">
        <el-col :span="8">
          <el-form-item label="预警阈值">
            <el-input-number v-model="form.threshold" :min="0" :step="0.01" style="width: 100%" />
          </el-form-item>
        </el-col>
        <el-col :span="8">
          <el-form-item label="显示在仪表盘">
            <el-switch v-model="form.is_visible" active-text="显示" inactive-text="隐藏" @change="syncEnabled" />
          </el-form-item>
        </el-col>
        <el-col :span="8">
          <el-form-item label="自动查询">
            <el-switch v-model="form.is_enabled" :disabled="!form.is_visible" active-text="启用" inactive-text="暂停" />
          </el-form-item>
        </el-col>
      </el-row>
    </el-form>
    <template #footer>
      <div class="dialog-footer">
        <el-button @click="visible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="submit">保存账号</el-button>
      </div>
    </template>
  </el-dialog>
</template>

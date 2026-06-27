<script setup>
import { nextTick, reactive, ref } from "vue";
import { ElMessage } from "element-plus";
import { CopyDocument } from "@element-plus/icons-vue";
import { api } from "../api";
import { clone, normalizeAccountForm, selectedGroupIds } from "../utils";

const emit = defineEmits(["saved", "pick-groups"]);

const visible = ref(false);
const saving = ref(false);
const mode = ref("create");
const formRef = ref(null);
const form = reactive(normalizeAccountForm());
const initialForm = ref(normalizeAccountForm());
const accessTokenSnippet = "localStorage.getItem('auth_token')";
const refreshTokenSnippet = "localStorage.getItem('refresh_token')";

const rules = {
  platform: [{ required: true, message: "请选择平台", trigger: "change" }],
  name: [{ required: true, message: "请输入名称", trigger: "blur" }],
  base_url: [{ required: true, message: "请输入 Base URL", trigger: "blur" }]
};

function open(account = null, nextMode = "create") {
  const normalized = normalizeAccountForm(account || {});
  Object.assign(form, normalized);
  initialForm.value = clone(normalized);
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
    nextTick(() => formRef.value?.clearValidate());
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

function shouldFetchGroupsAfterSave(account, savedPayload, isEdit) {
  if (!account?.is_visible || !["newApi", "sub2Api"].includes(account.platform)) {
    return false;
  }
  if (!isEdit) {
    return true;
  }
  if (account.platform === "newApi") {
    return (
      String(savedPayload.base_url || "").trim() !== String(initialForm.value.base_url || "").trim() ||
      Boolean(savedPayload.access_token) ||
      String(savedPayload.user_id || "").trim() !== String(initialForm.value.user_id || "").trim()
    );
  }
  return (
    String(savedPayload.base_url || "").trim() !== String(initialForm.value.base_url || "").trim() ||
    Boolean(savedPayload.email) ||
    Boolean(savedPayload.password) ||
    String(savedPayload.login_extra_params || "").trim() !== String(initialForm.value.login_extra_params || "").trim() ||
    Boolean(savedPayload.access_token) ||
    Boolean(savedPayload.refresh_token)
  );
}

async function copyText(text) {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);
  textarea.select();
  document.execCommand("copy");
  document.body.removeChild(textarea);
}

async function copyTokenSnippet(snippet) {
  try {
    await copyText(snippet);
    ElMessage.success("已复制 JS 命令");
  } catch {
    ElMessage.error("复制失败，请手动复制命令");
  }
}

async function submit() {
  if (form.is_visible) {
    await formRef.value?.validate();
  } else {
    formRef.value?.clearValidate();
  }
  saving.value = true;
  try {
    const isEdit = Boolean(form.id);
    const savedPayload = payload();
    const result = isEdit ? await api.updateAccount(form.id, savedPayload) : await api.createAccount(savedPayload);
    const account = result.account;
    emit("saved", account);
    ElMessage.success("账号已保存");
    visible.value = false;
    if (shouldFetchGroupsAfterSave(account, savedPayload, isEdit)) {
      try {
        const groups = account.platform === "sub2Api" ? await api.sub2ApiGroups(account.id) : await api.newApiGroups(account.id);
        emit("pick-groups", {
          accountId: account.id,
          platform: account.platform,
          groups: groups.groups || [],
          selected: groups.selected_group_ids ?? groups.selectedGroupIds ?? groups.selected_group_id ?? groups.selectedGroupId,
          originalSelected: groups.stored_selected_group_ids ?? groups.storedSelectedGroupIds
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
        <el-col :xs="24" :md="8">
          <el-form-item label="平台" prop="platform">
            <el-select v-model="form.platform" style="width: 100%">
              <el-option label="newApi" value="newApi" />
              <el-option label="sub2Api" value="sub2Api" />
            </el-select>
          </el-form-item>
        </el-col>
        <el-col :xs="24" :md="16">
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
        <el-col :xs="24" :md="12">
          <el-form-item label="充值金额">
            <el-input-number v-model="form.recharge_paid_amount" :min="0.000001" :step="0.000001" :precision="6" style="width: 100%" />
          </el-form-item>
        </el-col>
        <el-col :xs="24" :md="12">
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
        <el-col :xs="24" :md="12">
          <el-form-item label="sub2Api email">
            <el-input v-model="form.email" autocomplete="username" />
          </el-form-item>
        </el-col>
        <el-col :xs="24" :md="12">
          <el-form-item label="sub2Api password">
            <el-input v-model="form.password" type="password" show-password autocomplete="current-password" placeholder="留空则不修改" />
          </el-form-item>
        </el-col>
      </el-row>
      <el-form-item v-if="form.platform === 'sub2Api'" label="登录额外参数">
        <el-input v-model="form.login_extra_params" placeholder="not_in_cn_confirmed:true" />
      </el-form-item>
      <el-form-item label="accessToken">
        <el-input v-model="form.access_token" type="password" show-password autocomplete="off" :placeholder="form.platform === 'newApi' ? 'newApi 必填，编辑时留空不修改' : 'sub2Api 可选，编辑时留空不修改'" />
        <div v-if="form.platform === 'sub2Api'" class="token-helper">
          <span>2chat 登录后，在浏览器控制台执行获取 AT：</span>
          <el-button class="token-helper-command" :icon="CopyDocument" @click="copyTokenSnippet(accessTokenSnippet)">
            <code>{{ accessTokenSnippet }}</code>
          </el-button>
        </div>
      </el-form-item>
      <el-form-item v-if="form.platform === 'sub2Api'" label="refreshToken">
        <el-input v-model="form.refresh_token" type="password" show-password autocomplete="off" placeholder="可选，编辑时留空不修改" />
        <div class="token-helper">
          <span>2chat 登录后，在浏览器控制台执行获取 RT：</span>
          <el-button class="token-helper-command" :icon="CopyDocument" @click="copyTokenSnippet(refreshTokenSnippet)">
            <code>{{ refreshTokenSnippet }}</code>
          </el-button>
        </div>
      </el-form-item>
      <el-form-item v-if="form.platform === 'newApi'" label="newApi userId">
        <el-input v-model="form.user_id" />
      </el-form-item>
      <el-row :gutter="12">
        <el-col :xs="24" :md="8">
          <el-form-item label="预警阈值">
            <el-input-number v-model="form.threshold" :min="0" :step="0.01" style="width: 100%" />
          </el-form-item>
        </el-col>
        <el-col :xs="24" :md="8">
          <el-form-item label="显示在仪表盘">
            <el-switch v-model="form.is_visible" active-text="显示" inactive-text="隐藏" @change="syncEnabled" />
          </el-form-item>
        </el-col>
        <el-col :xs="24" :md="8">
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

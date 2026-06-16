<script setup>
import { inject, onMounted, reactive, ref } from "vue";
import { useRouter } from "vue-router";
import { ElMessage } from "element-plus";
import { Check, Message, Setting } from "@element-plus/icons-vue";
import { api } from "../api";

const router = useRouter();
const refreshSession = inject("refreshSession");
const loading = ref(false);
const savingGeneral = ref(false);
const savingSmtp = ref(false);
const testingSmtp = ref(false);
const changingPassword = ref(false);

const general = reactive({
  request_timeout: 10,
  query_interval: 300,
  group_rate_query_interval: 300,
  default_threshold: 5,
  monitor_paused: false
});
const smtp = reactive({
  host: "",
  port: null,
  username: "",
  password: "",
  sender: "",
  sender_name: "",
  receiver: "",
  security: "ssl",
  has_password: false
});
const password = reactive({
  current_password: "",
  new_password: "",
  confirm_password: ""
});

async function loadSettings() {
  loading.value = true;
  try {
    const payload = await api.settings();
    Object.assign(general, payload.settings || {});
    Object.assign(smtp, payload.smtp || {});
    smtp.password = "";
  } catch (error) {
    ElMessage.error(error.message || "加载设置失败");
  } finally {
    loading.value = false;
  }
}

async function saveGeneral() {
  savingGeneral.value = true;
  try {
    const payload = await api.saveGeneralSettings(general);
    Object.assign(general, payload.settings);
    ElMessage.success("查询设置已保存");
  } catch (error) {
    ElMessage.error(error.message || "保存失败");
  } finally {
    savingGeneral.value = false;
  }
}

async function saveSmtp() {
  savingSmtp.value = true;
  try {
    const payload = await api.saveSmtpSettings(smtp);
    Object.assign(smtp, payload.smtp);
    smtp.password = "";
    ElMessage.success("SMTP 已保存");
  } catch (error) {
    ElMessage.error(error.message || "保存失败");
  } finally {
    savingSmtp.value = false;
  }
}

async function testSmtp() {
  testingSmtp.value = true;
  try {
    await api.testSmtp();
    ElMessage.success("测试邮件已发送");
  } catch (error) {
    ElMessage.error(error.message || "测试邮件发送失败");
  } finally {
    testingSmtp.value = false;
  }
}

async function changePassword() {
  changingPassword.value = true;
  try {
    await api.changePassword(password);
    ElMessage.success("密码已更新，请重新登录");
    await refreshSession();
    await router.replace({ name: "login", query: { message: "password_changed" } });
  } catch (error) {
    ElMessage.error(error.message || "修改密码失败");
  } finally {
    changingPassword.value = false;
  }
}

onMounted(loadSettings);
</script>

<template>
  <section v-loading="loading">
    <div class="page-head">
      <div>
        <h1>通用设置</h1>
        <p>配置查询节奏、默认预警阈值和 SMTP 邮件。</p>
      </div>
    </div>

    <el-row :gutter="16">
      <el-col :xs="24" :lg="12">
        <el-card class="panel-card">
          <template #header>
            <strong><el-icon><Setting /></el-icon> 查询设置</strong>
          </template>
          <el-form :model="general" label-position="top" @submit.prevent="saveGeneral">
            <el-form-item label="请求超时时间（秒）">
              <el-input-number v-model="general.request_timeout" :min="1" :step="0.5" style="width: 100%" />
            </el-form-item>
            <el-form-item label="自动查询间隔（秒）">
              <el-input-number v-model="general.query_interval" :min="300" :step="1" style="width: 100%" />
            </el-form-item>
            <el-form-item label="自动查询分组倍率间隔（秒）">
              <el-input-number v-model="general.group_rate_query_interval" :min="60" :step="1" style="width: 100%" />
            </el-form-item>
            <el-form-item label="全局默认低余额阈值">
              <el-input-number v-model="general.default_threshold" :min="0" :step="0.01" style="width: 100%" />
            </el-form-item>
            <el-form-item label="自动监控">
              <el-switch v-model="general.monitor_paused" active-text="暂停" inactive-text="运行" />
            </el-form-item>
            <el-button type="primary" :icon="Check" :loading="savingGeneral" @click="saveGeneral">保存查询设置</el-button>
          </el-form>
        </el-card>
      </el-col>

      <el-col :xs="24" :lg="12">
        <el-card class="panel-card">
          <template #header>
            <strong><el-icon><Message /></el-icon> SMTP 邮件</strong>
          </template>
          <el-form :model="smtp" label-position="top" @submit.prevent="saveSmtp">
            <el-row :gutter="12">
              <el-col :span="16">
                <el-form-item label="Host"><el-input v-model="smtp.host" /></el-form-item>
              </el-col>
              <el-col :span="8">
                <el-form-item label="Port"><el-input-number v-model="smtp.port" :min="1" style="width: 100%" /></el-form-item>
              </el-col>
            </el-row>
            <el-form-item label="Username"><el-input v-model="smtp.username" /></el-form-item>
            <el-form-item label="Password">
              <el-input v-model="smtp.password" type="password" show-password :placeholder="smtp.has_password ? '已配置，留空不修改' : ''" />
            </el-form-item>
            <el-row :gutter="12">
              <el-col :span="12"><el-form-item label="Sender"><el-input v-model="smtp.sender" /></el-form-item></el-col>
              <el-col :span="12"><el-form-item label="Sender Name"><el-input v-model="smtp.sender_name" /></el-form-item></el-col>
            </el-row>
            <el-form-item label="Receiver"><el-input v-model="smtp.receiver" /></el-form-item>
            <el-form-item label="安全模式">
              <el-select v-model="smtp.security" style="width: 100%">
                <el-option label="SSL" value="ssl" />
                <el-option label="STARTTLS" value="starttls" />
                <el-option label="None" value="none" />
              </el-select>
            </el-form-item>
            <div class="dialog-footer">
              <el-button type="primary" :loading="savingSmtp" @click="saveSmtp">保存 SMTP</el-button>
              <el-button :loading="testingSmtp" @click="testSmtp">发送测试邮件</el-button>
            </div>
          </el-form>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="16" style="margin-top: 16px">
      <el-col :xs="24" :lg="12">
        <el-card class="panel-card">
          <template #header><strong>修改登录密码</strong></template>
          <el-form :model="password" label-position="top" @submit.prevent="changePassword">
            <el-form-item label="当前密码"><el-input v-model="password.current_password" type="password" show-password autocomplete="current-password" /></el-form-item>
            <el-form-item label="新密码"><el-input v-model="password.new_password" type="password" show-password autocomplete="new-password" /></el-form-item>
            <el-form-item label="确认新密码"><el-input v-model="password.confirm_password" type="password" show-password autocomplete="new-password" /></el-form-item>
            <el-button type="primary" :loading="changingPassword" @click="changePassword">更新密码</el-button>
          </el-form>
        </el-card>
      </el-col>
    </el-row>
  </section>
</template>

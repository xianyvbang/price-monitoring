<script setup>
import { inject, onMounted, reactive, ref } from "vue";
import { useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";
import { Bell, Check, Connection, Delete, Edit, Lock, Menu, Message, Plus, Refresh, Setting } from "@element-plus/icons-vue";
import { api } from "../api";

const router = useRouter();
const refreshSession = inject("refreshSession");
const updateTopMenuVisibility = inject("updateTopMenuVisibility");
const loading = ref(false);
const savingGeneral = ref(false);
const savingTopMenu = ref(false);
const savingSub2Api = ref(false);
const savingCpa = ref(false);
const savingSmtp = ref(false);
const testingSmtp = ref(false);
const changingPassword = ref(false);
const reminderLoading = ref(false);
const savingReminder = ref(false);
const deletingReminderId = ref(null);

const queryDialog = ref(false);
const topMenuDialog = ref(false);
const sub2ApiDialog = ref(false);
const cpaDialog = ref(false);
const smtpDialog = ref(false);
const passwordDialog = ref(false);
const remindersDialog = ref(false);
const reminderFormDialog = ref(false);
const editingReminderId = ref(null);

const general = reactive({
  request_timeout: 10,
  query_interval: 300,
  group_rate_query_interval: 300,
  default_threshold: 5,
  monitor_paused: false
});
const topMenuVisibility = reactive({
  dashboard: true,
  accounts: true,
  platform_dispatch: true,
  opencode_go: true,
  logs: true
});
const savedTopMenuVisibility = ref({ ...topMenuVisibility });
const sub2api = reactive({
  admin_key: "",
  admin_key_masked: "",
  has_admin_key: false,
  site_url: ""
});
const cpa = reactive({
  authorization: "",
  authorization_masked: "",
  has_authorization: false,
  site_url: ""
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
const reminders = ref([]);
const reminderForm = reactive({
  title: "",
  content: "",
  remind_at: ""
});

async function loadSettings() {
  loading.value = true;
  try {
    const payload = await api.settings();
    assignGeneralSettings(payload.settings || {});
    setTopMenuVisibility(payload.settings?.top_menu_visibility || payload.settings?.topMenuVisibility);
    assignSub2Api(payload.sub2api || {});
    assignCpa(payload.cpa || {});
    Object.assign(smtp, payload.smtp || {});
    smtp.password = "";
    reminders.value = payload.reminders || [];
  } catch (error) {
    ElMessage.error(error.message || "加载设置失败");
  } finally {
    loading.value = false;
  }
}

function assignGeneralSettings(value) {
  Object.assign(general, {
    request_timeout: value.request_timeout ?? general.request_timeout,
    query_interval: value.query_interval ?? general.query_interval,
    group_rate_query_interval: value.group_rate_query_interval ?? value.groupRateQueryInterval ?? general.group_rate_query_interval,
    default_threshold: value.default_threshold ?? general.default_threshold,
    monitor_paused: typeof value.monitor_paused === "boolean" ? value.monitor_paused : general.monitor_paused
  });
}

function assignTopMenuVisibility(value) {
  Object.keys(topMenuVisibility).forEach((key) => {
    topMenuVisibility[key] = typeof value?.[key] === "boolean" ? value[key] : true;
  });
}

function setTopMenuVisibility(value) {
  assignTopMenuVisibility(value);
  savedTopMenuVisibility.value = { ...topMenuVisibility };
}

function openTopMenuDialog() {
  assignTopMenuVisibility(savedTopMenuVisibility.value);
  topMenuDialog.value = true;
}

function cancelTopMenuDialog() {
  assignTopMenuVisibility(savedTopMenuVisibility.value);
  topMenuDialog.value = false;
}

function assignSub2Api(value) {
  sub2api.admin_key = "";
  sub2api.admin_key_masked = value.admin_key_masked || value.adminKeyMasked || "";
  sub2api.has_admin_key = Boolean(value.has_admin_key || value.hasAdminKey);
  sub2api.site_url = value.site_url || value.siteUrl || "";
}

function assignCpa(value) {
  cpa.authorization = "";
  cpa.authorization_masked = value.authorization_masked || value.authorizationMasked || "";
  cpa.has_authorization = Boolean(value.has_authorization || value.hasAuthorization);
  cpa.site_url = value.site_url || value.siteUrl || "";
}

async function loadReminders() {
  reminderLoading.value = true;
  try {
    const payload = await api.reminders();
    reminders.value = payload.reminders || [];
  } catch (error) {
    ElMessage.error(error.message || "加载提醒失败");
  } finally {
    reminderLoading.value = false;
  }
}

async function saveGeneral() {
  savingGeneral.value = true;
  try {
    const payload = await api.saveGeneralSettings(general);
    assignGeneralSettings(payload.settings || {});
    queryDialog.value = false;
    ElMessage.success("查询设置已保存");
  } catch (error) {
    ElMessage.error(error.message || "保存失败");
  } finally {
    savingGeneral.value = false;
  }
}

async function saveTopMenu() {
  savingTopMenu.value = true;
  try {
    const payload = await api.saveGeneralSettings({
      top_menu_visibility: { ...topMenuVisibility }
    });
    assignGeneralSettings(payload.settings || {});
    setTopMenuVisibility(payload.settings?.top_menu_visibility || payload.settings?.topMenuVisibility);
    updateTopMenuVisibility?.(topMenuVisibility);
    topMenuDialog.value = false;
    ElMessage.success("顶部菜单设置已保存");
  } catch (error) {
    ElMessage.error(error.message || "保存失败");
  } finally {
    savingTopMenu.value = false;
  }
}

async function saveSub2Api() {
  savingSub2Api.value = true;
  try {
    const payload = await api.saveSub2ApiSettings(sub2api);
    assignSub2Api(payload.sub2api || {});
    sub2ApiDialog.value = false;
    ElMessage.success("Sub2API 配置已保存");
  } catch (error) {
    ElMessage.error(error.message || "保存失败");
  } finally {
    savingSub2Api.value = false;
  }
}

async function saveCpa() {
  savingCpa.value = true;
  try {
    const payload = await api.saveCpaSettings(cpa);
    assignCpa(payload.cpa || {});
    cpaDialog.value = false;
    ElMessage.success("CPA 配置已保存");
  } catch (error) {
    ElMessage.error(error.message || "保存失败");
  } finally {
    savingCpa.value = false;
  }
}

async function saveSmtp() {
  savingSmtp.value = true;
  try {
    const payload = await api.saveSmtpSettings(smtp);
    Object.assign(smtp, payload.smtp);
    smtp.password = "";
    smtpDialog.value = false;
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

function openReminders() {
  remindersDialog.value = true;
  loadReminders();
}

function resetReminderForm() {
  editingReminderId.value = null;
  Object.assign(reminderForm, {
    title: "",
    content: "",
    remind_at: ""
  });
}

function createReminder() {
  resetReminderForm();
  reminderFormDialog.value = true;
}

function editReminder(row) {
  editingReminderId.value = row.id;
  Object.assign(reminderForm, {
    title: row.title || "",
    content: row.content || "",
    remind_at: row.remind_at_china || row.remindAtChina || ""
  });
  reminderFormDialog.value = true;
}

async function saveReminder() {
  savingReminder.value = true;
  try {
    const payload = { ...reminderForm };
    if (editingReminderId.value) {
      await api.updateReminder(editingReminderId.value, payload);
      ElMessage.success("提醒已更新");
    } else {
      await api.createReminder(payload);
      ElMessage.success("提醒已新增");
    }
    reminderFormDialog.value = false;
    resetReminderForm();
    await loadReminders();
  } catch (error) {
    ElMessage.error(error.message || "保存提醒失败");
  } finally {
    savingReminder.value = false;
  }
}

async function deleteReminder(row) {
  try {
    await ElMessageBox.confirm(`确定删除“${row.title}”？`, "删除提醒", { type: "warning" });
  } catch {
    return;
  }
  deletingReminderId.value = row.id;
  try {
    await api.deleteReminder(row.id);
    reminders.value = reminders.value.filter((item) => item.id !== row.id);
    ElMessage.success("提醒已删除");
  } catch (error) {
    ElMessage.error(error.message || "删除提醒失败");
  } finally {
    deletingReminderId.value = null;
  }
}

function reminderStatusType(row) {
  if (row.is_sent || row.isSent) {
    return "success";
  }
  if (row.last_error) {
    return "danger";
  }
  return "warning";
}

function reminderStatusText(row) {
  if (row.is_sent || row.isSent) {
    return "已发送";
  }
  if (row.last_error) {
    return "发送失败";
  }
  return "待发送";
}

onMounted(loadSettings);
</script>

<template>
  <section v-loading="loading">
    <div class="page-head">
      <div>
        <h1>通用设置</h1>
        <p>配置查询节奏、顶部菜单、SMTP 邮件、登录密码和定时提醒。</p>
      </div>
    </div>

    <div class="settings-launch-grid">
      <el-button class="settings-launch-button" :icon="Setting" @click="queryDialog = true">
        查询设置
      </el-button>
      <el-button class="settings-launch-button" :icon="Menu" @click="openTopMenuDialog">
        顶部菜单设置
      </el-button>
      <el-button class="settings-launch-button" :icon="Connection" @click="sub2ApiDialog = true">
        Sub2API 配置
      </el-button>
      <el-button class="settings-launch-button" :icon="Connection" @click="cpaDialog = true">
        CPA 配置
      </el-button>
      <el-button class="settings-launch-button" :icon="Message" @click="smtpDialog = true">
        SMTP 邮件
      </el-button>
      <el-button class="settings-launch-button" :icon="Lock" @click="passwordDialog = true">
        修改登录密码
      </el-button>
      <el-button class="settings-launch-button" :icon="Bell" @click="openReminders">
        定时提醒事项
      </el-button>
    </div>

    <el-dialog v-model="topMenuDialog" title="顶部菜单设置" width="520px">
      <el-form label-position="left" label-width="120px">
        <el-form-item label="仪表盘"><el-switch v-model="topMenuVisibility.dashboard" active-text="显示" inactive-text="隐藏" /></el-form-item>
        <el-form-item label="平台配置"><el-switch v-model="topMenuVisibility.accounts" active-text="显示" inactive-text="隐藏" /></el-form-item>
        <el-form-item label="平台调度"><el-switch v-model="topMenuVisibility.platform_dispatch" active-text="显示" inactive-text="隐藏" /></el-form-item>
        <el-form-item label="OpenCode Go"><el-switch v-model="topMenuVisibility.opencode_go" active-text="显示" inactive-text="隐藏" /></el-form-item>
        <el-form-item label="日志"><el-switch v-model="topMenuVisibility.logs" active-text="显示" inactive-text="隐藏" /></el-form-item>
      </el-form>
      <template #footer>
        <div class="dialog-footer">
          <el-button @click="cancelTopMenuDialog">取消</el-button>
          <el-button type="primary" :icon="Check" :loading="savingTopMenu" @click="saveTopMenu">保存</el-button>
        </div>
      </template>
    </el-dialog>

    <el-dialog v-model="queryDialog" title="查询设置" width="560px">
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
      </el-form>
      <template #footer>
        <div class="dialog-footer">
          <el-button @click="queryDialog = false">取消</el-button>
          <el-button type="primary" :icon="Check" :loading="savingGeneral" @click="saveGeneral">保存</el-button>
        </div>
      </template>
    </el-dialog>

    <el-dialog v-model="sub2ApiDialog" title="Sub2API 配置" width="620px">
      <el-form :model="sub2api" label-position="top" @submit.prevent="saveSub2Api">
        <el-form-item label="AdminKey">
          <el-input
            v-model="sub2api.admin_key"
            type="password"
            show-password
            :placeholder="sub2api.has_admin_key ? `已配置：${sub2api.admin_key_masked}，留空不修改` : ''"
            autocomplete="off"
          />
        </el-form-item>
        <div class="import-helper sub2api-helper">
          <span>系统设置-&gt;安全与认证-&gt;创建秘钥</span>
        </div>
        <el-form-item label="站点地址">
          <el-input v-model="sub2api.site_url" placeholder="https://your-sub2api.example.com" autocomplete="off" />
        </el-form-item>
      </el-form>
      <template #footer>
        <div class="dialog-footer">
          <el-button @click="sub2ApiDialog = false">取消</el-button>
          <el-button type="primary" :loading="savingSub2Api" @click="saveSub2Api">保存 Sub2API</el-button>
        </div>
      </template>
    </el-dialog>

    <el-dialog v-model="cpaDialog" title="CPA 配置" width="620px">
      <el-form :model="cpa" label-position="top" @submit.prevent="saveCpa">
        <el-form-item label="Authorization">
          <el-input
            v-model="cpa.authorization"
            type="password"
            show-password
            :placeholder="cpa.has_authorization ? `已配置：${cpa.authorization_masked}，留空不修改` : 'Bearer xxxx'"
            autocomplete="off"
          />
        </el-form-item>
        <div class="import-helper sub2api-helper">
          <span>可填写 Bearer xxxx 或纯管理密钥，请使用 CLI Proxy API 的 Management API 管理密钥。</span>
        </div>
        <el-form-item label="站点地址">
          <el-input v-model="cpa.site_url" placeholder="https://your-cpa.example.com 或 https://your-cpa.example.com/v0/management" autocomplete="off" />
        </el-form-item>
      </el-form>
      <template #footer>
        <div class="dialog-footer">
          <el-button @click="cpaDialog = false">取消</el-button>
          <el-button type="primary" :loading="savingCpa" @click="saveCpa">保存 CPA</el-button>
        </div>
      </template>
    </el-dialog>

    <el-dialog v-model="smtpDialog" title="SMTP 邮件" width="680px">
      <el-form :model="smtp" label-position="top" @submit.prevent="saveSmtp">
        <el-row :gutter="12">
          <el-col :xs="24" :md="16">
            <el-form-item label="Host"><el-input v-model="smtp.host" /></el-form-item>
          </el-col>
          <el-col :xs="24" :md="8">
            <el-form-item label="Port"><el-input-number v-model="smtp.port" :min="1" style="width: 100%" /></el-form-item>
          </el-col>
        </el-row>
        <el-form-item label="Username"><el-input v-model="smtp.username" /></el-form-item>
        <el-form-item label="Password">
          <el-input v-model="smtp.password" type="password" show-password :placeholder="smtp.has_password ? '已配置，留空不修改' : ''" />
        </el-form-item>
        <el-row :gutter="12">
          <el-col :xs="24" :md="12"><el-form-item label="Sender"><el-input v-model="smtp.sender" /></el-form-item></el-col>
          <el-col :xs="24" :md="12"><el-form-item label="Sender Name"><el-input v-model="smtp.sender_name" /></el-form-item></el-col>
        </el-row>
        <el-form-item label="Receiver"><el-input v-model="smtp.receiver" /></el-form-item>
        <el-form-item label="安全模式">
          <el-select v-model="smtp.security" style="width: 100%">
            <el-option label="SSL" value="ssl" />
            <el-option label="STARTTLS" value="starttls" />
            <el-option label="None" value="none" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <div class="dialog-footer">
          <el-button @click="smtpDialog = false">取消</el-button>
          <el-button :loading="testingSmtp" @click="testSmtp">发送测试邮件</el-button>
          <el-button type="primary" :loading="savingSmtp" @click="saveSmtp">保存 SMTP</el-button>
        </div>
      </template>
    </el-dialog>

    <el-dialog v-model="passwordDialog" title="修改登录密码" width="520px">
      <el-form :model="password" label-position="top" @submit.prevent="changePassword">
        <el-form-item label="当前密码"><el-input v-model="password.current_password" type="password" show-password autocomplete="current-password" /></el-form-item>
        <el-form-item label="新密码"><el-input v-model="password.new_password" type="password" show-password autocomplete="new-password" /></el-form-item>
        <el-form-item label="确认新密码"><el-input v-model="password.confirm_password" type="password" show-password autocomplete="new-password" /></el-form-item>
      </el-form>
      <template #footer>
        <div class="dialog-footer">
          <el-button @click="passwordDialog = false">取消</el-button>
          <el-button type="primary" :loading="changingPassword" @click="changePassword">更新密码</el-button>
        </div>
      </template>
    </el-dialog>

    <el-dialog v-model="remindersDialog" title="定时提醒事项" width="900px">
      <div class="settings-dialog-toolbar">
        <el-button type="primary" :icon="Plus" @click="createReminder">新增提醒</el-button>
        <el-button :icon="Refresh" :loading="reminderLoading" @click="loadReminders">刷新</el-button>
      </div>
      <div class="reminder-list">
        <el-table v-loading="reminderLoading" :data="reminders" border stripe row-key="id" style="width: 100%">
          <el-table-column label="标题" min-width="180">
            <template #default="{ row }">
              <strong class="reminder-title-cell">{{ row.title }}</strong>
            </template>
          </el-table-column>
          <el-table-column label="内容" min-width="240">
            <template #default="{ row }">
              <span class="reminder-content-cell">{{ row.content }}</span>
            </template>
          </el-table-column>
          <el-table-column label="提醒时间" min-width="170">
            <template #default="{ row }">{{ row.remind_at_formatted || row.remindAtFormatted }}</template>
          </el-table-column>
          <el-table-column label="状态" width="110">
            <template #default="{ row }">
              <el-tag :type="reminderStatusType(row)">{{ reminderStatusText(row) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="150" fixed="right">
            <template #default="{ row }">
              <div class="table-actions">
                <el-button size="small" :icon="Edit" @click="editReminder(row)">编辑</el-button>
                <el-button size="small" type="danger" :icon="Delete" :loading="deletingReminderId === row.id" @click="deleteReminder(row)">删除</el-button>
              </div>
            </template>
          </el-table-column>
        </el-table>
      </div>
    </el-dialog>

    <el-dialog v-model="reminderFormDialog" :title="editingReminderId ? '编辑提醒' : '新增提醒'" width="540px" append-to-body>
      <el-form :model="reminderForm" label-position="top" @submit.prevent="saveReminder">
        <el-form-item label="标题">
          <el-input v-model="reminderForm.title" maxlength="120" show-word-limit />
        </el-form-item>
        <el-form-item label="提醒时间">
          <el-date-picker
            v-model="reminderForm.remind_at"
            type="datetime"
            format="YYYY-MM-DD HH:mm:ss"
            value-format="YYYY-MM-DDTHH:mm:ss"
            style="width: 100%"
          />
        </el-form-item>
        <el-form-item label="内容">
          <el-input v-model="reminderForm.content" type="textarea" :rows="6" maxlength="2000" show-word-limit />
        </el-form-item>
      </el-form>
      <template #footer>
        <div class="dialog-footer">
          <el-button @click="reminderFormDialog = false">取消</el-button>
          <el-button type="primary" :loading="savingReminder" @click="saveReminder">保存提醒</el-button>
        </div>
      </template>
    </el-dialog>
  </section>
</template>

<style scoped>
.settings-launch-grid {
  display: grid;
  gap: 16px;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
}

.settings-launch-button.el-button {
  align-items: center;
  border-radius: 8px;
  font-size: 16px;
  font-weight: 700;
  height: 92px;
  justify-content: center;
  margin-left: 0;
  width: 100%;
}

.settings-launch-button :deep(.el-icon) {
  font-size: 22px;
}

.settings-dialog-toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  justify-content: flex-end;
  margin-bottom: 12px;
}

.sub2api-helper {
  margin: -8px 0 18px;
}

.reminder-list {
  overflow: auto;
}

.reminder-title-cell,
.reminder-content-cell {
  overflow-wrap: anywhere;
  white-space: normal;
}

@media (max-width: 900px) {
  .settings-launch-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 560px) {
  .settings-launch-grid {
    grid-template-columns: 1fr;
  }

  .settings-launch-button.el-button {
    height: 72px;
  }

  .settings-dialog-toolbar .el-button {
    flex: 1 1 0;
  }
}
</style>

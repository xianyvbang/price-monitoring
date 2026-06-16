<script setup>
import { inject, reactive, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage } from "element-plus";
import { Lock, User } from "@element-plus/icons-vue";
import { api } from "../api";

const router = useRouter();
const route = useRoute();
const refreshSession = inject("refreshSession");
const loading = ref(false);
const form = reactive({ username: "", password: "" });

async function submit() {
  loading.value = true;
  try {
    await api.login(form);
    await refreshSession();
    ElMessage.success("登录成功");
    await router.replace(String(route.query.redirect || "/"));
  } catch (error) {
    ElMessage.error(error.message || "登录失败");
  } finally {
    loading.value = false;
  }
}
</script>

<template>
  <section class="login-page">
    <el-form class="login-panel" :model="form" label-position="top" @submit.prevent="submit">
      <h1>管理员登录</h1>
      <el-alert v-if="route.query.message === 'password_changed'" title="密码已更新，请使用新密码登录。" type="success" show-icon :closable="false" />
      <el-form-item label="用户名">
        <el-input v-model="form.username" :prefix-icon="User" autocomplete="username" />
      </el-form-item>
      <el-form-item label="密码">
        <el-input v-model="form.password" :prefix-icon="Lock" type="password" autocomplete="current-password" show-password />
      </el-form-item>
      <el-button type="primary" native-type="submit" :loading="loading" style="width: 100%">登录</el-button>
    </el-form>
  </section>
</template>

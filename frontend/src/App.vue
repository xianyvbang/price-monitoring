<script setup>
import { computed, onMounted, provide, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage } from "element-plus";
import { Connection, Grid, HomeFilled, List, Setting, SwitchButton, Tickets } from "@element-plus/icons-vue";
import { api } from "./api";

const router = useRouter();
const route = useRoute();
const defaultTopMenuVisibility = {
  dashboard: true,
  accounts: true,
  platform_dispatch: true,
  opencode_go: true,
  logs: true
};
const session = ref({ loading: true, authenticated: false, user: null, topMenuVisibility: { ...defaultTopMenuVisibility } });

const isLogin = computed(() => route.name === "login");

function normalizeTopMenuVisibility(value) {
  const visibility = { ...defaultTopMenuVisibility };
  Object.keys(visibility).forEach((key) => {
    if (typeof value?.[key] === "boolean") {
      visibility[key] = value[key];
    }
  });
  return visibility;
}

function setSession(payload) {
  session.value = {
    loading: false,
    authenticated: payload.authenticated,
    user: payload.user,
    topMenuVisibility: normalizeTopMenuVisibility(payload.top_menu_visibility || payload.topMenuVisibility)
  };
}

function updateTopMenuVisibility(value) {
  session.value = { ...session.value, topMenuVisibility: normalizeTopMenuVisibility(value) };
}

async function refreshSession() {
  session.value.loading = true;
  try {
    const payload = await api.session();
    setSession(payload);
    if (!payload.authenticated && !route.meta.public) {
      await router.replace({ name: "login", query: { redirect: route.fullPath } });
    }
    if (payload.authenticated && route.name === "login") {
      await router.replace(String(route.query.redirect || "/"));
    }
  } catch {
    session.value = { loading: false, authenticated: false, user: null, topMenuVisibility: { ...defaultTopMenuVisibility } };
  }
}

async function logout() {
  await api.logout();
  session.value = { loading: false, authenticated: false, user: null, topMenuVisibility: { ...defaultTopMenuVisibility } };
  ElMessage.success("已退出登录");
  await router.replace("/login");
}

provide("session", session);
provide("refreshSession", refreshSession);
provide("updateTopMenuVisibility", updateTopMenuVisibility);

router.beforeEach(async (to) => {
  if (session.value.loading) {
    try {
      const payload = await api.session();
      setSession(payload);
    } catch {
      session.value = { loading: false, authenticated: false, user: null, topMenuVisibility: { ...defaultTopMenuVisibility } };
    }
  }
  if (!to.meta.public && !session.value.authenticated) {
    return { name: "login", query: { redirect: to.fullPath } };
  }
  if (to.name === "login" && session.value.authenticated) {
    return String(to.query.redirect || "/");
  }
  return true;
});

onMounted(refreshSession);
</script>

<template>
  <el-container class="app-shell" :class="{ 'is-login': isLogin }">
    <el-header v-if="!isLogin && session.authenticated" class="topbar">
      <router-link class="brand" to="/">
        <img class="brand-mark" src="/favicon.svg" alt="" />
        <span class="brand-text">余额监控</span>
      </router-link>
      <el-menu :default-active="route.path" mode="horizontal" router class="nav-menu" :ellipsis="false">
        <el-menu-item v-if="session.topMenuVisibility.dashboard" index="/">
          <el-icon><HomeFilled /></el-icon>
          <span>仪表盘</span>
        </el-menu-item>
        <el-menu-item v-if="session.topMenuVisibility.accounts" index="/accounts">
          <el-icon><List /></el-icon>
          <span>平台配置</span>
        </el-menu-item>
        <el-menu-item v-if="session.topMenuVisibility.platform_dispatch" index="/platform-dispatch">
          <el-icon><Grid /></el-icon>
          <span>平台调度</span>
        </el-menu-item>
        <el-menu-item v-if="session.topMenuVisibility.opencode_go" index="/opencode-go">
          <el-icon><Connection /></el-icon>
          <span>OpenCode Go</span>
        </el-menu-item>
        <el-menu-item index="/settings">
          <el-icon><Setting /></el-icon>
          <span>通用设置</span>
        </el-menu-item>
        <el-menu-item v-if="session.topMenuVisibility.logs" index="/logs">
          <el-icon><Tickets /></el-icon>
          <span>日志</span>
        </el-menu-item>
      </el-menu>
      <el-button :icon="SwitchButton" @click="logout">退出</el-button>
    </el-header>
    <el-main class="page-main">
      <router-view />
    </el-main>
  </el-container>
</template>

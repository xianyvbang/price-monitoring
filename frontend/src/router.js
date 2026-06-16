import { createRouter, createWebHistory } from "vue-router";

import LoginView from "./views/LoginView.vue";
import DashboardView from "./views/DashboardView.vue";
import AccountsView from "./views/AccountsView.vue";
import SettingsView from "./views/SettingsView.vue";
import LogsView from "./views/LogsView.vue";
import GroupRatesView from "./views/GroupRatesView.vue";

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/login", name: "login", component: LoginView, meta: { public: true } },
    { path: "/", name: "dashboard", component: DashboardView },
    { path: "/accounts", name: "accounts", component: AccountsView },
    { path: "/settings", name: "settings", component: SettingsView },
    { path: "/logs", name: "logs", component: LogsView },
    { path: "/accounts/:id/group-rates", name: "group-rates", component: GroupRatesView, props: true }
  ]
});

export default router;

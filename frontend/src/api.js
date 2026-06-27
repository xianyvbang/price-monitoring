const JSON_HEADERS = { "Content-Type": "application/json" };

export async function request(url, options = {}) {
  const response = await fetch(url, {
    credentials: "same-origin",
    ...options,
    headers: {
      ...(options.body ? JSON_HEADERS : {}),
      ...(options.headers || {})
    }
  });
  const text = await response.text();
  let payload = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = { message: text };
    }
  }
  if (!response.ok) {
    const error = new Error(payload?.detail || payload?.message || "请求失败");
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return payload;
}

export const api = {
  session: () => request("/api/session"),
  login: (payload) => request("/login", { method: "POST", body: JSON.stringify(payload) }),
  logout: () => request("/logout", { method: "POST", headers: JSON_HEADERS, body: JSON.stringify({}) }),
  dashboard: (params = {}) => request(`/api/dashboard${queryString(params)}`),
  accounts: (params = {}) => request(`/api/accounts${queryString(params)}`),
  account: (id) => request(`/api/accounts/${id}`),
  createAccount: (payload) => request("/api/accounts", { method: "POST", body: JSON.stringify(payload) }),
  updateAccount: (id, payload) => request(`/api/accounts/${id}`, { method: "PUT", body: JSON.stringify(payload) }),
  deleteAccount: (id) => request(`/api/accounts/${id}`, { method: "DELETE" }),
  bulkAccounts: (payload) => request("/api/accounts/bulk", { method: "POST", body: JSON.stringify(payload) }),
  setEnabled: (id, value) => request(`/api/accounts/${id}/enabled`, { method: "POST", body: JSON.stringify({ is_enabled: value }) }),
  setVisible: (id, value) => request(`/api/accounts/${id}/visible`, { method: "POST", body: JSON.stringify({ is_visible: value }) }),
  setEliminated: (id, value) => request(`/api/accounts/${id}/eliminated`, { method: "POST", body: JSON.stringify({ is_eliminated: value }) }),
  queryAccount: (id) => request(`/api/accounts/${id}/query`, { method: "POST" }),
  queryGroup: (id) => request(`/api/accounts/${id}/group-query`, { method: "POST" }),
  queryAll: () => request("/api/query-all", { method: "POST" }),
  pauseMonitor: (paused) => request("/api/monitor/pause", { method: "POST", body: JSON.stringify({ paused }) }),
  newApiGroups: (id) => request(`/api/accounts/${id}/newapi-groups`),
  sub2ApiGroups: (id) => request(`/api/accounts/${id}/sub2api-groups`),
  selectGroup: (id, payload) => request(`/api/accounts/${id}/selected-group`, { method: "POST", body: JSON.stringify(payload) }),
  groupRates: (id, params = {}) => request(`/api/accounts/${id}/group-rates${queryString(params)}`),
  balanceHistory: (id) => request(`/api/accounts/${id}/balance-history`),
  clearBalanceHistory: (id) => request(`/api/accounts/${id}/balance-history`, { method: "DELETE" }),
  setGroupRateChange: (id, payload) => request(`/api/accounts/${id}/group-rate-change-status`, { method: "POST", body: JSON.stringify(payload) }),
  settings: () => request("/api/settings"),
  saveGeneralSettings: (payload) => request("/api/settings/general", { method: "POST", body: JSON.stringify(payload) }),
  saveSmtpSettings: (payload) => request("/api/settings/smtp", { method: "POST", body: JSON.stringify(payload) }),
  testSmtp: () => request("/api/settings/smtp/test", { method: "POST" }),
  reminders: () => request("/api/settings/reminders"),
  createReminder: (payload) => request("/api/settings/reminders", { method: "POST", body: JSON.stringify(payload) }),
  updateReminder: (id, payload) => request(`/api/settings/reminders/${id}`, { method: "PUT", body: JSON.stringify(payload) }),
  deleteReminder: (id) => request(`/api/settings/reminders/${id}`, { method: "DELETE" }),
  changePassword: (payload) => request("/api/settings/password", { method: "POST", body: JSON.stringify(payload) }),
  opencodeGoAccounts: () => request("/api/opencode-go/accounts"),
  createOpencodeGoAccount: (payload) => request("/api/opencode-go/accounts", { method: "POST", body: JSON.stringify(payload) }),
  bulkOpencodeGoAccounts: (payload) => request("/api/opencode-go/accounts/bulk", { method: "POST", body: JSON.stringify(payload) }),
  updateOpencodeGoAccount: (id, payload) => request(`/api/opencode-go/accounts/${id}`, { method: "PUT", body: JSON.stringify(payload) }),
  deleteOpencodeGoAccount: (id) => request(`/api/opencode-go/accounts/${id}`, { method: "DELETE" }),
  setOpencodeGoEnabled: (id, value) => request(`/api/opencode-go/accounts/${id}/enabled`, { method: "POST", body: JSON.stringify({ is_enabled: value }) }),
  importOpencodeGoSession: (id, payload) => request(`/api/opencode-go/accounts/${id}/session`, { method: "POST", body: JSON.stringify(payload) }),
  refreshOpencodeGo: (id) => request(`/api/opencode-go/accounts/${id}/refresh`, { method: "POST" }),
  refreshAllOpencodeGo: () => request("/api/opencode-go/query-all", { method: "POST" }),
  opencodeGoHistory: (id, params = {}) => request(`/api/opencode-go/accounts/${id}/history${queryString(params)}`),
  opencodeGoApiKey: (id) => request(`/api/opencode-go/accounts/${id}/api-key`),
  opencodeGoPassword: (id) => request(`/api/opencode-go/accounts/${id}/password`),
  logs: (params = {}) => request(`/api/logs${queryString(params)}`),
  clearLogs: () => request("/api/logs", { method: "DELETE" })
};

function queryString(params) {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "") {
      search.set(key, value);
    }
  });
  const value = search.toString();
  return value ? `?${value}` : "";
}

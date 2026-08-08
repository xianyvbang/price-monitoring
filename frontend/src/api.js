const JSON_HEADERS = { "Content-Type": "application/json" };

export async function request(url, options = {}) {
  const response = await fetch(url, {
    credentials: "same-origin",
    cache: "no-store",
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
  dashboardConsumptionSummary: (params = {}) => request(`/api/dashboard/consumption-summary${queryString(params)}`),
  platformDispatch: () => request("/api/platform-dispatch"),
  platformDispatchAccounts: () => request("/api/platform-dispatch/accounts"),
  platformDispatchAccountEvidence: (id, params = {}) => request(`/api/platform-dispatch/accounts/${id}/evidence${queryString(params)}`),
  platformDispatchPolicy: () => request("/api/platform-dispatch/policy"),
  savePlatformDispatchPolicy: (payload) => request("/api/platform-dispatch/policy", { method: "PUT", body: JSON.stringify(payload) }),
  runPlatformDispatchPolicy: () => request("/api/platform-dispatch/policy/run", { method: "POST" }),
  stopPlatformDispatchPolicy: () => request("/api/platform-dispatch/policy/stop", { method: "POST" }),
  platformDispatchActions: (params = {}) => request(`/api/platform-dispatch/actions${queryString(params)}`),
  syncPlatformDispatch: (payload) => request("/api/platform-dispatch/sync", { method: "POST", body: JSON.stringify(payload) }),
  refreshPlatformDispatchEvidence: () => request("/api/platform-dispatch/evidence/refresh", { method: "POST" }),
  platformDispatchJob: () => request("/api/platform-dispatch/job"),
  refreshPlatformDispatch: (payload) => request("/api/platform-dispatch/refresh", { method: "POST", body: JSON.stringify(payload) }),
  excludePlatformDispatchGroup: (id) => request(`/api/platform-dispatch/groups/${id}/exclude`, { method: "POST" }),
  excludePlatformDispatchUngrouped: () => request("/api/platform-dispatch/ungrouped/exclude", { method: "POST" }),
  restorePlatformDispatchGroup: (id) => request(`/api/platform-dispatch/excluded-groups/${id}`, { method: "DELETE" }),
  excludePlatformDispatchAccount: (id) => request(`/api/platform-dispatch/accounts/${id}/exclude`, { method: "POST" }),
  restorePlatformDispatchAccount: (id) => request(`/api/platform-dispatch/excluded-accounts/${id}`, { method: "DELETE" }),
  probePlatformDispatchAccount: (id) => request(`/api/platform-dispatch/accounts/${id}/probe`, { method: "POST" }),
  setPlatformDispatchPriority: (id, priority) => request(`/api/platform-dispatch/accounts/${id}/priority`, { method: "PUT", body: JSON.stringify({ priority }) }),
  setPlatformDispatchProbeModel: (id, model) => request(`/api/platform-dispatch/accounts/${id}/probe-model`, { method: "PUT", body: JSON.stringify({ model }) }),
  setPlatformDispatchGroupProbeModel: (id, model) => request(`/api/platform-dispatch/groups/${id}/probe-model`, { method: "PUT", body: JSON.stringify({ model }) }),
  setPlatformDispatchGroupAutoDispatch: (id, enabled) => request(`/api/platform-dispatch/groups/${id}/auto-dispatch`, { method: "PUT", body: JSON.stringify({ enabled }) }),
  platformDispatchCostSourceOptions: () => request("/api/platform-dispatch/cost-source-options"),
  setPlatformDispatchCostBinding: (id, monitorGroupId) => request(`/api/platform-dispatch/accounts/${id}/cost-binding`, { method: "PUT", body: JSON.stringify({ monitor_group_id: monitorGroupId }) }),
  deletePlatformDispatchCostBinding: (id) => request(`/api/platform-dispatch/accounts/${id}/cost-binding`, { method: "DELETE" }),
  setPlatformDispatchSchedulable: (id, value) => request(`/api/platform-dispatch/accounts/${id}/schedulable`, { method: "POST", body: JSON.stringify({ schedulable: value }) }),
  setPlatformDispatchAutoDispatchPause: (id, payload) => request(`/api/platform-dispatch/accounts/${id}/auto-dispatch-pause`, { method: "PUT", body: JSON.stringify(payload) }),
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
  resetGroupRateChanges: (params = {}) => request(`/api/group-rate-change-status/bulk-reset${queryString(params)}`, { method: "POST" }),
  settings: () => request("/api/settings"),
  saveGeneralSettings: (payload) => request("/api/settings/general", { method: "POST", body: JSON.stringify(payload) }),
  saveSub2ApiSettings: (payload) => request("/api/settings/sub2api", { method: "POST", body: JSON.stringify(payload) }),
  saveCpaSettings: (payload) => request("/api/settings/cpa", { method: "POST", body: JSON.stringify(payload) }),
  saveSmtpSettings: (payload) => request("/api/settings/smtp", { method: "POST", body: JSON.stringify(payload) }),
  testSmtp: () => request("/api/settings/smtp/test", { method: "POST" }),
  reminders: () => request("/api/settings/reminders"),
  createReminder: (payload) => request("/api/settings/reminders", { method: "POST", body: JSON.stringify(payload) }),
  updateReminder: (id, payload) => request(`/api/settings/reminders/${id}`, { method: "PUT", body: JSON.stringify(payload) }),
  deleteReminder: (id) => request(`/api/settings/reminders/${id}`, { method: "DELETE" }),
  changePassword: (payload) => request("/api/settings/password", { method: "POST", body: JSON.stringify(payload) }),
  opencodeGoSettings: () => request("/api/opencode-go/settings"),
  saveOpencodeGoSettings: (payload) => request("/api/opencode-go/settings", { method: "POST", body: JSON.stringify(payload) }),
  setOpencodeGoCpaAutoDelete: (enabled) => request("/api/opencode-go/settings/cpa-auto-delete", { method: "POST", body: JSON.stringify({ enabled }) }),
  opencodeGoAccounts: (params = {}) => request(`/api/opencode-go/accounts${queryString(params)}`),
  createOpencodeGoAccount: (payload) => request("/api/opencode-go/accounts", { method: "POST", body: JSON.stringify(payload) }),
  bulkOpencodeGoAccounts: (payload) => request("/api/opencode-go/accounts/bulk", { method: "POST", body: JSON.stringify(payload) }),
  updateOpencodeGoAccount: (id, payload) => request(`/api/opencode-go/accounts/${id}`, { method: "PUT", body: JSON.stringify(payload) }),
  deleteOpencodeGoAccount: (id) => request(`/api/opencode-go/accounts/${id}`, { method: "DELETE" }),
  bulkDeleteOpencodeGoAccounts: (ids) => request("/api/opencode-go/accounts", { method: "DELETE", body: JSON.stringify({ account_ids: ids }) }),
  setOpencodeGoEnabled: (id, value) => request(`/api/opencode-go/accounts/${id}/enabled`, { method: "POST", body: JSON.stringify({ is_enabled: value }) }),
  opencodeGoSession: (id) => request(`/api/opencode-go/accounts/${id}/session`),
  importOpencodeGoSession: (id, payload) => request(`/api/opencode-go/accounts/${id}/session`, { method: "POST", body: JSON.stringify(payload) }),
  refreshOpencodeGo: (id) => request(`/api/opencode-go/accounts/${id}/refresh`, { method: "POST" }),
  acquireOpencodeGo: (id) => request(`/api/opencode-go/accounts/${id}/acquire`, { method: "POST" }),
  opencodeGoReferralCache: (id) => request(`/api/opencode-go/accounts/${id}/referral`, { method: "GET" }),
  opencodeGoReferral: (id) => request(`/api/opencode-go/accounts/${id}/referral`, { method: "POST" }),
  claimOpencodeGoReferral: (id) => request(`/api/opencode-go/accounts/${id}/referral/claim`, { method: "POST" }),
  batchClaimOpencodeGoReferral: (ids) => request("/api/opencode-go/referral/claim-batch", { method: "POST", body: JSON.stringify({ account_ids: ids }) }),
  refreshAllOpencodeGo: () => request("/api/opencode-go/query-all", { method: "POST" }),
  opencodeGoImportLogs: (params = {}) => request(`/api/opencode-go/import-logs${queryString(params)}`),
  opencodeGoHistory: (id, params = {}) => request(`/api/opencode-go/accounts/${id}/history${queryString(params)}`),
  opencodeGoApiKey: (id) => request(`/api/opencode-go/accounts/${id}/api-key`),
  opencodeGoPassword: (id) => request(`/api/opencode-go/accounts/${id}/password`),
  opencodeGoSub2ApiGroups: () => request("/api/opencode-go/sub2api/groups"),
  importOpencodeGoToSub2Api: (id, payload) => request(`/api/opencode-go/accounts/${id}/import-sub2api`, { method: "POST", body: JSON.stringify(payload) }),
  bulkImportOpencodeGoToSub2Api: (payload) => request("/api/opencode-go/accounts/import-sub2api", { method: "POST", body: JSON.stringify(payload) }),
  importOpencodeGoToCpa: (id) => request(`/api/opencode-go/accounts/${id}/import-cpa`, { method: "POST", body: JSON.stringify({}) }),
  bulkImportOpencodeGoToCpa: (payload) => request("/api/opencode-go/accounts/import-cpa", { method: "POST", body: JSON.stringify(payload) }),
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

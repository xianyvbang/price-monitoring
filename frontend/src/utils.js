export const platforms = ["newApi", "sub2Api"];

export function formatTime(value) {
  if (!value) {
    return "-";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  const parts = new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false
  }).formatToParts(date);
  const byType = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${byType.year}-${byType.month}-${byType.day} ${byType.hour}:${byType.minute}:${byType.second}`;
}

export function displayValue(value, fallback = "-") {
  if (value === null || value === undefined || value === "") {
    return fallback;
  }
  return value;
}

export function amountWithUnit(value, unit = "") {
  const amount = displayValue(value);
  return unit && amount !== "-" ? `${amount} ${unit}` : amount;
}

export function boolValue(value) {
  return value === true || value === 1 || value === "1" || value === "true";
}

export function selectedGroupIds(account) {
  const values = account?.selected_group_ids ?? account?.selectedGroupIds ?? [];
  if (Array.isArray(values)) {
    return values.filter(Boolean).map(String);
  }
  return String(values || "")
    .split(/[|;\n]/)
    .map((item) => item.trim())
    .filter(Boolean);
}

export function normalizeAccountForm(account = {}) {
  const groupIds = selectedGroupIds(account);
  return {
    id: account.id || "",
    platform: account.platform || "newApi",
    name: account.name || "",
    base_url: account.base_url || account.baseUrl || "",
    recharge_url: account.recharge_url || account.rechargeUrl || "",
    recharge_paid_amount: account.recharge_paid_amount ?? account.rechargePaidAmount ?? 1,
    recharge_received_amount: account.recharge_received_amount ?? account.rechargeReceivedAmount ?? 1,
    note: account.note || "",
    key_id: groupIds[0] || account.key_id || account.keyId || account.selected_group_id || account.selectedGroupId || "",
    monitor_group_ids: groupIds,
    api_key: "",
    email: account.email || "",
    password: "",
    login_extra_params: account.login_extra_params || account.loginExtraParams || "",
    access_token: "",
    refresh_token: "",
    user_id: account.user_id || account.userId || "",
    threshold: account.threshold ?? "",
    is_visible: account.is_visible ?? account.isVisible ?? true,
    is_enabled: account.is_enabled ?? account.isEnabled ?? true
  };
}

export function accountCredentialsText(account) {
  const groups = selectedGroupIds(account).join(", ") || "未选择";
  if (account.platform === "sub2Api") {
    return `当前分组: ${account.selected_group_id || "未选择"} / 监控分组: ${groups} / apiKey: ${configured(account.has_api_key)} / refreshToken: ${configured(account.has_refresh_token)} / accessToken: ${configured(account.has_access_token)} / email: ${configured(account.has_email)} / password: ${configured(account.has_password)}`;
  }
  return `accessToken: ${configured(account.has_access_token)} / userId: ${configured(account.has_user_id)} / 当前分组: ${account.selected_group_id || "未选择"} / 监控分组: ${groups}`;
}

export function configured(value) {
  return boolValue(value) ? "已配置" : "未配置";
}

export function parseJsonLoose(value) {
  if (!value) {
    return null;
  }
  if (typeof value !== "string") {
    return value;
  }
  try {
    return JSON.parse(value);
  } catch {
    return value;
  }
}

export function toNumberOrNull(value) {
  if (value === null || value === undefined || value === "") {
    return null;
  }
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

export function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

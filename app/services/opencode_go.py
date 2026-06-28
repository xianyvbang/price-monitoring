from __future__ import annotations

import json
import re
from typing import Any, Callable, Optional

import httpx

from app.models import utc_now
from app.security import decrypt_value

LogCallback = Callable[[str, str, str], None]

OPENCODE_BASE_URL = "https://opencode.ai"
OPENCODE_GO_PATH = "/go"
OPENCODE_GO_LITE_JS_URL_SETTING = "opencode_go_lite_subscription_js_url"
OPENCODE_GO_LITE_SERVER_ID_SETTING = "opencode_go_lite_subscription_server_id"
SESSION_GET_REFERENCE_ID = "9bc4808361cdaee17059a8d3822b36ee8c9a0d93f1adc289fa1926998e3c9768"
LITE_SUBSCRIPTION_GET_REFERENCE_ID = "c7389bd0e731f80f49593e5ee53835475f4e28594dd6bd83eb229bab753498cd"
LITE_SUBSCRIPTION_SERVER_INSTANCE = "server-fn:3"
KEY_LIST_REFERENCE_IDS = (
    "def2ab20a296ef06465b1c3cf86da4ea983c0696e7a5708b9468aaed85083d6b",
    "c22cd964237ba79f2f9b95faa2a14b804f870d1bab49279463379cc6a0fd0c85",
)
SERVER_REFERENCE_ALIASES = {
    "session.get": ("session.get", "session", "sessionGet", "session_get", "querySessionInfo", "querySessionInfo_query"),
    "lite.subscription.get": (
        "lite.subscription.get",
        "subscription",
        "usage",
        "goUsage",
        "liteSubscription",
        "lite_subscription",
        "queryLiteSubscription",
        "queryLiteSubscription_query",
    ),
    "key.list": ("key.list", "keys", "keyList", "key_list", "listKeys", "listKeys_query"),
}
SERVER_REFERENCE_ID_RE = re.compile(r"[0-9a-f]{64}", re.IGNORECASE)
LITE_SUBSCRIPTION_QUERY_RE = re.compile(
    r"queryLiteSubscription_query\s*=\s*createServerReference\(\s*[\"']([0-9a-f]{64})[\"']",
    re.IGNORECASE,
)
SERVER_FN_USAGE_WINDOW_RE = re.compile(
    r"(?P<name>rollingUsage|weeklyUsage|monthlyUsage)\s*:\s*\$R\[\d+\]\s*=\s*\{(?P<body>.*?)\}",
    re.DOTALL,
)
SERVER_FN_FIELD_RE = re.compile(
    r"(?P<key>status|resetInSec|usagePercent)\s*:\s*(?P<value>\"[^\"]*\"|'[^']*'|-?\d+(?:\.\d+)?)",
    re.DOTALL,
)
QUERY_TIMEOUT_MS = 45_000
DEFAULT_BROWSER_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Origin": OPENCODE_BASE_URL,
    "Referer": f"{OPENCODE_BASE_URL}{OPENCODE_GO_PATH}",
    "User-Agent": "Mozilla/5.0",
}
_LITE_SUBSCRIPTION_REFERENCE_CACHE: dict[str, str] = {}


async def refresh_opencode_go_account(
    account: Any,
    secret_key: str,
    timeout: float,
    log: LogCallback | None = None,
    lite_subscription_js_url: str | None = None,
    lite_subscription_server_id: str | None = None,
) -> dict[str, Any]:
    storage_state = _decrypt_json(_account_value(account, "storage_state_enc"), secret_key)
    if not storage_state:
        return _invalid("缺少 OpenCode Go 登录态，请先用本地浏览器登录后导入登录态 JSON 或 Cookie")
    try:
        result = await _run_http_refresh(
            storage_state,
            _account_value(account, "workspace_id"),
            timeout,
            log,
            lite_subscription_js_url,
            lite_subscription_server_id,
        )
    except Exception as exc:
        message = _friendly_refresh_error(exc)
        return _invalid(message)
    result["checked_at"] = utc_now()
    return result


async def query_opencode_server_reference(
    client: httpx.AsyncClient,
    reference_id: str,
    args: list[Any],
    instance: str = LITE_SUBSCRIPTION_SERVER_INSTANCE,
) -> Any:
    response = await client.post(
        f"{OPENCODE_BASE_URL}/_server?id={reference_id}",
        headers={
            **DEFAULT_BROWSER_HEADERS,
            "Content-Type": "application/json",
            "X-Server-Id": reference_id,
            "X-Server-Instance": instance,
        },
        json=_serialize_server_args(args),
    )
    return _parse_server_reference_response(response)


async def query_lite_subscription_usage(
    client: httpx.AsyncClient,
    reference_id: str,
    workspace_id: str,
) -> Any:
    args = json.dumps(_serialize_server_args([workspace_id]), ensure_ascii=False, separators=(",", ":"))
    response = await client.get(
        f"{OPENCODE_BASE_URL}/_server",
        params={"id": reference_id, "args": args},
        headers={
            **DEFAULT_BROWSER_HEADERS,
            "X-Server-Id": reference_id,
            "X-Server-Instance": LITE_SUBSCRIPTION_SERVER_INSTANCE,
        },
    )
    return _parse_server_reference_response(response)


def extract_lite_subscription_reference_id(source: str) -> str:
    match = LITE_SUBSCRIPTION_QUERY_RE.search(source or "")
    if not match:
        raise ValueError("未在 JS 文件中找到 queryLiteSubscription_query 的 server id")
    return match.group(1)


def validate_opencode_go_lite_js_url(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if not re.match(r"^https://opencode\.ai/_build/assets/[^?#]+\.js(?:[?#].*)?$", text, re.IGNORECASE):
        raise ValueError("JS 文件地址必须是 https://opencode.ai/_build/assets/*.js")
    return text


def validate_server_reference_id(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if not SERVER_REFERENCE_ID_RE.fullmatch(text):
        raise ValueError("X-Server-Id 格式不正确")
    return text


async def fetch_lite_subscription_reference_id(js_url: str, timeout: float = 15.0) -> str:
    url = validate_opencode_go_lite_js_url(js_url)
    if not url:
        raise ValueError("JS 文件地址不能为空")
    async with httpx.AsyncClient(headers=DEFAULT_BROWSER_HEADERS, follow_redirects=True, timeout=timeout) as client:
        return await resolve_lite_subscription_reference_id(client, url)


async def resolve_lite_subscription_reference_id(
    client: httpx.AsyncClient,
    js_url: str | None,
    reference_id: str | None = None,
) -> str:
    configured_reference_id = validate_server_reference_id(reference_id)
    if configured_reference_id:
        return configured_reference_id
    url = validate_opencode_go_lite_js_url(js_url)
    if not url:
        return LITE_SUBSCRIPTION_GET_REFERENCE_ID
    cached = _LITE_SUBSCRIPTION_REFERENCE_CACHE.get(url)
    if cached:
        return cached
    response = await client.get(
        url,
        headers={
            **DEFAULT_BROWSER_HEADERS,
            "Accept": "application/javascript, text/javascript, */*",
            "Referer": f"{OPENCODE_BASE_URL}{OPENCODE_GO_PATH}",
        },
    )
    response.raise_for_status()
    try:
        reference_id = extract_lite_subscription_reference_id(response.text)
    except ValueError as exc:
        raise RuntimeError(f"{exc}，OpenCode 前端接口可能已更新") from exc
    _LITE_SUBSCRIPTION_REFERENCE_CACHE[url] = reference_id
    return reference_id


def _parse_server_reference_response(response: httpx.Response) -> Any:
    response.raise_for_status()
    if response.headers.get("X-Error"):
        raise RuntimeError(_response_text(response) or "OpenCode server reference returned an error")
    content_type = response.headers.get("content-type", "")
    if content_type.startswith("application/json"):
        return response.json()
    parsed_usage = parse_server_function_usage_response(response.text)
    if parsed_usage:
        return parsed_usage
    if content_type.startswith("text/plain"):
        return response.text
    return response.text


def parse_server_function_usage_response(text: str) -> dict[str, Any]:
    value = str(text or "")
    if "rollingUsage" not in value and "weeklyUsage" not in value and "monthlyUsage" not in value:
        return {}
    usage: dict[str, Any] = {}
    for match in SERVER_FN_USAGE_WINDOW_RE.finditer(value):
        window: dict[str, Any] = {}
        for field in SERVER_FN_FIELD_RE.finditer(match.group("body")):
            key = field.group("key")
            raw_value = field.group("value").strip()
            if raw_value.startswith(("'", '"')) and raw_value.endswith(("'", '"')):
                window[key] = raw_value[1:-1]
                continue
            window[key] = _optional_number(raw_value)
        usage[match.group("name")] = window
    return usage


def normalize_usage_result(subscription: Any, keys_payload: Any, workspace_id: str | None = None, session_payload: Any = None) -> dict[str, Any]:
    subscription_data = _unwrap_data(subscription)
    key_items = _extract_keys(keys_payload)
    api_key = _best_api_key(key_items)
    api_key_masked = _mask_api_key(api_key) if api_key else _best_masked_api_key(key_items)
    raw = {
        "subscription": _safe_raw(subscription_data),
        "keys": _safe_raw(key_items),
    }
    if session_payload is not None:
        raw["session"] = _safe_raw(_summarize_session(session_payload))
    return {
        "is_valid": True,
        "workspace_id": workspace_id,
        "rolling_usage": _normalize_usage(subscription_data.get("rollingUsage") or subscription_data.get("rolling_usage")),
        "weekly_usage": _normalize_usage(subscription_data.get("weeklyUsage") or subscription_data.get("weekly_usage")),
        "monthly_usage": _normalize_usage(subscription_data.get("monthlyUsage") or subscription_data.get("monthly_usage")),
        "api_key": api_key,
        "api_key_masked": api_key_masked,
        "raw": raw,
    }


def public_usage_window(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = {}
    if not isinstance(value, dict):
        value = {}
    usage_percent = _optional_number(value.get("usage_percent", value.get("usagePercent")))
    reset_in_sec = _optional_number(value.get("reset_in_sec", value.get("resetInSec")))
    return {
        "usage_percent": usage_percent,
        "usagePercent": usage_percent,
        "reset_in_sec": reset_in_sec,
        "resetInSec": reset_in_sec,
    }


def mask_api_key(value: str | None) -> str:
    return _mask_api_key(value)


async def _run_http_refresh(
    storage_state: Any,
    workspace_id: str | None,
    timeout: float,
    log: LogCallback | None,
    lite_subscription_js_url: str | None,
    lite_subscription_server_id: str | None,
) -> dict[str, Any]:
    timeout = max(10.0, min(QUERY_TIMEOUT_MS, float(timeout or QUERY_TIMEOUT_MS)))
    cookies = _cookies_from_storage_state(storage_state)
    server_ids = _server_ids_from_storage_state(storage_state)
    async with httpx.AsyncClient(cookies=cookies, headers=DEFAULT_BROWSER_HEADERS, follow_redirects=True, timeout=timeout) as client:
        response = await client.get(OPENCODE_BASE_URL)
        response.raise_for_status()
        if "auth.opencode.ai" in str(response.url) or "/auth" in str(response.url):
            raise RuntimeError("OpenCode 登录态已失效，请重新导入")
        session = None
        if not workspace_id:
            session = await _query_first_server_reference(
                client,
                _server_reference_ids(server_ids, "session.get", [SESSION_GET_REFERENCE_ID]),
                [],
                "session.get",
            )
            workspace_id = _workspace_id_from_session(session)
        if not workspace_id:
            raise RuntimeError("缺少 OpenCode Workspace ID，请在导入登录态时填写 Workspace ID")
        lite_subscription_reference_id = await resolve_lite_subscription_reference_id(client, lite_subscription_js_url, lite_subscription_server_id)
        subscription = await query_lite_subscription_usage(client, lite_subscription_reference_id, workspace_id)
        try:
            keys_payload = await _query_first_server_reference(
                client,
                _server_reference_ids(server_ids, "key.list", KEY_LIST_REFERENCE_IDS),
                [workspace_id],
                "key.list",
            )
        except Exception as exc:
            keys_payload = {"data": []}
            _log(log, "warning", "opencode-go", f"OpenCode Go API key 查询失败，已仅刷新用量: {exc}")
        result = normalize_usage_result(subscription, keys_payload, workspace_id, session)
        result["storage_state"] = storage_state
        _log(log, "info", "opencode-go", f"OpenCode Go 刷新成功: workspace={workspace_id}")
        return result


async def _query_first_server_reference(
    client: httpx.AsyncClient,
    reference_ids: list[str],
    args: list[Any],
    label: str,
) -> Any:
    errors = []
    for reference_id in reference_ids:
        try:
            return await query_opencode_server_reference(client, reference_id, args)
        except Exception as exc:
            errors.append(f"{reference_id[:8]}: {exc}")
    raise RuntimeError(f"OpenCode {label} 接口不可用，OpenCode 前端接口可能已更新或 server id 无效: " + "; ".join(errors))


def _cookies_from_storage_state(storage_state: Any) -> httpx.Cookies:
    if not isinstance(storage_state, dict):
        raise RuntimeError("OpenCode 登录态格式不正确")
    cookie_items = storage_state.get("cookies")
    if not isinstance(cookie_items, list) or not cookie_items:
        raise RuntimeError("OpenCode 登录态 cookies 为空")
    cookies = httpx.Cookies()
    has_auth_cookie = False
    for item in cookie_items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        value = str(item.get("value") or "")
        if not name:
            continue
        domain = str(item.get("domain") or "opencode.ai").strip() or "opencode.ai"
        path = str(item.get("path") or "/").strip() or "/"
        cookies.set(name, value, domain=domain, path=path)
        if name == "auth" and value:
            has_auth_cookie = True
    if not has_auth_cookie:
        raise RuntimeError("OpenCode 登录态缺少 auth Cookie，请从 DevTools 的 Application 或 Network 请求头复制 auth=...")
    return cookies


def _server_ids_from_storage_state(storage_state: Any) -> dict[str, list[str]]:
    if not isinstance(storage_state, dict):
        return {}
    server_ids: dict[str, list[str]] = {}
    containers = []
    for key in ("serverIds", "server_ids", "serverReferences", "server_references"):
        value = storage_state.get(key)
        if isinstance(value, dict):
            containers.append(value)
    for container in containers:
        for label, aliases in SERVER_REFERENCE_ALIASES.items():
            for alias in aliases:
                _append_server_reference_ids(server_ids.setdefault(label, []), container.get(alias))
    for label, aliases in SERVER_REFERENCE_ALIASES.items():
        for alias in aliases:
            _append_server_reference_ids(server_ids.setdefault(label, []), storage_state.get(alias))
    for key in ("serverId", "serverID", "server_id"):
        _append_server_reference_ids(server_ids.setdefault("lite.subscription.get", []), storage_state.get(key))
    return {key: value for key, value in server_ids.items() if value}


def _server_reference_ids(server_ids: dict[str, list[str]], label: str, defaults: tuple[str, ...] | list[str]) -> list[str]:
    values: list[str] = []
    for value in server_ids.get(label, []):
        _append_server_reference_ids(values, value)
    for value in defaults:
        _append_server_reference_ids(values, value)
    return values


def _append_server_reference_ids(target: list[str], value: Any) -> None:
    if value is None:
        return
    if isinstance(value, (list, tuple, set)):
        for item in value:
            _append_server_reference_ids(target, item)
        return
    if isinstance(value, dict):
        for key in ("id", "serverId", "serverID", "server_id", "value"):
            _append_server_reference_ids(target, value.get(key))
        return
    match = SERVER_REFERENCE_ID_RE.search(str(value))
    if not match:
        return
    reference_id = match.group(0)
    if reference_id not in target:
        target.append(reference_id)


def _serialize_server_args(args: list[Any]) -> dict[str, Any]:
    return {
        "t": {
            "t": 9,
            "i": 0,
            "l": len(args),
            "a": [_serialize_server_value(item) for item in args],
            "o": 0,
        },
        "f": 31,
        "m": [],
    }


def _serialize_server_value(value: Any) -> dict[str, Any]:
    if value is None:
        return {"t": 8}
    if isinstance(value, bool):
        return {"t": 0, "s": value}
    if isinstance(value, (int, float)):
        return {"t": 0, "s": value}
    if isinstance(value, str):
        return {"t": 1, "s": value}
    if isinstance(value, list):
        return {"t": 9, "i": 0, "l": len(value), "a": [_serialize_server_value(item) for item in value], "o": 0}
    if isinstance(value, dict):
        keys = list(value.keys())
        return {
            "t": 10,
            "i": 1,
            "p": {
                "k": [str(key) for key in keys],
                "v": [_serialize_server_value(value[key]) for key in keys],
                "s": 1,
            },
            "o": 0,
        }
    return {"t": 1, "s": str(value)}


def _workspace_id_from_session(payload: Any) -> Optional[str]:
    data = _unwrap_data(payload)
    if not isinstance(data, dict):
        return None
    for key in ("workspaceID", "workspaceId", "workspace_id", "activeWorkspaceID", "activeWorkspaceId", "currentWorkspaceId"):
        value = data.get(key)
        if value:
            return str(value)
    for key in ("workspace", "activeWorkspace", "currentWorkspace"):
        workspace = data.get(key)
        if isinstance(workspace, dict):
            value = workspace.get("id") or workspace.get("workspaceID") or workspace.get("workspaceId")
            if value:
                return str(value)
    workspaces = data.get("workspaces")
    if isinstance(workspaces, list):
        for workspace in workspaces:
            if isinstance(workspace, dict) and workspace.get("id"):
                return str(workspace["id"])
    return None


def _summarize_session(payload: Any) -> dict[str, Any]:
    data = _unwrap_data(payload)
    if not isinstance(data, dict):
        return {}
    summary: dict[str, Any] = {}
    workspace_id = _workspace_id_from_session(data)
    if workspace_id:
        summary["workspace_id"] = workspace_id
    for key in ("user", "account"):
        value = data.get(key)
        if isinstance(value, dict):
            summary[key] = {
                item_key: item_value
                for item_key, item_value in value.items()
                if item_key in {"id", "email", "name"}
            }
    workspaces = data.get("workspaces")
    if isinstance(workspaces, list):
        summary["workspaces"] = [
            {
                item_key: item_value
                for item_key, item_value in workspace.items()
                if item_key in {"id", "name", "slug"}
            }
            for workspace in workspaces
            if isinstance(workspace, dict)
        ][:5]
    return summary


def _account_value(account: Any, key: str, default: Any = None) -> Any:
    if isinstance(account, dict):
        return account.get(key, default)
    try:
        return account[key]
    except (KeyError, IndexError, TypeError):
        return default


def _decrypt_json(value: Any, secret_key: str) -> Any:
    text = decrypt_value(value, secret_key)
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _normalize_usage(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        value = {}
    usage_percent = _optional_number(value.get("usagePercent", value.get("usage_percent")))
    reset_in_sec = _optional_number(value.get("resetInSec", value.get("reset_in_sec")))
    return {
        "usage_percent": usage_percent,
        "usagePercent": usage_percent,
        "reset_in_sec": reset_in_sec,
        "resetInSec": reset_in_sec,
    }


def _extract_keys(payload: Any) -> list[dict[str, Any]]:
    data = _unwrap_data(payload)
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("items", "keys", "list", "records", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _best_api_key(items: list[dict[str, Any]]) -> Optional[str]:
    for item in reversed(items):
        value = item.get("key") or item.get("api_key") or item.get("apiKey") or item.get("token")
        if value and "*" not in str(value):
            return str(value)
    return None


def _best_masked_api_key(items: list[dict[str, Any]]) -> Optional[str]:
    for item in reversed(items):
        value = item.get("key") or item.get("api_key") or item.get("apiKey") or item.get("token")
        if value:
            return str(value)
    return None


def _unwrap_data(payload: Any) -> Any:
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload if isinstance(payload, dict) else {}


def _safe_raw(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: ("***" if _is_sensitive_key(str(key)) else _safe_raw(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [_safe_raw(item) for item in value]
    return value


def _is_sensitive_key(key: str) -> bool:
    normalized = key.replace("-", "_").lower()
    return "password" in normalized or "token" in normalized or "secret" in normalized or normalized in {"key", "api_key", "apikey"}


def _mask_api_key(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= 12:
        return text[:3] + "*" * max(0, len(text) - 6) + text[-3:]
    return text[:8] + "*" * (len(text) - 12) + text[-4:]


def _optional_number(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _invalid(message: str) -> dict[str, Any]:
    return {"is_valid": False, "invalid_message": message, "checked_at": utc_now()}


def _friendly_refresh_error(exc: Exception) -> str:
    text = str(exc)
    if "server runtime export changed" in text or "server reference" in text.lower() or "接口" in text:
        return f"OpenCode 前端接口可能已更新: {text}"
    return f"OpenCode Go 刷新失败: {text}"

def _response_text(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except Exception:
        return response.text[:300]
    if isinstance(payload, dict):
        for key in ("message", "detail", "error"):
            if payload.get(key):
                return str(payload[key])[:300]
    return str(payload)[:300]


def _log(log: LogCallback | None, level: str, category: str, message: str) -> None:
    if log:
        log(level, category, message)

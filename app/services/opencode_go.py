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
OPENCODE_GO_KEY_LIST_JS_URL_SETTING = "opencode_go_key_list_js_url"
OPENCODE_GO_KEY_LIST_SERVER_ID_SETTING = "opencode_go_key_list_server_id"
SESSION_GET_REFERENCE_ID = "9bc4808361cdaee17059a8d3822b36ee8c9a0d93f1adc289fa1926998e3c9768"
LITE_SUBSCRIPTION_GET_REFERENCE_ID = "c7389bd0e731f80f49593e5ee53835475f4e28594dd6bd83eb229bab753498cd"
KEY_LIST_GET_REFERENCE_ID = "c22cd964237ba79f2f9b95faa2a14b804f870d1bab49279463379cc6a0fd0c85"
KEY_LIST_DEFAULT_JS_URL = "https://opencode.ai/_build/assets/index-PbCOrg8_.js"
LITE_SUBSCRIPTION_SERVER_INSTANCE = "server-fn:3"
KEY_LIST_SERVER_INSTANCE = "server-fn:2"
KEY_LIST_REFERENCE_IDS = (
    KEY_LIST_GET_REFERENCE_ID,
    "def2ab20a296ef06465b1c3cf86da4ea983c0696e7a5708b9468aaed85083d6b",
)
# 邀请奖励 referral：从 opencode 前端 index-DtPYjwk4.js 提取的 createServerReference ID
OPENCODE_GO_REFERRAL_QUERY_SERVER_ID_SETTING = "opencode_go_referral_query_server_id"
OPENCODE_GO_REFERRAL_QUERY_JS_URL_SETTING = "opencode_go_referral_query_js_url"
OPENCODE_GO_REFERRAL_ACTION_SERVER_ID_SETTING = "opencode_go_referral_action_server_id"
REFERRAL_QUERY_GET_REFERENCE_ID = "2a0b2fef5fd2ec9eff0cb5d4955e4ada4eece21fac85591ed4c09630168d4844"
REFERRAL_USAGE_PREVIEW_REFERENCE_ID = "46625df0aecf05f270f7ae4612cde374d11350c8abaf8649027572228b8af150"
REFERRAL_ACTION_REFERENCE_ID = "f386778c1b78eade3e6acff87c9284e02fcd86826463c080526143c4fe8fff23"
REFERRAL_QUERY_SERVER_INSTANCE = "server-fn:2"
# action（mutation）的 server-instance 未知，按列表顺序回退尝试
REFERRAL_ACTION_SERVER_INSTANCES = ("server-fn:2", "server-fn:3")
REFERRAL_QUERY_REFERENCE_IDS = (
    REFERRAL_QUERY_GET_REFERENCE_ID,
    REFERRAL_USAGE_PREVIEW_REFERENCE_ID,
)
REFERRAL_QUERY_RE = re.compile(
    r"queryGoReferral_query\s*=\s*createServerReference\(\s*[\"']([0-9a-f]{64})[\"']",
    re.IGNORECASE,
)
REFERRAL_STATUS_CLAIMED = {"claimed", "applied", "used", "done", "received", "completed", "redeemed"}
REFERRAL_STATUS_UNCLAIMED = {"available", "pending", "claimable", "unclaimed", "ready", "active"}
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
    "referral.query": (
        "referral.query",
        "referral",
        "goReferral",
        "queryGoReferral",
        "queryGoReferral_query",
        "query_go_referral",
        "queryGoReferralUsagePreview",
        "queryGoReferralUsagePreview_query",
        "referralQuery",
        "referral_query",
    ),
    "referral.action": (
        "referral.action",
        "applyGoReferralReward",
        "applyGoReferralReward_action",
        "referralAction",
        "referral_action",
        "claimReferral",
        "claimGoReferralReward",
    ),
}
SERVER_REFERENCE_ID_RE = re.compile(r"[0-9a-f]{64}", re.IGNORECASE)
LITE_SUBSCRIPTION_QUERY_RE = re.compile(
    r"queryLiteSubscription_query\s*=\s*createServerReference\(\s*[\"']([0-9a-f]{64})[\"']",
    re.IGNORECASE,
)
KEY_LIST_QUERY_RE = re.compile(
    r"listKeys_query\s*=\s*createServerReference\(\s*[\"']([0-9a-f]{64})[\"']",
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
# 邀请奖励 queryGoReferral 返回结构：顶层 referralCode/hasReferral/rewardAmount + rewards[] 数组
REFERRAL_HAS_FIELD_RE = re.compile(r"hasReferral\s*:\s*(?P<value>!0|!1|true|false|0|1)", re.IGNORECASE)
REFERRAL_CODE_RE = re.compile(r"referralCode\s*:\s*\"([^\"]*)\"")
REFERRAL_AMOUNT_RE = re.compile(r"rewardAmount\s*:\s*(-?\d+(?:\.\d+)?)")
# 每个 reward 对象（含 id/source/status/email/amount/timeCreated/timeApplied）
REFERRAL_REWARD_OBJECT_RE = re.compile(
    r"\{[^{}]*?\bid\b\s*:\s*\"[^\"]*\"[^{}]*?\}",
    re.DOTALL,
)
REFERRAL_REWARD_FIELD_RE = re.compile(
    r"(?P<key>id|source|status|email|amount)\s*:\s*"
    r"(?P<value>\"(?:\\.|[^\"])*\"|'(?:\\.|[^'])*'|-?\d+(?:\.\d+)?|!0|!1|true|false|null)",
    re.DOTALL,
)
REFERRAL_REWARD_TIME_RE = re.compile(
    r"(?P<key>timeCreated|timeApplied)\s*:\s*\$R\[\d+\]\s*=\s*new Date\(\s*\"(?P<value>[^\"]*)\"\s*\)",
    re.DOTALL,
)
SERVER_FN_KEY_OBJECT_RE = re.compile(
    r"\{(?P<body>[^{}]*(?:\bkey\b|\bapiKey\b|\bapi_key\b|\btoken\b)\s*:[^{}]*)\}",
    re.DOTALL,
)
SERVER_FN_KEY_FIELD_RE = re.compile(
    r"(?P<key>[A-Za-z_$][\w$]*|\"[^\"]+\"|'[^']+')\s*:\s*(?P<value>\"(?:\\.|[^\"])*\"|'(?:\\.|[^'])*'|-?\d+(?:\.\d+)?|true|false|null)",
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
_KEY_LIST_REFERENCE_CACHE: dict[str, str] = {}
_REFERRAL_REFERENCE_CACHE: dict[str, str] = {}


async def refresh_opencode_go_account(
    account: Any,
    secret_key: str,
    timeout: float,
    log: LogCallback | None = None,
    lite_subscription_js_url: str | None = None,
    lite_subscription_server_id: str | None = None,
    key_list_js_url: str | None = None,
    key_list_server_id: str | None = None,
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
            key_list_js_url,
            key_list_server_id,
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


async def query_key_list(
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
            "X-Server-Instance": KEY_LIST_SERVER_INSTANCE,
        },
    )
    return _parse_server_reference_response(response)


async def query_referral(
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
            "X-Server-Instance": REFERRAL_QUERY_SERVER_INSTANCE,
        },
    )
    return _parse_server_reference_response(response)


async def apply_referral_reward(
    client: httpx.AsyncClient,
    reference_id: str,
    args: list[Any],
    instances: tuple[str, ...] = REFERRAL_ACTION_SERVER_INSTANCES,
) -> Any:
    """POST 领取邀请奖励。action 的 server-instance 未知，按列表顺序回退尝试。"""
    body = _serialize_server_args(args)
    errors: list[str] = []
    for instance in instances:
        try:
            response = await client.post(
                f"{OPENCODE_BASE_URL}/_server?id={reference_id}",
                headers={
                    **DEFAULT_BROWSER_HEADERS,
                    "Content-Type": "application/json",
                    "X-Server-Id": reference_id,
                    "X-Server-Instance": instance,
                },
                json=body,
            )
            return _parse_server_reference_response(response)
        except Exception as exc:
            errors.append(f"{instance}: {exc}")
    raise RuntimeError("OpenCode 领取邀请奖励接口不可用: " + "; ".join(errors))


def extract_lite_subscription_reference_id(source: str) -> str:
    match = LITE_SUBSCRIPTION_QUERY_RE.search(source or "")
    if not match:
        raise ValueError("未在 JS 文件中找到 queryLiteSubscription_query 的 server id")
    return match.group(1)


def extract_key_list_reference_id(source: str) -> str:
    match = KEY_LIST_QUERY_RE.search(source or "")
    if not match:
        raise ValueError("未在 JS 文件中找到 listKeys_query 的 server id")
    return match.group(1)


def extract_referral_reference_id(source: str) -> str:
    match = REFERRAL_QUERY_RE.search(source or "")
    if not match:
        raise ValueError("未在 JS 文件中找到 queryGoReferral_query 的 server id")
    return match.group(1)


def validate_opencode_go_lite_js_url(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if not re.match(r"^https://opencode\.ai/_build/assets/[^?#]+\.js(?:[?#].*)?$", text, re.IGNORECASE):
        raise ValueError("JS 文件地址必须是 https://opencode.ai/_build/assets/*.js")
    return text


def validate_opencode_go_key_list_js_url(value: str | None) -> str:
    text = str(value or "").strip() or KEY_LIST_DEFAULT_JS_URL
    if not re.match(r"^https://opencode\.ai/_build/assets/[^?#]+\.js(?:[?#].*)?$", text, re.IGNORECASE):
        raise ValueError("API key JS 文件地址必须是 https://opencode.ai/_build/assets/*.js")
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


async def fetch_key_list_reference_id(js_url: str | None = None, timeout: float = 15.0) -> str:
    url = validate_opencode_go_key_list_js_url(js_url)
    async with httpx.AsyncClient(headers=DEFAULT_BROWSER_HEADERS, follow_redirects=True, timeout=timeout) as client:
        return await resolve_key_list_reference_id(client, url)


def validate_opencode_go_referral_js_url(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if not re.match(r"^https://opencode\.ai/_build/assets/[^?#]+\.js(?:[?#].*)?$", text, re.IGNORECASE):
        raise ValueError("JS 文件地址必须是 https://opencode.ai/_build/assets/*.js")
    return text


async def fetch_referral_reference_id(js_url: str | None = None, timeout: float = 15.0) -> str:
    url = validate_opencode_go_referral_js_url(js_url)
    if not url:
        return REFERRAL_QUERY_GET_REFERENCE_ID
    async with httpx.AsyncClient(headers=DEFAULT_BROWSER_HEADERS, follow_redirects=True, timeout=timeout) as client:
        return await resolve_referral_reference_id(client, url)


async def resolve_referral_reference_id(
    client: httpx.AsyncClient,
    js_url: str | None,
    reference_id: str | None = None,
) -> str:
    configured_reference_id = validate_server_reference_id(reference_id)
    if configured_reference_id:
        return configured_reference_id
    url = validate_opencode_go_referral_js_url(js_url)
    if not url:
        return REFERRAL_QUERY_GET_REFERENCE_ID
    cached = _REFERRAL_REFERENCE_CACHE.get(url)
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
        reference_id = extract_referral_reference_id(response.text)
    except ValueError as exc:
        raise RuntimeError(f"{exc}，OpenCode 前端接口可能已更新") from exc
    _REFERRAL_REFERENCE_CACHE[url] = reference_id
    return reference_id


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


async def resolve_key_list_reference_id(
    client: httpx.AsyncClient,
    js_url: str | None,
    reference_id: str | None = None,
) -> str:
    configured_reference_id = validate_server_reference_id(reference_id)
    if configured_reference_id:
        return configured_reference_id
    url = validate_opencode_go_key_list_js_url(js_url)
    cached = _KEY_LIST_REFERENCE_CACHE.get(url)
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
        reference_id = extract_key_list_reference_id(response.text)
    except ValueError as exc:
        raise RuntimeError(f"{exc}，OpenCode 前端接口可能已更新") from exc
    _KEY_LIST_REFERENCE_CACHE[url] = reference_id
    return reference_id


def _parse_server_reference_response(response: httpx.Response) -> Any:
    response.raise_for_status()
    if response.headers.get("X-Error"):
        raise RuntimeError(_response_text(response) or "OpenCode server reference returned an error")
    parsed_usage = parse_server_function_usage_response(response.text)
    if parsed_usage:
        return parsed_usage
    parsed_keys = parse_server_function_key_response(response.text)
    if parsed_keys:
        return {"data": parsed_keys}
    content_type = response.headers.get("content-type", "")
    if content_type.startswith("application/json"):
        return response.json()
    if content_type.startswith("text/plain"):
        return response.text
    return response.text


def parse_server_function_key_response(text: str) -> list[dict[str, Any]]:
    value = str(text or "")
    if "apiKey" not in value and "api_key" not in value and "key" not in value and "token" not in value:
        return []
    items: list[dict[str, Any]] = []
    for match in SERVER_FN_KEY_OBJECT_RE.finditer(value):
        item: dict[str, Any] = {}
        for field in SERVER_FN_KEY_FIELD_RE.finditer(match.group("body")):
            key = _strip_js_field_name(field.group("key"))
            item[key] = _parse_js_scalar(field.group("value").strip())
        if any(item.get(key) for key in ("key", "api_key", "apiKey", "token")):
            items.append(item)
    return items


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
    subscription_data = _usage_container(subscription)
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


REFERRAL_REWARD_FIELDS = (
    "status",
    "amount",
    "email",
    "source",
    "time",
    "id",
    "description",
    "action",
    "inviteUrl",
    "invite_url",
    "planName",
    "plan_name",
    "currency",
)


def _find_referral_container(value: Any) -> Optional[dict[str, Any]]:
    """递归查找含 reward / referral 字段的 dict。"""
    if isinstance(value, dict):
        if isinstance(value.get("reward"), dict) or any(
            isinstance(value.get(k), dict) for k in ("referral", "goReferral", "invite", "invitation")
        ):
            return value
        for key in ("data", "result", "referral", "goReferral", "invite", "invitation"):
            found = _find_referral_container(value.get(key))
            if found is not None:
                return found
        for item in value.values():
            found = _find_referral_container(item)
            if found is not None:
                return found
    if isinstance(value, list):
        for item in value:
            found = _find_referral_container(item)
            if found is not None:
                return found
    return None


def _extract_reward_object(container: dict[str, Any]) -> dict[str, Any]:
    reward = container.get("reward")
    if isinstance(reward, dict) and reward:
        return reward
    for k in ("referral", "goReferral", "invite", "invitation"):
        inner = container.get(k)
        if isinstance(inner, dict) and inner:
            inner_reward = inner.get("reward")
            if isinstance(inner_reward, dict) and inner_reward:
                return inner_reward
            return inner
    return {}


def _parse_referral_text(text: str) -> Optional[dict[str, Any]]:
    """从 server-fn grid 序列化文本里提取 queryGoReferral 结构。

    形如: ;0x...;((self.$R=...)[\"server-fn:2\"]=[],($R=>$R[0]={referralCode:\"...\",hasReferral:!0,
        rewardAmount:500,rewards:$R[1]=[$R[2]={id:\"ref_...\",source:\"invitee\",status:\"applied\",
        email:\"...\",amount:500,timeCreated:$R[3]=new Date(\"...\"),timeApplied:$R[4]=new Date(\"...\")}]})(...))
    """
    value = str(text or "")
    if "referralCode" not in value and "hasReferral" not in value and "rewards:" not in value and '"reward"' not in value:
        return None
    has_match = REFERRAL_HAS_FIELD_RE.search(value)
    code_match = REFERRAL_CODE_RE.search(value)
    amount_match = REFERRAL_AMOUNT_RE.search(value)
    if not has_match and not code_match and not amount_match and "rewards:" not in value:
        return None
    result: dict[str, Any] = {}
    if code_match:
        result["referralCode"] = code_match.group(1)
        result["referral_code"] = code_match.group(1)
    if has_match:
        raw = has_match.group("value")
        result["hasReferral"] = raw in ("!0", "true", "1")
        result["has_referral"] = result["hasReferral"]
    if amount_match:
        result["rewardAmount"] = float(amount_match.group(1))
        result["reward_amount"] = result["rewardAmount"]

    rewards: list[dict[str, Any]] = []
    # 截取 rewards:[...] 区段，按对象 {id:...} 逐个提取
    rewards_section = value
    rewards_idx = value.find("rewards:")
    if rewards_idx >= 0:
        rewards_section = value[rewards_idx:]
    for obj_match in REFERRAL_REWARD_OBJECT_RE.finditer(rewards_section):
        body = obj_match.group(0)
        item: dict[str, Any] = {}
        for field in REFERRAL_REWARD_FIELD_RE.finditer(body):
            key = field.group("key")
            raw_val = field.group("value")
            item[key] = _parse_referral_scalar(raw_val)
        for time_field in REFERRAL_REWARD_TIME_RE.finditer(body):
            item[time_field.group("key")] = time_field.group("value")
        if item.get("id") or item.get("status"):
            rewards.append(item)
    if rewards:
        result["rewards"] = rewards
    return result or None


def _parse_referral_scalar(value: str) -> Any:
    text = str(value or "").strip()
    if text in ("!0", "true"):
        return True
    if text in ("!1", "false"):
        return False
    if text == "null":
        return None
    return _parse_js_scalar(text)


def _parse_referral_struct(value: Any) -> Optional[dict[str, Any]]:
    """从已结构化（dict / list）的返回里提取 referral 结构。"""
    container = _find_referral_container(value)
    if container is None and isinstance(value, dict):
        container = value
    if not isinstance(container, dict):
        return None
    # 真实结构：顶层 referralCode/hasReferral/rewardAmount + rewards[]
    if any(k in container for k in ("referralCode", "hasReferral", "rewardAmount")) or isinstance(
        container.get("rewards"), list
    ):
        result: dict[str, Any] = {}
        if "referralCode" in container:
            result["referralCode"] = container["referralCode"]
            result["referral_code"] = container["referralCode"]
        if "hasReferral" in container:
            result["hasReferral"] = bool(container["hasReferral"])
            result["has_referral"] = result["hasReferral"]
        if "rewardAmount" in container:
            result["rewardAmount"] = container["rewardAmount"]
            result["reward_amount"] = container["rewardAmount"]
        rewards_raw = container.get("rewards")
        if isinstance(rewards_raw, list) and rewards_raw:
            result["rewards"] = [r for r in rewards_raw if isinstance(r, dict)]
        return result or None
    return None


def _normalize_referral(parsed: dict[str, Any]) -> dict[str, Any]:
    """把提取到的 referral 结构归一为 {has_reward, claimed, reward}。"""
    rewards = parsed.get("rewards") or []
    # 是否有推荐：hasReferral 优先，否则看 rewards 是否非空
    if "hasReferral" in parsed:
        has_reward = bool(parsed["hasReferral"])
    elif rewards:
        has_reward = True
    else:
        has_reward = None
    # 是否已用：任一 reward.status ∈ claimed 集合 → 已领；否则若有未领项 → 未领
    claimed: Optional[bool] = None
    if rewards:
        statuses = [str(r.get("status") or "").strip().lower() for r in rewards]
        any_claimed = any(s in REFERRAL_STATUS_CLAIMED for s in statuses)
        if any_claimed:
            claimed = True
        elif any(s in REFERRAL_STATUS_UNCLAIMED for s in statuses):
            claimed = False
        elif any(s for s in statuses):
            # 有状态但都不在已知集合 → 若 timeApplied 存在则视为已领
            has_time_applied = any(bool(r.get("timeApplied")) for r in rewards)
            claimed = True if has_time_applied else None
    # reward 汇总：取第一个 reward 的关键字段 + 顶层 referralCode/amount
    reward_summary: dict[str, Any] = {}
    if "referralCode" in parsed:
        reward_summary["referralCode"] = parsed["referralCode"]
    if "rewardAmount" in parsed:
        reward_summary["rewardAmount"] = parsed["rewardAmount"]
    if rewards:
        first = rewards[0]
        for k in ("id", "source", "status", "email", "amount", "timeCreated", "timeApplied"):
            if k in first:
                reward_summary[k] = first[k]
    if claimed is False and "has_referral" not in parsed:
        pass
    return {"has_reward": has_reward, "claimed": claimed, "reward": reward_summary, "rewards": rewards}


def parse_referral_payload(payload: Any) -> dict[str, Any]:
    """解析 server-fn queryGoReferral 返回 → {has_reward, claimed, reward, rewards}。

    支持两种形态：
    1) grid 序列化文本（self.$R=... / $R[0]={referralCode:...,rewards:[{...}]}）—— 文本正则提取。
    2) 已结构化 dict（{referralCode, hasReferral, rewardAmount, rewards:[]}）。

    状态判定：rewards[].status —— 'applied'/'claimed'/'used' 等 = 已用（已领）；
              'available'/'pending' 等 = 未用（可领）。has_reward 来自 hasReferral。
    """
    parsed: Optional[dict[str, Any]] = None
    if isinstance(payload, str):
        parsed = _parse_referral_text(payload)
        if not parsed:
            # 可能是 JSON 字符串
            try:
                parsed = _parse_referral_struct(json.loads(payload))
            except json.JSONDecodeError:
                parsed = None
    elif isinstance(payload, (dict, list)):
        parsed = _parse_referral_struct(payload)
    if not parsed:
        return {"has_reward": None, "claimed": None, "reward": {}, "rewards": []}
    return _normalize_referral(parsed)


async def query_referral_for_account(
    account: Any,
    secret_key: str,
    timeout: float,
    log: LogCallback | None = None,
    referral_query_js_url: str | None = None,
    referral_query_server_id: str | None = None,
) -> dict[str, Any]:
    storage_state = _decrypt_json(_account_value(account, "storage_state_enc"), secret_key)
    if not storage_state:
        return _referral_invalid("缺少 OpenCode Go 登录态，无法查询邀请奖励")
    timeout_s = max(10.0, min(QUERY_TIMEOUT_MS, float(timeout or QUERY_TIMEOUT_MS)))
    try:
        cookies = _cookies_from_storage_state(storage_state)
        server_ids = _server_ids_from_storage_state(storage_state)
        async with httpx.AsyncClient(cookies=cookies, headers=DEFAULT_BROWSER_HEADERS, follow_redirects=True, timeout=timeout_s) as client:
            resp = await client.get(OPENCODE_BASE_URL)
            resp.raise_for_status()
            if "auth.opencode.ai" in str(resp.url) or "/auth" in str(resp.url):
                raise RuntimeError("OpenCode 登录态已失效，请重新导入")
            reference_ids: list[str] = []
            configured_id = validate_server_reference_id(referral_query_server_id)
            if configured_id:
                reference_ids.append(configured_id)
            for value in server_ids.get("referral.query", []):
                _append_server_reference_ids(reference_ids, value)
            try:
                resolved = await resolve_referral_reference_id(client, referral_query_js_url, referral_query_server_id)
                _append_server_reference_ids(reference_ids, resolved)
            except Exception:
                pass
            for value in REFERRAL_QUERY_REFERENCE_IDS:
                _append_server_reference_ids(reference_ids, value)
            workspace_id = _account_value(account, "workspace_id")
            payload = None
            errors: list[str] = []
            for reference_id in reference_ids:
                try:
                    payload = await query_referral(client, reference_id, workspace_id)
                    break
                except Exception as exc:
                    errors.append(f"{reference_id[:8]}: {exc}")
            if payload is None:
                raise RuntimeError("OpenCode 邀请奖励查询接口不可用: " + "; ".join(errors))
            parsed = parse_referral_payload(payload)
            _log(log, "info", "opencode-go", f"OpenCode Go 邀请奖励查询成功 has_reward={parsed.get('has_reward')} claimed={parsed.get('claimed')}")
            return {
                "is_valid": True,
                "has_reward": parsed.get("has_reward"),
                "claimed": parsed.get("claimed"),
                "reward": parsed.get("reward"),
                "rewards": parsed.get("rewards"),
                "raw": _safe_referral_raw(payload),
                "checked_at": utc_now(),
            }
    except Exception as exc:
        return _referral_invalid(_friendly_referral_error(exc))


async def claim_referral_reward_for_account(
    account: Any,
    secret_key: str,
    timeout: float,
    log: LogCallback | None = None,
    referral_query_js_url: str | None = None,
    referral_query_server_id: str | None = None,
    referral_action_server_id: str | None = None,
) -> dict[str, Any]:
    storage_state = _decrypt_json(_account_value(account, "storage_state_enc"), secret_key)
    if not storage_state:
        return _referral_invalid("缺少 OpenCode Go 登录态，无法领取邀请奖励")
    timeout_s = max(10.0, min(QUERY_TIMEOUT_MS, float(timeout or QUERY_TIMEOUT_MS)))
    try:
        cookies = _cookies_from_storage_state(storage_state)
        server_ids = _server_ids_from_storage_state(storage_state)
        async with httpx.AsyncClient(cookies=cookies, headers=DEFAULT_BROWSER_HEADERS, follow_redirects=True, timeout=timeout_s) as client:
            resp = await client.get(OPENCODE_BASE_URL)
            resp.raise_for_status()
            if "auth.opencode.ai" in str(resp.url) or "/auth" in str(resp.url):
                raise RuntimeError("OpenCode 登录态已失效，请重新导入")
            workspace_id = _account_value(account, "workspace_id")

            # 1) 先查询拿 reward 信息，作为 action 参数
            query_reference_ids: list[str] = []
            configured_query = validate_server_reference_id(referral_query_server_id)
            if configured_query:
                query_reference_ids.append(configured_query)
            for value in server_ids.get("referral.query", []):
                _append_server_reference_ids(query_reference_ids, value)
            try:
                resolved = await resolve_referral_reference_id(client, referral_query_js_url, referral_query_server_id)
                _append_server_reference_ids(query_reference_ids, resolved)
            except Exception:
                pass
            for value in REFERRAL_QUERY_REFERENCE_IDS:
                _append_server_reference_ids(query_reference_ids, value)
            reward_payload = None
            query_errors: list[str] = []
            for reference_id in query_reference_ids:
                try:
                    reward_payload = await query_referral(client, reference_id, workspace_id)
                    break
                except Exception as exc:
                    query_errors.append(f"{reference_id[:8]}: {exc}")
            reward_info_full = parse_referral_payload(reward_payload) if reward_payload is not None else {}
            reward_info = reward_info_full.get("reward") or {}
            # 如果已领，直接返回
            if reward_payload is not None:
                parsed = reward_info_full
                if parsed.get("claimed") is True:
                    _log(log, "info", "opencode-go", "OpenCode Go 邀请奖励已领取，无需重复领取")
                    return {
                        "is_valid": True,
                        "claimed": True,
                        "message": "邀请奖励已领取，无需重复领取",
                        "reward": parsed.get("reward"),
                        "rewards": parsed.get("rewards"),
                        "raw": _safe_referral_raw(reward_payload),
                    }

            # 2) 调 applyGoReferralReward_action：参数为 [workspace_id, reward_id]（两个字符串）。
            #    从查询结果里取 reward.id（首个 reward 的 id）。
            action_reference_id = validate_server_reference_id(referral_action_server_id) or REFERRAL_ACTION_REFERENCE_ID
            for value in server_ids.get("referral.action", []):
                action_candidate = _first_server_reference_id(value)
                if action_candidate:
                    action_reference_id = action_candidate
                    break
            action_args: list[Any] = [workspace_id]
            rewards_list = reward_info_full.get("rewards") or []
            # 优先取未领取的 reward id，避免重复 apply 已领的
            unclaimed_reward = next(
                (r for r in rewards_list
                 if str(r.get("status") or "").strip().lower() in REFERRAL_STATUS_UNCLAIMED),
                None,
            )
            target_reward = unclaimed_reward or (rewards_list[0] if rewards_list else None)
            reward_id = ""
            if target_reward and target_reward.get("id"):
                reward_id = str(target_reward["id"])
                action_args.append(reward_id)
            if not reward_id:
                # 缺少 reward id，无法领取
                return _referral_invalid(
                    "未找到可领取的邀请奖励（缺少 reward id）；该账号可能没有未领取的奖励"
                )
            result = await apply_referral_reward(client, action_reference_id, action_args)
            # 3) 解析领取结果：再看一次 referral 状态，或宽松判定返回非空即成功
            reparse = parse_referral_payload(result) if result is not None else {"has_reward": None, "claimed": None, "reward": {}}
            claimed = reparse.get("claimed")
            if claimed is None:
                # 返回非空 data 视为成功
                unwrapped = _unwrap_data(result)
                claimed = bool(unwrapped) or bool(reparse.get("reward"))
            _log(log, "info", "opencode-go", f"OpenCode Go 领取邀请奖励结果 claimed={claimed}")
            return {
                "is_valid": True,
                "claimed": bool(claimed),
                "message": "领取成功" if claimed else "已提交但未能确认是否领取成功",
                "reward": reparse.get("reward") or reward_info,
                "rewards": reparse.get("rewards") or reward_info_full.get("rewards"),
                "raw": _safe_referral_raw(result),
            }
    except Exception as exc:
        return _referral_invalid(_friendly_referral_error(exc))


def _first_server_reference_id(value: Any) -> Optional[str]:
    ids: list[str] = []
    _append_server_reference_ids(ids, value)
    return ids[0] if ids else None


def _safe_referral_raw(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: ("***" if _is_opencode_referral_sensitive(str(key)) else _safe_referral_raw(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [_safe_referral_raw(item) for item in value]
    return value


def _is_opencode_referral_sensitive(key: str) -> bool:
    normalized = key.replace("-", "_").lower()
    return "password" in normalized or "token" in normalized or "secret" in normalized


def _referral_invalid(message: str) -> dict[str, Any]:
    return {
        "is_valid": False,
        "has_reward": None,
        "claimed": None,
        "reward": {},
        "invalid_message": message,
        "checked_at": utc_now(),
    }


def _friendly_referral_error(exc: Exception) -> str:
    text = str(exc)
    if "server runtime export changed" in text or "server reference" in text.lower() or "接口" in text:
        return f"OpenCode 前端接口可能已更新: {text}"
    return f"OpenCode Go 邀请奖励操作失败: {text}"


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
        "reset_in_sec": reset_in_sec,
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
    key_list_js_url: str | None,
    key_list_server_id: str | None,
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
        subscription_data = _usage_container(subscription)
        if not _has_usage_data(subscription_data):
            raise RuntimeError("OpenCode 用量接口未返回 rollingUsage/weeklyUsage/monthlyUsage，请检查 auth Cookie、Workspace ID 和 X-Server-Id")
        try:
            keys_payload = await _query_first_key_list_reference(
                client,
                await _key_list_reference_ids(client, server_ids, key_list_js_url, key_list_server_id),
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


async def _query_first_key_list_reference(
    client: httpx.AsyncClient,
    reference_ids: list[str],
    args: list[Any],
    label: str,
) -> Any:
    errors = []
    workspace_id = str(args[0] if args else "")
    for reference_id in reference_ids:
        try:
            return await query_key_list(client, reference_id, workspace_id)
        except Exception as exc:
            errors.append(f"{reference_id[:8]}: {exc}")
    raise RuntimeError(f"OpenCode {label} 接口不可用，OpenCode 前端接口可能已更新或 server id 无效: " + "; ".join(errors))


async def _key_list_reference_ids(
    client: httpx.AsyncClient,
    server_ids: dict[str, list[str]],
    js_url: str | None,
    reference_id: str | None,
) -> list[str]:
    values: list[str] = []
    try:
        _append_server_reference_ids(values, await resolve_key_list_reference_id(client, js_url, reference_id))
    except Exception:
        pass
    for value in server_ids.get("key.list", []):
        _append_server_reference_ids(values, value)
    for value in KEY_LIST_REFERENCE_IDS:
        _append_server_reference_ids(values, value)
    return values


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
        "reset_in_sec": reset_in_sec,
    }


def _usage_container(payload: Any) -> dict[str, Any]:
    data = _unwrap_data(payload)
    found = _find_usage_container(data)
    return found if found is not None else {}


def _find_usage_container(value: Any) -> Optional[dict[str, Any]]:
    if isinstance(value, dict):
        if any(key in value for key in ("rollingUsage", "rolling_usage", "weeklyUsage", "weekly_usage", "monthlyUsage", "monthly_usage")):
            return value
        for item in value.values():
            found = _find_usage_container(item)
            if found is not None:
                return found
    if isinstance(value, list):
        for item in value:
            found = _find_usage_container(item)
            if found is not None:
                return found
    return None


def _has_usage_data(value: dict[str, Any]) -> bool:
    for key in ("rollingUsage", "rolling_usage", "weeklyUsage", "weekly_usage", "monthlyUsage", "monthly_usage"):
        window = _normalize_usage(value.get(key))
        if window["usage_percent"] is not None or window["reset_in_sec"] is not None:
            return True
    return False


def _extract_keys(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, str):
        return parse_server_function_key_response(payload)
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
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


def _strip_js_field_name(value: str) -> str:
    text = str(value or "").strip()
    if text.startswith(("'", '"')) and text.endswith(("'", '"')):
        return text[1:-1]
    return text


def _parse_js_scalar(value: str) -> Any:
    text = str(value or "").strip()
    if text.startswith('"') and text.endswith('"'):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text[1:-1]
    if text.startswith("'") and text.endswith("'"):
        return text[1:-1].replace("\\'", "'").replace('\\"', '"')
    if text == "true":
        return True
    if text == "false":
        return False
    if text == "null":
        return None
    number = _optional_number(text)
    return number if number is not None else text


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

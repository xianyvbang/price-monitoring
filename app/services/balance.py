from __future__ import annotations

import json
from typing import Any, Callable, Optional

import httpx

from app.security import decrypt_value

LogCallback = Callable[[str, str, str], None]
SENSITIVE_HEADERS = {"authorization", "cookie", "set-cookie"}
SENSITIVE_FIELDS = {"api_key", "apikey", "password", "access_token", "refresh_token", "token", "secret"}
LOG_VALUE_LIMIT = 2000


def normalize_result(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "is_valid": bool(data.get("is_valid", data.get("isValid", True))),
        "invalid_message": data.get("invalid_message") or data.get("invalidMessage"),
        "remaining": _optional_number(data.get("remaining")),
        "unit": data.get("unit"),
        "plan_name": data.get("plan_name") or data.get("planName"),
        "total": _optional_number(data.get("total")),
        "used": _optional_number(data.get("used")),
        "extra": data.get("extra"),
    }


async def query_account(account: Any, secret_key: str, timeout: float, log: LogCallback | None = None) -> dict[str, Any]:
    try:
        if account["platform"] == "sub2Api":
            return await query_sub2api(account, secret_key, timeout, log)
        if account["platform"] == "newApi":
            return await query_newapi(account, secret_key, timeout, log)
        return normalize_result({"is_valid": False, "invalid_message": "不支持的平台"})
    except httpx.TimeoutException:
        return normalize_result({"is_valid": False, "invalid_message": "请求超时"})
    except httpx.HTTPError as exc:
        return normalize_result({"is_valid": False, "invalid_message": f"请求失败: {exc}"})
    except Exception as exc:
        return normalize_result({"is_valid": False, "invalid_message": f"查询异常: {exc}"})


async def query_sub2api(account: Any, secret_key: str, timeout: float, log: LogCallback | None = None) -> dict[str, Any]:
    api_key = decrypt_value(account["api_key_enc"], secret_key)
    if not api_key:
        return normalize_result({"is_valid": False, "invalid_message": "缺少 apiKey"})
    url = f"{account['base_url'].rstrip('/')}/v1/usage"
    headers = {"Authorization": f"Bearer {api_key}"}
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await _logged_get(client, url, headers, log, account)
        response.raise_for_status()
        payload = response.json()
    remaining = payload.get("remaining")
    if remaining is None and isinstance(payload.get("quota"), dict):
        remaining = payload["quota"].get("remaining")
    if remaining is None:
        remaining = payload.get("balance")
    unit = payload.get("unit")
    if not unit and isinstance(payload.get("quota"), dict):
        unit = payload["quota"].get("unit")
    return normalize_result(
        {
            "is_valid": payload.get("is_active", payload.get("isValid", True)),
            "remaining": remaining,
            "unit": unit or "USD",
            "plan_name": payload.get("planName") or payload.get("plan_name"),
            "total": payload.get("total"),
            "used": payload.get("used"),
            "extra": payload.get("extra"),
        }
    )


async def query_sub2api_group(account: Any, secret_key: str, timeout: float, log: LogCallback | None = None) -> dict[str, Any]:
    try:
        return await _query_sub2api_group(account, secret_key, timeout, log)
    except httpx.TimeoutException:
        return normalize_result({"is_valid": False, "invalid_message": "请求超时"})
    except httpx.HTTPError as exc:
        return normalize_result({"is_valid": False, "invalid_message": f"请求失败: {exc}"})
    except Exception as exc:
        return normalize_result({"is_valid": False, "invalid_message": f"查询异常: {exc}"})


async def _query_sub2api_group(account: Any, secret_key: str, timeout: float, log: LogCallback | None = None) -> dict[str, Any]:
    key_id = decrypt_value(_account_value(account, "key_id_enc"), secret_key)
    email = decrypt_value(_account_value(account, "email_enc"), secret_key)
    password = decrypt_value(_account_value(account, "password_enc"), secret_key)
    if not email:
        return normalize_result({"is_valid": False, "invalid_message": "缺少 email"})
    if not password:
        return normalize_result({"is_valid": False, "invalid_message": "缺少 password"})

    base_url = account["base_url"].rstrip("/")
    async with httpx.AsyncClient(timeout=timeout) as client:
        login_response = await _logged_post_json(
            client,
            f"{base_url}/api/v1/auth/login",
            {"email": email, "password": password},
            {},
            log,
            account,
        )
        login_response.raise_for_status()
        login_payload = login_response.json()
        token = _extract_access_token(login_payload)
        if not token:
            login_data = _unwrap_response_data(login_payload)
            if isinstance(login_data, dict) and login_data.get("requires_2fa"):
                return normalize_result({"is_valid": False, "invalid_message": "账号启用了 2FA，无法自动登录查组"})
            return normalize_result({"is_valid": False, "invalid_message": "登录成功但响应中没有 access_token"})

        auth_headers = {"Authorization": f"Bearer {token}"}
        groups_response = await _logged_get(client, f"{base_url}/api/v1/groups/available", auth_headers, log, account)
        groups_response.raise_for_status()
        groups_payload = groups_response.json()

        rates_response = await _logged_get(client, f"{base_url}/api/v1/groups/rates", auth_headers, log, account)
        rates_response.raise_for_status()
        rates_payload = rates_response.json()

    groups = _extract_groups(groups_payload)
    rates = _extract_group_rates(rates_payload)
    summary = _build_group_rate_summary(key_id, groups, rates)
    raw_json = _compact_json(summary)
    return normalize_result({"is_valid": True, "plan_name": summary["title"], "extra": raw_json})


async def query_newapi(account: Any, secret_key: str, timeout: float, log: LogCallback | None = None) -> dict[str, Any]:
    access_token = decrypt_value(account["access_token_enc"], secret_key)
    user_id = decrypt_value(account["user_id_enc"], secret_key)
    if not access_token:
        return normalize_result({"is_valid": False, "invalid_message": "缺少 accessToken"})
    if not user_id:
        return normalize_result({"is_valid": False, "invalid_message": "缺少 userId"})
    url = f"{account['base_url'].rstrip('/')}/api/user/self"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}",
        "New-Api-User": user_id,
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await _logged_get(client, url, headers, log, account)
        response.raise_for_status()
        payload = response.json()
    if payload.get("success") and payload.get("data"):
        data = payload["data"]
        quota = _optional_number(data.get("quota")) or 0
        used_quota = _optional_number(data.get("used_quota")) or 0
        return normalize_result(
            {
                "plan_name": data.get("group") or "默认套餐",
                "remaining": quota / 500000,
                "used": used_quota / 500000,
                "total": (quota + used_quota) / 500000,
                "unit": "USD",
            }
        )
    return normalize_result(
        {
            "is_valid": False,
            "invalid_message": payload.get("message") or "查询失败",
        }
    )


def _optional_number(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _account_value(account: Any, key: str) -> Any:
    if isinstance(account, dict):
        return account.get(key)
    try:
        return account[key]
    except (KeyError, IndexError):
        return None


async def _logged_get(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    log: LogCallback | None,
    account: Any,
) -> httpx.Response:
    if log:
        log(
            "info",
            "http",
            f"OUT {account['platform']} / {account['name']} GET {url} request={_log_text({'headers': _safe_headers(headers)})}",
        )
    response = await client.get(url, headers=headers)
    if log:
        log(
            "info",
            "http",
            f"OUT {account['platform']} / {account['name']} GET {url} response={_log_text(_response_payload(response))}",
        )
    return response


async def _logged_post_json(
    client: httpx.AsyncClient,
    url: str,
    json_payload: dict[str, Any],
    headers: dict[str, str],
    log: LogCallback | None,
    account: Any,
) -> httpx.Response:
    if log:
        log(
            "info",
            "http",
            f"OUT {account['platform']} / {account['name']} POST {url} request={_log_text({'headers': _safe_headers(headers), 'json': json_payload})}",
        )
    response = await client.post(url, json=json_payload, headers=headers)
    if log:
        log(
            "info",
            "http",
            f"OUT {account['platform']} / {account['name']} POST {url} response={_log_text(_response_payload(response))}",
        )
    return response


def _safe_headers(headers: dict[str, str]) -> dict[str, str]:
    return {key: "***" if key.lower() in SENSITIVE_HEADERS else value for key, value in headers.items()}


def _response_payload(response: Any) -> dict[str, Any]:
    payload: Any
    try:
        payload = response.json()
    except Exception:
        payload = getattr(response, "text", None)
        if payload is None:
            content = getattr(response, "content", b"")
            payload = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else str(content)
    return {
        "status": getattr(response, "status_code", None),
        "headers": _safe_headers(dict(getattr(response, "headers", {}) or {})),
        "body": payload,
    }


def _log_text(value: Any) -> str:
    text = json.dumps(_mask_sensitive(value), ensure_ascii=False, default=str)
    if len(text) > LOG_VALUE_LIMIT:
        return text[:LOG_VALUE_LIMIT] + "...<truncated>"
    return text


def _compact_json(value: Any) -> str:
    return _log_text(value)


def _mask_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "***" if _is_sensitive_field(str(key)) else _mask_sensitive(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_mask_sensitive(item) for item in value]
    return value


def _is_sensitive_field(key: str) -> bool:
    normalized = key.replace("-", "_").lower()
    return normalized in SENSITIVE_FIELDS or "password" in normalized or "token" in normalized or "secret" in normalized


def _unwrap_response_data(payload: Any) -> Any:
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def _extract_access_token(payload: Any) -> Optional[str]:
    data = _unwrap_response_data(payload)
    if isinstance(data, dict):
        token = data.get("access_token") or data.get("accessToken") or data.get("token")
        if token:
            return str(token)
    if isinstance(payload, dict):
        token = payload.get("access_token") or payload.get("accessToken") or payload.get("token")
        if token:
            return str(token)
    return None


def _extract_groups(payload: Any) -> list[dict[str, Any]]:
    data = _unwrap_response_data(payload)
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("items", "groups", "list"):
            items = data.get(key)
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
    return []


def _extract_group_rates(payload: Any) -> dict[str, float]:
    data = _unwrap_response_data(payload)
    if not isinstance(data, dict):
        return {}
    rates: dict[str, float] = {}
    for key, value in data.items():
        rate = _optional_number(value)
        if rate is not None:
            rates[str(key)] = rate
    return rates


def _build_group_rate_summary(key_id: Optional[str], groups: list[dict[str, Any]], rates: dict[str, float]) -> dict[str, Any]:
    group_summaries = [_summarize_group(group, rates) for group in groups]
    if key_id:
        target_group = next((item for item in group_summaries if str(item.get("id")) == str(key_id)), None)
        if target_group:
            title = f"{target_group.get('name') or key_id} 倍率 {target_group['effective_rate_multiplier']}"
            return {
                "title": title,
                "group_id": key_id,
                "group": target_group,
                "rates": rates,
            }
        rate = rates.get(str(key_id))
        if rate is not None:
            return {
                "title": f"分组 {key_id} 专属倍率 {rate}",
                "group_id": key_id,
                "group": None,
                "rates": rates,
            }
        return {
            "title": f"未找到分组 {key_id}",
            "group_id": key_id,
            "groups": group_summaries,
            "rates": rates,
        }
    return {
        "title": f"可用分组 {len(group_summaries)} 个",
        "groups": group_summaries,
        "rates": rates,
    }


def _summarize_group(group: dict[str, Any], rates: dict[str, float]) -> dict[str, Any]:
    group_id = group.get("id")
    group_key = str(group_id)
    default_rate = _optional_number(group.get("rate_multiplier"))
    user_rate = rates.get(group_key)
    return {
        "id": group_id,
        "name": group.get("name"),
        "platform": group.get("platform"),
        "status": group.get("status"),
        "default_rate_multiplier": default_rate,
        "user_rate_multiplier": user_rate,
        "effective_rate_multiplier": user_rate if user_rate is not None else default_rate,
    }

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import httpx

from app.security import decrypt_value

LogCallback = Callable[[str, str, str], None]
SENSITIVE_HEADERS = {"authorization", "cookie", "set-cookie"}
SENSITIVE_FIELDS = {"api_key", "apikey", "password", "access_token", "refresh_token", "token", "secret"}
LOG_VALUE_LIMIT = 2000
SUB2API_TOKEN_DEFAULT_TTL_SECONDS = 50 * 60
SUB2API_TOKEN_REFRESH_SKEW_SECONDS = 60
_SUB2API_TOKEN_CACHE: dict[str, dict[str, Any]] = {}


def normalize_result(data: dict[str, Any]) -> dict[str, Any]:
    result = {
        "is_valid": bool(data.get("is_valid", data.get("isValid", True))),
        "invalid_message": data.get("invalid_message") or data.get("invalidMessage"),
        "remaining": _optional_number(data.get("remaining")),
        "unit": data.get("unit"),
        "plan_name": data.get("plan_name") or data.get("planName"),
        "total": _optional_number(data.get("total")),
        "used": _optional_number(data.get("used")),
        "extra": data.get("extra"),
    }
    for passthrough_key in ("available_groups", "refreshed_access_token", "refreshed_refresh_token"):
        if passthrough_key in data:
            result[passthrough_key] = data[passthrough_key]
    return result


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


async def query_sub2api_group_options(account: Any, secret_key: str, timeout: float, log: LogCallback | None = None) -> dict[str, Any]:
    try:
        return await _query_sub2api_group_options(account, secret_key, timeout, log)
    except httpx.TimeoutException:
        return normalize_result({"is_valid": False, "invalid_message": "请求超时"})
    except httpx.HTTPError as exc:
        return normalize_result({"is_valid": False, "invalid_message": f"请求失败: {exc}"})
    except Exception as exc:
        return normalize_result({"is_valid": False, "invalid_message": f"查询异常: {exc}"})


async def login_sub2api_tokens(
    base_url: str,
    email: str,
    password: str,
    timeout: float,
    log: LogCallback | None = None,
    account: Any | None = None,
) -> dict[str, Any]:
    email = str(email or "").strip()
    password = str(password or "").strip()
    base_url = str(base_url or "").rstrip("/")
    if not email:
        return normalize_result({"is_valid": False, "invalid_message": "缺少 email，无法重新登录"})
    if not password:
        return normalize_result({"is_valid": False, "invalid_message": "缺少 password，无法重新登录"})
    account_context = account or {"platform": "sub2Api", "name": email, "base_url": base_url}
    login_extra_params = _sub2api_login_extra_params_from_account(account_context)
    cache_key = _sub2api_token_cache_key(base_url, email)
    async with httpx.AsyncClient(timeout=timeout) as client:
        token_result = await _login_sub2api(client, base_url, email, password, login_extra_params, cache_key, log, account_context)
    if isinstance(token_result, dict) and token_result.get("is_valid") is False:
        return token_result
    return {
        "is_valid": True,
        "access_token": token_result.get("access_token"),
        "refresh_token": token_result.get("refresh_token"),
    }


async def _query_sub2api_group(account: Any, secret_key: str, timeout: float, log: LogCallback | None = None) -> dict[str, Any]:
    key_id = decrypt_value(_account_value(account, "key_id_enc"), secret_key)
    api_key = decrypt_value(_account_value(account, "api_key_enc"), secret_key)
    email = decrypt_value(_account_value(account, "email_enc"), secret_key)
    password = decrypt_value(_account_value(account, "password_enc"), secret_key)
    login_extra_params = decrypt_value(_account_value(account, "login_extra_params_enc"), secret_key)
    configured_access_token = decrypt_value(_account_value(account, "access_token_enc"), secret_key)
    configured_refresh_token = decrypt_value(_account_value(account, "refresh_token_enc"), secret_key)
    if not api_key:
        return normalize_result({"is_valid": False, "invalid_message": "缺少 apiKey"})
    if not configured_access_token and not configured_refresh_token:
        if not email:
            return normalize_result({"is_valid": False, "invalid_message": "缺少 refreshToken/accessToken 或 email"})
        if not password:
            return normalize_result({"is_valid": False, "invalid_message": "缺少 refreshToken/accessToken 或 password"})

    base_url = account["base_url"].rstrip("/")
    token_cache_key = _sub2api_token_cache_key(base_url, email or "")
    async with httpx.AsyncClient(timeout=timeout) as client:
        usage_response = await _logged_get(client, f"{base_url}/v1/usage", {"Authorization": f"Bearer {api_key}"}, log, account)
        usage_response.raise_for_status()
        active_plan_name = _extract_usage_plan_name(usage_response.json())

        token_result = await _resolve_sub2api_access_token(
            client,
            base_url,
            configured_access_token,
            configured_refresh_token,
            email,
            password,
            login_extra_params,
            token_cache_key,
            log,
            account,
        )
        if isinstance(token_result, dict) and token_result.get("is_valid") is False:
            return token_result
        token_state = token_result
        token = token_state["access_token"]
        used_cached_token = bool(token_state.get("used_cached_token"))
        used_configured_token = bool(token_state.get("used_configured_token"))
        if not token:
            token_result = await _login_sub2api_token_state(client, base_url, email, password, login_extra_params, token_cache_key, log, account)
            if token_result.get("is_valid") is False:
                return token_result
            token = token_result["access_token"]
            token_state = token_result
        try:
            groups_payload, rates_payload = await _fetch_sub2api_group_payloads(client, base_url, token, log, account)
            groups = _extract_groups(groups_payload)
            rates = _extract_group_rates(rates_payload)
            active_key_group_id = None
            summary = _build_current_group_rate_summary(key_id, active_key_group_id, active_plan_name, groups, rates)
            if _is_unrecognized_group_summary(summary):
                summary = await _try_match_group_from_api_key(
                    client, base_url, token, log, account, api_key, key_id, active_plan_name, groups, rates, summary
                )
            available_groups = [_summarize_group(group, rates) for group in groups]
        except httpx.HTTPError:
            if not (
                used_configured_token
                or used_cached_token
                or token_state.get("used_refresh_token")
            ):
                raise
            if used_cached_token:
                _SUB2API_TOKEN_CACHE.pop(token_cache_key, None)
            token_result = await _recover_sub2api_token_after_access_failure(
                client,
                base_url,
                configured_refresh_token if used_configured_token else token_state.get("refresh_token"),
                email,
                password,
                login_extra_params,
                token_cache_key,
                log,
                account,
                allow_refresh=not bool(token_state.get("used_refresh_token")),
            )
            if token_result.get("is_valid") is False:
                return token_result
            token = token_result["access_token"]
            token_state = token_result
            groups_payload, rates_payload = await _fetch_sub2api_group_payloads(client, base_url, token, log, account)
            groups = _extract_groups(groups_payload)
            rates = _extract_group_rates(rates_payload)
            active_key_group_id = None
            summary = _build_current_group_rate_summary(key_id, active_key_group_id, active_plan_name, groups, rates)
            if _is_unrecognized_group_summary(summary):
                summary = await _try_match_group_from_api_key(
                    client, base_url, token, log, account, api_key, key_id, active_plan_name, groups, rates, summary
                )
            available_groups = [_summarize_group(group, rates) for group in groups]

    raw_json = _compact_json(summary)
    return normalize_result(
        {
            "is_valid": True,
            "plan_name": summary["title"],
            "extra": raw_json,
            "available_groups": available_groups,
            "refreshed_access_token": token_state.get("persist_access_token"),
            "refreshed_refresh_token": token_state.get("persist_refresh_token"),
        }
    )


async def _query_sub2api_group_options(account: Any, secret_key: str, timeout: float, log: LogCallback | None = None) -> dict[str, Any]:
    selected_group_id = decrypt_value(_account_value(account, "key_id_enc"), secret_key)
    email = decrypt_value(_account_value(account, "email_enc"), secret_key)
    password = decrypt_value(_account_value(account, "password_enc"), secret_key)
    login_extra_params = decrypt_value(_account_value(account, "login_extra_params_enc"), secret_key)
    configured_access_token = decrypt_value(_account_value(account, "access_token_enc"), secret_key)
    configured_refresh_token = decrypt_value(_account_value(account, "refresh_token_enc"), secret_key)
    if not configured_access_token and not configured_refresh_token:
        if not email:
            return normalize_result({"is_valid": False, "invalid_message": "缺少 refreshToken/accessToken 或 email"})
        if not password:
            return normalize_result({"is_valid": False, "invalid_message": "缺少 refreshToken/accessToken 或 password"})

    base_url = account["base_url"].rstrip("/")
    token_cache_key = _sub2api_token_cache_key(base_url, email or "")
    async with httpx.AsyncClient(timeout=timeout) as client:
        token_result = await _resolve_sub2api_access_token(
            client,
            base_url,
            configured_access_token,
            configured_refresh_token,
            email,
            password,
            login_extra_params,
            token_cache_key,
            log,
            account,
        )
        if isinstance(token_result, dict) and token_result.get("is_valid") is False:
            return token_result
        token_state = token_result
        token = token_state["access_token"]
        used_configured_token = bool(token_state.get("used_configured_token"))
        try:
            groups_payload, rates_payload = await _fetch_sub2api_group_payloads(client, base_url, token, log, account)
        except httpx.HTTPError:
            if used_configured_token or token_state.get("used_refresh_token") or token_state.get("used_cached_token"):
                if token_state.get("used_cached_token"):
                    _SUB2API_TOKEN_CACHE.pop(token_cache_key, None)
                token_result = await _recover_sub2api_token_after_access_failure(
                    client,
                    base_url,
                    configured_refresh_token if used_configured_token else token_state.get("refresh_token"),
                    email,
                    password,
                    login_extra_params,
                    token_cache_key,
                    log,
                    account,
                    allow_refresh=not bool(token_state.get("used_refresh_token")),
                )
                if token_result.get("is_valid") is False:
                    return token_result
                token_state = token_result
                groups_payload, rates_payload = await _fetch_sub2api_group_payloads(client, base_url, token_state["access_token"], log, account)
            else:
                raise

    groups = [_summarize_group(group, _extract_group_rates(rates_payload)) for group in _extract_groups(groups_payload)]
    return {
        "is_valid": True,
        "groups": groups,
        "selected_group_id": selected_group_id,
        "selectedGroupId": selected_group_id,
        "refreshed_access_token": token_state.get("persist_access_token"),
        "refreshed_refresh_token": token_state.get("persist_refresh_token"),
        "extra": _compact_json({"groups": groups, "selected_group_id": selected_group_id}),
    }


async def query_newapi(account: Any, secret_key: str, timeout: float, log: LogCallback | None = None) -> dict[str, Any]:
    headers_result = _newapi_auth_headers(account, secret_key)
    if "invalid_message" in headers_result:
        return normalize_result(headers_result)
    url = f"{account['base_url'].rstrip('/')}/api/user/self"
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await _logged_get(client, url, headers_result, log, account)
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


async def query_newapi_group_options(account: Any, secret_key: str, timeout: float, log: LogCallback | None = None) -> dict[str, Any]:
    try:
        headers_result = _newapi_auth_headers(account, secret_key)
        if isinstance(headers_result, dict) and "invalid_message" in headers_result:
            return normalize_result(headers_result)
        base_url = account["base_url"].rstrip("/")
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await _logged_get(client, f"{base_url}/api/user/self/groups", headers_result, log, account)
            response.raise_for_status()
            payload = response.json()
        if isinstance(payload, dict) and payload.get("success") is False:
            return normalize_result({"is_valid": False, "invalid_message": payload.get("message") or "获取分组失败"})
        groups = _extract_newapi_user_groups(payload)
        selected_group_id = decrypt_value(_account_value(account, "key_id_enc"), secret_key)
        return {
            "is_valid": True,
            "groups": groups,
            "selected_group_id": selected_group_id,
            "selectedGroupId": selected_group_id,
            "extra": _compact_json({"groups": groups, "selected_group_id": selected_group_id}),
        }
    except httpx.TimeoutException:
        return normalize_result({"is_valid": False, "invalid_message": "请求超时"})
    except httpx.HTTPError as exc:
        return normalize_result({"is_valid": False, "invalid_message": f"请求失败: {exc}"})
    except Exception as exc:
        return normalize_result({"is_valid": False, "invalid_message": f"查询异常: {exc}"})


async def query_newapi_group(account: Any, secret_key: str, timeout: float, log: LogCallback | None = None) -> dict[str, Any]:
    try:
        selected_group_id = decrypt_value(_account_value(account, "key_id_enc"), secret_key)
        options = await query_newapi_group_options(account, secret_key, timeout, log)
        if not options.get("is_valid"):
            return options
        groups = options.get("groups") if isinstance(options.get("groups"), list) else []
        if not selected_group_id:
            summary = {
                "title": "已获取可用分组",
                "group_id": None,
                "groups": [],
            }
            raw_json = _compact_json(summary)
            return normalize_result({"is_valid": True, "plan_name": summary["title"], "extra": raw_json, "available_groups": groups})
        summary = _build_current_group_rate_summary(selected_group_id, None, None, groups, {})
        raw_json = _compact_json(summary)
        return normalize_result({"is_valid": True, "plan_name": summary["title"], "extra": raw_json, "available_groups": groups})
    except httpx.TimeoutException:
        return normalize_result({"is_valid": False, "invalid_message": "请求超时"})
    except httpx.HTTPError as exc:
        return normalize_result({"is_valid": False, "invalid_message": f"请求失败: {exc}"})
    except Exception as exc:
        return normalize_result({"is_valid": False, "invalid_message": f"查询异常: {exc}"})


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


def _sub2api_login_extra_params_from_account(account: Any) -> str | None:
    value = _account_value(account, "login_extra_params")
    return str(value) if value is not None else None


def _sub2api_login_payload(email: str, password: str, login_extra_params: str | None) -> dict[str, Any]:
    extra_result = _parse_sub2api_login_extra_params(login_extra_params)
    if isinstance(extra_result, dict) and extra_result.get("is_valid") is False and "invalid_message" in extra_result:
        return extra_result
    return {"email": email, "password": password, **extra_result}


def _parse_sub2api_login_extra_params(value: str | None) -> dict[str, Any]:
    text = str(value or "").strip()
    if not text:
        return {}
    if ":" not in text:
        return normalize_result({"is_valid": False, "invalid_message": "登录额外参数格式错误，请使用 key:value"})
    key, raw_value = text.split(":", 1)
    key = key.strip()
    if not key:
        return normalize_result({"is_valid": False, "invalid_message": "登录额外参数格式错误，key 不能为空"})
    normalized_key = key.replace("-", "_").lower()
    if normalized_key in {"email", "password"}:
        return normalize_result({"is_valid": False, "invalid_message": "登录额外参数不能覆盖 email 或 password"})
    return {key: _parse_sub2api_login_extra_value(raw_value.strip())}


def _parse_sub2api_login_extra_value(value: str) -> Any:
    normalized = value.lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    if normalized == "null":
        return None
    number = _optional_number(value)
    if number is not None:
        return int(number) if number.is_integer() and "." not in value and "e" not in normalized else number
    return value


def _newapi_auth_headers(account: Any, secret_key: str) -> dict[str, Any]:
    access_token = decrypt_value(_account_value(account, "access_token_enc"), secret_key)
    user_id = decrypt_value(_account_value(account, "user_id_enc"), secret_key)
    if not access_token:
        return {"is_valid": False, "invalid_message": "缺少 accessToken"}
    if not user_id:
        return {"is_valid": False, "invalid_message": "缺少 userId"}
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}",
        "New-Api-User": user_id,
    }


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


async def _resolve_sub2api_access_token(
    client: httpx.AsyncClient,
    base_url: str,
    configured_access_token: str | None,
    configured_refresh_token: str | None,
    email: str | None,
    password: str | None,
    login_extra_params: str | None,
    cache_key: str,
    log: LogCallback | None,
    account: Any,
) -> dict[str, Any]:
    if configured_access_token:
        return {
            "access_token": configured_access_token,
            "refresh_token": configured_refresh_token,
            "used_cached_token": False,
            "used_configured_token": True,
        }
    if configured_refresh_token:
        refreshed = await _refresh_sub2api_token_state(client, base_url, configured_refresh_token, cache_key, log, account)
        if refreshed.get("is_valid") is False:
            return await _login_sub2api_token_state(client, base_url, email, password, login_extra_params, cache_key, log, account)
        return refreshed
    cached = _get_cached_sub2api_token_state(cache_key)
    if cached:
        if cached.get("refresh_token") and _token_needs_refresh(cached):
            refreshed = await _refresh_sub2api_token_state(client, base_url, str(cached["refresh_token"]), cache_key, log, account)
            if refreshed.get("is_valid") is not False:
                return refreshed
        return {
            "access_token": str(cached["access_token"]),
            "refresh_token": cached.get("refresh_token"),
            "used_cached_token": True,
            "used_configured_token": False,
        }
    return await _login_sub2api_token_state(client, base_url, email, password, login_extra_params, cache_key, log, account)


async def _refresh_sub2api_token_state(
    client: httpx.AsyncClient,
    base_url: str,
    refresh_token: str,
    cache_key: str,
    log: LogCallback | None,
    account: Any,
) -> dict[str, Any]:
    refreshed = await _refresh_sub2api_access_token(client, base_url, refresh_token, cache_key, log, account)
    if isinstance(refreshed, dict) and refreshed.get("is_valid") is False:
        return refreshed
    return {
        **refreshed,
        "used_cached_token": False,
        "used_configured_token": False,
        "used_refresh_token": True,
        "persist_access_token": refreshed.get("access_token"),
        "persist_refresh_token": refreshed.get("refresh_token"),
    }


async def _login_sub2api_token_state(
    client: httpx.AsyncClient,
    base_url: str,
    email: str | None,
    password: str | None,
    login_extra_params: str | None,
    cache_key: str,
    log: LogCallback | None,
    account: Any,
) -> dict[str, Any]:
    if not email:
        return normalize_result({"is_valid": False, "invalid_message": "缺少 email，无法重新登录"})
    if not password:
        return normalize_result({"is_valid": False, "invalid_message": "缺少 password，无法重新登录"})
    token_result = await _login_sub2api(client, base_url, email, password, login_extra_params, cache_key, log, account)
    if isinstance(token_result, dict) and token_result.get("is_valid") is False:
        return token_result
    return {
        **token_result,
        "used_cached_token": False,
        "used_configured_token": False,
        "used_login_token": True,
        "persist_access_token": token_result.get("access_token"),
        "persist_refresh_token": token_result.get("refresh_token"),
    }


async def _recover_sub2api_token_after_access_failure(
    client: httpx.AsyncClient,
    base_url: str,
    refresh_token: str | None,
    email: str | None,
    password: str | None,
    login_extra_params: str | None,
    cache_key: str,
    log: LogCallback | None,
    account: Any,
    *,
    allow_refresh: bool = True,
) -> dict[str, Any]:
    if refresh_token and allow_refresh:
        refreshed = await _refresh_sub2api_token_state(client, base_url, refresh_token, cache_key, log, account)
        if refreshed.get("is_valid") is not False:
            return refreshed
    return await _login_sub2api_token_state(client, base_url, email, password, login_extra_params, cache_key, log, account)


async def _login_sub2api(
    client: httpx.AsyncClient,
    base_url: str,
    email: str,
    password: str,
    login_extra_params: str | None,
    cache_key: str,
    log: LogCallback | None,
    account: Any,
) -> str | dict[str, Any]:
    login_payload_result = _sub2api_login_payload(email, password, login_extra_params)
    if isinstance(login_payload_result, dict) and login_payload_result.get("is_valid") is False:
        return login_payload_result
    login_response = await _logged_post_json(
        client,
        f"{base_url}/api/v1/auth/login",
        login_payload_result,
        {},
        log,
        account,
    )
    if login_response.status_code >= 400:
        return normalize_result({"is_valid": False, "invalid_message": _sub2api_login_error_message(base_url, login_response)})
    login_payload = login_response.json()
    token = _extract_access_token(login_payload)
    if not token:
        login_data = _unwrap_response_data(login_payload)
        if isinstance(login_data, dict) and login_data.get("requires_2fa"):
            return normalize_result({"is_valid": False, "invalid_message": "账号启用了 2FA，无法自动登录查组"})
        return normalize_result({"is_valid": False, "invalid_message": "登录成功但响应中没有 access_token"})
    refresh_token = _extract_refresh_token(login_payload)
    _cache_sub2api_token(cache_key, token, login_payload, refresh_token=refresh_token)
    return {"access_token": token, "refresh_token": refresh_token}


async def _refresh_sub2api_access_token(
    client: httpx.AsyncClient,
    base_url: str,
    refresh_token: str,
    cache_key: str,
    log: LogCallback | None,
    account: Any,
) -> dict[str, Any]:
    refresh_response = await _logged_post_json(
        client,
        f"{base_url}/api/v1/auth/refresh",
        {"refresh_token": refresh_token},
        {"Content-Type": "application/json"},
        log,
        account,
    )
    if refresh_response.status_code >= 400:
        return normalize_result({"is_valid": False, "invalid_message": _sub2api_refresh_error_message(base_url, refresh_response)})
    refresh_payload = refresh_response.json()
    access_token = _extract_access_token(refresh_payload)
    if not access_token:
        return normalize_result({"is_valid": False, "invalid_message": "刷新成功但响应中没有 access_token"})
    next_refresh_token = _extract_refresh_token(refresh_payload) or refresh_token
    _cache_sub2api_token(cache_key, access_token, refresh_payload, refresh_token=next_refresh_token)
    return {"access_token": access_token, "refresh_token": next_refresh_token}


def _sub2api_login_error_message(base_url: str, response: httpx.Response) -> str:
    detail = _extract_response_error_text(response)
    if response.status_code == 400 and _is_2chat_url(base_url):
        message = "2chat 登录接口返回 400，当前站点开启了 Turnstile，人机验证无法由服务端自动完成；请在账号里填写 2chat Web 登录后的 accessToken 再获取分组"
        return f"{message}。{detail}" if detail else message
    message = f"登录失败: HTTP {response.status_code}"
    return f"{message}，{detail}" if detail else message


def _sub2api_refresh_error_message(base_url: str, response: httpx.Response) -> str:
    detail = _extract_response_error_text(response)
    if response.status_code == 400 and _is_2chat_url(base_url):
        message = "refreshToken 刷新 accessToken 失败，请重新从 2chat 登录后复制最新 auth_token 和 refresh_token"
        return f"{message}。{detail}" if detail else message
    message = f"刷新 accessToken 失败: HTTP {response.status_code}"
    return f"{message}，{detail}" if detail else message


def _configured_sub2api_token_error_message(base_url: str) -> str:
    if _is_2chat_url(base_url):
        return "accessToken 无效或已过期，请重新从 2chat 登录后复制最新 accessToken"
    return "accessToken 无效或已过期，请重新登录后复制最新 accessToken"


def _extract_response_error_text(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except Exception:
        text = (getattr(response, "text", "") or "").strip()
        return text[:300]
    if isinstance(payload, dict):
        for key in ("message", "detail", "error", "code"):
            value = payload.get(key)
            if value:
                return str(value)[:300]
        data = payload.get("data")
        if isinstance(data, dict):
            for key in ("message", "detail", "error", "code"):
                value = data.get(key)
                if value:
                    return str(value)[:300]
    return ""


def _is_2chat_url(base_url: str) -> bool:
    return "2chat.cc" in base_url.lower()


async def _fetch_sub2api_group_payloads(
    client: httpx.AsyncClient,
    base_url: str,
    token: str,
    log: LogCallback | None,
    account: Any,
) -> tuple[Any, Any]:
    auth_headers = {"Authorization": f"Bearer {token}"}
    groups_response = await _logged_get(client, f"{base_url}/api/v1/groups/available", auth_headers, log, account)
    groups_response.raise_for_status()
    groups_payload = groups_response.json()

    rates_response = await _logged_get(client, f"{base_url}/api/v1/groups/rates", auth_headers, log, account)
    rates_response.raise_for_status()
    return groups_payload, rates_response.json()


async def _fetch_sub2api_keys_payload(
    client: httpx.AsyncClient,
    base_url: str,
    token: str,
    log: LogCallback | None,
    account: Any,
) -> Any:
    keys_response = await _logged_get(client, f"{base_url}/api/v1/keys?page=1&page_size=100", {"Authorization": f"Bearer {token}"}, log, account)
    keys_response.raise_for_status()
    return keys_response.json()


async def _try_match_group_from_api_key(
    client: httpx.AsyncClient,
    base_url: str,
    token: str,
    log: LogCallback | None,
    account: Any,
    api_key: str,
    key_id: Optional[str],
    active_plan_name: Optional[str],
    groups: list[dict[str, Any]],
    rates: dict[str, float],
    fallback_summary: dict[str, Any],
) -> dict[str, Any]:
    try:
        keys_payload = await _fetch_sub2api_keys_payload(client, base_url, token, log, account)
    except (httpx.HTTPError, KeyError, IndexError, ValueError):
        return fallback_summary
    active_key_group_id = _extract_api_key_group_id(keys_payload, api_key)
    if not active_key_group_id:
        return fallback_summary
    return _build_current_group_rate_summary(key_id, active_key_group_id, active_plan_name, groups, rates)


def _sub2api_token_cache_key(base_url: str, email: str) -> str:
    return f"{base_url}|{email.strip().lower()}"


def _get_cached_sub2api_token(cache_key: str) -> Optional[str]:
    cached = _get_cached_sub2api_token_state(cache_key)
    if not cached:
        return None
    token = cached.get("access_token")
    return str(token) if token else None


def _get_cached_sub2api_token_state(cache_key: str) -> Optional[dict[str, Any]]:
    cached = _SUB2API_TOKEN_CACHE.get(cache_key)
    if not cached:
        return None
    if _now_timestamp() >= float(cached.get("expires_at", 0)):
        _SUB2API_TOKEN_CACHE.pop(cache_key, None)
        return None
    return dict(cached)


def _cache_sub2api_token(cache_key: str, token: str, payload: Any, refresh_token: str | None = None) -> None:
    expires_at = _extract_token_expires_at(payload, token)
    _SUB2API_TOKEN_CACHE[cache_key] = {
        "access_token": token,
        "refresh_token": refresh_token,
        "expires_at": expires_at,
    }


def _token_needs_refresh(cached: dict[str, Any]) -> bool:
    expires_at = _optional_number(cached.get("expires_at"))
    if expires_at is None:
        return False
    return _now_timestamp() >= max(0.0, expires_at - SUB2API_TOKEN_REFRESH_SKEW_SECONDS)


def _extract_token_expires_at(payload: Any, token: str) -> float:
    expires_at = _extract_expiry_timestamp(payload)
    if expires_at is None:
        expires_at = _extract_jwt_expiry(token)
    if expires_at is None:
        expires_at = _now_timestamp() + SUB2API_TOKEN_DEFAULT_TTL_SECONDS
    return max(_now_timestamp(), expires_at - SUB2API_TOKEN_REFRESH_SKEW_SECONDS)


def _extract_expiry_timestamp(payload: Any) -> Optional[float]:
    data = _unwrap_response_data(payload)
    candidates = []
    if isinstance(data, dict):
        candidates.extend(
            [
                data.get("expires_at"),
                data.get("expiresAt"),
                data.get("expire_at"),
                data.get("expireAt"),
                data.get("exp"),
            ]
        )
        ttl = data.get("expires_in") or data.get("expiresIn")
        ttl_number = _optional_number(ttl)
        if ttl_number is not None:
            return _now_timestamp() + ttl_number
    if isinstance(payload, dict):
        candidates.extend(
            [
                payload.get("expires_at"),
                payload.get("expiresAt"),
                payload.get("expire_at"),
                payload.get("expireAt"),
                payload.get("exp"),
            ]
        )
        ttl = payload.get("expires_in") or payload.get("expiresIn")
        ttl_number = _optional_number(ttl)
        if ttl_number is not None:
            return _now_timestamp() + ttl_number
    for value in candidates:
        timestamp = _parse_expiry_value(value)
        if timestamp is not None:
            return timestamp
    return None


def _parse_expiry_value(value: Any) -> Optional[float]:
    number = _optional_number(value)
    if number is not None:
        return number / 1000 if number > 9999999999 else number
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _extract_jwt_expiry(token: str) -> Optional[float]:
    parts = token.split(".")
    if len(parts) < 2:
        return None
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return _parse_expiry_value(data.get("exp")) if isinstance(data, dict) else None


def _now_timestamp() -> float:
    return datetime.now(timezone.utc).timestamp()


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


def _extract_refresh_token(payload: Any) -> Optional[str]:
    data = _unwrap_response_data(payload)
    if isinstance(data, dict):
        token = data.get("refresh_token") or data.get("refreshToken")
        if token:
            return str(token)
    if isinstance(payload, dict):
        token = payload.get("refresh_token") or payload.get("refreshToken")
        if token:
            return str(token)
    return None


def _extract_usage_plan_name(payload: Any) -> Optional[str]:
    data = _unwrap_response_data(payload)
    if isinstance(data, dict):
        plan_name = data.get("planName") or data.get("plan_name") or data.get("group")
        if plan_name:
            return str(plan_name)
    if isinstance(payload, dict):
        plan_name = payload.get("planName") or payload.get("plan_name") or payload.get("group")
        if plan_name:
            return str(plan_name)
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


def _extract_newapi_user_groups(payload: Any) -> list[dict[str, Any]]:
    data = _unwrap_response_data(payload)
    if isinstance(data, dict):
        items = data.get("user_group") or data.get("userGroup") or data.get("groups") or data.get("items") or data.get("list")
        if not isinstance(items, list):
            items = [
                {"id": key, "name": key, **value}
                for key, value in data.items()
                if isinstance(value, dict)
            ]
    else:
        items = data
    if not isinstance(items, list):
        return []
    groups = []
    for item in items:
        if not isinstance(item, dict):
            continue
        group_id = item.get("id")
        if group_id is None:
            group_id = item.get("name") or item.get("group") or item.get("group_name") or item.get("groupName")
        if group_id is None or group_id == "":
            continue
        display_name = (
            item.get("desc")
            or item.get("description")
            or item.get("name")
            or item.get("group_name")
            or item.get("groupName")
            or item.get("group")
            or str(group_id)
        )
        default_rate = _optional_number(
            item.get("default_rate_multiplier")
            if item.get("default_rate_multiplier") is not None
            else item.get("rate")
            if item.get("rate") is not None
            else item.get("ratio")
            if item.get("ratio") is not None
            else item.get("rate_multiplier")
            if item.get("rate_multiplier") is not None
            else item.get("rateMultiplier")
        )
        user_rate = _optional_number(item.get("user_rate_multiplier"))
        effective_rate = _optional_number(item.get("effective_rate_multiplier"))
        if effective_rate is None:
            effective_rate = user_rate if user_rate is not None else default_rate
        if user_rate is None:
            user_rate = effective_rate
        groups.append(
            {
                "id": str(group_id),
                "name": display_name,
                "plan_name": display_name,
                "platform": "newApi",
                "status": item.get("status"),
                "default_rate_multiplier": default_rate,
                "user_rate_multiplier": user_rate,
                "effective_rate_multiplier": effective_rate,
            }
        )
    return groups


def _extract_api_key_group_id(payload: Any, api_key: str) -> Optional[str]:
    keys = _extract_api_keys(payload)
    if len(keys) == 1:
        return _value_as_string(_extract_group_id_from_key(keys[0]))
    for item in keys:
        candidate = item.get("key") or item.get("api_key") or item.get("apiKey") or item.get("token")
        if candidate is not None and _api_key_matches(str(candidate), api_key):
            return _value_as_string(_extract_group_id_from_key(item))
    return None


def _extract_api_keys(payload: Any) -> list[dict[str, Any]]:
    data = _unwrap_response_data(payload)
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("items", "keys", "list", "records", "data"):
            items = data.get(key)
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
    return []


def _extract_group_id_from_key(item: dict[str, Any]) -> Any:
    group = item.get("group")
    if isinstance(group, dict):
        return group.get("id") or group.get("group_id") or group.get("groupId")
    return item.get("group_id") or item.get("groupId") or item.get("groupID")


def _api_key_matches(candidate: str, api_key: str) -> bool:
    if candidate == api_key:
        return True
    if "*" in candidate or candidate.endswith("..."):
        visible = candidate.replace("*", "").replace("...", "")
        return bool(visible) and (api_key.startswith(visible) or api_key.endswith(visible))
    return False


def _value_as_string(value: Any) -> Optional[str]:
    if value is None or value == "":
        return None
    return str(value)


def _build_current_group_rate_summary(
    key_id: Optional[str],
    active_key_group_id: Optional[str],
    active_plan_name: Optional[str],
    groups: list[dict[str, Any]],
    rates: dict[str, float],
) -> dict[str, Any]:
    group_summaries = [_summarize_group(group, rates) for group in groups]
    match_group_id = active_key_group_id or key_id
    target_group = None
    if match_group_id:
        target_group = next((item for item in group_summaries if str(item.get("id")) == str(match_group_id)), None)
    if target_group is None:
        target_group = _find_active_group(active_plan_name, group_summaries)
    if target_group is None and len(group_summaries) == 1:
        target_group = group_summaries[0]
    if target_group:
        title_name = target_group.get("plan_name") or target_group.get("name") or target_group.get("id") or active_plan_name or key_id
        title = f"{title_name} 倍率 {target_group['effective_rate_multiplier']}"
        return {
            "title": title,
            "group_id": target_group.get("id") or match_group_id,
            "group": target_group,
            "groups": [target_group],
            "active_plan_name": active_plan_name,
            "active_key_group_id": active_key_group_id,
        }
    if match_group_id:
        rate = rates.get(str(match_group_id))
        if rate is not None:
            target_group = {
                "id": match_group_id,
                "name": None,
                "plan_name": active_plan_name or f"分组 {match_group_id}",
                "platform": None,
                "status": None,
                "default_rate_multiplier": None,
                "user_rate_multiplier": rate,
                "effective_rate_multiplier": rate,
            }
            return {
                "title": f"{target_group['plan_name']} 倍率 {rate}",
                "group_id": match_group_id,
                "group": target_group,
                "groups": [target_group],
                "active_plan_name": active_plan_name,
                "active_key_group_id": active_key_group_id,
            }
    return {
        "title": "未识别当前 apiKey 分组",
        "group_id": key_id,
        "groups": [],
        "available_groups": group_summaries,
        "active_plan_name": active_plan_name,
        "active_key_group_id": active_key_group_id,
    }


def _is_unrecognized_group_summary(summary: dict[str, Any]) -> bool:
    return summary.get("title") == "未识别当前 apiKey 分组"


def _find_active_group(active_plan_name: Optional[str], groups: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not active_plan_name:
        return None
    normalized = active_plan_name.strip().lower()
    for group in groups:
        candidates = (group.get("plan_name"), group.get("name"), group.get("id"))
        if any(str(candidate).strip().lower() == normalized for candidate in candidates if candidate is not None):
            return group
    return None


def _summarize_group(group: dict[str, Any], rates: dict[str, float]) -> dict[str, Any]:
    group_id = group.get("id")
    group_key = str(group_id)
    default_rate = _optional_number(group.get("default_rate_multiplier"))
    if default_rate is None:
        default_rate = _optional_number(group.get("rate_multiplier"))
    user_rate = rates.get(group_key)
    if user_rate is None:
        user_rate = _optional_number(group.get("user_rate_multiplier"))
    effective_rate = _optional_number(group.get("effective_rate_multiplier"))
    if effective_rate is None:
        effective_rate = user_rate if user_rate is not None else default_rate
    return {
        "id": group_id,
        "name": group.get("name"),
        "plan_name": group.get("plan_name") or group.get("planName") or group.get("name"),
        "platform": group.get("platform"),
        "status": group.get("status"),
        "default_rate_multiplier": default_rate,
        "user_rate_multiplier": user_rate,
        "effective_rate_multiplier": effective_rate,
    }

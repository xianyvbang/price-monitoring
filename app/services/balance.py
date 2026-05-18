from __future__ import annotations

from typing import Any, Optional

import httpx

from app.security import decrypt_value


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


async def query_account(account: Any, secret_key: str, timeout: float) -> dict[str, Any]:
    try:
        if account["platform"] == "sub2Api":
            return await query_sub2api(account, secret_key, timeout)
        if account["platform"] == "newApi":
            return await query_newapi(account, secret_key, timeout)
        return normalize_result({"is_valid": False, "invalid_message": "不支持的平台"})
    except httpx.TimeoutException:
        return normalize_result({"is_valid": False, "invalid_message": "请求超时"})
    except httpx.HTTPError as exc:
        return normalize_result({"is_valid": False, "invalid_message": f"请求失败: {exc}"})
    except Exception as exc:
        return normalize_result({"is_valid": False, "invalid_message": f"查询异常: {exc}"})


async def query_sub2api(account: Any, secret_key: str, timeout: float) -> dict[str, Any]:
    api_key = decrypt_value(account["api_key_enc"], secret_key)
    if not api_key:
        return normalize_result({"is_valid": False, "invalid_message": "缺少 apiKey"})
    url = f"{account['base_url'].rstrip('/')}/v1/usage"
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(url, headers={"Authorization": f"Bearer {api_key}"})
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


async def query_newapi(account: Any, secret_key: str, timeout: float) -> dict[str, Any]:
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
        response = await client.get(url, headers=headers)
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

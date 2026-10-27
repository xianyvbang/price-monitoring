from __future__ import annotations

import asyncio
import json
import math
import re
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeTimedSerializer

from app.config import get_config, uses_default_app_secret
from app.models import (
    BALANCE_QUERY_INTERVAL_SECONDS,
    CHINA_TZ,
    DEFAULT_BALANCE_UNIT,
    GROUP_RATE_QUERY_INTERVAL_SECONDS,
    Database,
    REQUEST_TIMEOUT_SECONDS,
    adjusted_consumption_stats,
    adjusted_used_balance,
    actual_consumption_amount,
    actual_consumption_stats,
    format_china_time,
    filter_platform_dispatch_accounts_by_available_groups,
    filter_platform_dispatch_accounts_by_group_account_exclusions,
    filter_platform_dispatch_accounts_by_groups,
    monitor_group_to_dict,
    row_to_dict,
    manual_today_consumption_adjustment,
    utc_now,
)
from app.security import decrypt_value, encrypt_value
from app.security import verify_password
from app.services.balance import login_sub2api_tokens, query_newapi_group_options, query_sub2api_group_options
from app.services.emailer import send_email
from app.services.extension_pack import build_account_grabber_extension_zip
from app.services.scheduler import (
    BalanceScheduler,
    query_all_accounts,
    query_group_rate_for_account,
    query_one_account,
    query_sub2api_group_for_account,
)
from app.services.sub2api_admin import (
    Sub2ApiAdminClient,
    Sub2ApiAdminError,
    public_dispatch_account,
    public_dispatch_group,
)
from app.services.platform_dispatch_policy import (
    DEFAULT_ACCOUNT_PRIORITY,
    POLICY_DEFAULTS,
    PlatformDispatchPolicyScheduler,
    public_platform_dispatch_cost_profile,
    resolve_platform_dispatch_cost_profiles,
    validate_policy_config,
)

config = get_config()
db = Database(config.database_path, config.app_secret_key)
templates = Jinja2Templates(directory="app/templates")
serializer = URLSafeTimedSerializer(config.app_secret_key, salt="balance-monitor-session")
scheduler = BalanceScheduler(db)
platform_dispatch_policy_scheduler = PlatformDispatchPolicyScheduler(db, lambda: sub2api_admin_client())
platform_dispatch_tasks: set[asyncio.Task[Any]] = set()
FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"
FRONTEND_INDEX = FRONTEND_DIR / "index.html"
FRONTEND_ASSETS_DIR = FRONTEND_DIR / "assets"
FRONTEND_SOURCE_DIR = Path(__file__).resolve().parent.parent / "frontend"
FRONTEND_PUBLIC_DIR = FRONTEND_SOURCE_DIR / "public"
FRONTEND_FAVICON = FRONTEND_DIR / "favicon.svg"
SOURCE_FAVICON = FRONTEND_PUBLIC_DIR / "favicon.svg"
SENSITIVE_HEADER_NAMES = {"authorization", "cookie", "set-cookie"}
SENSITIVE_FIELD_NAMES = {
    "api_key",
    "apiKey",
    "key_id",
    "keyId",
    "access_token",
    "accessToken",
    "refresh_token",
    "refreshToken",
    "password",
    "email",
    "token",
    "secret",
    "app_secret_key",
}
LOG_VALUE_LIMIT = 2000
LOG_PAGE_SIZE = 50
LOG_MAX_PAGE_SIZE = 200
CONSUMPTION_PERIODS = [
    {"key": "today", "label": "今日实际消耗总金额", "count_label": "个 Base URL 有今日记录"},
    {"key": "yesterday", "label": "昨日实际消耗总金额", "count_label": "个 Base URL 有昨日记录"},
    {"key": "last_24h", "label": "近24小时实际消耗总金额", "count_label": "个 Base URL 有近24小时记录"},
    {"key": "last_7d", "label": "近7天实际消耗总金额", "count_label": "个 Base URL 有近7天记录"},
    {"key": "last_14d", "label": "近14天实际消耗总金额", "count_label": "个 Base URL 有近14天记录"},
    {"key": "this_month", "label": "本月实际消耗总金额", "count_label": "个 Base URL 有本月记录"},
    {"key": "last_month", "label": "上月实际消耗总金额", "count_label": "个 Base URL 有上月记录"},
]
TODAY_REMAINING_SUMMARY = {
    "key": "today_remaining",
    "label": "今日剩余金额",
    "count_label": "个 Base URL 有今日余额",
}
SUB2API_ADMIN_KEY_SETTING = "sub2api_admin_key_enc"
SUB2API_SITE_URL_SETTING = "sub2api_site_url"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    db.ensure_admin(config.admin_username, config.admin_password)
    db.interrupt_platform_dispatch_job()
    if uses_default_app_secret(config.app_secret_key):
        db.add_log("warning", "security", "APP_SECRET_KEY 仍为默认值，请尽快在 .env 中更换为长随机密钥")
    scheduler.start()
    platform_dispatch_policy_scheduler.db = db
    platform_dispatch_policy_scheduler.start()
    yield
    tasks = list(platform_dispatch_tasks)
    for task in tasks:
        task.cancel()
    for task in tasks:
        with suppress(asyncio.CancelledError):
            await task
    await platform_dispatch_policy_scheduler.stop()
    await scheduler.stop()


app = FastAPI(title="余额监控", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.mount("/assets", StaticFiles(directory=FRONTEND_ASSETS_DIR, check_dir=False), name="frontend-assets")


@app.middleware("http")
async def log_http_requests(request: Request, call_next):
    body = await request.body()
    if should_log_http_request(request):
        db.add_log(
            "info",
            "http",
            f"IN {request.method} {request.url.path} request={_safe_request_payload(request, body)}",
        )

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request(request.scope, receive)
    try:
        response = await call_next(request)
    except Exception as exc:
        db.add_log("error", "http", f"IN {request.method} {request.url.path} error={exc}")
        raise
    return await _log_response(request, response)


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/favicon.svg", include_in_schema=False)
async def favicon_svg():
    if FRONTEND_FAVICON.exists():
        return FileResponse(FRONTEND_FAVICON, media_type="image/svg+xml")
    if SOURCE_FAVICON.exists():
        return FileResponse(SOURCE_FAVICON, media_type="image/svg+xml")
    raise HTTPException(status_code=404, detail="favicon not found")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon_ico():
    return RedirectResponse("/favicon.svg", status_code=307)


@app.get("/api/session")
async def api_session(request: Request):
    user = current_user(request)
    return {
        "authenticated": bool(user),
        "user": {"username": user} if user else None,
        "top_menu_visibility": db.get_general_settings()["top_menu_visibility"],
    }


def current_user(request: Request) -> str | None:
    token = request.cookies.get(config.session_cookie)
    if not token:
        return None
    try:
        payload = serializer.loads(token, max_age=config.session_max_age_seconds)
    except BadSignature:
        return None
    if not isinstance(payload, dict):
        return None
    username = str(payload.get("username") or "")
    if not username:
        return None
    try:
        session_version = int(payload.get("session_version"))
    except (TypeError, ValueError):
        return None
    user = db.get_user(username)
    if not user:
        return None
    try:
        current_session_version = int(user["session_version"])
    except (IndexError, KeyError, TypeError, ValueError):
        return None
    if session_version != current_session_version:
        return None
    return username


def require_user(request: Request) -> str:
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    return user


def redirect_if_needed(request: Request) -> RedirectResponse | None:
    if not current_user(request):
        return RedirectResponse("/login", status_code=303)
    return None


def template_context(request: Request, **extra: Any) -> dict[str, Any]:
    return {"request": request, "user": current_user(request), "format_time": format_china_time, **extra}


def spa_response() -> FileResponse | HTMLResponse:
    if FRONTEND_INDEX.exists():
        return FileResponse(FRONTEND_INDEX)
    return HTMLResponse(
        """
        <!doctype html>
        <html lang="zh-CN">
          <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <meta name="theme-color" content="#0b67ef">
            <link rel="icon" type="image/svg+xml" href="/favicon.svg">
            <title>余额监控</title>
          </head>
          <body>
            <main style="font-family: sans-serif; max-width: 720px; margin: 80px auto; line-height: 1.7;">
              <h1>前端尚未构建</h1>
              <p>请先在 <code>frontend</code> 目录执行 <code>npm install</code> 和 <code>npm run build</code>，或在开发模式运行 Vite。</p>
            </main>
          </body>
        </html>
        """,
        status_code=503,
    )


def account_filter_from_query(request: Request) -> dict[str, Any]:
    name = str(request.query_params.get("name") or "").strip()
    platform = str(request.query_params.get("platform") or "").strip()
    if platform not in {"newApi", "sub2Api"}:
        platform = ""
    low_balance_value = str(request.query_params.get("low_balance") or request.query_params.get("lowBalance") or "").strip().lower()
    if low_balance_value in {"1", "true", "yes", "low", "below", "below_threshold"}:
        low_balance = "low"
    elif low_balance_value in {"0", "false", "no", "normal", "not_low", "above", "above_threshold"}:
        low_balance = "normal"
    else:
        low_balance = ""
    return {
        "name": name,
        "platform": platform,
        "low_balance": low_balance,
        "lowBalance": low_balance,
        "active": bool(name or platform or low_balance),
    }


def _effective_threshold(account: dict[str, Any], default_threshold: float | None = None) -> float | None:
    threshold = _optional_number(account.get("threshold"))
    if threshold is not None:
        return threshold
    return default_threshold


def is_low_balance_account(account: dict[str, Any], default_threshold: float | None = None) -> bool:
    if account.get("is_eliminated"):
        return False
    remaining = _optional_number(account.get("last_remaining"))
    threshold = _effective_threshold(account, default_threshold)
    return remaining is not None and threshold is not None and remaining < threshold


def public_log(row: Any) -> dict[str, Any]:
    data = row_to_dict(row)
    data["created_at_formatted"] = format_china_time(data.get("created_at"))
    return data


def log_page_payload(page: int, page_size: int, category: str | None = None, message_query: str | None = None) -> dict[str, Any]:
    page = max(1, page)
    page_size = min(max(1, page_size), LOG_MAX_PAGE_SIZE)
    total = db.count_logs(category=category, message_query=message_query)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, total_pages)
    logs = [
        public_log(row)
        for row in db.list_logs(
            limit=page_size,
            offset=(page - 1) * page_size,
            category=category,
            message_query=message_query,
        )
    ]
    return {
        "logs": logs,
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
            "has_previous": page > 1,
            "has_next": page < total_pages,
            "previous_page": max(1, page - 1),
            "next_page": min(total_pages, page + 1),
        },
    }


def _positive_query_int(value: Any, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        return max(1, int(float(value)))
    except (TypeError, ValueError):
        return default


def should_log_http_request(request: Request) -> bool:
    path = request.url.path
    if path == "/assets" or path.startswith("/assets/"):
        return False
    if path == "/favicon.ico" or path == "/favicon.svg":
        return False
    if path == "/static" or path.startswith("/static/"):
        return False
    if path == "/logs" or path.startswith("/logs/"):
        return False
    if path == "/api/logs" or path.startswith("/api/logs/"):
        return False
    return True


def _log_text(value: Any) -> str:
    text = json.dumps(_mask_sensitive(value), ensure_ascii=False, default=str)
    if len(text) > LOG_VALUE_LIMIT:
        return text[:LOG_VALUE_LIMIT] + "...<truncated>"
    return text


def _mask_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "***"
            if _is_sensitive_key(str(key))
            else _mask_sensitive(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_mask_sensitive(item) for item in value]
    return value


def _is_sensitive_key(key: str) -> bool:
    normalized = key.replace("-", "_").lower()
    return (
        key in SENSITIVE_FIELD_NAMES
        or normalized in SENSITIVE_HEADER_NAMES
        or normalized == "email"
        or "password" in normalized
        or "token" in normalized
        or "secret" in normalized
        or normalized in {"api_key", "apikey", "key_id", "keyid"}
    )


def _safe_headers(headers: Any) -> dict[str, str]:
    return {
        key: "***" if _is_sensitive_key(key) else value
        for key, value in headers.items()
    }


def _parse_logged_body(content_type: str, body: bytes) -> Any:
    if not body:
        return None
    content_type = content_type.lower()
    if "application/json" in content_type:
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return body.decode("utf-8", errors="replace")
    if "application/x-www-form-urlencoded" in content_type:
        parsed = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
        return {key: values[0] if len(values) == 1 else values for key, values in parsed.items()}
    if content_type.startswith("text/"):
        return body.decode("utf-8", errors="replace")
    return f"<{len(body)} bytes>"


def _safe_request_payload(request: Request, body: bytes) -> str:
    payload = {
        "query": dict(request.query_params),
        "headers": _safe_headers(request.headers),
        "body": _parse_logged_body(request.headers.get("content-type", ""), body),
    }
    return _log_text(payload)


def _safe_response_payload(response: Any, body: bytes) -> str:
    payload = {
        "status": response.status_code,
        "headers": _safe_headers(response.headers),
        "body": _parse_logged_body(response.headers.get("content-type", ""), body),
    }
    return _log_text(payload)


_CAMEL_CASE_BOUNDARY_1 = re.compile(r"(.)([A-Z][a-z]+)")
_CAMEL_CASE_BOUNDARY_2 = re.compile(r"([a-z0-9])([A-Z])")


def _snake_case_key(key: str) -> str:
    return _CAMEL_CASE_BOUNDARY_2.sub(
        r"\1_\2", _CAMEL_CASE_BOUNDARY_1.sub(r"\1_\2", key)
    ).lower()


def _without_redundant_case_aliases(value: Any) -> Any:
    if isinstance(value, list):
        return [_without_redundant_case_aliases(item) for item in value]
    if not isinstance(value, dict):
        return value

    result: dict[Any, Any] = {}
    for key, item in value.items():
        if isinstance(key, str):
            snake_key = _snake_case_key(key)
            if snake_key != key and snake_key in value:
                continue
        result[key] = _without_redundant_case_aliases(item)
    return result


def _deduplicate_api_json_body(response: Any, body: bytes) -> bytes:
    if not body or "json" not in response.headers.get("content-type", "").lower():
        return body
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return body
    normalized = _without_redundant_case_aliases(payload)
    if normalized == payload:
        return body
    return json.dumps(
        normalized, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")


async def _log_response(request: Request, response: Any):
    body = b""
    async for chunk in response.body_iterator:
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8")
        body += chunk
    if request.url.path.startswith("/api/"):
        body = _deduplicate_api_json_body(response, body)
    if should_log_http_request(request):
        db.add_log(
            "info",
            "http",
            f"IN {request.method} {request.url.path} response={_safe_response_payload(response, body)}",
        )
    response.body_iterator = _body_iterator(body)
    response.headers["content-length"] = str(len(body))
    if request.url.path.startswith("/api/"):
        response.headers["cache-control"] = "no-store"
    return response


async def _body_iterator(body: bytes):
    yield body


def public_account(row: Any) -> dict[str, Any]:
    data = row_to_dict(row)
    data["is_enabled"] = bool(data.get("is_enabled", True))
    data["is_visible"] = bool(data.get("is_visible", True))
    data["is_eliminated"] = bool(data.get("is_eliminated"))
    data["last_unit"] = str(data.get("last_unit") or DEFAULT_BALANCE_UNIT).strip() or DEFAULT_BALANCE_UNIT
    key_id_enc = data.pop("key_id_enc", None)
    data["has_key_id"] = bool(key_id_enc)
    monitor_groups = [public_monitor_group(group) for group in db.list_monitor_groups(int(data["id"]))]
    selected_group_ids = [group["group_id"] for group in monitor_groups if group.get("group_id")]
    selected_group_id = selected_group_ids[0] if selected_group_ids else (
        decrypt_value(key_id_enc, config.app_secret_key) if data["platform"] in {"newApi", "sub2Api"} and key_id_enc else None
    )
    data["selected_group_id"] = selected_group_id
    data["selectedGroupId"] = selected_group_id
    data["selected_group_ids"] = selected_group_ids
    data["selectedGroupIds"] = selected_group_ids
    data["monitor_groups"] = monitor_groups
    data["monitorGroups"] = monitor_groups
    data["has_api_key"] = bool(data.pop("api_key_enc", None))
    data["has_email"] = bool(data.pop("email_enc", None))
    data["has_password"] = bool(data.pop("password_enc", None))
    data["has_login_extra_params"] = bool(data.pop("login_extra_params_enc", None))
    data["has_access_token"] = bool(data.pop("access_token_enc", None))
    data["has_refresh_token"] = bool(data.pop("refresh_token_enc", None))
    data["has_user_id"] = bool(data.pop("user_id_enc", None))
    data["group_rates"] = monitor_group_rates(monitor_groups) or group_rates_from_extra(data.get("last_extra"))
    data["recharge_paid_amount"] = float(data.get("recharge_paid_amount") or 1)
    data["recharge_received_amount"] = float(data.get("recharge_received_amount") or 1)
    data["rechargePaidAmount"] = data["recharge_paid_amount"]
    data["rechargeReceivedAmount"] = data["recharge_received_amount"]
    data["last_used"] = adjusted_used_balance(data.get("last_used"), data)
    data["lastUsed"] = data["last_used"]
    data["consumption_stats"] = db.get_consumption_stats(int(data["id"]))
    data["consumption_stats"] = adjusted_consumption_stats(data["consumption_stats"], data)
    data["consumptionStats"] = data["consumption_stats"]
    data["actual_consumption_stats"] = actual_consumption_stats(data["consumption_stats"], data)
    data["actualConsumptionStats"] = data["actual_consumption_stats"]
    data["today_consumption"] = data["consumption_stats"]["today"]
    data["todayConsumption"] = data["today_consumption"]
    data["actual_today_consumption"] = data["actual_consumption_stats"]["today"]
    data["actualTodayConsumption"] = data["actual_today_consumption"]
    data["last_group_rate_changed"] = bool(
        any(group.get("last_group_rate_changed") for group in monitor_groups)
        or data.get("last_group_rate_changed")
    )
    if data["platform"] in {"newApi", "sub2Api"} and selected_group_id and not data["group_rates"]:
        data["group_rates"] = [{"plan_name": f"当前分组 {selected_group_id}", "rate_multiplier": None}]
    return data


def public_account_summary(row: Any) -> dict[str, Any]:
    data = row_to_dict(row)
    key_id_enc = data.get("key_id_enc")
    monitor_groups = db.list_monitor_groups(int(data["id"]))
    selected_group_ids = [
        monitor_group_to_dict(group, db.secret_key)["group_id"]
        for group in monitor_groups
        if group["group_id_enc"]
    ]
    selected_group_id = selected_group_ids[0] if selected_group_ids else (
        decrypt_value(key_id_enc, config.app_secret_key)
        if data["platform"] in {"newApi", "sub2Api"} and key_id_enc
        else None
    )
    return {
        "id": data["id"],
        "platform": data["platform"],
        "name": data["name"],
        "base_url": data["base_url"],
        "note": data.get("note"),
        "recharge_url": data.get("recharge_url"),
        "recharge_paid_amount": float(data.get("recharge_paid_amount") or 1),
        "recharge_received_amount": float(data.get("recharge_received_amount") or 1),
        "threshold": data.get("threshold"),
        "is_enabled": bool(data.get("is_enabled", True)),
        "is_visible": bool(data.get("is_visible", True)),
        "selected_group_id": selected_group_id,
        "selected_group_ids": selected_group_ids,
        "has_api_key": bool(data.get("api_key_enc")),
        "has_email": bool(data.get("email_enc")),
        "has_password": bool(data.get("password_enc")),
        "has_access_token": bool(data.get("access_token_enc")),
        "has_refresh_token": bool(data.get("refresh_token_enc")),
        "has_user_id": bool(data.get("user_id_enc")),
    }


def public_monitor_group(row: Any) -> dict[str, Any]:
    data = monitor_group_to_dict(row, config.app_secret_key)
    data["display_name"] = data.get("plan_name") or data.get("name") or data.get("group_id") or "-"
    data["displayName"] = data["display_name"]
    return data


def monitor_group_rates(monitor_groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rates = []
    for group in monitor_groups:
        rates.append(
            {
                "monitor_group_id": group.get("id"),
                "group_id": group.get("group_id"),
                "plan_name": group.get("plan_name") or group.get("name") or group.get("group_id") or "-",
                "rate_multiplier": group.get("effective_rate_multiplier"),
            }
        )
    return rates


def public_dashboard_monitor_group(group: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": group.get("id"),
        "group_id": group.get("group_id"),
        "groupId": group.get("group_id"),
        "plan_name": group.get("plan_name"),
        "planName": group.get("plan_name"),
        "name": group.get("name"),
        "display_name": group.get("display_name"),
        "displayName": group.get("display_name"),
        "rate_multiplier": group.get("rate_multiplier"),
        "rateMultiplier": group.get("rate_multiplier"),
        "effective_rate_multiplier": group.get("effective_rate_multiplier"),
        "effectiveRateMultiplier": group.get("effective_rate_multiplier"),
        "last_group_rate_changed": bool(group.get("last_group_rate_changed")),
        "lastGroupRateChanged": bool(group.get("last_group_rate_changed")),
        "last_group_query_status": group.get("last_group_query_status") or "never",
        "lastGroupQueryStatus": group.get("last_group_query_status") or "never",
    }


def public_dashboard_account(
    account: dict[str, Any],
    monitor_group: dict[str, Any] | None,
    default_threshold: float | None = None,
) -> dict[str, Any]:
    consumption_stats = account.get("consumption_stats") if isinstance(account.get("consumption_stats"), dict) else {}
    actual_stats = (
        account.get("actual_consumption_stats")
        if isinstance(account.get("actual_consumption_stats"), dict)
        else actual_consumption_stats(consumption_stats, account)
    )
    row = {
        "id": account.get("id"),
        "platform": account.get("platform"),
        "name": account.get("name"),
        "base_url": account.get("base_url"),
        "baseUrl": account.get("base_url"),
        "note": account.get("note"),
        "recharge_url": account.get("recharge_url"),
        "rechargeUrl": account.get("recharge_url"),
        "recharge_paid_amount": float(account.get("recharge_paid_amount") or 1),
        "rechargePaidAmount": float(account.get("recharge_paid_amount") or 1),
        "recharge_received_amount": float(account.get("recharge_received_amount") or 1),
        "rechargeReceivedAmount": float(account.get("recharge_received_amount") or 1),
        "threshold": account.get("threshold"),
        "is_enabled": bool(account.get("is_enabled")),
        "isEnabled": bool(account.get("is_enabled")),
        "is_eliminated": bool(account.get("is_eliminated")),
        "isEliminated": bool(account.get("is_eliminated")),
        "last_status": account.get("last_status"),
        "lastStatus": account.get("last_status"),
        "last_remaining": account.get("last_remaining"),
        "lastRemaining": account.get("last_remaining"),
        "last_unit": account.get("last_unit"),
        "lastUnit": account.get("last_unit"),
        "last_total": account.get("last_total"),
        "lastTotal": account.get("last_total"),
        "last_used": account.get("last_used"),
        "lastUsed": account.get("last_used"),
        "last_checked_at": account.get("last_checked_at"),
        "lastCheckedAt": account.get("last_checked_at"),
        "last_group_query_status": account.get("last_group_query_status") or "never",
        "lastGroupQueryStatus": account.get("last_group_query_status") or "never",
        "today_consumption": consumption_stats.get("today"),
        "todayConsumption": consumption_stats.get("today"),
        "actual_today_consumption": actual_stats.get("today"),
        "actualTodayConsumption": actual_stats.get("today"),
    }
    public_group = public_dashboard_monitor_group(monitor_group) if monitor_group else None
    row["monitor_group"] = public_group
    row["monitorGroup"] = public_group
    if monitor_group:
        row["last_group_query_status"] = monitor_group.get("last_group_query_status") or "never"
        row["lastGroupQueryStatus"] = row["last_group_query_status"]
        row["dashboard_row_id"] = f"{account['id']}:group:{monitor_group['id']}"
        row["dashboardRowId"] = row["dashboard_row_id"]
        row["current_group_id"] = monitor_group.get("group_id")
        row["currentGroupId"] = row["current_group_id"]
        row["current_monitor_group_id"] = monitor_group.get("id")
        row["currentMonitorGroupId"] = row["current_monitor_group_id"]
        row["last_group_rate_changed"] = bool(monitor_group.get("last_group_rate_changed"))
        row["group_rates"] = [
            {
                "monitor_group_id": monitor_group.get("id"),
                "group_id": monitor_group.get("group_id"),
                "plan_name": monitor_group.get("plan_name") or monitor_group.get("name") or monitor_group.get("group_id") or "-",
                "rate_multiplier": monitor_group.get("effective_rate_multiplier"),
            }
        ]
    else:
        row["dashboard_row_id"] = f"{account['id']}:account"
        row["dashboardRowId"] = row["dashboard_row_id"]
        row["current_group_id"] = None
        row["currentGroupId"] = None
        row["current_monitor_group_id"] = None
        row["currentMonitorGroupId"] = None
        row["last_group_rate_changed"] = bool(account.get("last_group_rate_changed"))
        row["group_rates"] = account.get("group_rates") or []
    row["is_low_balance"] = is_low_balance_account(row, default_threshold)
    row["isLowBalance"] = row["is_low_balance"]
    return row


def group_rates_from_extra(extra: Any) -> list[dict[str, Any]]:
    if not extra:
        return []
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except json.JSONDecodeError:
            return []
    if not isinstance(extra, dict):
        return []

    groups = extra.get("groups")
    if not isinstance(groups, list) and isinstance(extra.get("group"), dict):
        groups = [extra["group"]]
    if not isinstance(groups, list):
        return []

    group_rates = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        rate = group.get("user_rate_multiplier")
        if rate is None:
            rate = group.get("effective_rate_multiplier")
        if rate is None:
            rate = group.get("default_rate_multiplier")
        group_rates.append(
            {
                "plan_name": group.get("plan_name") or group.get("planName") or group.get("name") or group.get("id") or "-",
                "rate_multiplier": rate,
            }
        )
    return group_rates


def account_group_result_from_selection(platform: str, group_id: str, group: dict[str, Any]) -> dict[str, Any]:
    selected_id = str(group.get("id") or group.get("name") or group_id)
    if selected_id != group_id:
        raise HTTPException(status_code=400, detail="分组信息与选择的分组不一致")
    plan_name = (
        group.get("plan_name")
        or group.get("planName")
        or group.get("name")
        or group.get("desc")
        or group.get("description")
        or group_id
    )
    default_rate = _optional_number(
        group.get("default_rate_multiplier")
        if group.get("default_rate_multiplier") is not None
        else group.get("rate")
        if group.get("rate") is not None
        else group.get("ratio")
        if group.get("ratio") is not None
        else group.get("rate_multiplier")
        if group.get("rate_multiplier") is not None
        else group.get("rateMultiplier")
    )
    user_rate = _optional_number(group.get("user_rate_multiplier"))
    effective_rate = _optional_number(group.get("effective_rate_multiplier"))
    if effective_rate is None:
        effective_rate = user_rate if user_rate is not None else default_rate
    selected_group = {
        "id": group_id,
        "name": group.get("name") or plan_name,
        "plan_name": plan_name,
        "platform": group.get("platform") or platform,
        "status": group.get("status"),
        "default_rate_multiplier": default_rate,
        "user_rate_multiplier": user_rate,
        "effective_rate_multiplier": effective_rate,
    }
    rate_label = effective_rate if effective_rate is not None else "-"
    title = f"{plan_name} 倍率 {rate_label}"
    extra = json.dumps(
        {
            "title": title,
            "group_id": group_id,
            "group": selected_group,
            "groups": [selected_group],
        },
        ensure_ascii=False,
        default=str,
    )
    return {"is_valid": True, "plan_name": title, "extra": extra}


def multi_group_result(results: list[dict[str, Any]]) -> dict[str, Any]:
    groups = []
    titles = []
    for result in results:
        extra = result.get("extra")
        if isinstance(extra, str):
            try:
                extra = json.loads(extra)
            except json.JSONDecodeError:
                extra = {}
        if isinstance(extra, dict):
            if isinstance(extra.get("group"), dict):
                groups.append(extra["group"])
            titles.append(str(extra.get("title") or result.get("plan_name") or ""))
    title = " / ".join(title for title in titles if title) or "已选择分组"
    return {
        "is_valid": True,
        "plan_name": title,
        "extra": json.dumps({"title": title, "groups": groups}, ensure_ascii=False, default=str),
    }


def _group_ids_from_payload(payload: dict[str, Any]) -> list[str]:
    raw = payload.get("group_ids")
    if raw is None:
        raw = payload.get("groupIds")
    if raw is None:
        raw = payload.get("monitor_group_ids")
    if raw is None:
        raw = payload.get("monitorGroupIds")
    if raw is None:
        raw = payload.get("group_id") or payload.get("groupId")
    if isinstance(raw, str):
        items = [part.strip() for value in raw.split("|") for part in value.split(";")]
    elif isinstance(raw, list):
        items = [str(item.get("id") or item.get("group_id") or item.get("groupId") or item.get("name") if isinstance(item, dict) else item).strip() for item in raw]
    else:
        items = [str(raw).strip()] if raw is not None else []
    result = []
    seen = set()
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _payload_has_group_selection(payload: dict[str, Any]) -> bool:
    return any(
        key in payload
        for key in (
            "group_ids",
            "groupIds",
            "monitor_group_ids",
            "monitorGroupIds",
            "group_id",
            "groupId",
        )
    )


def _monitor_group_ids(account_id: int) -> list[str]:
    return [
        group["group_id"]
        for group in (public_monitor_group(row) for row in db.list_monitor_groups(account_id))
        if group.get("group_id")
    ]


def _group_id_diff(current_group_ids: list[str], next_group_ids: list[str]) -> tuple[list[str], list[str]]:
    current_set = set(current_group_ids)
    next_set = set(next_group_ids)
    added = [group_id for group_id in next_group_ids if group_id not in current_set]
    removed = [group_id for group_id in current_group_ids if group_id not in next_set]
    return added, removed


def _group_id_from_group(group: Any) -> str:
    if not isinstance(group, dict):
        return ""
    return str(group.get("id") or group.get("group_id") or group.get("groupId") or group.get("name") or "").strip()


def _group_ids_from_groups(groups: Any) -> list[str]:
    if not isinstance(groups, list):
        return []
    result = []
    seen = set()
    for group in groups:
        group_id = _group_id_from_group(group)
        if group_id and group_id not in seen:
            seen.add(group_id)
            result.append(group_id)
    return result


def _groups_by_id_from_payload(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw_groups = payload.get("groups")
    if raw_groups is None and isinstance(payload.get("group"), dict):
        raw_groups = [payload["group"]]
    if not isinstance(raw_groups, list):
        raw_groups = []
    groups = {}
    for group in raw_groups:
        if not isinstance(group, dict):
            continue
        group_id = _group_id_from_group(group)
        if group_id:
            groups[group_id] = group
    return groups


def with_monitor_group_selection(account_id: int, result: dict[str, Any]) -> dict[str, Any]:
    stored_selected_group_ids = _monitor_group_ids(account_id)
    available_group_ids = _group_ids_from_groups(result.get("groups"))
    if "groups" in result:
        available_set = set(available_group_ids)
        selected_group_ids = [group_id for group_id in stored_selected_group_ids if group_id in available_set]
    else:
        selected_group_ids = stored_selected_group_ids
    result_selected_group_id = str(result.get("selected_group_id") or result.get("selectedGroupId") or "").strip()
    if not selected_group_ids and result_selected_group_id and (
        "groups" not in result or result_selected_group_id in set(available_group_ids)
    ):
        selected_group_ids = [result_selected_group_id]
    selected_group_id = selected_group_ids[0] if selected_group_ids else None
    return {
        **result,
        "selected_group_id": selected_group_id,
        "selectedGroupId": selected_group_id,
        "selected_group_ids": selected_group_ids,
        "selectedGroupIds": selected_group_ids,
        "stored_selected_group_ids": stored_selected_group_ids,
        "storedSelectedGroupIds": stored_selected_group_ids,
    }


def grouped_accounts(
    eliminated_last: bool = False,
    enabled_only: bool = False,
    visible_only: bool = False,
    platform: str | None = None,
    name_query: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    if platform in {"newApi", "sub2Api"}:
        grouped = {platform: []}
    else:
        grouped = {"newApi": [], "sub2Api": []}
        platform = None
    for row in db.list_accounts(platform=platform, name_query=name_query, enabled_only=enabled_only, visible_only=visible_only):
        grouped[row["platform"]].append(public_account(row))
    if eliminated_last:
        for accounts in grouped.values():
            accounts.sort(key=lambda account: (1 if account.get("is_eliminated") else 0, str(account.get("name") or "").lower()))
    return grouped


def grouped_account_summaries(
    platform: str | None = None,
    name_query: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    if platform in {"newApi", "sub2Api"}:
        grouped = {platform: []}
    else:
        grouped = {"newApi": [], "sub2Api": []}
        platform = None
    for row in db.list_account_summaries(platform=platform, name_query=name_query):
        grouped[row["platform"]].append(public_account_summary(row))
    return grouped


def public_dashboard_source_account(row: Any) -> dict[str, Any]:
    data = row_to_dict(row)
    data["is_enabled"] = bool(data.get("is_enabled", True))
    data["is_visible"] = bool(data.get("is_visible", True))
    data["is_eliminated"] = bool(data.get("is_eliminated"))
    data["last_unit"] = str(data.get("last_unit") or DEFAULT_BALANCE_UNIT).strip() or DEFAULT_BALANCE_UNIT
    data["recharge_paid_amount"] = float(data.get("recharge_paid_amount") or 1)
    data["recharge_received_amount"] = float(data.get("recharge_received_amount") or 1)
    today_consumption = db.get_today_consumption(int(data["id"]))
    data["last_used"] = adjusted_used_balance(data.get("last_used"), data)
    data["consumption_stats"] = {"today": today_consumption}
    data["consumption_stats"] = adjusted_consumption_stats(data["consumption_stats"], data)
    data["actual_consumption_stats"] = actual_consumption_stats(data["consumption_stats"], data)
    monitor_groups = [public_monitor_group(group) for group in db.list_dashboard_monitor_groups(int(data["id"]))]
    data["monitor_groups"] = monitor_groups
    data["group_rates"] = monitor_group_rates(monitor_groups) or group_rates_from_extra(data.get("last_extra"))
    data["last_group_rate_changed"] = bool(
        any(group.get("last_group_rate_changed") for group in monitor_groups)
        or data.get("last_group_rate_changed")
    )
    if data["platform"] in {"newApi", "sub2Api"} and not data["group_rates"]:
        key_id_enc = data.get("key_id_enc")
        selected_group_id = decrypt_value(key_id_enc, config.app_secret_key) if key_id_enc else None
        if selected_group_id:
            data["group_rates"] = [{"plan_name": f"当前分组 {selected_group_id}", "rate_multiplier": None}]
    return data


def dashboard_source_accounts(
    eliminated_last: bool = False,
    visible_only: bool = False,
    platform: str | None = None,
    name_query: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    if platform in {"newApi", "sub2Api"}:
        grouped = {platform: []}
    else:
        grouped = {"newApi": [], "sub2Api": []}
        platform = None
    for row in db.list_dashboard_accounts(platform=platform, name_query=name_query, visible_only=visible_only):
        grouped[row["platform"]].append(public_dashboard_source_account(row))
    if eliminated_last:
        for accounts in grouped.values():
            accounts.sort(key=lambda account: (1 if account.get("is_eliminated") else 0, str(account.get("name") or "").lower()))
    return grouped


def _filtered_dashboard_accounts(
    account_filter: dict[str, Any] | None = None,
    *,
    visible_only: bool,
    lightweight: bool = False,
) -> tuple[dict[str, list[dict[str, Any]]], float | None]:
    account_filter = account_filter or {}
    platform_filter = account_filter.get("platform") if account_filter.get("platform") in {"newApi", "sub2Api"} else None
    low_balance_filter = account_filter.get("low_balance") if account_filter.get("low_balance") in {"low", "normal"} else None
    default_threshold = _optional_number(db.get_general_settings().get("default_threshold"))
    grouped = {platform_filter: []} if platform_filter else {"newApi": [], "sub2Api": []}
    account_groups = (
        dashboard_source_accounts(
            eliminated_last=True,
            visible_only=visible_only,
            platform=platform_filter,
            name_query=account_filter.get("name") or None,
        )
        if lightweight
        else grouped_accounts(
            eliminated_last=True,
            visible_only=visible_only,
            platform=platform_filter,
            name_query=account_filter.get("name") or None,
        )
    )
    for platform, accounts in account_groups.items():
        for account in accounts:
            account_is_low = is_low_balance_account(account, default_threshold)
            if low_balance_filter in {"low", "normal"} and account.get("is_eliminated"):
                continue
            if low_balance_filter == "low" and not account_is_low:
                continue
            if low_balance_filter == "normal" and account_is_low:
                continue
            grouped[platform].append(account)
    return grouped, default_threshold


def dashboard_grouped_accounts(account_filter: dict[str, Any] | None = None) -> dict[str, list[dict[str, Any]]]:
    grouped_accounts_map, default_threshold = _filtered_dashboard_accounts(account_filter, visible_only=True, lightweight=True)
    grouped = {platform: [] for platform in grouped_accounts_map}
    for platform, accounts in grouped_accounts_map.items():
        for account in accounts:
            monitor_groups = account.get("monitor_groups") if isinstance(account.get("monitor_groups"), list) else []
            account_rows = []
            if monitor_groups:
                for monitor_group in monitor_groups:
                    account_rows.append(public_dashboard_account(account, monitor_group, default_threshold))
            else:
                account_rows.append(public_dashboard_account(account, None, default_threshold))
            row_count = len(account_rows)
            for index, row in enumerate(account_rows):
                row["dashboard_rowspan"] = row_count
                row["dashboardRowspan"] = row_count
                row["dashboard_is_first_row"] = index == 0
                row["dashboardIsFirstRow"] = row["dashboard_is_first_row"]
                row["dashboard_is_last_row"] = index == row_count - 1
                row["dashboardIsLastRow"] = row["dashboard_is_last_row"]
                grouped[platform].append(row)
    return grouped


def consumption_grouped_accounts(account_filter: dict[str, Any] | None = None) -> dict[str, list[dict[str, Any]]]:
    account_filter = account_filter or {}
    platform_filter = account_filter.get("platform") if account_filter.get("platform") in {"newApi", "sub2Api"} else None
    low_balance_filter = account_filter.get("low_balance") if account_filter.get("low_balance") in {"low", "normal"} else None
    default_threshold = _optional_number(db.get_general_settings().get("default_threshold"))
    grouped = {platform_filter: []} if platform_filter else {"newApi": [], "sub2Api": []}

    for row in db.list_dashboard_accounts(platform=platform_filter, name_query=account_filter.get("name") or None):
        account = row_to_dict(row)
        account["is_eliminated"] = bool(account.get("is_eliminated"))
        account_is_low = is_low_balance_account(account, default_threshold)
        if low_balance_filter in {"low", "normal"} and account["is_eliminated"]:
            continue
        if low_balance_filter == "low" and not account_is_low:
            continue
        if low_balance_filter == "normal" and account_is_low:
            continue
        account["last_unit"] = str(account.get("last_unit") or DEFAULT_BALANCE_UNIT).strip() or DEFAULT_BALANCE_UNIT
        account["recharge_paid_amount"] = float(account.get("recharge_paid_amount") or 1)
        account["recharge_received_amount"] = float(account.get("recharge_received_amount") or 1)
        grouped[str(account["platform"])].append(account)

    accounts = [account for platform_accounts in grouped.values() for account in platform_accounts]
    stats_by_account = db.get_consumption_stats_for_accounts(int(account["id"]) for account in accounts)
    for account in accounts:
        stats = stats_by_account.get(int(account["id"]), {})
        account["last_used"] = adjusted_used_balance(account.get("last_used"), account)
        account["today_consumption_adjustment"] = manual_today_consumption_adjustment(account)
        account["consumption_stats"] = adjusted_consumption_stats(stats, account)
        account["actual_consumption_stats"] = actual_consumption_stats(account["consumption_stats"], account)
    return grouped


def summarize_consumption_period(grouped: dict[str, list[dict[str, Any]]], period: dict[str, Any]) -> dict[str, Any]:
    totals: dict[str, float] = {}
    adjustment_totals: dict[str, float] = {}
    consumption_by_base_url: dict[str, tuple[int, float, str, float]] = {}
    key = period["key"]
    for accounts in grouped.values():
        for account in accounts:
            account_id = int(account["id"])
            if period.get("since"):
                consumption = db.get_consumption_between(account_id, str(period["since"]), period.get("until"))
                consumption = actual_consumption_amount(consumption, account)
            else:
                stats = account.get("actual_consumption_stats") if isinstance(account.get("actual_consumption_stats"), dict) else {}
                consumption = _optional_number(stats.get(key))
            if consumption is None:
                continue
            base_url_key = _consumption_base_url_key(account)
            unit = str(account.get("last_unit") or DEFAULT_BALANCE_UNIT).strip() or DEFAULT_BALANCE_UNIT
            adjustment = 0.0
            if key == "today":
                adjustment = actual_consumption_amount(account.get("today_consumption_adjustment"), account) or 0.0
            existing = consumption_by_base_url.get(base_url_key)
            if existing is None or account_id < existing[0]:
                consumption_by_base_url[base_url_key] = (account_id, consumption, unit, adjustment)
    for _, consumption, unit, adjustment in consumption_by_base_url.values():
        totals[unit] = round(totals.get(unit, 0.0) + consumption, 6)
        if adjustment:
            adjustment_totals[unit] = round(adjustment_totals.get(unit, 0.0) + adjustment, 6)
    if key == "today":
        dashboard_adjustment = db.get_dashboard_today_consumption_adjustment()
        if dashboard_adjustment:
            adjustment_unit = next(iter(totals), DEFAULT_BALANCE_UNIT)
            totals[adjustment_unit] = round(totals.get(adjustment_unit, 0.0) + dashboard_adjustment, 6)
            adjustment_totals[adjustment_unit] = round(
                adjustment_totals.get(adjustment_unit, 0.0) + dashboard_adjustment,
                6,
            )
    total_items = [{"amount": amount, "unit": unit} for unit, amount in totals.items()]
    adjustment_items = [{"amount": amount, "unit": unit} for unit, amount in adjustment_totals.items()]
    return {
        **period,
        "totals": total_items,
        "account_count": len(consumption_by_base_url),
        "adjustment_totals": adjustment_items,
        "adjustmentTotals": adjustment_items,
    }


def summarize_consumption_periods(grouped: dict[str, list[dict[str, Any]]], custom_range: dict[str, Any]) -> list[dict[str, Any]]:
    summaries = []
    for period in CONSUMPTION_PERIODS:
        summaries.append(summarize_consumption_period(grouped, period))
        if period["key"] == "today":
            summaries.append(summarize_today_remaining(grouped))
    return summaries


def summarize_today_remaining(grouped: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    remaining_by_base_url: dict[str, tuple[int, float, str]] = {}
    for accounts in grouped.values():
        for account in accounts:
            if not bool(account.get("is_visible", True)):
                continue
            remaining = _optional_number(account.get("last_remaining"))
            if remaining is None:
                continue
            account_id = int(account["id"])
            base_url_key = _consumption_base_url_key(account)
            unit = str(account.get("last_unit") or DEFAULT_BALANCE_UNIT).strip() or DEFAULT_BALANCE_UNIT
            existing = remaining_by_base_url.get(base_url_key)
            if existing is None or account_id < existing[0]:
                remaining_by_base_url[base_url_key] = (account_id, remaining, unit)

    totals: dict[str, float] = {}
    for _, remaining, unit in remaining_by_base_url.values():
        totals[unit] = round(totals.get(unit, 0.0) + remaining, 6)
    total_items = [{"amount": amount, "unit": unit} for unit, amount in totals.items()]
    return {
        **TODAY_REMAINING_SUMMARY,
        "totals": total_items,
        "account_count": len(remaining_by_base_url),
    }


def _consumption_base_url_key(account: dict[str, Any]) -> str:
    base_url = str(account.get("base_url") or "").strip().rstrip("/").lower()
    if base_url:
        return base_url
    return f"account:{account.get('id', '')}"


def consumption_date_range_from_query(request: Request) -> dict[str, Any]:
    start_raw = str(request.query_params.get("consumption_start") or "").strip()
    end_raw = str(request.query_params.get("consumption_end") or "").strip()
    result: dict[str, Any] = {"start": start_raw, "end": end_raw, "active": False}
    if not start_raw or not end_raw:
        return result
    try:
        start_date = datetime.strptime(start_raw, "%Y-%m-%d").date()
        end_date = datetime.strptime(end_raw, "%Y-%m-%d").date()
    except ValueError:
        return result
    if end_date < start_date:
        start_date, end_date = end_date, start_date
        start_raw, end_raw = end_raw, start_raw
    since = datetime(start_date.year, start_date.month, start_date.day, tzinfo=CHINA_TZ)
    until_date = end_date + timedelta(days=1)
    until = datetime(until_date.year, until_date.month, until_date.day, tzinfo=CHINA_TZ)
    return {
        "start": start_raw,
        "end": end_raw,
        "active": True,
        "since": since.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "until": until.astimezone(timezone.utc).isoformat(timespec="seconds"),
    }


def public_edit_account(account_id: int | None) -> dict[str, Any] | None:
    if not account_id:
        return None
    row = db.get_account(account_id)
    if not row:
        return None
    data = public_account(row)
    if row["platform"] == "sub2Api":
        data["key_id"] = decrypt_value(row["key_id_enc"], config.app_secret_key) or ""
        data["email"] = decrypt_value(row["email_enc"], config.app_secret_key) or ""
        data["login_extra_params"] = decrypt_value(row["login_extra_params_enc"], config.app_secret_key) or ""
    if row["platform"] == "newApi":
        data["key_id"] = decrypt_value(row["key_id_enc"], config.app_secret_key) or ""
        data["user_id"] = decrypt_value(row["user_id_enc"], config.app_secret_key) or ""
    return data


def public_adjustment_record(row: Any) -> dict[str, Any]:
    data = row_to_dict(row)
    adjustment_type = data.get("adjustment_type")
    type_label = "核算已用余额" if adjustment_type == "used_balance" else "核算今日消耗"
    if not data.get("adjustment_date") and data.get("created_at"):
        created_date = format_china_time(data["created_at"])
        if created_date != "-":
            data["adjustment_date"] = created_date[:10]
    data["type_label"] = type_label
    data["typeLabel"] = type_label
    data["accountId"] = data.get("account_id")
    data["adjustmentType"] = adjustment_type
    data["adjustmentDate"] = data.get("adjustment_date")
    data["createdAt"] = data.get("created_at")
    data["unit"] = data.get("last_unit") or DEFAULT_BALANCE_UNIT
    return data


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return spa_response()


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return spa_response()


@app.post("/login")
async def login(request: Request):
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        payload = await request.json()
    else:
        payload = await request.form()
    username = str(payload.get("username", ""))
    password = str(payload.get("password", ""))
    user = db.get_user(username)
    if not user or not verify_password(password, user["password_hash"]):
        db.add_log("warning", "auth", f"登录失败: {username or '-'}")
        if "application/json" in content_type:
            return JSONResponse({"ok": False, "detail": "用户名或密码错误"}, status_code=401)
        return RedirectResponse("/login?message=login_failed", status_code=303)
    db.add_log("info", "auth", f"登录成功: {username}")
    token = serializer.dumps({"username": username, "session_version": int(user["session_version"])})
    response = JSONResponse({"ok": True, "user": {"username": username}}) if "application/json" in content_type else RedirectResponse("/", status_code=303)
    response.set_cookie(
        config.session_cookie,
        token,
        httponly=True,
        max_age=config.session_max_age_seconds,
        samesite="lax",
    )
    return response


@app.post("/logout")
async def logout(request: Request):
    user = current_user(request)
    if user:
        db.add_log("info", "auth", f"退出登录: {user}")
    content_type = request.headers.get("content-type", "")
    wants_json = "application/json" in content_type or request.headers.get("accept", "").find("application/json") >= 0
    response = JSONResponse({"ok": True}) if wants_json else RedirectResponse("/login", status_code=303)
    response.delete_cookie(config.session_cookie)
    return response


@app.get("/accounts", response_class=HTMLResponse)
async def accounts_page(request: Request):
    return spa_response()


@app.get("/platform-dispatch", response_class=HTMLResponse)
async def platform_dispatch_page(request: Request):
    return spa_response()


@app.post("/accounts", response_class=HTMLResponse)
async def save_account_form(request: Request):
    redirect = redirect_if_needed(request)
    if redirect:
        return redirect
    form = await request.form()
    account_data = _account_from_form(form)
    account_id = _optional_int(form.get("account_id"))
    try:
        if account_id:
            current_account = db.get_account(account_id)
            if not current_account:
                raise ValueError
            account_data = await _prepare_account_data_for_save(account_data, current_account)
            db.update_account(account_id, account_data)
            db.add_log("info", "account", f"编辑账号: {account_data['platform']} / {account_data['name']}")
        else:
            account_data = await _prepare_account_data_for_save(account_data)
            db.upsert_account(account_data)
            db.add_log("info", "account", f"保存账号: {account_data['platform']} / {account_data['name']}")
    except ValueError:
        return templates.TemplateResponse(
            request,
            "accounts.html",
            template_context(
                request,
                grouped=grouped_accounts(),
                settings=db.get_general_settings(),
                message="账号不存在或已删除",
                edit_account=None,
                account_filter=account_filter_from_query(request),
            ),
            status_code=404,
        )
    except HTTPException as exc:
        return templates.TemplateResponse(
            request,
            "accounts.html",
            template_context(
                request,
                grouped=grouped_accounts(),
                settings=db.get_general_settings(),
                message=exc.detail,
                edit_account=None,
                account_filter=account_filter_from_query(request),
            ),
            status_code=exc.status_code,
        )
    return RedirectResponse("/accounts", status_code=303)


@app.post("/accounts/bulk", response_class=HTMLResponse)
async def bulk_accounts_form(request: Request):
    redirect = redirect_if_needed(request)
    if redirect:
        return redirect
    form = await request.form()
    platform = str(form.get("platform", ""))
    bulk_text = str(form.get("bulk_text", "")).strip()
    imported = import_bulk_accounts(platform, bulk_text)
    db.add_log("info", "account", f"批量导入 {platform} 账号 {imported} 个")
    return templates.TemplateResponse(
        request,
        "accounts.html",
        template_context(
            request,
            grouped=grouped_accounts(),
            settings=db.get_general_settings(),
            message=f"已导入或更新 {imported} 个账号",
            edit_account=None,
            account_filter=account_filter_from_query(request),
        ),
    )


@app.post("/accounts/{account_id}/delete")
async def delete_account_form(request: Request, account_id: int):
    redirect = redirect_if_needed(request)
    if redirect:
        return redirect
    account = db.get_account(account_id)
    db.delete_account(account_id)
    if account:
        db.add_log("warning", "account", f"删除账号: {account['platform']} / {account['name']}")
    return RedirectResponse("/accounts", status_code=303)


@app.post("/accounts/{account_id}/enabled")
async def set_account_enabled_form(request: Request, account_id: int):
    redirect = redirect_if_needed(request)
    if redirect:
        return redirect
    account = db.get_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="账号不存在")
    form = await request.form()
    is_enabled = _to_bool(form.get("is_enabled"))
    db.update_account_enabled(account_id, is_enabled)
    updated = db.get_account(account_id)
    effective_enabled = bool(updated and updated["is_enabled"])
    db.add_log("info", "account", f"{account['platform']} / {account['name']} 自动查询: {'启用' if effective_enabled else '不启用'}")
    return RedirectResponse("/accounts", status_code=303)


@app.post("/accounts/{account_id}/visible")
async def set_account_visible_form(request: Request, account_id: int):
    redirect = redirect_if_needed(request)
    if redirect:
        return redirect
    account = db.get_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="账号不存在")
    form = await request.form()
    is_visible = _to_bool(form.get("is_visible"))
    db.update_account_visible(account_id, is_visible)
    db.add_log("info", "account", f"{account['platform']} / {account['name']} 仪表盘显示: {'显示' if is_visible else '隐藏'}")
    return RedirectResponse("/accounts", status_code=303)


@app.post("/accounts/{account_id}/query")
async def query_account_form(request: Request, account_id: int):
    redirect = redirect_if_needed(request)
    if redirect:
        return redirect
    db.add_log("info", "query", f"手动查询账号: {account_id}")
    await query_one_account(db, account_id)
    return RedirectResponse("/", status_code=303)


@app.post("/accounts/{account_id}/group-query")
async def query_account_group_form(request: Request, account_id: int):
    redirect = redirect_if_needed(request)
    if redirect:
        return redirect
    db.add_log("info", "query", f"手动组查询账号: {account_id}")
    await query_sub2api_group_for_account(db, account_id)
    return RedirectResponse("/", status_code=303)


@app.post("/query-all")
async def query_all_form(request: Request):
    redirect = redirect_if_needed(request)
    if redirect:
        return redirect
    db.add_log("info", "query", "手动查询全部账号")
    await query_all_accounts(db)
    return RedirectResponse("/", status_code=303)


@app.post("/monitor/pause")
async def monitor_pause_form(request: Request):
    redirect = redirect_if_needed(request)
    if redirect:
        return redirect
    form = await request.form()
    paused = _to_bool(form.get("paused", True))
    db.set_monitor_paused(paused)
    scheduler.notify_settings_changed()
    db.add_log("info", "settings", "暂停自动监控" if paused else "恢复自动监控")
    return RedirectResponse("/", status_code=303)


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    return spa_response()


@app.get("/accounts/{account_id}/group-rates", response_class=HTMLResponse)
async def group_rates_page(request: Request, account_id: int):
    return spa_response()


@app.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request):
    return spa_response()


@app.post("/logs/clear")
async def clear_logs_form(request: Request):
    redirect = redirect_if_needed(request)
    if redirect:
        return redirect
    db.clear_logs()
    return RedirectResponse("/logs", status_code=303)


@app.post("/settings/general", response_class=HTMLResponse)
async def save_general_settings_form(request: Request):
    redirect = redirect_if_needed(request)
    if redirect:
        return redirect
    form = await request.form()
    db.update_general_settings(
        float(form.get("request_timeout") or REQUEST_TIMEOUT_SECONDS),
        int(float(form.get("query_interval") or BALANCE_QUERY_INTERVAL_SECONDS)),
        float(form.get("default_threshold") or 5),
        int(float(form.get("group_rate_query_interval") or GROUP_RATE_QUERY_INTERVAL_SECONDS)),
    )
    scheduler.notify_settings_changed()
    db.add_log("info", "settings", "更新通用设置")
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/smtp", response_class=HTMLResponse)
async def save_smtp_settings_form(request: Request):
    redirect = redirect_if_needed(request)
    if redirect:
        return redirect
    form = await request.form()
    db.update_smtp_settings(
        str(form.get("host", "")),
        _optional_int(form.get("port")),
        str(form.get("username", "")),
        str(form.get("password", "")),
        str(form.get("sender", "")),
        str(form.get("sender_name", "")),
        str(form.get("receiver", "")),
        str(form.get("security") or ""),
    )
    db.add_log("info", "settings", "更新 SMTP 设置")
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/smtp/test", response_class=HTMLResponse)
async def test_smtp_form(request: Request):
    redirect = redirect_if_needed(request)
    if redirect:
        return redirect
    form = await request.form()
    db.update_smtp_settings(
        str(form.get("host", "")),
        _optional_int(form.get("port")),
        str(form.get("username", "")),
        str(form.get("password", "")),
        str(form.get("sender", "")),
        str(form.get("sender_name", "")),
        str(form.get("receiver", "")),
        str(form.get("security") or ""),
    )
    message = "测试邮件已发送"
    try:
        send_email(db.get_smtp_settings(), db.secret_key, "余额监控测试邮件", "这是一封 SMTP 配置测试邮件。")
        db.add_log("info", "email", "SMTP 测试邮件发送成功")
    except Exception as exc:
        message = f"测试邮件发送失败: {exc}"
        db.add_log("error", "email", f"SMTP 测试邮件发送失败: {exc}")
    return templates.TemplateResponse(
        request,
        "settings.html",
        template_context(request, settings=db.get_general_settings(), smtp=public_smtp_settings(), message=message),
    )


@app.post("/settings/password", response_class=HTMLResponse)
async def change_password_form(request: Request):
    username = require_user(request)
    form = await request.form()
    current_password = str(form.get("current_password", ""))
    new_password = str(form.get("new_password", ""))
    confirm_password = str(form.get("confirm_password", ""))
    message = None
    user = db.get_user(username)
    if not user or not verify_password(current_password, user["password_hash"]):
        message = "当前密码错误"
        db.add_log("warning", "auth", f"修改密码失败: {username}")
    elif len(new_password) < 8:
        message = "新密码至少需要 8 位"
    elif new_password != confirm_password:
        message = "两次输入的新密码不一致"
    else:
        db.update_user_password(username, new_password)
        db.add_log("info", "auth", f"修改密码成功: {username}")
        response = RedirectResponse("/login?message=password_changed", status_code=303)
        response.delete_cookie(config.session_cookie)
        return response
    return templates.TemplateResponse(
        request,
        "settings.html",
        template_context(request, settings=db.get_general_settings(), smtp=public_smtp_settings(), message=message),
    )


@app.get("/api/accounts")
async def api_accounts(request: Request):
    require_user(request)
    account_filter = account_filter_from_query(request)
    return grouped_account_summaries(platform=account_filter.get("platform"), name_query=account_filter.get("name") or None)


@app.get("/api/platform-dispatch")
async def api_platform_dispatch(request: Request):
    require_user(request)
    return platform_dispatch_cache_response()


@app.get("/api/platform-dispatch/accounts")
async def api_platform_dispatch_accounts(request: Request):
    require_user(request)
    return platform_dispatch_accounts_response()


@app.get("/api/platform-dispatch/accounts/{account_id}/evidence")
async def api_platform_dispatch_account_evidence(
    request: Request, account_id: int, page: int = 1, page_size: int = 20
):
    require_user(request)
    if account_id <= 0:
        return JSONResponse({"ok": False, "message": "账号 ID 必须是正整数"}, status_code=400)
    site_url = db.get_setting(SUB2API_SITE_URL_SETTING, "").strip().rstrip("/")
    cache = db.get_platform_dispatch_cache(
        apply_local_group_account_exclusions=True
    )
    if not cache or cache.get("source_site_url") != site_url:
        return JSONResponse({"ok": False, "message": "请先同步当前站点的账号信息"}, status_code=409)
    account = next(
        (
            item
            for item in cache.get("accounts") or []
            if isinstance(item, dict) and _platform_dispatch_account_id(item) == account_id
        ),
        None,
    )
    if account is None:
        return JSONResponse({"ok": False, "message": "账号不存在"}, status_code=404)
    policy = db.get_platform_dispatch_policy(POLICY_DEFAULTS)["config"]
    short_window_seconds = max(
        1,
        int(policy["probe_interval_seconds"])
        * int(policy["evidence_ttl_multiplier"]),
    )
    since = (
        datetime.now(timezone.utc) - timedelta(seconds=short_window_seconds)
    ).isoformat()
    result = db.list_platform_dispatch_evidence_page(
        site_url,
        account_id,
        page=page,
        page_size=page_size,
        since=since,
    )
    return {
        "ok": True,
        "account": {
            "id": account_id,
            "name": str(account.get("name") or f"账号 {account_id}"),
        },
        "short_window_seconds": short_window_seconds,
        **result,
        "items": [public_platform_dispatch_evidence(item) for item in result["items"]],
    }


@app.get("/api/platform-dispatch/policy")
async def api_platform_dispatch_policy(request: Request):
    require_user(request)
    return platform_dispatch_policy_response()


@app.get("/api/platform-dispatch/cost-source-options")
async def api_platform_dispatch_cost_source_options(request: Request):
    require_user(request)
    items = []
    for option in db.list_platform_dispatch_cost_source_options():
        rate = _optional_number(option.get("effective_rate_multiplier"))
        paid = _optional_number(option.get("recharge_paid_amount")) or 1.0
        received = _optional_number(option.get("recharge_received_amount")) or 1.0
        cost = rate * paid / received if rate is not None and rate > 0 and paid > 0 and received > 0 else None
        items.append(
            {
                **option,
                "upstream_cost_multiplier": cost,
            }
        )
    return {"ok": True, "items": items}


@app.put("/api/platform-dispatch/accounts/{account_id}/cost-binding")
async def api_platform_dispatch_cost_binding_update(request: Request, account_id: int):
    require_user(request)
    if account_id <= 0:
        return JSONResponse({"ok": False, "message": "账号 ID 必须是正整数"}, status_code=400)
    if platform_dispatch_policy_scheduler.lock.locked() or db.has_active_platform_dispatch_job():
        return JSONResponse({"ok": False, "message": "平台调度任务执行期间不能修改成本绑定"}, status_code=409)
    try:
        payload = await request.json()
        monitor_group_id = int(payload.get("monitor_group_id", payload.get("monitorGroupId")))
    except (AttributeError, TypeError, ValueError):
        return JSONResponse({"ok": False, "message": "monitor_group_id 必须是正整数"}, status_code=400)
    if monitor_group_id <= 0:
        return JSONResponse({"ok": False, "message": "monitor_group_id 必须是正整数"}, status_code=400)
    site_url = db.get_setting(SUB2API_SITE_URL_SETTING, "").strip().rstrip("/")
    cache = db.get_platform_dispatch_cache()
    if not cache or cache.get("source_site_url") != site_url:
        return JSONResponse({"ok": False, "message": "请先同步当前站点的账号信息"}, status_code=409)
    account = next(
        (
            item for item in cache.get("accounts") or []
            if isinstance(item, dict) and _platform_dispatch_account_id(item) == account_id
        ),
        None,
    )
    if account is None:
        return JSONResponse({"ok": False, "message": "账号不存在"}, status_code=404)
    try:
        binding = db.save_platform_dispatch_cost_binding(site_url, account_id, monitor_group_id)
    except ValueError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=404)
    db.add_log(
        "info",
        "platform-dispatch-policy",
        f"{account.get('name') or account_id} 绑定上游成本分组 {binding.get('group_plan_name') or binding.get('group_name') or monitor_group_id}",
    )
    platform_dispatch_policy_scheduler.notify_changed(run_immediately=False)
    return {"ok": True}


@app.delete("/api/platform-dispatch/accounts/{account_id}/cost-binding")
async def api_platform_dispatch_cost_binding_delete(request: Request, account_id: int):
    require_user(request)
    if account_id <= 0:
        return JSONResponse({"ok": False, "message": "账号 ID 必须是正整数"}, status_code=400)
    if platform_dispatch_policy_scheduler.lock.locked() or db.has_active_platform_dispatch_job():
        return JSONResponse({"ok": False, "message": "平台调度任务执行期间不能修改成本绑定"}, status_code=409)
    site_url = db.get_setting(SUB2API_SITE_URL_SETTING, "").strip().rstrip("/")
    if not db.delete_platform_dispatch_cost_binding(site_url, account_id):
        return JSONResponse({"ok": False, "message": "成本绑定不存在"}, status_code=404)
    db.add_log("info", "platform-dispatch-policy", f"取消 Sub2API 调度账号 #{account_id} 的上游成本绑定")
    platform_dispatch_policy_scheduler.notify_changed(run_immediately=False)
    return {"ok": True}


@app.put("/api/platform-dispatch/policy")
async def api_platform_dispatch_policy_update(request: Request):
    require_user(request)
    try:
        payload = await request.json()
        current = db.get_platform_dispatch_policy(POLICY_DEFAULTS)["config"]
        config_value = validate_policy_config(payload, current)
    except (TypeError, ValueError) as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
    site_url = db.get_setting(SUB2API_SITE_URL_SETTING, "").strip().rstrip("/")
    db.save_platform_dispatch_policy(config_value, site_url)
    platform_dispatch_policy_scheduler.notify_changed(run_immediately=False)
    db.add_log("info", "platform-dispatch-policy", "平台调度策略配置已更新")
    return platform_dispatch_policy_response()


@app.post("/api/platform-dispatch/accounts/{account_id}/exclude")
async def api_platform_dispatch_account_exclude(request: Request, account_id: int):
    require_user(request)
    if account_id <= 0:
        return JSONResponse({"ok": False, "message": "账号 ID 必须是正整数"}, status_code=400)
    if db.has_active_platform_dispatch_job():
        return JSONResponse({"ok": False, "message": "平台调度任务执行期间不能修改排除账号"}, status_code=409)
    if platform_dispatch_policy_scheduler.lock.locked():
        return JSONResponse({"ok": False, "message": "自动调度策略执行期间不能修改排除账号"}, status_code=409)
    site_url = db.get_setting(SUB2API_SITE_URL_SETTING, "").strip().rstrip("/")
    cache = db.get_platform_dispatch_cache()
    if not cache or cache.get("source_site_url") != site_url:
        return JSONResponse({"ok": False, "message": "请先同步当前站点的账号信息"}, status_code=409)
    account = next(
        (
            item
            for item in cache.get("accounts") or []
            if isinstance(item, dict) and _platform_dispatch_account_id(item) == account_id
        ),
        None,
    )
    if account is None:
        return JSONResponse({"ok": False, "message": "账号不存在"}, status_code=404)
    current = db.get_platform_dispatch_policy(POLICY_DEFAULTS)["config"]
    excluded_ids = [int(value) for value in current.get("excluded_account_ids") or []]
    if account_id not in excluded_ids:
        excluded_ids.append(account_id)
        db.save_platform_dispatch_policy({**current, "excluded_account_ids": excluded_ids}, site_url)
        platform_dispatch_policy_scheduler.notify_changed()
        db.add_log(
            "info",
            "platform-dispatch-policy",
            f"排除 Sub2API 调度账号 {account.get('name') or account_id} (#{account_id})",
        )
    return platform_dispatch_policy_response()


@app.delete("/api/platform-dispatch/excluded-accounts/{account_id}")
async def api_platform_dispatch_excluded_account_delete(request: Request, account_id: int):
    require_user(request)
    if account_id <= 0:
        return JSONResponse({"ok": False, "message": "账号 ID 必须是正整数"}, status_code=400)
    if db.has_active_platform_dispatch_job():
        return JSONResponse({"ok": False, "message": "平台调度任务执行期间不能修改排除账号"}, status_code=409)
    if platform_dispatch_policy_scheduler.lock.locked():
        return JSONResponse({"ok": False, "message": "自动调度策略执行期间不能修改排除账号"}, status_code=409)
    current = db.get_platform_dispatch_policy(POLICY_DEFAULTS)["config"]
    excluded_ids = [int(value) for value in current.get("excluded_account_ids") or []]
    if account_id not in excluded_ids:
        return JSONResponse({"ok": False, "message": "该账号未设置排除"}, status_code=404)
    site_url = db.get_setting(SUB2API_SITE_URL_SETTING, "").strip().rstrip("/")
    db.save_platform_dispatch_policy(
        {**current, "excluded_account_ids": [value for value in excluded_ids if value != account_id]},
        site_url,
    )
    platform_dispatch_policy_scheduler.notify_changed()
    db.add_log("info", "platform-dispatch-policy", f"取消排除 Sub2API 调度账号 #{account_id}")
    return platform_dispatch_policy_response()


@app.put("/api/platform-dispatch/accounts/{account_id}/probe-model")
async def api_platform_dispatch_account_probe_model_update(request: Request, account_id: int):
    require_user(request)
    if account_id <= 0:
        return JSONResponse({"ok": False, "message": "账号 ID 必须是正整数"}, status_code=400)
    if db.has_active_platform_dispatch_job():
        return JSONResponse({"ok": False, "message": "平台调度任务执行期间不能修改探活模型"}, status_code=409)
    if platform_dispatch_policy_scheduler.lock.locked():
        return JSONResponse({"ok": False, "message": "自动调度策略执行期间不能修改探活模型"}, status_code=409)
    try:
        payload = await request.json()
        if not isinstance(payload, dict) or not any(
            key in payload for key in ("model", "probe_model", "probeModel")
        ):
            raise ValueError("model 必须是字符串")
        model = payload.get("model", payload.get("probe_model", payload.get("probeModel")))
        site_url = db.get_setting(SUB2API_SITE_URL_SETTING, "").strip().rstrip("/")
        cache = db.get_platform_dispatch_cache()
        if not cache or cache.get("source_site_url") != site_url:
            return JSONResponse({"ok": False, "message": "请先同步当前站点的账号信息"}, status_code=409)
        account = next(
            (
                item
                for item in cache.get("accounts") or []
                if isinstance(item, dict) and _platform_dispatch_account_id(item) == account_id
            ),
            None,
        )
        if account is None:
            return JSONResponse({"ok": False, "message": "账号不存在"}, status_code=404)
        current = db.get_platform_dispatch_policy(POLICY_DEFAULTS)["config"]
        account_models = dict(current.get("account_probe_models") or {})
        if isinstance(model, str) and not model.strip():
            account_models.pop(str(account_id), None)
        else:
            account_models[str(account_id)] = model
        config_value = validate_policy_config({"account_probe_models": account_models}, current)
    except (TypeError, ValueError) as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
    db.save_platform_dispatch_policy(config_value, site_url)
    platform_dispatch_policy_scheduler.notify_changed()
    configured_model = config_value["account_probe_models"].get(str(account_id), "")
    db.add_log(
        "info",
        "platform-dispatch-policy",
        f"Sub2API 调度账号 {account.get('name') or account_id} (#{account_id}) "
        f"探活模型已{'设置为 ' + configured_model if configured_model else '恢复继承配置'}",
    )
    return platform_dispatch_policy_response()


@app.post("/api/platform-dispatch/accounts/{account_id}/probe")
async def api_platform_dispatch_account_probe(request: Request, account_id: int):
    require_user(request)
    if account_id <= 0:
        return JSONResponse({"ok": False, "message": "账号 ID 必须是正整数"}, status_code=400)
    if db.has_active_platform_dispatch_job():
        return JSONResponse({"ok": False, "message": "平台调度任务执行期间不能单独探活"}, status_code=409)
    cache = db.get_platform_dispatch_cache()
    if not cache or not cache.get("accounts"):
        return JSONResponse({"ok": False, "message": "请先同步账号信息"}, status_code=409)
    try:
        admin_client = sub2api_admin_client()
        account_lock = platform_dispatch_policy_scheduler.account_lock(account_id)
        if account_lock.locked():
            return JSONResponse({"ok": False, "message": "该账号正在执行其他操作"}, status_code=409)
        async with account_lock:
            result = await platform_dispatch_policy_scheduler.probe_account_health(
                admin_client, cache, account_id
            )
    except Sub2ApiAdminError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=exc.status_code)
    except Exception as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=502)

    log_level = "info" if result.get("success") else "warning"
    db.add_log(
        log_level,
        "platform-dispatch-policy",
        f"Sub2API 调度账号 #{account_id} 单独探活{'成功' if result.get('success') else '失败'}"
        + (f"，模型 {result['model']}" if result.get("model") else ""),
    )
    account_payload = platform_dispatch_accounts_response(account_id=account_id)
    updated_account = next(
        (
            item
            for item in account_payload["accounts"]
            if isinstance(item, dict) and _platform_dispatch_account_id(item) == account_id
        ),
        None,
    )
    if updated_account is None:
        return JSONResponse({"ok": False, "message": "账号不存在"}, status_code=404)
    return {"ok": True, "probe": result, "account": updated_account}


@app.put("/api/platform-dispatch/accounts/{account_id}/priority")
async def api_platform_dispatch_account_priority_update(request: Request, account_id: int):
    require_user(request)
    if account_id <= 0:
        return JSONResponse({"ok": False, "message": "账号 ID 必须是正整数"}, status_code=400)
    if db.has_active_platform_dispatch_job():
        return JSONResponse({"ok": False, "message": "平台调度任务执行期间不能修改 Index"}, status_code=409)
    try:
        payload = await request.json()
        if not isinstance(payload, dict) or not any(key in payload for key in ("priority", "index")):
            raise ValueError("priority 或 index 字段不能为空")
        raw_priority = payload.get("priority", payload.get("index"))
        current = db.get_platform_dispatch_policy(POLICY_DEFAULTS)["config"]
        overrides = dict(current.get("account_priority_overrides") or {})
        if raw_priority is None or (isinstance(raw_priority, str) and not raw_priority.strip()):
            overrides.pop(str(account_id), None)
        else:
            overrides[str(account_id)] = raw_priority
        config_value = validate_policy_config({"account_priority_overrides": overrides}, current)
    except (TypeError, ValueError) as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)

    site_url = db.get_setting(SUB2API_SITE_URL_SETTING, "").strip().rstrip("/")
    cache = db.get_platform_dispatch_cache()
    if not cache or cache.get("source_site_url") != site_url:
        return JSONResponse({"ok": False, "message": "请先同步当前站点的账号信息"}, status_code=409)
    account = next(
        (
            item for item in cache.get("accounts") or []
            if isinstance(item, dict) and _platform_dispatch_account_id(item) == account_id
        ),
        None,
    )
    if account is None:
        return JSONResponse({"ok": False, "message": "账号不存在"}, status_code=404)

    account_lock = platform_dispatch_policy_scheduler.account_lock(account_id)
    if account_lock.locked():
        return JSONResponse({"ok": False, "message": "该账号正在执行其他操作"}, status_code=409)
    priority = config_value["account_priority_overrides"].get(
        str(account_id), DEFAULT_ACCOUNT_PRIORITY
    )
    try:
        async with account_lock:
            if _optional_int(account.get("priority")) == priority:
                updated = public_dispatch_account(account, account.get("recent_activity") or [])
            else:
                updated = await sub2api_admin_client().update_account_fields(
                    account_id, {"priority": priority}
                )
    except Sub2ApiAdminError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=exc.status_code)
    except Exception as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=502)

    db.update_platform_dispatch_cached_account(updated)
    db.save_platform_dispatch_policy(config_value, site_url)
    platform_dispatch_policy_scheduler.notify_changed(run_immediately=False)
    db.add_log(
        "info",
        "platform-dispatch-policy",
        f"Sub2API 调度账号 {account.get('name') or account_id} (#{account_id}) Index 设置为 {priority}",
    )
    response = platform_dispatch_accounts_response(account_id=account_id)
    return {
        "ok": True,
        "account": response["accounts"][0],
        "config": config_value,
    }


@app.put("/api/platform-dispatch/groups/{group_id}/probe-model")
async def api_platform_dispatch_group_probe_model_update(request: Request, group_id: int):
    require_user(request)
    if group_id <= 0:
        return JSONResponse({"ok": False, "message": "分组 ID 必须是正整数"}, status_code=400)
    if db.has_active_platform_dispatch_job():
        return JSONResponse({"ok": False, "message": "平台调度任务执行期间不能修改探活模型"}, status_code=409)
    if platform_dispatch_policy_scheduler.lock.locked():
        return JSONResponse({"ok": False, "message": "自动调度策略执行期间不能修改探活模型"}, status_code=409)
    try:
        payload = await request.json()
        if not isinstance(payload, dict) or not any(
            key in payload for key in ("model", "probe_model", "probeModel")
        ):
            raise ValueError("model 必须是字符串")
        model = payload.get("model", payload.get("probe_model", payload.get("probeModel")))
        site_url = db.get_setting(SUB2API_SITE_URL_SETTING, "").strip().rstrip("/")
        cache = db.get_platform_dispatch_cache()
        if not cache or cache.get("source_site_url") != site_url:
            return JSONResponse({"ok": False, "message": "请先同步当前站点的分组信息"}, status_code=409)
        group = next(
            (
                item
                for item in cache.get("groups") or []
                if isinstance(item, dict) and _platform_dispatch_account_id(item) == group_id
            ),
            None,
        )
        if group is None:
            return JSONResponse({"ok": False, "message": "分组不存在"}, status_code=404)
        current = db.get_platform_dispatch_policy(POLICY_DEFAULTS)["config"]
        group_models = dict(current.get("group_probe_models") or {})
        if isinstance(model, str) and not model.strip():
            group_models.pop(str(group_id), None)
        else:
            group_models[str(group_id)] = model
        config_value = validate_policy_config({"group_probe_models": group_models}, current)
    except (TypeError, ValueError) as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
    db.save_platform_dispatch_policy(config_value, site_url)
    platform_dispatch_policy_scheduler.notify_changed()
    configured_model = config_value["group_probe_models"].get(str(group_id), "")
    db.add_log(
        "info",
        "platform-dispatch-policy",
        f"Sub2API 调度分组 {group.get('name') or group_id} (#{group_id}) "
        f"探活模型已{'设置为 ' + configured_model if configured_model else '恢复默认'}",
    )
    return platform_dispatch_policy_response()


@app.put("/api/platform-dispatch/groups/{group_id}/auto-dispatch")
async def api_platform_dispatch_group_auto_dispatch_update(request: Request, group_id: int):
    require_user(request)
    if group_id <= 0:
        return JSONResponse({"ok": False, "message": "分组 ID 必须是正整数"}, status_code=400)
    if db.has_active_platform_dispatch_job():
        return JSONResponse(
            {"ok": False, "message": "平台调度任务执行期间不能修改分组参与状态"},
            status_code=409,
        )
    if platform_dispatch_policy_scheduler.lock.locked():
        return JSONResponse(
            {"ok": False, "message": "自动调度策略执行期间不能修改分组参与状态"},
            status_code=409,
        )
    try:
        payload = await request.json()
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = None
    if not isinstance(payload, dict) or not isinstance(payload.get("enabled"), bool):
        return JSONResponse({"ok": False, "message": "enabled 必须是布尔值"}, status_code=400)

    site_url = db.get_setting(SUB2API_SITE_URL_SETTING, "").strip().rstrip("/")
    cache = db.get_platform_dispatch_cache()
    if not cache or cache.get("source_site_url") != site_url:
        return JSONResponse({"ok": False, "message": "请先同步当前站点的分组信息"}, status_code=409)
    group = next(
        (
            item
            for item in cache.get("groups") or []
            if isinstance(item, dict) and _platform_dispatch_account_id(item) == group_id
        ),
        None,
    )
    if group is None:
        return JSONResponse({"ok": False, "message": "分组不存在或已经被排除"}, status_code=404)

    enabled = payload["enabled"]
    db.set_platform_dispatch_group_auto_dispatch_enabled(site_url, group_id, enabled)
    platform_dispatch_policy_scheduler.notify_changed()
    db.add_log(
        "info",
        "platform-dispatch-policy",
        f"Sub2API 调度分组 {group.get('name') or group_id} (#{group_id}) "
        f"已{'参与' if enabled else '退出'}自动调度",
    )
    return platform_dispatch_cache_response()


@app.post("/api/platform-dispatch/policy/run")
async def api_platform_dispatch_policy_run(request: Request):
    require_user(request)
    if db.has_active_platform_dispatch_job():
        return JSONResponse({"ok": False, "message": "平台调度同步任务正在执行"}, status_code=409)
    try:
        summary = await platform_dispatch_policy_scheduler.run_once()
    except Sub2ApiAdminError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=exc.status_code)
    except Exception as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=502)
    return {"ok": True, "summary": summary, **platform_dispatch_policy_response()}


@app.post("/api/platform-dispatch/policy/stop")
async def api_platform_dispatch_policy_stop(request: Request):
    require_user(request)
    stopped = await platform_dispatch_policy_scheduler.stop_automatic_round()
    config_value = db.get_platform_dispatch_policy(POLICY_DEFAULTS)["config"]
    mode = "自动调度" if config_value.get("enabled") else "自动评分"
    message = f"本轮{mode}已停止，下次仍按原计划执行" if stopped else "当前没有正在执行的自动轮次"
    if stopped:
        db.add_log("info", "platform-dispatch-policy", message)
    return {**platform_dispatch_policy_response(), "stopped": stopped, "message": message}


@app.get("/api/platform-dispatch/actions")
async def api_platform_dispatch_actions(request: Request, page: int = 1, page_size: int = 50):
    require_user(request)
    result = db.list_platform_dispatch_actions(page, page_size)
    return {"ok": True, **result}


@app.get("/api/platform-dispatch/job")
async def api_platform_dispatch_job(request: Request):
    require_user(request)
    return platform_dispatch_job_response()


@app.post("/api/platform-dispatch/sync")
@app.post("/api/platform-dispatch/refresh")
async def api_platform_dispatch_sync(request: Request):
    require_user(request)
    active_job = db.get_platform_dispatch_job()
    if active_job and active_job.get("status") in {"queued", "pending", "running"}:
        return JSONResponse(
            {"ok": False, "message": "已有平台调度任务正在执行", "job": public_platform_dispatch_job(active_job)},
            status_code=409,
        )
    if platform_dispatch_policy_scheduler.lock.locked():
        return JSONResponse({"ok": False, "message": "自动调度策略正在执行"}, status_code=409)
    try:
        payload = await request.json()
        refresh_filter = platform_dispatch_refresh_filter(payload)
    except (TypeError, ValueError) as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
    try:
        admin_client = sub2api_admin_client()
    except Sub2ApiAdminError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=exc.status_code)
    target_group_id = refresh_filter["group_id"]
    if target_group_id is not None:
        cache = db.get_platform_dispatch_cache()
        cached_groups = (
            cache.get("groups")
            if cache and cache.get("source_site_url") == admin_client.site_url
            else []
        )
        known_group_ids = {
            _platform_dispatch_account_id(group)
            for group in cached_groups
            if isinstance(group, dict)
        }
        excluded_group_ids = {
            int(group["id"])
            for group in db.list_platform_dispatch_excluded_groups(admin_client.site_url)
        }
        if target_group_id in excluded_group_ids:
            return JSONResponse({"ok": False, "message": "该分组已被排除，不能同步"}, status_code=400)
        if target_group_id not in known_group_ids:
            return JSONResponse({"ok": False, "message": "同步分组不存在，请先同步全部分组"}, status_code=404)
    job_id = uuid4().hex
    job = db.create_platform_dispatch_job(job_id, "accounts_sync", refresh_filter, admin_client.site_url)
    if job is None:
        return JSONResponse(
            {"ok": False, "message": "已有平台调度任务正在执行", "job": public_platform_dispatch_job(db.get_platform_dispatch_job())},
            status_code=409,
        )
    _start_platform_dispatch_task(_run_platform_dispatch_account_sync(job_id, admin_client, refresh_filter))
    return JSONResponse({"ok": True, "job": public_platform_dispatch_job(job)}, status_code=202)


@app.post("/api/platform-dispatch/evidence/refresh")
@app.post("/api/platform-dispatch/activities/refresh")
async def api_platform_dispatch_evidence_refresh(request: Request):
    require_user(request)
    if platform_dispatch_policy_scheduler.lock.locked():
        return JSONResponse({"ok": False, "message": "自动调度策略正在执行"}, status_code=409)
    cache = db.get_platform_dispatch_cache(
        apply_local_group_account_exclusions=True
    )
    accounts = cache.get("accounts") if cache else []
    if not accounts:
        return JSONResponse({"ok": False, "message": "请先同步账号信息"}, status_code=409)
    try:
        admin_client = sub2api_admin_client()
    except Sub2ApiAdminError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=exc.status_code)
    job_id = uuid4().hex
    job = db.create_platform_dispatch_job(
        job_id,
        "evidence_refresh",
        cache.get("refresh_filter") or {},
        admin_client.site_url,
    )
    if job is None:
        return JSONResponse(
            {"ok": False, "message": "已有平台调度任务正在执行", "job": public_platform_dispatch_job(db.get_platform_dispatch_job())},
            status_code=409,
        )
    _start_platform_dispatch_task(_run_platform_dispatch_evidence_refresh(job_id, admin_client, cache))
    return JSONResponse({"ok": True, "job": public_platform_dispatch_job(job)}, status_code=202)


@app.post("/api/platform-dispatch/groups/{group_id}/exclude")
async def api_platform_dispatch_group_exclude(request: Request, group_id: int):
    require_user(request)
    if group_id <= 0:
        return JSONResponse({"ok": False, "message": "分组 ID 必须是正整数"}, status_code=400)
    if db.has_active_platform_dispatch_job():
        return JSONResponse({"ok": False, "message": "平台调度任务执行期间不能修改排除分组"}, status_code=409)
    if platform_dispatch_policy_scheduler.lock.locked():
        return JSONResponse({"ok": False, "message": "自动调度策略执行期间不能修改排除分组"}, status_code=409)
    site_url = db.get_setting(SUB2API_SITE_URL_SETTING, "").strip().rstrip("/")
    cache = db.get_platform_dispatch_cache()
    if not cache or cache.get("source_site_url") != site_url:
        return JSONResponse({"ok": False, "message": "请先同步当前站点的账号信息"}, status_code=409)
    group = next(
        (
            item
            for item in cache.get("groups") or []
            if isinstance(item, dict) and _platform_dispatch_account_id(item) == group_id
        ),
        None,
    )
    if group is None:
        referenced = any(
            group_id in _cached_platform_dispatch_group_ids(account)
            for account in cache.get("accounts") or []
            if isinstance(account, dict)
        )
        if referenced:
            group = {"id": group_id, "name": f"分组 {group_id}", "platform": ""}
    if group is None:
        return JSONResponse({"ok": False, "message": "分组不存在或已经被排除"}, status_code=404)
    db.exclude_platform_dispatch_group(
        site_url,
        group_id,
        str(group.get("name") or f"分组 {group_id}"),
        str(group.get("platform") or ""),
    )
    db.add_log("info", "platform-dispatch", f"排除 Sub2API 分组 {group.get('name') or group_id} (#{group_id})")
    return platform_dispatch_cache_response()


@app.post("/api/platform-dispatch/ungrouped/exclude")
async def api_platform_dispatch_ungrouped_exclude(request: Request):
    require_user(request)
    if db.has_active_platform_dispatch_job():
        return JSONResponse({"ok": False, "message": "平台调度任务执行期间不能屏蔽未分组账号"}, status_code=409)
    if platform_dispatch_policy_scheduler.lock.locked():
        return JSONResponse({"ok": False, "message": "自动调度策略执行期间不能屏蔽未分组账号"}, status_code=409)
    site_url = db.get_setting(SUB2API_SITE_URL_SETTING, "").strip().rstrip("/")
    if not db.exclude_platform_dispatch_ungrouped_accounts(site_url):
        return JSONResponse({"ok": False, "message": "请先同步当前站点的账号信息"}, status_code=409)
    db.add_log("info", "platform-dispatch", "屏蔽 Sub2API 未分组账号")
    return platform_dispatch_cache_response()


@app.post("/api/platform-dispatch/groups/{group_id}/accounts/{account_id}/exclude")
async def api_platform_dispatch_group_account_exclude(
    request: Request, group_id: int, account_id: int
):
    require_user(request)
    if group_id <= 0 or account_id <= 0:
        return JSONResponse(
            {"ok": False, "message": "分组 ID 和账号 ID 必须是正整数"},
            status_code=400,
        )
    if db.has_active_platform_dispatch_job():
        return JSONResponse(
            {"ok": False, "message": "平台调度任务执行期间不能修改分组账号关系"},
            status_code=409,
        )
    if platform_dispatch_policy_scheduler.lock.locked():
        return JSONResponse(
            {"ok": False, "message": "自动调度策略执行期间不能修改分组账号关系"},
            status_code=409,
        )

    site_url = db.get_setting(SUB2API_SITE_URL_SETTING, "").strip().rstrip("/")
    cache = db.get_platform_dispatch_cache()
    if not cache or cache.get("source_site_url") != site_url:
        return JSONResponse(
            {"ok": False, "message": "请先同步当前站点的账号信息"},
            status_code=409,
        )
    excluded_group_ids = {
        int(item["id"])
        for item in db.list_platform_dispatch_excluded_groups(site_url)
    }
    if group_id in excluded_group_ids:
        return JSONResponse(
            {"ok": False, "message": "该分组已被排除，无法修改成员关系"},
            status_code=404,
        )
    group = next(
        (
            item
            for item in cache.get("groups") or []
            if isinstance(item, dict) and _platform_dispatch_account_id(item) == group_id
        ),
        None,
    )
    account = next(
        (
            item
            for item in cache.get("accounts") or []
            if isinstance(item, dict) and _platform_dispatch_account_id(item) == account_id
        ),
        None,
    )
    if group is None:
        referenced = any(
            group_id in _cached_platform_dispatch_group_ids(item)
            for item in cache.get("accounts") or []
            if isinstance(item, dict)
        )
        if referenced:
            group = {"id": group_id, "name": f"分组 {group_id}", "platform": ""}
    if group is None or account is None:
        return JSONResponse(
            {"ok": False, "message": "分组或账号不存在"},
            status_code=404,
        )
    if group_id not in _cached_platform_dispatch_group_ids(account):
        return JSONResponse(
            {"ok": False, "message": "账号不属于当前分组"},
            status_code=404,
        )

    db.exclude_platform_dispatch_group_account(
        site_url,
        group_id,
        account_id,
        account_name=str(account.get("name") or f"账号 {account_id}"),
        group_name=str(group.get("name") or f"分组 {group_id}"),
        group_platform=str(group.get("platform") or ""),
    )
    db.add_log(
        "info",
        "platform-dispatch",
        f"从 Sub2API 分组 {group.get('name') or group_id} 移除账号 "
        f"{account.get('name') or account_id} (#{account_id})（仅本项目）",
    )
    return platform_dispatch_cache_response()


@app.delete("/api/platform-dispatch/groups/{group_id}/accounts/{account_id}/exclude")
async def api_platform_dispatch_group_account_exclusion_delete(
    request: Request, group_id: int, account_id: int
):
    require_user(request)
    if group_id <= 0 or account_id <= 0:
        return JSONResponse(
            {"ok": False, "message": "分组 ID 和账号 ID 必须是正整数"},
            status_code=400,
        )
    if db.has_active_platform_dispatch_job():
        return JSONResponse(
            {"ok": False, "message": "平台调度任务执行期间不能修改分组账号关系"},
            status_code=409,
        )
    if platform_dispatch_policy_scheduler.lock.locked():
        return JSONResponse(
            {"ok": False, "message": "自动调度策略执行期间不能修改分组账号关系"},
            status_code=409,
        )
    site_url = db.get_setting(SUB2API_SITE_URL_SETTING, "").strip().rstrip("/")
    cache = db.get_platform_dispatch_cache()
    if not cache or cache.get("source_site_url") != site_url:
        return JSONResponse(
            {"ok": False, "message": "请先同步当前站点的账号信息"},
            status_code=409,
        )
    account = next(
        (
            item
            for item in cache.get("accounts") or []
            if isinstance(item, dict)
            and _platform_dispatch_account_id(item) == account_id
        ),
        None,
    )
    if account is None:
        return JSONResponse({"ok": False, "message": "账号不存在"}, status_code=404)
    group = next(
        (
            item
            for item in cache.get("groups") or []
            if isinstance(item, dict)
            and _platform_dispatch_account_id(item) == group_id
        ),
        None,
    )
    if group is None:
        referenced = any(
            group_id in _cached_platform_dispatch_group_ids(item)
            for item in cache.get("accounts") or []
            if isinstance(item, dict)
        )
        if referenced:
            group = {"id": group_id, "name": f"分组 {group_id}", "platform": ""}
    if group is None:
        return JSONResponse({"ok": False, "message": "分组不存在"}, status_code=404)
    if group_id not in _cached_platform_dispatch_group_ids(account):
        return JSONResponse(
            {"ok": False, "message": "账号不属于当前分组"},
            status_code=404,
        )
    if not db.remove_platform_dispatch_group_account_exclusion(
        site_url, group_id, account_id
    ):
        return JSONResponse(
            {"ok": False, "message": "该账号未从本分组移除"},
            status_code=404,
        )
    db.add_log(
        "info",
        "platform-dispatch",
        f"恢复 Sub2API 分组 #{group_id} 与账号 #{account_id} 的本地绑定",
    )
    return platform_dispatch_cache_response()


@app.delete("/api/platform-dispatch/excluded-groups/{group_id}")
async def api_platform_dispatch_excluded_group_delete(request: Request, group_id: int):
    require_user(request)
    if group_id <= 0:
        return JSONResponse({"ok": False, "message": "分组 ID 必须是正整数"}, status_code=400)
    if db.has_active_platform_dispatch_job():
        return JSONResponse({"ok": False, "message": "平台调度任务执行期间不能修改排除分组"}, status_code=409)
    if platform_dispatch_policy_scheduler.lock.locked():
        return JSONResponse({"ok": False, "message": "自动调度策略执行期间不能修改排除分组"}, status_code=409)
    site_url = db.get_setting(SUB2API_SITE_URL_SETTING, "").strip().rstrip("/")
    if not db.remove_platform_dispatch_excluded_group(site_url, group_id):
        return JSONResponse({"ok": False, "message": "该分组未设置排除"}, status_code=404)
    db.add_log("info", "platform-dispatch", f"取消排除 Sub2API 分组 #{group_id}")
    return platform_dispatch_cache_response()


@app.post("/api/platform-dispatch/accounts/{account_id}/schedulable")
async def api_platform_dispatch_account_schedulable(request: Request, account_id: int):
    require_user(request)
    if db.has_active_platform_dispatch_job():
        return JSONResponse({"ok": False, "message": "平台调度任务执行期间不能修改调度状态"}, status_code=409)
    payload = await request.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("schedulable"), bool):
        return JSONResponse({"ok": False, "message": "schedulable 必须是布尔值"}, status_code=400)
    schedulable = payload["schedulable"]
    account_lock = platform_dispatch_policy_scheduler.account_lock(account_id)
    if account_lock.locked():
        return JSONResponse({"ok": False, "message": "该账号正在执行其他操作"}, status_code=409)
    try:
        async with account_lock:
            account = await sub2api_admin_client().update_account_schedulable(account_id, schedulable)
    except Sub2ApiAdminError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=exc.status_code)
    db.update_platform_dispatch_cached_account(account)
    site_url = db.get_setting(SUB2API_SITE_URL_SETTING, "").strip().rstrip("/")
    db.upsert_platform_dispatch_account_state(
        site_url,
        account_id,
        price_protection_blocked=0,
        price_protection_blocked_at=None,
        price_protection_reason="",
        decision_reason=f"人工{'开启' if schedulable else '关闭'}调度",
        last_action_at=utc_now(),
    )
    db.add_log(
        "info",
        "platform-dispatch",
        f"Sub2API 账号 {account.get('name') or account_id}: {'开启' if schedulable else '关闭'}调度",
    )
    return {"ok": True, "account": public_dispatch_account(account, [])}


@app.put("/api/platform-dispatch/accounts/{account_id}/auto-dispatch-pause")
async def api_platform_dispatch_account_auto_dispatch_pause(request: Request, account_id: int):
    require_user(request)
    if account_id <= 0:
        return JSONResponse({"ok": False, "message": "账号 ID 必须是正整数"}, status_code=400)
    if db.has_active_platform_dispatch_job() or platform_dispatch_policy_scheduler.lock.locked():
        return JSONResponse(
            {"ok": False, "message": "平台调度任务执行期间不能修改自动调度暂停状态"},
            status_code=409,
        )
    payload = await request.json()
    if not isinstance(payload, dict) or type(payload.get("paused")) is not bool:
        return JSONResponse({"ok": False, "message": "paused 必须是布尔值"}, status_code=400)
    paused = payload["paused"]
    duration_minutes = payload.get("duration_minutes")
    if paused and duration_minutes is not None:
        if (
            type(duration_minutes) is not int
            or duration_minutes < 1
            or duration_minutes > 525600
        ):
            return JSONResponse(
                {"ok": False, "message": "duration_minutes 必须是 1 到 525600 的整数"},
                status_code=400,
            )

    site_url = db.get_setting(SUB2API_SITE_URL_SETTING, "").strip().rstrip("/")
    cache = db.get_platform_dispatch_cache()
    if not cache or cache.get("source_site_url") != site_url:
        return JSONResponse({"ok": False, "message": "请先同步当前站点的账号信息"}, status_code=409)
    account = next(
        (
            item
            for item in cache.get("accounts") or []
            if isinstance(item, dict) and _platform_dispatch_account_id(item) == account_id
        ),
        None,
    )
    if account is None:
        return JSONResponse({"ok": False, "message": "账号不存在"}, status_code=404)

    state = db.set_platform_dispatch_account_auto_dispatch_pause(
        site_url,
        account_id,
        paused,
        duration_minutes if paused else None,
    )
    if paused:
        detail = "永久暂停" if duration_minutes is None else f"暂停 {duration_minutes} 分钟"
        message = f"Sub2API 调度账号 {account.get('name') or account_id}: {detail}自动调度"
    else:
        message = f"Sub2API 调度账号 {account.get('name') or account_id}: 恢复自动调度"
    db.add_log("info", "platform-dispatch-policy", message)
    platform_dispatch_policy_scheduler.notify_changed(run_immediately=False)

    public_account = public_dispatch_account(account, account.get("recent_activity") or [])
    public_account.update(
        {
            "auto_dispatch_paused": account_id in db.active_platform_dispatch_auto_dispatch_pause_ids(site_url),
            "auto_dispatch_paused_at": state.get("auto_dispatch_paused_at") if paused else None,
            "auto_dispatch_pause_until": state.get("auto_dispatch_pause_until") if paused else None,
        }
    )
    return {"ok": True, "account": public_account}


@app.get("/api/dashboard")
async def api_dashboard(request: Request):
    require_user(request)
    account_filter = account_filter_from_query(request)
    grouped = dashboard_grouped_accounts(account_filter)
    return {
        "grouped": grouped,
        "settings": db.get_general_settings(),
        "account_filter": account_filter,
        "accountFilter": account_filter,
    }


@app.get("/api/dashboard/consumption-summary")
def api_dashboard_consumption_summary(request: Request):
    require_user(request)
    account_filter = account_filter_from_query(request)
    consumption_grouped = consumption_grouped_accounts(account_filter)
    consumption_filter = consumption_date_range_from_query(request)
    consumption_summaries = summarize_consumption_periods(consumption_grouped, consumption_filter)
    return {
        "consumption_summaries": consumption_summaries,
        "consumptionSummaries": consumption_summaries,
        "consumption_filter": consumption_filter,
        "consumptionFilter": consumption_filter,
        "account_filter": account_filter,
        "accountFilter": account_filter,
    }


@app.get("/api/dashboard/accounting-history")
async def api_dashboard_accounting_history(request: Request):
    require_user(request)
    adjustment_type = str(request.query_params.get("type") or "today_consumption").strip()
    if adjustment_type != "today_consumption":
        raise HTTPException(status_code=400, detail="核算类型不正确")
    try:
        limit = max(1, min(int(request.query_params.get("limit") or 200), 1000))
    except ValueError:
        limit = 200
    records = [public_adjustment_record(row) for row in db.list_dashboard_adjustment_history(limit=limit)]
    return {"ok": True, "items": records, "records": records}


@app.delete("/api/dashboard/accounting-history/{record_id}")
async def api_delete_dashboard_accounting_history(request: Request, record_id: int):
    require_user(request)
    deleted = db.delete_dashboard_adjustment_history(record_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="核算记录不存在")
    db.add_log("info", "account", f"删除仪表盘今日消耗核算记录: {deleted['delta']:+g}")
    return {
        "ok": True,
        "record": public_adjustment_record(deleted),
        "today_adjustment": db.get_dashboard_today_consumption_adjustment(),
    }


@app.post("/api/dashboard/today-consumption-adjustment")
async def api_dashboard_today_consumption_adjustment(request: Request):
    require_user(request)
    payload = await request.json()
    if not isinstance(payload, dict):
        return JSONResponse({"ok": False, "message": "请求内容格式不正确"}, status_code=400)
    try:
        delta = _adjustment_delta(payload)
    except ValueError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
    db.adjust_dashboard_today_consumption(delta)
    db.add_log("info", "account", f"核算仪表盘今日消耗总金额: {delta:+g}")
    return {"ok": True, "delta": delta, "today_adjustment": db.get_dashboard_today_consumption_adjustment()}


@app.post("/api/group-rate-change-status/bulk-reset")
async def api_bulk_reset_group_rate_change_status(request: Request):
    require_user(request)
    account_filter = account_filter_from_query(request)
    grouped = dashboard_grouped_accounts(account_filter)
    changed_rows = [
        row
        for rows in grouped.values()
        for row in rows
        if row.get("platform") in {"newApi", "sub2Api"} and row.get("last_group_rate_changed")
    ]
    for row in changed_rows:
        db.update_account_group_rate_change_status(
            int(row["id"]),
            False,
            monitor_group_id=_optional_int(row.get("current_monitor_group_id")),
            group_id=str(row.get("current_group_id") or "").strip() or None,
        )
    count = len(changed_rows)
    db.add_log("info", "account", f"批量重置分组倍率变化状态 {count} 条")
    return {"ok": True, "count": count, "reset_count": count, "resetCount": count}


@app.get("/api/accounts/{account_id}")
async def api_account_detail(request: Request, account_id: int):
    require_user(request)
    account = public_edit_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="账号不存在")
    return {"account": account}


@app.delete("/api/accounts/{account_id}")
async def api_delete_account(request: Request, account_id: int):
    require_user(request)
    account = db.get_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="账号不存在")
    db.delete_account(account_id)
    db.add_log("warning", "account", f"API 删除账号: {account['platform']} / {account['name']}")
    return {"ok": True}


@app.get("/api/account-grabber/extension.zip")
async def api_download_account_grabber(request: Request):
    """下载定制后的 NewAPI/Sub2API 账号导入浏览器扩展 zip。"""
    user = require_user(request)
    try:
        body, filename = build_account_grabber_extension_zip(request.base_url)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    db.add_log("info", "account", f"{user} 下载 NewAPI/Sub2API 账号导入扩展")
    return Response(
        content=body,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/logs")
async def api_logs(request: Request):
    require_user(request)
    return log_page_payload(
        _positive_query_int(request.query_params.get("page"), 1),
        _positive_query_int(request.query_params.get("page_size"), LOG_PAGE_SIZE),
    )


@app.delete("/api/logs")
async def api_clear_logs(request: Request):
    require_user(request)
    db.clear_logs()
    return {"ok": True}


@app.get("/api/accounts/{account_id}/group-rates")
async def api_group_rates(request: Request, account_id: int):
    require_user(request)
    account = db.get_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="账号不存在")
    monitor_group_id = _optional_int(request.query_params.get("monitor_group_id") or request.query_params.get("monitorGroupId"))
    monitor_group_row = db.get_monitor_group(monitor_group_id) if monitor_group_id else None
    if monitor_group_row and monitor_group_row["account_id"] != account_id:
        raise HTTPException(status_code=404, detail="分组不存在")
    return {
        "account": public_account(account),
        "monitor_group": public_monitor_group(monitor_group_row) if monitor_group_row else None,
        "records": [row_to_dict(row) for row in db.list_group_rate_records(account_id, monitor_group_id=monitor_group_id)],
    }


@app.get("/api/accounts/{account_id}/balance-history")
async def api_balance_history(request: Request, account_id: int):
    require_user(request)
    account = db.get_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="账号不存在")
    return {
        "account": public_account(account),
        "records": [row_to_dict(row) for row in db.list_balance_history(account_id)],
    }


@app.delete("/api/accounts/{account_id}/balance-history")
async def api_clear_balance_history(request: Request, account_id: int):
    require_user(request)
    account = db.get_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="账号不存在")
    db.clear_balance_history(account_id)
    db.add_log("warning", "account", f"{account['platform']} / {account['name']} 清空余额历史")
    return {"ok": True}


@app.post("/api/accounts/{account_id}/group-rate-change-status")
async def api_group_rate_change_status(request: Request, account_id: int):
    require_user(request)
    account = db.get_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="账号不存在")
    payload = await request.json()
    changed = False
    monitor_group_id = None
    group_id = None
    if isinstance(payload, dict):
        if "changed" in payload:
            changed = bool(payload.get("changed"))
        elif "group_rate_changed" in payload:
            changed = bool(payload.get("group_rate_changed"))
        elif "groupRateChanged" in payload:
            changed = bool(payload.get("groupRateChanged"))
        monitor_group_id = _optional_int(payload.get("monitor_group_id") or payload.get("monitorGroupId"))
        group_id = str(payload.get("group_id") or payload.get("groupId") or "").strip() or None
    db.update_account_group_rate_change_status(account_id, changed, monitor_group_id=monitor_group_id, group_id=group_id)
    updated = db.get_account(account_id)
    db.add_log("info", "account", f"{account['platform']} / {account['name']} 分组倍率状态: {'变化' if changed else '未变化'}")
    return {"ok": True, "account": public_account(updated)}


@app.post("/api/accounts/{account_id}/eliminated")
async def api_account_eliminated(request: Request, account_id: int):
    require_user(request)
    account = db.get_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="账号不存在")
    payload = await request.json()
    is_eliminated = False
    if isinstance(payload, dict):
        if "is_eliminated" in payload:
            is_eliminated = _to_bool(payload.get("is_eliminated"))
        elif "isEliminated" in payload:
            is_eliminated = _to_bool(payload.get("isEliminated"))
        elif "eliminated" in payload:
            is_eliminated = _to_bool(payload.get("eliminated"))
    db.update_account_eliminated(account_id, is_eliminated)
    updated = db.get_account(account_id)
    db.add_log("info", "account", f"{account['platform']} / {account['name']} 淘汰状态: {'已淘汰' if is_eliminated else '未淘汰'}")
    return {"ok": True, "account": public_account(updated)}


@app.post("/api/accounts/{account_id}/enabled")
async def api_account_enabled(request: Request, account_id: int):
    require_user(request)
    account = db.get_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="账号不存在")
    payload = await request.json()
    is_enabled = False
    if isinstance(payload, dict):
        if "is_enabled" in payload:
            is_enabled = _to_bool(payload.get("is_enabled"))
        elif "isEnabled" in payload:
            is_enabled = _to_bool(payload.get("isEnabled"))
        elif "enabled" in payload:
            is_enabled = _to_bool(payload.get("enabled"))
    db.update_account_enabled(account_id, is_enabled)
    updated = db.get_account(account_id)
    effective_enabled = bool(updated and updated["is_enabled"])
    db.add_log("info", "account", f"{account['platform']} / {account['name']} 自动查询: {'启用' if effective_enabled else '不启用'}")
    return {"ok": True, "account": public_account(updated)}


@app.post("/api/accounts/{account_id}/used-balance-adjustment")
async def api_account_used_balance_adjustment(request: Request, account_id: int):
    require_user(request)
    account = db.get_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="账号不存在")
    payload = await request.json()
    try:
        delta = _adjustment_delta(payload)
    except ValueError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
    updated = db.adjust_used_balance(account_id, delta)
    db.add_log("info", "account", f"{account['platform']} / {account['name']} 核算已用余额: {delta:+g}")
    return {"ok": True, "delta": delta, "account": public_account(updated)}


@app.get("/api/accounts/{account_id}/accounting-history")
async def api_account_accounting_history(request: Request, account_id: int):
    require_user(request)
    if not db.get_account(account_id):
        raise HTTPException(status_code=404, detail="账号不存在")
    adjustment_type = str(request.query_params.get("type") or "used_balance").strip()
    if adjustment_type not in {"used_balance", "today_consumption"}:
        raise HTTPException(status_code=400, detail="核算类型不正确")
    records = [
        public_adjustment_record(row)
        for row in db.list_adjustment_history(adjustment_type, account_id=account_id, limit=200)
    ]
    return {"ok": True, "items": records, "records": records}


@app.delete("/api/accounts/{account_id}/accounting-history/{record_id}")
async def api_delete_account_accounting_history(request: Request, account_id: int, record_id: int):
    require_user(request)
    account = db.get_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="账号不存在")
    deleted = db.delete_adjustment_history(account_id, record_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="核算记录不存在")
    updated = db.get_account(account_id)
    db.add_log("info", "account", f"{account['platform']} / {account['name']} 删除核算记录: {deleted['delta']:+g}")
    return {"ok": True, "record": public_adjustment_record(deleted), "account": public_account(updated)}


@app.post("/api/accounts/{account_id}/visible")
async def api_account_visible(request: Request, account_id: int):
    require_user(request)
    account = db.get_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="账号不存在")
    payload = await request.json()
    is_visible = False
    if isinstance(payload, dict):
        if "is_visible" in payload:
            is_visible = _to_bool(payload.get("is_visible"))
        elif "isVisible" in payload:
            is_visible = _to_bool(payload.get("isVisible"))
        elif "visible" in payload:
            is_visible = _to_bool(payload.get("visible"))
    db.update_account_visible(account_id, is_visible)
    updated = db.get_account(account_id)
    db.add_log("info", "account", f"{account['platform']} / {account['name']} 仪表盘显示: {'显示' if is_visible else '隐藏'}")
    return {"ok": True, "account": public_account(updated)}


@app.post("/api/accounts")
async def api_create_account(request: Request):
    require_user(request)
    payload = await request.json()
    account_data = _account_from_payload(payload)
    account_data = await _prepare_account_data_for_save(account_data)
    account_id = db.upsert_account(account_data)
    db.add_log("info", "account", f"API 保存账号: {payload.get('platform')} / {payload.get('name')}")
    return {"id": account_id, "ok": True, "account": public_edit_account(account_id)}


@app.post("/api/accounts/import-by-base-url")
async def api_import_account_by_base_url(request: Request):
    require_user(request)
    payload = await request.json()
    account_data = _account_from_payload(payload)
    current_account = _account_by_platform_base_url(account_data["platform"], account_data["base_url"])
    account_data["name"] = _unique_account_name(
        account_data["platform"],
        account_data["name"],
        exclude_account_id=int(current_account["id"]) if current_account else None,
    )
    if current_account:
        prepared = await _prepare_account_data_for_save(account_data, current_account)
        db.update_account(int(current_account["id"]), prepared)
        account_id = int(current_account["id"])
        action = "更新"
    else:
        prepared = await _prepare_account_data_for_save(account_data)
        account_id = db.upsert_account(prepared)
        action = "新增"
    db.add_log("info", "account", f"API 按 Base URL 导入账号{action}: {account_data['platform']} / {account_data['base_url']}")
    return {"id": account_id, "ok": True, "action": action, "account": public_edit_account(account_id)}


@app.put("/api/accounts/{account_id}")
async def api_update_account(request: Request, account_id: int):
    require_user(request)
    payload = await request.json()
    current_account = db.get_account(account_id)
    if not current_account:
        raise HTTPException(status_code=404, detail="账号不存在")
    try:
        account_data = _account_patch_from_payload(payload, current_account)
        account_data = await _prepare_account_data_for_save(account_data, current_account)
        db.update_account(account_id, account_data)
    except ValueError:
        raise HTTPException(status_code=404, detail="账号不存在") from None
    db.add_log("info", "account", f"API 更新账号: {payload.get('platform')} / {payload.get('name')}")
    return {"id": account_id, "ok": True, "account": public_edit_account(account_id)}


def _normalized_base_url(value: Any) -> str:
    return str(value or "").strip().rstrip("/")


def _account_by_platform_base_url(platform: str, base_url: str) -> Any | None:
    target = _normalized_base_url(base_url)
    if not target:
        return None
    for account in db.list_accounts(platform=platform):
        if _normalized_base_url(account["base_url"]) == target:
            return account
    return None


def _unique_account_name(platform: str, desired_name: str, exclude_account_id: int | None = None) -> str:
    base = str(desired_name or "").strip() or "account"
    used = {
        str(account["name"] or "")
        for account in db.list_accounts(platform=platform)
        if exclude_account_id is None or int(account["id"]) != exclude_account_id
    }
    if base not in used:
        return base
    index = 2
    while True:
        candidate = f"{base} ({index})"
        if candidate not in used:
            return candidate
        index += 1


async def _prepare_account_data_for_save(account_data: dict[str, Any], current_account: Any | None = None) -> dict[str, Any]:
    if account_data["platform"] == "sub2Api" and not bool(account_data.get("is_visible", True)):
        return account_data
    if _is_sub2api_visibility_only_save(account_data, current_account):
        return account_data
    return await _prepare_sub2api_account_data_for_save(account_data, current_account)


def _is_sub2api_visibility_only_save(account_data: dict[str, Any], current_account: Any | None = None) -> bool:
    if account_data["platform"] != "sub2Api" or not current_account or current_account["platform"] != "sub2Api":
        return False
    if str(account_data.get("platform") or "") != str(current_account["platform"] or ""):
        return False
    if str(account_data.get("name") or "").strip() != str(current_account["name"] or "").strip():
        return False
    if str(account_data.get("base_url") or "").rstrip("/") != str(current_account["base_url"] or "").rstrip("/"):
        return False
    if str(account_data.get("note") or "").strip() != str(current_account["note"] or "").strip():
        return False
    if str(account_data.get("recharge_url") or "").strip() != str(current_account["recharge_url"] or "").strip():
        return False
    if _optional_number(account_data.get("recharge_paid_amount")) != _optional_number(current_account["recharge_paid_amount"]):
        return False
    if _optional_number(account_data.get("recharge_received_amount")) != _optional_number(current_account["recharge_received_amount"]):
        return False
    if _optional_number(account_data.get("threshold")) != _optional_number(current_account["threshold"]):
        return False
    if str(account_data.get("key_id") or "").strip() != str(decrypt_value(current_account["key_id_enc"], config.app_secret_key) or "").strip():
        return False
    if (
        "monitor_group_ids" in account_data
        and _split_group_ids(account_data.get("monitor_group_ids")) != _monitor_group_ids(int(current_account["id"]))
    ):
        return False
    current_login_extra_params = decrypt_value(current_account["login_extra_params_enc"], config.app_secret_key) or ""
    if str(account_data.get("login_extra_params") or "").strip() != current_login_extra_params.strip():
        return False
    for key in ("api_key", "email", "password", "access_token", "refresh_token", "user_id"):
        if str(account_data.get(key) or "").strip():
            return False
    current_visible = bool(current_account["is_visible"])
    current_enabled = bool(current_account["is_enabled"])
    next_visible = bool(account_data.get("is_visible", current_visible))
    next_enabled = bool(account_data.get("is_enabled", current_enabled))
    return (next_visible != current_visible) or (next_enabled != current_enabled)


async def _prepare_sub2api_account_data_for_save(account_data: dict[str, Any], current_account: Any | None = None) -> dict[str, Any]:
    if account_data["platform"] != "sub2Api":
        return account_data
    access_token = str(account_data.get("access_token") or "").strip()
    refresh_token = str(account_data.get("refresh_token") or "").strip()
    if access_token or refresh_token:
        return account_data
    email = str(account_data.get("email") or "").strip()
    password = str(account_data.get("password") or "").strip()
    if not email or not password:
        return account_data
    settings = db.get_general_settings()
    try:
        token_result = await login_sub2api_tokens(
            account_data["base_url"],
            email,
            password,
            settings["request_timeout"],
            db.add_log,
            account_data,
        )
    except Exception as exc:
        db.add_log(
            "warning",
            "account",
            f"sub2Api 登录换取 token 异常，仍保存账号: {account_data.get('platform')} / {account_data.get('name')} - {exc}",
        )
        return account_data
    if not token_result.get("is_valid"):
        db.add_log(
            "warning",
            "account",
            f"sub2Api 登录换取 token 失败，仍保存账号: {account_data.get('platform')} / {account_data.get('name')} - "
            f"{token_result.get('invalid_message') or 'sub2Api 重新登录失败'}",
        )
        return account_data
    account_data["access_token"] = token_result.get("access_token")
    account_data["refresh_token"] = token_result.get("refresh_token")
    return account_data


@app.post("/api/accounts/bulk")
async def api_bulk_accounts(request: Request):
    require_user(request)
    payload = await request.json()
    platform = payload.get("platform")
    if platform not in {"newApi", "sub2Api"}:
        raise HTTPException(status_code=400, detail="platform 必须是 newApi 或 sub2Api")
    bulk_text = str(payload.get("bulk_text") or payload.get("bulkText") or "").strip()
    if bulk_text:
        count = import_bulk_accounts(platform, bulk_text)
        db.add_log("info", "account", f"API 批量导入 {platform} 账号 {count} 个")
        return {"ok": True, "count": count}
    accounts = payload.get("accounts", [])
    count = 0
    for item in accounts:
        item["platform"] = platform
        db.upsert_account(_account_from_payload(item))
        count += 1
    db.add_log("info", "account", f"API 批量导入 {platform} 账号 {count} 个")
    return {"ok": True, "count": count}


@app.post("/api/accounts/{account_id}/query")
async def api_query_account(request: Request, account_id: int):
    require_user(request)
    db.add_log("info", "query", f"API 手动查询账号: {account_id}")
    return await query_one_account(db, account_id)


@app.post("/api/accounts/{account_id}/group-query")
async def api_query_account_group(request: Request, account_id: int):
    require_user(request)
    db.add_log("info", "query", f"API 手动组查询账号: {account_id}")
    return await query_group_rate_for_account(db, account_id)


@app.get("/api/accounts/{account_id}/newapi-groups")
async def api_newapi_groups(request: Request, account_id: int):
    require_user(request)
    account = db.get_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="账号不存在")
    if account["platform"] != "newApi":
        raise HTTPException(status_code=400, detail="仅支持 newApi")
    settings = db.get_general_settings()
    result = await query_newapi_group_options(account, db.secret_key, settings["request_timeout"], db.add_log)
    if not result.get("is_valid"):
        return JSONResponse(result, status_code=400)
    return with_monitor_group_selection(account_id, result)

@app.get("/api/accounts/{account_id}/sub2api-groups")
async def api_sub2api_groups(request: Request, account_id: int):
    require_user(request)
    account = db.get_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="账号不存在")
    if account["platform"] != "sub2Api":
        raise HTTPException(status_code=400, detail="仅支持 sub2Api")
    settings = db.get_general_settings()
    result = await query_sub2api_group_options(account, db.secret_key, settings["request_timeout"], db.add_log)
    if not result.get("is_valid"):
        return JSONResponse(result, status_code=400)
    refreshed_access_token = result.get("refreshed_access_token")
    refreshed_refresh_token = result.get("refreshed_refresh_token")
    if refreshed_access_token or refreshed_refresh_token:
        db.update_account_tokens(account_id, access_token=refreshed_access_token, refresh_token=refreshed_refresh_token)
    return with_monitor_group_selection(account_id, result)


@app.post("/api/accounts/{account_id}/selected-group")
async def api_select_account_group(request: Request, account_id: int):
    require_user(request)
    account = db.get_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="账号不存在")
    if account["platform"] not in {"newApi", "sub2Api"}:
        raise HTTPException(status_code=400, detail="仅支持 newApi 或 sub2Api")
    payload = await request.json()
    group_ids = _group_ids_from_payload(payload)
    if not group_ids and not _payload_has_group_selection(payload):
        raise HTTPException(status_code=400, detail="请选择分组")
    groups = _groups_by_id_from_payload(payload)
    if "groups" in payload:
        available_group_ids = set(groups)
        group_ids = [group_id for group_id in group_ids if group_id in available_group_ids]
    current_group_ids = _monitor_group_ids(account_id)
    added_group_ids, removed_group_ids = _group_id_diff(current_group_ids, group_ids)
    if not added_group_ids and not removed_group_ids:
        updated = db.get_account(account_id)
        return {
            "ok": True,
            "changed": False,
            "account": public_account(updated),
            "added_group_ids": [],
            "removed_group_ids": [],
            "group_result": None,
            "group_results": [],
        }
    monitor_groups = []
    selected_results = []
    for group_id in group_ids:
        group = groups.get(group_id, {"id": group_id, "plan_name": f"当前分组 {group_id}"})
        monitor_groups.append({**group, "group_id": group_id})
        if isinstance(group, dict):
            selected_results.append(account_group_result_from_selection(account["platform"], group_id, group))
    db.replace_account_monitor_groups(account_id, monitor_groups)
    if selected_results:
        db.update_account_group_result(account_id, multi_group_result(selected_results))
    else:
        db.update_account_group_result(account_id, {"extra": None})
    updated = db.get_account(account_id)
    db.add_log("info", "account", f"{account['platform']} / {account['name']} 选择分组: {', '.join(group_ids) or '未选择'}")
    return {
        "ok": True,
        "changed": True,
        "account": public_account(updated),
        "added_group_ids": added_group_ids,
        "removed_group_ids": removed_group_ids,
        "group_result": selected_results[0] if selected_results else None,
        "group_results": selected_results,
    }


@app.post("/api/query-all")
async def api_query_all(request: Request):
    require_user(request)
    db.add_log("info", "query", "API 手动查询全部账号")
    return {"results": await query_all_accounts(db)}


@app.post("/api/monitor/pause")
async def api_monitor_pause(request: Request):
    require_user(request)
    payload = await request.json()
    paused = _to_bool(payload.get("paused", True)) if isinstance(payload, dict) else True
    db.set_monitor_paused(paused)
    scheduler.notify_settings_changed()
    db.add_log("info", "settings", "API 暂停自动监控" if paused else "API 恢复自动监控")
    return {"ok": True, "settings": db.get_general_settings()}


@app.get("/api/settings")
async def api_settings(request: Request):
    require_user(request)
    return {
        "settings": db.get_general_settings(),
        "smtp": public_smtp_settings(),
        "sub2api": public_sub2api_settings(),
    }


@app.post("/api/settings/general")
async def api_general_settings(request: Request):
    require_user(request)
    payload = await request.json()
    current = db.get_general_settings()
    db.update_general_settings(
        float(payload.get("request_timeout", current["request_timeout"])),
        int(payload.get("query_interval", current["query_interval"])),
        float(payload.get("default_threshold", current["default_threshold"])),
        int(payload.get("group_rate_query_interval", payload.get("groupRateQueryInterval", current["group_rate_query_interval"]))),
        _optional_bool_payload(payload, "monitor_paused", "monitorPaused"),
        payload.get("top_menu_visibility", payload.get("topMenuVisibility")),
    )
    scheduler.notify_settings_changed()
    db.add_log("info", "settings", "API 更新通用设置")
    return {"ok": True, "settings": db.get_general_settings()}


@app.post("/api/settings/smtp")
async def api_smtp_settings(request: Request):
    require_user(request)
    payload = await request.json()
    db.update_smtp_settings(
        payload.get("host", ""),
        _optional_int(payload.get("port")),
        payload.get("username", ""),
        payload.get("password", ""),
        payload.get("sender", ""),
        payload.get("sender_name", payload.get("senderName", "")),
        payload.get("receiver", ""),
        payload.get("security", ""),
    )
    db.add_log("info", "settings", "API 更新 SMTP 设置")
    return {"ok": True, "smtp": public_smtp_settings()}


@app.post("/api/settings/sub2api")
async def api_sub2api_settings(request: Request):
    require_user(request)
    payload = await request.json()
    try:
        admin_key = str(payload.get("admin_key", payload.get("adminKey", "")) or "").strip()
        site_url = _normalize_sub2api_site_url(payload.get("site_url", payload.get("siteUrl", "")))
    except ValueError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
    previous_site_url = db.get_setting(SUB2API_SITE_URL_SETTING, "")
    if admin_key:
        db.set_setting(SUB2API_ADMIN_KEY_SETTING, encrypt_value(admin_key, db.secret_key) or "")
    db.set_setting(SUB2API_SITE_URL_SETTING, site_url)
    if previous_site_url != site_url:
        db.clear_platform_dispatch_cache()
        db.disable_platform_dispatch_policy(POLICY_DEFAULTS, site_url)
        platform_dispatch_policy_scheduler.notify_changed()
    db.add_log("info", "settings", "API 更新 Sub2API 配置")
    return {"ok": True, "sub2api": public_sub2api_settings()}


@app.post("/api/settings/smtp/test")
async def api_test_smtp(request: Request):
    require_user(request)
    try:
        send_email(db.get_smtp_settings(), db.secret_key, "余额监控测试邮件", "这是一封 SMTP 配置测试邮件。")
    except Exception as exc:
        db.add_log("error", "email", f"API SMTP 测试邮件发送失败: {exc}")
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
    db.add_log("info", "email", "API SMTP 测试邮件发送成功")
    return {"ok": True}


@app.post("/api/settings/password")
async def api_change_password(request: Request):
    username = require_user(request)
    payload = await request.json()
    current_password = str(payload.get("current_password") or payload.get("currentPassword") or "")
    new_password = str(payload.get("new_password") or payload.get("newPassword") or "")
    confirm_password = str(payload.get("confirm_password") or payload.get("confirmPassword") or "")
    user = db.get_user(username)
    if not user or not verify_password(current_password, user["password_hash"]):
        db.add_log("warning", "auth", f"API 修改密码失败: {username}")
        return JSONResponse({"ok": False, "message": "当前密码错误"}, status_code=400)
    if len(new_password) < 8:
        return JSONResponse({"ok": False, "message": "新密码至少需要 8 位"}, status_code=400)
    if new_password != confirm_password:
        return JSONResponse({"ok": False, "message": "两次输入的新密码不一致"}, status_code=400)
    db.update_user_password(username, new_password)
    db.add_log("info", "auth", f"API 修改密码成功: {username}")
    response = JSONResponse({"ok": True})
    response.delete_cookie(config.session_cookie)
    return response


def _account_from_form(form: Any) -> dict[str, Any]:
    return _account_from_payload(
        {
            "platform": form.get("platform"),
            "name": form.get("name"),
            "base_url": form.get("base_url"),
            "note": form.get("note"),
            "recharge_url": form.get("recharge_url"),
            "recharge_paid_amount": form.get("recharge_paid_amount"),
            "recharge_received_amount": form.get("recharge_received_amount"),
            "key_id": form.get("key_id"),
            "api_key": form.get("api_key"),
            "email": form.get("email"),
            "password": form.get("password"),
            "login_extra_params": form.get("login_extra_params"),
            "access_token": form.get("access_token"),
            "refresh_token": form.get("refresh_token"),
            "user_id": form.get("user_id"),
            "threshold": form.get("threshold"),
            "is_enabled": form.get("is_enabled") == "on",
            "is_visible": form.get("is_visible") == "on",
        }
    )


def _account_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    platform = payload.get("platform")
    if platform not in {"newApi", "sub2Api"}:
        raise HTTPException(status_code=400, detail="platform 必须是 newApi 或 sub2Api")
    is_visible_value = payload.get("is_visible", payload.get("isVisible", payload.get("visible", True)))
    is_visible = _to_bool(is_visible_value)
    name = str(payload.get("name", "")).strip()
    base_url = str(payload.get("base_url") or payload.get("baseUrl") or "").strip()
    if is_visible and (not name or not base_url):
        raise HTTPException(status_code=400, detail="name 和 baseUrl 必填")
    is_eliminated = None
    if "is_eliminated" in payload:
        is_eliminated = _to_bool(payload.get("is_eliminated"))
    elif "isEliminated" in payload:
        is_eliminated = _to_bool(payload.get("isEliminated"))
    elif "eliminated" in payload:
        is_eliminated = _to_bool(payload.get("eliminated"))
    account_data = {
        "platform": platform,
        "name": name,
        "base_url": base_url,
        "note": payload.get("note") or payload.get("remark") or payload.get("remarks") or "",
        "recharge_url": payload.get("recharge_url") or payload.get("rechargeUrl") or "",
        "recharge_paid_amount": payload.get("recharge_paid_amount", payload.get("rechargePaidAmount", 1)),
        "recharge_received_amount": payload.get("recharge_received_amount", payload.get("rechargeReceivedAmount", 1)),
        "key_id": payload.get("key_id") or payload.get("keyId"),
        "api_key": payload.get("api_key") or payload.get("apiKey"),
        "email": payload.get("email"),
        "password": payload.get("password"),
        "login_extra_params": payload.get("login_extra_params", payload.get("loginExtraParams")),
        "access_token": payload.get("access_token") or payload.get("accessToken"),
        "refresh_token": payload.get("refresh_token") or payload.get("refreshToken"),
        "user_id": payload.get("user_id") or payload.get("userId"),
        "threshold": payload.get("threshold"),
        "is_eliminated": is_eliminated,
        "is_visible": is_visible,
    }
    if "monitor_groups" in payload or "monitorGroups" in payload:
        account_data["monitor_groups"] = payload.get("monitor_groups", payload.get("monitorGroups"))
    elif "monitor_group_ids" in payload or "monitorGroupIds" in payload or "group_ids" in payload or "groupIds" in payload:
        account_data["monitor_group_ids"] = (
            payload.get("monitor_group_ids")
            or payload.get("monitorGroupIds")
            or payload.get("group_ids")
            or payload.get("groupIds")
        )
    if "is_enabled" in payload or "isEnabled" in payload or "enabled" in payload:
        account_data["is_enabled"] = _to_bool(payload.get("is_enabled", payload.get("isEnabled", payload.get("enabled"))))
    if "is_visible" in payload or "isVisible" in payload or "visible" in payload:
        account_data["is_visible"] = is_visible
    return account_data


def _account_patch_from_payload(payload: dict[str, Any], current_account: Any) -> dict[str, Any]:
    current_public = public_edit_account(int(current_account["id"])) or {}
    current_platform = str(current_account["platform"] or "")
    patch: dict[str, Any] = {
        "platform": str(payload.get("platform") or current_platform).strip() or current_platform,
        "name": str(payload["name"]).strip() if "name" in payload else str(current_account["name"] or ""),
        "base_url": (
            str(payload.get("base_url") or payload.get("baseUrl") or "").strip()
            if ("base_url" in payload or "baseUrl" in payload)
            else str(current_account["base_url"] or "")
        ),
        "note": (
            payload.get("note") or payload.get("remark") or payload.get("remarks") or ""
            if ("note" in payload or "remark" in payload or "remarks" in payload)
            else str(current_account["note"] or "")
        ),
        "recharge_url": (
            payload.get("recharge_url") or payload.get("rechargeUrl") or ""
            if ("recharge_url" in payload or "rechargeUrl" in payload)
            else str(current_account["recharge_url"] or "")
        ),
        "recharge_paid_amount": payload.get("recharge_paid_amount", payload.get("rechargePaidAmount", current_account["recharge_paid_amount"])),
        "recharge_received_amount": payload.get(
            "recharge_received_amount",
            payload.get("rechargeReceivedAmount", current_account["recharge_received_amount"]),
        ),
        "key_id": (
            payload.get("key_id") or payload.get("keyId")
            if ("key_id" in payload or "keyId" in payload)
            else (current_public.get("key_id") or "")
        ),
        "api_key": payload.get("api_key", payload.get("apiKey")) if ("api_key" in payload or "apiKey" in payload) else None,
        "email": payload.get("email") if "email" in payload else (current_public.get("email") or ""),
        "password": payload.get("password") if "password" in payload else None,
        "login_extra_params": (
            payload.get("login_extra_params", payload.get("loginExtraParams"))
            if ("login_extra_params" in payload or "loginExtraParams" in payload)
            else (current_public.get("login_extra_params") or "")
        ),
        "access_token": payload.get("access_token", payload.get("accessToken")) if ("access_token" in payload or "accessToken" in payload) else None,
        "refresh_token": payload.get("refresh_token", payload.get("refreshToken")) if ("refresh_token" in payload or "refreshToken" in payload) else None,
        "user_id": payload.get("user_id", payload.get("userId")) if ("user_id" in payload or "userId" in payload) else (current_public.get("user_id") or ""),
        "threshold": payload.get("threshold", current_account["threshold"]),
        "is_visible": (
            _to_bool(payload.get("is_visible", payload.get("isVisible", payload.get("visible"))))
            if ("is_visible" in payload or "isVisible" in payload or "visible" in payload)
            else bool(current_account["is_visible"])
        ),
        "is_enabled": (
            _to_bool(payload.get("is_enabled", payload.get("isEnabled", payload.get("enabled"))))
            if ("is_enabled" in payload or "isEnabled" in payload or "enabled" in payload)
            else bool(current_account["is_enabled"])
        ),
        "is_eliminated": (
            _to_bool(payload.get("is_eliminated", payload.get("isEliminated", payload.get("eliminated"))))
            if ("is_eliminated" in payload or "isEliminated" in payload or "eliminated" in payload)
            else bool(current_account["is_eliminated"])
        ),
    }
    if "monitor_groups" in payload or "monitorGroups" in payload:
        patch["monitor_groups"] = payload.get("monitor_groups", payload.get("monitorGroups"))
    elif "monitor_group_ids" in payload or "monitorGroupIds" in payload or "group_ids" in payload or "groupIds" in payload:
        patch["monitor_group_ids"] = (
            payload.get("monitor_group_ids")
            or payload.get("monitorGroupIds")
            or payload.get("group_ids")
            or payload.get("groupIds")
        )
    return patch


def import_bulk_accounts(platform: str, bulk_text: str) -> int:
    if platform not in {"newApi", "sub2Api"}:
        raise HTTPException(status_code=400, detail="platform 必须是 newApi 或 sub2Api")
    if not bulk_text:
        return 0
    count = 0
    try:
        parsed = json.loads(bulk_text)
        items = parsed if isinstance(parsed, list) else parsed.get("accounts", [])
    except json.JSONDecodeError:
        items = []
        for line in bulk_text.splitlines():
            if not line.strip():
                continue
            parts = [part.strip() for part in line.split(",")]
            if platform == "sub2Api":
                if len(parts) >= 7:
                    group_ids = _split_group_ids(parts[2])
                    items.append(
                        {
                            "name": parts[0],
                            "base_url": parts[1],
                            "key_id": parts[2],
                            "monitor_group_ids": group_ids,
                            "email": parts[3],
                            "password": parts[4],
                            "api_key": parts[5],
                            "threshold": parts[6] if len(parts) > 6 else None,
                            "note": parts[7] if len(parts) > 7 else "",
                            "recharge_url": parts[8] if len(parts) > 8 else "",
                        }
                    )
                elif len(parts) >= 6:
                    items.append(
                        {
                            "name": parts[0],
                            "base_url": parts[1],
                            "email": parts[2],
                            "password": parts[3],
                            "api_key": parts[4],
                            "threshold": parts[5] if len(parts) > 5 else None,
                            "note": parts[6] if len(parts) > 6 else "",
                            "recharge_url": parts[7] if len(parts) > 7 else "",
                        }
                    )
                elif len(parts) >= 5:
                    group_ids = _split_group_ids(parts[2])
                    items.append(
                        {
                            "name": parts[0],
                            "base_url": parts[1],
                            "key_id": parts[2],
                            "monitor_group_ids": group_ids,
                            "api_key": parts[3],
                            "threshold": parts[4] if len(parts) > 4 else None,
                            "note": parts[5] if len(parts) > 5 else "",
                            "recharge_url": parts[6] if len(parts) > 6 else "",
                        }
                    )
                elif len(parts) >= 4:
                    items.append(
                        {
                            "name": parts[0],
                            "base_url": parts[1],
                            "api_key": parts[2],
                            "threshold": parts[3] if len(parts) > 3 else None,
                            "note": parts[4] if len(parts) > 4 else "",
                            "recharge_url": parts[5] if len(parts) > 5 else "",
                        }
                    )
            if platform == "newApi" and len(parts) >= 4:
                items.append(
                    {
                        "name": parts[0],
                        "base_url": parts[1],
                        "access_token": parts[2],
                        "user_id": parts[3],
                        "threshold": parts[4] if len(parts) > 4 else None,
                        "note": parts[5] if len(parts) > 5 else "",
                        "recharge_url": parts[6] if len(parts) > 6 else "",
                    }
                )
    for item in items:
        item["platform"] = platform
        db.upsert_account(_account_from_payload(item))
        count += 1
    return count


def public_smtp_settings() -> dict[str, Any]:
    row = db.get_smtp_settings()
    data = row_to_dict(row)
    data.pop("password_enc", None)
    data["has_password"] = bool(row["password_enc"])
    return data


def public_sub2api_settings() -> dict[str, Any]:
    admin_key_enc = db.get_setting(SUB2API_ADMIN_KEY_SETTING, "")
    admin_key = decrypt_value(admin_key_enc, db.secret_key) if admin_key_enc else ""
    site_url = db.get_setting(SUB2API_SITE_URL_SETTING, "")
    masked = _mask_credential(admin_key) if admin_key else ""
    return {
        "site_url": site_url,
        "has_admin_key": bool(admin_key),
        "admin_key_masked": masked,
    }


def _start_platform_dispatch_task(coro: Any) -> None:
    task = asyncio.create_task(coro)
    platform_dispatch_tasks.add(task)
    task.add_done_callback(platform_dispatch_tasks.discard)


async def _run_platform_dispatch_account_sync(
    job_id: str,
    admin_client: Sub2ApiAdminClient,
    refresh_filter: dict[str, Any],
) -> None:
    async with platform_dispatch_policy_scheduler.lock:
        await _run_platform_dispatch_account_sync_locked(job_id, admin_client, refresh_filter)


async def _run_platform_dispatch_account_sync_locked(
    job_id: str,
    admin_client: Sub2ApiAdminClient,
    refresh_filter: dict[str, Any],
) -> None:
    try:
        db.update_platform_dispatch_job(
            job_id,
            status="running",
            phase="groups",
            started_at=utc_now(),
            message="正在获取分组信息",
        )
        old_cache = db.get_platform_dispatch_cache()
        target_group_id = refresh_filter.get("group_id")
        groups = await admin_client.list_groups(
            platform=None if target_group_id is not None else refresh_filter["platform"] or None
        )
        db.refresh_platform_dispatch_excluded_group_metadata(admin_client.site_url, groups)
        excluded_groups = db.list_platform_dispatch_excluded_groups(admin_client.site_url)
        excluded_group_ids = {int(group["id"]) for group in excluded_groups}
        retained_groups: list[dict[str, Any]] = []
        retained_group_ids: set[int] = set()
        for group in groups:
            group_id = _platform_dispatch_account_id(group)
            if not group_id or group_id in excluded_group_ids or group_id in retained_group_ids:
                continue
            retained_group_ids.add(group_id)
            retained_groups.append(group)
        if target_group_id is not None:
            target_group = next(
                (
                    group
                    for group in retained_groups
                    if _platform_dispatch_account_id(group) == target_group_id
                ),
                None,
            )
            if target_group is None:
                raise Sub2ApiAdminError("同步分组不存在或已被排除", status_code=404)
            query_targets: list[tuple[int | str, str]] = [
                (target_group_id, str(target_group.get("name") or f"分组 {target_group_id}"))
            ]
        else:
            query_targets = [
                (group_id, str(group.get("name") or f"分组 {group_id}"))
                for group in retained_groups
                if (group_id := _platform_dispatch_account_id(group))
            ]
        if target_group_id is None and refresh_filter["include_ungrouped"]:
            query_targets.append(("ungrouped", "未分组账号"))

        db.update_platform_dispatch_job(job_id, phase="accounts", message="正在按分组获取账号")
        accounts_by_id: dict[int, dict[str, Any]] = {}
        group_ids_by_account: dict[int, set[int]] = {}
        pages_queried = 0
        target_count = len(query_targets)
        for target_index, (group_query, group_label) in enumerate(query_targets):
            page = 1
            target_processed = 0
            while True:
                page_data = await admin_client.list_accounts_page(
                    page=page,
                    page_size=100,
                    platform=None if target_group_id is not None else refresh_filter["platform"] or None,
                    account_type="apikey",
                    status=None if target_group_id is not None else refresh_filter["status"] or None,
                    group_id=group_query,
                )
                pages_queried += 1
                page_accounts = page_data.get("accounts") or []
                target_processed += len(page_accounts)
                for account in page_accounts:
                    account_id = _platform_dispatch_account_id(account)
                    if not account_id:
                        continue
                    if account_id not in accounts_by_id:
                        accounts_by_id[account_id] = {
                            key: value
                            for key, value in account.items()
                            if key
                            not in {"group_id", "groupId", "group_ids", "groupIds", "groups", "plans"}
                        }
                        group_ids_by_account[account_id] = set()
                    if isinstance(group_query, int):
                        group_ids_by_account[account_id].add(group_query)

                response_total = page_data.get("total")
                response_pages = page_data.get("pages")
                target_total = max(0, int(response_total)) if response_total is not None else 0
                target_pages = max(0, int(response_pages)) if response_pages is not None else 0
                if target_pages:
                    target_progress = min(1, page / target_pages)
                elif target_total:
                    target_progress = min(1, target_processed / target_total)
                else:
                    target_progress = 1 if not page_accounts or len(page_accounts) < 100 else 0
                percent = (
                    min(99, round((target_index + target_progress) * 100 / target_count))
                    if target_count
                    else 99
                )
                processed = len(accounts_by_id)
                db.update_platform_dispatch_job(
                    job_id,
                    current_page=pages_queried,
                    total_pages=pages_queried if percent >= 99 else 0,
                    processed=processed,
                    total=processed,
                    percent=percent,
                    message=f"正在获取 {group_label}，第 {page} 页",
                )
                if not page_accounts:
                    break
                if target_pages and page >= target_pages:
                    break
                if target_total and target_processed >= target_total:
                    break
                if response_pages is None and response_total is None and len(page_accounts) < 100:
                    break
                page += 1

        raw_accounts: list[dict[str, Any]] = []
        for account_id, account in accounts_by_id.items():
            account["group_ids"] = sorted(group_ids_by_account[account_id])
            raw_accounts.append(account)
        raw_accounts.sort(
            key=lambda account: (
                str(account.get("name") or "").strip().lower(),
                _platform_dispatch_account_id(account),
            )
        )
        processed = len(raw_accounts)
        total = processed
        total_pages = pages_queried

        db.update_platform_dispatch_job(job_id, phase="finalizing", percent=99, message="正在写入账号缓存")
        _ensure_platform_dispatch_site_unchanged(admin_client.site_url)
        activities_refreshed_at = ""
        old_accounts: list[dict[str, Any]] = []
        old_groups: list[dict[str, Any]] = []
        if old_cache and old_cache.get("source_site_url") == admin_client.site_url:
            activities_refreshed_at = str(old_cache.get("activities_refreshed_at") or "")
            old_accounts = [
                account
                for account in old_cache.get("accounts") or []
                if isinstance(account, dict)
                and str(account.get("type") or account.get("account_type") or account.get("accountType") or "")
                .strip()
                .casefold()
                in {"", "apikey"}
            ]
            old_groups = [group for group in old_cache.get("groups") or [] if isinstance(group, dict)]
        fetched_accounts = [public_dispatch_account(account, []) for account in raw_accounts]
        public_accounts, added_count, existing_count, membership_count = _merge_platform_dispatch_accounts(
            old_accounts,
            fetched_accounts,
        )
        public_groups = _merge_platform_dispatch_groups(
            old_groups,
            [public_dispatch_group(group) for group in retained_groups],
            excluded_group_ids,
        )
        db.replace_platform_dispatch_cache(
            admin_client.site_url,
            public_accounts,
            public_groups,
            [],
            refresh_filter,
            recent_limit=6,
            activities_refreshed_at=activities_refreshed_at,
        )
        finished_at = utc_now()
        db.update_platform_dispatch_job(
            job_id,
            status="succeeded",
            phase="completed",
            processed=processed,
            total=total,
            percent=100,
            message=(
                f"账号同步完成：拉取 {processed} 个，新增 {added_count} 个，"
                f"跳过 {existing_count} 个，补充分组归属 {membership_count} 个，"
                f"共缓存 {len(public_accounts)} 个账号"
            ),
            error="",
            finished_at=finished_at,
        )
        db.add_log(
            "info",
            "platform-dispatch",
            f"手动同步 Sub2API 平台调度缓存：拉取 {processed} 个，新增 {added_count} 个，"
            f"跳过 {existing_count} 个，补充分组归属 {membership_count} 个，共缓存 {len(public_accounts)} 个账号",
        )
    except asyncio.CancelledError:
        _fail_platform_dispatch_job(job_id, "任务因服务停止中断")
        raise
    except Exception as exc:
        _fail_platform_dispatch_job(job_id, str(exc))


async def _run_platform_dispatch_evidence_refresh(
    job_id: str,
    admin_client: Sub2ApiAdminClient,
    snapshot: dict[str, Any],
) -> None:
    async with platform_dispatch_policy_scheduler.lock:
        await _run_platform_dispatch_evidence_refresh_locked(job_id, admin_client, snapshot)


async def _run_platform_dispatch_evidence_refresh_locked(
    job_id: str,
    admin_client: Sub2ApiAdminClient,
    snapshot: dict[str, Any],
) -> None:
    accounts = [account for account in snapshot.get("accounts") or [] if isinstance(account, dict)]
    policy = db.get_platform_dispatch_policy(POLICY_DEFAULTS)["config"]
    excluded_account_ids = {int(value) for value in policy.get("excluded_account_ids") or []}
    candidates = [
        account
        for account in accounts
        if _platform_dispatch_account_id(account) not in excluded_account_ids
    ]
    total = len(
        filter_platform_dispatch_accounts_by_groups(
            candidates,
            db.disabled_platform_dispatch_group_ids(admin_client.site_url),
        )
    )

    def update_progress(phase: str, processed: int, phase_total: int) -> None:
        denominator = max(1, phase_total)
        if phase == "evidence":
            percent = round(processed * 70 / denominator)
            message = f"正在重新获取健康证据，{processed} / {phase_total}"
        else:
            percent = 70 + round(processed * 25 / denominator)
            message = f"正在探活账号，{processed} / {phase_total}"
        db.update_platform_dispatch_job(
            job_id,
            phase=phase,
            processed=processed,
            total=phase_total,
            percent=min(95, percent),
            message=message,
        )

    try:
        db.update_platform_dispatch_job(
            job_id,
            status="running",
            phase="evidence",
            total=total,
            started_at=utc_now(),
            message=f"正在重新获取健康证据，0 / {total}",
        )
        summary = await platform_dispatch_policy_scheduler.refresh_health_evidence(
            admin_client, snapshot, update_progress
        )
        db.update_platform_dispatch_job(job_id, phase="finalizing", percent=98, message="正在重新计算健康分")
        _ensure_platform_dispatch_site_unchanged(admin_client.site_url)
        current_cache = db.get_platform_dispatch_cache()
        if not current_cache or current_cache.get("source_site_url") != admin_client.site_url:
            raise Sub2ApiAdminError("账号缓存已变化，请重新同步后再获取证据", status_code=409)
        warnings = list(summary.get("warnings") or [])
        if not db.replace_platform_dispatch_activities(
            admin_client.site_url, current_cache.get("accounts") or [], warnings
        ):
            raise Sub2ApiAdminError("健康证据刷新时间写入失败，请重新同步后重试", status_code=409)
        db.update_platform_dispatch_job(
            job_id,
            status="succeeded",
            phase="completed",
            processed=total,
            total=total,
            percent=100,
            message=f"健康证据已重新获取，共 {summary['evidence_count']} 条，已探活 {summary['probe_count']} 个账号",
            error="",
            finished_at=utc_now(),
        )
        db.add_log(
            "info",
            "platform-dispatch",
            f"手动重新获取 Sub2API 健康证据 {summary['evidence_count']} 条，探活 {summary['probe_count']} 个账号",
        )
    except asyncio.CancelledError:
        _fail_platform_dispatch_job(job_id, "任务因服务停止中断")
        raise
    except Exception as exc:
        _fail_platform_dispatch_job(job_id, str(exc))


def _ensure_platform_dispatch_site_unchanged(source_site_url: str) -> None:
    current_site_url = db.get_setting(SUB2API_SITE_URL_SETTING, "").strip().rstrip("/")
    if current_site_url != str(source_site_url or "").strip().rstrip("/"):
        raise Sub2ApiAdminError("Sub2API 站点地址已变化，本次任务结果未写入", status_code=409)


def _platform_dispatch_account_id(account: Any) -> int:
    if not isinstance(account, dict):
        return 0
    try:
        account_id = int(account.get("id"))
    except (TypeError, ValueError):
        return 0
    return account_id if account_id > 0 else 0


def _cached_platform_dispatch_group_ids(account: dict[str, Any]) -> set[int]:
    raw_group_ids = account.get("group_ids", account.get("groupIds", []))
    if not isinstance(raw_group_ids, list):
        raw_group_ids = [raw_group_ids]
    group_ids: set[int] = set()
    for value in raw_group_ids:
        try:
            group_id = int(value)
        except (TypeError, ValueError):
            continue
        if group_id > 0:
            group_ids.add(group_id)
    return group_ids


def _merge_platform_dispatch_accounts(
    historical_accounts: list[dict[str, Any]],
    fetched_accounts: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int, int, int]:
    merged_by_id: dict[int, dict[str, Any]] = {}
    for account in historical_accounts:
        account_id = _platform_dispatch_account_id(account)
        if account_id and account_id not in merged_by_id:
            merged_by_id[account_id] = dict(account)

    added_count = 0
    existing_count = 0
    membership_count = 0
    for account in fetched_accounts:
        account_id = _platform_dispatch_account_id(account)
        if not account_id:
            continue
        historical = merged_by_id.get(account_id)
        if historical is None:
            merged_by_id[account_id] = dict(account)
            added_count += 1
            continue
        existing_count += 1
        historical_group_ids = _cached_platform_dispatch_group_ids(historical)
        merged_group_ids = sorted(historical_group_ids | _cached_platform_dispatch_group_ids(account))
        if set(merged_group_ids) != historical_group_ids:
            historical["group_ids"] = merged_group_ids
            historical["groupIds"] = merged_group_ids
            membership_count += 1

    merged = list(merged_by_id.values())
    merged.sort(
        key=lambda account: (
            str(account.get("name") or "").strip().lower(),
            _platform_dispatch_account_id(account),
        )
    )
    return merged, added_count, existing_count, membership_count


def _merge_platform_dispatch_groups(
    historical_groups: list[dict[str, Any]],
    fetched_groups: list[dict[str, Any]],
    excluded_group_ids: set[int],
) -> list[dict[str, Any]]:
    merged_by_id: dict[int, dict[str, Any]] = {}
    for group in [*historical_groups, *fetched_groups]:
        group_id = _platform_dispatch_account_id(group)
        if not group_id or group_id in excluded_group_ids or group_id in merged_by_id:
            continue
        merged_by_id[group_id] = dict(group)
    return sorted(
        merged_by_id.values(),
        key=lambda group: (
            str(group.get("platform") or "").strip().lower(),
            str(group.get("name") or "").strip().lower(),
            _platform_dispatch_account_id(group),
        ),
    )


def _fail_platform_dispatch_job(job_id: str, error: str) -> None:
    message = str(error or "平台调度任务执行失败")
    db.update_platform_dispatch_job(
        job_id,
        status="failed",
        phase="failed",
        message=message,
        error=message,
        finished_at=utc_now(),
    )
    db.add_log("error", "platform-dispatch", f"平台调度任务失败: {message}")


def public_platform_dispatch_job(job: dict[str, Any] | None) -> dict[str, Any] | None:
    if job is None:
        return None
    return dict(job)


def platform_dispatch_job_response() -> dict[str, Any]:
    return {"ok": True, "job": public_platform_dispatch_job(db.get_platform_dispatch_job())}


def platform_dispatch_policy_response() -> dict[str, Any]:
    policy = db.get_platform_dispatch_policy(POLICY_DEFAULTS)
    actions = db.list_platform_dispatch_actions(1, 3)["items"]
    runtime = dict(policy["runtime"])
    is_running = platform_dispatch_policy_scheduler.lock.locked()
    automatic_running = platform_dispatch_policy_scheduler.automatic_round_running
    next_run_at = platform_dispatch_policy_scheduler.next_automatic_run_at
    runtime["is_running"] = is_running
    runtime["automatic_running"] = automatic_running
    runtime["next_run_at"] = next_run_at
    return {
        "ok": True,
        "config": policy["config"],
        "runtime": runtime,
        "is_running": is_running,
        "automatic_running": automatic_running,
        "next_run_at": next_run_at,
        "actions": actions,
    }


def public_platform_dispatch_evidence(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": record.get("id"),
        "source_kind": str(record.get("source_kind") or "unknown"),
        "category": str(record.get("category") or "unknown"),
        "score": record.get("score"),
        "status_code": record.get("status_code"),
        "first_token_ms": record.get("first_token_ms"),
        "duration_ms": record.get("duration_ms"),
        "is_timeout": bool(record.get("is_timeout")),
        "is_probe_success": bool(record.get("is_probe_success")),
        "message": str(record.get("message") or ""),
        "occurred_at": record.get("occurred_at"),
    }


def public_platform_dispatch_excluded_group(group: dict[str, Any]) -> dict[str, Any]:
    return dict(group)


def public_platform_dispatch_group_account_exclusion(
    exclusion: dict[str, Any],
    *,
    account: dict[str, Any] | None = None,
    group: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = dict(exclusion)
    if account:
        result["account_name"] = str(account.get("name") or result.get("account_name") or "")
    if group:
        result["group_name"] = str(group.get("name") or result.get("group_name") or "")
        result["group_platform"] = str(
            group.get("platform") or result.get("group_platform") or ""
        )
    result["group_id"] = int(result.get("group_id") or 0)
    result["account_id"] = int(result.get("account_id") or 0)
    return result


def _snake_case_platform_dispatch_payload(value: Any) -> Any:
    if isinstance(value, list):
        return [_snake_case_platform_dispatch_payload(item) for item in value]
    if not isinstance(value, dict):
        return value

    result: dict[Any, Any] = {}
    for key, item in value.items():
        if isinstance(key, str):
            snake_key = _snake_case_key(key)
            if snake_key != key and snake_key in value:
                continue
        else:
            snake_key = key
        result[snake_key] = _snake_case_platform_dispatch_payload(item)
    return result


def platform_dispatch_cache_response(
    *, include_accounts: bool = False, account_id: int | None = None
) -> dict[str, Any]:
    cache = db.get_platform_dispatch_cache()
    site_url = db.get_setting(SUB2API_SITE_URL_SETTING, "")
    local_group_account_exclusions = db.list_platform_dispatch_group_account_exclusions(site_url)
    excluded_group_rows = db.list_platform_dispatch_excluded_groups(site_url)
    excluded_group_ids = {int(group["id"]) for group in excluded_group_rows}
    excluded_groups = [
        public_platform_dispatch_excluded_group(group)
        for group in excluded_group_rows
    ]
    excluded_group_ids = {int(group["id"]) for group in excluded_groups}
    if cache is None:
        raw_cache_accounts: list[dict[str, Any]] = []
        accounts: list[dict[str, Any]] = []
        groups: list[dict[str, Any]] = []
        warnings: list[str] = []
        refresh_filter = {"platform": "", "type": "", "status": "", "include_ungrouped": True}
        recent_limit = 6
        activities_refreshed_at = None
        refreshed_at = None
        source_site_url = ""
    else:
        raw_cache_accounts = [
            account for account in cache["accounts"] if isinstance(account, dict)
        ]
        effective_cache_accounts = filter_platform_dispatch_accounts_by_group_account_exclusions(
            raw_cache_accounts, local_group_account_exclusions
        )
        accounts = (
            filter_platform_dispatch_accounts_by_groups(effective_cache_accounts, excluded_group_ids)
            if include_accounts
            else []
        )
        groups = [
            group
            for group in cache["groups"]
            if not isinstance(group, dict)
            or _platform_dispatch_account_id(group) not in excluded_group_ids
        ]
        warnings = cache["warnings"]
        refresh_filter = cache["refresh_filter"]
        recent_limit = cache["recent_limit"]
        activities_refreshed_at = cache["activities_refreshed_at"] or None
        refreshed_at = cache["refreshed_at"]
        source_site_url = cache["source_site_url"]
    account_by_id = {
        _platform_dispatch_account_id(account): account
        for account in raw_cache_accounts
        if _platform_dispatch_account_id(account)
    }
    group_by_id = {
        _platform_dispatch_account_id(group): group
        for group in (cache.get("groups") if cache else [])
        if isinstance(group, dict) and _platform_dispatch_account_id(group)
    }
    removed_group_accounts = [
        public_platform_dispatch_group_account_exclusion(
            exclusion,
            account=account_by_id.get(int(exclusion["account_id"])),
            group=group_by_id.get(int(exclusion["group_id"])),
        )
        for exclusion in local_group_account_exclusions
    ]
    groups = [
        group
        for group in groups
        if not isinstance(group, dict)
        or _platform_dispatch_account_id(group) not in excluded_group_ids
    ]
    available_group_ids = {
        _platform_dispatch_account_id(group)
        for group in groups
        if isinstance(group, dict) and _platform_dispatch_account_id(group)
    }
    if include_accounts and excluded_group_ids:
        accounts = filter_platform_dispatch_accounts_by_available_groups(
            accounts, available_group_ids
        )
    if include_accounts and account_id is not None:
        accounts = [
            account
            for account in accounts
            if isinstance(account, dict)
            and _platform_dispatch_account_id(account) == account_id
        ]
    group_auto_dispatch_settings = (
        db.get_platform_dispatch_group_auto_dispatch_settings(source_site_url)
        if source_site_url
        else {}
    )
    for group in groups:
        if not isinstance(group, dict):
            continue
        group_id = _platform_dispatch_account_id(group)
        enabled = group_auto_dispatch_settings.get(group_id, True)
        group["auto_dispatch_enabled"] = enabled
    if include_accounts:
        for account in accounts:
            if isinstance(account, dict):
                account.pop("rate_multiplier", None)
                account.pop("rateMultiplier", None)
    if include_accounts and source_site_url and accounts:
        account_map = {
            _platform_dispatch_account_id(account): account
            for account in accounts
            if isinstance(account, dict) and _platform_dispatch_account_id(account)
        }
        group_rates = {
            _platform_dispatch_account_id(group): _optional_number(group.get("rate_multiplier"))
            for group in groups
            if isinstance(group, dict) and _platform_dispatch_account_id(group)
        }
        policy = db.get_platform_dispatch_policy(POLICY_DEFAULTS)["config"]
        priority_overrides = policy.get("account_priority_overrides") or {}
        profiles = resolve_platform_dispatch_cost_profiles(
            db,
            source_site_url,
            account_map,
            group_rates,
            float(policy["minimum_profit_margin_percent"]),
        )
        if account_id is None:
            states = {
                int(item["account_id"]): item
                for item in db.list_platform_dispatch_account_states(source_site_url)
            }
        else:
            state = db.get_platform_dispatch_account_state(source_site_url, account_id)
            states = {account_id: state} if state else {}
        paused_account_ids = db.active_platform_dispatch_auto_dispatch_pause_ids(source_site_url)
        probes_by_account = db.list_recent_platform_dispatch_probes(
            source_site_url, 15, account_id=account_id
        )
        requests_by_account = db.list_recent_platform_dispatch_requests(
            source_site_url, 10, account_id=account_id
        )
        ttl_seconds = max(
            1,
            int(policy["probe_interval_seconds"])
            * int(policy["evidence_ttl_multiplier"]),
        )
        short_evidence_since = (
            datetime.now(timezone.utc) - timedelta(seconds=ttl_seconds)
        ).isoformat()
        short_evidence_by_account = db.list_short_platform_dispatch_evidence(
            source_site_url, 10, short_evidence_since, account_id=account_id
        )
        for account_id, account in account_map.items():
            priority_override = priority_overrides.get(str(account_id))
            effective_priority = (
                int(priority_override)
                if priority_override is not None
                else DEFAULT_ACCOUNT_PRIORITY
            )
            account["remote_priority"] = _optional_int(account.get("priority"))
            account["priority"] = effective_priority
            account["index"] = effective_priority
            account["priority_override"] = priority_override
            account["default_priority"] = DEFAULT_ACCOUNT_PRIORITY
            account["effective_priority"] = effective_priority
            account.update(public_platform_dispatch_cost_profile(profiles[account_id]))
            state = states.get(account_id) or {}
            account["health_score"] = state.get("health_score")
            account["health_short_score"] = state.get("short_score")
            account["health_long_score"] = state.get("long_score")
            account["health_evidence_count"] = state.get("evidence_count")
            account["health_evidence_at"] = state.get("evidence_at")
            account["health_evidence_fresh"] = bool(state.get("evidence_fresh"))
            account["target_concurrency"] = state.get("target_concurrency")
            account["target_load_factor"] = state.get("target_load_factor")
            account["price_protection_blocked"] = bool(state.get("price_protection_blocked"))
            account["price_protection_blocked_at"] = state.get("price_protection_blocked_at")
            account["price_protection_reason"] = str(state.get("price_protection_reason") or "")
            is_auto_dispatch_paused = account_id in paused_account_ids
            account["auto_dispatch_paused"] = is_auto_dispatch_paused
            account["auto_dispatch_paused_at"] = (
                state.get("auto_dispatch_paused_at") if is_auto_dispatch_paused else None
            )
            account["auto_dispatch_pause_until"] = (
                state.get("auto_dispatch_pause_until") if is_auto_dispatch_paused else None
            )
            if state.get("decision_reason"):
                account["decision_reason"] = state["decision_reason"]
            if state.get("last_action_at"):
                account["last_policy_action_at"] = state["last_action_at"]
            probe_records = [
                public_platform_dispatch_evidence(record)
                for record in probes_by_account.get(account_id, [])
            ]
            short_evidence_records = [
                public_platform_dispatch_evidence(record)
                for record in short_evidence_by_account.get(account_id, [])
            ]
            recent_request_records = [
                public_platform_dispatch_evidence(record)
                for record in requests_by_account.get(account_id, [])
            ]
            account["probe_records"] = probe_records
            account["short_evidence_records"] = short_evidence_records
            account["recent_request_records"] = recent_request_records
    payload = {
        "ok": True,
        "has_cache": cache is not None,
        "site_url": site_url,
        "source_site_url": source_site_url,
        "groups": groups,
        "warnings": warnings,
        "excluded_groups": excluded_groups,
        "removed_group_accounts": removed_group_accounts,
        "recent_limit": recent_limit,
        "activities_refreshed_at": activities_refreshed_at,
        "refreshed_at": refreshed_at,
        "refresh_filter": refresh_filter,
    }
    if include_accounts:
        payload["accounts"] = accounts
    return _snake_case_platform_dispatch_payload(payload)


def platform_dispatch_accounts_response(account_id: int | None = None) -> dict[str, Any]:
    payload = platform_dispatch_cache_response(
        include_accounts=True,
        account_id=account_id,
    )
    return {"ok": True, "accounts": payload["accounts"]}


def platform_dispatch_refresh_filter(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("请求内容格式不正确")
    values: dict[str, Any] = {}
    aliases = {
        "platform": ("platform",),
        "type": ("type", "account_type", "accountType"),
        "status": ("status",),
    }
    for key, candidates in aliases.items():
        raw = next((payload[candidate] for candidate in candidates if candidate in payload), "")
        if raw is None:
            raw = ""
        if not isinstance(raw, str):
            raise ValueError(f"{key} 必须是字符串")
        value = raw.strip()
        if len(value) > 100:
            raise ValueError(f"{key} 长度不能超过 100 个字符")
        values[key] = value
    values["status"] = values["status"].lower()
    allowed_statuses = {"", "active", "inactive", "error", "rate_limited", "temp_unschedulable", "unschedulable"}
    if values["status"] not in allowed_statuses:
        raise ValueError("status 不是有效的 Sub2API 账号筛选状态")
    include_ungrouped = payload.get("include_ungrouped", payload.get("includeUngrouped", True))
    if not isinstance(include_ungrouped, bool):
        raise ValueError("include_ungrouped 必须是布尔值")
    values["include_ungrouped"] = include_ungrouped
    raw_group_id = payload.get("group_id", payload.get("groupId"))
    if raw_group_id is None or raw_group_id == "":
        values["group_id"] = None
    else:
        if isinstance(raw_group_id, bool):
            raise ValueError("group_id 必须是正整数")
        try:
            group_id = int(raw_group_id)
        except (TypeError, ValueError):
            raise ValueError("group_id 必须是正整数") from None
        if group_id <= 0 or str(raw_group_id).strip() != str(group_id):
            raise ValueError("group_id 必须是正整数")
        values["group_id"] = group_id
    values["type"] = "apikey"
    return values


def _mask_credential(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= 12:
        return text[:3] + "*" * max(0, len(text) - 6) + text[-3:]
    return text[:8] + "*" * (len(text) - 12) + text[-4:]


def sub2api_admin_client() -> Sub2ApiAdminClient:
    admin_key_enc = db.get_setting(SUB2API_ADMIN_KEY_SETTING, "")
    admin_key = decrypt_value(admin_key_enc, db.secret_key) if admin_key_enc else ""
    site_url = db.get_setting(SUB2API_SITE_URL_SETTING, "")
    timeout = db.get_general_settings().get("request_timeout", REQUEST_TIMEOUT_SECONDS)
    return Sub2ApiAdminClient(site_url, admin_key, timeout=float(timeout or REQUEST_TIMEOUT_SECONDS))


def _sub2api_import_group_ids(payload: Any) -> list[int]:
    if not isinstance(payload, dict):
        raise ValueError("请求内容格式不正确")
    raw = payload.get("group_ids", payload.get("groupIds", []))
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise ValueError("分组必须是数组")
    group_ids: list[int] = []
    seen = set()
    for item in raw:
        try:
            group_id = int(item)
        except (TypeError, ValueError) as exc:
            raise ValueError("分组 ID 格式不正确") from exc
        if group_id <= 0:
            raise ValueError("分组 ID 格式不正确")
        if group_id in seen:
            continue
        seen.add(group_id)
        group_ids.append(group_id)
    if not group_ids:
        raise ValueError("请选择至少一个 Sub2API 分组")
    return group_ids


def _normalize_sub2api_site_url(value: Any) -> str:
    text = str(value or "").strip().rstrip("/")
    if not text:
        return ""
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("站点地址必须是 http 或 https URL")
    return text


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() not in {"0", "false", "no", "off", ""}


def _optional_bool_payload(payload: dict[str, Any], *keys: str) -> bool | None:
    for key in keys:
        if key in payload:
            return _to_bool(payload.get(key))
    return None


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(float(value))


def _optional_number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _adjustment_delta(payload: Any) -> float:
    if not isinstance(payload, dict):
        raise ValueError("请求内容格式不正确")
    raw_delta = payload.get("delta")
    if raw_delta is None or raw_delta == "":
        amount = _optional_number(payload.get("amount"))
        if amount is None or not math.isfinite(amount) or amount <= 0:
            raise ValueError("请输入大于 0 的金额")
        direction = str(payload.get("direction") or "increase").strip().lower()
        if direction in {"decrease", "minus", "subtract", "減少", "减少"}:
            amount = -amount
        elif direction not in {"increase", "plus", "add", "增加"}:
            raise ValueError("调整方向不正确")
        return amount
    delta = _optional_number(raw_delta)
    if delta is None or not math.isfinite(delta) or delta == 0:
        raise ValueError("调整金额不能为 0")
    return delta


def _split_group_ids(value: Any) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    values = [text]
    for separator in ("|", ";", "\n"):
        values = [part for value in values for part in value.split(separator)]
    return [value.strip() for value in values if value.strip()]

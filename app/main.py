from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

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
    actual_consumption_amount,
    actual_consumption_stats,
    format_china_time,
    monitor_group_to_dict,
    reminder_to_dict,
    row_to_dict,
    utc_now,
)
from app.security import decrypt_value, encrypt_value
from app.security import verify_password
from app.services.balance import login_sub2api_tokens, query_newapi_group_options, query_sub2api_group_options
from app.services.emailer import send_email
from app.services.extension_pack import build_account_grabber_extension_zip, build_extension_zip
from app.services.opencode_go import (
    KEY_LIST_DEFAULT_JS_URL,
    KEY_LIST_GET_REFERENCE_ID,
    KEY_LIST_SERVER_INSTANCE,
    LITE_SUBSCRIPTION_GET_REFERENCE_ID,
    LITE_SUBSCRIPTION_SERVER_INSTANCE,
    OPENCODE_GO_KEY_LIST_JS_URL_SETTING,
    OPENCODE_GO_KEY_LIST_SERVER_ID_SETTING,
    OPENCODE_GO_LITE_JS_URL_SETTING,
    OPENCODE_GO_LITE_SERVER_ID_SETTING,
    fetch_key_list_reference_id,
    fetch_lite_subscription_reference_id,
    mask_api_key,
    public_usage_window,
    validate_opencode_go_key_list_js_url,
    validate_opencode_go_lite_js_url,
)
from app.services.cpa_admin import (
    CPA_AUTHORIZATION_SETTING,
    CPA_SITE_URL_SETTING,
    OPENCODE_GO_CPA_AUTO_DELETE_SETTING,
    CpaAdminClient,
    CpaAdminError,
    cpa_admin_client_from_db,
    normalize_cpa_authorization,
    normalize_cpa_management_url,
)
from app.services.scheduler import (
    BalanceScheduler,
    query_all_accounts,
    query_all_opencode_go_accounts,
    query_group_rate_for_account,
    query_one_account,
    query_opencode_go_for_account,
    query_sub2api_group_for_account,
)
from app.services.sub2api_admin import Sub2ApiAdminClient, Sub2ApiAdminError

config = get_config()
db = Database(config.database_path, config.app_secret_key)
templates = Jinja2Templates(directory="app/templates")
serializer = URLSafeTimedSerializer(config.app_secret_key, salt="balance-monitor-session")
scheduler = BalanceScheduler(db)
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
OPENCODE_GO_PAGE_SIZE = 20
OPENCODE_GO_MAX_PAGE_SIZE = 200
CONSUMPTION_PERIODS = [
    {"key": "today", "label": "今日实际消耗总金额", "count_label": "个 Base URL 有今日记录"},
    {"key": "yesterday", "label": "昨日实际消耗总金额", "count_label": "个 Base URL 有昨日记录"},
    {"key": "last_24h", "label": "近24小时实际消耗总金额", "count_label": "个 Base URL 有近24小时记录"},
    {"key": "last_7d", "label": "近7天实际消耗总金额", "count_label": "个 Base URL 有近7天记录"},
    {"key": "last_14d", "label": "近14天实际消耗总金额", "count_label": "个 Base URL 有近14天记录"},
    {"key": "this_month", "label": "本月实际消耗总金额", "count_label": "个 Base URL 有本月记录"},
    {"key": "last_month", "label": "上月实际消耗总金额", "count_label": "个 Base URL 有上月记录"},
]
SUB2API_ADMIN_KEY_SETTING = "sub2api_admin_key_enc"
SUB2API_SITE_URL_SETTING = "sub2api_site_url"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    db.ensure_admin(config.admin_username, config.admin_password)
    if uses_default_app_secret(config.app_secret_key):
        db.add_log("warning", "security", "APP_SECRET_KEY 仍为默认值，请尽快在 .env 中更换为长随机密钥")
    scheduler.start()
    yield
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
    return {"authenticated": bool(user), "user": {"username": user} if user else None}


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


async def _log_response(request: Request, response: Any):
    body = b""
    async for chunk in response.body_iterator:
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8")
        body += chunk
    if should_log_http_request(request):
        db.add_log(
            "info",
            "http",
            f"IN {request.method} {request.url.path} response={_safe_response_payload(response, body)}",
        )
    response.body_iterator = _body_iterator(body)
    response.headers["content-length"] = str(len(body))
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
    data["consumption_stats"] = db.get_consumption_stats(int(data["id"]))
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


def public_opencode_go_account(row: Any, include_api_key: bool = False) -> dict[str, Any]:
    data = row_to_dict(row)
    email_enc = data.pop("email_enc", None)
    password_enc = data.pop("password_enc", None)
    recovery_email_enc = data.pop("recovery_email_enc", None)
    storage_state_enc = data.pop("storage_state_enc", None)
    api_key_enc = data.pop("api_key_enc", None)
    email = decrypt_value(email_enc, db.secret_key) if email_enc else ""
    recovery_email = decrypt_value(recovery_email_enc, db.secret_key) if recovery_email_enc else ""
    api_key = decrypt_value(api_key_enc, db.secret_key) if include_api_key and api_key_enc else None
    data["email"] = email
    data["recovery_email"] = recovery_email
    data["recoveryEmail"] = recovery_email
    data["has_password"] = bool(password_enc)
    data["hasPassword"] = data["has_password"]
    data["has_session"] = bool(storage_state_enc)
    data["hasSession"] = data["has_session"]
    data["has_api_key"] = bool(api_key_enc)
    data["hasApiKey"] = data["has_api_key"]
    data["api_key_masked"] = data.get("api_key_masked") or (mask_api_key(api_key) if api_key else "")
    data["apiKeyMasked"] = data["api_key_masked"]
    if include_api_key:
        data["api_key"] = api_key or ""
        data["apiKey"] = data["api_key"]
    data["rolling_usage"] = public_usage_window(data.pop("last_rolling_usage", None))
    data["weekly_usage"] = public_usage_window(data.pop("last_weekly_usage", None))
    data["monthly_usage"] = public_usage_window(data.pop("last_monthly_usage", None))
    data.pop("last_raw_json", None)
    data["created_at_formatted"] = format_china_time(data.get("created_at"))
    data["createdAtFormatted"] = data["created_at_formatted"]
    data["createdAt"] = data.get("created_at")
    data["updatedAt"] = data.get("updated_at")
    data["last_checked_at_formatted"] = format_china_time(data.get("last_checked_at"))
    data["lastCheckedAtFormatted"] = data["last_checked_at_formatted"]
    data["lastCheckedAt"] = data.get("last_checked_at")
    data["is_enabled"] = bool(data.get("is_enabled"))
    data["isEnabled"] = data["is_enabled"]
    cpa_provider_disabled = data.get("cpa_provider_disabled")
    data["cpa_provider_disabled"] = None if cpa_provider_disabled is None else bool(cpa_provider_disabled)
    data["cpaProviderDisabled"] = data["cpa_provider_disabled"]
    data["cpa_provider_deleted"] = bool(data.get("cpa_provider_deleted"))
    data["cpaProviderDeleted"] = data["cpa_provider_deleted"]
    data["cpa_deleted_at"] = data.get("cpa_deleted_at")
    data["cpaDeletedAt"] = data["cpa_deleted_at"]
    data["cpa_reenable_pending"] = bool(data.get("cpa_reenable_pending"))
    data["cpaReenablePending"] = data["cpa_reenable_pending"]
    data["cpa_last_action_at"] = data.get("cpa_last_action_at")
    data["cpaLastActionAt"] = data["cpa_last_action_at"]
    data["cpa_last_action_error"] = data.get("cpa_last_action_error")
    data["cpaLastActionError"] = data["cpa_last_action_error"]
    return data


def opencode_go_overall_usage_summary(rows: list[Any]) -> dict[str, Any]:
    eligible_count = 0
    rolling_usage_sum = 0.0
    weekly_usage_sum = 0.0
    for row in rows:
        data = row_to_dict(row)
        weekly_percent = _opencode_go_usage_percent(data.get("last_weekly_usage"))
        if weekly_percent is None or weekly_percent >= 99:
            continue
        rolling_percent = _opencode_go_usage_percent(data.get("last_rolling_usage")) or 0.0
        eligible_count += 1
        rolling_usage_sum += rolling_percent
        weekly_usage_sum += weekly_percent

    rolling_average = rolling_usage_sum / eligible_count if eligible_count else None
    weekly_average = weekly_usage_sum / eligible_count if eligible_count else None
    return {
        "eligible_account_count": eligible_count,
        "eligibleAccountCount": eligible_count,
        "overall_rolling_usage_percent": rolling_average,
        "overallRollingUsagePercent": rolling_average,
        "overall_weekly_usage_percent": weekly_average,
        "overallWeeklyUsagePercent": weekly_average,
    }


def _opencode_go_usage_percent(value: Any) -> float | None:
    usage_percent = public_usage_window(value).get("usage_percent")
    if isinstance(usage_percent, bool) or not isinstance(usage_percent, (int, float)):
        return None
    return float(usage_percent)


def public_opencode_go_history(row: Any) -> dict[str, Any]:
    data = row_to_dict(row)
    data["is_valid"] = bool(data.get("is_valid"))
    data["isValid"] = data["is_valid"]
    data["rolling_usage"] = public_usage_window(data.pop("rolling_usage", None))
    data["weekly_usage"] = public_usage_window(data.pop("weekly_usage", None))
    data["monthly_usage"] = public_usage_window(data.pop("monthly_usage", None))
    data["checked_at_formatted"] = format_china_time(data.get("checked_at"))
    data["checkedAtFormatted"] = data["checked_at_formatted"]
    return data


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
        "today_consumption": consumption_stats.get("today"),
        "todayConsumption": consumption_stats.get("today"),
        "actual_today_consumption": actual_stats.get("today"),
        "actualTodayConsumption": actual_stats.get("today"),
    }
    public_group = public_dashboard_monitor_group(monitor_group) if monitor_group else None
    row["monitor_group"] = public_group
    row["monitorGroup"] = public_group
    if monitor_group:
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
    data["consumption_stats"] = {"today": today_consumption}
    data["actual_consumption_stats"] = {"today": actual_consumption_amount(today_consumption, data)}
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
        account["consumption_stats"] = stats
        account["actual_consumption_stats"] = actual_consumption_stats(stats, account)
    return grouped


def summarize_consumption_period(grouped: dict[str, list[dict[str, Any]]], period: dict[str, Any]) -> dict[str, Any]:
    totals: dict[str, float] = {}
    consumption_by_base_url: dict[str, tuple[int, float, str]] = {}
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
            existing = consumption_by_base_url.get(base_url_key)
            if existing is None or account_id < existing[0]:
                consumption_by_base_url[base_url_key] = (account_id, consumption, unit)
    for _, consumption, unit in consumption_by_base_url.values():
        totals[unit] = round(totals.get(unit, 0.0) + consumption, 6)
    total_items = [{"amount": amount, "unit": unit} for unit, amount in totals.items()]
    return {**period, "totals": total_items, "account_count": len(consumption_by_base_url)}


def summarize_consumption_periods(grouped: dict[str, list[dict[str, Any]]], custom_range: dict[str, Any]) -> list[dict[str, Any]]:
    return [summarize_consumption_period(grouped, period) for period in CONSUMPTION_PERIODS]


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


@app.get("/opencode-go", response_class=HTMLResponse)
async def opencode_go_page(request: Request):
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


@app.get("/api/opencode-go/accounts")
async def api_opencode_go_accounts(request: Request):
    require_user(request)
    page = _positive_query_int(request.query_params.get("page"), 1)
    page_size = min(_positive_query_int(request.query_params.get("page_size") or request.query_params.get("pageSize"), OPENCODE_GO_PAGE_SIZE), OPENCODE_GO_MAX_PAGE_SIZE)
    email = str(request.query_params.get("email") or "").strip()
    weekly_usage_gte_99 = _to_bool(request.query_params.get("weekly_usage_gte_99") or request.query_params.get("weeklyUsageGte99"))
    monthly_usage_gte_99 = _to_bool(request.query_params.get("monthly_usage_gte_99") or request.query_params.get("monthlyUsageGte99"))
    sort_by = str(request.query_params.get("sort_by") or request.query_params.get("sortBy") or "created_at").strip()
    sort_order = str(request.query_params.get("sort_order") or request.query_params.get("sortOrder") or "desc").strip().lower()
    if sort_by not in {"name", "created_at", "updated_at", "last_checked_at"}:
        sort_by = "created_at"
    if sort_order not in {"asc", "desc"}:
        sort_order = "desc"
    total = db.count_opencode_go_accounts(
        email=email,
        weekly_usage_gte_99=weekly_usage_gte_99,
        monthly_usage_gte_99=monthly_usage_gte_99,
    )
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, total_pages)
    accounts = [
        public_opencode_go_account(row)
        for row in db.list_opencode_go_accounts(
            email=email,
            weekly_usage_gte_99=weekly_usage_gte_99,
            monthly_usage_gte_99=monthly_usage_gte_99,
            limit=page_size,
            offset=(page - 1) * page_size,
            sort_by=sort_by,
            sort_order=sort_order,
        )
    ]
    last_success = db.latest_successful_opencode_go_checked_at()
    overall_summary = opencode_go_overall_usage_summary(db.list_opencode_go_accounts())
    return {
        "accounts": accounts,
        "pagination": {
            "page": page,
            "page_size": page_size,
            "pageSize": page_size,
            "total": total,
            "total_pages": total_pages,
            "totalPages": total_pages,
            "has_previous": page > 1,
            "hasPrevious": page > 1,
            "has_next": page < total_pages,
            "hasNext": page < total_pages,
            "sort_by": sort_by,
            "sortBy": sort_by,
            "sort_order": sort_order,
            "sortOrder": sort_order,
        },
        "summary": {
            "account_count": total,
            "accountCount": total,
            "last_success_at": last_success,
            "lastSuccessAt": last_success,
            **overall_summary,
        },
    }


@app.get("/api/opencode-go/settings")
async def api_opencode_go_settings(request: Request):
    require_user(request)
    general_settings = db.get_general_settings()
    js_url = db.get_setting(OPENCODE_GO_LITE_JS_URL_SETTING, "")
    server_id = db.get_setting(OPENCODE_GO_LITE_SERVER_ID_SETTING, "")
    key_js_url = db.get_setting(OPENCODE_GO_KEY_LIST_JS_URL_SETTING, KEY_LIST_DEFAULT_JS_URL)
    key_server_id = db.get_setting(OPENCODE_GO_KEY_LIST_SERVER_ID_SETTING, "")
    cpa_auto_delete_enabled = _opencode_go_cpa_auto_delete_enabled()
    return {
        "settings": {
            "query_interval": general_settings["query_interval"],
            "queryInterval": general_settings["query_interval"],
            "monitor_paused": general_settings["monitor_paused"],
            "monitorPaused": general_settings["monitor_paused"],
            "cpa_auto_delete_enabled": cpa_auto_delete_enabled,
            "cpaAutoDeleteEnabled": cpa_auto_delete_enabled,
            "lite_subscription_js_url": js_url,
            "liteSubscriptionJsUrl": js_url,
            "lite_subscription_server_id": server_id,
            "liteSubscriptionServerId": server_id,
            "key_list_js_url": key_js_url,
            "keyListJsUrl": key_js_url,
            "key_list_server_id": key_server_id,
            "keyListServerId": key_server_id,
            "default_server_id": LITE_SUBSCRIPTION_GET_REFERENCE_ID,
            "defaultServerId": LITE_SUBSCRIPTION_GET_REFERENCE_ID,
            "server_instance": LITE_SUBSCRIPTION_SERVER_INSTANCE,
            "serverInstance": LITE_SUBSCRIPTION_SERVER_INSTANCE,
            "default_key_list_js_url": KEY_LIST_DEFAULT_JS_URL,
            "defaultKeyListJsUrl": KEY_LIST_DEFAULT_JS_URL,
            "default_key_list_server_id": KEY_LIST_GET_REFERENCE_ID,
            "defaultKeyListServerId": KEY_LIST_GET_REFERENCE_ID,
            "key_list_server_instance": KEY_LIST_SERVER_INSTANCE,
            "keyListServerInstance": KEY_LIST_SERVER_INSTANCE,
        }
    }


@app.post("/api/opencode-go/settings")
async def api_save_opencode_go_settings(request: Request):
    require_user(request)
    payload = await request.json()
    raw_url = payload.get("lite_subscription_js_url", payload.get("liteSubscriptionJsUrl", payload.get("js_url", payload.get("jsUrl", "")))) if isinstance(payload, dict) else ""
    raw_key_url = payload.get("key_list_js_url", payload.get("keyListJsUrl", payload.get("api_key_js_url", payload.get("apiKeyJsUrl", KEY_LIST_DEFAULT_JS_URL)))) if isinstance(payload, dict) else KEY_LIST_DEFAULT_JS_URL
    try:
        js_url = validate_opencode_go_lite_js_url(raw_url)
        key_js_url = validate_opencode_go_key_list_js_url(raw_key_url)
    except ValueError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
    server_id = ""
    if js_url:
        try:
            server_id = await fetch_lite_subscription_reference_id(js_url, timeout=15)
        except Exception as exc:
            return JSONResponse({"ok": False, "message": f"解析 JS 文件失败: {exc}"}, status_code=400)
    try:
        key_server_id = await fetch_key_list_reference_id(key_js_url, timeout=15)
    except Exception as exc:
        return JSONResponse({"ok": False, "message": f"解析 API key JS 文件失败: {exc}"}, status_code=400)
    db.set_setting(OPENCODE_GO_LITE_JS_URL_SETTING, js_url)
    db.set_setting(OPENCODE_GO_LITE_SERVER_ID_SETTING, server_id)
    db.set_setting(OPENCODE_GO_KEY_LIST_JS_URL_SETTING, key_js_url)
    db.set_setting(OPENCODE_GO_KEY_LIST_SERVER_ID_SETTING, key_server_id)
    scheduler.notify_settings_changed()
    general_settings = db.get_general_settings()
    cpa_auto_delete_enabled = _opencode_go_cpa_auto_delete_enabled()
    db.add_log("info", "opencode-go", "API 更新 OpenCode Go 用量和 API key JS 文件配置")
    return {
        "ok": True,
        "settings": {
            "query_interval": general_settings["query_interval"],
            "queryInterval": general_settings["query_interval"],
            "monitor_paused": general_settings["monitor_paused"],
            "monitorPaused": general_settings["monitor_paused"],
            "cpa_auto_delete_enabled": cpa_auto_delete_enabled,
            "cpaAutoDeleteEnabled": cpa_auto_delete_enabled,
            "lite_subscription_js_url": js_url,
            "liteSubscriptionJsUrl": js_url,
            "lite_subscription_server_id": server_id,
            "liteSubscriptionServerId": server_id,
            "key_list_js_url": key_js_url,
            "keyListJsUrl": key_js_url,
            "key_list_server_id": key_server_id,
            "keyListServerId": key_server_id,
            "default_server_id": LITE_SUBSCRIPTION_GET_REFERENCE_ID,
            "defaultServerId": LITE_SUBSCRIPTION_GET_REFERENCE_ID,
            "server_instance": LITE_SUBSCRIPTION_SERVER_INSTANCE,
            "serverInstance": LITE_SUBSCRIPTION_SERVER_INSTANCE,
            "default_key_list_js_url": KEY_LIST_DEFAULT_JS_URL,
            "defaultKeyListJsUrl": KEY_LIST_DEFAULT_JS_URL,
            "default_key_list_server_id": KEY_LIST_GET_REFERENCE_ID,
            "defaultKeyListServerId": KEY_LIST_GET_REFERENCE_ID,
            "key_list_server_instance": KEY_LIST_SERVER_INSTANCE,
            "keyListServerInstance": KEY_LIST_SERVER_INSTANCE,
        },
    }


@app.post("/api/opencode-go/settings/cpa-auto-delete")
async def api_save_opencode_go_cpa_auto_delete(request: Request):
    require_user(request)
    payload = await request.json()
    enabled = payload.get("enabled") if isinstance(payload, dict) else None
    if not isinstance(enabled, bool):
        return JSONResponse({"ok": False, "message": "enabled 必须是布尔值"}, status_code=400)
    db.set_setting(OPENCODE_GO_CPA_AUTO_DELETE_SETTING, "1" if enabled else "0")
    db.add_log(
        "warning" if enabled else "info",
        "opencode-go",
        f"OpenCode Go 30d CPA 测试失败自动删除已{'开启' if enabled else '关闭'}",
    )
    return {
        "ok": True,
        "settings": {
            "cpa_auto_delete_enabled": enabled,
            "cpaAutoDeleteEnabled": enabled,
        },
    }


@app.post("/api/opencode-go/accounts")
async def api_create_opencode_go_account(request: Request):
    require_user(request)
    payload = await request.json()
    try:
        account_id = db.upsert_opencode_go_account(_opencode_go_account_payload(payload, require_password=True))
    except ValueError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
    account = db.get_opencode_go_account(account_id)
    db.add_log("info", "opencode-go", f"API 保存 OpenCode Go 账号: {account['name']}")
    return {"ok": True, "id": account_id, "account": public_opencode_go_account(account)}


@app.post("/api/opencode-go/accounts/bulk")
async def api_bulk_opencode_go_accounts(request: Request):
    require_user(request)
    payload = await request.json()
    bulk_text = str(payload.get("bulk_text") or payload.get("bulkText") or payload.get("text") or "").strip()
    try:
        result = import_bulk_opencode_go_accounts(bulk_text)
    except ValueError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
    db.add_log("info", "opencode-go", f"API 批量导入 OpenCode Go 账号 {result['count']} 个")
    return {"ok": True, **result}


@app.put("/api/opencode-go/accounts/{account_id}")
async def api_update_opencode_go_account(request: Request, account_id: int):
    require_user(request)
    if not db.get_opencode_go_account(account_id):
        raise HTTPException(status_code=404, detail="OpenCode Go 账号不存在")
    payload = await request.json()
    try:
        db.update_opencode_go_account(account_id, _opencode_go_account_payload(payload, require_password=False))
    except ValueError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
    account = db.get_opencode_go_account(account_id)
    db.add_log("info", "opencode-go", f"API 更新 OpenCode Go 账号: {account['name']}")
    return {"ok": True, "id": account_id, "account": public_opencode_go_account(account)}


@app.delete("/api/opencode-go/accounts/{account_id}")
async def api_delete_opencode_go_account(request: Request, account_id: int):
    require_user(request)
    account = db.get_opencode_go_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="OpenCode Go 账号不存在")
    db.delete_opencode_go_account(account_id)
    db.add_log("warning", "opencode-go", f"API 删除 OpenCode Go 账号: {account['name']}")
    return {"ok": True}


@app.post("/api/opencode-go/accounts/{account_id}/enabled")
async def api_opencode_go_enabled(request: Request, account_id: int):
    require_user(request)
    account = db.get_opencode_go_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="OpenCode Go 账号不存在")
    payload = await request.json()
    is_enabled = _to_bool(payload.get("is_enabled", payload.get("isEnabled", payload.get("enabled")))) if isinstance(payload, dict) else False
    db.update_opencode_go_enabled(account_id, is_enabled)
    scheduler.notify_settings_changed()
    updated = db.get_opencode_go_account(account_id)
    db.add_log("info", "opencode-go", f"{account['name']} OpenCode Go 自动刷新: {'启用' if is_enabled else '停用'}")
    return {"ok": True, "account": public_opencode_go_account(updated)}


@app.get("/api/opencode-go/sub2api/groups")
async def api_opencode_go_sub2api_groups(request: Request):
    require_user(request)
    try:
        groups = await sub2api_admin_client().list_openai_groups()
    except Sub2ApiAdminError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=exc.status_code)
    return {"ok": True, "groups": groups}


@app.post("/api/opencode-go/accounts/{account_id}/import-sub2api")
async def api_import_opencode_go_to_sub2api(request: Request, account_id: int):
    require_user(request)
    account = db.get_opencode_go_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="OpenCode Go 账号不存在")
    payload = await request.json()
    try:
        group_ids = _sub2api_import_group_ids(payload)
    except ValueError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)

    email = decrypt_value(account["email_enc"], db.secret_key) if account["email_enc"] else ""
    api_key = decrypt_value(account["api_key_enc"], db.secret_key) if account["api_key_enc"] else ""
    try:
        result = await sub2api_admin_client().import_opencode_go_account(email, api_key, group_ids)
    except Sub2ApiAdminError as exc:
        db.add_log("error", "opencode-go", f"{account['name']} 导入 Sub2API 失败: {exc}")
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=exc.status_code)
    db.add_log("info", "opencode-go", f"{account['name']} 已导入 Sub2API: {result['name']}")
    return {"ok": True, **result}


@app.post("/api/opencode-go/accounts/{account_id}/import-cpa")
async def api_import_opencode_go_to_cpa(request: Request, account_id: int):
    require_user(request)
    account = db.get_opencode_go_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="OpenCode Go 账号不存在")
    try:
        result = await _import_opencode_go_account_to_cpa(account, cpa_admin_client())
    except CpaAdminError as exc:
        db.add_log("error", "opencode-go", f"{account['name']} 导入 CPA 失败: {exc}")
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=exc.status_code)
    db.add_log("info", "opencode-go", f"{account['name']} 已导入 CPA: {result['name']}，模型 {result['model_count']} 个")
    return {"ok": True, **result}


@app.post("/api/opencode-go/accounts/import-cpa")
async def api_bulk_import_opencode_go_to_cpa(request: Request):
    require_user(request)
    payload = await request.json()
    try:
        account_ids = _opencode_go_account_ids_payload(payload)
        client = cpa_admin_client()
    except (ValueError, CpaAdminError) as exc:
        status_code = exc.status_code if isinstance(exc, CpaAdminError) else 400
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=status_code)

    imported = []
    failed = []
    for account_id in account_ids:
        account = db.get_opencode_go_account(account_id)
        if not account:
            failed.append({"id": account_id, "message": "OpenCode Go 账号不存在"})
            continue
        email = decrypt_value(account["email_enc"], db.secret_key) if account["email_enc"] else ""
        try:
            result = await _import_opencode_go_account_to_cpa(account, client)
        except CpaAdminError as exc:
            failed.append({"id": account_id, "email": email, "message": str(exc)})
            db.add_log("error", "opencode-go", f"{account['name']} 批量导入 CPA 失败: {exc}")
            continue
        imported.append(result)
        db.add_log("info", "opencode-go", f"{account['name']} 已批量导入 CPA: {result['name']}，模型 {result['model_count']} 个")
    return {
        "ok": True,
        "count": len(imported),
        "failed_count": len(failed),
        "failedCount": len(failed),
        "imported": imported,
        "failed": failed,
    }


@app.get("/api/opencode-go/import-logs")
async def api_opencode_go_import_logs(request: Request):
    require_user(request)
    page = _positive_query_int(request.query_params.get("page"), 1)
    page_size = _positive_query_int(request.query_params.get("page_size") or request.query_params.get("pageSize"), LOG_PAGE_SIZE)
    return log_page_payload(page, page_size, category="opencode-go", message_query="导入")


@app.post("/api/opencode-go/accounts/{account_id}/login")
async def api_login_opencode_go_account(request: Request, account_id: int):
    require_user(request)
    account = db.get_opencode_go_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="OpenCode Go 账号不存在")
    message = "已取消内置浏览器登录，请在“导入登录态”弹窗中打开本地浏览器登录页，并导入登录态 JSON 或 Cookie"
    result = {"is_valid": False, "invalid_message": message}
    db.update_opencode_go_result(account_id, result)
    db.add_log("warning", "opencode-go", f"{account['name']} OpenCode Go 内置浏览器登录已停用")
    return JSONResponse({"ok": False, "message": message, "result": _public_opencode_go_result(result)}, status_code=400)


@app.post("/api/opencode-go/accounts/{account_id}/session")
async def api_import_opencode_go_session(request: Request, account_id: int):
    require_user(request)
    account = db.get_opencode_go_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="OpenCode Go 账号不存在")
    payload = await request.json()
    try:
        storage_state, workspace_id = _opencode_go_session_payload(payload)
    except ValueError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
    db.update_opencode_go_session(account_id, storage_state, workspace_id=workspace_id, status="logged_in")
    updated = db.get_opencode_go_account(account_id)
    db.add_log("info", "opencode-go", f"API 导入 OpenCode Go 登录态: {account['name']}")
    return {"ok": True, "account": public_opencode_go_account(updated)}


@app.get("/api/opencode-go/accounts/{account_id}/session")
async def api_opencode_go_session(request: Request, account_id: int):
    require_user(request)
    account = db.get_opencode_go_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="OpenCode Go 账号不存在")
    storage_state_enc = account["storage_state_enc"]
    if not storage_state_enc:
        raise HTTPException(status_code=404, detail="尚未保存登录态")
    storage_state_text = decrypt_value(storage_state_enc, db.secret_key) or ""
    workspace_id = str(account["workspace_id"] or "")
    try:
        storage_state_value = json.loads(storage_state_text)
        if isinstance(storage_state_value, dict):
            workspace_id = workspace_id or str(storage_state_value.get("workspace_id") or storage_state_value.get("workspaceId") or "")
        storage_state_text = json.dumps(storage_state_value, ensure_ascii=False, indent=2)
    except json.JSONDecodeError:
        pass
    db.add_log("info", "opencode-go", f"API 读取 OpenCode Go 登录态用于编辑: {account['name']}")
    return {
        "workspace_id": workspace_id,
        "workspaceId": workspace_id,
        "storage_state": storage_state_text,
        "storageState": storage_state_text,
    }


@app.post("/api/opencode-go/accounts/{account_id}/refresh")
async def api_refresh_opencode_go_account(request: Request, account_id: int):
    require_user(request)
    if not db.get_opencode_go_account(account_id):
        raise HTTPException(status_code=404, detail="OpenCode Go 账号不存在")
    result = await query_opencode_go_for_account(db, account_id)
    updated = db.get_opencode_go_account(account_id)
    if not result.get("is_valid"):
        return JSONResponse(
            {"ok": False, "message": result.get("invalid_message") or "刷新失败", "result": _public_opencode_go_result(result), "account": public_opencode_go_account(updated)},
            status_code=400,
        )
    return {"ok": True, "result": _public_opencode_go_result(result), "account": public_opencode_go_account(updated)}


@app.post("/api/opencode-go/query-all")
async def api_query_all_opencode_go(request: Request):
    require_user(request)
    db.add_log("info", "opencode-go", "API 手动刷新全部 OpenCode Go 账号")
    return {"results": [_public_opencode_go_result(result) for result in await query_all_opencode_go_accounts(db)]}


@app.get("/api/opencode-go/accounts/{account_id}/history")
async def api_opencode_go_history(request: Request, account_id: int):
    require_user(request)
    account = db.get_opencode_go_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="OpenCode Go 账号不存在")
    limit = _positive_query_int(request.query_params.get("limit"), 100)
    return {
        "account": public_opencode_go_account(account),
        "records": [public_opencode_go_history(row) for row in db.list_opencode_go_history(account_id, limit=limit)],
    }


@app.get("/api/opencode-go/accounts/{account_id}/api-key")
async def api_opencode_go_api_key(request: Request, account_id: int):
    require_user(request)
    account = db.get_opencode_go_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="OpenCode Go 账号不存在")
    data = public_opencode_go_account(account, include_api_key=True)
    if not data.get("api_key"):
        raise HTTPException(status_code=404, detail="尚未获取 API key")
    db.add_log("info", "opencode-go", f"API 读取 OpenCode Go API key: {account['name']}")
    return {"api_key": data["api_key"], "apiKey": data["api_key"], "api_key_masked": data["api_key_masked"], "apiKeyMasked": data["api_key_masked"]}


@app.get("/api/opencode-go/accounts/{account_id}/password")
async def api_opencode_go_password(request: Request, account_id: int):
    require_user(request)
    account = db.get_opencode_go_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="OpenCode Go 账号不存在")
    password = decrypt_value(account["password_enc"], db.secret_key) if account["password_enc"] else ""
    if not password:
        raise HTTPException(status_code=404, detail="尚未保存 Google 密码")
    db.add_log("info", "opencode-go", f"API 读取 OpenCode Go 密码用于复制: {account['name']}")
    return {"password": password}


@app.get("/api/opencode-go-grabber/extension.zip")
async def api_download_opencode_go_grabber(request: Request):
    """下载定制后的 OpenCode Go Grabber 浏览器扩展 zip。

    manifest 中的 content_scripts.matches / externally_connectable.matches / host_permissions
    会按当前部署域名（request.base_url）烘焙好，选项页默认 app 地址也一并填入。
    """
    user = require_user(request)
    try:
        body, filename = build_extension_zip(request.base_url)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    db.add_log("info", "opencode-go", f"{user} 下载 OpenCode Go 抓取扩展")
    return Response(
        content=body,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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
        "cpa": public_cpa_settings(),
        "reminders": public_reminders(),
    }


@app.get("/api/settings/reminders")
async def api_reminders(request: Request):
    require_user(request)
    return {"reminders": public_reminders()}


@app.post("/api/settings/reminders")
async def api_create_reminder(request: Request):
    require_user(request)
    payload = await request.json()
    try:
        data = _reminder_payload(payload)
    except ValueError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
    reminder_id = db.create_reminder(data["title"], data["content"], data["remind_at"])
    scheduler.notify_settings_changed()
    db.add_log("info", "reminder", f"API 新增定时提醒: {data['title']}")
    return {"ok": True, "reminder": reminder_to_dict(db.get_reminder(reminder_id))}


@app.put("/api/settings/reminders/{reminder_id}")
async def api_update_reminder(request: Request, reminder_id: int):
    require_user(request)
    if not db.get_reminder(reminder_id):
        raise HTTPException(status_code=404, detail="提醒事项不存在")
    payload = await request.json()
    try:
        data = _reminder_payload(payload)
    except ValueError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
    reminder = db.update_reminder(reminder_id, data["title"], data["content"], data["remind_at"])
    scheduler.notify_settings_changed()
    db.add_log("info", "reminder", f"API 更新定时提醒: {data['title']}")
    return {"ok": True, "reminder": reminder_to_dict(reminder)}


@app.delete("/api/settings/reminders/{reminder_id}")
async def api_delete_reminder(request: Request, reminder_id: int):
    require_user(request)
    reminder = db.get_reminder(reminder_id)
    if not reminder:
        raise HTTPException(status_code=404, detail="提醒事项不存在")
    db.delete_reminder(reminder_id)
    scheduler.notify_settings_changed()
    db.add_log("warning", "reminder", f"API 删除定时提醒: {reminder['title']}")
    return {"ok": True}


@app.post("/api/settings/general")
async def api_general_settings(request: Request):
    require_user(request)
    payload = await request.json()
    db.update_general_settings(
        float(payload.get("request_timeout", REQUEST_TIMEOUT_SECONDS)),
        int(payload.get("query_interval", BALANCE_QUERY_INTERVAL_SECONDS)),
        float(payload.get("default_threshold", 5)),
        int(payload.get("group_rate_query_interval", payload.get("groupRateQueryInterval", GROUP_RATE_QUERY_INTERVAL_SECONDS))),
        _optional_bool_payload(payload, "monitor_paused", "monitorPaused"),
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
    if admin_key:
        db.set_setting(SUB2API_ADMIN_KEY_SETTING, encrypt_value(admin_key, db.secret_key) or "")
    db.set_setting(SUB2API_SITE_URL_SETTING, site_url)
    db.add_log("info", "settings", "API 更新 Sub2API 配置")
    return {"ok": True, "sub2api": public_sub2api_settings()}


@app.post("/api/settings/cpa")
async def api_cpa_settings(request: Request):
    require_user(request)
    payload = await request.json()
    try:
        authorization = normalize_cpa_authorization(
            payload.get("authorization", payload.get("Authorization", payload.get("token", "")))
        )
        site_url = normalize_cpa_management_url(payload.get("site_url", payload.get("siteUrl", "")))
    except CpaAdminError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=exc.status_code)
    if authorization:
        db.set_setting(CPA_AUTHORIZATION_SETTING, encrypt_value(authorization, db.secret_key) or "")
    db.set_setting(CPA_SITE_URL_SETTING, site_url)
    db.add_log("info", "settings", "API 更新 CPA 配置")
    return {"ok": True, "cpa": public_cpa_settings()}


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


def _opencode_go_account_payload(payload: dict[str, Any], require_password: bool = False) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("请求内容格式不正确")
    email = str(payload.get("email") or "").strip()
    password = str(payload.get("password") or "").strip()
    if not email:
        raise ValueError("Google 邮箱不能为空")
    if require_password and not password:
        raise ValueError("Google 密码不能为空")
    data = {
        "email": email,
        "recovery_email": str(payload.get("recovery_email") or payload.get("recoveryEmail") or "").strip(),
        "is_enabled": _to_bool(payload.get("is_enabled", payload.get("isEnabled", payload.get("enabled", False)))),
    }
    if password:
        data["password"] = password
    workspace_id = str(payload.get("workspace_id") or payload.get("workspaceId") or "").strip()
    if workspace_id:
        data["workspace_id"] = workspace_id
    return data


def import_bulk_opencode_go_accounts(bulk_text: str) -> dict[str, Any]:
    text = str(bulk_text or "").strip()
    if not text:
        raise ValueError("请输入要导入的账号")
    accounts = []
    errors = []
    seen_emails: set[str] = set()
    for line_number, line in enumerate(text.splitlines(), start=1):
        raw_line = line.strip()
        if not raw_line:
            continue
        parts = [part.strip() for part in raw_line.split("|")]
        if len(parts) not in {2, 3}:
            errors.append(f"第 {line_number} 行格式错误，请使用 账号|密码 或 账号|密码|恢复电子邮件")
            continue
        email = parts[0]
        password = parts[1]
        recovery_email = parts[2] if len(parts) == 3 else ""
        if not email:
            errors.append(f"第 {line_number} 行缺少账号")
            continue
        if not password:
            errors.append(f"第 {line_number} 行缺少密码")
            continue
        normalized_email = email.lower()
        if normalized_email in seen_emails:
            errors.append(f"第 {line_number} 行账号重复: {email}")
            continue
        seen_emails.add(normalized_email)
        accounts.append({"email": email, "password": password, "recovery_email": recovery_email, "is_enabled": False})
    if errors:
        raise ValueError("；".join(errors[:5]) + ("；..." if len(errors) > 5 else ""))
    if not accounts:
        raise ValueError("没有可导入的账号")
    imported = []
    for item in accounts:
        account_id = db.upsert_opencode_go_account(item)
        account = db.get_opencode_go_account(account_id)
        if account:
            imported.append(public_opencode_go_account(account))
    return {"count": len(imported), "accounts": imported}


def _opencode_go_session_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
    if not isinstance(payload, dict):
        raise ValueError("请求内容格式不正确")
    raw_state = (
        payload.get("storage_state")
        or payload.get("storageState")
        or payload.get("storage_state_json")
        or payload.get("storageStateJson")
        or payload.get("json")
    )
    raw_cookies = payload.get("cookies")
    raw_cookie_header = payload.get("cookie") or payload.get("cookie_header") or payload.get("cookieHeader")
    if isinstance(raw_state, str):
        raw_state = raw_state.strip()
        if not raw_state:
            raise ValueError("请输入登录态 JSON 或 Cookie")
        try:
            storage_state = json.loads(raw_state)
        except json.JSONDecodeError as exc:
            if "=" not in raw_state:
                raise ValueError("登录态 JSON 格式不正确") from exc
            storage_state = {"cookies": _cookies_from_header(raw_state), "origins": []}
    elif isinstance(raw_state, dict):
        storage_state = raw_state
    elif isinstance(raw_state, list):
        storage_state = {"cookies": raw_state, "origins": []}
    elif isinstance(raw_cookies, list):
        storage_state = {"cookies": raw_cookies, "origins": []}
    elif isinstance(raw_cookie_header, str) and raw_cookie_header.strip():
        storage_state = {"cookies": _cookies_from_header(raw_cookie_header), "origins": []}
    else:
        raise ValueError("请输入登录态 JSON 或 Cookie")
    if isinstance(storage_state, dict):
        raw_state_dict = raw_state if isinstance(raw_state, dict) else storage_state
        nested_state = raw_state_dict.get("storage_state") or raw_state_dict.get("storageState")
        if isinstance(nested_state, dict):
            for key, value in raw_state_dict.items():
                if key not in nested_state and key not in {"storage_state", "storageState"}:
                    nested_state[key] = value
            storage_state = nested_state
            raw_state_dict = storage_state
        for key in ("serverIds", "server_ids", "serverReferences", "server_references", "serverId", "serverID", "server_id"):
            if key in raw_state_dict and key not in storage_state:
                storage_state[key] = raw_state_dict[key]
    cookies = storage_state.get("cookies")
    origins = storage_state.get("origins", [])
    if not isinstance(cookies, list) or not isinstance(origins, list):
        raise ValueError("登录态必须包含 cookies 和 origins 数组")
    if not cookies:
        raise ValueError("登录态 cookies 为空")
    if not _has_cookie(cookies, "auth"):
        raise ValueError("登录态必须包含 auth Cookie，请从 DevTools 的 Application 或 Network 请求头复制 auth=...")
    workspace_id = str(
        payload.get("workspace_id")
        or payload.get("workspaceId")
        or (storage_state.get("workspace_id") if isinstance(storage_state, dict) else "")
        or (storage_state.get("workspaceId") if isinstance(storage_state, dict) else "")
        or ""
    ).strip()
    if not workspace_id:
        raise ValueError("Workspace ID 不能为空")
    return storage_state, workspace_id


def _has_cookie(cookies: list[Any], name: str) -> bool:
    for item in cookies:
        if not isinstance(item, dict):
            continue
        if str(item.get("name") or "").strip() == name and str(item.get("value") or ""):
            return True
    return False


def _cookies_from_header(value: str) -> list[dict[str, str]]:
    value = value.strip()
    if value.lower().startswith("cookie:"):
        value = value.split(":", 1)[1].strip()
    cookies = []
    for part in value.split(";"):
        if "=" not in part:
            continue
        name, cookie_value = part.split("=", 1)
        name = name.strip()
        if not name:
            continue
        cookies.append(
            {
                "name": name,
                "value": cookie_value.strip(),
                "domain": ".opencode.ai",
                "path": "/",
            }
        )
    if not cookies:
        raise ValueError("Cookie 内容格式不正确")
    return cookies


def _public_opencode_go_result(result: dict[str, Any]) -> dict[str, Any]:
    public = {
        key: value
        for key, value in result.items()
        if key
        not in {
            "api_key",
            "apiKey",
            "storage_state",
            "storageState",
            "raw",
            "raw_json",
            "rawJson",
            "rollingUsage",
            "weeklyUsage",
            "monthlyUsage",
            "rolling_usage",
            "weekly_usage",
            "monthly_usage",
        }
    }
    public["rolling_usage"] = public_usage_window(result.get("rolling_usage") or result.get("rollingUsage"))
    public["weekly_usage"] = public_usage_window(result.get("weekly_usage") or result.get("weeklyUsage"))
    public["monthly_usage"] = public_usage_window(result.get("monthly_usage") or result.get("monthlyUsage"))
    if result.get("api_key") or result.get("apiKey"):
        masked = result.get("api_key_masked") or result.get("apiKeyMasked") or mask_api_key(result.get("api_key") or result.get("apiKey"))
        public["api_key_masked"] = masked
        public["apiKeyMasked"] = masked
    return public


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
    masked = mask_api_key(admin_key) if admin_key else ""
    return {
        "site_url": site_url,
        "siteUrl": site_url,
        "has_admin_key": bool(admin_key),
        "hasAdminKey": bool(admin_key),
        "admin_key_masked": masked,
        "adminKeyMasked": masked,
    }


def public_cpa_settings() -> dict[str, Any]:
    authorization_enc = db.get_setting(CPA_AUTHORIZATION_SETTING, "")
    authorization = decrypt_value(authorization_enc, db.secret_key) if authorization_enc else ""
    site_url = db.get_setting(CPA_SITE_URL_SETTING, "")
    masked = mask_api_key(authorization) if authorization else ""
    return {
        "site_url": site_url,
        "siteUrl": site_url,
        "has_authorization": bool(authorization),
        "hasAuthorization": bool(authorization),
        "authorization_masked": masked,
        "authorizationMasked": masked,
    }


def sub2api_admin_client() -> Sub2ApiAdminClient:
    admin_key_enc = db.get_setting(SUB2API_ADMIN_KEY_SETTING, "")
    admin_key = decrypt_value(admin_key_enc, db.secret_key) if admin_key_enc else ""
    site_url = db.get_setting(SUB2API_SITE_URL_SETTING, "")
    timeout = db.get_general_settings().get("request_timeout", REQUEST_TIMEOUT_SECONDS)
    return Sub2ApiAdminClient(site_url, admin_key, timeout=float(timeout or REQUEST_TIMEOUT_SECONDS))


def cpa_admin_client() -> CpaAdminClient:
    return cpa_admin_client_from_db(db, timeout_default=REQUEST_TIMEOUT_SECONDS)


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


def _opencode_go_account_ids_payload(payload: Any) -> list[int]:
    if not isinstance(payload, dict):
        raise ValueError("请求内容格式不正确")
    raw = payload.get("account_ids", payload.get("accountIds", payload.get("ids", [])))
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise ValueError("账号 ID 必须是数组")
    account_ids: list[int] = []
    seen = set()
    for item in raw:
        try:
            account_id = int(item)
        except (TypeError, ValueError) as exc:
            raise ValueError("账号 ID 格式不正确") from exc
        if account_id <= 0:
            raise ValueError("账号 ID 格式不正确")
        if account_id in seen:
            continue
        seen.add(account_id)
        account_ids.append(account_id)
    if not account_ids:
        raise ValueError("请选择至少一个 OpenCode Go 账号")
    return account_ids


async def _import_opencode_go_account_to_cpa(account: Any, client: CpaAdminClient) -> dict[str, Any]:
    email = decrypt_value(account["email_enc"], db.secret_key) if account["email_enc"] else ""
    api_key = decrypt_value(account["api_key_enc"], db.secret_key) if account["api_key_enc"] else ""
    result = await client.import_opencode_go_account(email, api_key)
    db.update_opencode_go_cpa_state(
        int(account["id"]),
        provider_disabled=False,
        provider_deleted=False,
        clear_deleted_at=True,
        reenable_pending=False,
        action_at=utc_now(),
        clear_error=True,
    )
    return result


def _opencode_go_cpa_auto_delete_enabled() -> bool:
    return _to_bool(db.get_setting(OPENCODE_GO_CPA_AUTO_DELETE_SETTING, "0"))


def public_reminders() -> list[dict[str, Any]]:
    return [reminder_to_dict(row) for row in db.list_reminders()]


def _normalize_sub2api_site_url(value: Any) -> str:
    text = str(value or "").strip().rstrip("/")
    if not text:
        return ""
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("站点地址必须是 http 或 https URL")
    return text


def _reminder_payload(payload: Any) -> dict[str, str]:
    if not isinstance(payload, dict):
        raise ValueError("请求内容格式不正确")
    title = str(payload.get("title") or "").strip()
    content = str(payload.get("content") or "").strip()
    remind_at = _parse_reminder_time(payload.get("remind_at", payload.get("remindAt")))
    if not title:
        raise ValueError("标题不能为空")
    if not content:
        raise ValueError("内容不能为空")
    return {"title": title, "content": content, "remind_at": remind_at}


def _parse_reminder_time(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("提醒时间不能为空")
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("提醒时间格式不正确") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CHINA_TZ)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


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


def _split_group_ids(value: Any) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    values = [text]
    for separator in ("|", ";", "\n"):
        values = [part for value in values for part in value.split(separator)]
    return [value.strip() for value in values if value.strip()]

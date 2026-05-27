from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeTimedSerializer

from app.config import get_config, uses_default_app_secret
from app.models import BALANCE_QUERY_INTERVAL_SECONDS, GROUP_RATE_QUERY_INTERVAL_SECONDS, Database, format_china_time, row_to_dict
from app.security import decrypt_value
from app.security import verify_password
from app.services.balance import query_newapi_group_options
from app.services.emailer import send_email
from app.services.scheduler import BalanceScheduler, query_all_accounts, query_group_rate_for_account, query_one_account, query_sub2api_group_for_account

config = get_config()
db = Database(config.database_path, config.app_secret_key)
templates = Jinja2Templates(directory="app/templates")
serializer = URLSafeTimedSerializer(config.app_secret_key, salt="balance-monitor-session")
scheduler = BalanceScheduler(db)
SENSITIVE_HEADER_NAMES = {"authorization", "cookie", "set-cookie"}
SENSITIVE_FIELD_NAMES = {
    "api_key",
    "apiKey",
    "key_id",
    "keyId",
    "access_token",
    "accessToken",
    "password",
    "email",
    "token",
    "secret",
    "app_secret_key",
}
LOG_VALUE_LIMIT = 2000
LOG_PAGE_SIZE = 50
LOG_MAX_PAGE_SIZE = 200


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


def public_log(row: Any) -> dict[str, Any]:
    data = row_to_dict(row)
    data["created_at_formatted"] = format_china_time(data.get("created_at"))
    return data


def log_page_payload(page: int, page_size: int) -> dict[str, Any]:
    page = max(1, page)
    page_size = min(max(1, page_size), LOG_MAX_PAGE_SIZE)
    total = db.count_logs()
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, total_pages)
    logs = [public_log(row) for row in db.list_logs(limit=page_size, offset=(page - 1) * page_size)]
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
    data["is_eliminated"] = bool(data.get("is_eliminated"))
    key_id_enc = data.pop("key_id_enc", None)
    data["has_key_id"] = bool(key_id_enc)
    selected_group_id = decrypt_value(key_id_enc, config.app_secret_key) if data["platform"] == "newApi" and key_id_enc else None
    data["selected_group_id"] = selected_group_id
    data["has_api_key"] = bool(data.pop("api_key_enc", None))
    data["has_email"] = bool(data.pop("email_enc", None))
    data["has_password"] = bool(data.pop("password_enc", None))
    data["has_access_token"] = bool(data.pop("access_token_enc", None))
    data["has_user_id"] = bool(data.pop("user_id_enc", None))
    data["group_rates"] = group_rates_from_extra(data.get("last_extra"))
    if data["platform"] == "newApi" and selected_group_id and not data["group_rates"]:
        data["group_rates"] = [{"plan_name": f"当前分组 {selected_group_id}", "rate_multiplier": None}]
    return data


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


def newapi_group_result_from_selection(group_id: str, group: dict[str, Any]) -> dict[str, Any]:
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
        "platform": "newApi",
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


def grouped_accounts(eliminated_last: bool = False) -> dict[str, list[dict[str, Any]]]:
    grouped = {"newApi": [], "sub2Api": []}
    for row in db.list_accounts():
        grouped[row["platform"]].append(public_account(row))
    if eliminated_last:
        for accounts in grouped.values():
            accounts.sort(key=lambda account: (1 if account.get("is_eliminated") else 0, str(account.get("name") or "").lower()))
    return grouped


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
    if row["platform"] == "newApi":
        data["key_id"] = decrypt_value(row["key_id_enc"], config.app_secret_key) or ""
        data["user_id"] = decrypt_value(row["user_id_enc"], config.app_secret_key) or ""
    return data


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    redirect = redirect_if_needed(request)
    if redirect:
        return redirect
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        template_context(request, grouped=grouped_accounts(eliminated_last=True), settings=db.get_general_settings()),
    )


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    message = request.query_params.get("message")
    return templates.TemplateResponse(request, "login.html", template_context(request, error=None, message=message))


@app.post("/login")
async def login(request: Request):
    form = await request.form()
    username = str(form.get("username", ""))
    password = str(form.get("password", ""))
    user = db.get_user(username)
    if not user or not verify_password(password, user["password_hash"]):
        db.add_log("warning", "auth", f"登录失败: {username or '-'}")
        return templates.TemplateResponse(
            request,
            "login.html",
            template_context(request, error="用户名或密码错误"),
            status_code=401,
        )
    db.add_log("info", "auth", f"登录成功: {username}")
    token = serializer.dumps({"username": username, "session_version": int(user["session_version"])})
    response = RedirectResponse("/", status_code=303)
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
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(config.session_cookie)
    return response


@app.get("/accounts", response_class=HTMLResponse)
async def accounts_page(request: Request):
    redirect = redirect_if_needed(request)
    if redirect:
        return redirect
    edit_id = _optional_int(request.query_params.get("edit_id"))
    return templates.TemplateResponse(
        request,
        "accounts.html",
        template_context(
            request,
            grouped=grouped_accounts(),
            settings=db.get_general_settings(),
            message=None,
            edit_account=public_edit_account(edit_id),
        ),
    )


@app.post("/accounts", response_class=HTMLResponse)
async def save_account_form(request: Request):
    redirect = redirect_if_needed(request)
    if redirect:
        return redirect
    form = await request.form()
    account_data = _account_from_form(form)
    account_id = _optional_int(form.get("account_id"))
    if account_id:
        try:
            db.update_account(account_id, account_data)
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
                ),
                status_code=404,
            )
        db.add_log("info", "account", f"编辑账号: {account_data['platform']} / {account_data['name']}")
    else:
        db.upsert_account(account_data)
        db.add_log("info", "account", f"保存账号: {account_data['platform']} / {account_data['name']}")
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


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    redirect = redirect_if_needed(request)
    if redirect:
        return redirect
    return templates.TemplateResponse(
        request,
        "settings.html",
        template_context(request, settings=db.get_general_settings(), smtp=public_smtp_settings(), message=None),
    )


@app.get("/accounts/{account_id}/group-rates", response_class=HTMLResponse)
async def group_rates_page(request: Request, account_id: int):
    redirect = redirect_if_needed(request)
    if redirect:
        return redirect
    account = db.get_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="账号不存在")
    return templates.TemplateResponse(
        request,
        "group_rates.html",
        template_context(
            request,
            account=public_account(account),
            records=[row_to_dict(row) for row in db.list_group_rate_records(account_id)],
        ),
    )


@app.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request):
    redirect = redirect_if_needed(request)
    if redirect:
        return redirect
    payload = log_page_payload(
        _positive_query_int(request.query_params.get("page"), 1),
        _positive_query_int(request.query_params.get("page_size"), LOG_PAGE_SIZE),
    )
    return templates.TemplateResponse(
        request,
        "logs.html",
        template_context(request, logs=payload["logs"], pagination=payload["pagination"]),
    )


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
        float(form.get("request_timeout") or 15),
        int(float(form.get("query_interval") or BALANCE_QUERY_INTERVAL_SECONDS)),
        float(form.get("default_threshold") or 5),
        int(float(form.get("group_rate_query_interval") or GROUP_RATE_QUERY_INTERVAL_SECONDS)),
    )
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
    return grouped_accounts()


@app.get("/api/logs")
async def api_logs(request: Request):
    require_user(request)
    return log_page_payload(
        _positive_query_int(request.query_params.get("page"), 1),
        _positive_query_int(request.query_params.get("page_size"), LOG_PAGE_SIZE),
    )


@app.get("/api/accounts/{account_id}/group-rates")
async def api_group_rates(request: Request, account_id: int):
    require_user(request)
    account = db.get_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="账号不存在")
    return {
        "account": public_account(account),
        "records": [row_to_dict(row) for row in db.list_group_rate_records(account_id)],
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
    if isinstance(payload, dict):
        if "changed" in payload:
            changed = bool(payload.get("changed"))
        elif "group_rate_changed" in payload:
            changed = bool(payload.get("group_rate_changed"))
        elif "groupRateChanged" in payload:
            changed = bool(payload.get("groupRateChanged"))
    db.update_account_group_rate_change_status(account_id, changed)
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


@app.post("/api/accounts")
async def api_create_account(request: Request):
    require_user(request)
    payload = await request.json()
    account_id = db.upsert_account(_account_from_payload(payload))
    db.add_log("info", "account", f"API 保存账号: {payload.get('platform')} / {payload.get('name')}")
    return {"id": account_id, "ok": True}


@app.put("/api/accounts/{account_id}")
async def api_update_account(request: Request, account_id: int):
    require_user(request)
    payload = await request.json()
    try:
        db.update_account(account_id, _account_from_payload(payload))
    except ValueError:
        raise HTTPException(status_code=404, detail="账号不存在") from None
    db.add_log("info", "account", f"API 更新账号: {payload.get('platform')} / {payload.get('name')}")
    return {"id": account_id, "ok": True}


@app.post("/api/accounts/bulk")
async def api_bulk_accounts(request: Request):
    require_user(request)
    payload = await request.json()
    platform = payload.get("platform")
    accounts = payload.get("accounts", [])
    if platform not in {"newApi", "sub2Api"}:
        raise HTTPException(status_code=400, detail="platform 必须是 newApi 或 sub2Api")
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
    return result


@app.post("/api/accounts/{account_id}/selected-group")
async def api_select_account_group(request: Request, account_id: int):
    require_user(request)
    account = db.get_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="账号不存在")
    if account["platform"] != "newApi":
        raise HTTPException(status_code=400, detail="仅支持 newApi")
    payload = await request.json()
    group_id = str(payload.get("group_id") or payload.get("groupId") or "").strip()
    if not group_id:
        raise HTTPException(status_code=400, detail="请选择分组")
    group = payload.get("group")
    group_result = newapi_group_result_from_selection(group_id, group) if isinstance(group, dict) else None
    db.update_account_selected_group(account_id, group_id)
    db.update_account_group_result(account_id, group_result or {"extra": None})
    updated = db.get_account(account_id)
    db.add_log("info", "account", f"newApi / {account['name']} 选择分组: {group_id}")
    return {"ok": True, "account": public_account(updated), "group_result": group_result}


@app.post("/api/query-all")
async def api_query_all(request: Request):
    require_user(request)
    db.add_log("info", "query", "API 手动查询全部账号")
    return {"results": await query_all_accounts(db)}


@app.post("/api/settings/general")
async def api_general_settings(request: Request):
    require_user(request)
    payload = await request.json()
    db.update_general_settings(
        float(payload.get("request_timeout", 15)),
        int(payload.get("query_interval", BALANCE_QUERY_INTERVAL_SECONDS)),
        float(payload.get("default_threshold", 5)),
        int(payload.get("group_rate_query_interval", payload.get("groupRateQueryInterval", GROUP_RATE_QUERY_INTERVAL_SECONDS))),
    )
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


def _account_from_form(form: Any) -> dict[str, Any]:
    return _account_from_payload(
        {
            "platform": form.get("platform"),
            "name": form.get("name"),
            "base_url": form.get("base_url"),
            "note": form.get("note"),
            "recharge_url": form.get("recharge_url"),
            "key_id": form.get("key_id"),
            "api_key": form.get("api_key"),
            "email": form.get("email"),
            "password": form.get("password"),
            "access_token": form.get("access_token"),
            "user_id": form.get("user_id"),
            "threshold": form.get("threshold"),
            "is_enabled": form.get("is_enabled") == "on",
        }
    )


def _account_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    platform = payload.get("platform")
    if platform not in {"newApi", "sub2Api"}:
        raise HTTPException(status_code=400, detail="platform 必须是 newApi 或 sub2Api")
    name = str(payload.get("name", "")).strip()
    base_url = str(payload.get("base_url") or payload.get("baseUrl") or "").strip()
    if not name or not base_url:
        raise HTTPException(status_code=400, detail="name 和 baseUrl 必填")
    is_eliminated = None
    if "is_eliminated" in payload:
        is_eliminated = _to_bool(payload.get("is_eliminated"))
    elif "isEliminated" in payload:
        is_eliminated = _to_bool(payload.get("isEliminated"))
    elif "eliminated" in payload:
        is_eliminated = _to_bool(payload.get("eliminated"))
    return {
        "platform": platform,
        "name": name,
        "base_url": base_url,
        "note": payload.get("note") or payload.get("remark") or payload.get("remarks") or "",
        "recharge_url": payload.get("recharge_url") or payload.get("rechargeUrl") or "",
        "key_id": payload.get("key_id") or payload.get("keyId"),
        "api_key": payload.get("api_key") or payload.get("apiKey"),
        "email": payload.get("email"),
        "password": payload.get("password"),
        "access_token": payload.get("access_token") or payload.get("accessToken"),
        "user_id": payload.get("user_id") or payload.get("userId"),
        "threshold": payload.get("threshold"),
        "is_enabled": _to_bool(payload.get("is_enabled", payload.get("isEnabled", True))),
        "is_eliminated": is_eliminated,
    }


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
                    items.append(
                        {
                            "name": parts[0],
                            "base_url": parts[1],
                            "key_id": parts[2],
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
                    items.append(
                        {
                            "name": parts[0],
                            "base_url": parts[1],
                            "key_id": parts[2],
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


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() not in {"0", "false", "no", "off", ""}


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

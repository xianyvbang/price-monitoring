from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeSerializer

from app.config import get_config
from app.models import Database, row_to_dict
from app.security import verify_password
from app.services.emailer import send_email
from app.services.scheduler import BalanceScheduler, query_all_accounts, query_one_account

config = get_config()
db = Database(config.database_path, config.app_secret_key)
templates = Jinja2Templates(directory="app/templates")
serializer = URLSafeSerializer(config.app_secret_key, salt="balance-monitor-session")
scheduler = BalanceScheduler(db)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    db.ensure_admin(config.admin_username, config.admin_password)
    scheduler.start()
    yield
    await scheduler.stop()


app = FastAPI(title="余额监控", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.get("/health")
async def health():
    return {"ok": True}


def current_user(request: Request) -> str | None:
    token = request.cookies.get(config.session_cookie)
    if not token:
        return None
    try:
        payload = serializer.loads(token)
    except BadSignature:
        return None
    return payload.get("username")


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
    return {"request": request, "user": current_user(request), **extra}


def public_account(row: Any) -> dict[str, Any]:
    data = row_to_dict(row)
    data["has_api_key"] = bool(data.pop("api_key_enc", None))
    data["has_access_token"] = bool(data.pop("access_token_enc", None))
    data["has_user_id"] = bool(data.pop("user_id_enc", None))
    return data


def grouped_accounts() -> dict[str, list[dict[str, Any]]]:
    grouped = {"newApi": [], "sub2Api": []}
    for row in db.list_accounts():
        grouped[row["platform"]].append(public_account(row))
    return grouped


def public_edit_account(account_id: int | None) -> dict[str, Any] | None:
    if not account_id:
        return None
    row = db.get_account(account_id)
    if not row:
        return None
    return public_account(row)


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    redirect = redirect_if_needed(request)
    if redirect:
        return redirect
    return templates.TemplateResponse(
        "dashboard.html",
        template_context(request, grouped=grouped_accounts(), settings=db.get_general_settings()),
    )


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", template_context(request, error=None))


@app.post("/login")
async def login(request: Request):
    form = await request.form()
    username = str(form.get("username", ""))
    password = str(form.get("password", ""))
    user = db.get_user(username)
    if not user or not verify_password(password, user["password_hash"]):
        db.add_log("warning", "auth", f"登录失败: {username or '-'}")
        return templates.TemplateResponse(
            "login.html",
            template_context(request, error="用户名或密码错误"),
            status_code=401,
        )
    db.add_log("info", "auth", f"登录成功: {username}")
    token = serializer.dumps({"username": username})
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(config.session_cookie, token, httponly=True, samesite="lax")
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
        db.update_account(account_id, account_data)
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
        "settings.html",
        template_context(request, settings=db.get_general_settings(), smtp=public_smtp_settings(), message=None),
    )


@app.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request):
    redirect = redirect_if_needed(request)
    if redirect:
        return redirect
    return templates.TemplateResponse(
        "logs.html",
        template_context(request, logs=[row_to_dict(row) for row in db.list_logs()]),
    )


@app.post("/settings/general", response_class=HTMLResponse)
async def save_general_settings_form(request: Request):
    redirect = redirect_if_needed(request)
    if redirect:
        return redirect
    form = await request.form()
    db.update_general_settings(
        float(form.get("request_timeout") or 15),
        int(float(form.get("query_interval") or 30)),
        float(form.get("default_threshold") or 5),
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
    return {"logs": [row_to_dict(row) for row in db.list_logs()]}


@app.post("/api/accounts")
async def api_create_account(request: Request):
    require_user(request)
    payload = await request.json()
    account_id = db.upsert_account(_account_from_payload(payload))
    db.add_log("info", "account", f"API 保存账号: {payload.get('platform')} / {payload.get('name')}")
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
        int(payload.get("query_interval", 30)),
        float(payload.get("default_threshold", 5)),
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
            "api_key": form.get("api_key"),
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
    return {
        "platform": platform,
        "name": name,
        "base_url": base_url,
        "api_key": payload.get("api_key") or payload.get("apiKey"),
        "access_token": payload.get("access_token") or payload.get("accessToken"),
        "user_id": payload.get("user_id") or payload.get("userId"),
        "threshold": payload.get("threshold"),
        "is_enabled": _to_bool(payload.get("is_enabled", payload.get("isEnabled", True))),
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
            if platform == "sub2Api" and len(parts) >= 3:
                items.append({"name": parts[0], "base_url": parts[1], "api_key": parts[2], "threshold": parts[3] if len(parts) > 3 else None})
            if platform == "newApi" and len(parts) >= 4:
                items.append(
                    {
                        "name": parts[0],
                        "base_url": parts[1],
                        "access_token": parts[2],
                        "user_id": parts[3],
                        "threshold": parts[4] if len(parts) > 4 else None,
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

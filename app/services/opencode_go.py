from __future__ import annotations

import json
import re
import time
from typing import Any, Callable, Optional

import httpx

from app.models import utc_now
from app.security import decrypt_value

LogCallback = Callable[[str, str, str], None]

OPENCODE_BASE_URL = "https://opencode.ai"
OPENCODE_AUTH_URL = f"{OPENCODE_BASE_URL}/auth"
OPENCODE_GO_PATH = "/go"
OPENAUTH_BASE_URL = "https://auth.opencode.ai"
OPENAUTH_GOOGLE_AUTHORIZE_PATH = "/google/authorize"
OPENAUTH_GOOGLE_AUTHORIZE_URL = f"{OPENAUTH_BASE_URL}{OPENAUTH_GOOGLE_AUTHORIZE_PATH}"
SESSION_GET_REFERENCE_ID = "9bc4808361cdaee17059a8d3822b36ee8c9a0d93f1adc289fa1926998e3c9768"
LITE_SUBSCRIPTION_GET_REFERENCE_ID = "c7389bd0e731f80f49593e5ee53835475f4e28594dd6bd83eb229bab753498cd"
KEY_LIST_REFERENCE_IDS = (
    "def2ab20a296ef06465b1c3cf86da4ea983c0696e7a5708b9468aaed85083d6b",
    "c22cd964237ba79f2f9b95faa2a14b804f870d1bab49279463379cc6a0fd0c85",
)
SERVER_RUNTIME_PATTERN = re.compile(r'href="(?P<path>/_build/assets/server-runtime-[^"]+\.js)"')
LOGIN_TIMEOUT_MS = 90_000
QUERY_TIMEOUT_MS = 45_000


async def login_opencode_go_account(
    account: Any,
    secret_key: str,
    timeout: float,
    log: LogCallback | None = None,
) -> dict[str, Any]:
    email = decrypt_value(_account_value(account, "email_enc"), secret_key)
    password = decrypt_value(_account_value(account, "password_enc"), secret_key)
    if not email:
        return _invalid("缺少 Google 邮箱")
    if not password:
        return _invalid("缺少 Google 密码")
    try:
        return await _run_browser_login(email, password, timeout, log)
    except Exception as exc:
        return _invalid(_friendly_login_error(exc))


async def refresh_opencode_go_account(
    account: Any,
    secret_key: str,
    timeout: float,
    log: LogCallback | None = None,
) -> dict[str, Any]:
    storage_state = _decrypt_json(_account_value(account, "storage_state_enc"), secret_key)
    if not storage_state:
        login_result = await login_opencode_go_account(account, secret_key, timeout, log)
        if not login_result.get("is_valid"):
            return login_result
        storage_state = login_result.get("storage_state")
    try:
        result = await _run_browser_refresh(storage_state, _account_value(account, "workspace_id"), timeout, log)
    except Exception as exc:
        message = _friendly_refresh_error(exc)
        if _looks_like_session_error(message):
            login_result = await login_opencode_go_account(account, secret_key, timeout, log)
            if not login_result.get("is_valid"):
                return login_result
            try:
                result = await _run_browser_refresh(login_result.get("storage_state"), login_result.get("workspace_id"), timeout, log)
                result["storage_state"] = login_result.get("storage_state")
            except Exception as retry_exc:
                return _invalid(_friendly_refresh_error(retry_exc))
        else:
            return _invalid(message)
    result["checked_at"] = utc_now()
    return result


async def query_opencode_server_reference(
    client: httpx.AsyncClient,
    reference_id: str,
    args: list[Any],
    instance: str = "server-fn:0",
) -> Any:
    response = await client.post(
        f"{OPENCODE_BASE_URL}/_server",
        headers={
            "Content-Type": "application/json",
            "X-Server-Id": reference_id,
            "X-Server-Instance": instance,
        },
        json=args,
    )
    response.raise_for_status()
    if response.headers.get("X-Error"):
        raise RuntimeError(_response_text(response) or "OpenCode server reference returned an error")
    content_type = response.headers.get("content-type", "")
    if content_type.startswith("application/json"):
        return response.json()
    if content_type.startswith("text/plain"):
        return response.text
    return response.text


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


async def _run_browser_login(email: str, password: str, timeout: float, log: LogCallback | None) -> dict[str, Any]:
    playwright = _import_playwright()
    async with playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        try:
            page.set_default_timeout(min(LOGIN_TIMEOUT_MS, max(10_000, int(timeout * 1000))))
            await page.goto(OPENAUTH_GOOGLE_AUTHORIZE_URL, wait_until="domcontentloaded")
            await _fill_google_login(page, email, password)
            await _wait_for_opencode_login(page)
            storage_state = await context.storage_state()
            workspace_id = await _discover_workspace_id(page)
            _log(log, "info", "opencode-go", f"OpenCode Go 登录成功: {email}")
            return {
                "is_valid": True,
                "storage_state": storage_state,
                "workspace_id": workspace_id,
            }
        finally:
            await context.close()
            await browser.close()


async def _run_browser_refresh(
    storage_state: Any,
    workspace_id: str | None,
    timeout: float,
    log: LogCallback | None,
) -> dict[str, Any]:
    playwright = _import_playwright()
    async with playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(storage_state=storage_state or {})
        page = await context.new_page()
        try:
            page.set_default_timeout(min(QUERY_TIMEOUT_MS, max(10_000, int(timeout * 1000))))
            await page.goto(OPENCODE_BASE_URL, wait_until="domcontentloaded")
            runtime_path = await _discover_server_runtime_path(page)
            session = await _call_server_reference_in_page(page, runtime_path, SESSION_GET_REFERENCE_ID, [])
            workspace_id = workspace_id or _workspace_id_from_session(session) or await _discover_workspace_id(page)
            if not workspace_id:
                raise RuntimeError("未能识别 OpenCode workspace，请确认账号已登录并已创建 workspace")
            subscription = await _call_server_reference_in_page(page, runtime_path, LITE_SUBSCRIPTION_GET_REFERENCE_ID, [workspace_id])
            keys_payload = None
            key_errors = []
            for reference_id in KEY_LIST_REFERENCE_IDS:
                try:
                    keys_payload = await _call_server_reference_in_page(page, runtime_path, reference_id, [workspace_id])
                    break
                except Exception as exc:
                    key_errors.append(str(exc))
            if keys_payload is None:
                raise RuntimeError("OpenCode key.list 接口不可用，OpenCode 前端接口可能已更新: " + "; ".join(key_errors))
            result = normalize_usage_result(subscription, keys_payload, workspace_id, session)
            result["storage_state"] = await context.storage_state()
            _log(log, "info", "opencode-go", f"OpenCode Go 刷新成功: workspace={workspace_id}")
            return result
        finally:
            await context.close()
            await browser.close()


async def _fill_google_login(page: Any, email: str, password: str) -> None:
    await page.wait_for_load_state("domcontentloaded")
    await _wait_for_google_login_form(page)
    await _fill_first_available(page, ['input[type="email"]', 'input[name="identifier"]'], email)
    await _click_first_available(page, ['button:has-text("Next")', '#identifierNext button', 'button[jsname]'])
    await _fill_first_available(page, ['input[type="password"]', 'input[name="Passwd"]'], password)
    await _click_first_available(page, ['button:has-text("Next")', '#passwordNext button', 'button[jsname]'])


async def _wait_for_google_login_form(page: Any) -> None:
    started = time.monotonic()
    while time.monotonic() - started < LOGIN_TIMEOUT_MS / 1000:
        if await _has_any_selector(page, ['input[type="email"]', 'input[name="identifier"]'], timeout=500):
            return
        if "accounts.google.com" in page.url:
            await page.wait_for_timeout(500)
            continue
        if "auth.opencode.ai" in page.url and "/google/authorize" in page.url:
            await page.wait_for_load_state("domcontentloaded")
            await page.wait_for_timeout(1000)
            continue
        if await _click_first_available_optional(
            page,
            [
                'a[href*="google"]',
                'button:has-text("Google")',
                'a:has-text("Google")',
                '[aria-label*="Google"]',
            ],
        ):
            await page.wait_for_load_state("domcontentloaded")
            continue
        await page.wait_for_timeout(1000)
    raise RuntimeError(f"等待 Google 登录页面超时，当前页面: {page.url}")


async def _wait_for_opencode_login(page: Any) -> None:
    started = time.monotonic()
    while time.monotonic() - started < LOGIN_TIMEOUT_MS / 1000:
        url = page.url
        if "opencode.ai" in url and "/auth" not in url and "accounts.google.com" not in url:
            return
        text = ""
        try:
            text = (await page.locator("body").inner_text(timeout=1500)).lower()
        except Exception:
            pass
        if any(marker in text for marker in ("verify", "verification", "2-step", "two-step", "captcha", "couldn’t sign you in", "couldn't sign you in")):
            raise RuntimeError("Google 登录需要验证码、2FA 或人工验证")
        await page.wait_for_timeout(1000)
    raise RuntimeError("Google OAuth 登录超时")


async def _discover_workspace_id(page: Any) -> Optional[str]:
    for path in ("/", "/go"):
        try:
            await page.goto(f"{OPENCODE_BASE_URL}{path}", wait_until="domcontentloaded")
            hrefs = await page.locator('a[href^="/workspace/"]').evaluate_all("(nodes) => nodes.map((node) => node.getAttribute('href'))")
            workspace_id = _workspace_id_from_hrefs(hrefs)
            if workspace_id:
                return workspace_id
            match = re.search(r"/workspace/([^/]+)/", page.url)
            if match:
                return match.group(1)
        except Exception:
            continue
    return None


async def _discover_server_runtime_path(page: Any) -> str:
    html = await page.content()
    match = SERVER_RUNTIME_PATTERN.search(html)
    if match:
        return match.group("path")
    return "/_build/assets/server-runtime-BBEC8-uW.js"


async def _call_server_reference_in_page(page: Any, runtime_path: str, reference_id: str, args: list[Any]) -> Any:
    return await page.evaluate(
        """
        async ({ runtimePath, referenceId, args }) => {
          const mod = await import(runtimePath);
          const createServerReference = mod.a || mod.createServerReference;
          if (!createServerReference) {
            throw new Error("OpenCode server runtime export changed");
          }
          const fn = createServerReference(referenceId);
          return await fn(...args);
        }
        """,
        {"runtimePath": runtime_path, "referenceId": reference_id, "args": args},
    )


async def _fill_first_available(page: Any, selectors: list[str], value: str) -> None:
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            await locator.wait_for(state="visible", timeout=10_000)
            await locator.fill(value)
            return
        except Exception:
            continue
    raise RuntimeError("未找到 Google 登录输入框")


async def _has_any_selector(page: Any, selectors: list[str], timeout: int = 1000) -> bool:
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            await locator.wait_for(state="visible", timeout=timeout)
            return True
        except Exception:
            continue
    return False


async def _click_first_available(page: Any, selectors: list[str]) -> None:
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            await locator.wait_for(state="visible", timeout=10_000)
            await locator.click()
            return
        except Exception:
            continue
    raise RuntimeError("未找到 Google 登录下一步按钮")


async def _click_first_available_optional(page: Any, selectors: list[str]) -> bool:
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            await locator.wait_for(state="visible", timeout=1000)
            await locator.click()
            return True
        except Exception:
            continue
    return False


def _workspace_id_from_hrefs(hrefs: Any) -> Optional[str]:
    if not isinstance(hrefs, list):
        return None
    for href in hrefs:
        match = re.search(r"/workspace/([^/]+)", str(href or ""))
        if match:
            return match.group(1)
    return None


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


def _import_playwright() -> Any:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError("未安装 Playwright，请先安装 playwright 并执行 playwright install chromium") from exc
    return async_playwright


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


def _friendly_login_error(exc: Exception) -> str:
    text = str(exc)
    if "Executable doesn't exist" in text or "playwright install" in text:
        return "Playwright Chromium 未安装，请执行 playwright install chromium"
    if "2FA" in text or "验证码" in text or "verification" in text.lower() or "captcha" in text.lower():
        return "Google 登录需要验证码、2FA 或人工验证"
    return f"OpenCode Go 登录失败: {text}"


def _friendly_refresh_error(exc: Exception) -> str:
    text = str(exc)
    if "server runtime export changed" in text or "server reference" in text.lower() or "接口" in text:
        return f"OpenCode 前端接口可能已更新: {text}"
    if "Playwright" in text or "playwright install" in text:
        return "Playwright Chromium 未安装，请执行 playwright install chromium"
    return f"OpenCode Go 刷新失败: {text}"


def _looks_like_session_error(message: str) -> bool:
    text = message.lower()
    return "登录" in message or "unauthorized" in text or "401" in text or "workspace" in text or "auth" in text


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

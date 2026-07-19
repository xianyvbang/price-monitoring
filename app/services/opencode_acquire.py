"""OpenCode Go 自动获取 cookie / API key 的服务层。

把根目录 opencode_login_http.py 的核心逻辑抽成可被 app 调用的函数：
- 阶段 A：Playwright 走完 Google OAuth，拿到 opencode 的 provider/auth cookie，组装成
  与 _opencode_go_session_payload 兼容的 Playwright storage_state（cookies + origins）。
- 阶段 B：用 httpx（项目既有依赖）复用 cookie，/auth 302 取 workspace_id，/workspace/<id> 正则取 sk- key。

设计约束：
- 同步 Playwright API（sync_playwright），需在 async 路由里用 run_in_executor 调用。
- playwright / httpx 都按需导入，避免无浏览器环境起不来服务（httpx 已是项目依赖，顶层导入安全）。
- 失败不抛异常，返回 status="error" + error 原因，由上层决定是否建号 + 标记状态。
"""

from __future__ import annotations

import re
from typing import Any

import httpx

OPENCODE_BASE = "https://opencode.ai"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
KEY_RE = re.compile(r"sk-[A-Za-z0-9_\-]{12,}")
WRK_RE = re.compile(r"wrk_[0-9A-Za-z]+")

# Stage B 的纯协议请求默认头
_PROTOCOL_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": f"{OPENCODE_BASE}/",
}

# Playwright 启动 / 等待的统一超时（毫秒）
GOTO_TIMEOUT = 30000
WAIT_TIMEOUT = 25000


def _cookie_pair_to_str(provider: str, auth: str) -> str:
    parts = []
    if provider:
        parts.append(f"provider={provider}")
    if auth:
        parts.append(f"auth={auth}")
    return "; ".join(parts)


def _build_storage_state(cookies: list[dict[str, Any]], workspace_id: str = "") -> dict[str, Any]:
    """组装成与项目其它流程兼容的 storage_state 结构（cookies + origins）。"""
    state: dict[str, Any] = {"cookies": cookies, "origins": []}
    # workspace_id 同时塞进顶层字段，供 _opencode_go_session_payload / opencode_go 服务复用。
    if workspace_id:
        state["workspace_id"] = workspace_id
        state["workspaceId"] = workspace_id
    return state


def _discover_workspace_id(client: httpx.Client) -> tuple[str | None, int, str]:
    """GET /auth（不跟随）-> 302 Location: /workspace/<wrk_id>。"""
    r = client.get(f"{OPENCODE_BASE}/auth", follow_redirects=False, timeout=30.0)
    loc = r.headers.get("location", "") or ""
    if not loc:
        # 兼容少数情况：直接 SSR HTML 里带 wrk_id
        text = r.text if r.content else ""
        wrks = WRK_RE.findall(text)
        return (wrks[0] if wrks else None), r.status_code, loc
    m = WRK_RE.search(loc)
    return (m.group(0) if m else None), r.status_code, loc


def _fetch_keys_from_workspace(client: httpx.Client, wrk_id: str | None) -> list[str]:
    if not wrk_id:
        return []
    r = client.get(f"{OPENCODE_BASE}/workspace/{wrk_id}", follow_redirects=True, timeout=30.0)
    keys = KEY_RE.findall(r.text)
    return list(dict.fromkeys(keys))


def _google_login(page: Any, email: str, password: str) -> tuple[bool, str]:
    """Playwright 填 Google 邮箱 / 密码。返回 (ok, message)。"""
    try:
        page.wait_for_selector('input#identifierId, input[name="identifier"]', timeout=20000)
        page.fill('input#identifierId', email)
        page.click('#identifierNext')
        page.wait_for_timeout(2000)
        err = page.locator('div[class*="EjBTad"], div[data-error], div[role="alert"]').first
        if err.count() > 0:
            try:
                if err.is_visible(timeout=1500):
                    txt = (err.text_content() or "").strip()
                    if txt:
                        return False, f"邮箱错误: {txt[:60]}"
            except Exception:
                pass
        page.wait_for_selector('input[name="Passwd"], input[type="password"]:visible', timeout=20000)
        pw = 'input[name="Passwd"]'
        if page.locator(pw).count() == 0:
            pw = 'input[type="password"]:visible'
        page.fill(pw, password)
        page.click('#passwordNext')
        try:
            page.wait_for_url(lambda u: "opencode.ai" in u, timeout=WAIT_TIMEOUT)
        except Exception:
            cur = page.url
            if "accounts.google.com" in cur and (
                "challenge" in cur or "signin/v2" in cur or ".secondfactor" in cur
            ):
                return False, "需要二次验证/验证码"
            return False, f"未完成, 停在: {cur}"
        return True, "ok"
    except Exception as e:
        return False, f"google_login异常: {e}"


def _oauth_login(p: Any, email: str, password: str) -> tuple[str | None, list[str], str]:
    """Playwright 走完 Google OAuth + opencode 回跳，拿到 provider/auth cookie。

    返回 (cookie_str, info_steps, status_message)。cookie_str 为 None 表示失败。
    """
    browser = p.chromium.launch(
        headless=True, args=["--disable-blink-features=AutomationControlled"]
    )
    context = browser.new_context(user_agent=UA, locale="zh-CN")
    context.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
    page = context.new_page()
    info: list[str] = []
    try:
        # 1) opencode /auth -> auth.opencode.ai/authorize (302)
        page.goto(f"{OPENCODE_BASE}/auth", wait_until="domcontentloaded", timeout=GOTO_TIMEOUT)
        page.wait_for_load_state("networkidle", timeout=20000)
        info.append(f"entry:{page.url}")

        # 2) 点击 Continue with Google
        link = page.get_by_role("link", name="Continue with Google")
        if link.count() == 0:
            link = page.locator('a[href*="google/authorize"], a[href*="google"]')
        if link.count() == 0:
            return None, info + ["NO_GOOGLE_BTN"], "未找到 Google 登录按钮"
        link.first.click()
        page.wait_for_url(lambda u: "accounts.google.com" in u, timeout=30000)
        page.wait_for_load_state("domcontentloaded", timeout=30000)
        info.append(f"g_auth:{page.url[:80]}")

        # 3) Google 填邮箱 / 密码
        ok, msg = _google_login(page, email, password)
        info.append(f"g_login:{ok}:{msg}")
        if not ok:
            return None, info, msg

        # 4) 已回到 opencode.ai/workspace/...
        page.wait_for_load_state("networkidle", timeout=20000)
        info.append(f"final:{page.url}")

        # 5) 只收 provider/auth 两枚 cookie，后续完全用 httpx 协议取 key
        cookies = context.cookies()
        provider = ""
        auth = ""
        for c in cookies:
            if c["name"] == "provider":
                provider = c["value"]
            elif c["name"] == "auth":
                auth = c["value"]
        cookie_str = _cookie_pair_to_str(provider, auth)
        if not cookie_str:
            return None, info, "未拿到 provider/auth cookie"
        return cookie_str, info, "ok"
    except Exception as e:
        info.append(f"ERR:{e}")
        return None, info, f"OAuth异常: {e}"
    finally:
        try:
            context.close()
        except Exception:
            pass
        try:
            browser.close()
        except Exception:
            pass


def acquire_opencode_go_account(
    email: str, password: str, recovery_email: str | None = None
) -> dict[str, Any]:
    """自动登录 opencode.ai 并取回 storage_state / workspace_id / api_key。

    返回固定结构（不会抛异常）：
        {
            "storage_state": dict|None,   # Playwright storage_state（含 auth cookie）
            "workspace_id": str,          # wrk_...，失败为 ""
            "api_key": str,               # sk-...，失败为 ""
            "status": "ok"|"error",
            "error": str,                 # status=error 时为失败原因
            "info": list[str],            # 调试步骤日志
        }
    """
    email = (email or "").strip()
    password = password or ""
    if not email or not password:
        return _error_result("缺少 Google 邮箱或密码", [])

    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        return _error_result(f"未安装 Playwright，无法自动登录: {e}", [])

    info: list[str] = []
    try:
        with sync_playwright() as p:
            cookie_str, info_steps, status = _oauth_login(p, email, password)
            info.extend(info_steps)
            if not cookie_str:
                return _error_result(status or "登录失败", info)

            # 阶段 B：httpx 协议补全
            headers = dict(_PROTOCOL_HEADERS)
            headers["Cookie"] = cookie_str
            with httpx.Client(headers=headers, follow_redirects=True, timeout=30.0) as s:
                wrk_id, st, loc = _discover_workspace_id(s)
                info.append(f"discover:status={st} loc={loc} wrk_id={wrk_id}")
                keys = _fetch_keys_from_workspace(s, wrk_id)
            info.append(f"keys:{keys}")
            api_key = keys[0] if keys else ""

            if not wrk_id:
                return {
                    "storage_state": None,
                    "workspace_id": "",
                    "api_key": "",
                    "status": "error",
                    "error": "已登录但未能获取 workspace id",
                    "info": info,
                }

            # 组装 storage_state：把 provider/auth 写成 opencode.ai 域的 cookie，
            # 与 _opencode_go_session_payload 产出的结构对齐（要求含 auth cookie）。
            cookies = []
            cookie_pairs = [kv for kv in cookie_str.split("; ") if kv]
            for pair in cookie_pairs:
                if "=" not in pair:
                    continue
                name, value = pair.split("=", 1)
                cookies.append(
                    {
                        "name": name.strip(),
                        "value": value.strip(),
                        "domain": ".opencode.ai",
                        "path": "/",
                    }
                )
            storage_state = _build_storage_state(cookies, wrk_id)
            return {
                "storage_state": storage_state,
                "workspace_id": wrk_id,
                "api_key": api_key,
                "status": "ok",
                "error": "",
                "info": info,
            }
    except Exception as e:
        info.append(f"acquire异常:{e}")
        return _error_result(f"自动获取异常: {e}", info)


def _error_result(message: str, info: list[str]) -> dict[str, Any]:
    return {
        "storage_state": None,
        "workspace_id": "",
        "api_key": "",
        "status": "error",
        "error": message,
        "info": info,
    }
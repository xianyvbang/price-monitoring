import os
import re
import json
import time
import requests
from urllib.parse import urljoin, urlparse, parse_qs
from playwright.sync_api import sync_playwright

CP_FILE = r"D:\常用软件\opencode\cp.txt"
OUT_FILE = r"D:\常用软件\opencode\cp_out.txt"
ERROR_FILE = r"D:\常用软件\opencode\cp_errors.txt"
COOKIE_JSON = r"D:\常用软件\opencode\opencode_cookies.json"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
KEY_RE = re.compile(r"sk-[A-Za-z0-9_\-]{12,}")
WRK_RE = re.compile(r"wrk_[0-9A-Z]+")


def parse_line(line):
    parts = line.strip().split("----")
    if len(parts) < 2:
        return None, None, parts
    return parts[0].strip(), parts[1].strip(), parts[2:]


# ==================== 协议层 (纯 requests) ====================

def oc_session(cookie_str=None):
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": "https://opencode.ai/",
        }
    )
    if cookie_str:
        s.headers["Cookie"] = cookie_str
    return s


def discover_workspace_id(s):
    """GET /auth (不跟随) -> 302 Location: /workspace/<wrk_id>."""
    r = s.get("https://opencode.ai/auth", allow_redirects=False, timeout=30)
    loc = r.headers.get("Location", "")
    if not loc:
        # 兼容少数情况: 直接 SSR HTML 里带 wrk_id
        wrks = WRK_RE.findall(r.text)
        return wrks[0] if wrks else None, r.status_code, loc
    m = WRK_RE.search(loc)
    return (m.group(0) if m else None), r.status_code, loc


def fetch_keys_from_workspace(s, wrk_id):
    """GET /workspace/<wrk_id> -> SSR HTML 内嵌水合数据, 正则提取 sk- key."""
    if not wrk_id:
        return []
    r = s.get(f"https://opencode.ai/workspace/{wrk_id}", timeout=30)
    keys = KEY_RE.findall(r.text)
    return list(dict.fromkeys(keys))


def refresh_keys_by_cookie(cookie_str):
    """协议复用: 已有 cookie, 免登录刷新 key. 返回 (wrk_id, keys, info)."""
    s = oc_session(cookie_str)
    wrk_id, st, loc = discover_workspace_id(s)
    keys = fetch_keys_from_workspace(s, wrk_id)
    return wrk_id, keys, {"status": st, "location": loc}


def cookie_pair_to_str(provider, auth):
    """provider 域为 auth.opencode.ai, auth 域为 opencode.ai; 拼单行 Cookie 头."""
    parts = []
    if provider:
        parts.append(f"provider={provider}")
    if auth:
        parts.append(f"auth={auth}")
    return "; ".join(parts)


def safe_key(keys):
    return keys[0] if keys else ""


# ==================== Google OAuth (Playwright, botguard 不可协议化) ====================

def google_login(page, email, password):
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
            page.wait_for_url(lambda u: "opencode.ai" in u, timeout=25000)
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


def oauth_login(p, email, password):
    """Playwright 走完 Google OAuth, 拿到 opencode provider/auth cookie."""
    browser = p.chromium.launch(
        headless=True, args=["--disable-blink-features=AutomationControlled"]
    )
    context = browser.new_context(user_agent=UA, locale="zh-CN")
    context.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
    page = context.new_page()
    info = []
    try:
        # 1) opencode /auth -> -> auth.opencode.ai/authorize (302)
        page.goto("https://opencode.ai/auth", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_load_state("networkidle", timeout=20000)
        info.append(f"entry:{page.url}")

        # 2) 点击 Continue with Google -> auth.opencode.ai/google/authorize -> accounts.google.com
        link = page.get_by_role("link", name="Continue with Google")
        if link.count() == 0:
            link = page.locator('a[href*="google/authorize"], a[href*="google"]')
        if link.count() == 0:
            return None, info + ["NO_GOOGLE_BTN"], "未找到 Google 登录按钮"
        link.first.click()
        page.wait_for_url(lambda u: "accounts.google.com" in u, timeout=30000)
        page.wait_for_load_state("domcontentloaded", timeout=30000)
        info.append(f"g_auth:{page.url[:80]}")

        # 3) Google 填邮箱/密码
        ok, msg = google_login(page, email, password)
        info.append(f"g_login:{ok}:{msg}")
        if not ok:
            return None, info, msg

        # 4) 已回到 opencode.ai/workspace/...
        page.wait_for_load_state("networkidle", timeout=20000)
        info.append(f"final:{page.url}")

        # 5) 只收 provider/auth 两枚 cookie, 后续完全用 requests 协议取 key
        cookies = context.cookies()
        provider = ""
        auth = ""
        for c in cookies:
            if c["name"] == "provider":
                provider = c["value"]
            elif c["name"] == "auth":
                auth = c["value"]
        cookie_str = cookie_pair_to_str(provider, auth)
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


# ==================== 主流程 ====================

def main():
    if not os.path.exists(CP_FILE):
        print("cp.txt 不存在:", CP_FILE)
        return
    with open(CP_FILE, "r", encoding="utf-8") as f:
        lines = [ln for ln in f.read().splitlines() if ln.strip()]

    print(f"读取 {len(lines)} 个账号\n")

    out_lines = []
    err_lines = []
    cookie_json = {}

    with sync_playwright() as p:
        for idx, line in enumerate(lines, 1):
            email, password, _ = parse_line(line)
            if not email:
                continue
            print(f"[{idx}] {email}")

            # ---- 阶段A: Playwright Google OAuth, 拿 cookie ----
            cookie_str, info, status = oauth_login(p, email, password)
            print(f"    A) oauth: {status}  steps={info}")
            if not cookie_str:
                out_lines.append(line + "----" + "NO_COOKIE" + "----" + "NO_KEY")
                err_lines.append(f"{line}----{status}")
                cookie_json[email] = {"cookie": "", "wrk_id": "", "keys": [], "status": status}
                time.sleep(1)
                continue

            # ---- 阶段B: requests 协议补全: /auth 302 -> /workspace/<id> -> SSR 取 key ----
            s = oc_session(cookie_str)
            wrk_id, st, loc = discover_workspace_id(s)
            print(f"    B) discover: status={st} loc={loc} wrk_id={wrk_id}")
            keys = fetch_keys_from_workspace(s, wrk_id)
            print(f"    C) keys: {keys}")

            key_str = safe_key(keys)
            out_lines.append(line + "----" + cookie_str + "----" + key_str)
            cookie_json[email] = {
                "cookie": cookie_str,
                "wrk_id": wrk_id,
                "keys": keys,
                "status": status,
            }
            if not key_str:
                err_lines.append(f"{line}----NO_KEY:{status}")
            time.sleep(1)

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(out_lines))
    with open(ERROR_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(err_lines))
    with open(COOKIE_JSON, "w", encoding="utf-8") as f:
        json.dump(cookie_json, f, ensure_ascii=False, indent=2)

    print(f"\n完成 {len(out_lines)} 行")
    print(f"  -> {OUT_FILE}")
    print(f"  -> {ERROR_FILE}")
    print(f"  -> {COOKIE_JSON}")
    print("\n后续免登录刷新 key 调用 refresh_keys_by_cookie(cookie) 即可, 无需再开浏览器")


if __name__ == "__main__":
    main()
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app, public_opencode_go_account
from app.models import Database
from app.security import decrypt_value, encrypt_value
from app.services.opencode_go import (
    KEY_LIST_DEFAULT_JS_URL,
    KEY_LIST_GET_REFERENCE_ID,
    KEY_LIST_SERVER_INSTANCE,
    LITE_SUBSCRIPTION_GET_REFERENCE_ID,
    extract_key_list_reference_id,
    extract_lite_subscription_reference_id,
    extract_referral_reference_id,
    parse_referral_payload,
    query_key_list,
    query_lite_subscription_usage,
    normalize_usage_result,
    parse_server_function_key_response,
    parse_server_function_usage_response,
    query_opencode_server_reference,
    refresh_opencode_go_account,
)
from app.services.cpa_admin import CpaAdminClient
from app.services.scheduler import query_all_opencode_go_accounts, query_opencode_go_for_account


class DummySub2ApiResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload


class DummySub2ApiClient:
    requests = []
    responses = []

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def request(self, method, url, headers=None, **kwargs):
        self.requests.append({"method": method, "url": url, "headers": headers or {}, **kwargs})
        if not self.responses:
            return DummySub2ApiResponse({"code": 0, "data": {}})
        return self.responses.pop(0)


class DummyResponse:
    headers = {"content-type": "application/json"}
    text = "{}"

    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class DummyAsyncClient:
    last_request = None

    async def post(self, url, headers, json):
        DummyAsyncClient.last_request = {"url": url, "headers": headers, "json": json}
        return DummyResponse({"ok": True})

    async def get(self, url, params=None, headers=None):
        DummyAsyncClient.last_request = {"url": url, "params": params, "headers": headers}
        return DummyResponse({"ok": True})


class DummyRefreshClient:
    requests = []

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def get(self, url, params=None, headers=None):
        self.requests.append({"method": "GET", "url": url, "params": params, "headers": headers or {}})
        if url == "https://opencode.ai/_build/assets/index-test.js":
            return DummyHttpxResponse(
                None,
                headers={"content-type": "application/javascript"},
                text='const queryLiteSubscription_query = createServerReference("' + ("b" * 64) + '");',
                url=url,
            )
        if url == KEY_LIST_DEFAULT_JS_URL:
            return DummyHttpxResponse(
                None,
                headers={"content-type": "application/javascript"},
                text='const listKeys_query = createServerReference("' + ("c" * 64) + '");',
                url=url,
            )
        if url == "https://opencode.ai/_server":
            server_id = (headers or {})["X-Server-Id"]
            server_instance = (headers or {})["X-Server-Instance"]
            if server_instance == KEY_LIST_SERVER_INSTANCE and server_id in {KEY_LIST_GET_REFERENCE_ID, "c" * 64}:
                return DummyHttpxResponse({"data": [{"key": "sk-dynamic-secret"}]})
            if server_id in {LITE_SUBSCRIPTION_GET_REFERENCE_ID, "b" * 64}:
                return DummyHttpxResponse(
                    None,
                    headers={"content-type": "text/plain"},
                    text=''';0x00000123;
(($R => $R[0] = {
rollingUsage: $R[1] = { status: "ok", resetInSec: 60, usagePercent: 12 },
weeklyUsage: $R[2] = { status: "ok", resetInSec: 120, usagePercent: 23 },
monthlyUsage: $R[3] = { status: "ok", resetInSec: 180, usagePercent: 34 }
})($R["server-fn:3"]))''',
                )
            return DummyHttpxResponse({"message": "bad server id"}, headers={"X-Error": "1"})
        return DummyHttpxResponse({}, url="https://opencode.ai/")

    async def post(self, url, headers, json):
        self.requests.append({"method": "POST", "url": url, "headers": headers, "json": json})
        server_id = headers["X-Server-Id"]
        if server_id == "a" * 64:
            return DummyHttpxResponse({"data": {"workspaces": [{"id": "ws_dynamic", "name": "Main"}]}})
        if server_id == "c" * 64:
            return DummyHttpxResponse({"data": [{"key": "sk-dynamic-secret"}]})
        return DummyHttpxResponse({"message": "bad server id"}, headers={"X-Error": "1"})


class DummyHttpxResponse:
    def __init__(self, payload, headers=None, text=None, url="https://opencode.ai/_server"):
        self.payload = payload
        self.headers = {"content-type": "application/json", **(headers or {})}
        self.text = "{}" if text is None else text
        self.url = url

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def login(client: TestClient) -> None:
    client.post("/login", data={"username": "admin", "password": "password123"})


def setup_test_db(tmp_path, monkeypatch) -> Database:
    test_db = Database(str(tmp_path / "app.db"), "test-key")
    test_db.init()
    test_db.ensure_admin("admin", "password123")
    monkeypatch.setattr("app.main.db", test_db)
    monkeypatch.setattr("app.main.scheduler.db", test_db)
    monkeypatch.setattr("app.main.scheduler.start", lambda: None)

    async def stop_scheduler():
        return None

    monkeypatch.setattr("app.main.scheduler.stop", stop_scheduler)
    return test_db


def configure_sub2api(test_db: Database) -> None:
    test_db.set_setting("sub2api_site_url", "https://sub.example")
    test_db.set_setting("sub2api_admin_key_enc", encrypt_value("admin-secret", test_db.secret_key) or "")


def configure_cpa(test_db: Database) -> None:
    test_db.set_setting("cpa_site_url", "https://cpa.example/v0/management")
    test_db.set_setting("cpa_authorization_enc", encrypt_value("cpa-secret", test_db.secret_key) or "")


def test_opencode_go_account_encrypts_secrets(tmp_path):
    db = Database(str(tmp_path / "app.db"), "test-key")
    db.init()

    account_id = db.upsert_opencode_go_account(
        {
            "name": "go-main",
            "email": "user@example.com",
            "password": "secret-password",
            "is_enabled": True,
        }
    )
    db.update_opencode_go_session(account_id, {"cookies": [{"name": "session", "value": "cookie"}]}, workspace_id="ws_1")
    db.update_opencode_go_result(
        account_id,
        {
            "is_valid": True,
            "rolling_usage": {"usagePercent": 12.5, "resetInSec": 60},
            "weekly_usage": {"usagePercent": 25, "resetInSec": 120},
            "monthly_usage": {"usagePercent": 50, "resetInSec": 180},
            "api_key": "sk-opencode-secret",
            "workspace_id": "ws_1",
        },
    )

    account = db.get_opencode_go_account(account_id)
    history = db.list_opencode_go_history(account_id)

    assert account["email_enc"] != "user@example.com"
    assert decrypt_value(account["email_enc"], "test-key") == "user@example.com"
    assert decrypt_value(account["password_enc"], "test-key") == "secret-password"
    assert "cookie" in decrypt_value(account["storage_state_enc"], "test-key")
    assert decrypt_value(account["api_key_enc"], "test-key") == "sk-opencode-secret"
    assert account["api_key_masked"].startswith("sk-openc")
    assert len(history) == 1
    assert history[0]["api_key_masked"] == account["api_key_masked"]


@pytest.mark.asyncio
async def test_opencode_server_reference_request_headers():
    client = DummyAsyncClient()

    result = await query_opencode_server_reference(client, LITE_SUBSCRIPTION_GET_REFERENCE_ID, ["ws_1"], instance="server-fn:test")

    assert result == {"ok": True}
    assert DummyAsyncClient.last_request["url"] == f"https://opencode.ai/_server?id={LITE_SUBSCRIPTION_GET_REFERENCE_ID}"
    assert DummyAsyncClient.last_request["headers"]["X-Server-Id"] == LITE_SUBSCRIPTION_GET_REFERENCE_ID
    assert DummyAsyncClient.last_request["headers"]["X-Server-Instance"] == "server-fn:test"
    assert DummyAsyncClient.last_request["json"]["t"]["a"][0]["s"] == "ws_1"
    assert DummyAsyncClient.last_request["json"]["f"] == 31


@pytest.mark.asyncio
async def test_lite_subscription_usage_uses_fixed_get_endpoint():
    client = DummyAsyncClient()

    result = await query_lite_subscription_usage(client, LITE_SUBSCRIPTION_GET_REFERENCE_ID, "wrk_01KW01D1")

    assert result == {"ok": True}
    assert DummyAsyncClient.last_request["url"] == "https://opencode.ai/_server"
    assert DummyAsyncClient.last_request["params"]["id"] == LITE_SUBSCRIPTION_GET_REFERENCE_ID
    assert DummyAsyncClient.last_request["params"]["args"].startswith('{"t":{"t":9')
    assert '"s":"wrk_01KW01D1"' in DummyAsyncClient.last_request["params"]["args"]
    assert DummyAsyncClient.last_request["headers"]["X-Server-Id"] == LITE_SUBSCRIPTION_GET_REFERENCE_ID
    assert DummyAsyncClient.last_request["headers"]["X-Server-Instance"] == "server-fn:3"


@pytest.mark.asyncio
async def test_key_list_uses_fixed_get_endpoint_and_instance():
    client = DummyAsyncClient()

    result = await query_key_list(client, KEY_LIST_GET_REFERENCE_ID, "wrk_01KW01D1")

    assert result == {"ok": True}
    assert DummyAsyncClient.last_request["url"] == "https://opencode.ai/_server"
    assert DummyAsyncClient.last_request["params"]["id"] == KEY_LIST_GET_REFERENCE_ID
    assert DummyAsyncClient.last_request["params"]["args"].startswith('{"t":{"t":9')
    assert '"s":"wrk_01KW01D1"' in DummyAsyncClient.last_request["params"]["args"]
    assert DummyAsyncClient.last_request["headers"]["X-Server-Id"] == KEY_LIST_GET_REFERENCE_ID
    assert DummyAsyncClient.last_request["headers"]["X-Server-Instance"] == KEY_LIST_SERVER_INSTANCE


def test_extract_lite_subscription_reference_id_from_js():
    source = 'const queryLiteSubscription_query = createServerReference("c7389bd0e731f80f49593e5ee53835475f4e28594dd6bd83eb229bab753498cd");'

    assert extract_lite_subscription_reference_id(source) == LITE_SUBSCRIPTION_GET_REFERENCE_ID


def test_extract_key_list_reference_id_from_js():
    source = 'const listKeys_query = createServerReference("c22cd964237ba79f2f9b95faa2a14b804f870d1bab49279463379cc6a0fd0c85");'

    assert extract_key_list_reference_id(source) == KEY_LIST_GET_REFERENCE_ID


@pytest.mark.asyncio
async def test_refresh_prefers_cached_lite_subscription_server_id(tmp_path, monkeypatch):
    DummyRefreshClient.requests = []
    monkeypatch.setattr("app.services.opencode_go.httpx.AsyncClient", DummyRefreshClient)
    db = Database(str(tmp_path / "app.db"), "test-key")
    db.init()

    account_id = db.upsert_opencode_go_account(
        {"name": "go-main", "email": "user@example.com", "password": "secret-password"}
    )
    db.update_opencode_go_session(
        account_id,
        {
            "cookies": [{"name": "auth", "value": "cookie", "domain": ".opencode.ai"}],
            "origins": [],
            "serverIds": {"key.list": "c" * 64},
        },
        workspace_id="ws_manual",
    )

    result = await refresh_opencode_go_account(
        db.get_opencode_go_account(account_id),
        "test-key",
        10,
        lite_subscription_js_url="https://opencode.ai/_build/assets/index-test.js",
        lite_subscription_server_id="b" * 64,
    )

    assert result["is_valid"] is True
    assert not any(request["url"] == "https://opencode.ai/_build/assets/index-test.js" for request in DummyRefreshClient.requests)
    usage_request = next(request for request in DummyRefreshClient.requests if request["method"] == "GET" and request["url"] == "https://opencode.ai/_server")
    assert usage_request["headers"]["X-Server-Id"] == "b" * 64


def test_normalize_usage_result_maps_go_windows_and_keys():
    result = normalize_usage_result(
        {
            "rollingUsage": {"usagePercent": 10, "resetInSec": 300},
            "weeklyUsage": {"usagePercent": 20, "resetInSec": 600},
            "monthlyUsage": {"usagePercent": 30, "resetInSec": 900},
        },
        {"data": [{"name": "default", "key": "sk-opencode-secret"}]},
        "ws_1",
        {
            "data": {
                "user": {"id": "user_1", "email": "user@example.com", "token": "secret-token"},
                "workspaces": [{"id": "ws_1", "name": "Main"}],
            }
        },
    )

    assert result["workspace_id"] == "ws_1"
    assert result["rolling_usage"]["usage_percent"] == 10
    assert result["weekly_usage"]["usage_percent"] == 20
    assert result["monthly_usage"]["usage_percent"] == 30
    assert result["api_key"] == "sk-opencode-secret"
    assert result["api_key_masked"] == "sk-openc******cret"
    assert result["raw"]["keys"][0]["key"] == "***"
    assert result["raw"]["session"]["user"] == {"id": "user_1", "email": "user@example.com"}
    assert result["raw"]["session"]["workspaces"] == [{"id": "ws_1", "name": "Main"}]
    assert "secret-token" not in str(result["raw"])


def test_parse_server_function_usage_response_maps_windows():
    payload = r''';0x00000123;
((self.$R = self.$R || {})["server-fn:3"] = [],
($R => $R[0] = {
    mine: !0,
    useBalance: !1,
    rollingUsage: $R[1] = {
        status: "ok",
        resetInSec: 7238,
        usagePercent: 0
    },
    weeklyUsage: $R[2] = {
        status: "ok",
        resetInSec: 75218,
        usagePercent: 12.5
    },
    monthlyUsage: $R[3] = {
        status: "ok",
        resetInSec: 2465336,
        usagePercent: 33
    }
})($R["server-fn:3"]))'''

    parsed = parse_server_function_usage_response(payload)
    result = normalize_usage_result(parsed, {"data": []}, "ws_1")

    assert parsed["rollingUsage"]["resetInSec"] == 7238
    assert parsed["rollingUsage"]["usagePercent"] == 0
    assert result["rolling_usage"]["usage_percent"] == 0
    assert result["rolling_usage"]["reset_in_sec"] == 7238
    assert result["weekly_usage"]["usage_percent"] == 12.5
    assert result["weekly_usage"]["reset_in_sec"] == 75218
    assert result["monthly_usage"]["usage_percent"] == 33
    assert result["monthly_usage"]["reset_in_sec"] == 2465336


def test_parse_server_function_key_response_maps_keys():
    payload = r''';0x00000124;
(($R => $R[0] = [
  $R[1] = { id: "key_1", name: "Default", key: "sk-opencode-secret", createdAt: 1 }
])($R["server-fn:2"]))'''

    parsed = parse_server_function_key_response(payload)

    assert parsed == [{"id": "key_1", "name": "Default", "key": "sk-opencode-secret", "createdAt": 1.0}]


def test_normalize_usage_result_finds_nested_usage_payload():
    result = normalize_usage_result(
        {
            "data": {
                "workspace": {
                    "subscription": {
                        "rollingUsage": {"usagePercent": 4, "resetInSec": 50},
                        "weeklyUsage": {"usagePercent": 5, "resetInSec": 60},
                        "monthlyUsage": {"usagePercent": 6, "resetInSec": 70},
                    }
                }
            }
        },
        {"data": []},
        "ws_1",
    )

    assert result["rolling_usage"] == {"usage_percent": 4, "reset_in_sec": 50}
    assert result["weekly_usage"] == {"usage_percent": 5, "reset_in_sec": 60}
    assert result["monthly_usage"] == {"usage_percent": 6, "reset_in_sec": 70}


def test_normalize_usage_result_allows_missing_key():
    result = normalize_usage_result(
        {
            "rollingUsage": {"usagePercent": 1, "resetInSec": 2},
            "weeklyUsage": {},
            "monthlyUsage": {},
        },
        {"data": []},
    )

    assert result["api_key"] is None
    assert result["api_key_masked"] is None


@pytest.mark.asyncio
async def test_refresh_uses_fixed_lite_subscription_endpoint_with_saved_workspace(tmp_path, monkeypatch):
    DummyRefreshClient.requests = []
    monkeypatch.setattr("app.services.opencode_go.httpx.AsyncClient", DummyRefreshClient)
    db = Database(str(tmp_path / "app.db"), "test-key")
    db.init()

    account_id = db.upsert_opencode_go_account(
        {"name": "go-main", "email": "user@example.com", "password": "secret-password"}
    )
    db.update_opencode_go_session(
        account_id,
        {
            "cookies": [{"name": "auth", "value": "cookie", "domain": ".opencode.ai"}],
            "origins": [],
            "serverIds": {
                "session.get": "a" * 64,
                "lite.subscription.get": "b" * 64,
                "key.list": "c" * 64,
            },
        },
        workspace_id="ws_saved",
    )

    result = await refresh_opencode_go_account(db.get_opencode_go_account(account_id), "test-key", 10)

    assert result["is_valid"] is True
    assert result["workspace_id"] == "ws_saved"
    assert result["rolling_usage"]["usage_percent"] == 12
    assert result["api_key"] == "sk-dynamic-secret"
    server_requests = [request for request in DummyRefreshClient.requests if request["url"].startswith("https://opencode.ai/_server")]
    assert [request["headers"]["X-Server-Id"] for request in server_requests] == [LITE_SUBSCRIPTION_GET_REFERENCE_ID, "c" * 64]
    assert server_requests[0]["method"] == "GET"
    assert '"s":"ws_saved"' in server_requests[0]["params"]["args"]


@pytest.mark.asyncio
async def test_refresh_resolves_lite_subscription_server_id_from_js_url(tmp_path, monkeypatch):
    DummyRefreshClient.requests = []
    monkeypatch.setattr("app.services.opencode_go.httpx.AsyncClient", DummyRefreshClient)
    db = Database(str(tmp_path / "app.db"), "test-key")
    db.init()

    account_id = db.upsert_opencode_go_account(
        {"name": "go-main", "email": "user@example.com", "password": "secret-password"}
    )
    db.update_opencode_go_session(
        account_id,
        {
            "cookies": [{"name": "auth", "value": "cookie", "domain": ".opencode.ai"}],
            "origins": [],
            "serverIds": {"session.get": "a" * 64, "key.list": "c" * 64},
        },
        workspace_id="ws_manual",
    )

    result = await refresh_opencode_go_account(
        db.get_opencode_go_account(account_id),
        "test-key",
        10,
        lite_subscription_js_url="https://opencode.ai/_build/assets/index-test.js",
    )

    assert result["is_valid"] is True
    assert any(request["url"] == "https://opencode.ai/_build/assets/index-test.js" for request in DummyRefreshClient.requests)
    usage_request = next(request for request in DummyRefreshClient.requests if request["method"] == "GET" and request["url"] == "https://opencode.ai/_server")
    assert usage_request["headers"]["X-Server-Id"] == "b" * 64
    assert usage_request["headers"]["X-Server-Instance"] == "server-fn:3"


def test_opencode_go_api_requires_login(tmp_path, monkeypatch):
    db = setup_test_db(tmp_path, monkeypatch)
    account_id = db.upsert_opencode_go_account(
        {"name": "go-main", "email": "user@example.com", "password": "secret-password"}
    )

    with TestClient(app) as client:
        response = client.get("/api/opencode-go/accounts")
        password_response = client.get(f"/api/opencode-go/accounts/{account_id}/password")

    assert response.status_code == 401
    assert password_response.status_code == 401


def test_opencode_go_import_logs_filters_import_messages(tmp_path, monkeypatch):
    db = setup_test_db(tmp_path, monkeypatch)
    db.add_log("info", "opencode-go", "first@example.com 已导入 CPA: first@example.com，模型 2 个")
    db.add_log("error", "opencode-go", "second@example.com 批量导入 CPA 失败: 请求 CPA 失败")
    db.add_log("info", "opencode-go", "API 更新 OpenCode Go 账号: other@example.com")
    db.add_log("info", "account", "API 批量导入 sub2Api 账号 1 个")

    with TestClient(app) as client:
        login(client)
        response = client.get("/api/opencode-go/import-logs")

    assert response.status_code == 200
    payload = response.json()
    assert payload["pagination"]["total"] == 2
    assert [log["category"] for log in payload["logs"]] == ["opencode-go", "opencode-go"]
    assert all("导入" in log["message"] for log in payload["logs"])
    assert "API 更新 OpenCode Go 账号" not in response.text
    assert "sub2Api" not in response.text


def test_opencode_go_api_crud_and_masks_secrets(tmp_path, monkeypatch):
    db = setup_test_db(tmp_path, monkeypatch)

    async def fake_refresh(
        account,
        secret_key,
        timeout,
        log=None,
        lite_subscription_js_url=None,
        lite_subscription_server_id=None,
        key_list_js_url=None,
        key_list_server_id=None,
    ):
        return {
            "is_valid": True,
            "workspace_id": "ws_1",
            "rolling_usage": {"usagePercent": 11, "resetInSec": 60},
            "weekly_usage": {"usagePercent": 22, "resetInSec": 120},
            "monthly_usage": {"usagePercent": 33, "resetInSec": 180},
            "api_key": "sk-opencode-secret",
        }

    monkeypatch.setattr("app.services.scheduler.refresh_opencode_go_account", fake_refresh)

    with TestClient(app) as client:
        login(client)
        created = client.post(
            "/api/opencode-go/accounts",
            json={"name": "go-main", "email": "user@example.com", "password": "secret-password"},
        )
        account_id = created.json()["id"]
        refreshed = client.post(f"/api/opencode-go/accounts/{account_id}/refresh")
        listed = client.get("/api/opencode-go/accounts")
        key_response = client.get(f"/api/opencode-go/accounts/{account_id}/api-key")
        password_response = client.get(f"/api/opencode-go/accounts/{account_id}/password")
        history = client.get(f"/api/opencode-go/accounts/{account_id}/history")
        updated = client.put(
            f"/api/opencode-go/accounts/{account_id}",
            json={"name": "go-main", "email": "next@example.com", "is_enabled": False},
        )

    assert created.status_code == 200
    assert created.json()["account"]["email"] == "user@example.com"
    assert created.json()["account"]["is_enabled"] is False
    assert "secret-password" not in created.text
    assert refreshed.status_code == 200
    assert "sk-opencode-secret" not in refreshed.text
    assert refreshed.json()["account"]["api_key_masked"] == "sk-openc******cret"
    listed_account = listed.json()["accounts"][0]
    assert listed_account["has_api_key"] is True
    assert listed_account["rolling_usage"] == {"usage_percent": 11, "reset_in_sec": 60}
    assert "rollingUsage" not in listed_account
    assert "usagePercent" not in listed_account["rolling_usage"]
    assert "resetInSec" not in listed_account["rolling_usage"]
    assert "sk-opencode-secret" not in listed.text
    assert "secret-password" not in listed.text
    assert key_response.json()["api_key"] == "sk-opencode-secret"
    assert password_response.json()["password"] == "secret-password"
    assert history.json()["records"][0]["rolling_usage"]["usage_percent"] == 11
    assert updated.json()["account"]["email"] == "next@example.com"
    assert updated.json()["account"]["is_enabled"] is False
    assert decrypt_value(db.get_opencode_go_account(account_id)["password_enc"], "test-key") == "secret-password"


def test_opencode_go_settings_save_js_url(tmp_path, monkeypatch):
    db = setup_test_db(tmp_path, monkeypatch)
    db.update_general_settings(10, 900, 5, 60, monitor_paused=True)

    async def fake_fetch_js_server_id(js_url, timeout=15.0):
        return "d" * 64

    async def fake_fetch_key_js_server_id(js_url=None, timeout=15.0):
        return "e" * 64

    monkeypatch.setattr("app.main.fetch_lite_subscription_reference_id", fake_fetch_js_server_id)
    monkeypatch.setattr("app.main.fetch_key_list_reference_id", fake_fetch_key_js_server_id)

    with TestClient(app) as client:
        login(client)
        saved = client.post(
            "/api/opencode-go/settings",
            json={
                "lite_subscription_js_url": "https://opencode.ai/_build/assets/index-DtPYjwk4.js",
                "key_list_js_url": "https://opencode.ai/_build/assets/index-PbCOrg8_.js",
            },
        )
        loaded = client.get("/api/opencode-go/settings")

    assert saved.status_code == 200
    assert saved.json()["settings"]["lite_subscription_js_url"] == "https://opencode.ai/_build/assets/index-DtPYjwk4.js"
    assert saved.json()["settings"]["lite_subscription_server_id"] == "d" * 64
    assert saved.json()["settings"]["key_list_js_url"] == "https://opencode.ai/_build/assets/index-PbCOrg8_.js"
    assert saved.json()["settings"]["key_list_server_id"] == "e" * 64
    assert saved.json()["settings"]["query_interval"] == 900
    assert saved.json()["settings"]["queryInterval"] == 900
    assert saved.json()["settings"]["monitor_paused"] is True
    assert saved.json()["settings"]["monitorPaused"] is True
    assert loaded.json()["settings"]["lite_subscription_js_url"] == "https://opencode.ai/_build/assets/index-DtPYjwk4.js"
    assert loaded.json()["settings"]["lite_subscription_server_id"] == "d" * 64
    assert loaded.json()["settings"]["key_list_js_url"] == "https://opencode.ai/_build/assets/index-PbCOrg8_.js"
    assert loaded.json()["settings"]["key_list_server_id"] == "e" * 64
    assert loaded.json()["settings"]["query_interval"] == 900
    assert loaded.json()["settings"]["queryInterval"] == 900
    assert loaded.json()["settings"]["monitor_paused"] is True
    assert loaded.json()["settings"]["monitorPaused"] is True
    assert db.get_setting("opencode_go_lite_subscription_js_url") == "https://opencode.ai/_build/assets/index-DtPYjwk4.js"
    assert db.get_setting("opencode_go_lite_subscription_server_id") == "d" * 64
    assert db.get_setting("opencode_go_key_list_js_url") == "https://opencode.ai/_build/assets/index-PbCOrg8_.js"
    assert db.get_setting("opencode_go_key_list_server_id") == "e" * 64


def test_opencode_go_settings_rejects_non_opencode_js_url(tmp_path, monkeypatch):
    setup_test_db(tmp_path, monkeypatch)

    with TestClient(app) as client:
        login(client)
        response = client.post("/api/opencode-go/settings", json={"lite_subscription_js_url": "https://example.com/index.js"})

    assert response.status_code == 400
    assert "opencode.ai" in response.json()["message"]


def test_opencode_go_cpa_auto_delete_setting_defaults_off_and_persists(tmp_path, monkeypatch):
    db = setup_test_db(tmp_path, monkeypatch)

    with TestClient(app) as client:
        login(client)
        initial = client.get("/api/opencode-go/settings")
        invalid = client.post("/api/opencode-go/settings/cpa-auto-delete", json={"enabled": "true"})
        enabled = client.post("/api/opencode-go/settings/cpa-auto-delete", json={"enabled": True})
        loaded = client.get("/api/opencode-go/settings")
        disabled = client.post("/api/opencode-go/settings/cpa-auto-delete", json={"enabled": False})

    assert initial.status_code == 200
    assert initial.json()["settings"]["cpa_auto_delete_enabled"] is False
    assert initial.json()["settings"]["cpaAutoDeleteEnabled"] is False
    assert invalid.status_code == 400
    assert enabled.json()["settings"]["cpa_auto_delete_enabled"] is True
    assert loaded.json()["settings"]["cpaAutoDeleteEnabled"] is True
    assert disabled.json()["settings"]["cpa_auto_delete_enabled"] is False
    assert db.get_setting("opencode_go_cpa_auto_delete_enabled") == "0"


def test_opencode_go_query_all_skips_disabled_accounts(tmp_path, monkeypatch):
    db = setup_test_db(tmp_path, monkeypatch)
    calls = []

    async def fake_refresh(
        account,
        secret_key,
        timeout,
        log=None,
        lite_subscription_js_url=None,
        lite_subscription_server_id=None,
        key_list_js_url=None,
        key_list_server_id=None,
    ):
        calls.append(account["name"])
        return {
            "is_valid": True,
            "workspace_id": "ws_1",
            "rolling_usage": {"usagePercent": 11, "resetInSec": 60},
            "weekly_usage": {"usagePercent": 22, "resetInSec": 120},
            "monthly_usage": {"usagePercent": 33, "resetInSec": 180},
        }

    monkeypatch.setattr("app.services.scheduler.refresh_opencode_go_account", fake_refresh)
    enabled_id = db.upsert_opencode_go_account({"name": "enabled", "email": "enabled@example.com", "is_enabled": True})
    disabled_id = db.upsert_opencode_go_account({"name": "disabled", "email": "disabled@example.com", "is_enabled": False})

    with TestClient(app) as client:
        login(client)
        all_response = client.post("/api/opencode-go/query-all")
        manual_response = client.post(f"/api/opencode-go/accounts/{disabled_id}/refresh")

    assert all_response.status_code == 200
    assert [result["account_id"] for result in all_response.json()["results"]] == [enabled_id]
    assert calls == ["enabled@example.com", "disabled@example.com"]
    assert manual_response.status_code == 200
    assert db.list_opencode_go_history(enabled_id)
    assert db.list_opencode_go_history(disabled_id)


@pytest.mark.asyncio
async def test_opencode_go_query_all_refreshes_weekly_limit_and_manual_refreshes(tmp_path, monkeypatch):
    db = setup_test_db(tmp_path, monkeypatch)
    calls = []

    async def fake_refresh(
        account,
        secret_key,
        timeout,
        log=None,
        lite_subscription_js_url=None,
        lite_subscription_server_id=None,
        key_list_js_url=None,
        key_list_server_id=None,
    ):
        calls.append(account["name"])
        return {
            "is_valid": True,
            "workspace_id": "ws_1",
            "rolling_usage": {"usagePercent": 11, "resetInSec": 60},
            "weekly_usage": {"usagePercent": 22, "resetInSec": 120},
            "monthly_usage": {"usagePercent": 33, "resetInSec": 180},
        }

    monkeypatch.setattr("app.services.scheduler.refresh_opencode_go_account", fake_refresh)
    high_id = db.upsert_opencode_go_account({"name": "high@example.com", "email": "high@example.com", "is_enabled": True})
    low_id = db.upsert_opencode_go_account({"name": "low@example.com", "email": "low@example.com", "is_enabled": True})
    missing_id = db.upsert_opencode_go_account({"name": "missing@example.com", "email": "missing@example.com", "is_enabled": True})
    invalid_id = db.upsert_opencode_go_account({"name": "invalid@example.com", "email": "invalid@example.com", "is_enabled": True})
    db.update_opencode_go_result(
        high_id,
        {"is_valid": True, "rolling_usage": {"usagePercent": 90}, "weekly_usage": {"usagePercent": 99}},
    )
    db.update_opencode_go_result(
        low_id,
        {"is_valid": True, "rolling_usage": {"usagePercent": 10}, "weekly_usage": {"usagePercent": 98.9}},
    )
    with db.connect() as conn:
        conn.execute("UPDATE opencode_go_accounts SET last_weekly_usage = ? WHERE id = ?", ("not-json", invalid_id))

    results = await query_all_opencode_go_accounts(db)
    manual_result = await query_opencode_go_for_account(db, high_id)

    assert {result["account_id"] for result in results} == {high_id, low_id, missing_id, invalid_id}
    assert manual_result["account_id"] == high_id
    assert "high@example.com" in calls[:-1]
    assert calls[-1] == "high@example.com"


def test_opencode_go_accounts_summary_averages_only_normal_eligible_accounts(tmp_path, monkeypatch):
    db = setup_test_db(tmp_path, monkeypatch)
    account_ids = []
    for index in range(25):
        email = f"summary{index:02d}@example.com"
        account_ids.append(db.upsert_opencode_go_account({"name": email, "email": email, "password": "secret-password"}))

    db.update_opencode_go_result(
        account_ids[0],
        {
            "is_valid": True,
            "rolling_usage": {"usagePercent": 10},
            "weekly_usage": {"usagePercent": 20},
            "monthly_usage": {"usagePercent": 30},
        },
    )
    db.update_opencode_go_result(
        account_ids[1],
        {"is_valid": True, "weekly_usage": {"usagePercent": 98.9}, "monthly_usage": {"usagePercent": 50}},
    )
    db.update_opencode_go_result(
        account_ids[2],
        {"is_valid": True, "rolling_usage": {"usagePercent": 70}, "weekly_usage": {"usagePercent": 99}},
    )
    db.update_opencode_go_result(
        account_ids[3],
        {"is_valid": True, "rolling_usage": {"usagePercent": 80}, "weekly_usage": {"usagePercent": 100}},
    )
    db.update_opencode_go_result(
        account_ids[4],
        {"is_valid": True, "rolling_usage": {"usagePercent": 90}, "weekly_usage": {"usagePercent": 10}, "monthly_usage": {"usagePercent": 90}},
    )
    db.update_opencode_go_session(account_ids[4], {"cookies": []})
    db.update_opencode_go_result(
        account_ids[5],
        {"is_valid": True, "rolling_usage": {"usagePercent": 90}, "weekly_usage": {"usagePercent": 10}, "monthly_usage": {"usagePercent": 90}},
    )
    db.update_opencode_go_cpa_state(account_ids[5], provider_deleted=True)
    db.update_opencode_go_result(
        account_ids[6],
        {"is_valid": True, "rolling_usage": {"usagePercent": 90}, "weekly_usage": {"usagePercent": 10}, "monthly_usage": {"usagePercent": 90}},
    )
    with db.connect() as conn:
        conn.execute("UPDATE opencode_go_accounts SET last_status = 'invalid' WHERE id = ?", (account_ids[6],))
    for account_id in account_ids[7:]:
        db.update_opencode_go_result(
            account_id,
            {"is_valid": True, "rolling_usage": {"usagePercent": 90}, "weekly_usage": {"usagePercent": 100}},
        )
    with db.connect() as conn:
        for index, account_id in enumerate(account_ids):
            conn.execute(
                "UPDATE opencode_go_accounts SET created_at = ?, updated_at = ? WHERE id = ?",
                (f"2026-01-{index + 1:02d}T00:00:00+00:00", f"2026-01-{index + 1:02d}T00:00:00+00:00", account_id),
            )

    with TestClient(app) as client:
        login(client)
        response = client.get("/api/opencode-go/accounts")

    payload = response.json()
    first_page_emails = [account["email"] for account in payload["accounts"]]
    summary = payload["summary"]
    assert response.status_code == 200
    assert "summary00@example.com" not in first_page_emails
    assert "summary01@example.com" not in first_page_emails
    assert summary["eligible_account_count"] == 2
    assert summary["eligibleAccountCount"] == 2
    assert summary["overall_rolling_usage_percent"] == pytest.approx(5.0)
    assert summary["overallRollingUsagePercent"] == pytest.approx(5.0)
    assert summary["overall_weekly_usage_percent"] == pytest.approx(59.45)
    assert summary["overallWeeklyUsagePercent"] == pytest.approx(59.45)
    assert summary["overall_monthly_usage_percent"] == pytest.approx(40.0)
    assert summary["overallMonthlyUsagePercent"] == pytest.approx(40.0)


def test_opencode_go_accounts_summary_returns_null_when_no_eligible_accounts(tmp_path, monkeypatch):
    db = setup_test_db(tmp_path, monkeypatch)
    high_id = db.upsert_opencode_go_account({"name": "high@example.com", "email": "high@example.com"})
    missing_id = db.upsert_opencode_go_account({"name": "missing@example.com", "email": "missing@example.com"})
    db.update_opencode_go_result(high_id, {"is_valid": True, "weekly_usage": {"usagePercent": 99}})

    with TestClient(app) as client:
        login(client)
        response = client.get("/api/opencode-go/accounts")

    summary = response.json()["summary"]
    assert response.status_code == 200
    assert summary["eligible_account_count"] == 0
    assert summary["overall_rolling_usage_percent"] is None
    assert summary["overall_weekly_usage_percent"] is None
    assert summary["overall_monthly_usage_percent"] is None
    assert db.get_opencode_go_account(missing_id)


def test_opencode_go_bulk_import_uses_email_as_name_and_masks_password(tmp_path, monkeypatch):
    db = setup_test_db(tmp_path, monkeypatch)

    def fake_acquire(email, password, recovery_email=None):
        return {
            "storage_state": {"cookies": [{"name": "auth", "value": "x", "domain": ".opencode.ai", "path": "/"}], "origins": []},
            "workspace_id": "wrk_TEST",
            "api_key": f"sk-{email.split('@')[0]}",
            "status": "ok",
            "error": "",
            "info": ["fake"],
        }

    monkeypatch.setattr("app.main.acquire_opencode_go_account", fake_acquire)

    with TestClient(app) as client:
        login(client)
        response = client.post(
            "/api/opencode-go/accounts/bulk",
            json={"bulk_text": "first@example.com|first-pass\nsecond@example.com|second-pass"},
        )

    assert response.status_code == 200
    assert response.json()["count"] == 2
    assert response.json()["accounts"][0]["name"] == "first@example.com"
    assert response.json()["accounts"][0]["email"] == "first@example.com"
    assert response.json()["accounts"][0]["is_enabled"] is False
    assert "first-pass" not in response.text
    first = db.get_opencode_go_account(response.json()["accounts"][0]["id"])
    assert decrypt_value(first["password_enc"], "test-key") == "first-pass"
    assert first["workspace_id"] == "wrk_TEST"


def test_opencode_go_bulk_import_rejects_bad_lines(tmp_path, monkeypatch):
    setup_test_db(tmp_path, monkeypatch)

    with TestClient(app) as client:
        login(client)
        response = client.post("/api/opencode-go/accounts/bulk", json={"bulk_text": "broken-line"})

    assert response.status_code == 400
    assert "账号|密码" in response.json()["message"]


def test_opencode_go_bulk_import_accepts_dash_separator(tmp_path, monkeypatch):
    db = setup_test_db(tmp_path, monkeypatch)

    def fake_acquire(email, password, recovery_email=None):
        return {
            "storage_state": {"cookies": [{"name": "auth", "value": "x", "domain": ".opencode.ai", "path": "/"}], "origins": []},
            "workspace_id": "wrk_TEST",
            "api_key": f"sk-{email.split('@')[0]}",
            "status": "ok",
            "error": "",
            "info": ["fake"],
        }

    monkeypatch.setattr("app.main.acquire_opencode_go_account", fake_acquire)

    with TestClient(app) as client:
        login(client)
        response = client.post(
            "/api/opencode-go/accounts/bulk",
            json={"bulk_text": "user1@example.com----pass1----recover1@example.com\nuser2@example.com----pass2"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 2
    first = db.get_opencode_go_account_by_email("user1@example.com")
    assert decrypt_value(first["password_enc"], "test-key") == "pass1"
    assert decrypt_value(first["recovery_email_enc"], "test-key") == "recover1@example.com"
    second = db.get_opencode_go_account_by_email("user2@example.com")
    assert decrypt_value(second["password_enc"], "test-key") == "pass2"


def test_opencode_go_bulk_import_skips_duplicates_and_keeps_failed(tmp_path, monkeypatch):
    db = setup_test_db(tmp_path, monkeypatch)

    calls: list[tuple] = []

    def fake_acquire(email, password, recovery_email=None):
        calls.append((email, password, recovery_email))
        if "fail" in email:
            return {"storage_state": None, "workspace_id": "", "api_key": "", "status": "error", "error": "登录被拒", "info": []}
        return {
            "storage_state": {"cookies": [{"name": "auth", "value": "x", "domain": ".opencode.ai", "path": "/"}], "origins": []},
            "workspace_id": "wrk_OK",
            "api_key": "sk-good",
            "status": "ok",
            "error": "",
            "info": ["fake"],
        }

    monkeypatch.setattr("app.main.acquire_opencode_go_account", fake_acquire)

    with TestClient(app) as client:
        login(client)
        client.post(
            "/api/opencode-go/accounts",
            json={"name": "dup@example.com", "email": "dup@example.com", "password": "p"},
        )
        response = client.post(
            "/api/opencode-go/accounts/bulk",
            json={
                "bulk_text": "dup@example.com|p\nnew@example.com|q\nfail@example.com|q",
                "skip_duplicates": True,
            },
        )

    body = response.json()
    assert response.status_code == 200
    assert body["count"] == 2          # new + fail 都建号
    assert body["skipped"] == 1        # dup 跳过
    assert body["failed"] == 1
    # 重复账号没跑浏览器
    assert not any(c[0] == "dup@example.com" for c in calls)
    # 失败账号也建号并标记 invalid
    fail_row = next(a for a in body["accounts"] if a["email"] == "fail@example.com")
    assert fail_row["last_status"] == "invalid"
    assert "登录被拒" in (fail_row["last_error"] or "")
    dup_existing = db.get_opencode_go_account_by_email("dup@example.com")
    assert decrypt_value(dup_existing["password_enc"], "test-key") == "p"


def test_opencode_go_acquire_endpoint_writes_session_and_key(tmp_path, monkeypatch):
    db = setup_test_db(tmp_path, monkeypatch)

    def fake_acquire(email, password, recovery_email=None):
        return {
            "storage_state": {"cookies": [{"name": "auth", "value": "tok", "domain": ".opencode.ai", "path": "/"}], "origins": []},
            "workspace_id": "wrk_ACQ",
            "api_key": "sk-acquired",
            "status": "ok",
            "error": "",
            "info": ["fake"],
        }

    monkeypatch.setattr("app.main.acquire_opencode_go_account", fake_acquire)

    with TestClient(app) as client:
        login(client)
        created = client.post(
            "/api/opencode-go/accounts",
            json={"name": "u@example.com", "email": "u@example.com", "password": "pw"},
        )
        account_id = created.json()["id"]
        response = client.post(f"/api/opencode-go/accounts/{account_id}/acquire")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["has_api_key"] is True
    account = db.get_opencode_go_account(account_id)
    assert account["workspace_id"] == "wrk_ACQ"
    assert decrypt_value(account["api_key_enc"], "test-key") == "sk-acquired"
    assert bool(account["storage_state_enc"]) is True


def test_opencode_go_import_session_encrypts_and_masks_state(tmp_path, monkeypatch):
    db = setup_test_db(tmp_path, monkeypatch)

    with TestClient(app) as client:
        login(client)
        created = client.post(
            "/api/opencode-go/accounts",
            json={"name": "go-main", "email": "user@example.com", "password": "secret-password"},
        )
        account_id = created.json()["id"]
        response = client.post(
            f"/api/opencode-go/accounts/{account_id}/session",
            json={
                "workspace_id": "ws_manual",
                "storage_state": {
                    "cookies": [{"name": "auth", "value": "manual-cookie", "domain": ".opencode.ai"}],
                    "origins": [],
                },
            },
        )

    assert response.status_code == 200
    assert response.json()["account"]["has_session"] is True
    assert "manual-cookie" not in response.text
    account = db.get_opencode_go_account(account_id)
    assert account["workspace_id"] == "ws_manual"
    assert "manual-cookie" in decrypt_value(account["storage_state_enc"], "test-key")


def test_opencode_go_session_endpoint_returns_saved_state_for_dialog(tmp_path, monkeypatch):
    setup_test_db(tmp_path, monkeypatch)

    with TestClient(app) as client:
        login(client)
        created = client.post(
            "/api/opencode-go/accounts",
            json={"name": "go-main", "email": "user@example.com", "password": "secret-password"},
        )
        account_id = created.json()["id"]
        client.post(
            f"/api/opencode-go/accounts/{account_id}/session",
            json={"workspace_id": "ws_manual", "storage_state": "auth=browser-cookie; other=value"},
        )
        session_response = client.get(f"/api/opencode-go/accounts/{account_id}/session")
        list_response = client.get("/api/opencode-go/accounts")

    assert session_response.status_code == 200
    assert session_response.json()["workspace_id"] == "ws_manual"
    assert "browser-cookie" in session_response.json()["storage_state"]
    assert "browser-cookie" not in list_response.text


def test_opencode_go_import_session_rejects_bad_json(tmp_path, monkeypatch):
    db = setup_test_db(tmp_path, monkeypatch)
    account_id = db.upsert_opencode_go_account(
        {"name": "go-main", "email": "user@example.com", "password": "secret-password"}
    )

    with TestClient(app) as client:
        login(client)
        response = client.post(
            f"/api/opencode-go/accounts/{account_id}/session",
            json={"storage_state": "not-json"},
        )

    assert response.status_code == 400
    assert "JSON" in response.json()["message"]


def test_opencode_go_import_session_requires_workspace_id_and_auth_cookie(tmp_path, monkeypatch):
    db = setup_test_db(tmp_path, monkeypatch)
    account_id = db.upsert_opencode_go_account(
        {"name": "go-main", "email": "user@example.com", "password": "secret-password"}
    )

    with TestClient(app) as client:
        login(client)
        missing_workspace = client.post(
            f"/api/opencode-go/accounts/{account_id}/session",
            json={"storage_state": "auth=browser-cookie"},
        )
        missing_auth = client.post(
            f"/api/opencode-go/accounts/{account_id}/session",
            json={"workspace_id": "ws_1", "storage_state": "oc_locale=zh"},
        )

    assert missing_workspace.status_code == 400
    assert "Workspace ID" in missing_workspace.json()["message"]
    assert missing_auth.status_code == 400
    assert "auth Cookie" in missing_auth.json()["message"]


def test_opencode_go_import_session_accepts_cookie_header(tmp_path, monkeypatch):
    db = setup_test_db(tmp_path, monkeypatch)

    with TestClient(app) as client:
        login(client)
        created = client.post(
            "/api/opencode-go/accounts",
            json={"name": "go-main", "email": "user@example.com", "password": "secret-password"},
        )
        account_id = created.json()["id"]
        response = client.post(
            f"/api/opencode-go/accounts/{account_id}/session",
            json={"workspace_id": "ws_cookie", "storage_state": "auth=browser-cookie; other=value"},
        )

    assert response.status_code == 200
    assert response.json()["account"]["has_session"] is True
    assert "browser-cookie" not in response.text
    account = db.get_opencode_go_account(account_id)
    assert account["workspace_id"] == "ws_cookie"
    assert "browser-cookie" in decrypt_value(account["storage_state_enc"], "test-key")


def test_opencode_go_import_session_accepts_wrapped_server_ids(tmp_path, monkeypatch):
    db = setup_test_db(tmp_path, monkeypatch)

    with TestClient(app) as client:
        login(client)
        created = client.post(
            "/api/opencode-go/accounts",
            json={"name": "go-main", "email": "user@example.com", "password": "secret-password"},
        )
        account_id = created.json()["id"]
        response = client.post(
            f"/api/opencode-go/accounts/{account_id}/session",
            json={
                "storage_state": {
                    "storage_state": {
                        "cookies": [{"name": "auth", "value": "manual-cookie", "domain": ".opencode.ai"}],
                        "origins": [],
                    },
                    "serverIds": {"lite.subscription.get": "b" * 64},
                    "workspace_id": "ws_wrapped",
                }
            },
        )

    assert response.status_code == 200
    account = db.get_opencode_go_account(account_id)
    saved_state = decrypt_value(account["storage_state_enc"], "test-key")
    assert account["workspace_id"] == "ws_wrapped"
    assert "manual-cookie" in saved_state
    assert "serverIds" in saved_state


def test_opencode_go_sub2api_groups_requires_config(tmp_path, monkeypatch):
    setup_test_db(tmp_path, monkeypatch)

    with TestClient(app) as client:
        login(client)
        response = client.get("/api/opencode-go/sub2api/groups")

    assert response.status_code == 400
    assert "Sub2API 站点地址" in response.json()["message"]


def test_opencode_go_sub2api_groups_requires_admin_key(tmp_path, monkeypatch):
    db = setup_test_db(tmp_path, monkeypatch)
    db.set_setting("sub2api_site_url", "https://sub.example")

    with TestClient(app) as client:
        login(client)
        response = client.get("/api/opencode-go/sub2api/groups")

    assert response.status_code == 400
    assert "AdminKey" in response.json()["message"]


def test_opencode_go_sub2api_groups_filters_openai_and_unwraps_response(tmp_path, monkeypatch):
    db = setup_test_db(tmp_path, monkeypatch)
    configure_sub2api(db)
    DummySub2ApiClient.requests = []
    DummySub2ApiClient.responses = [
        DummySub2ApiResponse(
            {
                "code": 0,
                "data": [
                    {"id": 1, "name": "OpenAI A", "platform": "openai"},
                    {"id": 2, "name": "Anthropic", "platform": "anthropic"},
                    {"id": 3, "name": "Legacy"},
                ],
            }
        )
    ]
    monkeypatch.setattr("app.services.sub2api_admin.httpx.AsyncClient", DummySub2ApiClient)

    with TestClient(app) as client:
        login(client)
        response = client.get("/api/opencode-go/sub2api/groups")

    assert response.status_code == 200
    assert [group["id"] for group in response.json()["groups"]] == [1, 3]
    request = DummySub2ApiClient.requests[0]
    assert request["method"] == "GET"
    assert request["url"] == "https://sub.example/api/v1/admin/groups/all"
    assert request["params"] == {"platform": "openai"}
    assert request["headers"]["x-api-key"] == "admin-secret"
    assert "admin-secret" not in response.text


def test_opencode_go_import_sub2api_requires_api_key(tmp_path, monkeypatch):
    db = setup_test_db(tmp_path, monkeypatch)
    configure_sub2api(db)
    account_id = db.upsert_opencode_go_account(
        {"name": "go-main", "email": "user@example.com", "password": "secret-password"}
    )

    with TestClient(app) as client:
        login(client)
        response = client.post(f"/api/opencode-go/accounts/{account_id}/import-sub2api", json={"group_ids": [1]})

    assert response.status_code == 400
    assert "尚未获取 API key" in response.json()["message"]


def test_opencode_go_import_sub2api_requires_existing_account(tmp_path, monkeypatch):
    db = setup_test_db(tmp_path, monkeypatch)
    configure_sub2api(db)

    with TestClient(app) as client:
        login(client)
        response = client.post("/api/opencode-go/accounts/999/import-sub2api", json={"group_ids": [1]})

    assert response.status_code == 404
    assert "OpenCode Go 账号不存在" in response.json()["detail"]


def test_opencode_go_import_sub2api_creates_remote_account_with_synced_models(tmp_path, monkeypatch):
    db = setup_test_db(tmp_path, monkeypatch)
    configure_sub2api(db)
    account_id = db.upsert_opencode_go_account(
        {
            "name": "go-main",
            "email": "user@example.com",
            "password": "secret-password",
            "api_key": "sk-opencode-secret",
        }
    )
    DummySub2ApiClient.requests = []
    DummySub2ApiClient.responses = [
        DummySub2ApiResponse({"code": 0, "data": {"models": ["gpt-5", "gpt-5-mini", "gpt-5"]}}),
        DummySub2ApiResponse(
            {
                "code": 0,
                "data": {
                    "id": 123,
                    "name": "opencode-user@example.com",
                    "credentials": {
                        "base_url": "https://opencode.ai/zen/go",
                        "api_key": "sk-opencode-secret",
                        "model_mapping": {"gpt-5": "gpt-5"},
                    },
                },
            }
        ),
    ]
    monkeypatch.setattr("app.services.sub2api_admin.httpx.AsyncClient", DummySub2ApiClient)

    with TestClient(app) as client:
        login(client)
        response = client.post(f"/api/opencode-go/accounts/{account_id}/import-sub2api", json={"group_ids": ["2", 3, 2]})
        logs = client.get("/api/logs")

    assert response.status_code == 200
    payload = response.json()
    assert payload["name"] == "opencode-user@example.com"
    assert payload["models"] == ["gpt-5", "gpt-5-mini"]
    assert payload["group_ids"] == [2, 3]
    assert payload["account"]["credentials"] == {
        "base_url": "https://opencode.ai/zen/go",
        "model_mapping": {"gpt-5": "gpt-5"},
    }
    assert "sk-opencode-secret" not in response.text
    assert "admin-secret" not in response.text

    preview_request = DummySub2ApiClient.requests[0]
    assert preview_request["method"] == "POST"
    assert preview_request["url"] == "https://sub.example/api/v1/admin/accounts/models/sync-upstream-preview"
    assert preview_request["json"] == {
        "platform": "openai",
        "type": "apikey",
        "base_url": "https://opencode.ai/zen/go",
        "api_key": "sk-opencode-secret",
    }

    create_request = DummySub2ApiClient.requests[1]
    assert create_request["method"] == "POST"
    assert create_request["url"] == "https://sub.example/api/v1/admin/accounts"
    assert create_request["json"]["name"] == "opencode-user@example.com"
    assert create_request["json"]["platform"] == "openai"
    assert create_request["json"]["type"] == "apikey"
    assert create_request["json"]["concurrency"] == 10
    assert create_request["json"]["group_ids"] == [2, 3]
    assert create_request["json"]["credentials"]["base_url"] == "https://opencode.ai/zen/go"
    assert create_request["json"]["credentials"]["api_key"] == "sk-opencode-secret"
    assert create_request["json"]["credentials"]["pool_mode"] is True
    assert create_request["json"]["credentials"]["model_mapping"] == {
        "gpt-5": "gpt-5",
        "gpt-5-mini": "gpt-5-mini",
    }
    assert create_request["json"]["extra"]["codex_image_generation_bridge"] is False
    assert "sk-opencode-secret" not in logs.text
    assert "admin-secret" not in logs.text


def test_opencode_go_import_sub2api_requires_groups(tmp_path, monkeypatch):
    db = setup_test_db(tmp_path, monkeypatch)
    configure_sub2api(db)
    account_id = db.upsert_opencode_go_account(
        {
            "name": "go-main",
            "email": "user@example.com",
            "password": "secret-password",
            "api_key": "sk-opencode-secret",
        }
    )

    with TestClient(app) as client:
        login(client)
        response = client.post(f"/api/opencode-go/accounts/{account_id}/import-sub2api", json={"group_ids": []})

    assert response.status_code == 400
    assert "请选择至少一个" in response.json()["message"]


def test_opencode_go_bulk_import_sub2api_skips_duplicate_names_in_group(tmp_path, monkeypatch):
    db = setup_test_db(tmp_path, monkeypatch)
    configure_sub2api(db)
    first_id = db.upsert_opencode_go_account(
        {
            "name": "first@example.com",
            "email": "first@example.com",
            "password": "secret-password",
            "api_key": "sk-first-secret",
        }
    )
    duplicate_id = db.upsert_opencode_go_account(
        {
            "name": "duplicate@example.com",
            "email": "duplicate@example.com",
            "password": "secret-password",
            "api_key": "sk-duplicate-secret",
        }
    )
    missing_key_id = db.upsert_opencode_go_account(
        {
            "name": "missing@example.com",
            "email": "missing@example.com",
            "password": "secret-password",
        }
    )
    DummySub2ApiClient.requests = []
    DummySub2ApiClient.responses = [
        DummySub2ApiResponse(
            {
                "code": 0,
                "data": {
                    "accounts": [
                        {"id": 201, "name": "opencode-duplicate@example.com", "platform": "openai", "group_ids": [8]},
                        {"id": 202, "name": "opencode-other@example.com", "platform": "openai", "group_ids": [9]},
                    ]
                },
            }
        ),
        DummySub2ApiResponse({"code": 0, "data": {"models": ["gpt-5", "gpt-5-mini"]}}),
        DummySub2ApiResponse(
            {
                "code": 0,
                "data": {
                    "id": 123,
                    "name": "opencode-first@example.com",
                    "credentials": {
                        "base_url": "https://opencode.ai/zen/go",
                        "api_key": "sk-first-secret",
                    },
                },
            }
        ),
    ]
    monkeypatch.setattr("app.services.sub2api_admin.httpx.AsyncClient", DummySub2ApiClient)

    with TestClient(app) as client:
        login(client)
        response = client.post(
            "/api/opencode-go/accounts/import-sub2api",
            json={"account_ids": [first_id, duplicate_id, missing_key_id, first_id], "group_ids": [8]},
        )
        logs = client.get("/api/logs")

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["skipped_count"] == 1
    assert payload["failed_count"] == 1
    assert payload["imported"][0]["name"] == "opencode-first@example.com"
    assert payload["skipped"][0]["id"] == duplicate_id
    assert payload["skipped"][0]["name"] == "opencode-duplicate@example.com"
    assert payload["failed"][0]["id"] == missing_key_id
    assert "尚未获取 API key" in payload["failed"][0]["message"]
    assert "sk-first-secret" not in response.text
    assert "sk-duplicate-secret" not in response.text
    assert "admin-secret" not in response.text
    assert "sk-first-secret" not in logs.text
    assert "sk-duplicate-secret" not in logs.text
    assert "admin-secret" not in logs.text

    list_request = DummySub2ApiClient.requests[0]
    assert list_request["method"] == "GET"
    assert list_request["url"] == "https://sub.example/api/v1/admin/accounts"
    assert list_request["params"]["platform"] == "openai"
    assert list_request["params"]["group_id"] == 8

    preview_requests = [request for request in DummySub2ApiClient.requests if request["url"].endswith("/models/sync-upstream-preview")]
    create_requests = [request for request in DummySub2ApiClient.requests if request["method"] == "POST" and request["url"] == "https://sub.example/api/v1/admin/accounts"]
    assert len(preview_requests) == 1
    assert len(create_requests) == 1
    assert preview_requests[0]["json"]["api_key"] == "sk-first-secret"
    assert create_requests[0]["json"]["name"] == "opencode-first@example.com"
    assert create_requests[0]["json"]["group_ids"] == [8]


def test_opencode_go_import_cpa_requires_config(tmp_path, monkeypatch):
    db = setup_test_db(tmp_path, monkeypatch)
    account_id = db.upsert_opencode_go_account(
        {
            "name": "go-main",
            "email": "user@example.com",
            "password": "secret-password",
            "api_key": "sk-opencode-secret",
        }
    )

    with TestClient(app) as client:
        login(client)
        response = client.post(f"/api/opencode-go/accounts/{account_id}/import-cpa")

    assert response.status_code == 400
    assert "CPA 站点地址" in response.json()["message"]


def test_opencode_go_import_cpa_requires_api_key(tmp_path, monkeypatch):
    db = setup_test_db(tmp_path, monkeypatch)
    configure_cpa(db)
    account_id = db.upsert_opencode_go_account(
        {
            "name": "go-main",
            "email": "user@example.com",
            "password": "secret-password",
        }
    )

    with TestClient(app) as client:
        login(client)
        response = client.post(f"/api/opencode-go/accounts/{account_id}/import-cpa")

    assert response.status_code == 400
    assert "尚未获取 API key" in response.json()["message"]


def test_opencode_go_import_cpa_requires_existing_account(tmp_path, monkeypatch):
    db = setup_test_db(tmp_path, monkeypatch)
    configure_cpa(db)

    with TestClient(app) as client:
        login(client)
        response = client.post("/api/opencode-go/accounts/999/import-cpa")

    assert response.status_code == 404
    assert "OpenCode Go 账号不存在" in response.json()["detail"]


def test_opencode_go_accounts_are_paginated_by_created_at_desc(tmp_path, monkeypatch):
    db = setup_test_db(tmp_path, monkeypatch)
    account_ids = []
    for index in range(25):
        account_id = db.upsert_opencode_go_account(
            {
                "name": f"user{index:02d}@example.com",
                "email": f"user{index:02d}@example.com",
                "password": "secret-password",
            }
        )
        account_ids.append(account_id)
    with db.connect() as conn:
        for index, account_id in enumerate(account_ids):
            conn.execute(
                "UPDATE opencode_go_accounts SET created_at = ?, updated_at = ? WHERE id = ?",
                (f"2026-01-{index + 1:02d}T00:00:00+00:00", f"2026-01-{index + 1:02d}T00:00:00+00:00", account_id),
            )

    with TestClient(app) as client:
        login(client)
        first_page = client.get("/api/opencode-go/accounts")
        second_page = client.get("/api/opencode-go/accounts", params={"page": 2})

    assert first_page.status_code == 200
    assert first_page.json()["pagination"]["page_size"] == 20
    assert first_page.json()["pagination"]["total"] == 25
    assert len(first_page.json()["accounts"]) == 20
    assert [account["email"] for account in first_page.json()["accounts"][:2]] == [
        "user24@example.com",
        "user23@example.com",
    ]
    assert first_page.json()["accounts"][0]["created_at"] == "2026-01-25T00:00:00+00:00"
    assert len(second_page.json()["accounts"]) == 5
    assert second_page.json()["accounts"][0]["email"] == "user04@example.com"


def test_opencode_go_accounts_support_case_insensitive_email_search(tmp_path, monkeypatch):
    db = setup_test_db(tmp_path, monkeypatch)
    for email in ("alice@example.com", "ALICE+work@example.com", "bob@example.com"):
        db.upsert_opencode_go_account({"email": email, "password": "secret-password"})

    with TestClient(app) as client:
        login(client)
        response = client.get("/api/opencode-go/accounts", params={"email": "alice"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["pagination"]["total"] == 2
    assert {account["email"] for account in payload["accounts"]} == {"alice@example.com", "ALICE+work@example.com"}


def test_opencode_go_accounts_filter_by_displayed_status(tmp_path, monkeypatch):
    db = setup_test_db(tmp_path, monkeypatch)
    account_ids = {
        status: db.upsert_opencode_go_account({"email": f"{status}@example.com", "password": "secret-password"})
        for status in ("never", "logged_in", "valid", "invalid", "deleted")
    }
    db.update_opencode_go_session(account_ids["logged_in"], {"cookies": []})
    db.update_opencode_go_result(account_ids["valid"], {"is_valid": True})
    db.update_opencode_go_result(account_ids["invalid"], {"is_valid": False, "error": "query failed"})
    db.update_opencode_go_result(account_ids["deleted"], {"is_valid": True})
    db.update_opencode_go_cpa_state(account_ids["deleted"], provider_deleted=True)

    with TestClient(app) as client:
        login(client)
        responses = {
            status: client.get("/api/opencode-go/accounts", params={"status": status})
            for status in account_ids
        }

    for status, response in responses.items():
        assert response.status_code == 200
        payload = response.json()
        assert payload["pagination"]["total"] == 1
        assert [account["email"] for account in payload["accounts"]] == [f"{status}@example.com"]


def test_opencode_go_accounts_filter_usage_at_least_99_percent(tmp_path, monkeypatch):
    db = setup_test_db(tmp_path, monkeypatch)
    usage_by_email = {
        "both@example.com": ({"usagePercent": 99}, {"usage_percent": 100}),
        "weekly@example.com": ({"usage_percent": 99.5}, {"usagePercent": 98.9}),
        "monthly@example.com": ({"usagePercent": 20}, {"usage_percent": 99}),
        "below@example.com": ({"usagePercent": 98.9}, {"usagePercent": 98.9}),
    }
    for email, (weekly_usage, monthly_usage) in usage_by_email.items():
        account_id = db.upsert_opencode_go_account({"email": email, "password": "secret-password"})
        db.update_opencode_go_result(
            account_id,
            {"is_valid": True, "weekly_usage": weekly_usage, "monthly_usage": monthly_usage},
        )
    invalid_id = db.upsert_opencode_go_account({"email": "invalid@example.com", "password": "secret-password"})
    db.upsert_opencode_go_account({"email": "missing@example.com", "password": "secret-password"})
    with db.connect() as conn:
        conn.execute(
            "UPDATE opencode_go_accounts SET last_weekly_usage = ?, last_monthly_usage = ? WHERE id = ?",
            ("not-json", "not-json", invalid_id),
        )

    with TestClient(app) as client:
        login(client)
        weekly = client.get("/api/opencode-go/accounts", params={"weekly_usage_gte_99": "true"})
        monthly = client.get("/api/opencode-go/accounts", params={"monthlyUsageGte99": "true"})
        both = client.get(
            "/api/opencode-go/accounts",
            params={"weekly_usage_gte_99": "true", "monthly_usage_gte_99": "true"},
        )

    assert weekly.status_code == 200
    assert weekly.json()["pagination"]["total"] == 2
    assert {account["email"] for account in weekly.json()["accounts"]} == {
        "both@example.com",
        "weekly@example.com",
    }
    assert monthly.status_code == 200
    assert monthly.json()["pagination"]["total"] == 2
    assert {account["email"] for account in monthly.json()["accounts"]} == {
        "both@example.com",
        "monthly@example.com",
    }
    assert both.status_code == 200
    assert both.json()["pagination"]["total"] == 1
    assert [account["email"] for account in both.json()["accounts"]] == ["both@example.com"]


def test_opencode_go_import_cpa_upserts_openai_provider_with_all_models(tmp_path, monkeypatch):
    db = setup_test_db(tmp_path, monkeypatch)
    configure_cpa(db)
    account_id = db.upsert_opencode_go_account(
        {
            "name": "go-main",
            "email": "user@example.com",
            "password": "secret-password",
            "api_key": "sk-opencode-secret",
        }
    )
    db.update_opencode_go_cpa_state(
        account_id,
        provider_disabled=True,
        provider_deleted=True,
        deleted_at="2026-07-18T00:00:00+00:00",
        error="old error",
    )
    DummySub2ApiClient.requests = []
    DummySub2ApiClient.responses = [
        DummySub2ApiResponse({"status_code": 200, "body": {"data": [{"id": "gpt-5"}, {"id": "gpt-5-mini"}, {"id": "gpt-5"}]}}),
        DummySub2ApiResponse(
            {
                "openai-compatibility": [
                    {
                        "name": "user@example.com",
                        "disabled": True,
                        "headers": {"X-Keep": "1"},
                        "models": [{"name": "old-model"}],
                    },
                    {"name": "other@example.com", "base-url": "https://other.example/v1"},
                ]
            }
        ),
        DummySub2ApiResponse({"ok": True}),
    ]
    monkeypatch.setattr("app.services.cpa_admin.httpx.AsyncClient", DummySub2ApiClient)

    with TestClient(app) as client:
        login(client)
        response = client.post(f"/api/opencode-go/accounts/{account_id}/import-cpa")
        logs = client.get("/api/logs")

    assert response.status_code == 200
    payload = response.json()
    assert payload["name"] == "user@example.com"
    assert payload["base_url"] == "https://opencode.ai/zen/go/v1"
    assert payload["models"] == ["gpt-5", "gpt-5-mini"]
    assert payload["model_count"] == 2
    assert "sk-opencode-secret" not in response.text
    assert "cpa-secret" not in response.text

    model_request = DummySub2ApiClient.requests[0]
    assert model_request["method"] == "POST"
    assert model_request["url"] == "https://cpa.example/v0/management/api-call"
    assert model_request["headers"]["Authorization"] == "Bearer cpa-secret"
    assert model_request["json"] == {
        "method": "GET",
        "url": "https://opencode.ai/zen/go/v1/models",
        "header": {"Authorization": "Bearer sk-opencode-secret"},
    }

    config_request = DummySub2ApiClient.requests[1]
    assert config_request["method"] == "GET"
    assert config_request["url"] == "https://cpa.example/v0/management/config"

    save_request = DummySub2ApiClient.requests[2]
    assert save_request["method"] == "PUT"
    assert save_request["url"] == "https://cpa.example/v0/management/openai-compatibility"
    saved_providers = save_request["json"]
    assert len(saved_providers) == 2
    assert saved_providers[0] == {
        "name": "user@example.com",
        "disabled": False,
        "headers": {"X-Keep": "1"},
        "base-url": "https://opencode.ai/zen/go/v1",
        "api-key-entries": [{"api-key": "sk-opencode-secret"}],
        "models": [{"name": "gpt-5"}, {"name": "gpt-5-mini"}],
    }
    assert saved_providers[1]["name"] == "other@example.com"
    account = db.get_opencode_go_account(account_id)
    assert account["cpa_provider_disabled"] == 0
    assert account["cpa_provider_deleted"] == 0
    assert account["cpa_deleted_at"] is None
    assert account["cpa_last_action_error"] is None
    assert "sk-opencode-secret" not in logs.text
    assert "cpa-secret" not in logs.text


@pytest.mark.asyncio
async def test_cpa_set_provider_disabled_noop_when_already_target_state(monkeypatch):
    DummySub2ApiClient.requests = []
    DummySub2ApiClient.responses = [
        DummySub2ApiResponse(
            {
                "openai-compatibility": [
                    {"name": "ready@example.com", "disabled": True},
                    {"name": "other@example.com", "disabled": False},
                ]
            }
        ),
    ]
    monkeypatch.setattr("app.services.cpa_admin.httpx.AsyncClient", DummySub2ApiClient)

    client = CpaAdminClient("https://cpa.example", "cpa-secret")
    result = await client.set_openai_provider_disabled("ready@example.com", True)

    assert result == {"name": "ready@example.com", "disabled": True, "changed": False}
    assert len(DummySub2ApiClient.requests) == 1
    assert DummySub2ApiClient.requests[0]["method"] == "GET"
    assert DummySub2ApiClient.requests[0]["url"] == "https://cpa.example/v0/management/openai-compatibility"


@pytest.mark.asyncio
async def test_cpa_provider_test_checks_all_keys_and_accepts_any_success(monkeypatch):
    DummySub2ApiClient.requests = []
    DummySub2ApiClient.responses = [
        DummySub2ApiResponse(
            {
                "openai-compatibility": [
                    {
                        "name": "multi@example.com",
                        "base-url": "https://opencode.ai/zen/go/v1",
                        "headers": {"X-Custom": "kept"},
                        "api-key-entries": [
                            {"api-key": "sk-failed", "auth-index": "auth-failed"},
                            {"api-key": "sk-working", "auth-index": "auth-working"},
                        ],
                        "models": [{"name": "gpt-test"}],
                    }
                ]
            }
        ),
        DummySub2ApiResponse({"status_code": 401, "body": '{"error":{"message":"invalid key"}}'}),
        DummySub2ApiResponse({"status_code": 200, "body": '{"choices":[]}'}),
    ]
    monkeypatch.setattr("app.services.cpa_admin.httpx.AsyncClient", DummySub2ApiClient)

    client = CpaAdminClient("https://cpa.example", "cpa-secret")
    result = await client.test_openai_provider("multi@example.com")

    assert result["healthy"] is True
    assert result["tested_key_count"] == 2
    assert result["success_count"] == 1
    assert result["failure_count"] == 1
    assert [request["method"] for request in DummySub2ApiClient.requests] == ["GET", "POST", "POST"]
    first_test = DummySub2ApiClient.requests[1]["json"]
    second_test = DummySub2ApiClient.requests[2]["json"]
    assert first_test["url"] == "https://opencode.ai/zen/go/v1/chat/completions"
    assert first_test["auth_index"] == "auth-failed"
    assert second_test["auth_index"] == "auth-working"
    assert first_test["header"] == {
        "X-Custom": "kept",
        "Content-Type": "application/json",
        "Authorization": "Bearer sk-failed",
    }
    assert json.loads(first_test["data"]) == {
        "model": "gpt-test",
        "messages": [{"role": "user", "content": "Hi"}],
        "stream": False,
        "max_tokens": 5,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(("weekly_percent", "should_test"), [(98.9, False), (99, True)])
async def test_opencode_go_weekly_threshold_boundary(tmp_path, monkeypatch, weekly_percent, should_test):
    db = setup_test_db(tmp_path, monkeypatch)
    configure_cpa(db)
    account_id = db.upsert_opencode_go_account(
        {"email": "weekly-boundary@example.com", "password": "secret-password", "is_enabled": True}
    )

    async def fake_refresh(*args, **kwargs):
        return {
            "is_valid": True,
            "rolling_usage": {"usagePercent": 10},
            "weekly_usage": {"usagePercent": weekly_percent},
            "monthly_usage": {"usagePercent": 20},
        }

    DummySub2ApiClient.requests = []
    DummySub2ApiClient.responses = []
    if should_test:
        DummySub2ApiClient.responses = [
            DummySub2ApiResponse(
                {
                    "openai-compatibility": [
                        {
                            "name": "weekly-boundary@example.com",
                            "base-url": "https://opencode.ai/zen/go/v1",
                            "api-key-entries": [{"api-key": "sk-working"}],
                            "models": [{"name": "gpt-5"}],
                        }
                    ]
                }
            ),
            DummySub2ApiResponse({"status_code": 200, "body": {"choices": []}}),
        ]
    monkeypatch.setattr("app.services.scheduler.refresh_opencode_go_account", fake_refresh)
    monkeypatch.setattr("app.services.cpa_admin.httpx.AsyncClient", DummySub2ApiClient)

    await query_opencode_go_for_account(db, account_id)

    expected_methods = ["GET", "POST"] if should_test else []
    assert [request["method"] for request in DummySub2ApiClient.requests] == expected_methods
    assert db.get_opencode_go_account(account_id)["cpa_provider_disabled"] is None


@pytest.mark.asyncio
async def test_opencode_go_refresh_tests_five_hour_limit_then_disables_and_reenables(tmp_path, monkeypatch):
    db = setup_test_db(tmp_path, monkeypatch)
    configure_cpa(db)
    account_id = db.upsert_opencode_go_account(
        {
            "email": "auto-disable@example.com",
            "password": "secret-password",
            "is_enabled": True,
        }
    )

    async def fake_refresh(*args, **kwargs):
        return {
            "is_valid": True,
            "workspace_id": "ws_1",
            "rolling_usage": {"usagePercent": 99, "resetInSec": 60},
            "weekly_usage": {"usagePercent": 98.9, "resetInSec": 120},
            "monthly_usage": {"usagePercent": 50, "resetInSec": 180},
        }

    DummySub2ApiClient.requests = []
    DummySub2ApiClient.responses = [
        DummySub2ApiResponse(
            {
                "openai-compatibility": [
                    {
                        "name": "auto-disable@example.com",
                        "disabled": False,
                        "base-url": "https://opencode.ai/zen/go/v1",
                        "api-key-entries": [{"api-key": "sk-failed", "auth-index": "auth-1"}],
                        "models": [{"name": "gpt-5"}],
                    }
                ]
            }
        ),
        DummySub2ApiResponse({"status_code": 429, "body": {"error": {"message": "quota exhausted"}}}),
        DummySub2ApiResponse({"openai-compatibility": [{"name": "auto-disable@example.com", "disabled": False}]}),
        DummySub2ApiResponse({"ok": True}),
        DummySub2ApiResponse(
            {
                "openai-compatibility": [
                    {
                        "name": "auto-disable@example.com",
                        "disabled": True,
                        "base-url": "https://opencode.ai/zen/go/v1",
                        "api-key-entries": [{"api-key": "sk-recovered", "auth-index": "auth-1"}],
                        "models": [{"name": "gpt-5"}],
                    }
                ]
            }
        ),
        DummySub2ApiResponse({"status_code": 200, "body": {"choices": []}}),
        DummySub2ApiResponse({"openai-compatibility": [{"name": "auto-disable@example.com", "disabled": True}]}),
        DummySub2ApiResponse({"ok": True}),
    ]
    monkeypatch.setattr("app.services.scheduler.refresh_opencode_go_account", fake_refresh)
    monkeypatch.setattr("app.services.cpa_admin.httpx.AsyncClient", DummySub2ApiClient)

    await query_opencode_go_for_account(db, account_id)
    after_first = db.get_opencode_go_account(account_id)
    await query_opencode_go_for_account(db, account_id)
    after_second = db.get_opencode_go_account(account_id)

    assert after_first["cpa_provider_disabled"] == 1
    assert after_first["cpa_reenable_pending"] == 0
    assert after_second["cpa_provider_disabled"] == 0
    assert [request["method"] for request in DummySub2ApiClient.requests] == [
        "GET",
        "POST",
        "GET",
        "PATCH",
        "GET",
        "POST",
        "GET",
        "PATCH",
    ]
    assert DummySub2ApiClient.requests[3]["json"] == {"index": 0, "value": {"disabled": True}}
    assert DummySub2ApiClient.requests[7]["json"] == {"index": 0, "value": {"disabled": False}}
    assert any("CPA 自动停用成功" in log["message"] for log in db.list_logs(category="opencode-go"))
    assert any("CPA 自动启用成功" in log["message"] for log in db.list_logs(category="opencode-go"))


@pytest.mark.asyncio
async def test_opencode_go_refresh_auto_enables_cpa_after_recovery_and_retries_failure(tmp_path, monkeypatch):
    db = setup_test_db(tmp_path, monkeypatch)
    configure_cpa(db)
    account_id = db.upsert_opencode_go_account(
        {
            "email": "auto-enable@example.com",
            "password": "secret-password",
            "is_enabled": True,
        }
    )
    db.update_opencode_go_result(
        account_id,
        {
            "is_valid": True,
            "rolling_usage": {"usagePercent": 99, "resetInSec": 60},
            "weekly_usage": {"usagePercent": 99, "resetInSec": 120},
            "monthly_usage": {"usagePercent": 80, "resetInSec": 180},
        },
    )
    db.update_opencode_go_cpa_state(account_id, provider_disabled=True, clear_error=True)

    async def fake_refresh(*args, **kwargs):
        return {
            "is_valid": True,
            "workspace_id": "ws_1",
            "rolling_usage": {"usagePercent": 10, "resetInSec": 60},
            "weekly_usage": {"usagePercent": 20, "resetInSec": 120},
            "monthly_usage": {"usagePercent": 80, "resetInSec": 180},
        }

    DummySub2ApiClient.requests = []
    DummySub2ApiClient.responses = [
        DummySub2ApiResponse({"openai-compatibility": [{"name": "auto-enable@example.com", "disabled": True}]}),
        DummySub2ApiResponse({"message": "save failed"}, status_code=500),
        DummySub2ApiResponse({"openai-compatibility": [{"name": "auto-enable@example.com", "disabled": True}]}),
        DummySub2ApiResponse({"ok": True}),
    ]
    monkeypatch.setattr("app.services.scheduler.refresh_opencode_go_account", fake_refresh)
    monkeypatch.setattr("app.services.cpa_admin.httpx.AsyncClient", DummySub2ApiClient)

    await query_opencode_go_for_account(db, account_id)
    after_failure = db.get_opencode_go_account(account_id)
    await query_opencode_go_for_account(db, account_id)
    after_success = db.get_opencode_go_account(account_id)
    requests_after_success = list(DummySub2ApiClient.requests)
    DummySub2ApiClient.requests = []
    await query_opencode_go_for_account(db, account_id)

    assert after_failure["cpa_provider_disabled"] == 1
    assert after_failure["cpa_reenable_pending"] == 1
    assert "save failed" in after_failure["cpa_last_action_error"]
    assert after_success["cpa_provider_disabled"] == 0
    assert after_success["cpa_reenable_pending"] == 0
    assert after_success["cpa_last_action_error"] is None
    assert [request["method"] for request in requests_after_success] == ["GET", "PATCH", "GET", "PATCH"]
    assert requests_after_success[-1]["json"] == {"index": 0, "value": {"disabled": False}}
    assert DummySub2ApiClient.requests == []
    logs = db.list_logs(category="opencode-go")
    assert any("CPA 自动启用失败" in log["message"] for log in logs)
    assert any("CPA 自动启用成功" in log["message"] for log in logs)


@pytest.mark.asyncio
async def test_opencode_go_refresh_reenables_auto_disabled_cpa_when_monthly_test_passes(tmp_path, monkeypatch):
    db = setup_test_db(tmp_path, monkeypatch)
    configure_cpa(db)
    account_id = db.upsert_opencode_go_account(
        {
            "email": "monthly-full@example.com",
            "password": "secret-password",
            "is_enabled": True,
        }
    )
    db.update_opencode_go_result(
        account_id,
        {
            "is_valid": True,
            "rolling_usage": {"usagePercent": 20, "resetInSec": 60},
            "weekly_usage": {"usagePercent": 99, "resetInSec": 120},
            "monthly_usage": {"usagePercent": 99, "resetInSec": 180},
        },
    )
    db.update_opencode_go_cpa_state(account_id, provider_disabled=True, clear_error=True)

    async def fake_refresh(*args, **kwargs):
        return {
            "is_valid": True,
            "workspace_id": "ws_1",
            "rolling_usage": {"usagePercent": 20, "resetInSec": 60},
            "weekly_usage": {"usagePercent": 20, "resetInSec": 120},
            "monthly_usage": {"usagePercent": 99, "resetInSec": 180},
        }

    DummySub2ApiClient.requests = []
    DummySub2ApiClient.responses = [
        DummySub2ApiResponse(
            {
                "openai-compatibility": [
                    {
                        "name": "monthly-full@example.com",
                        "disabled": True,
                        "base-url": "https://opencode.ai/zen/go/v1",
                        "api-key-entries": [{"api-key": "sk-working"}],
                        "models": [{"name": "gpt-5"}],
                    }
                ]
            }
        ),
        DummySub2ApiResponse({"status_code": 200, "body": {"choices": []}}),
        DummySub2ApiResponse({"openai-compatibility": [{"name": "monthly-full@example.com", "disabled": True}]}),
        DummySub2ApiResponse({"ok": True}),
    ]
    monkeypatch.setattr("app.services.scheduler.refresh_opencode_go_account", fake_refresh)
    monkeypatch.setattr("app.services.cpa_admin.httpx.AsyncClient", DummySub2ApiClient)

    await query_opencode_go_for_account(db, account_id)

    account = db.get_opencode_go_account(account_id)
    assert account["cpa_provider_disabled"] == 0
    assert account["cpa_reenable_pending"] == 0
    assert [request["method"] for request in DummySub2ApiClient.requests] == ["GET", "POST", "GET", "PATCH"]


@pytest.mark.asyncio
async def test_opencode_go_monthly_test_failure_disables_when_auto_delete_is_off(tmp_path, monkeypatch):
    db = setup_test_db(tmp_path, monkeypatch)
    configure_cpa(db)
    account_id = db.upsert_opencode_go_account(
        {"email": "monthly-disable@example.com", "password": "secret-password", "is_enabled": True}
    )

    async def fake_refresh(*args, **kwargs):
        return {
            "is_valid": True,
            "rolling_usage": {"usagePercent": 20},
            "weekly_usage": {"usagePercent": 30},
            "monthly_usage": {"usagePercent": 99},
        }

    provider = {
        "name": "monthly-disable@example.com",
        "disabled": False,
        "base-url": "https://opencode.ai/zen/go/v1",
        "api-key-entries": [{"api-key": "sk-failed"}],
        "models": [{"name": "gpt-5"}],
    }
    DummySub2ApiClient.requests = []
    DummySub2ApiClient.responses = [
        DummySub2ApiResponse({"openai-compatibility": [provider]}),
        DummySub2ApiResponse({"status_code": 401, "body": {"error": "expired"}}),
        DummySub2ApiResponse({"openai-compatibility": [provider]}),
        DummySub2ApiResponse({"ok": True}),
    ]
    monkeypatch.setattr("app.services.scheduler.refresh_opencode_go_account", fake_refresh)
    monkeypatch.setattr("app.services.cpa_admin.httpx.AsyncClient", DummySub2ApiClient)

    await query_opencode_go_for_account(db, account_id)

    account = db.get_opencode_go_account(account_id)
    assert db.get_setting("opencode_go_cpa_auto_delete_enabled") == "0"
    assert account["cpa_provider_disabled"] == 1
    assert account["cpa_provider_deleted"] == 0
    assert [request["method"] for request in DummySub2ApiClient.requests] == ["GET", "POST", "GET", "PATCH"]


@pytest.mark.asyncio
async def test_opencode_go_monthly_test_failure_deletes_and_later_skips_cpa(tmp_path, monkeypatch):
    db = setup_test_db(tmp_path, monkeypatch)
    configure_cpa(db)
    db.set_setting("opencode_go_cpa_auto_delete_enabled", "1")
    account_id = db.upsert_opencode_go_account(
        {"email": "monthly-delete@example.com", "password": "secret-password", "is_enabled": True}
    )
    refresh_calls = []

    async def fake_refresh(*args, **kwargs):
        refresh_calls.append("refresh")
        return {
            "is_valid": True,
            "rolling_usage": {"usagePercent": 20},
            "weekly_usage": {"usagePercent": 99},
            "monthly_usage": {"usagePercent": 100},
        }

    provider = {
        "name": "monthly-delete@example.com",
        "disabled": False,
        "base-url": "https://opencode.ai/zen/go/v1",
        "api-key-entries": [{"api-key": "sk-failed"}],
        "models": [{"name": "gpt-5"}],
    }
    DummySub2ApiClient.requests = []
    DummySub2ApiClient.responses = [
        DummySub2ApiResponse({"openai-compatibility": [provider]}),
        DummySub2ApiResponse({"status_code": 429, "body": {"error": "quota exhausted"}}),
        DummySub2ApiResponse({"openai-compatibility": [provider]}),
        DummySub2ApiResponse({"status": "ok"}),
    ]
    monkeypatch.setattr("app.services.scheduler.refresh_opencode_go_account", fake_refresh)
    monkeypatch.setattr("app.services.cpa_admin.httpx.AsyncClient", DummySub2ApiClient)

    await query_opencode_go_for_account(db, account_id)
    deleted = db.get_opencode_go_account(account_id)
    first_requests = list(DummySub2ApiClient.requests)
    DummySub2ApiClient.requests = []
    await query_opencode_go_for_account(db, account_id)

    assert deleted["cpa_provider_deleted"] == 1
    assert deleted["cpa_provider_disabled"] == 0
    assert deleted["cpa_deleted_at"]
    assert deleted["is_enabled"] == 1
    public = public_opencode_go_account(deleted)
    assert public["cpa_provider_deleted"] is True
    assert public["cpaProviderDeleted"] is True
    assert public["cpaDeletedAt"] == deleted["cpa_deleted_at"]
    assert [request["method"] for request in first_requests] == ["GET", "POST", "GET", "DELETE"]
    assert first_requests[-1]["params"] == {"name": "monthly-delete@example.com"}
    assert DummySub2ApiClient.requests == []
    assert refresh_calls == ["refresh", "refresh"]


@pytest.mark.asyncio
async def test_opencode_go_monthly_delete_failure_does_not_mark_deleted(tmp_path, monkeypatch):
    db = setup_test_db(tmp_path, monkeypatch)
    configure_cpa(db)
    db.set_setting("opencode_go_cpa_auto_delete_enabled", "1")
    account_id = db.upsert_opencode_go_account(
        {"email": "delete-failed@example.com", "password": "secret-password", "is_enabled": True}
    )

    async def fake_refresh(*args, **kwargs):
        return {
            "is_valid": True,
            "rolling_usage": {"usagePercent": 20},
            "weekly_usage": {"usagePercent": 20},
            "monthly_usage": {"usagePercent": 99},
        }

    provider = {
        "name": "delete-failed@example.com",
        "base-url": "https://opencode.ai/zen/go/v1",
        "api-key-entries": [{"api-key": "sk-failed"}],
        "models": [{"name": "gpt-5"}],
    }
    DummySub2ApiClient.requests = []
    DummySub2ApiClient.responses = [
        DummySub2ApiResponse({"openai-compatibility": [provider]}),
        DummySub2ApiResponse({"status_code": 500, "body": {"error": "upstream failed"}}),
        DummySub2ApiResponse({"openai-compatibility": [provider]}),
        DummySub2ApiResponse({"error": "save failed"}, status_code=500),
    ]
    monkeypatch.setattr("app.services.scheduler.refresh_opencode_go_account", fake_refresh)
    monkeypatch.setattr("app.services.cpa_admin.httpx.AsyncClient", DummySub2ApiClient)

    await query_opencode_go_for_account(db, account_id)

    account = db.get_opencode_go_account(account_id)
    assert account["cpa_provider_deleted"] == 0
    assert account["cpa_deleted_at"] is None
    assert "save failed" in account["cpa_last_action_error"]
    assert any("CPA 自动删除失败" in log["message"] for log in db.list_logs(category="opencode-go"))


@pytest.mark.asyncio
async def test_opencode_go_refresh_cpa_missing_provider_logs_and_keeps_refresh_success(tmp_path, monkeypatch):
    db = setup_test_db(tmp_path, monkeypatch)
    configure_cpa(db)
    account_id = db.upsert_opencode_go_account(
        {
            "email": "missing-provider@example.com",
            "password": "secret-password",
            "is_enabled": True,
        }
    )

    async def fake_refresh(*args, **kwargs):
        return {
            "is_valid": True,
            "workspace_id": "ws_1",
            "weekly_usage": {"usagePercent": 100, "resetInSec": 120},
            "monthly_usage": {"usagePercent": 20, "resetInSec": 180},
        }

    DummySub2ApiClient.requests = []
    DummySub2ApiClient.responses = [DummySub2ApiResponse({"openai-compatibility": []})]
    monkeypatch.setattr("app.services.scheduler.refresh_opencode_go_account", fake_refresh)
    monkeypatch.setattr("app.services.cpa_admin.httpx.AsyncClient", DummySub2ApiClient)

    result = await query_opencode_go_for_account(db, account_id)
    account = db.get_opencode_go_account(account_id)

    assert result["is_valid"] is True
    assert account["last_status"] == "valid"
    assert account["cpa_provider_disabled"] is None
    assert "CPA 中未找到邮箱 provider" in account["cpa_last_action_error"]
    assert [request["method"] for request in DummySub2ApiClient.requests] == ["GET"]
    assert any("CPA 自动测试无法执行" in log["message"] for log in db.list_logs(category="opencode-go"))


def test_opencode_go_bulk_import_cpa_imports_selected_accounts(tmp_path, monkeypatch):
    db = setup_test_db(tmp_path, monkeypatch)
    configure_cpa(db)
    first_id = db.upsert_opencode_go_account(
        {
            "name": "first@example.com",
            "email": "first@example.com",
            "password": "secret-password",
            "api_key": "sk-first-secret",
        }
    )
    missing_key_id = db.upsert_opencode_go_account(
        {
            "name": "missing@example.com",
            "email": "missing@example.com",
            "password": "secret-password",
        }
    )
    second_id = db.upsert_opencode_go_account(
        {
            "name": "second@example.com",
            "email": "second@example.com",
            "password": "secret-password",
            "api_key": "sk-second-secret",
        }
    )
    DummySub2ApiClient.requests = []
    DummySub2ApiClient.responses = [
        DummySub2ApiResponse({"status_code": 200, "body": {"data": [{"id": "gpt-5"}]}}),
        DummySub2ApiResponse({"openai-compatibility": []}),
        DummySub2ApiResponse({"ok": True}),
        DummySub2ApiResponse({"status_code": 200, "body": {"data": [{"id": "gpt-5-mini"}]}}),
        DummySub2ApiResponse({"openai-compatibility": []}),
        DummySub2ApiResponse({"ok": True}),
    ]
    monkeypatch.setattr("app.services.cpa_admin.httpx.AsyncClient", DummySub2ApiClient)

    with TestClient(app) as client:
        login(client)
        response = client.post(
            "/api/opencode-go/accounts/import-cpa",
            json={"account_ids": [first_id, missing_key_id, second_id, first_id]},
        )
        logs = client.get("/api/logs")

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 2
    assert payload["failed_count"] == 1
    assert [item["name"] for item in payload["imported"]] == ["first@example.com", "second@example.com"]
    assert payload["failed"][0]["id"] == missing_key_id
    assert "尚未获取 API key" in payload["failed"][0]["message"]
    model_requests = [request for request in DummySub2ApiClient.requests if request["url"].endswith("/api-call")]
    assert len(model_requests) == 2
    assert model_requests[0]["json"]["header"]["Authorization"] == "Bearer sk-first-secret"
    assert model_requests[1]["json"]["header"]["Authorization"] == "Bearer sk-second-secret"
    assert "sk-first-secret" not in response.text
    assert "sk-second-secret" not in response.text
    assert "cpa-secret" not in response.text
    assert "sk-first-secret" not in logs.text
    assert "sk-second-secret" not in logs.text
    assert "cpa-secret" not in logs.text


# ---------------- 邀请奖励 referral ----------------

def test_parse_referral_payload_classifies_status():
    # 已使用：rewards[] 里有 status='applied'
    applied = {"referralCode": "GXM0E6KDFD", "hasReferral": True, "rewardAmount": 500, "rewards": [{"id": "ref_1", "source": "invitee", "status": "applied", "amount": 500, "timeCreated": "2026-07-14", "timeApplied": "2026-07-19"}]}
    res = parse_referral_payload(applied)
    assert res["has_reward"] is True
    assert res["claimed"] is True
    assert res["reward"]["referralCode"] == "GXM0E6KDFD"
    assert res["reward"]["status"] == "applied"
    assert res["rewards"][0]["status"] == "applied"
    # 可领：status='available'
    available = {"referralCode": "ABC", "hasReferral": True, "rewardAmount": 100, "rewards": [{"id": "r", "status": "available", "amount": 100}]}
    avres = parse_referral_payload(available)
    assert avres["has_reward"] is True
    assert avres["claimed"] is False
    # 无推荐：hasReferral=false
    none = {"referralCode": "ABC", "hasReferral": False, "rewardAmount": 0, "rewards": []}
    noneres = parse_referral_payload(none)
    assert noneres["has_reward"] is False
    assert noneres["claimed"] is None
    # 未知结构
    unknown = parse_referral_payload({"unrelated": "x"})
    assert unknown["has_reward"] is None
    assert unknown["claimed"] is None


def test_parse_referral_payload_from_grid_text():
    # 真实 server-fn grid 序列化文本（你的样本）
    text = ';0x00000180;((self.$R=self.$R||{})["server-fn:2"]=[],($R=>$R[0]={referralCode:"GXM0E6KDFD",hasReferral:!0,rewardAmount:500,rewards:$R[1]=[$R[2]={id:"ref_01KXFJ1G1N1SR5EZ327XN2P0QZ",source:"invitee",status:"applied",email:"blythedickersongokpn@zjeb.us",amount:500,timeCreated:$R[3]=new Date("2026-07-14T05:38:58.000Z"),timeApplied:$R[4]=new Date("2026-07-19T11:09:32.000Z")}]})($R["server-fn:2"]))'
    res = parse_referral_payload(text)
    assert res["has_reward"] is True
    assert res["claimed"] is True  # applied = 已使用
    assert res["reward"]["referralCode"] == "GXM0E6KDFD"
    assert res["reward"]["status"] == "applied"
    assert res["reward"]["email"] == "blythedickersongokpn@zjeb.us"
    assert res["reward"]["timeApplied"] == "2026-07-19T11:09:32.000Z"
    assert res["rewards"][0]["amount"] == 500


def test_extract_referral_reference_id():
    assert extract_referral_reference_id('queryGoReferral_query = createServerReference("2a0b2fef5fd2ec9eff0cb5d4955e4ada4eece21fac85591ed4c09630168d4844"') == "2a0b2fef5fd2ec9eff0cb5d4955e4ada4eece21fac85591ed4c09630168d4844"
    with pytest.raises(ValueError):
        extract_referral_reference_id("no match here")


def test_opencode_go_referral_endpoint_updates_columns(tmp_path, monkeypatch):
    db = setup_test_db(tmp_path, monkeypatch)

    async def fake_query(account, secret_key, timeout, log, referral_query_js_url=None, referral_query_server_id=None):
        return {"is_valid": True, "has_reward": True, "claimed": False, "reward": {"status": "available", "amount": 10}, "raw": {}, "checked_at": "2026-01-01T00:00:00Z"}

    monkeypatch.setattr("app.main.query_referral_for_account", fake_query)

    with TestClient(app) as client:
        login(client)
        created = client.post("/api/opencode-go/accounts", json={"name": "r@example.com", "email": "r@example.com", "password": "pw"})
        account_id = created.json()["id"]
        response = client.post(f"/api/opencode-go/accounts/{account_id}/referral")

    body = response.json()
    assert response.status_code == 200
    assert body["ok"] is True
    assert body["referral"]["has_reward"] is True
    assert body["referral"]["claimed"] is False
    account = db.get_opencode_go_account(account_id)
    assert account["referral_has_reward"] == 1
    assert account["referral_claimed"] == 0
    # reward json 已加密
    stored = json.loads(decrypt_value(account["referral_reward_json"], "test-key"))
    assert stored["status"] == "available"
    assert account["last_error"] is None


def test_opencode_go_referral_endpoint_records_error(tmp_path, monkeypatch):
    db = setup_test_db(tmp_path, monkeypatch)

    async def fake_query(account, secret_key, timeout, log, referral_query_js_url=None, referral_query_server_id=None):
        return {"is_valid": False, "has_reward": None, "claimed": None, "reward": {}, "invalid_message": "登录态失效", "checked_at": "2026-01-01T00:00:00Z"}

    monkeypatch.setattr("app.main.query_referral_for_account", fake_query)

    with TestClient(app) as client:
        login(client)
        created = client.post("/api/opencode-go/accounts", json={"name": "r2@example.com", "email": "r2@example.com", "password": "pw"})
        account_id = created.json()["id"]
        response = client.post(f"/api/opencode-go/accounts/{account_id}/referral")

    body = response.json()
    assert body["ok"] is False
    assert "登录态失效" in body["message"]
    account = db.get_opencode_go_account(account_id)
    assert account["last_error"] == "登录态失效"


def test_opencode_go_referral_claim_endpoint(tmp_path, monkeypatch):
    db = setup_test_db(tmp_path, monkeypatch)

    async def fake_claim(account, secret_key, timeout, log, referral_query_js_url=None, referral_query_server_id=None, referral_action_server_id=None):
        return {"is_valid": True, "claimed": True, "message": "领取成功", "reward": {"status": "claimed", "amount": 10}, "raw": {}}

    monkeypatch.setattr("app.main.claim_referral_reward_for_account", fake_claim)

    with TestClient(app) as client:
        login(client)
        created = client.post("/api/opencode-go/accounts", json={"name": "c@example.com", "email": "c@example.com", "password": "pw"})
        account_id = created.json()["id"]
        response = client.post(f"/api/opencode-go/accounts/{account_id}/referral/claim")

    body = response.json()
    assert response.status_code == 200
    assert body["ok"] is True
    assert body["referral"]["claimed"] is True
    account = db.get_opencode_go_account(account_id)
    assert account["referral_claimed"] == 1


def test_opencode_go_referral_cache_endpoint_reads_db(tmp_path, monkeypatch):
    db = setup_test_db(tmp_path, monkeypatch)
    SENT = {"is_valid": True, "has_reward": True, "claimed": False, "reward": {"referralCode": "X", "status": "available", "amount": 100},
            "rewards": [{"id": "ref_x", "status": "available", "amount": 100}], "raw": {}, "checked_at": "2026-01-01T00:00:00Z"}

    async def fake_query(account, secret_key, timeout, log, referral_query_js_url=None, referral_query_server_id=None):
        return SENT

    monkeypatch.setattr("app.main.query_referral_for_account", fake_query)

    with TestClient(app) as client:
        login(client)
        created = client.post("/api/opencode-go/accounts", json={"name": "c@example.com", "email": "c@example.com", "password": "pw"})
        account_id = created.json()["id"]
        # POST 查询写入 DB 缓存
        client.post(f"/api/opencode-go/accounts/{account_id}/referral")
        # GET 读缓存，不再触发查询
        resp = client.get(f"/api/opencode-go/accounts/{account_id}/referral")

    body = resp.json()
    assert resp.status_code == 200
    assert body["referral"]["has_reward"] is True
    assert body["referral"]["claimed"] is False
    assert body["referral"]["rewards"][0]["status"] == "available"
    assert body["referral"]["reward"]["status"] == "available"


def test_opencode_go_settings_roundtrip_referral(tmp_path, monkeypatch):
    setup_test_db(tmp_path, monkeypatch)

    async def fake_fetch_lite(js_url, timeout=15):
        return "deadbeef" * 8

    async def fake_fetch_key(js_url, timeout=15):
        return "cafecafe" * 8

    monkeypatch.setattr("app.main.fetch_lite_subscription_reference_id", fake_fetch_lite)
    monkeypatch.setattr("app.main.fetch_key_list_reference_id", fake_fetch_key)

    with TestClient(app) as client:
        login(client)
        saved = client.post(
            "/api/opencode-go/settings",
            json={
                "referral_query_js_url": "",
                "referral_query_server_id": "2a0b2fef5fd2ec9eff0cb5d4955e4ada4eece21fac85591ed4c09630168d4844",
                "referral_action_server_id": "f386778c1b78eade3e6acff87c9284e02fcd86826463c080526143c4fe8fff23",
            },
        )
        assert saved.status_code == 200
        s = saved.json()["settings"]
        assert s["referral_query_server_id"] == "2a0b2fef5fd2ec9eff0cb5d4955e4ada4eece21fac85591ed4c09630168d4844"
        assert s["referral_action_server_id"] == "f386778c1b78eade3e6acff87c9284e02fcd86826463c080526143c4fe8fff23"
        # GET 回显
        got = client.get("/api/opencode-go/settings").json()["settings"]
        assert got["referral_query_server_id"] == "2a0b2fef5fd2ec9eff0cb5d4955e4ada4eece21fac85591ed4c09630168d4844"


def test_opencode_go_accounts_filter_by_referral_status(tmp_path, monkeypatch):
    db = setup_test_db(tmp_path, monkeypatch)
    unclaimed_id = db.upsert_opencode_go_account({"email": "unclaimed@example.com", "password": "secret-password"})
    claimed_id = db.upsert_opencode_go_account({"email": "claimed@example.com", "password": "secret-password"})
    none_id = db.upsert_opencode_go_account({"email": "none@example.com", "password": "secret-password"})
    unknown_id = db.upsert_opencode_go_account({"email": "unknown@example.com", "password": "secret-password"})
    db.update_opencode_go_referral(unclaimed_id, True, False, {"referralCode": "U", "status": "available"}, referral_json=[{"id": "ref_u", "status": "available"}])
    db.update_opencode_go_referral(claimed_id, True, True, {"referralCode": "C", "status": "applied"}, referral_json=[{"id": "ref_c", "status": "applied"}])
    db.update_opencode_go_referral(none_id, False, None, {"referralCode": "N"}, referral_json=[])

    with TestClient(app) as client:
        login(client)
        unclaimed = client.get("/api/opencode-go/accounts", params={"referral_status": "unclaimed"})
        claimed = client.get("/api/opencode-go/accounts", params={"referral_status": "claimed"})
        none = client.get("/api/opencode-go/accounts", params={"referral_status": "none"})
        has = client.get("/api/opencode-go/accounts", params={"referral_status": "has"})

    def emails(resp):
        payload = resp.json()
        return {account["email"] for account in payload["accounts"]}

    assert resp_ok(unclaimed, 200)
    assert emails(unclaimed) == {"unclaimed@example.com"}
    assert resp_ok(claimed, 200)
    assert emails(claimed) == {"claimed@example.com"}
    assert resp_ok(none, 200)
    assert emails(none) == {"none@example.com"}
    assert resp_ok(has, 200)
    assert emails(has) == {"unclaimed@example.com", "claimed@example.com"}


def resp_ok(response, status=200):
    return response.status_code == status


@pytest.mark.asyncio
async def test_opencode_go_scheduler_auto_claims_referral_on_threshold(tmp_path, monkeypatch):
    db = setup_test_db(tmp_path, monkeypatch)
    account_id = db.upsert_opencode_go_account({"email": "autoacclaim@example.com", "password": "secret-password", "is_enabled": True})

    async def fake_refresh(*args, **kwargs):
        return {
            "is_valid": True,
            "rolling_usage": {"usagePercent": 60},  # >= 50 → 自动领取
            "weekly_usage": {"usagePercent": 10},
            "monthly_usage": {"usagePercent": 5},
        }

    async def fake_query(account, secret_key, timeout, log, referral_query_js_url=None, referral_query_server_id=None):
        return {"is_valid": True, "has_reward": True, "claimed": False,
                "reward": {"referralCode": "X", "status": "available", "id": "ref_x"},
                "rewards": [{"id": "ref_x", "status": "available", "amount": 500}], "raw": {}, "checked_at": "2026-01-01T00:00:00Z"}

    claim_calls = []

    async def fake_claim(account, secret_key, timeout, log, referral_query_js_url=None, referral_query_server_id=None, referral_action_server_id=None):
        claim_calls.append(account["id"])
        return {"is_valid": True, "claimed": True, "message": "领取成功",
                "reward": {"referralCode": "X", "status": "applied", "id": "ref_x"},
                "rewards": [{"id": "ref_x", "status": "applied", "amount": 500}], "raw": {}}

    monkeypatch.setattr("app.services.scheduler.refresh_opencode_go_account", fake_refresh)
    monkeypatch.setattr("app.services.scheduler.query_referral_for_account", fake_query)
    monkeypatch.setattr("app.services.scheduler.claim_referral_reward_for_account", fake_claim)

    await query_opencode_go_for_account(db, account_id)
    assert claim_calls == [account_id]
    account = db.get_opencode_go_account(account_id)
    assert account["referral_claimed"] == 1


@pytest.mark.asyncio
async def test_opencode_go_scheduler_skips_auto_claim_below_threshold(tmp_path, monkeypatch):
    db = setup_test_db(tmp_path, monkeypatch)
    account_id = db.upsert_opencode_go_account({"email": "low@example.com", "password": "secret-password", "is_enabled": True})

    async def fake_refresh(*args, **kwargs):
        return {"is_valid": True, "rolling_usage": {"usagePercent": 10},
                "weekly_usage": {"usagePercent": 5}, "monthly_usage": {"usagePercent": 3}}

    async def fake_query(account, secret_key, timeout, log, referral_query_js_url=None, referral_query_server_id=None):
        return {"is_valid": True, "has_reward": True, "claimed": False,
                "reward": {"referralCode": "Y"}, "rewards": [{"id": "ref_y", "status": "available"}], "raw": {}}

    claim_calls = []

    async def fake_claim(account, secret_key, timeout, log, **kwargs):
        claim_calls.append(account["id"])
        return {"is_valid": True, "claimed": True, "message": "ok", "reward": {}, "rewards": []}

    monkeypatch.setattr("app.services.scheduler.refresh_opencode_go_account", fake_refresh)
    monkeypatch.setattr("app.services.scheduler.query_referral_for_account", fake_query)
    monkeypatch.setattr("app.services.scheduler.claim_referral_reward_for_account", fake_claim)
    await query_opencode_go_for_account(db, account_id)
    assert claim_calls == []


def test_opencode_go_referral_claim_batch_endpoint(tmp_path, monkeypatch):
    db = setup_test_db(tmp_path, monkeypatch)

    async def fake_claim(account, secret_key, timeout, log, referral_query_js_url=None, referral_query_server_id=None, referral_action_server_id=None):
        return {"is_valid": True, "claimed": True, "message": "领取成功",
                "reward": {"referralCode": "X", "status": "applied", "id": "ref_x"},
                "rewards": [{"id": "ref_x", "status": "applied", "amount": 500}], "raw": {}}

    monkeypatch.setattr("app.main.claim_referral_reward_for_account", fake_claim)

    with TestClient(app) as client:
        login(client)
        unclaimed_id = db.upsert_opencode_go_account({"email": "batch1@example.com", "password": "pw"})
        claimed_id = db.upsert_opencode_go_account({"email": "batch2@example.com", "password": "pw"})
        db.update_opencode_go_referral(unclaimed_id, True, False, {"status": "available"}, referral_json=[{"id": "ref_a", "status": "available"}])
        db.update_opencode_go_referral(claimed_id, True, True, {"status": "applied"}, referral_json=[{"id": "ref_b", "status": "applied"}])
        response = client.post("/api/opencode-go/referral/claim-batch", json={"account_ids": [unclaimed_id, claimed_id]})

    body = response.json()
    assert response.status_code == 200
    assert body["success"] == 1
    assert body["skipped"] == 1
    assert body["failed"] == 0
    by_email = {item.get("account", {}).get("email"): item for item in body["results"]}
    assert by_email["batch1@example.com"]["ok"] is True
    assert by_email["batch2@example.com"]["skipped"] is True
    assert db.get_opencode_go_account(unclaimed_id)["referral_claimed"] == 1


def test_opencode_go_cpa_status_lists_missing_accounts(tmp_path, monkeypatch):
    db = setup_test_db(tmp_path, monkeypatch)
    configure_cpa(db)
    import_id = db.upsert_opencode_go_account(
        {"email": "imported@example.com", "password": "pw", "api_key": "sk-imported"}
    )
    missing_id = db.upsert_opencode_go_account(
        {"email": "missing@example.com", "password": "pw", "api_key": "sk-missing"}
    )
    nokey_id = db.upsert_opencode_go_account({"email": "nokey@example.com", "password": "pw"})
    DummySub2ApiClient.requests = []
    DummySub2ApiClient.responses = [
        DummySub2ApiResponse(
            {
                "openai-compatibility": [
                    {"name": "IMPORTED@example.com"},
                    {"name": "other@example.com", "base-url": "https://other.example/v1"},
                ]
            }
        ),
    ]
    monkeypatch.setattr("app.services.cpa_admin.httpx.AsyncClient", DummySub2ApiClient)

    with TestClient(app) as client:
        login(client)
        response = client.get("/api/opencode-go/cpa-status")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["total"] == 3
    emails_missing = {account["email"] for account in body["missing"]}
    assert emails_missing == {"missing@example.com", "nokey@example.com"}
    # importable 子集必须排除没有 API key 的账号
    emails_importable = {account["email"] for account in body["importable"]}
    assert emails_importable == {"missing@example.com"}
    # 大小写不敏感对比：imported@example.com 即使 CPA 里写成大写也算已导入
    assert "imported@example.com" not in emails_missing
    # 拉取 provider 请求只发了一次（GET /openai-compatibility）
    list_requests = [req for req in DummySub2ApiClient.requests if req["url"] == "https://cpa.example/v0/management/openai-compatibility"]
    assert len(list_requests) == 1
    assert list_requests[0]["method"] == "GET"
    _ = (import_id, missing_id, nokey_id)


def test_opencode_go_cpa_status_requires_cpa_config(tmp_path, monkeypatch):
    db = setup_test_db(tmp_path, monkeypatch)
    # 未配置 CPA 站点地址/Authorization
    with TestClient(app) as client:
        login(client)
        response = client.get("/api/opencode-go/cpa-status")
    assert response.status_code in (400, 503)


def test_normalize_model_ids_filters_grok():
    from app.services.cpa_admin import _normalize_model_ids

    models = _normalize_model_ids({"data": ["gpt-5", "grok-4", "Grok-3-mini", "claude-opus", "GPT-5"]})
    # grok 模型（大小写不敏感）被移除，其余保留
    assert not any("grok" in m.lower() for m in models)
    assert "grok-4" not in models
    assert "Grok-3-mini" not in models
    assert "gpt-5" in models and "claude-opus" in models

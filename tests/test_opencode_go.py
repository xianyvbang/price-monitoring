import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import Database
from app.security import decrypt_value, encrypt_value
from app.services.opencode_go import (
    KEY_LIST_DEFAULT_JS_URL,
    KEY_LIST_GET_REFERENCE_ID,
    KEY_LIST_SERVER_INSTANCE,
    LITE_SUBSCRIPTION_GET_REFERENCE_ID,
    extract_key_list_reference_id,
    extract_lite_subscription_reference_id,
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
async def test_opencode_go_query_all_skips_weekly_limit_but_manual_refreshes(tmp_path, monkeypatch):
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

    assert {result["account_id"] for result in results} == {low_id, missing_id, invalid_id}
    assert manual_result["account_id"] == high_id
    assert "high@example.com" not in calls[:-1]
    assert calls[-1] == "high@example.com"


def test_opencode_go_accounts_summary_averages_all_eligible_accounts(tmp_path, monkeypatch):
    db = setup_test_db(tmp_path, monkeypatch)
    account_ids = []
    for index in range(25):
        email = f"summary{index:02d}@example.com"
        account_ids.append(db.upsert_opencode_go_account({"name": email, "email": email, "password": "secret-password"}))

    db.update_opencode_go_result(
        account_ids[0],
        {"is_valid": True, "rolling_usage": {"usagePercent": 10}, "weekly_usage": {"usagePercent": 20}},
    )
    db.update_opencode_go_result(
        account_ids[1],
        {"is_valid": True, "weekly_usage": {"usagePercent": 98.9}},
    )
    db.update_opencode_go_result(
        account_ids[2],
        {"is_valid": True, "rolling_usage": {"usagePercent": 70}, "weekly_usage": {"usagePercent": 99}},
    )
    db.update_opencode_go_result(
        account_ids[3],
        {"is_valid": True, "rolling_usage": {"usagePercent": 80}, "weekly_usage": {"usagePercent": 100}},
    )
    for account_id in account_ids[5:]:
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
    assert db.get_opencode_go_account(missing_id)


def test_opencode_go_bulk_import_uses_email_as_name_and_masks_password(tmp_path, monkeypatch):
    db = setup_test_db(tmp_path, monkeypatch)

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


def test_opencode_go_bulk_import_rejects_bad_lines(tmp_path, monkeypatch):
    setup_test_db(tmp_path, monkeypatch)

    with TestClient(app) as client:
        login(client)
        response = client.post("/api/opencode-go/accounts/bulk", json={"bulk_text": "broken-line"})

    assert response.status_code == 400
    assert "账号|密码" in response.json()["message"]


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
        "disabled": True,
        "headers": {"X-Keep": "1"},
        "base-url": "https://opencode.ai/zen/go/v1",
        "api-key-entries": [{"api-key": "sk-opencode-secret"}],
        "models": [{"name": "gpt-5"}, {"name": "gpt-5-mini"}],
    }
    assert saved_providers[1]["name"] == "other@example.com"
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
    assert DummySub2ApiClient.requests[0]["url"] == "https://cpa.example/v0/management/config"


@pytest.mark.asyncio
async def test_opencode_go_refresh_auto_disables_cpa_on_weekly_limit_and_stops_repeating(tmp_path, monkeypatch):
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
            "weekly_usage": {"usagePercent": 99, "resetInSec": 120},
            "monthly_usage": {"usagePercent": 50, "resetInSec": 180},
        }

    DummySub2ApiClient.requests = []
    DummySub2ApiClient.responses = [
        DummySub2ApiResponse({"openai-compatibility": [{"name": "auto-disable@example.com", "disabled": False}]}),
        DummySub2ApiResponse({"ok": True}),
    ]
    monkeypatch.setattr("app.services.scheduler.refresh_opencode_go_account", fake_refresh)
    monkeypatch.setattr("app.services.cpa_admin.httpx.AsyncClient", DummySub2ApiClient)

    first = await query_opencode_go_for_account(db, account_id)
    after_first = db.get_opencode_go_account(account_id)
    first_requests = list(DummySub2ApiClient.requests)
    DummySub2ApiClient.requests = []
    second = await query_opencode_go_for_account(db, account_id)

    assert first["is_valid"] is True
    assert second["is_valid"] is True
    assert after_first["cpa_provider_disabled"] == 1
    assert after_first["cpa_reenable_pending"] == 0
    assert [request["method"] for request in first_requests] == ["GET", "PUT"]
    assert first_requests[1]["json"][0]["disabled"] is True
    assert DummySub2ApiClient.requests == []
    assert any("CPA 自动停用成功" in log["message"] for log in db.list_logs(category="opencode-go"))


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
            "weekly_usage": {"usagePercent": 99, "resetInSec": 120},
            "monthly_usage": {"usagePercent": 80, "resetInSec": 180},
        },
    )
    db.update_opencode_go_cpa_state(account_id, provider_disabled=True, clear_error=True)

    async def fake_refresh(*args, **kwargs):
        return {
            "is_valid": True,
            "workspace_id": "ws_1",
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
    assert [request["method"] for request in requests_after_success] == ["GET", "PUT", "GET", "PUT"]
    assert requests_after_success[-1]["json"][0]["disabled"] is False
    assert DummySub2ApiClient.requests == []
    logs = db.list_logs(category="opencode-go")
    assert any("CPA 自动启用失败" in log["message"] for log in logs)
    assert any("CPA 自动启用成功" in log["message"] for log in logs)


@pytest.mark.asyncio
async def test_opencode_go_refresh_does_not_enable_cpa_while_monthly_still_limited(tmp_path, monkeypatch):
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
            "weekly_usage": {"usagePercent": 99, "resetInSec": 120},
            "monthly_usage": {"usagePercent": 99, "resetInSec": 180},
        },
    )
    db.update_opencode_go_cpa_state(account_id, provider_disabled=True, clear_error=True)

    async def fake_refresh(*args, **kwargs):
        return {
            "is_valid": True,
            "workspace_id": "ws_1",
            "weekly_usage": {"usagePercent": 20, "resetInSec": 120},
            "monthly_usage": {"usagePercent": 99, "resetInSec": 180},
        }

    DummySub2ApiClient.requests = []
    DummySub2ApiClient.responses = []
    monkeypatch.setattr("app.services.scheduler.refresh_opencode_go_account", fake_refresh)
    monkeypatch.setattr("app.services.cpa_admin.httpx.AsyncClient", DummySub2ApiClient)

    await query_opencode_go_for_account(db, account_id)

    account = db.get_opencode_go_account(account_id)
    assert account["cpa_provider_disabled"] == 1
    assert account["cpa_reenable_pending"] == 0
    assert DummySub2ApiClient.requests == []


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
    assert any("CPA 自动停用失败" in log["message"] for log in db.list_logs(category="opencode-go"))


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

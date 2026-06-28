import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import Database
from app.security import decrypt_value
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
    assert loaded.json()["settings"]["lite_subscription_js_url"] == "https://opencode.ai/_build/assets/index-DtPYjwk4.js"
    assert loaded.json()["settings"]["lite_subscription_server_id"] == "d" * 64
    assert loaded.json()["settings"]["key_list_js_url"] == "https://opencode.ai/_build/assets/index-PbCOrg8_.js"
    assert loaded.json()["settings"]["key_list_server_id"] == "e" * 64
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
    assert "邮箱|邮箱密码" in response.json()["message"]


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

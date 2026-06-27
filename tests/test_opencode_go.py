import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import Database
from app.security import decrypt_value
from app.services.opencode_go import (
    LITE_SUBSCRIPTION_GET_REFERENCE_ID,
    normalize_usage_result,
    query_opencode_server_reference,
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
    assert DummyAsyncClient.last_request["url"] == "https://opencode.ai/_server"
    assert DummyAsyncClient.last_request["headers"]["X-Server-Id"] == LITE_SUBSCRIPTION_GET_REFERENCE_ID
    assert DummyAsyncClient.last_request["headers"]["X-Server-Instance"] == "server-fn:test"
    assert DummyAsyncClient.last_request["json"] == ["ws_1"]


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


def test_opencode_go_api_requires_login(tmp_path, monkeypatch):
    setup_test_db(tmp_path, monkeypatch)

    with TestClient(app) as client:
        response = client.get("/api/opencode-go/accounts")

    assert response.status_code == 401


def test_opencode_go_api_crud_and_masks_secrets(tmp_path, monkeypatch):
    db = setup_test_db(tmp_path, monkeypatch)

    async def fake_refresh(account, secret_key, timeout, log=None):
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
        history = client.get(f"/api/opencode-go/accounts/{account_id}/history")
        updated = client.put(
            f"/api/opencode-go/accounts/{account_id}",
            json={"name": "go-main", "email": "next@example.com", "is_enabled": False},
        )

    assert created.status_code == 200
    assert created.json()["account"]["email"] == "user@example.com"
    assert "secret-password" not in created.text
    assert refreshed.status_code == 200
    assert "sk-opencode-secret" not in refreshed.text
    assert refreshed.json()["account"]["api_key_masked"] == "sk-openc******cret"
    assert listed.json()["accounts"][0]["has_api_key"] is True
    assert "sk-opencode-secret" not in listed.text
    assert key_response.json()["api_key"] == "sk-opencode-secret"
    assert history.json()["records"][0]["rolling_usage"]["usage_percent"] == 11
    assert updated.json()["account"]["email"] == "next@example.com"
    assert updated.json()["account"]["is_enabled"] is False
    assert decrypt_value(db.get_opencode_go_account(account_id)["password_enc"], "test-key") == "secret-password"


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

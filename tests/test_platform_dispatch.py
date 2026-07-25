from fastapi.testclient import TestClient
import pytest

from app.main import app
from app.models import Database
from app.services.sub2api_admin import (
    Sub2ApiAdminClient,
    merge_recent_activity,
    public_dispatch_account,
)


class DummyResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload


class DummyAsyncClient:
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
        return self.responses.pop(0)


def setup_test_db(tmp_path, monkeypatch):
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


def login(client):
    client.post("/login", data={"username": "admin", "password": "password123"})


def test_merge_recent_activity_keeps_success_and_error_records():
    activities = merge_recent_activity(
        [
            {
                "id": 11,
                "user_id": 7,
                "user": {"id": 7, "email": "user@example.com"},
                "model": "gpt-5",
                "upstream_model": "gpt-5.1",
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_creation_tokens": 3,
                "cache_read_tokens": 4,
                "actual_cost": 0.012345,
                "total_cost": 0.02,
                "first_token_ms": 320,
                "duration_ms": 1480,
                "created_at": "2026-07-25T08:00:00Z",
            }
        ],
        [
            {
                "id": 22,
                "user_id": 8,
                "user_email": "failed@example.com",
                "requested_model": "gpt-5-mini",
                "status_code": 429,
                "message": "rate limited",
                "phase": "upstream",
                "created_at": "2026-07-25T09:00:00Z",
            }
        ],
        6,
    )

    assert [item["kind"] for item in activities] == ["error", "success"]
    assert activities[0]["user_email"] == "failed@example.com"
    assert activities[0]["status_code"] == 429
    assert activities[0]["total_tokens"] is None
    assert activities[1]["user_email"] == "user@example.com"
    assert activities[1]["model"] == "gpt-5.1"
    assert activities[1]["requested_model"] == "gpt-5"
    assert activities[1]["total_tokens"] == 127
    assert activities[1]["cost"] == 0.012345


def test_public_dispatch_account_does_not_expose_credentials():
    account = public_dispatch_account(
        {
            "id": 3,
            "name": "dispatch-account",
            "platform": "openai",
            "type": "apikey",
            "status": "active",
            "credentials": {"api_key": "secret"},
            "group_ids": [5],
        },
        [],
    )

    assert account["is_enabled"] is True
    assert account["group_ids"] == [5]
    assert "credentials" not in account
    assert "secret" not in str(account)


@pytest.mark.asyncio
async def test_sub2api_admin_list_accounts_reads_all_pages(monkeypatch):
    DummyAsyncClient.requests = []
    DummyAsyncClient.responses = [
        DummyResponse({"code": 0, "data": {"items": [{"id": 1, "name": "a"}], "total": 2, "pages": 2}}),
        DummyResponse({"code": 0, "data": {"items": [{"id": 2, "name": "b"}], "total": 2, "pages": 2}}),
    ]
    monkeypatch.setattr("app.services.sub2api_admin.httpx.AsyncClient", DummyAsyncClient)

    accounts = await Sub2ApiAdminClient("https://sub.example", "admin-key").list_accounts()

    assert [account["id"] for account in accounts] == [1, 2]
    assert [request["params"]["page"] for request in DummyAsyncClient.requests] == [1, 2]
    assert all(request["headers"]["x-api-key"] == "admin-key" for request in DummyAsyncClient.requests)


@pytest.mark.asyncio
async def test_sub2api_admin_updates_account_status(monkeypatch):
    DummyAsyncClient.requests = []
    DummyAsyncClient.responses = [
        DummyResponse({"code": 0, "data": {"id": 9, "name": "remote", "status": "inactive"}})
    ]
    monkeypatch.setattr("app.services.sub2api_admin.httpx.AsyncClient", DummyAsyncClient)

    account = await Sub2ApiAdminClient("https://sub.example/", "admin-key").update_account_status(9, False)

    assert account["status"] == "inactive"
    assert account["is_enabled"] is False
    request = DummyAsyncClient.requests[0]
    assert request["method"] == "PUT"
    assert request["url"] == "https://sub.example/api/v1/admin/accounts/9"
    assert request["json"] == {"status": "inactive"}


def test_platform_dispatch_page_api_and_enabled_proxy(tmp_path, monkeypatch):
    setup_test_db(tmp_path, monkeypatch)

    class FakeAdminClient:
        updated = []

        async def platform_dispatch(self, recent_limit=6):
            assert recent_limit == 6
            return {
                "accounts": [
                    {
                        "id": 9,
                        "name": "remote",
                        "platform": "openai",
                        "type": "apikey",
                        "status": "active",
                        "is_enabled": True,
                        "group_ids": [2],
                        "recent_activity": [{"id": "error-1", "kind": "error", "is_error": True}],
                    }
                ],
                "groups": [{"id": 2, "name": "主分组", "platform": "openai"}],
                "warnings": [],
                "recent_limit": 6,
            }

        async def update_account_status(self, account_id, enabled):
            self.updated.append((account_id, enabled))
            return {
                "id": account_id,
                "name": "remote",
                "status": "active" if enabled else "inactive",
                "is_enabled": enabled,
            }

    fake = FakeAdminClient()
    monkeypatch.setattr("app.main.sub2api_admin_client", lambda: fake)
    monkeypatch.setattr(
        "app.main.public_sub2api_settings",
        lambda: {"site_url": "https://sub.example", "has_admin_key": True},
    )

    with TestClient(app) as client:
        login(client)
        page = client.get("/platform-dispatch")
        response = client.get("/api/platform-dispatch")
        invalid = client.post("/api/platform-dispatch/accounts/9/enabled", json={"is_enabled": "yes"})
        updated = client.post("/api/platform-dispatch/accounts/9/enabled", json={"is_enabled": False})

    assert page.status_code == 200
    assert '<div id="app"></div>' in page.text
    assert response.status_code == 200
    assert response.json()["site_url"] == "https://sub.example"
    assert response.json()["accounts"][0]["recent_activity"][0]["kind"] == "error"
    assert invalid.status_code == 400
    assert updated.status_code == 200
    assert updated.json()["account"]["status"] == "inactive"
    assert fake.updated == [(9, False)]

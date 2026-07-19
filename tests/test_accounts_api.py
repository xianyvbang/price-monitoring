from fastapi.testclient import TestClient

from app.main import app, config
from app.models import Database


def test_accounts_list_only_returns_fields_used_by_list_view(tmp_path, monkeypatch):
    test_db = Database(str(tmp_path / "app.db"), config.app_secret_key)
    test_db.init()
    test_db.ensure_admin("admin", "password123")
    account_id = test_db.upsert_account(
        {
            "platform": "sub2Api",
            "name": "summary-account",
            "base_url": "https://example.com",
            "key_id": "current-group",
            "api_key": "api-secret",
            "email": "user@example.com",
            "password": "password",
            "access_token": "access-secret",
            "refresh_token": "refresh-secret",
            "threshold": 3,
            "note": "primary",
            "recharge_url": "https://example.com/topup",
        }
    )
    test_db.replace_account_monitor_groups(account_id, ["monitor-a", "monitor-b"])
    monkeypatch.setattr("app.main.db", test_db)
    monkeypatch.setattr("app.main.scheduler.db", test_db)
    monkeypatch.setattr("app.main.scheduler.start", lambda: None)

    async def stop_scheduler():
        return None

    monkeypatch.setattr("app.main.scheduler.stop", stop_scheduler)

    with TestClient(app) as client:
        client.post("/login", data={"username": "admin", "password": "password123"})
        response = client.get("/api/accounts", params={"platform": "sub2Api", "name": "summary"})

    assert response.status_code == 200
    assert set(response.json()) == {"sub2Api"}
    account = response.json()["sub2Api"][0]
    assert set(account) == {
        "id",
        "platform",
        "name",
        "base_url",
        "note",
        "recharge_url",
        "recharge_paid_amount",
        "recharge_received_amount",
        "threshold",
        "is_enabled",
        "is_visible",
        "selected_group_id",
        "selected_group_ids",
        "has_api_key",
        "has_email",
        "has_password",
        "has_access_token",
        "has_refresh_token",
        "has_user_id",
    }
    assert account["selected_group_id"] == "monitor-a"
    assert account["selected_group_ids"] == ["monitor-a", "monitor-b"]
    assert account["has_api_key"] is True
    assert account["has_refresh_token"] is True
    assert "monitor_groups" not in account
    assert "consumption_stats" not in account
    assert "last_extra" not in account
    assert "created_at" not in account


def test_account_detail_remains_complete_after_list_optimization(tmp_path, monkeypatch):
    test_db = Database(str(tmp_path / "app.db"), config.app_secret_key)
    test_db.init()
    test_db.ensure_admin("admin", "password123")
    account_id = test_db.upsert_account(
        {
            "platform": "newApi",
            "name": "detail-account",
            "base_url": "https://example.com",
            "access_token": "access-secret",
            "user_id": "user-1",
        }
    )
    monkeypatch.setattr("app.main.db", test_db)
    monkeypatch.setattr("app.main.scheduler.db", test_db)
    monkeypatch.setattr("app.main.scheduler.start", lambda: None)

    async def stop_scheduler():
        return None

    monkeypatch.setattr("app.main.scheduler.stop", stop_scheduler)

    with TestClient(app) as client:
        client.post("/login", data={"username": "admin", "password": "password123"})
        response = client.get(f"/api/accounts/{account_id}")

    account = response.json()["account"]
    assert response.status_code == 200
    assert account["user_id"] == "user-1"
    assert account["has_access_token"] is True
    assert "monitor_groups" in account
    assert "consumption_stats" in account

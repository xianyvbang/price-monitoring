from fastapi.testclient import TestClient

from app.main import app
from app.models import Database


def test_group_query_buttons_use_api_fetch(tmp_path, monkeypatch):
    test_db = Database(str(tmp_path / "app.db"), "test-key")
    test_db.init()
    test_db.ensure_admin("admin", "password123")
    account_id = test_db.upsert_account(
        {
            "platform": "sub2Api",
            "name": "sub",
            "base_url": "https://sub.example",
            "api_key": "secret",
            "note": "monitor note",
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
        dashboard = client.get("/")
        accounts = client.get("/accounts")
        settings = client.get("/settings")

    assert dashboard.status_code == 200
    assert accounts.status_code == 200
    assert settings.status_code == 200
    assert 'data-refresh-interval="30"' in dashboard.text
    assert "备注" in dashboard.text
    assert "monitor note" in dashboard.text
    assert 'action="/query-all"' not in dashboard.text
    assert "/accounts/1/query" not in dashboard.text
    assert "/accounts/1/group-query" not in dashboard.text
    assert "/accounts/1/group-query" not in accounts.text
    assert 'fetch("/api/query-all"' in dashboard.text
    assert "/api/accounts/${button.dataset.accountId}/query" in dashboard.text
    assert "/api/accounts/${button.dataset.accountId}/group-query" in dashboard.text
    assert "/api/accounts/${button.dataset.accountId}/group-query" in accounts.text
    assert f"/accounts/{account_id}/group-rates" in dashboard.text
    assert "group_rate_query_interval" in settings.text


def test_dashboard_shows_group_rate_column(tmp_path, monkeypatch):
    test_db = Database(str(tmp_path / "app.db"), "test-key")
    test_db.init()
    test_db.ensure_admin("admin", "password123")
    account_id = test_db.upsert_account(
        {
            "platform": "sub2Api",
            "name": "sub",
            "base_url": "https://sub.example",
            "api_key": "secret",
            "note": "monitor note",
        }
    )
    test_db.update_account_group_result(
        account_id,
        {
            "extra": (
                '{"groups": ['
                '{"plan_name": "Basic Plan", "default_rate_multiplier": 1.2, "user_rate_multiplier": 0.8},'
                '{"plan_name": "Pro Plan", "default_rate_multiplier": 2.0, "user_rate_multiplier": null}'
                "]}"
            ),
        },
    )
    monkeypatch.setattr("app.main.db", test_db)
    monkeypatch.setattr("app.main.scheduler.db", test_db)
    monkeypatch.setattr("app.main.scheduler.start", lambda: None)

    async def stop_scheduler():
        return None

    monkeypatch.setattr("app.main.scheduler.stop", stop_scheduler)

    with TestClient(app) as client:
        client.post("/login", data={"username": "admin", "password": "password123"})
        dashboard = client.get("/")

    assert dashboard.status_code == 200
    assert "分组倍率" in dashboard.text
    assert "Basic Plan: 0.8" in dashboard.text
    assert "Pro Plan: 2.0" in dashboard.text


def test_group_rate_history_page_and_api(tmp_path, monkeypatch):
    test_db = Database(str(tmp_path / "app.db"), "test-key")
    test_db.init()
    test_db.ensure_admin("admin", "password123")
    account_id = test_db.upsert_account(
        {
            "platform": "sub2Api",
            "name": "sub",
            "base_url": "https://sub.example",
            "api_key": "secret",
            "note": "monitor note",
        }
    )
    test_db.record_group_rate_if_changed(
        account_id,
        {
            "group": {"plan_name": "Basic Plan", "effective_rate_multiplier": 0.8},
            "raw_json": '{"group":{"plan_name":"Basic Plan","effective_rate_multiplier":0.8}}',
        },
        "2026-05-19T00:00:00+00:00",
    )
    monkeypatch.setattr("app.main.db", test_db)
    monkeypatch.setattr("app.main.scheduler.db", test_db)
    monkeypatch.setattr("app.main.scheduler.start", lambda: None)

    async def stop_scheduler():
        return None

    monkeypatch.setattr("app.main.scheduler.stop", stop_scheduler)

    with TestClient(app) as client:
        client.post("/login", data={"username": "admin", "password": "password123"})
        page = client.get(f"/accounts/{account_id}/group-rates")
        api = client.get(f"/api/accounts/{account_id}/group-rates")

    assert page.status_code == 200
    assert "Basic Plan" in page.text
    assert "0.8" in page.text
    assert "2026-05-19 08:00:00" in page.text
    assert "查看 JSON" in page.text
    assert api.status_code == 200
    assert api.json()["records"][0]["plan_name"] == "Basic Plan"

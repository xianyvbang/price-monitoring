from fastapi.testclient import TestClient

from app.main import app
from app.models import Database


def test_group_query_buttons_use_api_fetch(tmp_path, monkeypatch):
    test_db = Database(str(tmp_path / "app.db"), "test-key")
    test_db.init()
    test_db.ensure_admin("admin", "password123")
    test_db.upsert_account(
        {
            "platform": "sub2Api",
            "name": "sub",
            "base_url": "https://sub.example",
            "api_key": "secret",
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

    assert dashboard.status_code == 200
    assert accounts.status_code == 200
    assert "/accounts/1/group-query" not in dashboard.text
    assert "/accounts/1/group-query" not in accounts.text
    assert "/api/accounts/${button.dataset.accountId}/group-query" in dashboard.text
    assert "/api/accounts/${button.dataset.accountId}/group-query" in accounts.text

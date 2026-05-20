from fastapi.testclient import TestClient

from app.main import app
from app.models import Database
from app.security import decrypt_value


def test_update_account_changes_name_without_losing_secret(tmp_path):
    db = Database(str(tmp_path / "app.db"), "test-key")
    db.init()
    account_id = db.upsert_account(
        {
            "platform": "sub2Api",
            "name": "old",
            "base_url": "https://old.example",
            "key_id": "group-1",
            "api_key": "secret",
            "email": "user@example.com",
            "password": "login-password",
            "note": "old note",
            "recharge_url": "https://old.example/topup",
        }
    )

    db.update_account(
        account_id,
        {
            "platform": "sub2Api",
            "name": "new",
            "base_url": "https://new.example",
            "key_id": "",
            "api_key": "",
            "email": "",
            "password": "",
            "threshold": 2,
            "note": "重要账号",
            "recharge_url": "https://new.example/topup",
            "is_enabled": False,
        },
    )

    account = db.get_account(account_id)

    assert account["name"] == "new"
    assert account["base_url"] == "https://new.example"
    assert account["note"] == "重要账号"
    assert account["recharge_url"] == "https://new.example/topup"
    assert account["threshold"] == 2
    assert account["is_enabled"] == 0
    assert decrypt_value(account["key_id_enc"], "test-key") == "group-1"
    assert decrypt_value(account["api_key_enc"], "test-key") == "secret"
    assert decrypt_value(account["email_enc"], "test-key") == "user@example.com"
    assert decrypt_value(account["password_enc"], "test-key") == "login-password"


def test_api_update_missing_account_returns_404(tmp_path, monkeypatch):
    test_db = Database(str(tmp_path / "app.db"), "test-key")
    test_db.init()
    test_db.ensure_admin("admin", "password123")
    monkeypatch.setattr("app.main.db", test_db)
    monkeypatch.setattr("app.main.scheduler.db", test_db)
    monkeypatch.setattr("app.main.scheduler.start", lambda: None)

    async def stop_scheduler():
        return None

    monkeypatch.setattr("app.main.scheduler.stop", stop_scheduler)

    with TestClient(app) as client:
        client.post("/login", data={"username": "admin", "password": "password123"})
        response = client.put(
            "/api/accounts/999",
            json={
                "platform": "sub2Api",
                "name": "missing",
                "base_url": "https://example.com",
            },
        )

    assert response.status_code == 404
    assert response.json()["detail"] == "账号不存在"


def test_form_update_missing_account_returns_404(tmp_path, monkeypatch):
    test_db = Database(str(tmp_path / "app.db"), "test-key")
    test_db.init()
    test_db.ensure_admin("admin", "password123")
    monkeypatch.setattr("app.main.db", test_db)
    monkeypatch.setattr("app.main.scheduler.db", test_db)
    monkeypatch.setattr("app.main.scheduler.start", lambda: None)

    async def stop_scheduler():
        return None

    monkeypatch.setattr("app.main.scheduler.stop", stop_scheduler)

    with TestClient(app) as client:
        client.post("/login", data={"username": "admin", "password": "password123"})
        response = client.post(
            "/accounts",
            data={
                "account_id": "999",
                "platform": "sub2Api",
                "name": "missing",
                "base_url": "https://example.com",
            },
            follow_redirects=False,
        )

    assert response.status_code == 404
    assert "账号不存在或已删除" in response.text

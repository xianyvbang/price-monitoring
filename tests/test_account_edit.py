from fastapi.testclient import TestClient

from app.main import app
from app.main import config
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


def test_api_update_returns_account_for_local_row_refresh(tmp_path, monkeypatch):
    test_db = Database(str(tmp_path / "app.db"), config.app_secret_key)
    test_db.init()
    test_db.ensure_admin("admin", "password123")
    account_id = test_db.upsert_account(
        {
            "platform": "sub2Api",
            "name": "old",
            "base_url": "https://old.example",
            "api_key": "old-secret",
            "email": "old@example.com",
            "password": "old-password",
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
        response = client.put(
            f"/api/accounts/{account_id}",
            json={
                "platform": "sub2Api",
                "name": "new",
                "base_url": "https://new.example",
                "key_id": "group-2",
                "api_key": "new-secret",
                "email": "new@example.com",
                "password": "new-password",
                "threshold": 3.5,
                "note": "new note",
                "recharge_url": "https://new.example/topup",
                "is_visible": False,
                "is_enabled": True,
            },
        )

    payload = response.json()

    assert response.status_code == 200
    assert payload["ok"] is True
    assert payload["id"] == account_id
    assert payload["account"]["id"] == account_id
    assert payload["account"]["name"] == "new"
    assert payload["account"]["base_url"] == "https://new.example"
    assert payload["account"]["recharge_url"] == "https://new.example/topup"
    assert payload["account"]["note"] == "new note"
    assert payload["account"]["threshold"] == 3.5
    assert payload["account"]["is_visible"] is False
    assert payload["account"]["is_enabled"] is False
    assert payload["account"]["key_id"] == "group-2"
    assert payload["account"]["email"] == "new@example.com"
    assert payload["account"]["has_api_key"] is True
    assert payload["account"]["has_password"] is True


def test_account_form_only_fetches_groups_after_save_when_visible(tmp_path, monkeypatch):
    test_db = Database(str(tmp_path / "app.db"), config.app_secret_key)
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
        page = client.get("/accounts")
        create_hidden = client.post(
            "/api/accounts",
            json={
                "platform": "newApi",
                "name": "hidden-new",
                "base_url": "https://hidden.example",
                "access_token": "token",
                "user_id": "1",
                "is_visible": False,
            },
        )
        create_visible = client.post(
            "/api/accounts",
            json={
                "platform": "newApi",
                "name": "visible-new",
                "base_url": "https://visible.example",
                "access_token": "token",
                "user_id": "2",
                "is_visible": True,
            },
        )

    assert page.status_code == 200
    assert '<div id="app"></div>' in page.text
    assert create_hidden.status_code == 200
    assert create_hidden.json()["account"]["is_visible"] is False
    assert create_hidden.json()["account"]["is_enabled"] is False
    assert create_visible.status_code == 200
    assert create_visible.json()["account"]["is_visible"] is True


def test_copy_account_button_uses_unsaved_dialog_data(tmp_path, monkeypatch):
    test_db = Database(str(tmp_path / "app.db"), config.app_secret_key)
    test_db.init()
    test_db.ensure_admin("admin", "password123")
    account_id = test_db.upsert_account(
        {
            "platform": "sub2Api",
            "name": "copy-source",
            "base_url": "https://copy.example",
            "key_id": "basic",
            "api_key": "secret",
            "email": "copy@example.com",
            "password": "login-password",
            "threshold": 4.5,
            "note": "copy note",
            "recharge_url": "https://copy.example/topup",
            "is_visible": True,
            "is_enabled": True,
        }
    )
    monkeypatch.setattr("app.main.db", test_db)
    monkeypatch.setattr("app.main.scheduler.db", test_db)
    monkeypatch.setattr("app.main.scheduler.start", lambda: None)

    async def stop_scheduler():
        return None

    monkeypatch.setattr("app.main.scheduler.stop", stop_scheduler)

    before_accounts = [(row["id"], row["name"]) for row in test_db.list_accounts()]
    with TestClient(app) as client:
        client.post("/login", data={"username": "admin", "password": "password123"})
        page = client.get("/accounts")
        detail = client.get(f"/api/accounts/{account_id}")
    after_accounts = [(row["id"], row["name"]) for row in test_db.list_accounts()]

    assert page.status_code == 200
    assert detail.status_code == 200
    assert '<div id="app"></div>' in page.text
    assert before_accounts == after_accounts
    assert detail.json()["account"]["name"] == "copy-source"
    assert detail.json()["account"]["key_id"] == "basic"
    assert detail.json()["account"]["email"] == "copy@example.com"
    assert "api_key" not in detail.json()["account"]
    assert "password" not in detail.json()["account"]


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

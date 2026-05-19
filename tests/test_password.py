from app.models import Database
from app.main import app, config
from app.security import verify_password
from fastapi.testclient import TestClient


def test_update_user_password_replaces_hash(tmp_path):
    db = Database(str(tmp_path / "app.db"), "test-key")
    db.init()
    db.ensure_admin("admin", "old-password")

    db.update_user_password("admin", "new-password")

    user = db.get_user("admin")
    assert user is not None
    assert verify_password("new-password", user["password_hash"])
    assert not verify_password("old-password", user["password_hash"])


def test_change_password_logs_out_current_session(tmp_path, monkeypatch):
    test_db = Database(str(tmp_path / "app.db"), "test-key")
    test_db.init()
    test_db.ensure_admin("admin", "old-password")
    monkeypatch.setattr("app.main.db", test_db)
    monkeypatch.setattr("app.main.scheduler.db", test_db)
    monkeypatch.setattr("app.main.scheduler.start", lambda: None)

    async def stop_scheduler():
        return None

    monkeypatch.setattr("app.main.scheduler.stop", stop_scheduler)

    with TestClient(app) as client:
        client.post("/login", data={"username": "admin", "password": "old-password"})
        response = client.post(
            "/settings/password",
            data={
                "current_password": "old-password",
                "new_password": "new-password",
                "confirm_password": "new-password",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/login?message=password_changed"
    assert config.session_cookie in response.headers["set-cookie"]
    assert "Max-Age=0" in response.headers["set-cookie"]

    user = test_db.get_user("admin")
    assert user is not None
    assert verify_password("new-password", user["password_hash"])

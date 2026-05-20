from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.main import app
from app.models import Database


def test_logs_keep_recent_7_days(tmp_path):
    db = Database(str(tmp_path / "app.db"), "test-key")
    db.init()
    db.add_log("info", "test", "recent")
    old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat(timespec="seconds")
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO app_logs (level, category, message, created_at) VALUES (?, ?, ?, ?)",
            ("info", "test", "old", old),
        )

    logs = [dict(row) for row in db.list_logs()]

    assert [log["message"] for log in logs] == ["recent"]


def test_clear_logs_removes_all_logs(tmp_path):
    db = Database(str(tmp_path / "app.db"), "test-key")
    db.init()
    db.add_log("info", "test", "first")
    db.add_log("warning", "test", "second")

    db.clear_logs()

    assert db.list_logs() == []


def test_clear_logs_page_action(tmp_path, monkeypatch):
    test_db = Database(str(tmp_path / "app.db"), "test-key")
    test_db.init()
    test_db.ensure_admin("admin", "password123")
    test_db.add_log("info", "test", "clear me")
    monkeypatch.setattr("app.main.db", test_db)
    monkeypatch.setattr("app.main.scheduler.db", test_db)
    monkeypatch.setattr("app.main.scheduler.start", lambda: None)

    async def stop_scheduler():
        return None

    monkeypatch.setattr("app.main.scheduler.stop", stop_scheduler)

    with TestClient(app) as client:
        client.post("/login", data={"username": "admin", "password": "password123"})
        page = client.get("/logs")
        response = client.post("/logs/clear", follow_redirects=False)
        cleared = client.get("/logs")

    assert page.status_code == 200
    assert "清空日志" in page.text
    assert "clear me" in page.text
    assert response.status_code == 303
    assert response.headers["location"] == "/logs"
    assert "暂无日志" in cleared.text
    assert "clear me" not in cleared.text
    assert test_db.list_logs() == []

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


def test_logs_can_be_listed_by_page(tmp_path):
    db = Database(str(tmp_path / "app.db"), "test-key")
    db.init()
    for index in range(1, 6):
        db.add_log("info", "test", f"page log {index}")

    first_page = [dict(row) for row in db.list_logs(limit=2)]
    second_page = [dict(row) for row in db.list_logs(limit=2, offset=2)]

    assert db.count_logs() == 5
    assert [log["message"] for log in first_page] == ["page log 5", "page log 4"]
    assert [log["message"] for log in second_page] == ["page log 3", "page log 2"]


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
        api = client.get("/api/logs")
        response = client.delete("/api/logs")
        cleared = client.get("/api/logs")

    assert page.status_code == 200
    assert '<div id="app"></div>' in page.text
    assert api.status_code == 200
    assert any(log["message"] == "clear me" for log in api.json()["logs"])
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert cleared.status_code == 200
    assert cleared.json()["logs"] == []
    assert test_db.list_logs() == []


def test_logs_page_and_api_are_paginated(tmp_path, monkeypatch):
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
        test_db.clear_logs()
        for index in range(1, 6):
            test_db.add_log("info", "test", f"paged log {index}")
        page = client.get("/logs?page=1&page_size=2")
        api = client.get("/api/logs?page=2&page_size=2")

    assert page.status_code == 200
    assert '<div id="app"></div>' in page.text
    assert api.status_code == 200
    assert [log["message"] for log in api.json()["logs"]] == ["paged log 3", "paged log 2"]
    assert api.json()["pagination"] == {
        "page": 2,
        "page_size": 2,
        "total": 5,
        "total_pages": 3,
        "has_previous": True,
        "has_next": True,
        "previous_page": 1,
        "next_page": 3,
    }

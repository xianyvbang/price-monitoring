from fastapi.testclient import TestClient

from app.main import app
from app.models import Database


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


def test_reminders_api_requires_login(tmp_path, monkeypatch):
    setup_test_db(tmp_path, monkeypatch)

    with TestClient(app) as client:
        response = client.get("/api/settings/reminders")

    assert response.status_code == 401


def test_reminders_api_crud_and_settings_payload(tmp_path, monkeypatch):
    test_db = setup_test_db(tmp_path, monkeypatch)

    with TestClient(app) as client:
        login(client)
        created = client.post(
            "/api/settings/reminders",
            json={
                "title": "交付检查",
                "content": "查看构建结果",
                "remind_at": "2026-06-27T09:30:00",
            },
        )
        reminder = created.json()["reminder"]
        test_db.mark_reminder_sent(reminder["id"], "2026-06-27T01:30:10+00:00")
        settings = client.get("/api/settings")
        updated = client.put(
            f"/api/settings/reminders/{reminder['id']}",
            json={
                "title": "更新交付检查",
                "content": "查看构建和测试结果",
                "remindAt": "2026-06-28T10:00:00",
            },
        )
        updated_row = test_db.get_reminder(reminder["id"])
        listed = client.get("/api/settings/reminders")
        deleted = client.delete(f"/api/settings/reminders/{reminder['id']}")
        after_delete = client.get("/api/settings/reminders")

    assert created.status_code == 200
    assert reminder["title"] == "交付检查"
    assert reminder["remind_at"] == "2026-06-27T01:30:00+00:00"
    assert reminder["remind_at_china"] == "2026-06-27T09:30:00"
    assert settings.status_code == 200
    assert settings.json()["reminders"][0]["id"] == reminder["id"]
    assert updated.status_code == 200
    assert updated.json()["reminder"]["title"] == "更新交付检查"
    assert updated.json()["reminder"]["remind_at"] == "2026-06-28T02:00:00+00:00"
    assert updated.json()["reminder"]["is_sent"] is False
    assert updated_row["sent_at"] is None
    assert listed.status_code == 200
    assert listed.json()["reminders"][0]["content"] == "查看构建和测试结果"
    assert deleted.status_code == 200
    assert after_delete.json()["reminders"] == []

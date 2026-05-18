from datetime import datetime, timedelta, timezone

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

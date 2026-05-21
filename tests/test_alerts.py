from app.models import Database
from app.services.alerts import handle_alert_state


def test_alert_only_sends_on_state_change(tmp_path, monkeypatch):
    sent = []

    def fake_send(settings, secret_key, subject, body):
        sent.append((subject, body))

    monkeypatch.setattr("app.services.alerts.send_email", fake_send)
    db = Database(str(tmp_path / "app.db"), "test-key")
    db.init()
    db.update_smtp_settings("smtp.example", 587, "u", "p", "from@example.com", "monitor", "to@example.com", "starttls")
    account_id = db.upsert_account(
        {
            "platform": "sub2Api",
            "name": "low",
            "base_url": "https://example.com",
            "api_key": "k",
            "threshold": 5,
        }
    )
    db.update_account_result(account_id, {"is_valid": True, "remaining": 3, "unit": "USD"})

    handle_alert_state(db, account_id)
    handle_alert_state(db, account_id)

    assert len(sent) == 1

    db.update_account_result(account_id, {"is_valid": True, "remaining": 8, "unit": "USD"})
    handle_alert_state(db, account_id)
    db.update_account_result(account_id, {"is_valid": True, "remaining": 2, "unit": "USD"})
    handle_alert_state(db, account_id)

    assert len(sent) == 2


def test_eliminated_account_skips_low_balance_alert(tmp_path, monkeypatch):
    sent = []

    def fake_send(settings, secret_key, subject, body):
        sent.append((subject, body))

    monkeypatch.setattr("app.services.alerts.send_email", fake_send)
    db = Database(str(tmp_path / "app.db"), "test-key")
    db.init()
    db.update_smtp_settings("smtp.example", 587, "u", "p", "from@example.com", "monitor", "to@example.com", "starttls")
    account_id = db.upsert_account(
        {
            "platform": "sub2Api",
            "name": "eliminated-low",
            "base_url": "https://example.com",
            "api_key": "k",
            "threshold": 5,
        }
    )
    db.update_account_result(account_id, {"is_valid": True, "remaining": 3, "unit": "USD"})
    db.update_account_eliminated(account_id, True)

    handle_alert_state(db, account_id)

    assert sent == []
    assert db.get_account(account_id)["low_balance_active"] == 0

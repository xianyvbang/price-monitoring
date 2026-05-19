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
            "is_enabled": False,
        },
    )

    account = db.get_account(account_id)

    assert account["name"] == "new"
    assert account["base_url"] == "https://new.example"
    assert account["note"] == "重要账号"
    assert account["threshold"] == 2
    assert account["is_enabled"] == 0
    assert decrypt_value(account["key_id_enc"], "test-key") == "group-1"
    assert decrypt_value(account["api_key_enc"], "test-key") == "secret"
    assert decrypt_value(account["email_enc"], "test-key") == "user@example.com"
    assert decrypt_value(account["password_enc"], "test-key") == "login-password"

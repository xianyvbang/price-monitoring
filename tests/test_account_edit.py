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
            "api_key": "secret",
        }
    )

    db.update_account(
        account_id,
        {
            "platform": "sub2Api",
            "name": "new",
            "base_url": "https://new.example",
            "api_key": "",
            "threshold": 2,
            "is_enabled": False,
        },
    )

    account = db.get_account(account_id)

    assert account["name"] == "new"
    assert account["base_url"] == "https://new.example"
    assert account["threshold"] == 2
    assert account["is_enabled"] == 0
    assert decrypt_value(account["api_key_enc"], "test-key") == "secret"

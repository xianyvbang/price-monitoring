from app.models import Database
from app.security import decrypt_value


def test_newapi_uses_api_key_as_access_token_when_missing(tmp_path):
    db = Database(str(tmp_path / "app.db"), "test-key")
    db.init()

    account_id = db.upsert_account(
        {
            "platform": "newApi",
            "name": "fallback",
            "base_url": "https://example.com",
            "api_key": "token-from-api-key",
        }
    )

    account = db.get_account(account_id)

    assert decrypt_value(account["access_token_enc"], "test-key") == "token-from-api-key"


def test_sub2api_saves_login_without_group_id(tmp_path):
    db = Database(str(tmp_path / "app.db"), "test-key")
    db.init()

    account_id = db.upsert_account(
        {
            "platform": "sub2Api",
            "name": "sub-login",
            "base_url": "https://example.com",
            "email": "user@example.com",
            "password": "login-password",
            "api_key": "sk-test",
        }
    )

    account = db.get_account(account_id)

    assert account["key_id_enc"] is None
    assert decrypt_value(account["email_enc"], "test-key") == "user@example.com"
    assert decrypt_value(account["password_enc"], "test-key") == "login-password"

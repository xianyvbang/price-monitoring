from app.models import Database, format_china_time
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


def test_account_note_is_saved_and_updated(tmp_path):
    db = Database(str(tmp_path / "app.db"), "test-key")
    db.init()
    account_id = db.upsert_account(
        {
            "platform": "sub2Api",
            "name": "sub-note",
            "base_url": "https://example.com",
            "email": "user@example.com",
            "password": "login-password",
            "api_key": "sk-test",
            "note": "first note",
        }
    )

    db.update_account(
        account_id,
        {
            "platform": "sub2Api",
            "name": "sub-note",
            "base_url": "https://example.com",
            "email": "",
            "password": "",
            "api_key": "",
            "note": "second note",
            "is_enabled": True,
        },
    )

    account = db.get_account(account_id)

    assert account["note"] == "second note"


def test_group_rate_change_status_defaults_and_updates(tmp_path):
    db = Database(str(tmp_path / "app.db"), "test-key")
    db.init()
    account_id = db.upsert_account(
        {
            "platform": "sub2Api",
            "name": "sub-rate-status",
            "base_url": "https://example.com",
            "email": "user@example.com",
            "password": "login-password",
            "api_key": "sk-test",
        }
    )

    assert db.get_account(account_id)["last_group_rate_changed"] == 0

    db.update_account_group_rate_change_status(account_id, True)

    assert db.get_account(account_id)["last_group_rate_changed"] == 1


def test_group_result_only_updates_extra(tmp_path):
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
    db.update_account_result(
        account_id,
        {
            "is_valid": True,
            "remaining": 12,
            "unit": "USD",
            "plan_name": "old-plan",
            "extra": "old-extra",
        },
    )

    db.update_account_group_result(
        account_id,
        {
            "is_valid": True,
            "plan_name": "new-group",
            "remaining": None,
            "extra": "new-extra",
        },
    )

    account = db.get_account(account_id)

    assert account["last_status"] == "valid"
    assert account["last_plan_name"] == "old-plan"
    assert account["last_remaining"] == 12
    assert account["last_unit"] == "USD"
    assert account["last_extra"] == "new-extra"


def test_account_result_keeps_group_result_even_with_extra(tmp_path):
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
    db.update_account_group_result(account_id, {"extra": "group-extra"})

    db.update_account_result(
        account_id,
        {
            "is_valid": True,
            "remaining": 8,
            "unit": "USD",
            "plan_name": "balance-plan",
            "extra": None,
        },
    )

    account = db.get_account(account_id)

    assert account["last_plan_name"] == "balance-plan"
    assert account["last_remaining"] == 8
    assert account["last_extra"] == "group-extra"

    db.update_account_result(
        account_id,
        {
            "is_valid": False,
            "invalid_message": "bad",
            "extra": "balance-extra",
        },
    )

    account = db.get_account(account_id)

    assert account["last_status"] == "invalid"
    assert account["last_error"] == "bad"
    assert account["last_extra"] == "group-extra"


def test_group_rate_records_only_insert_on_change(tmp_path):
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
    first = {
        "group": {"plan_name": "Basic", "effective_rate_multiplier": 0.8},
        "raw_json": '{"group":{"plan_name":"Basic","effective_rate_multiplier":0.8}}',
    }
    same = {
        "group": {"plan_name": "Basic", "effective_rate_multiplier": 0.8},
        "raw_json": '{"group":{"plan_name":"Basic","effective_rate_multiplier":0.8}}',
    }
    changed = {
        "group": {"plan_name": "Basic", "effective_rate_multiplier": 1.1},
        "raw_json": '{"group":{"plan_name":"Basic","effective_rate_multiplier":1.1}}',
    }

    first_result = db.record_group_rate_if_changed(account_id, first, "2026-05-19T00:00:00+00:00")
    same_result = db.record_group_rate_if_changed(account_id, same, "2026-05-19T00:01:00+00:00")
    changed_result = db.record_group_rate_if_changed(account_id, changed, "2026-05-19T00:02:00+00:00")

    records = db.list_group_rate_records(account_id)

    assert first_result["inserted"] is True
    assert first_result["changed"] is False
    assert same_result["inserted"] is False
    assert changed_result["inserted"] is True
    assert changed_result["changed"] is True
    assert changed_result["previous_rate"] == 0.8
    assert changed_result["current_rate"] == 1.1
    assert len(records) == 2
    assert records[0]["rate_multiplier"] == 1.1


def test_group_rate_records_skip_unrecognized_empty_summary(tmp_path):
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

    result = db.record_group_rate_if_changed(
        account_id,
        {
            "title": "未识别当前 apiKey 分组",
            "group_id": None,
            "groups": [],
            "active_plan_name": "钱包余额",
            "raw_json": '{"title":"未识别当前 apiKey 分组","groups":[]}',
        },
        "2026-05-19T00:00:00+00:00",
    )

    assert result["inserted"] is False
    assert db.list_group_rate_records(account_id) == []


def test_group_rate_records_cascade_delete_and_settings_default(tmp_path):
    db = Database(str(tmp_path / "app.db"), "test-key")
    db.init()
    settings = db.get_general_settings()
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
    db.record_group_rate_if_changed(
        account_id,
        {"group": {"plan_name": "Basic", "effective_rate_multiplier": 0.8}, "raw_json": "{}"},
        "2026-05-19T00:00:00+00:00",
    )

    db.update_general_settings(2, 30, 5, 600)
    db.delete_account(account_id)

    assert settings["group_rate_query_interval"] == 1200
    assert db.get_general_settings()["group_rate_query_interval"] == 600
    assert db.list_group_rate_records(account_id) == []


def test_format_china_time():
    assert format_china_time("2026-05-19T00:00:00+00:00") == "2026-05-19 08:00:00"

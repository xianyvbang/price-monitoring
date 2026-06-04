from datetime import datetime, timedelta, timezone

from app.models import BALANCE_QUERY_INTERVAL_SECONDS, GROUP_RATE_QUERY_INTERVAL_SECONDS, Database, format_china_time
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
            "recharge_url": "https://example.com/topup",
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
            "recharge_url": "https://example.com/new-topup",
            "is_enabled": True,
        },
    )

    account = db.get_account(account_id)

    assert account["note"] == "second note"
    assert account["recharge_url"] == "https://example.com/new-topup"


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


def test_account_eliminated_defaults_updates_and_survives_edit(tmp_path):
    db = Database(str(tmp_path / "app.db"), "test-key")
    db.init()
    account_id = db.upsert_account(
        {
            "platform": "sub2Api",
            "name": "sub-eliminated",
            "base_url": "https://example.com",
            "email": "user@example.com",
            "password": "login-password",
            "api_key": "sk-test",
        }
    )

    assert db.get_account(account_id)["is_eliminated"] == 0

    db.update_account_eliminated(account_id, True)
    db.update_account(
        account_id,
        {
            "platform": "sub2Api",
            "name": "sub-eliminated",
            "base_url": "https://example.com",
            "email": "",
            "password": "",
            "api_key": "",
            "is_enabled": True,
        },
    )

    assert db.get_account(account_id)["is_eliminated"] == 1

    db.update_account_eliminated(account_id, False)

    assert db.get_account(account_id)["is_eliminated"] == 0


def test_account_visibility_disables_but_does_not_restore_auto_query(tmp_path):
    db = Database(str(tmp_path / "app.db"), "test-key")
    db.init()
    account_id = db.upsert_account(
        {
            "platform": "sub2Api",
            "name": "sub-visible",
            "base_url": "https://example.com",
            "email": "user@example.com",
            "password": "login-password",
            "api_key": "sk-test",
        }
    )

    assert db.get_account(account_id)["is_visible"] == 1
    assert db.get_account(account_id)["is_enabled"] == 1

    db.update_account_visible(account_id, False)

    assert db.get_account(account_id)["is_visible"] == 0
    assert db.get_account(account_id)["is_enabled"] == 0

    db.update_account_enabled(account_id, True)

    assert db.get_account(account_id)["is_visible"] == 0
    assert db.get_account(account_id)["is_enabled"] == 0

    db.update_account_visible(account_id, True)

    assert db.get_account(account_id)["is_visible"] == 1
    assert db.get_account(account_id)["is_enabled"] == 0

    db.update_account_enabled(account_id, True)

    assert db.get_account(account_id)["is_enabled"] == 1


def test_upsert_preserves_visibility_when_updating_existing_account(tmp_path):
    db = Database(str(tmp_path / "app.db"), "test-key")
    db.init()
    account_id = db.upsert_account(
        {
            "platform": "sub2Api",
            "name": "sub-preserve",
            "base_url": "https://example.com",
            "email": "user@example.com",
            "password": "login-password",
            "api_key": "sk-test",
        }
    )
    db.update_account_visible(account_id, False)

    db.upsert_account(
        {
            "platform": "sub2Api",
            "name": "sub-preserve",
            "base_url": "https://updated.example.com",
        }
    )

    account = db.get_account(account_id)
    assert account["base_url"] == "https://updated.example.com"
    assert account["is_visible"] == 0
    assert account["is_enabled"] == 0


def test_selected_group_can_be_replaced(tmp_path):
    db = Database(str(tmp_path / "app.db"), "test-key")
    db.init()
    account_id = db.upsert_account(
        {
            "platform": "newApi",
            "name": "new-group",
            "base_url": "https://example.com",
            "access_token": "token",
            "user_id": "1",
            "key_id": "default",
        }
    )
    db.update_account_group_rate_change_status(account_id, True)

    db.update_account_selected_group(account_id, "pro")

    account = db.get_account(account_id)
    assert decrypt_value(account["key_id_enc"], "test-key") == "pro"
    assert account["last_group_rate_changed"] == 0


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


def test_update_account_name_rate_suffix_replaces_existing_suffix(tmp_path):
    db = Database(str(tmp_path / "app.db"), "test-key")
    db.init()
    account_id = db.upsert_account(
        {
            "platform": "sub2Api",
            "name": "sub-login-0.8",
            "base_url": "https://example.com",
            "email": "user@example.com",
            "password": "login-password",
            "api_key": "sk-test",
        }
    )

    updated_name = db.update_account_name_rate_suffix(account_id, 1.1)

    assert updated_name == "sub-login-1.1"
    assert db.get_account(account_id)["name"] == "sub-login-1.1"


def test_update_account_name_rate_suffix_appends_when_missing(tmp_path):
    db = Database(str(tmp_path / "app.db"), "test-key")
    db.init()
    account_id = db.upsert_account(
        {
            "platform": "newApi",
            "name": "new-login",
            "base_url": "https://example.com",
            "access_token": "token",
            "user_id": "1",
            "key_id": "basic",
        }
    )

    updated_name = db.update_account_name_rate_suffix(account_id, 0.7)

    assert updated_name == "new-login-0.7"
    assert db.get_account(account_id)["name"] == "new-login-0.7"


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


def test_balance_history_keeps_recent_fourteen_days_and_valid_balances(tmp_path):
    db = Database(str(tmp_path / "app.db"), "test-key")
    db.init()
    account_id = db.upsert_account(
        {
            "platform": "sub2Api",
            "name": "sub-history",
            "base_url": "https://example.com",
            "email": "user@example.com",
            "password": "login-password",
            "api_key": "sk-test",
        }
    )
    old_time = (datetime.now(timezone.utc) - timedelta(days=14, minutes=1)).isoformat(timespec="seconds")
    retained_time = (datetime.now(timezone.utc) - timedelta(days=13, hours=23)).isoformat(timespec="seconds")
    recent_time = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds")

    db.update_account_result(account_id, {"is_valid": True, "remaining": 4, "checked_at": old_time})
    db.update_account_result(account_id, {"is_valid": True, "remaining": 6.5, "unit": "USD", "checked_at": retained_time})
    db.update_account_result(account_id, {"is_valid": False, "remaining": 5, "checked_at": recent_time})
    db.update_account_result(account_id, {"is_valid": True, "remaining": 7.5, "unit": "USD", "checked_at": recent_time})

    records = db.list_balance_history(account_id)

    assert [record["remaining"] for record in records] == [6.5, 7.5]
    assert [record["unit"] for record in records] == ["USD", "USD"]


def test_balance_history_can_be_cleared_for_one_account(tmp_path):
    db = Database(str(tmp_path / "app.db"), "test-key")
    db.init()
    first_account_id = db.upsert_account(
        {
            "platform": "sub2Api",
            "name": "sub-history-a",
            "base_url": "https://example.com",
            "email": "user@example.com",
            "password": "login-password",
            "api_key": "sk-test",
        }
    )
    second_account_id = db.upsert_account(
        {
            "platform": "newApi",
            "name": "new-history-b",
            "base_url": "https://example.com",
            "access_token": "token",
            "user_id": "1",
        }
    )
    checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    db.update_account_result(first_account_id, {"is_valid": True, "remaining": 1, "checked_at": checked_at})
    db.update_account_result(second_account_id, {"is_valid": True, "remaining": 2, "checked_at": checked_at})

    db.clear_balance_history(first_account_id)

    assert db.list_balance_history(first_account_id) == []
    assert len(db.list_balance_history(second_account_id)) == 1


def test_today_consumption_counts_today_decreases_and_ignores_recharges(tmp_path):
    db = Database(str(tmp_path / "app.db"), "test-key")
    db.init()
    account_id = db.upsert_account(
        {
            "platform": "sub2Api",
            "name": "sub-today-consumption",
            "base_url": "https://example.com",
            "email": "user@example.com",
            "password": "login-password",
            "api_key": "sk-test",
        }
    )
    china_tz = timezone(timedelta(hours=8))
    today_start = datetime.now(china_tz).replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday = (today_start - timedelta(minutes=1)).astimezone(timezone.utc).isoformat(timespec="seconds")
    today_1 = (today_start + timedelta(hours=1)).astimezone(timezone.utc).isoformat(timespec="seconds")
    today_2 = (today_start + timedelta(hours=2)).astimezone(timezone.utc).isoformat(timespec="seconds")
    today_3 = (today_start + timedelta(hours=3)).astimezone(timezone.utc).isoformat(timespec="seconds")
    today_4 = (today_start + timedelta(hours=4)).astimezone(timezone.utc).isoformat(timespec="seconds")

    db.update_account_result(account_id, {"is_valid": True, "remaining": 200, "checked_at": yesterday})
    db.update_account_result(account_id, {"is_valid": True, "remaining": 100, "checked_at": today_1})
    db.update_account_result(account_id, {"is_valid": True, "remaining": 92.5, "checked_at": today_2})
    db.update_account_result(account_id, {"is_valid": True, "remaining": 110, "checked_at": today_3})
    db.update_account_result(account_id, {"is_valid": True, "remaining": 104.25, "checked_at": today_4})

    assert db.get_today_consumption(account_id) == 13.25


def test_consumption_stats_cover_24h_7d_and_14d_windows(tmp_path):
    db = Database(str(tmp_path / "app.db"), "test-key")
    db.init()
    account_id = db.upsert_account(
        {
            "platform": "sub2Api",
            "name": "sub-period-consumption",
            "base_url": "https://example.com",
            "email": "user@example.com",
            "password": "login-password",
            "api_key": "sk-test",
        }
    )
    now = datetime.now(timezone.utc).replace(microsecond=0)
    records = [
        (now - timedelta(days=13), 100),
        (now - timedelta(days=13) + timedelta(hours=1), 95),
        (now - timedelta(days=6), 80),
        (now - timedelta(days=6) + timedelta(hours=1), 76),
        (now - timedelta(hours=23), 50),
        (now - timedelta(hours=22), 45),
    ]
    for checked_at, remaining in records:
        db.update_account_result(
            account_id,
            {"is_valid": True, "remaining": remaining, "checked_at": checked_at.isoformat(timespec="seconds")},
        )

    stats = db.get_consumption_stats(account_id)

    assert stats["last_24h"] == 5
    assert stats["last_7d"] == 35
    assert stats["last_14d"] == 55


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


def test_group_rate_records_new_plan_is_new_baseline_not_change(tmp_path):
    db = Database(str(tmp_path / "app.db"), "test-key")
    db.init()
    account_id = db.upsert_account(
        {
            "platform": "newApi",
            "name": "new-login",
            "base_url": "https://example.com",
            "access_token": "token",
            "user_id": "1",
            "key_id": "basic",
        }
    )

    first = db.record_group_rate_if_changed(
        account_id,
        {"group": {"plan_name": "Basic", "effective_rate_multiplier": 0.8}, "raw_json": "{}"},
        "2026-05-19T00:00:00+00:00",
    )
    replaced = db.record_group_rate_if_changed(
        account_id,
        {"group": {"plan_name": "Pro", "effective_rate_multiplier": 1.2}, "raw_json": "{}"},
        "2026-05-19T00:01:00+00:00",
    )

    assert first["changed"] is False
    assert replaced["inserted"] is True
    assert replaced["changed"] is False


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

    assert settings["query_interval"] == BALANCE_QUERY_INTERVAL_SECONDS
    assert settings["group_rate_query_interval"] == GROUP_RATE_QUERY_INTERVAL_SECONDS
    assert settings["monitor_paused"] is False
    assert db.get_general_settings()["query_interval"] == BALANCE_QUERY_INTERVAL_SECONDS
    assert db.get_general_settings()["group_rate_query_interval"] == 600
    assert db.get_general_settings()["monitor_paused"] is False
    assert db.list_group_rate_records(account_id) == []


def test_monitor_pause_setting_can_be_toggled(tmp_path):
    db = Database(str(tmp_path / "app.db"), "test-key")
    db.init()

    assert db.get_general_settings()["monitor_paused"] is False

    db.set_monitor_paused(True)

    assert db.get_general_settings()["monitor_paused"] is True

    db.update_general_settings(15, BALANCE_QUERY_INTERVAL_SECONDS, 5, GROUP_RATE_QUERY_INTERVAL_SECONDS)

    assert db.get_general_settings()["monitor_paused"] is True

    db.update_general_settings(15, BALANCE_QUERY_INTERVAL_SECONDS, 5, GROUP_RATE_QUERY_INTERVAL_SECONDS, False)

    assert db.get_general_settings()["monitor_paused"] is False


def test_default_accounts_only_seed_on_first_init(tmp_path):
    db = Database(str(tmp_path / "app.db"), "test-key")
    db.init()
    accounts = db.list_accounts()
    assert [(account["platform"], account["name"], account["base_url"]) for account in accounts] == [
        ("newApi", "cctq-0.15", "https://www.cctq.ai")
    ]

    for account in accounts:
        db.delete_account(account["id"])

    assert db.list_accounts() == []

    db.init()

    assert db.list_accounts() == []


def test_format_china_time():
    assert format_china_time("2026-05-19T00:00:00+00:00") == "2026-05-19 08:00:00"

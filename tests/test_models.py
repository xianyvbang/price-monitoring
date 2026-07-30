import sqlite3
from datetime import datetime, timedelta, timezone

from app.models import (
    BALANCE_QUERY_INTERVAL_SECONDS,
    GROUP_RATE_QUERY_INTERVAL_SECONDS,
    REQUEST_TIMEOUT_SECONDS,
    TOP_MENU_VISIBILITY_DEFAULTS,
    Database,
    actual_consumption_amount,
    format_china_time,
)
from app.security import decrypt_value


def test_accounts_group_query_status_migration(tmp_path):
    database_path = tmp_path / "legacy.db"
    with sqlite3.connect(database_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE accounts (id INTEGER PRIMARY KEY)")
        Database._migrate_accounts_group_query_status(conn)
        columns = {row["name"]: row for row in conn.execute("PRAGMA table_info(accounts)")}
        conn.execute("INSERT INTO accounts (id) VALUES (1)")
        status = conn.execute("SELECT last_group_query_status FROM accounts WHERE id = 1").fetchone()[0]

    assert columns["last_group_query_status"]["notnull"] == 1
    assert columns["last_group_query_status"]["dflt_value"] == "'never'"
    assert status == "never"


def test_monitor_groups_query_status_migration(tmp_path):
    database_path = tmp_path / "legacy.db"
    with sqlite3.connect(database_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE account_monitor_groups (id INTEGER PRIMARY KEY)")
        Database._migrate_account_monitor_groups_query_status(conn)
        columns = {row["name"]: row for row in conn.execute("PRAGMA table_info(account_monitor_groups)")}
        conn.execute("INSERT INTO account_monitor_groups (id) VALUES (1)")
        status = conn.execute(
            "SELECT last_group_query_status FROM account_monitor_groups WHERE id = 1"
        ).fetchone()[0]

    assert columns["last_group_query_status"]["notnull"] == 1
    assert columns["last_group_query_status"]["dflt_value"] == "'never'"
    assert status == "never"


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


def test_sub2api_saves_refresh_token(tmp_path):
    db = Database(str(tmp_path / "app.db"), "test-key")
    db.init()

    account_id = db.upsert_account(
        {
            "platform": "sub2Api",
            "name": "sub-refresh",
            "base_url": "https://example.com",
            "api_key": "sk-test",
            "refresh_token": "rt-test",
        }
    )

    account = db.get_account(account_id)

    assert decrypt_value(account["refresh_token_enc"], "test-key") == "rt-test"


def test_sub2api_saves_and_clears_login_extra_params(tmp_path):
    db = Database(str(tmp_path / "app.db"), "test-key")
    db.init()

    account_id = db.upsert_account(
        {
            "platform": "sub2Api",
            "name": "sub-extra",
            "base_url": "https://example.com",
            "email": "user@example.com",
            "password": "login-password",
            "api_key": "sk-test",
            "login_extra_params": "not_in_cn_confirmed:true",
        }
    )

    account = db.get_account(account_id)
    assert decrypt_value(account["login_extra_params_enc"], "test-key") == "not_in_cn_confirmed:true"

    db.update_account(
        account_id,
        {
            "platform": "sub2Api",
            "name": "sub-extra",
            "base_url": "https://example.com",
            "login_extra_params": "",
        },
    )

    assert db.get_account(account_id)["login_extra_params_enc"] is None


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
    assert account["recharge_paid_amount"] == 1
    assert account["recharge_received_amount"] == 1


def test_recharge_ratio_calculates_actual_consumption(tmp_path):
    db = Database(str(tmp_path / "app.db"), "test-key")
    db.init()
    account_id = db.upsert_account(
        {
            "platform": "sub2Api",
            "name": "sub-ratio",
            "base_url": "https://example.com",
            "email": "user@example.com",
            "password": "login-password",
            "api_key": "sk-test",
            "recharge_paid_amount": 1,
            "recharge_received_amount": 2,
        }
    )
    checked_at = datetime.now(timezone.utc).replace(microsecond=0)
    db.update_account_result(account_id, {"is_valid": True, "remaining": 50, "checked_at": checked_at.isoformat(timespec="seconds")})
    db.update_account_result(account_id, {"is_valid": True, "remaining": 44.5, "checked_at": (checked_at + timedelta(minutes=1)).isoformat(timespec="seconds")})

    account = db.get_account(account_id)

    assert account["recharge_paid_amount"] == 1
    assert account["recharge_received_amount"] == 2
    assert db.get_consumption_since(account_id, checked_at.isoformat(timespec="seconds")) == 5.5
    assert actual_consumption_amount(5.5, account) == 2.75


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


def test_monitor_groups_replace_uses_group_id_diff(tmp_path):
    db = Database(str(tmp_path / "app.db"), "test-key")
    db.init()
    account_id = db.upsert_account(
        {
            "platform": "sub2Api",
            "name": "sub-diff",
            "base_url": "https://example.com",
            "api_key": "sk-test",
        }
    )
    db.replace_account_monitor_groups(
        account_id,
        [
            {"group_id": "basic", "plan_name": "Basic Plan", "effective_rate_multiplier": 0.8},
            {"group_id": "pro", "plan_name": "Pro Plan", "effective_rate_multiplier": 1.5},
        ],
    )
    db.update_account_group_rate_change_status(account_id, True, group_id="pro")

    before = {decrypt_value(row["group_id_enc"], "test-key"): dict(row) for row in db.list_monitor_groups(account_id)}
    db.replace_account_monitor_groups(
        account_id,
        [
            {"group_id": "basic", "plan_name": "Renamed Basic", "effective_rate_multiplier": 9.9},
            {"group_id": "pro", "plan_name": "Renamed Pro", "effective_rate_multiplier": 8.8},
        ],
    )
    unchanged = {decrypt_value(row["group_id_enc"], "test-key"): dict(row) for row in db.list_monitor_groups(account_id)}

    assert unchanged["basic"]["id"] == before["basic"]["id"]
    assert unchanged["basic"]["plan_name"] == "Basic Plan"
    assert unchanged["pro"]["id"] == before["pro"]["id"]
    assert unchanged["pro"]["plan_name"] == "Pro Plan"
    assert unchanged["pro"]["last_group_rate_changed"] == 1

    db.replace_account_monitor_groups(
        account_id,
        [
            {"group_id": "pro", "plan_name": "Ignored Pro Rename", "effective_rate_multiplier": 7.7},
            {"group_id": "team", "plan_name": "Team Plan", "effective_rate_multiplier": 1.1},
        ],
    )
    after = {decrypt_value(row["group_id_enc"], "test-key"): dict(row) for row in db.list_monitor_groups(account_id)}

    assert list(after) == ["pro", "team"]
    assert after["pro"]["id"] == before["pro"]["id"]
    assert after["pro"]["plan_name"] == "Pro Plan"
    assert after["pro"]["last_group_rate_changed"] == 1
    assert after["team"]["id"] not in {before["basic"]["id"], before["pro"]["id"]}
    assert after["team"]["plan_name"] == "Team Plan"
    assert decrypt_value(db.get_account(account_id)["key_id_enc"], "test-key") == "pro"


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


def test_balance_history_keeps_nine_months_but_trend_lists_recent_three_days(tmp_path):
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
    now = datetime.now(timezone.utc).replace(microsecond=0)
    expired_time = (now - timedelta(days=310)).isoformat(timespec="seconds")
    stored_but_not_trend_time = (now - timedelta(days=4)).isoformat(timespec="seconds")
    recent_time = (now - timedelta(hours=2)).isoformat(timespec="seconds")

    db.update_account_result(account_id, {"is_valid": True, "remaining": 4, "checked_at": expired_time})
    db.update_account_result(account_id, {"is_valid": True, "remaining": 6.5, "unit": "USD", "checked_at": stored_but_not_trend_time})
    db.update_account_result(account_id, {"is_valid": False, "remaining": 5, "checked_at": recent_time})
    db.update_account_result(account_id, {"is_valid": True, "remaining": 7.5, "unit": "USD", "checked_at": recent_time})

    records = db.list_balance_history(account_id)
    with db.connect() as conn:
        stored = conn.execute(
            "SELECT remaining FROM query_records WHERE account_id = ? ORDER BY checked_at ASC, id ASC",
            (account_id,),
        ).fetchall()

    assert [record["remaining"] for record in records] == [7.5]
    assert [record["unit"] for record in records] == ["USD"]
    assert [record["remaining"] for record in stored] == [6.5, 5, 7.5]


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


def test_consumption_stats_batch_multiple_accounts(tmp_path):
    db = Database(str(tmp_path / "app.db"), "test-key")
    db.init()

    def make_account(name: str) -> int:
        return db.upsert_account(
            {
                "platform": "sub2Api",
                "name": name,
                "base_url": f"https://{name}.example.com",
                "api_key": "sk-test",
            }
        )

    first_account = make_account("batch-first")
    second_account = make_account("batch-second")
    empty_account = make_account("batch-empty")
    now = datetime.now(timezone.utc).replace(microsecond=0)
    for account_id, values in (
        (first_account, [100, 94, 110, 107]),
        (second_account, [50, 48.5]),
    ):
        for offset, remaining in enumerate(values):
            db.update_account_result(
                account_id,
                {
                    "is_valid": True,
                    "remaining": remaining,
                    "checked_at": (now - timedelta(hours=len(values) - offset)).isoformat(timespec="seconds"),
                },
            )

    stats = db.get_consumption_stats_for_accounts([second_account, empty_account, first_account, first_account])

    assert list(stats) == [first_account, second_account, empty_account]
    assert stats[first_account]["last_24h"] == 9
    assert stats[second_account]["last_24h"] == 1.5
    assert stats[empty_account]["last_24h"] is None


def test_consumption_stats_cover_calendar_windows(tmp_path):
    db = Database(str(tmp_path / "app.db"), "test-key")
    db.init()

    def make_account(name: str) -> int:
        return db.upsert_account(
            {
                "platform": "sub2Api",
                "name": name,
                "base_url": "https://example.com",
                "email": "user@example.com",
                "password": "login-password",
                "api_key": "sk-test",
            }
        )

    def utc_text(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat(timespec="seconds")

    china_tz = timezone(timedelta(hours=8))
    today_start = datetime.now(china_tz).replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start = today_start - timedelta(days=1)
    this_month_start = today_start.replace(day=1)
    if this_month_start.month == 1:
        last_month_start = this_month_start.replace(year=this_month_start.year - 1, month=12)
    else:
        last_month_start = this_month_start.replace(month=this_month_start.month - 1)

    yesterday_account = make_account("sub-yesterday-consumption")
    db.update_account_result(yesterday_account, {"is_valid": True, "remaining": 40, "checked_at": utc_text(yesterday_start + timedelta(hours=1))})
    db.update_account_result(yesterday_account, {"is_valid": True, "remaining": 35, "checked_at": utc_text(yesterday_start + timedelta(hours=2))})

    this_month_account = make_account("sub-this-month-consumption")
    db.update_account_result(this_month_account, {"is_valid": True, "remaining": 90, "checked_at": utc_text(this_month_start + timedelta(hours=1))})
    db.update_account_result(this_month_account, {"is_valid": True, "remaining": 86, "checked_at": utc_text(this_month_start + timedelta(hours=2))})

    last_month_account = make_account("sub-last-month-consumption")
    db.update_account_result(last_month_account, {"is_valid": True, "remaining": 70, "checked_at": utc_text(last_month_start + timedelta(hours=1))})
    db.update_account_result(last_month_account, {"is_valid": True, "remaining": 63, "checked_at": utc_text(last_month_start + timedelta(hours=2))})
    db.update_account_result(last_month_account, {"is_valid": True, "remaining": 80, "checked_at": utc_text(last_month_start + timedelta(hours=3))})
    db.update_account_result(last_month_account, {"is_valid": True, "remaining": 78, "checked_at": utc_text(last_month_start + timedelta(hours=4))})

    assert db.get_consumption_stats(yesterday_account)["yesterday"] == 5
    assert db.get_consumption_stats(this_month_account)["this_month"] == 4
    assert db.get_consumption_stats(last_month_account)["last_month"] == 9
    assert db.get_consumption_between(last_month_account, utc_text(last_month_start), utc_text(this_month_start)) == 9


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


def test_request_timeout_defaults_to_one_minute(tmp_path):
    db = Database(str(tmp_path / "app.db"), "test-key")
    db.init()

    assert db.get_general_settings()["request_timeout"] == REQUEST_TIMEOUT_SECONDS


def test_legacy_request_timeout_default_is_upgraded(tmp_path):
    db = Database(str(tmp_path / "app.db"), "test-key")
    db.init()
    db.update_general_settings(15, BALANCE_QUERY_INTERVAL_SECONDS, 5, GROUP_RATE_QUERY_INTERVAL_SECONDS)

    db.init()

    assert db.get_general_settings()["request_timeout"] == REQUEST_TIMEOUT_SECONDS


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


def test_top_menu_visibility_defaults_and_partial_updates(tmp_path):
    db = Database(str(tmp_path / "app.db"), "test-key")
    db.init()

    assert db.get_general_settings()["top_menu_visibility"] == TOP_MENU_VISIBILITY_DEFAULTS

    db.update_general_settings(
        15,
        BALANCE_QUERY_INTERVAL_SECONDS,
        5,
        GROUP_RATE_QUERY_INTERVAL_SECONDS,
        top_menu_visibility={"dashboard": False, "logs": False, "unknown": False},
    )
    db.update_general_settings(
        15,
        BALANCE_QUERY_INTERVAL_SECONDS,
        5,
        GROUP_RATE_QUERY_INTERVAL_SECONDS,
        top_menu_visibility={"accounts": False, "logs": "invalid"},
    )

    assert db.get_general_settings()["top_menu_visibility"] == {
        "dashboard": False,
        "accounts": False,
        "platform_dispatch": True,
        "opencode_go": True,
        "logs": False,
    }

    db.set_setting("top_menu_visibility", "invalid")

    assert db.get_general_settings()["top_menu_visibility"] == TOP_MENU_VISIBILITY_DEFAULTS


def test_reminder_crud_and_update_resets_sent_state(tmp_path):
    db = Database(str(tmp_path / "app.db"), "test-key")
    db.init()
    first_time = "2026-05-19T01:00:00+00:00"
    second_time = "2026-05-20T01:00:00+00:00"

    reminder_id = db.create_reminder("开会", "准备周会材料", first_time)
    db.mark_reminder_sent(reminder_id, "2026-05-19T01:00:10+00:00")

    sent = db.get_reminder(reminder_id)
    assert sent["is_sent"] == 1
    assert sent["sent_at"] == "2026-05-19T01:00:10+00:00"

    updated = db.update_reminder(reminder_id, "更新会议", "准备复盘材料", second_time)

    assert updated["title"] == "更新会议"
    assert updated["content"] == "准备复盘材料"
    assert updated["remind_at"] == second_time
    assert updated["is_sent"] == 0
    assert updated["sent_at"] is None
    assert updated["last_error"] is None
    assert updated["last_attempt_at"] is None
    assert [row["id"] for row in db.list_reminders()] == [reminder_id]

    assert db.delete_reminder(reminder_id) is True
    assert db.get_reminder(reminder_id) is None
    assert db.delete_reminder(reminder_id) is False


def test_due_reminders_respect_retry_window(tmp_path):
    db = Database(str(tmp_path / "app.db"), "test-key")
    db.init()
    now = "2026-05-19T01:00:00+00:00"
    fresh_id = db.create_reminder("到期", "应该发送", "2026-05-19T00:59:00+00:00")
    future_id = db.create_reminder("未来", "不发送", "2026-05-19T01:01:00+00:00")
    recent_failed_id = db.create_reminder("刚失败", "暂缓重试", "2026-05-19T00:58:00+00:00")
    old_failed_id = db.create_reminder("旧失败", "可以重试", "2026-05-19T00:57:00+00:00")

    db.mark_reminder_failed(recent_failed_id, "smtp busy", "2026-05-19T00:58:30+00:00")
    db.mark_reminder_failed(old_failed_id, "smtp busy", "2026-05-19T00:54:59+00:00")

    due_ids = [row["id"] for row in db.list_due_reminders(now=now, retry_seconds=300)]

    assert due_ids == [old_failed_id, fresh_id]
    assert future_id not in due_ids
    assert recent_failed_id not in due_ids


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


def test_list_accounts_filters_by_platform_and_fuzzy_name(tmp_path):
    db = Database(str(tmp_path / "app.db"), "test-key")
    db.init()
    for account in db.list_accounts():
        db.delete_account(account["id"])

    db.upsert_account({"platform": "newApi", "name": "alpha-new", "base_url": "https://new.example"})
    db.upsert_account({"platform": "sub2Api", "name": "alpha-sub", "base_url": "https://sub.example"})
    db.upsert_account({"platform": "sub2Api", "name": "literal-%-sub", "base_url": "https://literal.example"})

    filtered = db.list_accounts(platform="sub2Api", name_query="alpha")
    literal = db.list_accounts(name_query="%")

    assert [(account["platform"], account["name"]) for account in filtered] == [("sub2Api", "alpha-sub")]
    assert [account["name"] for account in literal] == ["literal-%-sub"]


def test_format_china_time():
    assert format_china_time("2026-05-19T00:00:00+00:00") == "2026-05-19 08:00:00"

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.main import app
from app.main import config
from app.models import Database
from app.security import decrypt_value


def login(client: TestClient) -> None:
    client.post("/login", data={"username": "admin", "password": "password123"})


def assert_spa_page(response) -> None:
    assert response.status_code == 200
    assert '<div id="app"></div>' in response.text
    assert "/assets/" in response.text


def first_dashboard_row(payload: dict, platform: str = "sub2Api") -> dict:
    rows = payload["grouped"][platform]
    return next(row for row in rows if row["dashboard_is_first_row"])


def test_spa_pages_and_dashboard_api_expose_monitor_data(tmp_path, monkeypatch):
    test_db = Database(str(tmp_path / "app.db"), "test-key")
    test_db.init()
    test_db.ensure_admin("admin", "password123")
    account_id = test_db.upsert_account(
        {
            "platform": "sub2Api",
            "name": "sub",
            "base_url": "https://sub.example",
            "api_key": "secret",
            "note": "monitor note",
            "recharge_url": "https://sub.example/topup",
        }
    )
    monkeypatch.setattr("app.main.db", test_db)
    monkeypatch.setattr("app.main.scheduler.db", test_db)
    monkeypatch.setattr("app.main.scheduler.start", lambda: None)

    async def stop_scheduler():
        return None

    monkeypatch.setattr("app.main.scheduler.stop", stop_scheduler)

    with TestClient(app) as client:
        login(client)
        dashboard_page = client.get("/")
        accounts_page = client.get("/accounts")
        settings_page = client.get("/settings")
        dashboard = client.get("/api/dashboard")
        accounts = client.get("/api/accounts")
        settings = client.get("/api/settings")

    assert_spa_page(dashboard_page)
    assert_spa_page(accounts_page)
    assert_spa_page(settings_page)
    assert dashboard.status_code == 200
    assert accounts.status_code == 200
    assert settings.status_code == 200

    dashboard_payload = dashboard.json()
    row = first_dashboard_row(dashboard_payload)
    assert dashboard_payload["settings"]["query_interval"] == 300
    assert dashboard_payload["settings"]["monitor_paused"] is False
    assert row["id"] == account_id
    assert row["name"] == "sub"
    assert row["note"] == "monitor note"
    assert row["base_url"] == "https://sub.example"
    assert row["recharge_url"] == "https://sub.example/topup"
    assert row["last_group_rate_changed"] is False
    assert accounts.json()["sub2Api"][0]["id"] == account_id
    assert accounts.json()["sub2Api"][0]["recharge_url"] == "https://sub.example/topup"
    assert "group_rate_query_interval" in settings.json()["settings"]


def test_monitor_pause_api_updates_dashboard_payload(tmp_path, monkeypatch):
    test_db = Database(str(tmp_path / "app.db"), "test-key")
    test_db.init()
    test_db.ensure_admin("admin", "password123")
    monkeypatch.setattr("app.main.db", test_db)
    monkeypatch.setattr("app.main.scheduler.db", test_db)
    monkeypatch.setattr("app.main.scheduler.start", lambda: None)
    monkeypatch.setattr("app.main.scheduler.notify_settings_changed", lambda: None)

    async def stop_scheduler():
        return None

    monkeypatch.setattr("app.main.scheduler.stop", stop_scheduler)

    with TestClient(app) as client:
        login(client)
        response = client.post("/api/monitor/pause", json={"paused": True})
        dashboard = client.get("/api/dashboard")
        resume = client.post("/api/monitor/pause", json={"paused": False})

    assert response.status_code == 200
    assert response.json()["settings"]["monitor_paused"] is True
    assert dashboard.json()["settings"]["monitor_paused"] is True
    assert resume.status_code == 200
    assert resume.json()["settings"]["monitor_paused"] is False


def test_newapi_group_picker_data_and_api_routes(tmp_path, monkeypatch):
    test_db = Database(str(tmp_path / "app.db"), config.app_secret_key)
    test_db.init()
    test_db.ensure_admin("admin", "password123")
    account_id = test_db.upsert_account(
        {
            "platform": "newApi",
            "name": "new",
            "base_url": "https://new.example",
            "access_token": "token",
            "user_id": "42",
            "key_id": "pro",
        }
    )
    monkeypatch.setattr("app.main.db", test_db)
    monkeypatch.setattr("app.main.scheduler.db", test_db)
    monkeypatch.setattr("app.main.scheduler.start", lambda: None)

    async def stop_scheduler():
        return None

    monkeypatch.setattr("app.main.scheduler.stop", stop_scheduler)

    with TestClient(app) as client:
        login(client)
        dashboard_page = client.get("/")
        accounts_page = client.get("/accounts")
        dashboard = client.get("/api/dashboard")
        accounts = client.get("/api/accounts")

    assert_spa_page(dashboard_page)
    assert_spa_page(accounts_page)
    assert dashboard.status_code == 200
    assert accounts.status_code == 200
    account = next(account for account in accounts.json()["newApi"] if account["id"] == account_id)
    row = next(row for row in dashboard.json()["grouped"]["newApi"] if row["id"] == account_id)
    assert account["selected_group_id"] == "pro"
    assert row["group_rates"][0]["group_id"] == "pro"
    assert row["group_rates"][0]["plan_name"] == "当前分组 pro"
    assert row["group_rates"][0]["rate_multiplier"] is None
    assert row["id"] == account_id


def test_sub2api_group_picker_data_and_api_routes(tmp_path, monkeypatch):
    test_db = Database(str(tmp_path / "app.db"), config.app_secret_key)
    test_db.init()
    test_db.ensure_admin("admin", "password123")
    account_id = test_db.upsert_account(
        {
            "platform": "sub2Api",
            "name": "sub",
            "base_url": "https://sub.example",
            "api_key": "secret",
            "email": "user@example.com",
            "password": "password",
            "key_id": "basic",
        }
    )
    monkeypatch.setattr("app.main.db", test_db)
    monkeypatch.setattr("app.main.scheduler.db", test_db)
    monkeypatch.setattr("app.main.scheduler.start", lambda: None)

    async def stop_scheduler():
        return None

    async def fake_sub2api_group_options(account, secret_key, timeout, log=None):
        return {
            "is_valid": True,
            "selected_group_id": "basic",
            "groups": [
                {"id": "basic", "plan_name": "Basic Plan", "effective_rate_multiplier": 0.8},
                {"id": "pro", "plan_name": "Pro Plan", "effective_rate_multiplier": 1.5},
            ],
        }

    monkeypatch.setattr("app.main.scheduler.stop", stop_scheduler)
    monkeypatch.setattr("app.main.query_sub2api_group_options", fake_sub2api_group_options)

    with TestClient(app) as client:
        login(client)
        accounts_before = client.get("/api/accounts")
        dashboard_before = client.get("/api/dashboard")
        options = client.get(f"/api/accounts/{account_id}/sub2api-groups")
        selection = client.post(
            f"/api/accounts/{account_id}/selected-group",
            json={
                "group_id": "pro",
                "group": {"id": "pro", "plan_name": "Pro Plan", "effective_rate_multiplier": 1.5},
            },
        )
        dashboard_after = client.get("/api/dashboard")

    assert accounts_before.status_code == 200
    assert dashboard_before.status_code == 200
    assert options.status_code == 200
    assert selection.status_code == 200
    assert accounts_before.json()["sub2Api"][0]["selected_group_id"] == "basic"
    assert first_dashboard_row(dashboard_before.json())["group_rates"][0]["plan_name"] == "当前分组 basic"
    assert options.json()["groups"][1]["id"] == "pro"
    assert selection.json()["account"]["selected_group_id"] == "pro"
    assert selection.json()["account"]["group_rates"][0]["plan_name"] == "Pro Plan"
    assert decrypt_value(test_db.get_account(account_id)["key_id_enc"], config.app_secret_key) == "pro"
    assert first_dashboard_row(dashboard_after.json())["group_rates"][0] == {
        "monitor_group_id": selection.json()["account"]["monitor_groups"][0]["id"],
        "group_id": "pro",
        "plan_name": "Pro Plan",
        "rate_multiplier": 1.5,
    }


def test_selected_newapi_group_shows_rate_on_dashboard_api(tmp_path, monkeypatch):
    test_db = Database(str(tmp_path / "app.db"), config.app_secret_key)
    test_db.init()
    test_db.ensure_admin("admin", "password123")
    account_id = test_db.upsert_account(
        {
            "platform": "newApi",
            "name": "new",
            "base_url": "https://new.example",
            "access_token": "token",
            "user_id": "42",
        }
    )
    monkeypatch.setattr("app.main.db", test_db)
    monkeypatch.setattr("app.main.scheduler.db", test_db)
    monkeypatch.setattr("app.main.scheduler.start", lambda: None)

    async def stop_scheduler():
        return None

    monkeypatch.setattr("app.main.scheduler.stop", stop_scheduler)

    with TestClient(app) as client:
        login(client)
        response = client.post(
            f"/api/accounts/{account_id}/selected-group",
            json={
                "group_id": "pro",
                "group": {"id": "pro", "name": "专业分组", "rate": 0.75},
            },
        )
        dashboard = client.get("/api/dashboard")

    assert response.status_code == 200
    assert response.json()["account"]["group_rates"][0]["plan_name"] == "专业分组"
    assert response.json()["account"]["group_rates"][0]["rate_multiplier"] == 0.75
    selected_row = next(row for row in dashboard.json()["grouped"]["newApi"] if row["current_group_id"] == "pro")
    assert selected_row["group_rates"][0]["plan_name"] == "专业分组"
    assert selected_row["group_rates"][0]["rate_multiplier"] == 0.75


def test_dashboard_api_shows_group_rate_column_data(tmp_path, monkeypatch):
    test_db = Database(str(tmp_path / "app.db"), "test-key")
    test_db.init()
    test_db.ensure_admin("admin", "password123")
    account_id = test_db.upsert_account(
        {
            "platform": "sub2Api",
            "name": "sub",
            "base_url": "https://sub.example",
            "api_key": "secret",
            "note": "monitor note",
        }
    )
    test_db.update_account_group_result(
        account_id,
        {
            "extra": (
                '{"groups": ['
                '{"plan_name": "Basic Plan", "default_rate_multiplier": 1.2, "user_rate_multiplier": 0.8},'
                '{"plan_name": "Pro Plan", "default_rate_multiplier": 2.0, "user_rate_multiplier": null}'
                "]}"
            ),
        },
    )
    test_db.update_account_group_rate_change_status(account_id, True)
    monkeypatch.setattr("app.main.db", test_db)
    monkeypatch.setattr("app.main.scheduler.db", test_db)
    monkeypatch.setattr("app.main.scheduler.start", lambda: None)

    async def stop_scheduler():
        return None

    monkeypatch.setattr("app.main.scheduler.stop", stop_scheduler)

    with TestClient(app) as client:
        login(client)
        dashboard = client.get("/api/dashboard")

    assert dashboard.status_code == 200
    row = first_dashboard_row(dashboard.json())
    assert row["last_group_rate_changed"] is True
    assert row["group_rates"] == [
        {"plan_name": "Basic Plan", "rate_multiplier": 0.8},
        {"plan_name": "Pro Plan", "rate_multiplier": 2.0},
    ]


def test_dashboard_api_repeats_multi_group_account_as_group_rows(tmp_path, monkeypatch):
    test_db = Database(str(tmp_path / "app.db"), config.app_secret_key)
    test_db.init()
    test_db.ensure_admin("admin", "password123")
    account_id = test_db.upsert_account(
        {
            "platform": "sub2Api",
            "name": "multi-group-site",
            "base_url": "https://multi.example",
            "api_key": "secret",
            "note": "merged note",
            "recharge_url": "https://multi.example/recharge",
        }
    )
    test_db.replace_account_monitor_groups(
        account_id,
        [
            {
                "group_id": "basic",
                "plan_name": "Basic Plan",
                "effective_rate_multiplier": 0.8,
                "last_group_rate_changed": False,
            },
            {
                "group_id": "pro",
                "plan_name": "Pro Plan",
                "effective_rate_multiplier": 1.5,
                "last_group_rate_changed": True,
            },
        ],
    )
    test_db.update_account_group_rate_change_status(account_id, True, group_id="pro")
    test_db.update_account_result(account_id, {"is_valid": True, "remaining": 42, "unit": "USD"})
    monkeypatch.setattr("app.main.db", test_db)
    monkeypatch.setattr("app.main.scheduler.db", test_db)
    monkeypatch.setattr("app.main.scheduler.start", lambda: None)

    async def stop_scheduler():
        return None

    monkeypatch.setattr("app.main.scheduler.stop", stop_scheduler)

    with TestClient(app) as client:
        login(client)
        dashboard = client.get("/api/dashboard")

    assert dashboard.status_code == 200
    rows = dashboard.json()["grouped"]["sub2Api"]
    assert len(rows) == 2
    assert [row["dashboard_rowspan"] for row in rows] == [2, 2]
    assert [row["dashboard_is_first_row"] for row in rows] == [True, False]
    assert [row["dashboard_is_last_row"] for row in rows] == [False, True]
    assert [row["dashboard_row_id"] for row in rows] == [f"{account_id}:group:1", f"{account_id}:group:2"]
    assert rows[0]["name"] == "multi-group-site"
    assert rows[0]["note"] == "merged note"
    assert rows[0]["recharge_url"] == "https://multi.example/recharge"
    assert rows[0]["group_rates"][0]["plan_name"] == "Basic Plan"
    assert rows[0]["group_rates"][0]["rate_multiplier"] == 0.8
    assert rows[1]["group_rates"][0]["plan_name"] == "Pro Plan"
    assert rows[1]["group_rates"][0]["rate_multiplier"] == 1.5
    assert rows[0]["last_group_rate_changed"] is False
    assert rows[1]["last_group_rate_changed"] is True
    assert rows[1]["monitor_group"]["last_group_rate_changed"] is True


def test_dashboard_api_shows_today_consumption_summary(tmp_path, monkeypatch):
    test_db = Database(str(tmp_path / "app.db"), "test-key")
    test_db.init()
    test_db.ensure_admin("admin", "password123")
    account_id = test_db.upsert_account(
        {
            "platform": "sub2Api",
            "name": "sub-consumption",
            "base_url": "https://sub.example",
            "api_key": "secret",
        }
    )
    second_account_id = test_db.upsert_account(
        {
            "platform": "sub2Api",
            "name": "sub-consumption-2",
            "base_url": "https://sub2.example",
            "api_key": "secret",
        }
    )
    duplicate_base_url_account_id = test_db.upsert_account(
        {
            "platform": "sub2Api",
            "name": "sub-consumption-duplicate-url",
            "base_url": "https://SUB.example/",
            "api_key": "secret",
        }
    )
    china_tz = timezone(timedelta(hours=8))
    today_start = datetime.now(china_tz).replace(hour=0, minute=0, second=0, microsecond=0)
    first = (today_start + timedelta(hours=1)).astimezone(timezone.utc).isoformat(timespec="seconds")
    second = (today_start + timedelta(hours=2)).astimezone(timezone.utc).isoformat(timespec="seconds")
    third = (today_start + timedelta(hours=3)).astimezone(timezone.utc).isoformat(timespec="seconds")
    test_db.update_account_result(account_id, {"is_valid": True, "remaining": 50, "unit": "USD", "checked_at": first})
    test_db.update_account_result(account_id, {"is_valid": True, "remaining": 44.5, "unit": "USD", "checked_at": second})
    test_db.update_account_result(account_id, {"is_valid": True, "remaining": 48, "unit": "USD", "checked_at": third})
    test_db.update_account_result(second_account_id, {"is_valid": True, "remaining": 10, "checked_at": first})
    test_db.update_account_result(second_account_id, {"is_valid": True, "remaining": 8.75, "checked_at": second})
    test_db.update_account_result(duplicate_base_url_account_id, {"is_valid": True, "remaining": 100, "checked_at": first})
    test_db.update_account_result(duplicate_base_url_account_id, {"is_valid": True, "remaining": 80, "checked_at": second})
    monkeypatch.setattr("app.main.db", test_db)
    monkeypatch.setattr("app.main.scheduler.db", test_db)
    monkeypatch.setattr("app.main.scheduler.start", lambda: None)

    async def stop_scheduler():
        return None

    monkeypatch.setattr("app.main.scheduler.stop", stop_scheduler)

    with TestClient(app) as client:
        login(client)
        dashboard = client.get("/api/dashboard")

    assert dashboard.status_code == 200
    payload = dashboard.json()
    summary_by_key = {summary["key"]: summary for summary in payload["consumption_summaries"]}
    assert list(summary_by_key) == ["today", "yesterday", "last_24h", "last_7d", "last_14d", "this_month", "last_month"]
    assert summary_by_key["today"]["totals"] == [{"amount": 6.75, "unit": "USD"}]
    assert summary_by_key["today"]["account_count"] == 2
    rows = payload["grouped"]["sub2Api"]
    source_row = next(row for row in rows if row["id"] == account_id)
    assert source_row["today_consumption"] == 5.5
    assert source_row["actual_today_consumption"] == 5.5
    duplicate_row = next(row for row in rows if row["id"] == duplicate_base_url_account_id)
    assert duplicate_row["base_url"] == "https://SUB.example"
    assert "yesterday" in duplicate_row["consumption_stats"]
    assert "last_14d" in duplicate_row["consumption_stats"]
    assert "this_month" in duplicate_row["consumption_stats"]
    assert "last_month" in duplicate_row["consumption_stats"]


def test_dashboard_consumption_summary_includes_hidden_accounts(tmp_path, monkeypatch):
    test_db = Database(str(tmp_path / "app.db"), "test-key")
    test_db.init()
    test_db.ensure_admin("admin", "password123")
    visible_id = test_db.upsert_account(
        {
            "platform": "sub2Api",
            "name": "visible-consumption",
            "base_url": "https://visible.example",
            "api_key": "secret",
        }
    )
    hidden_id = test_db.upsert_account(
        {
            "platform": "sub2Api",
            "name": "hidden-consumption",
            "base_url": "https://hidden.example",
            "api_key": "secret",
            "is_visible": False,
        }
    )
    china_tz = timezone(timedelta(hours=8))
    today_start = datetime.now(china_tz).replace(hour=0, minute=0, second=0, microsecond=0)
    first = (today_start + timedelta(hours=1)).astimezone(timezone.utc).isoformat(timespec="seconds")
    second = (today_start + timedelta(hours=2)).astimezone(timezone.utc).isoformat(timespec="seconds")
    test_db.update_account_result(visible_id, {"is_valid": True, "remaining": 20, "unit": "USD", "checked_at": first})
    test_db.update_account_result(visible_id, {"is_valid": True, "remaining": 16, "unit": "USD", "checked_at": second})
    test_db.update_account_result(hidden_id, {"is_valid": True, "remaining": 30, "unit": "USD", "checked_at": first})
    test_db.update_account_result(hidden_id, {"is_valid": True, "remaining": 24, "unit": "USD", "checked_at": second})

    monkeypatch.setattr("app.main.db", test_db)
    monkeypatch.setattr("app.main.scheduler.db", test_db)
    monkeypatch.setattr("app.main.scheduler.start", lambda: None)

    async def stop_scheduler():
        return None

    monkeypatch.setattr("app.main.scheduler.stop", stop_scheduler)

    with TestClient(app) as client:
        login(client)
        dashboard = client.get("/api/dashboard")

    assert dashboard.status_code == 200
    payload = dashboard.json()
    assert [row["name"] for row in payload["grouped"]["sub2Api"]] == ["visible-consumption"]
    summary_by_key = {summary["key"]: summary for summary in payload["consumption_summaries"]}
    assert summary_by_key["today"]["totals"] == [{"amount": 10.0, "unit": "USD"}]
    assert summary_by_key["today"]["account_count"] == 2


def test_dashboard_api_shows_actual_consumption_from_recharge_ratio(tmp_path, monkeypatch):
    test_db = Database(str(tmp_path / "app.db"), "test-key")
    test_db.init()
    test_db.ensure_admin("admin", "password123")
    account_id = test_db.upsert_account(
        {
            "platform": "sub2Api",
            "name": "sub-actual-consumption",
            "base_url": "https://actual.example",
            "api_key": "secret",
            "recharge_paid_amount": 1,
            "recharge_received_amount": 2,
        }
    )
    china_tz = timezone(timedelta(hours=8))
    today_start = datetime.now(china_tz).replace(hour=0, minute=0, second=0, microsecond=0)
    first = (today_start + timedelta(hours=1)).astimezone(timezone.utc).isoformat(timespec="seconds")
    second = (today_start + timedelta(hours=2)).astimezone(timezone.utc).isoformat(timespec="seconds")
    test_db.update_account_result(account_id, {"is_valid": True, "remaining": 50, "unit": "USD", "checked_at": first})
    test_db.update_account_result(account_id, {"is_valid": True, "remaining": 44.5, "unit": "USD", "checked_at": second})
    monkeypatch.setattr("app.main.db", test_db)
    monkeypatch.setattr("app.main.scheduler.db", test_db)
    monkeypatch.setattr("app.main.scheduler.start", lambda: None)

    async def stop_scheduler():
        return None

    monkeypatch.setattr("app.main.scheduler.stop", stop_scheduler)

    with TestClient(app) as client:
        login(client)
        dashboard = client.get("/api/dashboard")

    assert dashboard.status_code == 200
    row = first_dashboard_row(dashboard.json())
    assert row["today_consumption"] == 5.5
    assert row["actual_today_consumption"] == 2.75
    assert row["actual_consumption_stats"]["today"] == 2.75


def test_dashboard_api_orders_eliminated_accounts_last(tmp_path, monkeypatch):
    test_db = Database(str(tmp_path / "app.db"), "test-key")
    test_db.init()
    test_db.ensure_admin("admin", "password123")
    for account in test_db.list_accounts():
        test_db.delete_account(account["id"])
    eliminated_id = test_db.upsert_account(
        {
            "platform": "sub2Api",
            "name": "a-eliminated-row",
            "base_url": "https://eliminated.example",
            "api_key": "secret",
        }
    )
    test_db.upsert_account(
        {
            "platform": "sub2Api",
            "name": "b-active-row",
            "base_url": "https://active.example",
            "api_key": "secret",
        }
    )
    test_db.update_account_eliminated(eliminated_id, True)
    monkeypatch.setattr("app.main.db", test_db)
    monkeypatch.setattr("app.main.scheduler.db", test_db)
    monkeypatch.setattr("app.main.scheduler.start", lambda: None)

    async def stop_scheduler():
        return None

    monkeypatch.setattr("app.main.scheduler.stop", stop_scheduler)

    with TestClient(app) as client:
        login(client)
        dashboard = client.get("/api/dashboard")

    assert dashboard.status_code == 200
    names = [row["name"] for row in dashboard.json()["grouped"]["sub2Api"]]
    assert names == ["b-active-row", "a-eliminated-row"]


def test_dashboard_and_accounts_api_filter_by_name_and_platform(tmp_path, monkeypatch):
    test_db = Database(str(tmp_path / "app.db"), "test-key")
    test_db.init()
    test_db.ensure_admin("admin", "password123")
    for account in test_db.list_accounts():
        test_db.delete_account(account["id"])
    test_db.upsert_account(
        {
            "platform": "newApi",
            "name": "alpha-new-row",
            "base_url": "https://new.example",
            "access_token": "token",
            "user_id": "42",
        }
    )
    sub_id = test_db.upsert_account(
        {
            "platform": "sub2Api",
            "name": "alpha-sub-row",
            "base_url": "https://sub.example",
            "api_key": "secret",
        }
    )
    test_db.upsert_account(
        {
            "platform": "sub2Api",
            "name": "beta-sub-row",
            "base_url": "https://beta.example",
            "api_key": "secret",
        }
    )
    monkeypatch.setattr("app.main.db", test_db)
    monkeypatch.setattr("app.main.scheduler.db", test_db)
    monkeypatch.setattr("app.main.scheduler.start", lambda: None)

    async def stop_scheduler():
        return None

    monkeypatch.setattr("app.main.scheduler.stop", stop_scheduler)

    with TestClient(app) as client:
        login(client)
        dashboard_page = client.get("/?name=alpha&platform=sub2Api")
        accounts_page = client.get("/accounts?name=alpha&platform=sub2Api")
        dashboard = client.get("/api/dashboard?name=alpha&platform=sub2Api")
        accounts = client.get("/api/accounts?name=alpha&platform=sub2Api")

    assert_spa_page(dashboard_page)
    assert_spa_page(accounts_page)
    assert dashboard.status_code == 200
    assert accounts.status_code == 200
    assert "newApi" not in dashboard.json()["grouped"]
    assert dashboard.json()["grouped"]["sub2Api"][0]["id"] == sub_id
    assert [account["name"] for account in accounts.json()["sub2Api"]] == ["alpha-sub-row"]
    assert "newApi" not in accounts.json()


def test_dashboard_api_filters_by_low_balance_state(tmp_path, monkeypatch):
    test_db = Database(str(tmp_path / "app.db"), "test-key")
    test_db.init()
    test_db.ensure_admin("admin", "password123")
    for account in test_db.list_accounts():
        test_db.delete_account(account["id"])

    low_id = test_db.upsert_account(
        {
            "platform": "sub2Api",
            "name": "low-row",
            "base_url": "https://low.example",
            "api_key": "secret",
            "threshold": 10,
        }
    )
    normal_id = test_db.upsert_account(
        {
            "platform": "sub2Api",
            "name": "normal-row",
            "base_url": "https://normal.example",
            "api_key": "secret",
            "threshold": 10,
        }
    )
    eliminated_id = test_db.upsert_account(
        {
            "platform": "sub2Api",
            "name": "eliminated-row",
            "base_url": "https://eliminated.example",
            "api_key": "secret",
            "threshold": 10,
            "is_eliminated": True,
        }
    )
    test_db.update_account_result(low_id, {"is_valid": True, "remaining": 4, "unit": "USD"})
    test_db.update_account_result(normal_id, {"is_valid": True, "remaining": 12, "unit": "USD"})
    test_db.update_account_result(eliminated_id, {"is_valid": True, "remaining": 1, "unit": "USD"})

    monkeypatch.setattr("app.main.db", test_db)
    monkeypatch.setattr("app.main.scheduler.db", test_db)
    monkeypatch.setattr("app.main.scheduler.start", lambda: None)

    async def stop_scheduler():
        return None

    monkeypatch.setattr("app.main.scheduler.stop", stop_scheduler)

    with TestClient(app) as client:
        login(client)
        low_dashboard = client.get("/api/dashboard?low_balance=low")
        normal_dashboard = client.get("/api/dashboard?low_balance=normal")

    assert low_dashboard.status_code == 200
    assert normal_dashboard.status_code == 200
    assert [row["name"] for row in low_dashboard.json()["grouped"]["sub2Api"]] == ["low-row"]
    assert [row["name"] for row in normal_dashboard.json()["grouped"]["sub2Api"]] == ["normal-row"]
    assert all(row["name"] != "eliminated-row" for row in normal_dashboard.json()["grouped"]["sub2Api"])


def test_account_visibility_and_enabled_controls_are_separate(tmp_path, monkeypatch):
    test_db = Database(str(tmp_path / "app.db"), "test-key")
    test_db.init()
    test_db.ensure_admin("admin", "password123")
    for account in test_db.list_accounts():
        test_db.delete_account(account["id"])
    enabled_id = test_db.upsert_account(
        {
            "platform": "sub2Api",
            "name": "enabled-row",
            "base_url": "https://enabled.example",
            "api_key": "secret",
        }
    )
    disabled_id = test_db.upsert_account(
        {
            "platform": "sub2Api",
            "name": "disabled-row",
            "base_url": "https://disabled.example",
            "api_key": "secret",
            "is_enabled": False,
        }
    )
    hidden_id = test_db.upsert_account(
        {
            "platform": "sub2Api",
            "name": "hidden-row",
            "base_url": "https://hidden.example",
            "api_key": "secret",
            "is_visible": False,
        }
    )
    monkeypatch.setattr("app.main.db", test_db)
    monkeypatch.setattr("app.main.scheduler.db", test_db)
    monkeypatch.setattr("app.main.scheduler.start", lambda: None)

    async def stop_scheduler():
        return None

    monkeypatch.setattr("app.main.scheduler.stop", stop_scheduler)

    with TestClient(app) as client:
        login(client)
        dashboard = client.get("/api/dashboard")
        accounts = client.get("/api/accounts")
        disable_response = client.post(
            f"/accounts/{enabled_id}/enabled",
            data={"is_enabled": "false"},
            follow_redirects=False,
        )
        dashboard_after_disable = client.get("/api/dashboard")
        hide_response = client.post(
            f"/accounts/{enabled_id}/visible",
            data={"is_visible": "false"},
            follow_redirects=False,
        )
        enable_response = client.post(
            f"/accounts/{disabled_id}/enabled",
            data={"is_enabled": "true"},
            follow_redirects=False,
        )
        show_response = client.post(
            f"/accounts/{hidden_id}/visible",
            data={"is_visible": "true"},
            follow_redirects=False,
        )
        dashboard_after_toggle = client.get("/api/dashboard")

    assert [row["name"] for row in dashboard.json()["grouped"]["sub2Api"]] == ["disabled-row", "enabled-row"]
    assert [row["name"] for row in accounts.json()["sub2Api"]] == ["disabled-row", "enabled-row", "hidden-row"]
    assert disable_response.status_code == 303
    assert hide_response.status_code == 303
    assert enable_response.status_code == 303
    assert show_response.status_code == 303
    assert test_db.get_account(enabled_id)["is_enabled"] == 0
    assert test_db.get_account(enabled_id)["is_visible"] == 0
    assert test_db.get_account(disabled_id)["is_enabled"] == 1
    assert test_db.get_account(hidden_id)["is_visible"] == 1
    assert test_db.get_account(hidden_id)["is_enabled"] == 0
    assert "enabled-row" in [row["name"] for row in dashboard_after_disable.json()["grouped"]["sub2Api"]]
    assert "enabled-row" not in [row["name"] for row in dashboard_after_toggle.json()["grouped"]["sub2Api"]]
    assert "disabled-row" in [row["name"] for row in dashboard_after_toggle.json()["grouped"]["sub2Api"]]
    assert "hidden-row" in [row["name"] for row in dashboard_after_toggle.json()["grouped"]["sub2Api"]]


def test_api_update_sub2api_visibility_only_does_not_require_login(tmp_path, monkeypatch):
    test_db = Database(str(tmp_path / "app.db"), config.app_secret_key)
    test_db.init()
    test_db.ensure_admin("admin", "password123")
    account_id = test_db.upsert_account(
        {
            "platform": "sub2Api",
            "name": "hidden-toggle",
            "base_url": "https://sub.example",
            "api_key": "secret",
            "email": "user@example.com",
            "password": "password",
            "is_visible": True,
            "is_enabled": True,
        }
    )
    monkeypatch.setattr("app.main.db", test_db)
    monkeypatch.setattr("app.main.scheduler.db", test_db)
    monkeypatch.setattr("app.main.scheduler.start", lambda: None)

    async def stop_scheduler():
        return None

    async def fail_login(*args, **kwargs):
        raise AssertionError("visibility-only update should not call sub2Api login")

    monkeypatch.setattr("app.main.scheduler.stop", stop_scheduler)
    monkeypatch.setattr("app.main.login_sub2api_tokens", fail_login)

    with TestClient(app) as client:
        login(client)
        response = client.put(
            f"/api/accounts/{account_id}",
            json={
                "platform": "sub2Api",
                "name": "hidden-toggle",
                "base_url": "https://sub.example",
                "api_key": "",
                "email": "",
                "password": "",
                "is_visible": False,
                "is_enabled": False,
            },
        )
        dashboard = client.get("/api/dashboard")

    assert response.status_code == 200
    assert response.json()["account"]["is_visible"] is False
    assert response.json()["account"]["is_enabled"] is False
    assert "hidden-toggle" not in [row["name"] for row in dashboard.json()["grouped"].get("sub2Api", [])]


def test_group_rate_change_status_reset_api(tmp_path, monkeypatch):
    test_db = Database(str(tmp_path / "app.db"), "test-key")
    test_db.init()
    test_db.ensure_admin("admin", "password123")
    account_id = test_db.upsert_account(
        {
            "platform": "sub2Api",
            "name": "sub",
            "base_url": "https://sub.example",
            "api_key": "secret",
            "email": "user@example.com",
            "password": "password",
        }
    )
    test_db.update_account_group_rate_change_status(account_id, True)
    monkeypatch.setattr("app.main.db", test_db)
    monkeypatch.setattr("app.main.scheduler.db", test_db)
    monkeypatch.setattr("app.main.scheduler.start", lambda: None)

    async def stop_scheduler():
        return None

    monkeypatch.setattr("app.main.scheduler.stop", stop_scheduler)

    with TestClient(app) as client:
        login(client)
        response = client.post(
            f"/api/accounts/{account_id}/group-rate-change-status",
            json={"changed": False},
        )
        dashboard = client.get("/api/dashboard")

    assert response.status_code == 200
    assert response.json()["account"]["last_group_rate_changed"] == 0
    assert test_db.get_account(account_id)["last_group_rate_changed"] == 0
    assert first_dashboard_row(dashboard.json())["last_group_rate_changed"] is False


def test_group_rate_history_spa_page_and_api(tmp_path, monkeypatch):
    test_db = Database(str(tmp_path / "app.db"), "test-key")
    test_db.init()
    test_db.ensure_admin("admin", "password123")
    account_id = test_db.upsert_account(
        {
            "platform": "sub2Api",
            "name": "sub",
            "base_url": "https://sub.example",
            "api_key": "secret",
            "note": "monitor note",
        }
    )
    test_db.record_group_rate_if_changed(
        account_id,
        {
            "group": {"plan_name": "Basic Plan", "effective_rate_multiplier": 0.8},
            "raw_json": '{"group":{"plan_name":"Basic Plan","effective_rate_multiplier":0.8}}',
        },
        "2026-05-19T00:00:00+00:00",
    )
    monkeypatch.setattr("app.main.db", test_db)
    monkeypatch.setattr("app.main.scheduler.db", test_db)
    monkeypatch.setattr("app.main.scheduler.start", lambda: None)

    async def stop_scheduler():
        return None

    monkeypatch.setattr("app.main.scheduler.stop", stop_scheduler)

    with TestClient(app) as client:
        login(client)
        page = client.get(f"/accounts/{account_id}/group-rates")
        api = client.get(f"/api/accounts/{account_id}/group-rates")

    assert_spa_page(page)
    assert api.status_code == 200
    assert api.json()["records"][0]["plan_name"] == "Basic Plan"
    assert api.json()["records"][0]["rate_multiplier"] == 0.8
    assert api.json()["records"][0]["checked_at"] == "2026-05-19T00:00:00+00:00"

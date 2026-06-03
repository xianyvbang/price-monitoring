from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.main import app
from app.main import config
from app.models import Database


def test_group_query_buttons_use_api_fetch(tmp_path, monkeypatch):
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
        client.post("/login", data={"username": "admin", "password": "password123"})
        dashboard = client.get("/")
        accounts = client.get("/accounts")
        settings = client.get("/settings")

    assert dashboard.status_code == 200
    assert accounts.status_code == 200
    assert settings.status_code == 200
    assert 'data-refresh-interval="300"' in dashboard.text
    assert 'data-monitor-toggle' in dashboard.text
    assert "暂停监控" in dashboard.text
    assert "备注" in dashboard.text
    assert "monitor note" in dashboard.text
    assert "充值路径" in accounts.text
    assert "https://sub.example/topup" in dashboard.text
    assert "https://sub.example/topup" in accounts.text
    assert "倍率变化" in dashboard.text
    assert "未变化" in dashboard.text
    assert "重置" in dashboard.text
    assert 'data-group-rate-reset' in dashboard.text
    assert 'action="/query-all"' not in dashboard.text
    assert "/accounts/1/query" not in dashboard.text
    assert "/accounts/1/group-query" not in dashboard.text
    assert "/accounts/1/group-query" not in accounts.text
    assert 'fetch("/api/query-all"' in dashboard.text
    assert "/api/accounts/${button.dataset.accountId}/query" in dashboard.text
    assert "/api/accounts/${button.dataset.accountId}/group-query" in dashboard.text
    assert "/api/accounts/${button.dataset.accountId}/group-query" in accounts.text
    assert f"/accounts/{account_id}/group-rates" in dashboard.text
    assert "group_rate_query_interval" in settings.text


def test_monitor_pause_api_updates_dashboard(tmp_path, monkeypatch):
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
        client.post("/login", data={"username": "admin", "password": "password123"})
        response = client.post("/api/monitor/pause", json={"paused": True})
        dashboard = client.get("/")
        resume = client.post("/api/monitor/pause", json={"paused": False})

    assert response.status_code == 200
    assert response.json()["settings"]["monitor_paused"] is True
    assert dashboard.status_code == 200
    assert "自动监控已暂停" in dashboard.text
    assert "恢复监控" in dashboard.text
    assert resume.status_code == 200
    assert resume.json()["settings"]["monitor_paused"] is False


def test_newapi_group_picker_ui_and_api_routes(tmp_path, monkeypatch):
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
        client.post("/login", data={"username": "admin", "password": "password123"})
        dashboard = client.get("/")
        accounts = client.get("/accounts")

    assert dashboard.status_code == 200
    assert accounts.status_code == 200
    assert "重新获取分组" in dashboard.text
    assert "重新获取分组" in accounts.text
    assert "当前分组: pro" in accounts.text
    assert "当前分组 pro: -" in dashboard.text
    assert 'data-group-picker' in dashboard.text
    assert 'data-group-picker' in accounts.text
    assert f"/accounts/{account_id}/group-rates" in dashboard.text
    assert "/api/accounts/${button.dataset.accountId}/newapi-groups" in dashboard.text
    assert "/api/accounts/${accountId}/selected-group" in accounts.text


def test_selected_newapi_group_shows_rate_on_dashboard(tmp_path, monkeypatch):
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
        client.post("/login", data={"username": "admin", "password": "password123"})
        response = client.post(
            f"/api/accounts/{account_id}/selected-group",
            json={
                "group_id": "pro",
                "group": {"id": "pro", "name": "专业分组", "rate": 0.75},
            },
        )
        dashboard = client.get("/")

    assert response.status_code == 200
    assert response.json()["account"]["group_rates"][0]["plan_name"] == "专业分组"
    assert response.json()["account"]["group_rates"][0]["rate_multiplier"] == 0.75
    assert "专业分组: 0.75" in dashboard.text


def test_dashboard_shows_group_rate_column(tmp_path, monkeypatch):
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
        client.post("/login", data={"username": "admin", "password": "password123"})
        dashboard = client.get("/")

    assert dashboard.status_code == 200
    assert "分组倍率" in dashboard.text
    assert "倍率变化" in dashboard.text
    assert "变化" in dashboard.text
    assert 'change-status changed' in dashboard.text
    assert "Basic Plan: 0.8" in dashboard.text
    assert "Pro Plan: 2.0" in dashboard.text


def test_dashboard_shows_today_consumption_column(tmp_path, monkeypatch):
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
    china_tz = timezone(timedelta(hours=8))
    today_start = datetime.now(china_tz).replace(hour=0, minute=0, second=0, microsecond=0)
    first = (today_start + timedelta(hours=1)).astimezone(timezone.utc).isoformat(timespec="seconds")
    second = (today_start + timedelta(hours=2)).astimezone(timezone.utc).isoformat(timespec="seconds")
    third = (today_start + timedelta(hours=3)).astimezone(timezone.utc).isoformat(timespec="seconds")
    test_db.update_account_result(account_id, {"is_valid": True, "remaining": 50, "unit": "USD", "checked_at": first})
    test_db.update_account_result(account_id, {"is_valid": True, "remaining": 44.5, "unit": "USD", "checked_at": second})
    test_db.update_account_result(account_id, {"is_valid": True, "remaining": 48, "unit": "USD", "checked_at": third})
    monkeypatch.setattr("app.main.db", test_db)
    monkeypatch.setattr("app.main.scheduler.db", test_db)
    monkeypatch.setattr("app.main.scheduler.start", lambda: None)

    async def stop_scheduler():
        return None

    monkeypatch.setattr("app.main.scheduler.stop", stop_scheduler)

    with TestClient(app) as client:
        client.post("/login", data={"username": "admin", "password": "password123"})
        dashboard = client.get("/")

    assert dashboard.status_code == 200
    assert "今日消耗" in dashboard.text
    assert "5.5 USD" in dashboard.text


def test_dashboard_orders_eliminated_accounts_last(tmp_path, monkeypatch):
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
        client.post("/login", data={"username": "admin", "password": "password123"})
        dashboard = client.get("/")

    assert dashboard.status_code == 200
    assert "套餐" not in dashboard.text
    assert dashboard.text.index("b-active-row") < dashboard.text.index("a-eliminated-row")


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
        client.post("/login", data={"username": "admin", "password": "password123"})
        dashboard = client.get("/")
        accounts = client.get("/accounts")
        disable_response = client.post(
            f"/accounts/{enabled_id}/enabled",
            data={"is_enabled": "false"},
            follow_redirects=False,
        )
        dashboard_after_disable = client.get("/")
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
        dashboard_after_toggle = client.get("/")

    assert dashboard.status_code == 200
    assert accounts.status_code == 200
    assert "enabled-row" in dashboard.text
    assert "disabled-row" in dashboard.text
    assert "hidden-row" not in dashboard.text
    assert "disabled-row" in accounts.text
    assert "hidden-row" in accounts.text
    assert f'action="/accounts/{disabled_id}/enabled"' in accounts.text
    assert f'action="/accounts/{hidden_id}/visible"' in accounts.text
    assert 'class="enable-state disabled"' in accounts.text
    assert disable_response.status_code == 303
    assert hide_response.status_code == 303
    assert enable_response.status_code == 303
    assert show_response.status_code == 303
    assert test_db.get_account(enabled_id)["is_enabled"] == 0
    assert test_db.get_account(enabled_id)["is_visible"] == 0
    assert test_db.get_account(disabled_id)["is_enabled"] == 1
    assert test_db.get_account(hidden_id)["is_visible"] == 1
    assert test_db.get_account(hidden_id)["is_enabled"] == 0
    assert "enabled-row" in dashboard_after_disable.text
    assert "enabled-row" not in dashboard_after_toggle.text
    assert "disabled-row" in dashboard_after_toggle.text
    assert "hidden-row" in dashboard_after_toggle.text


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
        client.post("/login", data={"username": "admin", "password": "password123"})
        response = client.post(
            f"/api/accounts/{account_id}/group-rate-change-status",
            json={"changed": False},
        )
        dashboard = client.get("/")

    assert response.status_code == 200
    assert response.json()["account"]["last_group_rate_changed"] == 0
    assert test_db.get_account(account_id)["last_group_rate_changed"] == 0
    assert 'class="change-status unchanged"' in dashboard.text


def test_group_rate_history_page_and_api(tmp_path, monkeypatch):
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
        client.post("/login", data={"username": "admin", "password": "password123"})
        page = client.get(f"/accounts/{account_id}/group-rates")
        api = client.get(f"/api/accounts/{account_id}/group-rates")

    assert page.status_code == 200
    assert "Basic Plan" in page.text
    assert "0.8" in page.text
    assert "2026-05-19 08:00:00" in page.text
    assert "查看 JSON" in page.text
    assert api.status_code == 200
    assert api.json()["records"][0]["plan_name"] == "Basic Plan"

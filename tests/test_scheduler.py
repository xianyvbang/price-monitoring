import json
import asyncio

import pytest

from app.models import Database
from app.services.scheduler import BalanceScheduler, query_all_group_rates, query_group_rate_for_account


def _sub2api_account(name: str, enabled: bool = True, credentials: bool = True) -> dict:
    data = {
        "platform": "sub2Api",
        "name": name,
        "base_url": "https://sub.example",
        "api_key": "sk-test",
        "is_enabled": enabled,
    }
    if credentials:
        data.update({"email": "user@example.com", "password": "password"})
    return data


def _newapi_account(name: str, enabled: bool = True, selected_group: bool = True) -> dict:
    data = {
        "platform": "newApi",
        "name": name,
        "base_url": "https://new.example",
        "access_token": "token",
        "user_id": "1",
        "is_enabled": enabled,
    }
    if selected_group:
        data["key_id"] = "pro"
    return data


def _group_result(plan_name: str, rate: float) -> dict:
    extra = json.dumps(
        {
            "title": f"{plan_name} 倍率 {rate}",
            "group": {
                "plan_name": plan_name,
                "default_rate_multiplier": 1.2,
                "user_rate_multiplier": rate,
                "effective_rate_multiplier": rate,
            },
            "groups": [
                {
                    "plan_name": plan_name,
                    "default_rate_multiplier": 1.2,
                    "user_rate_multiplier": rate,
                    "effective_rate_multiplier": rate,
                }
            ],
        },
        ensure_ascii=False,
    )
    return {"is_valid": True, "plan_name": f"{plan_name} 倍率 {rate}", "extra": extra}


@pytest.mark.asyncio
async def test_query_group_rate_records_baseline_and_emails_only_on_change(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "app.db"), "test-key")
    db.init()
    account_id = db.upsert_account(_sub2api_account("sub-0.8"))
    results = [
        _group_result("Basic", 0.8),
        _group_result("Basic", 0.8),
        _group_result("Basic", 1.1),
        _group_result("Basic", 1.1),
    ]
    sent = []

    async def fake_query(account, secret_key, timeout, log):
        return results.pop(0)

    def fake_send(settings, secret_key, subject, body):
        sent.append((subject, body))

    monkeypatch.setattr("app.services.scheduler.query_sub2api_group", fake_query)
    monkeypatch.setattr("app.services.scheduler.send_email", fake_send)

    first = await query_group_rate_for_account(db, account_id, notify=True)
    same = await query_group_rate_for_account(db, account_id, notify=True)
    changed = await query_group_rate_for_account(db, account_id, notify=True)
    sticky = await query_group_rate_for_account(db, account_id, notify=True)

    records = db.list_group_rate_records(account_id)
    account = db.get_account(account_id)

    assert first["group_rate_record"]["inserted"] is True
    assert first["group_rate_record"]["changed"] is False
    assert first["group_rate_changed"] is False
    assert same["group_rate_record"]["inserted"] is False
    assert changed["group_rate_record"]["changed"] is True
    assert changed["group_rate_changed"] is True
    assert sticky["group_rate_record"]["changed"] is False
    assert sticky["group_rate_changed"] is True
    assert len(records) == 2
    assert records[0]["rate_multiplier"] == 1.1
    assert account["last_extra"] == changed["extra"]
    assert account["last_group_rate_changed"] == 1
    assert account["name"] == "sub-1.1"
    assert len(sent) == 1
    assert sent[0][0] == "分组倍率变化: sub-1.1"
    assert "旧倍率: 0.8" in sent[0][1]
    assert "新倍率: 1.1" in sent[0][1]
    assert "查询时间:" in sent[0][1]


@pytest.mark.asyncio
async def test_eliminated_account_skips_group_rate_change_email(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "app.db"), "test-key")
    db.init()
    account_id = db.upsert_account(_sub2api_account("sub-0.8"))
    results = [
        _group_result("Basic", 0.8),
        _group_result("Basic", 1.1),
    ]
    sent = []

    async def fake_query(account, secret_key, timeout, log):
        return results.pop(0)

    def fake_send(settings, secret_key, subject, body):
        sent.append((subject, body))

    monkeypatch.setattr("app.services.scheduler.query_sub2api_group", fake_query)
    monkeypatch.setattr("app.services.scheduler.send_email", fake_send)

    await query_group_rate_for_account(db, account_id, notify=True)
    db.update_account_eliminated(account_id, True)
    changed = await query_group_rate_for_account(db, account_id, notify=True)

    account = db.get_account(account_id)

    assert changed["group_rate_record"]["changed"] is True
    assert changed["group_rate_changed"] is True
    assert account["last_group_rate_changed"] == 1
    assert account["name"] == "sub-1.1"
    assert sent == []


@pytest.mark.asyncio
async def test_query_all_group_rates_only_queries_enabled_sub2api_with_credentials(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "app.db"), "test-key")
    db.init()
    enabled_id = db.upsert_account(_sub2api_account("enabled"))
    db.upsert_account(_sub2api_account("disabled", enabled=False))
    db.upsert_account(_sub2api_account("missing-creds", credentials=False))
    db.upsert_account(
        {
            "platform": "newApi",
            "name": "new",
            "base_url": "https://new.example",
            "access_token": "token",
            "user_id": "1",
        }
    )
    called = []

    async def fake_query(account, secret_key, timeout, log):
        called.append(account["id"])
        return _group_result("Basic", 0.8)

    monkeypatch.setattr("app.services.scheduler.query_sub2api_group", fake_query)

    results = await query_all_group_rates(db, notify=False)
    logs = db.list_logs()

    assert [result["is_valid"] for result in results] == [True]
    assert called == [enabled_id]
    assert len(db.list_group_rate_records(enabled_id)) == 1
    assert any("自动查组跳过: 缺少 apiKey/email/password" in log["message"] for log in logs)


@pytest.mark.asyncio
async def test_query_all_group_rates_queries_selected_newapi_group(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "app.db"), "test-key")
    db.init()
    sub_id = db.upsert_account(_sub2api_account("sub"))
    new_id = db.upsert_account(_newapi_account("new"))
    db.upsert_account(_newapi_account("new-missing-group", selected_group=False))
    called_sub = []
    called_new = []

    async def fake_sub_query(account, secret_key, timeout, log):
        called_sub.append(account["id"])
        return _group_result("Basic", 0.8)

    async def fake_new_query(account, secret_key, timeout, log):
        called_new.append(account["id"])
        return _group_result("Pro", 0.7)

    monkeypatch.setattr("app.services.scheduler.query_sub2api_group", fake_sub_query)
    monkeypatch.setattr("app.services.scheduler.query_newapi_group", fake_new_query)

    results = await query_all_group_rates(db, notify=False)
    logs = db.list_logs()

    assert [result["is_valid"] for result in results] == [True, True]
    assert called_sub == [sub_id]
    assert called_new == [new_id]
    assert len(db.list_group_rate_records(new_id)) == 1
    assert any("自动查组跳过: 缺少 accessToken/userId/已选分组" in log["message"] for log in logs)


@pytest.mark.asyncio
async def test_scheduler_skips_automatic_queries_while_monitor_paused(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "app.db"), "test-key")
    db.init()
    db.set_monitor_paused(True)
    scheduler = BalanceScheduler(db)
    balance_calls = []
    group_calls = []

    async def fake_query_all_accounts(target_db):
        balance_calls.append(target_db)
        return []

    async def fake_query_all_group_rates(target_db, notify=True):
        group_calls.append((target_db, notify))
        return []

    monkeypatch.setattr("app.services.scheduler.query_all_accounts", fake_query_all_accounts)
    monkeypatch.setattr("app.services.scheduler.query_all_group_rates", fake_query_all_group_rates)

    scheduler.start()
    await asyncio.sleep(0.02)

    assert balance_calls == []
    assert group_calls == []

    db.set_monitor_paused(False)
    scheduler.notify_settings_changed()
    await asyncio.sleep(0.02)
    await scheduler.stop()

    assert len(balance_calls) == 1
    assert len(group_calls) == 1

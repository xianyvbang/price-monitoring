from datetime import datetime, timedelta, timezone

import pytest

from app.models import Database
from app.services.platform_dispatch_policy import (
    POLICY_DEFAULTS,
    PlatformDispatchPolicyScheduler,
    allocate_weighted_increments,
    allocate_weighted_points,
    calculate_health,
    classify_activity,
    validate_policy_config,
)


def make_db(tmp_path):
    db = Database(str(tmp_path / "policy.db"), "test-key")
    db.init()
    return db


def healthy_state(score=100.0):
    return {
        "health_score": score,
        "short_score": score,
        "long_score": score,
        "evidence_count": 1,
        "evidence_at": "2026-07-26T08:00:00+00:00",
        "evidence_fresh": True,
        "latest_probe_success_at": "2026-07-26T08:00:00+00:00",
        "evidence": [],
    }


def test_health_classification_and_formula():
    assert classify_activity({"is_error": False})["score"] == 100
    assert classify_activity({"is_error": False, "first_token_ms": 15001})["score"] == 65
    assert classify_activity({"is_error": True, "status_code": 429})["score"] == 25
    assert classify_activity({"is_error": True, "is_timeout": True})["score"] == 10
    fatal = classify_activity({"is_error": True, "status_code": 429, "message": "余额不足"})
    assert fatal["category"] == "fatal_balance"
    assert fatal["score"] == 0

    now = datetime(2026, 7, 26, 8, tzinfo=timezone.utc)
    evidence = [
        {"score": score, "occurred_at": (now - timedelta(seconds=index)).isoformat()}
        for index, score in enumerate([100] + [50] * 9)
    ]
    result = calculate_health(evidence, now, 180)
    assert result["short_score"] == 75
    assert result["long_score"] == 55
    assert result["health_score"] == 69
    assert result["evidence_fresh"] is True
    expired = calculate_health(
        [{"score": 100, "occurred_at": (now - timedelta(seconds=181)).isoformat()}], now, 180
    )
    assert expired["evidence_fresh"] is False


def test_config_validation_and_deterministic_allocators():
    with pytest.raises(ValueError):
        validate_policy_config({"account_min_concurrency": 251, "account_max_concurrency": 250})
    with pytest.raises(ValueError):
        validate_policy_config({"failure_window": 2, "failure_threshold": 3})
    assert allocate_weighted_points([2, 1], {1: 1, 2: 1}, 41, 20, 500) == {1: 21, 2: 20}
    assert allocate_weighted_points([1, 2], {1: 1, 2: 1}, 2000, 20, 500) == {1: 500, 2: 500}
    assert allocate_weighted_increments({1: 245, 2: 100}, {1: 10, 2: 1}, 30, 250) == {1: 5, 2: 25}


def test_evidence_deduplicates_and_keeps_latest_60(tmp_path):
    db = make_db(tmp_path)
    site = "https://sub.example"
    for index in range(65):
        db.add_platform_dispatch_evidence(
            site,
            {
                "account_id": 1,
                "source_kind": "usage",
                "source_id": str(index),
                "category": "healthy",
                "score": 100,
                "occurred_at": f"2026-07-26T08:{index:02d}:00+00:00",
            },
        )
    assert not db.add_platform_dispatch_evidence(
        site,
        {
            "account_id": 1,
            "source_kind": "usage",
            "source_id": "64",
            "category": "healthy",
            "score": 100,
            "occurred_at": "2026-07-26T09:00:00+00:00",
        },
    )
    records = db.list_platform_dispatch_evidence(site, 1)
    assert len(records) == 60
    assert {record["source_id"] for record in records}.isdisjoint({"0", "1", "2", "3", "4"})


class PolicyClient:
    site_url = "https://sub.example"

    def __init__(self, accounts):
        self.accounts = accounts
        self.probes = []
        self.updates = []
        self.realtime_reads = 0

    async def list_accounts(self, **kwargs):
        return [dict(account) for account in self.accounts]

    async def list_groups(self, **kwargs):
        return []

    async def list_recent_usage(self, account_id, limit):
        return []

    async def list_recent_errors(self, account_id, limit):
        return []

    async def probe_account(self, account_id):
        self.probes.append(account_id)
        return {"success": True, "message": "ok"}

    async def get_concurrency_stats(self, platform=None):
        self.realtime_reads += 1
        return {"account": {}}

    async def get_account_availability(self, platform=None):
        self.realtime_reads += 1
        return {"account": {}}

    async def update_account_status(self, account_id, enabled):
        self.updates.append((account_id, "status", "active" if enabled else "inactive"))
        account = next(item for item in self.accounts if item["id"] == account_id)
        account["status"] = "active" if enabled else "inactive"
        return dict(account)

    async def update_account_fields(self, account_id, fields):
        self.updates.append((account_id, next(iter(fields)), next(iter(fields.values()))))
        account = next(item for item in self.accounts if item["id"] == account_id)
        account.update(fields)
        return dict(account)


def prepare_cache(db, accounts):
    db.replace_platform_dispatch_cache(
        "https://sub.example", accounts, [], [], {"platform": "", "type": "", "status": ""}
    )


@pytest.mark.asyncio
async def test_master_off_only_collects_and_never_probes_or_writes(tmp_path):
    db = make_db(tmp_path)
    accounts = [{"id": 1, "name": "one", "status": "inactive", "concurrency": 10}]
    prepare_cache(db, accounts)
    client = PolicyClient(accounts)
    scheduler = PlatformDispatchPolicyScheduler(db, lambda: client)

    await scheduler.run_once()

    assert client.probes == []
    assert client.updates == []
    assert client.realtime_reads == 0


@pytest.mark.asyncio
async def test_minimum_pool_recovers_with_all_subpolicies_off(tmp_path):
    db = make_db(tmp_path)
    accounts = [{"id": 1, "name": "one", "status": "inactive", "concurrency": 20}]
    prepare_cache(db, accounts)
    db.save_platform_dispatch_policy({**POLICY_DEFAULTS, "enabled": True}, "https://sub.example")
    client = PolicyClient(accounts)
    scheduler = PlatformDispatchPolicyScheduler(db, lambda: client)

    summary = await scheduler.run_once()

    assert client.probes == [1]
    assert client.updates == [(1, "status", "active")]
    assert summary["status_action"].startswith("启用 one")


@pytest.mark.asyncio
async def test_status_policy_switches_only_one_account_per_round(tmp_path):
    db = make_db(tmp_path)
    accounts = {
        1: {"id": 1, "name": "one", "status": "active"},
        2: {"id": 2, "name": "two", "status": "active"},
    }
    client = PolicyClient(list(accounts.values()))
    scheduler = PlatformDispatchPolicyScheduler(db, lambda: client)
    health = {
        account_id: {
            **healthy_state(0),
            "evidence": [{"category": "fatal_auth", "score": 0, "occurred_at": "2026-07-26T08:00:00Z"}],
        }
        for account_id in accounts
    }

    await scheduler._apply_status_policy(
        client, client.site_url, accounts, health, [1, 2], {**POLICY_DEFAULTS, "enabled": True}, 180
    )

    assert len(client.updates) == 1
    assert client.updates[0][1:] == ("status", "inactive")


@pytest.mark.asyncio
async def test_smart_expansion_floors_low_and_preserves_above_max(tmp_path):
    db = make_db(tmp_path)
    accounts = {
        1: {"id": 1, "name": "low", "status": "active", "concurrency": 10, "rate_multiplier": 1},
        2: {"id": 2, "name": "high", "status": "active", "concurrency": 300, "rate_multiplier": 1},
    }
    client = PolicyClient(list(accounts.values()))
    scheduler = PlatformDispatchPolicyScheduler(db, lambda: client)
    health = {1: healthy_state(), 2: healthy_state()}
    realtime = {1: {"current_in_use": 20}, 2: {"current_in_use": 300}}

    await scheduler._apply_concurrency_policy(
        client,
        client.site_url,
        accounts,
        health,
        realtime,
        {},
        {**POLICY_DEFAULTS, "smart_expand_enabled": True},
    )

    assert client.updates == [(1, "concurrency", 52)]
    assert accounts[2]["concurrency"] == 300


@pytest.mark.asyncio
async def test_load_factor_deadband_price_protection_and_cooldown(tmp_path):
    db = make_db(tmp_path)
    account = {
        "id": 1,
        "name": "one",
        "status": "active",
        "concurrency": 100,
        "load_factor": 100,
        "rate_multiplier": 2,
        "group_ids": [8, 9],
    }
    accounts = {1: account}
    client = PolicyClient([account])
    scheduler = PlatformDispatchPolicyScheduler(db, lambda: client)
    config = {
        **POLICY_DEFAULTS,
        "price_protection_enabled": True,
        "load_change_threshold_percent": 10,
    }

    await scheduler._apply_load_policy(client, client.site_url, accounts, {1: healthy_state()}, {8: 1.8, 9: 1.0}, config)
    assert client.updates == [(1, "load_factor", 50)]
    state = db.get_platform_dispatch_account_state(client.site_url, 1)
    assert state["baseline_load_factor"] == 100
    assert state["target_load_factor"] == 50

    client.updates.clear()
    account["load_factor"] = 52
    await scheduler._apply_load_policy(client, client.site_url, accounts, {1: healthy_state()}, {8: 1.8, 9: 1.0}, config)
    assert client.updates == []

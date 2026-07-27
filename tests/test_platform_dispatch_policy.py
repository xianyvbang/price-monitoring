import asyncio
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
    resolve_platform_dispatch_cost_profiles,
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
    assert validate_policy_config({"autoScoringEnabled": False})["auto_scoring_enabled"] is False
    assert validate_policy_config({})["oauth_account_threshold"] == 3
    assert validate_policy_config({"oauthAccountThreshold": 4})["oauth_account_threshold"] == 4
    probe_models = validate_policy_config(
        {
            "defaultProbeModel": " default-model ",
            "groupProbeModels": {"4": " group-model ", "5": ""},
            "accountProbeModels": {"2": " custom-model ", "3": ""},
        }
    )
    assert probe_models["default_probe_model"] == "default-model"
    assert probe_models["group_probe_models"] == {"4": "group-model"}
    assert probe_models["account_probe_models"] == {"2": "custom-model"}
    with pytest.raises(ValueError, match="字符串"):
        validate_policy_config({"default_probe_model": 123})
    with pytest.raises(ValueError, match="正整数"):
        validate_policy_config({"account_probe_models": {"bad": "model"}})
    with pytest.raises(ValueError, match="分组 ID"):
        validate_policy_config({"group_probe_models": {"bad": "model"}})
    with pytest.raises(ValueError, match="200"):
        validate_policy_config({"account_probe_models": {"1": "x" * 201}})
    with pytest.raises(ValueError, match="自动评分"):
        validate_policy_config({"enabled": True, "auto_scoring_enabled": False})
    with pytest.raises(ValueError, match="大于 0"):
        validate_policy_config({"oauth_account_threshold": 0})
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


def test_recent_probe_records_are_grouped_and_limited_per_account(tmp_path):
    db = make_db(tmp_path)
    site = "https://sub.example"
    for account_id in (1, 2):
        for index in range(20):
            db.add_platform_dispatch_evidence(
                site,
                {
                    "account_id": account_id,
                    "source_kind": "probe",
                    "source_id": f"{account_id}-{index}",
                    "category": "healthy",
                    "score": 100,
                    "is_probe_success": True,
                    "occurred_at": f"2026-07-26T08:{index:02d}:00+00:00",
                },
            )
    usage_start = datetime(2026, 7, 26, 9, tzinfo=timezone.utc)
    for index in range(65):
        db.add_platform_dispatch_evidence(
            site,
            {
                "account_id": 1,
                "source_kind": "usage",
                "source_id": f"usage-{index}",
                "category": "healthy",
                "score": 100,
                "occurred_at": (usage_start + timedelta(seconds=index)).isoformat(),
            },
        )

    grouped = db.list_recent_platform_dispatch_probes(site, 15)

    assert set(grouped) == {1, 2}
    assert len(grouped[1]) == 15
    assert len(grouped[2]) == 15
    assert grouped[1][0]["source_id"] == "1-19"
    assert grouped[1][-1]["source_id"] == "1-5"
    assert len(db.list_platform_dispatch_evidence(site, 1)) == 60


def test_short_evidence_filters_usage_and_errors_after_ranking_latest_ten(tmp_path):
    db = make_db(tmp_path)
    site = "https://sub.example"
    started_at = datetime(2026, 7, 26, 8, tzinfo=timezone.utc)

    account_one = [
        ("error", "older-error"),
        ("usage", "included-usage"),
        ("error", "included-error"),
        *(("probe", f"probe-{index}") for index in range(8)),
    ]
    for index, (source_kind, source_id) in enumerate(account_one):
        db.add_platform_dispatch_evidence(
            site,
            {
                "account_id": 1,
                "source_kind": source_kind,
                "source_id": source_id,
                "category": "healthy" if source_kind != "error" else "upstream_error",
                "score": 100 if source_kind != "error" else 40,
                "occurred_at": (started_at + timedelta(minutes=index)).isoformat(),
            },
        )

    db.add_platform_dispatch_evidence(
        site,
        {
            "account_id": 2,
            "source_kind": "usage",
            "source_id": "older-usage",
            "category": "healthy",
            "score": 100,
            "occurred_at": started_at.isoformat(),
        },
    )
    for index in range(10):
        db.add_platform_dispatch_evidence(
            site,
            {
                "account_id": 2,
                "source_kind": "probe",
                "source_id": f"all-probe-{index}",
                "category": "healthy",
                "score": 100,
                "occurred_at": (started_at + timedelta(minutes=index + 1)).isoformat(),
            },
        )

    for index, source_kind in enumerate(("usage", "error")):
        db.add_platform_dispatch_evidence(
            site,
            {
                "account_id": 3,
                "source_kind": source_kind,
                "source_id": f"few-{source_kind}",
                "category": "healthy" if source_kind == "usage" else "upstream_error",
                "score": 100 if source_kind == "usage" else 40,
                "occurred_at": (started_at + timedelta(minutes=index)).isoformat(),
            },
        )

    grouped = db.list_short_platform_dispatch_evidence(site)

    assert [item["source_id"] for item in grouped[1]] == ["included-error", "included-usage"]
    assert "older-error" not in {item["source_id"] for item in grouped[1]}
    assert 2 not in grouped
    assert [item["source_id"] for item in grouped[3]] == ["few-error", "few-usage"]


class PolicyClient:
    site_url = "https://sub.example"

    def __init__(self, accounts, groups=None, oauth_accounts=None):
        self.accounts = accounts
        self.groups = groups or []
        self.oauth_accounts = oauth_accounts
        self.account_read_filters = []
        self.probes = []
        self.probe_models = []
        self.updates = []
        self.field_payloads = []
        self.realtime_reads = 0

    async def list_accounts(self, **kwargs):
        self.account_read_filters.append(dict(kwargs))
        if str(kwargs.get("account_type") or "").casefold() == "oauth":
            source = self.accounts if self.oauth_accounts is None else self.oauth_accounts
            return [
                dict(account)
                for account in source
                if str(account.get("type") or "").casefold() == "oauth"
            ]
        return [dict(account) for account in self.accounts]

    async def list_groups(self, **kwargs):
        return [dict(group) for group in self.groups]

    async def list_recent_usage(self, account_id, limit):
        return []

    async def list_recent_errors(self, account_id, limit):
        return []

    async def probe_account(self, account_id, model=None):
        self.probes.append(account_id)
        self.probe_models.append((account_id, model))
        return {"success": True, "message": "ok"}

    async def get_concurrency_stats(self, platform=None):
        self.realtime_reads += 1
        return {"account": {}}

    async def get_account_availability(self, platform=None):
        self.realtime_reads += 1
        return {"account": {}}

    async def update_account_schedulable(self, account_id, schedulable):
        self.updates.append((account_id, "schedulable", schedulable))
        account = next(item for item in self.accounts if item["id"] == account_id)
        account["schedulable"] = schedulable
        return dict(account)

    async def update_account_fields(self, account_id, fields):
        self.field_payloads.append((account_id, dict(fields)))
        self.updates.append((account_id, next(iter(fields)), next(iter(fields.values()))))
        account = next(item for item in self.accounts if item["id"] == account_id)
        account.update(fields)
        return dict(account)


def prepare_cache(db, accounts, groups=None):
    db.replace_platform_dispatch_cache(
        "https://sub.example", accounts, groups or [], [], {"platform": "", "type": "", "status": ""}
    )


@pytest.mark.asyncio
async def test_scoring_only_probes_without_remote_policy_writes(tmp_path, monkeypatch):
    db = make_db(tmp_path)
    accounts = [{"id": 1, "name": "one", "status": "inactive", "concurrency": 10}]
    prepare_cache(db, accounts)
    client = PolicyClient(accounts)
    scheduler = PlatformDispatchPolicyScheduler(db, lambda: client)
    progress_updates = []
    update_progress = db.update_platform_dispatch_policy_progress

    def capture_progress(summary):
        progress_updates.append(dict(summary))
        update_progress(summary)

    monkeypatch.setattr(db, "update_platform_dispatch_policy_progress", capture_progress)

    await scheduler.run_once()

    assert client.probes == [1]
    assert client.updates == []
    assert client.realtime_reads == 0
    evidence = db.list_platform_dispatch_evidence("https://sub.example", 1)
    assert evidence[0]["source_kind"] == "probe"
    assert evidence[0]["score"] == 100
    phases = [item["phase"] for item in progress_updates]
    assert {"loading", "evidence", "probe", "scoring", "finalizing"}.issubset(phases)
    assert [item["percent"] for item in progress_updates] == sorted(
        item["percent"] for item in progress_updates
    )
    runtime_summary = db.get_platform_dispatch_policy(POLICY_DEFAULTS)["runtime"]["summary"]
    assert runtime_summary["phase"] == "completed"
    assert runtime_summary["percent"] == 100


@pytest.mark.asyncio
async def test_recent_usage_does_not_suppress_due_probe(tmp_path):
    db = make_db(tmp_path)
    accounts = [{"id": 1, "name": "one", "status": "active", "concurrency": 10}]
    prepare_cache(db, accounts)
    db.add_platform_dispatch_evidence(
        "https://sub.example",
        {
            "account_id": 1,
            "source_kind": "usage",
            "source_id": "recent-usage",
            "category": "healthy",
            "score": 100,
            "occurred_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    client = PolicyClient(accounts)
    scheduler = PlatformDispatchPolicyScheduler(db, lambda: client)

    await scheduler.run_once(automatic=True)
    await scheduler.run_once(automatic=True)

    assert client.probes == [1]


@pytest.mark.asyncio
async def test_automatic_scoring_can_be_disabled_without_blocking_manual_probe(tmp_path):
    db = make_db(tmp_path)
    accounts = [{"id": 1, "name": "one", "status": "inactive", "concurrency": 10}]
    prepare_cache(db, accounts)
    db.save_platform_dispatch_policy(
        {**POLICY_DEFAULTS, "enabled": False, "auto_scoring_enabled": False},
        "https://sub.example",
    )
    client = PolicyClient(accounts)
    factory_calls = 0

    def client_factory():
        nonlocal factory_calls
        factory_calls += 1
        return client

    scheduler = PlatformDispatchPolicyScheduler(db, client_factory)

    skipped = await scheduler.run_once(automatic=True)
    manual = await scheduler.run_once()

    assert skipped == {"skipped": True, "message": "自动评分已关闭"}
    assert factory_calls == 1
    assert manual["managed_accounts"] == 1
    assert client.probes == [1]
    assert client.updates == []


@pytest.mark.asyncio
async def test_disabling_automatic_scoring_cancels_active_round_and_keeps_scheduler_alive(tmp_path):
    db = make_db(tmp_path)
    accounts = [{"id": 1, "name": "one", "status": "inactive", "concurrency": 10}]
    prepare_cache(db, accounts)
    db.save_platform_dispatch_policy(
        {**POLICY_DEFAULTS, "enabled": False, "auto_scoring_enabled": True},
        "https://sub.example",
    )

    class BlockingPolicyClient(PolicyClient):
        def __init__(self, client_accounts):
            super().__init__(client_accounts)
            self.first_read_started = asyncio.Event()
            self.first_read_cancelled = asyncio.Event()
            self.account_reads = 0

        async def list_accounts(self, **kwargs):
            self.account_reads += 1
            if self.account_reads == 1:
                self.first_read_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.first_read_cancelled.set()
                    raise
            return await super().list_accounts(**kwargs)

    client = BlockingPolicyClient(accounts)
    scheduler = PlatformDispatchPolicyScheduler(db, lambda: client)
    scheduler.start()
    try:
        await asyncio.wait_for(client.first_read_started.wait(), timeout=1)
        automatic_run_task = scheduler._automatic_run_task
        assert automatic_run_task is not None
        running = db.get_platform_dispatch_policy(POLICY_DEFAULTS)["runtime"]
        assert running["status"] == "running"
        assert running["summary"]["phase"] == "loading"
        assert running["summary"]["percent"] == 2

        db.save_platform_dispatch_policy(
            {**POLICY_DEFAULTS, "enabled": False, "auto_scoring_enabled": False},
            "https://sub.example",
        )
        scheduler.notify_changed()

        with pytest.raises(asyncio.CancelledError):
            await automatic_run_task
        await asyncio.wait_for(client.first_read_cancelled.wait(), timeout=1)

        policy = db.get_platform_dispatch_policy(POLICY_DEFAULTS)
        assert scheduler.lock.locked() is False
        assert scheduler._task is not None and not scheduler._task.done()
        assert policy["runtime"]["status"] == "idle"
        assert policy["runtime"]["summary"] == {"message": "自动评分已关闭"}
        assert db.list_platform_dispatch_evidence("https://sub.example", 1) == []
        assert db.list_platform_dispatch_account_states("https://sub.example") == []

        manual = await scheduler.run_once()
        assert manual["managed_accounts"] == 1
        assert client.account_reads == 4
    finally:
        await scheduler.stop()


@pytest.mark.asyncio
async def test_stopping_current_automatic_round_keeps_configuration_and_scheduler_alive(tmp_path):
    db = make_db(tmp_path)
    accounts = [{"id": 1, "name": "one", "status": "active", "concurrency": 10}]
    prepare_cache(db, accounts)
    config = {
        **POLICY_DEFAULTS,
        "enabled": True,
        "auto_scoring_enabled": True,
        "probe_interval_seconds": 5,
    }
    db.save_platform_dispatch_policy(config, "https://sub.example")

    class BlockingPolicyClient(PolicyClient):
        def __init__(self, client_accounts):
            super().__init__(client_accounts)
            self.first_read_started = asyncio.Event()
            self.first_read_cancelled = asyncio.Event()
            self.next_read_started = asyncio.Event()
            self.account_reads = 0

        async def list_accounts(self, **kwargs):
            self.account_reads += 1
            if self.account_reads == 1:
                self.first_read_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.first_read_cancelled.set()
                    raise
            if self.account_reads >= 3:
                self.next_read_started.set()
            return await super().list_accounts(**kwargs)

    client = BlockingPolicyClient(accounts)
    scheduler = PlatformDispatchPolicyScheduler(db, lambda: client)
    scheduler.start()
    try:
        await asyncio.wait_for(client.first_read_started.wait(), timeout=1)
        assert scheduler.automatic_round_running is True

        stopped = await scheduler.stop_automatic_round()

        assert stopped is True
        await asyncio.wait_for(client.first_read_cancelled.wait(), timeout=1)
        assert scheduler.automatic_round_running is False
        policy = db.get_platform_dispatch_policy(POLICY_DEFAULTS)
        assert policy["config"]["enabled"] is True
        assert policy["config"]["auto_scoring_enabled"] is True
        assert policy["runtime"]["status"] == "idle"
        assert policy["runtime"]["summary"] == {
            "phase": "stopped",
            "message": "本轮自动调度已停止",
        }
        assert scheduler._task is not None and not scheduler._task.done()
        assert scheduler._run_after_change is False

        await asyncio.wait_for(client.next_read_started.wait(), timeout=7)
        assert client.account_reads >= 3
    finally:
        await scheduler.stop()


@pytest.mark.asyncio
async def test_policy_change_can_rearm_timer_without_starting_an_automatic_round(tmp_path, monkeypatch):
    db = make_db(tmp_path)
    scheduler = PlatformDispatchPolicyScheduler(db, lambda: None)
    calls = 0
    first_run = asyncio.Event()
    immediate_run = asyncio.Event()

    async def fake_run_once(*, automatic=False):
        nonlocal calls
        assert automatic is True
        calls += 1
        if calls == 1:
            first_run.set()
        elif calls == 2:
            immediate_run.set()
        return {}

    monkeypatch.setattr(scheduler, "run_once", fake_run_once)
    scheduler.start()
    try:
        await asyncio.wait_for(first_run.wait(), timeout=1)

        scheduler.notify_changed(run_immediately=False)
        await asyncio.sleep(0.05)
        assert calls == 1

        scheduler.notify_changed()
        await asyncio.wait_for(immediate_run.wait(), timeout=1)
        assert calls == 2
    finally:
        await scheduler.stop()


@pytest.mark.asyncio
async def test_oauth_group_statistics_count_only_normal_schedulable_accounts(tmp_path):
    db = make_db(tmp_path)
    accounts = [
        {
            "id": 1,
            "name": "managed-api-key",
            "platform": "openai",
            "type": "apikey",
            "status": "active",
            "schedulable": True,
            "group_ids": [10],
        }
    ]
    groups = [
        {"id": 10, "name": "Ten", "platform": "openai"},
        {"id": 20, "name": "Twenty", "platform": "openai"},
        {"id": 30, "name": "Thirty", "platform": "openai"},
    ]
    oauth_accounts = [
        {"id": 101, "type": "oauth", "status": "active", "schedulable": True, "group_ids": [10, 20]},
        {"id": 101, "type": "oauth", "status": "active", "schedulable": True, "group_ids": [10, 20]},
        {"id": 102, "type": "oauth", "status": "active", "schedulable": False, "group_ids": [10]},
        {"id": 103, "type": "oauth", "status": "inactive", "schedulable": True, "group_ids": [10]},
        {"id": 104, "type": "oauth", "status": "active", "rate_limit_reset_at": "2999-01-01T00:00:00Z", "group_ids": [10]},
        {"id": 105, "type": "oauth", "status": "active", "overload_until": "2999-01-01T00:00:00Z", "group_ids": [10]},
        {"id": 106, "type": "oauth", "status": "active", "temp_unschedulable_until": "2999-01-01T00:00:00Z", "group_ids": [10]},
        {"id": 107, "type": "oauth", "status": "active", "group_ids": [20]},
        {"id": 108, "type": "oauth", "status": "active", "group_ids": [999]},
    ]
    prepare_cache(db, accounts, groups)
    client = PolicyClient(accounts, groups, oauth_accounts)
    scheduler = PlatformDispatchPolicyScheduler(db, lambda: client)

    summary = await scheduler.run_once()

    statistics = {item["group_id"]: item for item in summary["oauth_group_statistics"]}
    assert statistics[10] == {
        "group_id": 10,
        "group_name": "Ten",
        "normal_oauth_accounts": 1,
        "oauth_account_threshold": 3,
        "threshold_exceeded": False,
        "affected_apikey_accounts": 0,
    }
    assert statistics[20]["normal_oauth_accounts"] == 2
    assert statistics[30]["normal_oauth_accounts"] == 0
    assert [read.get("account_type") for read in client.account_read_filters] == [None, "oauth"]
    assert client.updates == []


@pytest.mark.asyncio
async def test_oauth_threshold_requires_all_groups_and_switches_one_apikey_per_round(tmp_path):
    db = make_db(tmp_path)
    accounts = [
        {"id": 1, "name": "single", "type": "apikey", "status": "active", "schedulable": True, "group_ids": [10]},
        {"id": 2, "name": "multi", "type": "apikey", "status": "active", "schedulable": True, "group_ids": [10, 20]},
        {"id": 3, "name": "unknown", "type": "apikey", "status": "active", "schedulable": True, "group_ids": [999]},
        {"id": 4, "name": "ungrouped", "type": "apikey", "status": "active", "schedulable": True, "group_ids": []},
    ]
    groups = [{"id": 10, "name": "Ten"}, {"id": 20, "name": "Twenty"}]
    oauth_accounts = [
        *[
            {"id": 100 + index, "type": "oauth", "status": "active", "group_ids": [10]}
            for index in range(4)
        ],
        *[
            {"id": 200 + index, "type": "oauth", "status": "active", "group_ids": [20]}
            for index in range(3)
        ],
    ]
    prepare_cache(db, accounts, groups)
    db.save_platform_dispatch_policy({**POLICY_DEFAULTS, "enabled": True}, "https://sub.example")
    client = PolicyClient(accounts, groups, oauth_accounts)
    scheduler = PlatformDispatchPolicyScheduler(db, lambda: client)

    first = await scheduler.run_once()

    first_statistics = {item["group_id"]: item for item in first["oauth_group_statistics"]}
    assert client.updates == [(1, "schedulable", False)]
    assert first_statistics[10]["threshold_exceeded"] is True
    assert first_statistics[10]["affected_apikey_accounts"] == 1
    assert first_statistics[20]["threshold_exceeded"] is False
    assert first_statistics[20]["affected_apikey_accounts"] == 0

    oauth_accounts.append(
        {"id": 203, "type": "oauth", "status": "active", "group_ids": [20]}
    )
    second = await scheduler.run_once()

    second_statistics = {item["group_id"]: item for item in second["oauth_group_statistics"]}
    assert client.updates == [
        (1, "schedulable", False),
        (2, "schedulable", False),
    ]
    assert second_statistics[10]["affected_apikey_accounts"] == 2
    assert second_statistics[20]["affected_apikey_accounts"] == 1


@pytest.mark.asyncio
async def test_oauth_policy_prefers_lowest_health_and_blocks_recovery_until_threshold_clears(tmp_path):
    db = make_db(tmp_path)
    accounts = {
        1: {"id": 1, "name": "healthier", "type": "apikey", "status": "active", "schedulable": True, "group_ids": [10]},
        2: {"id": 2, "name": "lower", "type": "apikey", "status": "active", "schedulable": True, "group_ids": [10]},
    }
    client = PolicyClient(list(accounts.values()), [{"id": 10, "name": "Ten"}])
    scheduler = PlatformDispatchPolicyScheduler(db, lambda: client)
    health = {1: healthy_state(90), 2: healthy_state(70)}

    action = await scheduler._apply_schedulable_policy(
        client,
        client.site_url,
        accounts,
        health,
        [1, 2],
        {**POLICY_DEFAULTS, "enabled": True},
        180,
        {10: {"id": 10, "name": "Ten"}},
        oauth_affected_ids={1, 2},
        oauth_group_counts={10: 4},
    )

    assert client.updates == [(2, "schedulable", False)]
    assert "Ten 正常 OAuth 4 个" in action
    assert "严格超过阈值 3" in action
    assert "APIKey 调度" in db.list_platform_dispatch_actions(1, 10)["items"][0]["reason"]

    accounts[1]["schedulable"] = False
    client.accounts[0]["schedulable"] = False
    client.updates.clear()
    probe_at = datetime.now(timezone.utc).isoformat()
    health[1]["latest_probe_success_at"] = probe_at
    blocked = await scheduler._apply_schedulable_policy(
        client,
        client.site_url,
        {1: accounts[1]},
        {1: health[1]},
        [],
        {**POLICY_DEFAULTS, "enabled": True},
        180,
        {10: {"id": 10, "name": "Ten"}},
        oauth_affected_ids={1},
        oauth_group_counts={10: 4},
    )
    assert blocked == ""
    assert client.updates == []

    recovered = await scheduler._apply_schedulable_policy(
        client,
        client.site_url,
        {1: accounts[1]},
        {1: health[1]},
        [],
        {**POLICY_DEFAULTS, "enabled": True},
        180,
        {10: {"id": 10, "name": "Ten"}},
        oauth_affected_ids=set(),
        oauth_group_counts={10: 3},
    )
    assert client.updates == [(1, "schedulable", True)]
    assert recovered.startswith("开启调度 healthier")


@pytest.mark.asyncio
async def test_oauth_read_failure_stops_round_before_probes_or_remote_writes(tmp_path):
    db = make_db(tmp_path)
    accounts = [
        {"id": 1, "name": "one", "type": "apikey", "status": "active", "group_ids": [10]}
    ]
    groups = [{"id": 10, "name": "Ten"}]
    prepare_cache(db, accounts, groups)
    db.save_platform_dispatch_policy({**POLICY_DEFAULTS, "enabled": True}, "https://sub.example")

    class FailingOAuthClient(PolicyClient):
        async def list_accounts(self, **kwargs):
            if str(kwargs.get("account_type") or "").casefold() == "oauth":
                raise RuntimeError("oauth read failed")
            return await super().list_accounts(**kwargs)

    client = FailingOAuthClient(accounts, groups)
    scheduler = PlatformDispatchPolicyScheduler(db, lambda: client)

    with pytest.raises(RuntimeError, match="oauth read failed"):
        await scheduler.run_once()

    assert client.probes == []
    assert client.updates == []
    runtime = db.get_platform_dispatch_policy(POLICY_DEFAULTS)["runtime"]
    assert runtime["status"] == "failed"
    assert runtime["last_error"] == "oauth read failed"


@pytest.mark.asyncio
async def test_minimum_pool_recovers_manually_unschedulable_account_with_all_subpolicies_off(tmp_path):
    db = make_db(tmp_path)
    accounts = [
        {
            "id": 1,
            "name": "group-ten",
            "status": "active",
            "schedulable": True,
            "concurrency": 20,
            "group_ids": [10],
        },
        {
            "id": 2,
            "name": "group-twenty",
            "status": "active",
            "schedulable": False,
            "concurrency": 20,
            "group_ids": [20],
        },
    ]
    prepare_cache(db, accounts)
    db.save_platform_dispatch_policy({**POLICY_DEFAULTS, "enabled": True}, "https://sub.example")
    client = PolicyClient(accounts)
    scheduler = PlatformDispatchPolicyScheduler(db, lambda: client)

    summary = await scheduler.run_once()

    assert client.probes == [1, 2]
    assert client.updates == [(2, "schedulable", True)]
    assert summary["scheduling_action"].startswith("开启调度 group-twenty")
    assert "分组 20 低于每组最低保障" in summary["scheduling_action"]
    assert summary["status_action"] == summary["scheduling_action"]
    assert summary["available_accounts"] == 1
    assert summary["group_availability"] == [
        {
            "pool_key": "group-10",
            "group_id": 10,
            "group_name": "分组 10",
            "managed_accounts": 1,
            "available_accounts": 1,
            "minimum_target": 1,
            "healthy_target": 3,
        },
        {
            "pool_key": "group-20",
            "group_id": 20,
            "group_name": "分组 20",
            "managed_accounts": 1,
            "available_accounts": 0,
            "minimum_target": 1,
            "healthy_target": 3,
        },
    ]


@pytest.mark.asyncio
async def test_health_return_target_is_applied_per_group(tmp_path):
    db = make_db(tmp_path)
    accounts = [
        {"id": 1, "name": "ten-a", "status": "active", "schedulable": True, "group_ids": [10]},
        {"id": 2, "name": "ten-b", "status": "active", "schedulable": True, "group_ids": [10]},
        {"id": 3, "name": "twenty-a", "status": "active", "schedulable": True, "group_ids": [20]},
        {"id": 4, "name": "twenty-b", "status": "active", "schedulable": False, "group_ids": [20]},
    ]
    prepare_cache(db, accounts)
    db.save_platform_dispatch_policy(
        {
            **POLICY_DEFAULTS,
            "enabled": True,
            "return_pool_enabled": True,
            "healthy_target_accounts": 2,
        },
        "https://sub.example",
    )
    client = PolicyClient(accounts)
    scheduler = PlatformDispatchPolicyScheduler(db, lambda: client)

    summary = await scheduler.run_once()

    assert summary["available_accounts"] == 3
    assert client.updates == [(4, "schedulable", True)]
    assert "分组 20 低于每组健康回池目标" in summary["scheduling_action"]


@pytest.mark.asyncio
async def test_recovery_prefers_account_covering_more_deficient_groups(tmp_path):
    db = make_db(tmp_path)
    accounts = {
        1: {
            "id": 1,
            "name": "multi-group",
            "status": "active",
            "schedulable": False,
            "group_ids": [10, 20],
        },
        2: {
            "id": 2,
            "name": "single-group",
            "status": "active",
            "schedulable": False,
            "group_ids": [10],
        },
    }
    client = PolicyClient(list(accounts.values()))
    scheduler = PlatformDispatchPolicyScheduler(db, lambda: client)
    probe_at = datetime.now(timezone.utc).isoformat()
    health = {1: healthy_state(80), 2: healthy_state(90)}
    health[1]["latest_probe_success_at"] = probe_at
    health[2]["latest_probe_success_at"] = probe_at

    action = await scheduler._apply_schedulable_policy(
        client,
        client.site_url,
        accounts,
        health,
        [],
        {**POLICY_DEFAULTS, "enabled": True},
        180,
    )

    assert client.updates == [(1, "schedulable", True)]
    assert "分组 10、分组 20 低于每组最低保障" in action


@pytest.mark.asyncio
async def test_minimum_pool_does_not_reactivate_inactive_account(tmp_path):
    db = make_db(tmp_path)
    accounts = [{"id": 1, "name": "one", "status": "inactive", "schedulable": False, "concurrency": 20}]
    prepare_cache(db, accounts)
    db.save_platform_dispatch_policy({**POLICY_DEFAULTS, "enabled": True}, "https://sub.example")
    client = PolicyClient(accounts)
    scheduler = PlatformDispatchPolicyScheduler(db, lambda: client)

    summary = await scheduler.run_once()

    assert client.updates == []
    assert summary["scheduling_action"] == ""


@pytest.mark.asyncio
async def test_probe_model_prefers_account_then_group_then_default(tmp_path):
    db = make_db(tmp_path)
    accounts = [
        {"id": 1, "name": "default", "status": "active", "concurrency": 20, "group_ids": [30]},
        {"id": 2, "name": "account", "status": "active", "concurrency": 20, "group_ids": [10]},
        {"id": 3, "name": "group", "status": "active", "concurrency": 20, "group_ids": [20]},
        {"id": 4, "name": "multi-group", "status": "active", "concurrency": 20, "group_ids": [20, 10]},
    ]
    groups = [
        {"id": 10, "name": "OpenAI 10", "platform": "openai"},
        {"id": 20, "name": "Anthropic 20", "platform": "anthropic"},
        {"id": 30, "name": "OpenAI 30", "platform": "openai"},
    ]
    prepare_cache(db, accounts, groups)
    db.save_platform_dispatch_policy(
        {
            **POLICY_DEFAULTS,
            "enabled": True,
            "default_probe_model": "default-model",
            "group_probe_models": {"10": "group-ten", "20": "group-twenty"},
            "account_probe_models": {"2": "custom-model"},
        },
        "https://sub.example",
    )
    client = PolicyClient(accounts, groups)
    scheduler = PlatformDispatchPolicyScheduler(db, lambda: client)

    await scheduler.run_once()

    assert client.probe_models == [
        (1, "default-model"),
        (2, "custom-model"),
        (3, "group-twenty"),
        (4, "group-ten"),
    ]


@pytest.mark.asyncio
async def test_probe_model_uses_sub2api_default_when_no_override_is_configured(tmp_path):
    db = make_db(tmp_path)
    accounts = [{"id": 1, "name": "default", "status": "active", "group_ids": [10]}]
    prepare_cache(db, accounts)
    client = PolicyClient(accounts)
    scheduler = PlatformDispatchPolicyScheduler(db, lambda: client)

    await scheduler.run_once()

    assert client.probe_models == [(1, None)]


@pytest.mark.asyncio
async def test_probe_model_uses_group_platform_default(tmp_path):
    db = make_db(tmp_path)
    accounts = [
        {"id": 1, "name": "openai", "status": "active", "group_ids": [10]},
        {"id": 2, "name": "anthropic", "status": "active", "group_ids": [20]},
        {"id": 3, "name": "unknown", "status": "active", "group_ids": [30]},
        {"id": 4, "name": "ungrouped", "status": "active", "group_ids": []},
    ]
    groups = [
        {"id": 10, "name": "OpenAI", "platform": "openai"},
        {"id": 20, "name": "Anthropic", "platform": "anthropic"},
        {"id": 30, "name": "Gemini", "platform": "gemini"},
    ]
    prepare_cache(db, accounts, groups)
    client = PolicyClient(accounts, groups)
    scheduler = PlatformDispatchPolicyScheduler(db, lambda: client)

    await scheduler.run_once()
    await scheduler.refresh_health_evidence(client, db.get_platform_dispatch_cache())

    assert client.probe_models == [
        (1, "gpt-5.5"),
        (2, "claude-sonnet-4-6"),
        (3, None),
        (4, None),
        (1, "gpt-5.5"),
        (2, "claude-sonnet-4-6"),
        (3, None),
        (4, None),
    ]


@pytest.mark.asyncio
async def test_manual_refresh_and_automatic_scoring_share_evidence_collection(tmp_path):
    db = make_db(tmp_path)
    accounts = [{"id": 1, "name": "one", "status": "active", "group_ids": [10]}]
    prepare_cache(db, accounts)
    db.save_platform_dispatch_policy(
        {**POLICY_DEFAULTS, "group_probe_models": {"10": "group-model"}},
        "https://sub.example",
    )
    client = PolicyClient(accounts)

    class TrackingScheduler(PlatformDispatchPolicyScheduler):
        def __init__(self):
            super().__init__(db, lambda: client)
            self.collection_modes = []

        async def _collect_health_evidence(self, *args, **kwargs):
            self.collection_modes.append(
                (bool(kwargs.get("force_full")), bool(kwargs.get("force_probe")))
            )
            return await super()._collect_health_evidence(*args, **kwargs)

    scheduler = TrackingScheduler()

    await scheduler.run_once(automatic=True)
    await scheduler.refresh_health_evidence(client, db.get_platform_dispatch_cache())

    assert scheduler.collection_modes == [(False, False), (True, True)]
    assert client.probe_models == [(1, "group-model"), (1, "group-model")]


@pytest.mark.asyncio
async def test_schedulable_policy_switches_only_one_account_per_round(tmp_path):
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

    await scheduler._apply_schedulable_policy(
        client, client.site_url, accounts, health, [1, 2], {**POLICY_DEFAULTS, "enabled": True}, 180
    )

    assert len(client.updates) == 1
    assert client.updates[0][1:] == ("schedulable", False)


@pytest.mark.asyncio
async def test_smart_expansion_floors_low_and_preserves_above_max(tmp_path):
    db = make_db(tmp_path)
    accounts = {
        1: {"id": 1, "name": "low", "status": "active", "concurrency": 10},
        2: {"id": 2, "name": "high", "status": "active", "concurrency": 300},
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
        {
            1: {"cost_available": True, "upstream_cost_multiplier": 1},
            2: {"cost_available": True, "upstream_cost_multiplier": 1},
        },
        {**POLICY_DEFAULTS, "smart_expand_enabled": True},
    )

    assert client.updates == [(1, "concurrency", 52)]
    assert accounts[2]["concurrency"] == 300


@pytest.mark.asyncio
async def test_oauth_affected_accounts_are_excluded_from_capacity_and_load_writes(tmp_path):
    db = make_db(tmp_path)
    accounts = {
        1: {
            "id": 1,
            "name": "oauth-covered",
            "status": "active",
            "schedulable": True,
            "concurrency": 10,
            "load_factor": 20,
        },
        2: {
            "id": 2,
            "name": "eligible",
            "status": "active",
            "schedulable": True,
            "concurrency": 10,
            "load_factor": 20,
        },
    }
    client = PolicyClient(list(accounts.values()))
    scheduler = PlatformDispatchPolicyScheduler(db, lambda: client)
    health = {1: healthy_state(), 2: healthy_state()}
    cost_profiles = {
        account_id: {"cost_available": True, "upstream_cost_multiplier": 1.0}
        for account_id in accounts
    }

    await scheduler._apply_concurrency_policy(
        client,
        client.site_url,
        accounts,
        health,
        {1: {"current_in_use": 10}, 2: {"current_in_use": 10}},
        cost_profiles,
        {**POLICY_DEFAULTS, "smart_expand_enabled": True},
        excluded_account_ids={1},
    )
    assert client.field_payloads
    assert {account_id for account_id, _ in client.field_payloads} == {2}

    client.field_payloads.clear()
    client.updates.clear()
    await scheduler._apply_load_policy(
        client,
        client.site_url,
        accounts,
        health,
        cost_profiles,
        {**POLICY_DEFAULTS, "load_factor_enabled": True},
        excluded_account_ids={1},
    )
    assert client.field_payloads
    assert {account_id for account_id, _ in client.field_payloads} == {2}


@pytest.mark.asyncio
async def test_load_factor_deadband_and_cooldown_use_upstream_cost(tmp_path):
    db = make_db(tmp_path)
    account = {
        "id": 1,
        "name": "one",
        "status": "active",
        "concurrency": 100,
        "load_factor": 100,
        "group_ids": [8, 9],
    }
    accounts = {1: account}
    client = PolicyClient([account])
    scheduler = PlatformDispatchPolicyScheduler(db, lambda: client)
    config = {
        **POLICY_DEFAULTS,
        "load_factor_enabled": True,
        "load_factor_total": 150,
        "load_change_threshold_percent": 10,
    }

    cost_profiles = {1: {"cost_available": True, "upstream_cost_multiplier": 2}}
    await scheduler._apply_load_policy(client, client.site_url, accounts, {1: healthy_state()}, cost_profiles, config)
    assert client.updates == [(1, "load_factor", 150)]
    state = db.get_platform_dispatch_account_state(client.site_url, 1)
    assert state["target_load_factor"] == 150

    client.updates.clear()
    account["load_factor"] = 143
    await scheduler._apply_load_policy(client, client.site_url, accounts, {1: healthy_state()}, cost_profiles, config)
    assert client.updates == []


@pytest.mark.asyncio
async def test_load_factor_adjustment_also_updates_priority_order(tmp_path):
    db = make_db(tmp_path)
    accounts = {
        1: {
            "id": 1,
            "name": "lower-cost",
            "status": "active",
            "concurrency": 20,
            "load_factor": 20,
            "priority": 50,
            "rate_multiplier": 999,
        },
        2: {
            "id": 2,
            "name": "higher-cost",
            "status": "active",
            "concurrency": 20,
            "load_factor": 20,
            "priority": 50,
            "rate_multiplier": 0.01,
        },
    }
    client = PolicyClient(list(accounts.values()))
    scheduler = PlatformDispatchPolicyScheduler(db, lambda: client)
    config = {
        **POLICY_DEFAULTS,
        "load_factor_enabled": True,
        "load_factor_total": 120,
        "account_min_load_factor": 20,
        "load_change_threshold_percent": 0,
    }

    await scheduler._apply_load_policy(
        client,
        client.site_url,
        accounts,
        {1: healthy_state(), 2: healthy_state()},
        {
            1: {"cost_available": True, "upstream_cost_multiplier": 1},
            2: {"cost_available": True, "upstream_cost_multiplier": 2},
        },
        config,
    )

    payloads = dict(client.field_payloads)
    assert set(payloads) == {1, 2}
    assert payloads[1]["load_factor"] > payloads[2]["load_factor"]
    assert payloads[1]["load_factor"] + payloads[2]["load_factor"] == 120
    assert payloads[1]["priority"] == 1
    assert payloads[2]["priority"] == payloads[1]["load_factor"] - payloads[2]["load_factor"] + 1
    assert accounts[1]["priority"] == 1
    assert accounts[2]["priority"] == payloads[2]["priority"]


def test_cost_profiles_use_monitor_group_rate_recharge_ratio_and_expire(tmp_path):
    db = make_db(tmp_path)
    balance_account_id = db.upsert_account(
        {
            "platform": "sub2Api",
            "name": "balance-source",
            "base_url": "https://balance.example",
            "api_key": "sk-test",
            "recharge_paid_amount": 1,
            "recharge_received_amount": 2,
        }
    )
    db.replace_account_monitor_groups(
        balance_account_id,
        [{"group_id": "pro", "plan_name": "Pro", "effective_rate_multiplier": 2}],
    )
    monitor_group_id = int(db.list_monitor_groups(balance_account_id)[0]["id"])
    now = datetime(2026, 7, 26, 8, tzinfo=timezone.utc)
    db.update_monitor_group_snapshot(
        monitor_group_id,
        {"group_id": "pro", "plan_name": "Pro", "effective_rate_multiplier": 2},
        now.isoformat(),
    )
    db.save_platform_dispatch_cost_binding("https://sub.example", 11, monitor_group_id)
    accounts = {11: {"id": 11, "group_ids": [8]}}

    fresh = resolve_platform_dispatch_cost_profiles(
        db, "https://sub.example", accounts, {8: 1.1}, 10, refresh_snapshots=True, now=now
    )[11]

    assert fresh["upstream_group_rate_multiplier"] == 2
    assert fresh["upstream_cost_multiplier"] == 1
    assert fresh["minimum_safe_rate_multiplier"] == pytest.approx(1.1)
    assert fresh["price_protection_status"] == "safe"
    assert fresh["cost_available"] is True

    stale_at = now - timedelta(seconds=2401)
    db.update_monitor_group_snapshot(
        monitor_group_id,
        {"group_id": "pro", "plan_name": "Pro", "effective_rate_multiplier": 2},
        stale_at.isoformat(),
    )
    stale = resolve_platform_dispatch_cost_profiles(
        db, "https://sub.example", accounts, {8: 1.1}, 10, now=now
    )[11]

    assert stale["price_protection_status"] == "rate_expired"
    assert stale["cost_available"] is False
    assert stale["price_unsafe"] is True


def test_cost_bindings_are_site_isolated_and_cascade_with_monitor_group(tmp_path):
    db = make_db(tmp_path)
    balance_account_id = db.upsert_account(
        {
            "platform": "sub2Api",
            "name": "balance-source",
            "base_url": "https://balance.example",
            "api_key": "sk-test",
        }
    )
    db.replace_account_monitor_groups(
        balance_account_id,
        [{"group_id": "pro", "effective_rate_multiplier": 1.5}],
    )
    monitor_group_id = int(db.list_monitor_groups(balance_account_id)[0]["id"])

    db.save_platform_dispatch_cost_binding("https://one.example/", 7, monitor_group_id)
    db.save_platform_dispatch_cost_binding("https://two.example", 7, monitor_group_id)

    assert db.get_platform_dispatch_cost_binding("https://one.example", 7)["monitor_group_id"] == monitor_group_id
    assert db.get_platform_dispatch_cost_binding("https://two.example", 7)["monitor_group_id"] == monitor_group_id
    assert db.delete_platform_dispatch_cost_binding("https://one.example", 7) is True
    assert db.get_platform_dispatch_cost_binding("https://two.example", 7) is not None

    db.replace_account_monitor_groups(balance_account_id, [])
    assert db.get_platform_dispatch_cost_binding("https://two.example", 7) is None


@pytest.mark.asyncio
async def test_price_protection_closes_all_unsafe_accounts_in_one_round(tmp_path):
    db = make_db(tmp_path)
    accounts = {
        1: {"id": 1, "name": "one", "status": "active", "schedulable": True},
        2: {"id": 2, "name": "two", "status": "active", "schedulable": True},
    }
    client = PolicyClient(list(accounts.values()))
    scheduler = PlatformDispatchPolicyScheduler(db, lambda: client)
    profiles = {
        account_id: {
            "price_unsafe": True,
            "price_protection_status": "unsafe",
            "local_min_rate_multiplier": 1,
            "minimum_safe_rate_multiplier": 1.1,
            "upstream_cost_multiplier": 1,
            "cost_binding": {
                "balance_account_name": "source",
                "group_name": "pro",
            },
        }
        for account_id in accounts
    }

    actions, unsafe_ids = await scheduler._apply_price_protection(
        client, client.site_url, accounts, profiles
    )

    assert unsafe_ids == {1, 2}
    assert client.updates == [(1, "schedulable", False), (2, "schedulable", False)]
    assert len(actions) == 2
    assert "成本来源：source / pro" in actions[0]


@pytest.mark.asyncio
async def test_price_safe_recovery_can_override_manual_close_but_only_once(tmp_path):
    db = make_db(tmp_path)
    accounts = {
        1: {"id": 1, "name": "manual", "status": "active", "schedulable": False},
        2: {"id": 2, "name": "system", "status": "active", "schedulable": False},
    }
    db.upsert_platform_dispatch_account_state(
        "https://sub.example", 1, decision_reason="人工关闭调度"
    )
    client = PolicyClient(list(accounts.values()))
    scheduler = PlatformDispatchPolicyScheduler(db, lambda: client)
    profiles = {
        account_id: {
            "price_protection_status": "safe",
            "local_min_rate_multiplier": 1.2,
            "minimum_safe_rate_multiplier": 1.1,
            "upstream_cost_multiplier": 1,
            "cost_binding": {"balance_account_name": "source", "group_name": "pro"},
        }
        for account_id in accounts
    }

    action = await scheduler._apply_schedulable_policy(
        client,
        client.site_url,
        accounts,
        {1: healthy_state(100), 2: healthy_state(90)},
        [],
        {**POLICY_DEFAULTS, "enabled": True, "price_protection_enabled": True},
        180,
        cost_profiles=profiles,
    )

    assert client.updates == [(1, "schedulable", True)]
    assert "成本来源：source / pro" in action
    assert "覆盖人工关闭状态：是" in action


@pytest.mark.asyncio
async def test_dispatch_refreshes_local_rates_and_ignores_excluded_groups(tmp_path):
    db = make_db(tmp_path)
    accounts = [
        {
            "id": 1,
            "name": "mixed-groups",
            "status": "active",
            "schedulable": True,
            "group_ids": [8, 9],
        }
    ]
    prepare_cache(db, accounts)
    db.exclude_platform_dispatch_group("https://sub.example", 8, "excluded-low", "openai")
    balance_account_id = db.upsert_account(
        {
            "platform": "sub2Api",
            "name": "balance-source",
            "base_url": "https://balance.example",
            "api_key": "sk-test",
        }
    )
    db.replace_account_monitor_groups(
        balance_account_id,
        [{"group_id": "pro", "effective_rate_multiplier": 1}],
    )
    monitor_group_id = int(db.list_monitor_groups(balance_account_id)[0]["id"])
    db.update_monitor_group_snapshot(
        monitor_group_id,
        {"group_id": "pro", "effective_rate_multiplier": 1},
        datetime.now(timezone.utc).isoformat(),
    )
    db.save_platform_dispatch_cost_binding("https://sub.example", 1, monitor_group_id)
    db.save_platform_dispatch_policy(
        {**POLICY_DEFAULTS, "enabled": True, "price_protection_enabled": True},
        "https://sub.example",
    )
    client = PolicyClient(accounts)

    async def list_groups(**kwargs):
        return [
            {"id": 8, "name": "excluded-low", "platform": "openai", "rate_multiplier": 0.5},
            {"id": 9, "name": "included-safe", "platform": "openai", "rate_multiplier": 1.2},
        ]

    client.list_groups = list_groups
    scheduler = PlatformDispatchPolicyScheduler(db, lambda: client)

    summary = await scheduler.run_once()
    cache = db.get_platform_dispatch_cache()

    assert not any(update == (1, "schedulable", False) for update in client.updates)
    assert summary["price_unsafe_accounts"] == 0
    assert cache["accounts"][0]["group_ids"] == [9]
    assert [(group["id"], group["rate_multiplier"]) for group in cache["groups"]] == [(9, 1.2)]

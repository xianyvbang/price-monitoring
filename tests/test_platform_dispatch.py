import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import Database
from app.services.sub2api_admin import (
    Sub2ApiAdminClient,
    Sub2ApiAdminError,
    merge_recent_activity,
    public_dispatch_account,
)


class DummyResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload


class DummyAsyncClient:
    requests = []
    responses = []

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def request(self, method, url, headers=None, **kwargs):
        self.requests.append({"method": method, "url": url, "headers": headers or {}, **kwargs})
        return self.responses.pop(0)


def setup_test_db(tmp_path, monkeypatch):
    test_db = Database(str(tmp_path / "app.db"), "test-key")
    test_db.init()
    test_db.ensure_admin("admin", "password123")
    monkeypatch.setattr("app.main.db", test_db)
    monkeypatch.setattr("app.main.scheduler.db", test_db)
    monkeypatch.setattr("app.main.scheduler.start", lambda: None)

    async def stop_scheduler():
        return None

    monkeypatch.setattr("app.main.scheduler.stop", stop_scheduler)
    return test_db


def login(client):
    client.post("/login", data={"username": "admin", "password": "password123"})


def wait_for_dispatch_job(client, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get("/api/platform-dispatch/job")
        assert response.status_code == 200
        job = response.json()["job"]
        if job and job["status"] in {"succeeded", "failed"}:
            return job
        time.sleep(0.01)
    raise AssertionError("平台调度任务未在预期时间内结束")


def test_platform_dispatch_policy_api_defaults_validation_and_save(tmp_path, monkeypatch):
    setup_test_db(tmp_path, monkeypatch)

    with TestClient(app) as client:
        login(client)
        initial = client.get("/api/platform-dispatch/policy")
        assert initial.status_code == 200
        assert initial.json()["config"]["enabled"] is False
        assert initial.json()["config"]["auto_scoring_enabled"] is True
        assert initial.json()["config"]["default_probe_model"] == ""
        assert initial.json()["config"]["group_probe_models"] == {}
        assert initial.json()["config"]["account_probe_models"] == {}
        assert initial.json()["config"]["excluded_account_ids"] == []
        assert initial.json()["config"]["oauth_account_threshold"] == 3
        assert initial.json()["is_running"] is False
        assert initial.json()["runtime"]["is_running"] is False
        assert initial.json()["automatic_running"] is False
        assert initial.json()["runtime"]["automatic_running"] is False

        stopped = client.post("/api/platform-dispatch/policy/stop")
        assert stopped.status_code == 200
        assert stopped.json()["stopped"] is False
        assert stopped.json()["message"] == "当前没有正在执行的自动轮次"

        invalid = client.put(
            "/api/platform-dispatch/policy",
            json={"account_min_concurrency": 300, "account_max_concurrency": 250},
        )
        assert invalid.status_code == 400

        invalid_oauth_threshold = client.put(
            "/api/platform-dispatch/policy",
            json={"oauth_account_threshold": 0},
        )
        assert invalid_oauth_threshold.status_code == 400

        saved = client.put(
            "/api/platform-dispatch/policy",
            json={
                "enabled": True,
                "return_pool_enabled": False,
                "smart_expand_enabled": False,
                "load_factor_enabled": False,
                "price_protection_enabled": False,
                "probe_interval_seconds": 125,
                "oauthAccountThreshold": 4,
                "excluded_account_ids": [7, 8],
            },
        )
        assert saved.status_code == 200
        assert saved.json()["config"]["enabled"] is True
        assert saved.json()["config"]["auto_scoring_enabled"] is True
        assert saved.json()["config"]["probe_interval_seconds"] == 125
        assert saved.json()["config"]["oauth_account_threshold"] == 4
        assert saved.json()["config"]["excluded_account_ids"] == [7, 8]

        incompatible = client.put(
            "/api/platform-dispatch/policy",
            json={"enabled": True, "autoScoringEnabled": False},
        )
        assert incompatible.status_code == 400

        disabled = client.put(
            "/api/platform-dispatch/policy",
            json={"enabled": False, "autoScoringEnabled": False},
        )
        assert disabled.status_code == 200
        assert disabled.json()["config"]["auto_scoring_enabled"] is False

        run = client.post("/api/platform-dispatch/policy/run")
        assert run.status_code == 200
        assert run.json()["summary"]["managed_accounts"] == 0
        persisted = client.get("/api/platform-dispatch/policy")
        assert persisted.status_code == 200
        assert persisted.json()["config"]["enabled"] is False
        assert persisted.json()["config"]["auto_scoring_enabled"] is False

        actions = client.get("/api/platform-dispatch/actions?page=1&page_size=10")
        assert actions.status_code == 200
        assert actions.json()["items"] == []


def test_platform_dispatch_account_exclusion_api_updates_only_policy_and_handles_conflicts(tmp_path, monkeypatch):
    test_db = setup_test_db(tmp_path, monkeypatch)
    test_db.set_setting("sub2api_site_url", "https://sub.example")
    cached_accounts = [
        {"id": 1, "name": "exclude-me", "status": "active", "group_ids": [2]},
        {"id": 2, "name": "keep-me", "status": "active", "group_ids": [2]},
    ]
    test_db.replace_platform_dispatch_cache(
        "https://sub.example",
        cached_accounts,
        [{"id": 2, "name": "主分组"}],
        [],
        {"platform": "", "type": "", "status": "", "include_ungrouped": True},
    )

    with TestClient(app) as client:
        login(client)
        saved = client.put("/api/platform-dispatch/policy", json={"probe_interval_seconds": 125})
        excluded = client.post("/api/platform-dispatch/accounts/1/exclude")
        excluded_again = client.post("/api/platform-dispatch/accounts/1/exclude")
        missing = client.post("/api/platform-dispatch/accounts/99/exclude")
        invalid = client.post("/api/platform-dispatch/accounts/-1/exclude")
        cached = client.get("/api/platform-dispatch")
        restored = client.delete("/api/platform-dispatch/excluded-accounts/1")
        missing_restore = client.delete("/api/platform-dispatch/excluded-accounts/1")
        test_db.create_platform_dispatch_job("active-job", "accounts_sync", {}, "https://sub.example")
        conflict_exclude = client.post("/api/platform-dispatch/accounts/2/exclude")
        conflict_restore = client.delete("/api/platform-dispatch/excluded-accounts/1")

    assert saved.status_code == 200
    assert excluded.status_code == 200
    assert excluded.json()["config"]["probe_interval_seconds"] == 125
    assert excluded.json()["config"]["excluded_account_ids"] == [1]
    assert excluded_again.status_code == 200
    assert excluded_again.json()["config"]["excluded_account_ids"] == [1]
    assert missing.status_code == 404
    assert invalid.status_code == 400
    assert [item["id"] for item in cached.json()["accounts"]] == [1, 2]
    assert all(item["price_protection_status"] == "unbound" for item in cached.json()["accounts"])
    assert restored.status_code == 200
    assert restored.json()["config"]["excluded_account_ids"] == []
    assert restored.json()["config"]["probe_interval_seconds"] == 125
    assert missing_restore.status_code == 404
    assert conflict_exclude.status_code == 409
    assert conflict_restore.status_code == 409


def test_platform_dispatch_cost_binding_api_and_job_conflict(tmp_path, monkeypatch):
    test_db = setup_test_db(tmp_path, monkeypatch)
    test_db.set_setting("sub2api_site_url", "https://sub.example")
    balance_account_id = test_db.upsert_account(
        {
            "platform": "sub2Api",
            "name": "balance-source",
            "base_url": "https://balance.example",
            "api_key": "sk-test",
            "recharge_paid_amount": 1,
            "recharge_received_amount": 2,
        }
    )
    test_db.replace_account_monitor_groups(
        balance_account_id,
        [{"group_id": "pro", "plan_name": "Pro", "effective_rate_multiplier": 2}],
    )
    monitor_group_id = int(test_db.list_monitor_groups(balance_account_id)[0]["id"])
    checked_at = datetime.now(timezone.utc).isoformat()
    test_db.update_monitor_group_snapshot(
        monitor_group_id,
        {"group_id": "pro", "plan_name": "Pro", "effective_rate_multiplier": 2},
        checked_at,
    )
    test_db.replace_platform_dispatch_cache(
        "https://sub.example",
        [{"id": 1, "name": "dispatch", "status": "active", "group_ids": [8], "rate_multiplier": 99}],
        [{"id": 8, "name": "local", "rate_multiplier": 1.2}],
        [],
        {"platform": "", "type": "", "status": "", "include_ungrouped": True},
    )

    with TestClient(app) as client:
        login(client)
        options = client.get("/api/platform-dispatch/cost-source-options")
        bound = client.put(
            "/api/platform-dispatch/accounts/1/cost-binding",
            json={"monitor_group_id": monitor_group_id},
        )
        missing_account = client.put(
            "/api/platform-dispatch/accounts/99/cost-binding",
            json={"monitor_group_id": monitor_group_id},
        )
        invalid_group = client.put(
            "/api/platform-dispatch/accounts/1/cost-binding",
            json={"monitor_group_id": 99999},
        )
        deleted = client.delete("/api/platform-dispatch/accounts/1/cost-binding")
        deleted_again = client.delete("/api/platform-dispatch/accounts/1/cost-binding")
        client.put(
            "/api/platform-dispatch/accounts/1/cost-binding",
            json={"monitor_group_id": monitor_group_id},
        )
        test_db.create_platform_dispatch_job("active-cost-job", "accounts_sync", {}, "https://sub.example")
        conflict = client.delete("/api/platform-dispatch/accounts/1/cost-binding")

    assert options.status_code == 200
    option = options.json()["items"][0]
    assert option["monitor_group_id"] == monitor_group_id
    assert option["upstream_cost_multiplier"] == 1
    assert option["last_checked_at"] == checked_at
    assert bound.status_code == 200
    account = bound.json()["accounts"][0]
    assert account["cost_binding"]["monitor_group_id"] == monitor_group_id
    assert account["upstream_group_rate_multiplier"] == 2
    assert account["upstream_cost_multiplier"] == 1
    assert account["local_min_rate_multiplier"] == 1.2
    assert account["price_protection_status"] == "safe"
    assert "rate_multiplier" not in account
    assert "rateMultiplier" not in account
    assert missing_account.status_code == 404
    assert invalid_group.status_code == 404
    assert deleted.status_code == 200
    assert deleted.json()["accounts"][0]["price_protection_status"] == "unbound"
    assert deleted_again.status_code == 404
    assert conflict.status_code == 409


def test_platform_dispatch_account_probe_model_api_sets_override_and_restores_default(tmp_path, monkeypatch):
    test_db = setup_test_db(tmp_path, monkeypatch)
    test_db.set_setting("sub2api_site_url", "https://sub.example")
    cached_accounts = [{"id": 1, "name": "model-account", "status": "active", "group_ids": [2]}]
    test_db.replace_platform_dispatch_cache(
        "https://sub.example",
        cached_accounts,
        [{"id": 2, "name": "主分组"}],
        [],
        {"platform": "", "type": "", "status": "", "include_ungrouped": True},
    )

    with TestClient(app) as client:
        login(client)
        saved = client.put(
            "/api/platform-dispatch/policy",
            json={"default_probe_model": "default-model", "probe_interval_seconds": 125},
        )
        configured = client.put(
            "/api/platform-dispatch/accounts/1/probe-model",
            json={"model": " custom-model "},
        )
        invalid = client.put(
            "/api/platform-dispatch/accounts/1/probe-model",
            json={"model": "x" * 201},
        )
        missing = client.put(
            "/api/platform-dispatch/accounts/99/probe-model",
            json={"model": "other-model"},
        )
        restored = client.put("/api/platform-dispatch/accounts/1/probe-model", json={"model": ""})
        test_db.create_platform_dispatch_job("active-job", "accounts_sync", {}, "https://sub.example")
        conflict = client.put(
            "/api/platform-dispatch/accounts/1/probe-model",
            json={"model": "blocked-model"},
        )

    assert saved.status_code == 200
    assert configured.status_code == 200
    assert configured.json()["config"]["default_probe_model"] == "default-model"
    assert configured.json()["config"]["probe_interval_seconds"] == 125
    assert configured.json()["config"]["account_probe_models"] == {"1": "custom-model"}
    assert invalid.status_code == 400
    assert missing.status_code == 404
    assert restored.status_code == 200
    assert restored.json()["config"]["account_probe_models"] == {}
    assert restored.json()["config"]["default_probe_model"] == "default-model"
    assert conflict.status_code == 409


def test_platform_dispatch_account_probe_api_uses_configured_model_and_updates_health(tmp_path, monkeypatch):
    test_db = setup_test_db(tmp_path, monkeypatch)
    test_db.set_setting("sub2api_site_url", "https://sub.example")
    test_db.replace_platform_dispatch_cache(
        "https://sub.example",
        [{"id": 1, "name": "probe-account", "status": "active", "group_ids": [2]}],
        [{"id": 2, "name": "主分组"}],
        [],
        {"platform": "", "type": "", "status": "", "include_ungrouped": True},
    )
    test_db.save_platform_dispatch_policy(
        {"group_probe_models": {"2": "group-model"}}, "https://sub.example"
    )

    class ProbeClient:
        site_url = "https://sub.example"

        def __init__(self):
            self.probes = []

        async def probe_account(self, account_id, model=None):
            self.probes.append((account_id, model))
            return {"success": True, "message": "ok"}

    probe_client = ProbeClient()
    monkeypatch.setattr("app.main.sub2api_admin_client", lambda: probe_client)

    with TestClient(app) as client:
        login(client)
        response = client.post("/api/platform-dispatch/accounts/1/probe")
        missing = client.post("/api/platform-dispatch/accounts/99/probe")
        test_db.create_platform_dispatch_job("active-job", "accounts_sync", {}, "https://sub.example")
        conflict = client.post("/api/platform-dispatch/accounts/1/probe")

    assert response.status_code == 200
    assert response.json()["probe"] == {
        "success": True,
        "message": "ok",
        "account_id": 1,
        "model": "group-model",
        "health_score": 100.0,
    }
    assert probe_client.probes == [(1, "group-model")]
    state = response.json()["accounts"][0]
    assert state["health_score"] == 100.0
    assert state["probe_records"][0]["is_probe_success"] is True
    assert missing.status_code == 404
    assert conflict.status_code == 409


def test_platform_dispatch_account_probe_api_uses_group_platform_default(tmp_path, monkeypatch):
    test_db = setup_test_db(tmp_path, monkeypatch)
    test_db.set_setting("sub2api_site_url", "https://sub.example")
    test_db.replace_platform_dispatch_cache(
        "https://sub.example",
        [{"id": 1, "name": "anthropic-account", "status": "active", "group_ids": [2]}],
        [{"id": 2, "name": "Anthropic 分组", "platform": "anthropic"}],
        [],
        {"platform": "", "type": "", "status": "", "include_ungrouped": True},
    )
    test_db.save_platform_dispatch_policy(
        {"auto_scoring_enabled": False}, "https://sub.example"
    )

    class ProbeClient:
        site_url = "https://sub.example"

        def __init__(self):
            self.probes = []

        async def probe_account(self, account_id, model=None):
            self.probes.append((account_id, model))
            return {"success": True, "message": "ok"}

    probe_client = ProbeClient()
    monkeypatch.setattr("app.main.sub2api_admin_client", lambda: probe_client)

    with TestClient(app) as client:
        login(client)
        response = client.post("/api/platform-dispatch/accounts/1/probe")

    assert response.status_code == 200
    assert response.json()["probe"]["model"] == "claude-sonnet-4-6"
    assert probe_client.probes == [(1, "claude-sonnet-4-6")]


def test_platform_dispatch_group_probe_model_api_sets_override_and_restores_default(tmp_path, monkeypatch):
    test_db = setup_test_db(tmp_path, monkeypatch)
    test_db.set_setting("sub2api_site_url", "https://sub.example")
    test_db.replace_platform_dispatch_cache(
        "https://sub.example",
        [{"id": 1, "name": "model-account", "status": "active", "group_ids": [2]}],
        [{"id": 2, "name": "主分组"}],
        [],
        {"platform": "", "type": "", "status": "", "include_ungrouped": True},
    )

    with TestClient(app) as client:
        login(client)
        saved = client.put(
            "/api/platform-dispatch/policy",
            json={"defaultProbeModel": "default-model", "groupProbeModels": {"3": "other-group"}},
        )
        configured = client.put(
            "/api/platform-dispatch/groups/2/probe-model",
            json={"probeModel": " group-model "},
        )
        invalid = client.put(
            "/api/platform-dispatch/groups/2/probe-model",
            json={"model": "x" * 201},
        )
        missing = client.put(
            "/api/platform-dispatch/groups/99/probe-model",
            json={"model": "missing-model"},
        )
        restored = client.put("/api/platform-dispatch/groups/2/probe-model", json={"model": ""})
        test_db.create_platform_dispatch_job("active-job", "accounts_sync", {}, "https://sub.example")
        conflict = client.put(
            "/api/platform-dispatch/groups/2/probe-model",
            json={"model": "blocked-model"},
        )

    assert saved.status_code == 200
    assert saved.json()["config"]["group_probe_models"] == {"3": "other-group"}
    assert configured.status_code == 200
    assert configured.json()["config"]["default_probe_model"] == "default-model"
    assert configured.json()["config"]["group_probe_models"] == {
        "2": "group-model",
        "3": "other-group",
    }
    assert invalid.status_code == 400
    assert missing.status_code == 404
    assert restored.status_code == 200
    assert restored.json()["config"]["group_probe_models"] == {"3": "other-group"}
    assert conflict.status_code == 409


def test_merge_recent_activity_keeps_success_and_error_records():
    activities = merge_recent_activity(
        [
            {
                "id": 11,
                "user_id": 7,
                "user": {"id": 7, "email": "user@example.com"},
                "model": "gpt-5",
                "upstream_model": "gpt-5.1",
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_creation_tokens": 3,
                "cache_read_tokens": 4,
                "actual_cost": 0.012345,
                "total_cost": 0.02,
                "first_token_ms": 320,
                "duration_ms": 1480,
                "created_at": "2026-07-25T08:00:00Z",
            }
        ],
        [
            {
                "id": 22,
                "user_id": 8,
                "user_email": "failed@example.com",
                "requested_model": "gpt-5-mini",
                "status_code": 429,
                "message": "rate limited",
                "phase": "upstream",
                "created_at": "2026-07-25T09:00:00Z",
            }
        ],
        6,
    )

    assert [item["kind"] for item in activities] == ["error", "success"]
    assert activities[0]["user_email"] == "failed@example.com"
    assert activities[0]["status_code"] == 429
    assert activities[0]["total_tokens"] is None
    assert activities[1]["user_email"] == "user@example.com"
    assert activities[1]["model"] == "gpt-5.1"
    assert activities[1]["requested_model"] == "gpt-5"
    assert activities[1]["total_tokens"] == 127
    assert activities[1]["cost"] == 0.012345


def test_public_dispatch_account_does_not_expose_credentials():
    account = public_dispatch_account(
        {
            "id": 3,
            "name": "dispatch-account",
            "platform": "openai",
            "type": "apikey",
            "status": "active",
            "rate_multiplier": 7,
            "rateMultiplier": 8,
            "rate_limit_reset_at": "2999-01-01T00:00:00Z",
            "credentials": {"api_key": "secret"},
            "group_ids": [5],
        },
        [],
    )

    assert account["is_enabled"] is True
    assert account["filter_status"] == "rate_limited"
    assert account["group_ids"] == [5]
    assert "rate_multiplier" not in account
    assert "rateMultiplier" not in account
    assert "credentials" not in account
    assert "secret" not in str(account)


@pytest.mark.parametrize(
    ("account", "expected"),
    [
        ({"status": "active"}, "active"),
        ({"status": "inactive"}, "inactive"),
        ({"status": "error"}, "error"),
        ({"status": "active", "rate_limit_reset_at": "2999-01-01T00:00:00Z"}, "rate_limited"),
        (
            {
                "status": "active",
                "rate_limit_reset_at": "2999-01-01T00:00:00Z",
                "temp_unschedulable_until": "2999-01-02T00:00:00Z",
            },
            "temp_unschedulable",
        ),
        ({"status": "active", "schedulable": False}, "unschedulable"),
        (
            {"status": "active", "schedulable": False, "rate_limit_reset_at": "2999-01-01T00:00:00Z"},
            "rate_limited",
        ),
    ],
)
def test_public_dispatch_account_derives_sub2api_filter_status(account, expected):
    public = public_dispatch_account({"id": 1, "name": "runtime", **account}, [])

    assert public["filter_status"] == expected
    assert public["filterStatus"] == expected


@pytest.mark.asyncio
async def test_sub2api_admin_list_accounts_reads_all_pages(monkeypatch):
    DummyAsyncClient.requests = []
    DummyAsyncClient.responses = [
        DummyResponse({"code": 0, "data": {"items": [{"id": 1, "name": "a"}], "total": 2, "pages": 2}}),
        DummyResponse({"code": 0, "data": {"items": [{"id": 2, "name": "b"}], "total": 2, "pages": 2}}),
    ]
    monkeypatch.setattr("app.services.sub2api_admin.httpx.AsyncClient", DummyAsyncClient)

    accounts = await Sub2ApiAdminClient("https://sub.example", "admin-key").list_accounts(
        platform="openai",
        account_type="apikey",
        status="active",
    )

    assert [account["id"] for account in accounts] == [1, 2]
    assert [request["params"]["page"] for request in DummyAsyncClient.requests] == [1, 2]
    assert all(request["headers"]["x-api-key"] == "admin-key" for request in DummyAsyncClient.requests)
    assert all(request["params"]["platform"] == "openai" for request in DummyAsyncClient.requests)
    assert all(request["params"]["type"] == "apikey" for request in DummyAsyncClient.requests)
    assert all(request["params"]["status"] == "active" for request in DummyAsyncClient.requests)


@pytest.mark.asyncio
async def test_sub2api_admin_updates_account_schedulable(monkeypatch):
    DummyAsyncClient.requests = []
    DummyAsyncClient.responses = [
        DummyResponse({"code": 0, "data": {"id": 9, "name": "remote", "status": "active", "schedulable": False}})
    ]
    monkeypatch.setattr("app.services.sub2api_admin.httpx.AsyncClient", DummyAsyncClient)

    account = await Sub2ApiAdminClient("https://sub.example/", "admin-key").update_account_schedulable(9, False)

    assert account["status"] == "active"
    assert account["schedulable"] is False
    request = DummyAsyncClient.requests[0]
    assert request["method"] == "POST"
    assert request["url"] == "https://sub.example/api/v1/admin/accounts/9/schedulable"
    assert request["json"] == {"schedulable": False}


@pytest.mark.asyncio
async def test_sub2api_admin_probe_sends_configured_model_and_omits_empty_model(monkeypatch):
    configured_response = DummyResponse(None)
    configured_response.text = 'data: {"type":"done"}'
    default_response = DummyResponse(None)
    default_response.text = 'data: {"type":"done"}'
    DummyAsyncClient.requests = []
    DummyAsyncClient.responses = [configured_response, default_response]
    monkeypatch.setattr("app.services.sub2api_admin.httpx.AsyncClient", DummyAsyncClient)
    admin_client = Sub2ApiAdminClient("https://sub.example", "admin-key")

    configured = await admin_client.probe_account(9, model="gpt-5-mini")
    default = await admin_client.probe_account(10)

    assert configured["success"] is True
    assert default["success"] is True
    assert DummyAsyncClient.requests[0]["json"] == {"model_id": "gpt-5-mini"}
    assert DummyAsyncClient.requests[1]["json"] == {}


def test_platform_dispatch_cache_roundtrip_replace_clear_and_status_merge(tmp_path):
    db = Database(str(tmp_path / "app.db"), "test-key")
    db.init()
    first_account = {
        "id": 9,
        "name": "remote",
        "status": "active",
        "is_enabled": True,
        "recent_activity": [{"id": "usage-1"}],
    }
    db.replace_platform_dispatch_cache(
        "https://sub.example/",
        [first_account],
        [{"id": 2, "name": "主分组"}],
        ["partial warning"],
        {"platform": "openai", "type": "apikey", "status": "active"},
        6,
    )

    cached = db.get_platform_dispatch_cache()
    assert cached["source_site_url"] == "https://sub.example"
    assert cached["accounts"] == [first_account]
    assert cached["refresh_filter"] == {
        "platform": "openai",
        "type": "apikey",
        "status": "active",
        "include_ungrouped": True,
    }
    assert cached["warnings"] == ["partial warning"]

    assert db.update_platform_dispatch_cached_account(
        {"id": 9, "status": "inactive", "is_enabled": False, "isEnabled": False}
    )
    cached = db.get_platform_dispatch_cache()
    assert cached["accounts"][0]["status"] == "inactive"
    assert cached["accounts"][0]["is_enabled"] is False
    assert cached["accounts"][0]["isEnabled"] is False
    assert cached["accounts"][0]["recent_activity"] == [{"id": "usage-1"}]

    db.replace_platform_dispatch_cache(
        "https://sub.example",
        [],
        [],
        [],
        {"platform": "gemini", "type": "", "status": ""},
    )
    assert db.get_platform_dispatch_cache()["accounts"] == []
    db.clear_platform_dispatch_cache()
    assert db.get_platform_dispatch_cache() is None


@pytest.mark.asyncio
async def test_platform_dispatch_passes_filters_to_accounts_api():
    client = Sub2ApiAdminClient("https://sub.example", "admin-key")
    usage_ids = []
    error_ids = []

    async def list_accounts(**kwargs):
        assert kwargs == {"platform": "openai", "account_type": "apikey", "status": "active"}
        return [
            {"id": 1, "name": "matched", "platform": "OPENAI", "type": "apikey", "status": "active"},
        ]

    async def list_groups(platform=None):
        assert platform == "openai"
        return []

    async def list_usage(account_id, limit=6):
        usage_ids.append(account_id)
        return []

    async def list_errors(account_id=None, limit=6):
        error_ids.append(account_id)
        return []

    client.list_accounts = list_accounts
    client.list_groups = list_groups
    client.list_recent_usage = list_usage
    client.list_recent_errors = list_errors

    result = await client.platform_dispatch(6, platform="openai", account_type="apikey", status="active")

    assert [account["id"] for account in result["accounts"]] == [1]
    assert usage_ids == [1]
    assert error_ids == [None, 1]


def test_platform_dispatch_syncs_pages_without_loading_activity_and_preserves_cached_activity(tmp_path, monkeypatch):
    test_db = setup_test_db(tmp_path, monkeypatch)
    test_db.set_setting("sub2api_site_url", "https://sub.example")
    test_db.replace_platform_dispatch_cache(
        "https://sub.example",
        [{"id": 9, "name": "cached", "status": "active", "recent_activity": [{"id": "old-activity"}]}],
        [],
        [],
        {"platform": "", "type": "", "status": ""},
        activities_refreshed_at="2026-07-25T01:00:00+00:00",
    )

    class FakeAdminClient:
        def __init__(self):
            self.site_url = "https://sub.example"
            self.pages = []
            self.updated = []

        async def list_groups(self, platform=None):
            assert platform == "openai"
            return [{"id": 2, "name": "主分组", "platform": "openai"}]

        async def list_accounts_page(self, page, page_size, **filters):
            self.pages.append((page, page_size, filters))
            accounts = {
                1: [
                    {"id": 9, "name": "remote", "platform": "openai", "type": "apikey", "status": "active", "group_ids": [2]},
                ],
                2: [{"id": 10, "name": "new", "platform": "openai", "type": "apikey", "status": "active", "group_ids": [2]}],
            }
            return {
                "accounts": accounts[page],
                "page": page,
                "page_size": page_size,
                "total": 2,
                "pages": 2,
            }

        async def update_account_schedulable(self, account_id, schedulable):
            self.updated.append((account_id, schedulable))
            return {
                "id": account_id,
                "name": "remote",
                "status": "active",
                "is_enabled": True,
                "schedulable": schedulable,
            }

    fake = FakeAdminClient()
    monkeypatch.setattr("app.main.sub2api_admin_client", lambda: fake)

    with TestClient(app) as client:
        login(client)
        page = client.get("/platform-dispatch")
        before = client.get("/api/platform-dispatch")
        invalid_filter = client.post("/api/platform-dispatch/refresh", json={"status": "unknown"})
        started = client.post(
            "/api/platform-dispatch/sync",
            json={"platform": " openai ", "type": "apikey", "status": "ACTIVE"},
        )
        job = wait_for_dispatch_job(client)
        response = client.get("/api/platform-dispatch")
        invalid = client.post("/api/platform-dispatch/accounts/9/schedulable", json={"schedulable": "yes"})
        updated = client.post("/api/platform-dispatch/accounts/9/schedulable", json={"schedulable": False})
        after_update = client.get("/api/platform-dispatch")

    assert page.status_code == 200
    assert '<div id="app"></div>' in page.text
    assert before.json()["accounts"][0]["name"] == "cached"
    assert invalid_filter.status_code == 400
    assert started.status_code == 202
    assert started.json()["job"]["kind"] == "accounts_sync"
    assert job["status"] == "succeeded"
    assert job["current_page"] == 2
    assert job["total_pages"] == 2
    assert job["percent"] == 100
    assert [item[0] for item in fake.pages] == [1, 2]
    assert all(item[1] == 100 for item in fake.pages)
    assert all(
        item[2] == {"platform": "openai", "account_type": "apikey", "status": "active"}
        for item in fake.pages
    )
    assert response.status_code == 200
    assert response.json()["site_url"] == "https://sub.example"
    assert response.json()["refresh_filter"] == {
        "platform": "openai",
        "type": "apikey",
        "status": "active",
        "include_ungrouped": True,
    }
    assert response.json()["accounts"][0]["recent_activity"] == [{"id": "old-activity"}]
    assert response.json()["accounts"][1]["recent_activity"] == []
    assert [account["id"] for account in response.json()["accounts"]] == [9, 10]
    assert response.json()["activities_refreshed_at"] == "2026-07-25T01:00:00+00:00"
    assert invalid.status_code == 400
    assert updated.status_code == 200
    assert updated.json()["account"]["status"] == "active"
    assert updated.json()["account"]["schedulable"] is False
    assert after_update.json()["accounts"][0]["status"] == "active"
    assert after_update.json()["accounts"][0]["schedulable"] is False
    assert after_update.json()["accounts"][0]["recent_activity"] == [{"id": "old-activity"}]
    assert fake.updated == [(9, False)]


def test_platform_dispatch_sync_failure_keeps_cache_and_site_change_clears_it(tmp_path, monkeypatch):
    test_db = setup_test_db(tmp_path, monkeypatch)
    test_db.set_setting("sub2api_site_url", "https://sub.example")
    test_db.replace_platform_dispatch_cache(
        "https://sub.example",
        [{"id": 7, "name": "cached", "status": "active"}],
        [],
        [],
        {"platform": "", "type": "", "status": ""},
    )

    class FailingAdminClient:
        site_url = "https://sub.example"
        pages = []

        async def list_groups(self, **kwargs):
            return []

        async def list_accounts_page(self, page, **kwargs):
            self.pages.append(page)
            if page == 2:
                raise Sub2ApiAdminError("upstream unavailable", status_code=502)
            return {
                "accounts": [{"id": 99, "name": "partial", "status": "active"}],
                "page": 1,
                "page_size": 100,
                "total": 2,
                "pages": 2,
            }

    monkeypatch.setattr("app.main.sub2api_admin_client", lambda: FailingAdminClient())

    with TestClient(app) as client:
        login(client)
        started = client.post("/api/platform-dispatch/refresh", json={})
        failed = wait_for_dispatch_job(client)
        still_cached = client.get("/api/platform-dispatch")
        same_site = client.post("/api/settings/sub2api", json={"site_url": "https://sub.example"})
        after_same_site = client.get("/api/platform-dispatch")
        changed_site = client.post("/api/settings/sub2api", json={"site_url": "https://other.example"})
        after_changed_site = client.get("/api/platform-dispatch")

    assert started.status_code == 202
    assert failed["status"] == "failed"
    assert failed["error"] == "upstream unavailable"
    assert FailingAdminClient.pages == [1, 2]
    assert still_cached.json()["accounts"][0]["name"] == "cached"
    assert same_site.status_code == 200
    assert after_same_site.json()["has_cache"] is True
    assert changed_site.status_code == 200
    assert after_changed_site.json()["has_cache"] is False


def test_platform_dispatch_evidence_refresh_reloads_history_probes_and_recalculates(tmp_path, monkeypatch):
    test_db = setup_test_db(tmp_path, monkeypatch)
    test_db.set_setting("sub2api_site_url", "https://sub.example")
    test_db.replace_platform_dispatch_cache(
        "https://sub.example",
        [
            {
                "id": 1,
                "name": "works",
                "status": "active",
                "group_ids": [7],
                "recent_activity": [{"id": "old-1"}],
            },
            {
                "id": 2,
                "name": "fails",
                "status": "active",
                "group_ids": [7],
                "recent_activity": [{"id": "old-2"}],
            },
        ],
        [],
        [],
        {"platform": "", "type": "", "status": ""},
    )
    test_db.save_platform_dispatch_policy(
        {"group_probe_models": {"7": "group-model"}}, "https://sub.example"
    )
    evidence_now = datetime.now(timezone.utc)

    class EvidenceClient:
        site_url = "https://sub.example"

        def __init__(self):
            self.probe_models = []

        async def list_usage_page(self, account_id, page, page_size, start_date):
            assert page_size == 100
            assert start_date
            if account_id == 2:
                raise Sub2ApiAdminError("usage unavailable", status_code=502)
            records = [
                {
                    "id": index,
                    "created_at": (evidence_now - timedelta(seconds=80 - index * 10)).isoformat(),
                    "model": "gpt-5",
                }
                for index in range(1, 7)
            ]
            return {"records": records, "pages": 1}

        async def list_errors_page(self, account_id, page, page_size, time_range):
            assert page_size == 100
            assert time_range == "30d"
            if account_id == 2:
                raise Sub2ApiAdminError("errors unavailable", status_code=502)
            return {
                "records": [
                    {
                        "id": 99,
                        "created_at": (evidence_now - timedelta(seconds=10)).isoformat(),
                        "message": "latest error",
                    }
                ],
                "pages": 1,
            }

        async def probe_account(self, account_id, model=None):
            self.probe_models.append((account_id, model))
            if account_id == 2:
                return {"success": False, "status_code": 503, "message": "probe unavailable"}
            return {"success": True, "message": "ok"}

    evidence_client = EvidenceClient()
    monkeypatch.setattr("app.main.sub2api_admin_client", lambda: evidence_client)

    with TestClient(app) as client:
        login(client)
        started = client.post("/api/platform-dispatch/evidence/refresh")
        job = wait_for_dispatch_job(client)
        response = client.get("/api/platform-dispatch")
        policy = client.get("/api/platform-dispatch/policy")

    assert started.status_code == 202
    assert job["status"] == "succeeded"
    assert job["processed"] == 2
    assert job["total"] == 2
    assert job["kind"] == "evidence_refresh"
    assert evidence_client.probe_models == [(1, "group-model"), (2, "group-model")]
    cached_accounts = response.json()["accounts"]
    assert cached_accounts[0]["recent_activity"] == [{"id": "old-1"}]
    assert cached_accounts[1]["recent_activity"] == [{"id": "old-2"}]
    assert response.json()["activities_refreshed_at"]
    assert any("usage unavailable" in warning for warning in response.json()["warnings"])
    states = {item["account_id"]: item for item in policy.json()["accounts"]}
    assert states[1]["evidence_count"] == 8
    assert states[1]["probe_records"][0]["is_probe_success"] is True
    short_evidence = states[1]["short_evidence_records"]
    assert states[1]["shortEvidenceRecords"] == short_evidence
    assert [item["source_kind"] for item in short_evidence] == ["probe", "error"] + ["usage"] * 6
    assert short_evidence[0]["sourceKind"] == "probe"
    assert short_evidence[0]["isProbeSuccess"] is True
    recent_requests = states[1]["recent_request_records"]
    assert states[1]["recentRequestRecords"] == recent_requests
    assert [item["source_kind"] for item in recent_requests] == ["error"] + ["usage"] * 6
    assert all(item["source_kind"] != "probe" for item in recent_requests)
    assert {
        "source_kind",
        "category",
        "score",
        "status_code",
        "first_token_ms",
        "is_timeout",
        "message",
        "occurred_at",
    } <= short_evidence[0].keys()
    assert states[2]["evidence_count"] == 1
    assert states[2]["health_score"] == 10
    assert states[2]["probe_records"][0]["is_probe_success"] is False
    assert states[2]["probe_records"][0]["status_code"] == 503
    assert [item["source_kind"] for item in states[2]["short_evidence_records"]] == ["probe"]
    assert states[2]["shortEvidenceRecords"] == states[2]["short_evidence_records"]
    assert states[2]["short_evidence_records"][0]["is_probe_success"] is False
    assert states[2]["recent_request_records"] == []


def test_platform_dispatch_job_exclusion_interrupt_and_activity_without_cache(tmp_path, monkeypatch):
    test_db = setup_test_db(tmp_path, monkeypatch)
    first = test_db.create_platform_dispatch_job("job-1", "accounts_sync", {}, "https://sub.example")
    second = test_db.create_platform_dispatch_job("job-2", "activity_refresh", {}, "https://sub.example")

    assert first["status"] == "queued"
    assert second is None
    assert test_db.interrupt_platform_dispatch_job()
    assert test_db.get_platform_dispatch_job()["status"] == "failed"
    assert "重启中断" in test_db.get_platform_dispatch_job()["error"]

    with TestClient(app) as client:
        login(client)
        response = client.post("/api/platform-dispatch/activities/refresh")

    assert response.status_code == 409
    assert response.json()["message"] == "请先同步账号信息"


def test_platform_dispatch_duplicate_api_job_returns_conflict(tmp_path, monkeypatch):
    test_db = setup_test_db(tmp_path, monkeypatch)
    test_db.set_setting("sub2api_site_url", "https://sub.example")

    class SlowClient:
        site_url = "https://sub.example"

        async def list_groups(self, **kwargs):
            await asyncio.sleep(10)
            return []

    monkeypatch.setattr("app.main.sub2api_admin_client", lambda: SlowClient())

    with TestClient(app) as client:
        login(client)
        first = client.post("/api/platform-dispatch/sync", json={})
        second = client.post("/api/platform-dispatch/sync", json={})

    assert first.status_code == 202
    assert second.status_code == 409
    assert second.json()["job"]["job_id"] == first.json()["job"]["job_id"]


def test_platform_dispatch_evidence_refresh_keeps_activity_when_history_fails(tmp_path, monkeypatch):
    test_db = setup_test_db(tmp_path, monkeypatch)
    test_db.set_setting("sub2api_site_url", "https://sub.example")
    original = [{"id": 1, "name": "cached", "status": "active", "recent_activity": [{"id": "old"}]}]
    test_db.replace_platform_dispatch_cache(
        "https://sub.example",
        original,
        [],
        [],
        {"platform": "", "type": "", "status": ""},
    )

    class FailingActivityClient:
        site_url = "https://sub.example"

        async def list_recent_usage(self, account_id, limit):
            raise Sub2ApiAdminError("usage unavailable", status_code=502)

        async def list_recent_errors(self, account_id, limit):
            raise Sub2ApiAdminError("errors unavailable", status_code=502)

        async def probe_account(self, account_id, model=None):
            return {"success": False, "is_timeout": True, "message": "probe timeout"}

    monkeypatch.setattr("app.main.sub2api_admin_client", lambda: FailingActivityClient())

    with TestClient(app) as client:
        login(client)
        started = client.post("/api/platform-dispatch/evidence/refresh")
        job = wait_for_dispatch_job(client)
        cache = client.get("/api/platform-dispatch").json()
        policy = client.get("/api/platform-dispatch/policy").json()

    assert started.status_code == 202
    assert job["status"] == "succeeded"
    assert cache["accounts"][0]["recent_activity"] == [{"id": "old"}]
    assert cache["activities_refreshed_at"]
    state = policy["accounts"][0]
    assert state["health_score"] == 10
    assert state["probe_records"][0]["category"] == "probe_failure"


def test_platform_dispatch_excluded_group_prunes_cache_and_is_scoped_by_site(tmp_path):
    db = Database(str(tmp_path / "app.db"), "test-key")
    db.init()
    db.replace_platform_dispatch_cache(
        "https://sub.example",
        [
            {"id": 1, "name": "only-excluded", "group_ids": [2], "groupIds": [2]},
            {
                "id": 2,
                "name": "multi-group",
                "group_id": 2,
                "group_ids": [2, 3],
                "groupIds": [2, 3],
                "groups": [{"id": 2}, {"id": 3}],
                "plans": [{"group_id": 2}, {"group_id": 3}],
            },
            {"id": 3, "name": "ungrouped", "group_ids": []},
        ],
        [{"id": 2, "name": "排除组"}, {"id": 3, "name": "保留组"}],
        [],
        {"platform": "", "type": "", "status": ""},
    )

    db.exclude_platform_dispatch_group("https://sub.example/", 2, "排除组", "openai")

    excluded = db.list_platform_dispatch_excluded_groups("https://sub.example")
    cached = db.get_platform_dispatch_cache()
    assert [(group["id"], group["name"], group["platform"]) for group in excluded] == [(2, "排除组", "openai")]
    assert db.list_platform_dispatch_excluded_groups("https://other.example") == []
    assert [group["id"] for group in cached["groups"]] == [3]
    assert [account["id"] for account in cached["accounts"]] == [2, 3]
    multi_group = cached["accounts"][0]
    assert multi_group["group_ids"] == [3]
    assert multi_group["groupIds"] == [3]
    assert not {"group_id", "groups", "plans"}.intersection(multi_group)

    assert db.remove_platform_dispatch_excluded_group("https://sub.example", 2)
    assert db.list_platform_dispatch_excluded_groups("https://sub.example") == []
    assert [account["id"] for account in db.get_platform_dispatch_cache()["accounts"]] == [2, 3]


def test_platform_dispatch_exclude_ungrouped_prunes_cache_and_updates_sync_default(tmp_path):
    db = Database(str(tmp_path / "app.db"), "test-key")
    db.init()
    db.replace_platform_dispatch_cache(
        "https://sub.example",
        [
            {"id": 1, "name": "grouped", "group_ids": [2]},
            {"id": 2, "name": "ungrouped", "group_ids": []},
            {"id": 3, "name": "legacy-grouped", "groups": [{"id": 3}]},
        ],
        [{"id": 2, "name": "主分组"}, {"id": 3, "name": "兼容分组"}],
        ["保留告警"],
        {"platform": "openai", "type": "oauth", "status": "active", "include_ungrouped": True},
        activities_refreshed_at="2026-07-26T00:00:00+00:00",
    )
    before = db.get_platform_dispatch_cache()

    assert db.exclude_platform_dispatch_ungrouped_accounts("https://other.example") is False
    assert db.exclude_platform_dispatch_ungrouped_accounts("https://sub.example/") is True

    cached = db.get_platform_dispatch_cache()
    assert [account["id"] for account in cached["accounts"]] == [1, 3]
    assert cached["refresh_filter"] == {
        "platform": "openai",
        "type": "oauth",
        "status": "active",
        "include_ungrouped": False,
    }
    assert cached["groups"] == before["groups"]
    assert cached["warnings"] == ["保留告警"]
    assert cached["activities_refreshed_at"] == before["activities_refreshed_at"]
    assert cached["refreshed_at"] == before["refreshed_at"]


def test_platform_dispatch_excluded_group_api_updates_cache_and_conflicts_with_job(tmp_path, monkeypatch):
    test_db = setup_test_db(tmp_path, monkeypatch)
    test_db.set_setting("sub2api_site_url", "https://sub.example")
    test_db.replace_platform_dispatch_cache(
        "https://sub.example",
        [
            {"id": 1, "name": "only", "group_ids": [2]},
            {"id": 2, "name": "multi", "group_ids": [2, 3]},
        ],
        [{"id": 2, "name": "排除组", "platform": "openai"}, {"id": 3, "name": "保留组"}],
        [],
        {"platform": "", "type": "", "status": ""},
    )

    with TestClient(app) as client:
        login(client)
        excluded = client.post("/api/platform-dispatch/groups/2/exclude")
        restored = client.delete("/api/platform-dispatch/excluded-groups/2")
        test_db.exclude_platform_dispatch_group("https://sub.example", 3, "保留组", "")
        test_db.create_platform_dispatch_job("active-job", "accounts_sync", {}, "https://sub.example")
        conflict_exclude = client.post("/api/platform-dispatch/groups/99/exclude")
        conflict_restore = client.delete("/api/platform-dispatch/excluded-groups/3")

    assert excluded.status_code == 200
    assert [group["id"] for group in excluded.json()["groups"]] == [3]
    assert [group["id"] for group in excluded.json()["excluded_groups"]] == [2]
    assert [account["id"] for account in excluded.json()["accounts"]] == [2]
    assert excluded.json()["accounts"][0]["group_ids"] == [3]
    assert restored.status_code == 200
    assert restored.json()["excluded_groups"] == []
    assert [account["id"] for account in restored.json()["accounts"]] == [2]
    assert conflict_exclude.status_code == 409
    assert conflict_restore.status_code == 409


def test_platform_dispatch_ungrouped_exclude_api_updates_cache_and_conflicts_with_job(tmp_path, monkeypatch):
    test_db = setup_test_db(tmp_path, monkeypatch)
    test_db.set_setting("sub2api_site_url", "https://sub.example")
    test_db.replace_platform_dispatch_cache(
        "https://sub.example",
        [
            {"id": 1, "name": "grouped", "group_ids": [2]},
            {"id": 2, "name": "ungrouped", "group_ids": []},
        ],
        [{"id": 2, "name": "主分组"}],
        [],
        {"platform": "", "type": "", "status": "", "include_ungrouped": True},
    )

    with TestClient(app) as client:
        login(client)
        excluded = client.post("/api/platform-dispatch/ungrouped/exclude")
        test_db.create_platform_dispatch_job("active-job", "accounts_sync", {}, "https://sub.example")
        conflict = client.post("/api/platform-dispatch/ungrouped/exclude")

    assert excluded.status_code == 200
    assert [account["id"] for account in excluded.json()["accounts"]] == [1]
    assert excluded.json()["refresh_filter"]["include_ungrouped"] is False
    assert conflict.status_code == 409


def test_platform_dispatch_can_exclude_group_referenced_only_by_cached_account(tmp_path, monkeypatch):
    test_db = setup_test_db(tmp_path, monkeypatch)
    test_db.set_setting("sub2api_site_url", "https://sub.example")
    test_db.replace_platform_dispatch_cache(
        "https://sub.example",
        [{"id": 1, "name": "fallback-group", "group_ids": [99]}],
        [],
        [],
        {"platform": "", "type": "", "status": ""},
    )

    with TestClient(app) as client:
        login(client)
        response = client.post("/api/platform-dispatch/groups/99/exclude")

    assert response.status_code == 200
    assert response.json()["accounts"] == []
    assert response.json()["excluded_groups"][0]["id"] == 99
    assert response.json()["excluded_groups"][0]["name"] == "分组 99"


def test_platform_dispatch_sync_filters_excluded_groups_and_refreshes_their_metadata(tmp_path, monkeypatch):
    test_db = setup_test_db(tmp_path, monkeypatch)
    test_db.set_setting("sub2api_site_url", "https://sub.example")
    test_db.exclude_platform_dispatch_group("https://sub.example", 2, "旧名称", "")

    class ExcludedGroupClient:
        site_url = "https://sub.example"

        async def list_groups(self, platform=None):
            return [
                {"id": 2, "name": "新名称", "platform": "openai"},
                {"id": 3, "name": "保留组", "platform": "openai"},
            ]

        async def list_accounts_page(self, page, page_size, **filters):
            assert page == 1
            return {
                "accounts": [
                    {"id": 1, "name": "only-excluded", "status": "active", "group_ids": [2]},
                    {"id": 2, "name": "multi", "status": "active", "group_ids": [2, 3]},
                    {"id": 3, "name": "included", "status": "active", "group_ids": [3]},
                    {"id": 4, "name": "ungrouped", "status": "active", "group_ids": []},
                ],
                "page": 1,
                "page_size": page_size,
                "total": 4,
                "pages": 1,
            }

    monkeypatch.setattr("app.main.sub2api_admin_client", lambda: ExcludedGroupClient())

    with TestClient(app) as client:
        login(client)
        started = client.post("/api/platform-dispatch/sync", json={})
        job = wait_for_dispatch_job(client)
        response = client.get("/api/platform-dispatch").json()

    assert started.status_code == 202
    assert job["status"] == "succeeded"
    assert [group["id"] for group in response["groups"]] == [3]
    assert [account["id"] for account in response["accounts"]] == [2, 3, 4]
    assert response["accounts"][0]["group_ids"] == [3]
    assert response["excluded_groups"][0]["name"] == "新名称"
    assert response["excluded_groups"][0]["platform"] == "openai"


def test_platform_dispatch_response_filters_stale_cache_against_excluded_groups(tmp_path, monkeypatch):
    test_db = setup_test_db(tmp_path, monkeypatch)
    test_db.set_setting("sub2api_site_url", "https://sub.example")
    test_db.exclude_platform_dispatch_group("https://sub.example", 11, "排除组 11", "openai")
    test_db.exclude_platform_dispatch_group("https://sub.example", 19, "排除组 19", "openai")
    test_db.replace_platform_dispatch_cache(
        "https://sub.example",
        [
            {"id": 1, "name": "only-eleven", "group_ids": [11]},
            {"id": 2, "name": "only-nineteen", "group_ids": [19]},
            {"id": 3, "name": "mixed", "group_ids": [11, 23]},
            {"id": 4, "name": "included", "group_ids": [23]},
        ],
        [
            {"id": 11, "name": "排除组 11"},
            {"id": 19, "name": "排除组 19"},
            {"id": 23, "name": "保留组"},
        ],
        [],
        {"platform": "", "type": "", "status": ""},
    )

    with TestClient(app) as client:
        login(client)
        response = client.get("/api/platform-dispatch")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert [group["id"] for group in payload["groups"]] == [23]
    assert [account["id"] for account in payload["accounts"]] == [3, 4]
    assert payload["accounts"][0]["group_ids"] == [23]
    assert [group["id"] for group in payload["excluded_groups"]] == [11, 19]


def test_platform_dispatch_sync_can_exclude_ungrouped_accounts(tmp_path, monkeypatch):
    test_db = setup_test_db(tmp_path, monkeypatch)
    test_db.set_setting("sub2api_site_url", "https://sub.example")

    class AccountClient:
        site_url = "https://sub.example"

        async def list_groups(self, platform=None):
            return [{"id": 2, "name": "主分组", "platform": "openai"}]

        async def list_accounts_page(self, page, page_size, **filters):
            return {
                "accounts": [
                    {"id": 1, "name": "grouped", "status": "active", "group_ids": [2]},
                    {"id": 2, "name": "ungrouped", "status": "active", "group_ids": []},
                ],
                "total": 2,
                "pages": 1,
            }

    monkeypatch.setattr("app.main.sub2api_admin_client", lambda: AccountClient())

    with TestClient(app) as client:
        login(client)
        invalid = client.post("/api/platform-dispatch/sync", json={"include_ungrouped": "false"})
        started = client.post("/api/platform-dispatch/sync", json={"includeUngrouped": False})
        job = wait_for_dispatch_job(client)
        response = client.get("/api/platform-dispatch").json()

    assert invalid.status_code == 400
    assert started.status_code == 202
    assert job["status"] == "succeeded"
    assert [account["id"] for account in response["accounts"]] == [1]
    assert response["refresh_filter"]["include_ungrouped"] is False

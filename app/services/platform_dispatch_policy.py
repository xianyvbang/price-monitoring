from __future__ import annotations

import asyncio
import math
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from uuid import uuid4

from app.models import Database, utc_now
from app.services.sub2api_admin import (
    Sub2ApiAdminClient,
    Sub2ApiAdminError,
    normalize_sub2api_error_record,
    normalize_sub2api_usage_record,
    public_dispatch_account,
    public_dispatch_group,
)


POLICY_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "auto_scoring_enabled": True,
    "return_pool_enabled": False,
    "smart_expand_enabled": False,
    "load_factor_enabled": False,
    "price_protection_enabled": False,
    "probe_interval_seconds": 60,
    "health_threshold": 75.0,
    "evidence_ttl_multiplier": 3,
    "minimum_available_accounts": 1,
    "healthy_target_accounts": 3,
    "total_concurrency": 900,
    "account_min_concurrency": 20,
    "account_max_concurrency": 250,
    "expand_trigger_percent": 80.0,
    "expand_step_percent": 10.0,
    "load_factor_total": 400,
    "account_min_load_factor": 20,
    "account_max_load_factor": 500,
    "rate_weight_exponent": 1.0,
    "load_change_threshold_percent": 10.0,
    "load_change_cooldown_seconds": 60,
    "failure_window": 5,
    "failure_threshold": 3,
    "failure_health_threshold": 60.0,
    "slow_window": 10,
    "slow_first_token_ms": 15000,
    "slow_threshold": 5,
    "default_probe_model": "",
    "group_probe_models": {},
    "account_probe_models": {},
    "excluded_account_ids": [1430, 1431],
}

BOOL_FIELDS = {
    "enabled",
    "auto_scoring_enabled",
    "return_pool_enabled",
    "smart_expand_enabled",
    "load_factor_enabled",
    "price_protection_enabled",
}
INT_FIELDS = {
    "probe_interval_seconds",
    "evidence_ttl_multiplier",
    "minimum_available_accounts",
    "healthy_target_accounts",
    "total_concurrency",
    "account_min_concurrency",
    "account_max_concurrency",
    "load_factor_total",
    "account_min_load_factor",
    "account_max_load_factor",
    "load_change_cooldown_seconds",
    "failure_window",
    "failure_threshold",
    "slow_window",
    "slow_first_token_ms",
    "slow_threshold",
}
FLOAT_FIELDS = {
    "health_threshold",
    "expand_trigger_percent",
    "expand_step_percent",
    "rate_weight_exponent",
    "load_change_threshold_percent",
    "failure_health_threshold",
}

FATAL_BALANCE_MARKERS = (
    "insufficient balance",
    "insufficient credit",
    "balance exhausted",
    "余额不足",
    "余额耗尽",
)
FATAL_USAGE_MARKERS = (
    "usage limit",
    "usage_limit",
    "quota exceeded",
    "insufficient_quota",
    "用量上限",
    "额度耗尽",
    "配额耗尽",
)
TIMEOUT_MARKERS = ("timeout", "timed out", "deadline exceeded", "context deadline", "超时")


def validate_policy_config(payload: Any, current: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("策略配置格式不正确")
    config = dict(POLICY_DEFAULTS)
    if current:
        config.update(current)
    aliases = {
        "autoScoringEnabled": "auto_scoring_enabled",
        "returnPoolEnabled": "return_pool_enabled",
        "smartExpandEnabled": "smart_expand_enabled",
        "loadFactorEnabled": "load_factor_enabled",
        "priceProtectionEnabled": "price_protection_enabled",
        "defaultProbeModel": "default_probe_model",
        "groupProbeModels": "group_probe_models",
        "accountProbeModels": "account_probe_models",
        "excludedAccountIds": "excluded_account_ids",
    }
    normalized = {aliases.get(key, key): value for key, value in payload.items()}
    for key in BOOL_FIELDS:
        if key in normalized:
            if not isinstance(normalized[key], bool):
                raise ValueError(f"{key} 必须是布尔值")
            config[key] = normalized[key]
    for key in INT_FIELDS:
        if key in normalized:
            if isinstance(normalized[key], bool):
                raise ValueError(f"{key} 必须是整数")
            try:
                value = int(normalized[key])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{key} 必须是整数") from exc
            if value <= 0:
                raise ValueError(f"{key} 必须大于 0")
            config[key] = value
    for key in FLOAT_FIELDS:
        if key in normalized:
            if isinstance(normalized[key], bool):
                raise ValueError(f"{key} 必须是数字")
            try:
                value = float(normalized[key])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{key} 必须是数字") from exc
            config[key] = value
    if "excluded_account_ids" in normalized:
        raw_ids = normalized["excluded_account_ids"]
        if not isinstance(raw_ids, list):
            raise ValueError("excluded_account_ids 必须是数组")
        ids: list[int] = []
        for item in raw_ids:
            try:
                account_id = int(item)
            except (TypeError, ValueError) as exc:
                raise ValueError("排除账号 ID 必须是正整数") from exc
            if account_id <= 0:
                raise ValueError("排除账号 ID 必须是正整数")
            if account_id not in ids:
                ids.append(account_id)
        config["excluded_account_ids"] = ids
    if "default_probe_model" in normalized:
        config["default_probe_model"] = _validated_probe_model(
            normalized["default_probe_model"], "默认探活模型"
        )
    if "group_probe_models" in normalized:
        raw_models = normalized["group_probe_models"]
        if not isinstance(raw_models, dict):
            raise ValueError("group_probe_models 必须是对象")
        group_models: dict[str, str] = {}
        for raw_group_id, raw_model in raw_models.items():
            try:
                group_id = int(raw_group_id)
            except (TypeError, ValueError) as exc:
                raise ValueError("探活模型分组 ID 必须是正整数") from exc
            if group_id <= 0:
                raise ValueError("探活模型分组 ID 必须是正整数")
            model = _validated_probe_model(raw_model, f"分组 {group_id} 探活模型")
            if model:
                group_models[str(group_id)] = model
        config["group_probe_models"] = group_models
    if "account_probe_models" in normalized:
        raw_models = normalized["account_probe_models"]
        if not isinstance(raw_models, dict):
            raise ValueError("account_probe_models 必须是对象")
        account_models: dict[str, str] = {}
        for raw_account_id, raw_model in raw_models.items():
            try:
                account_id = int(raw_account_id)
            except (TypeError, ValueError) as exc:
                raise ValueError("探活模型账号 ID 必须是正整数") from exc
            if account_id <= 0:
                raise ValueError("探活模型账号 ID 必须是正整数")
            model = _validated_probe_model(raw_model, f"账号 {account_id} 探活模型")
            if model:
                account_models[str(account_id)] = model
        config["account_probe_models"] = account_models

    for key in ("health_threshold", "failure_health_threshold", "expand_trigger_percent", "expand_step_percent", "load_change_threshold_percent"):
        if not 0 <= float(config[key]) <= 100:
            raise ValueError(f"{key} 必须在 0 到 100 之间")
    if float(config["rate_weight_exponent"]) < 0:
        raise ValueError("rate_weight_exponent 不能小于 0")
    if config["account_min_concurrency"] > config["account_max_concurrency"]:
        raise ValueError("单账号并发下限不能大于上限")
    if config["account_min_load_factor"] > config["account_max_load_factor"]:
        raise ValueError("负载因子下限不能大于上限")
    if config["minimum_available_accounts"] > config["healthy_target_accounts"]:
        raise ValueError("可用池下限不能大于健康目标")
    if config["failure_threshold"] > config["failure_window"]:
        raise ValueError("异常次数不能大于异常窗口")
    if config["slow_threshold"] > config["slow_window"]:
        raise ValueError("慢首字次数不能大于慢首字窗口")
    if config["enabled"] and not config["auto_scoring_enabled"]:
        raise ValueError("开启自动调度时必须同时开启自动评分")
    return config


def _validated_probe_model(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} 必须是字符串")
    model = value.strip()
    if len(model) > 200:
        raise ValueError(f"{field_name}不能超过 200 个字符")
    if any(ord(character) < 32 for character in model):
        raise ValueError(f"{field_name}不能包含换行或控制字符")
    return model


def classify_activity(activity: dict[str, Any], *, probe: bool = False) -> dict[str, Any]:
    status_code = _optional_int(activity.get("status_code", activity.get("statusCode")))
    first_token_ms = _optional_float(activity.get("first_token_ms", activity.get("firstTokenMs")))
    message = str(activity.get("message") or "")
    lower = message.casefold()
    is_error = bool(activity.get("is_error", activity.get("isError", activity.get("kind") == "error")))
    is_timeout = bool(activity.get("is_timeout")) or any(marker in lower for marker in TIMEOUT_MARKERS)

    if not is_error:
        category = "slow" if first_token_ms is not None and first_token_ms > 15000 else "healthy"
        score = 65.0 if category == "slow" else 100.0
    elif status_code in {401, 403}:
        category, score = "fatal_auth", 0.0
    elif any(marker in lower for marker in FATAL_BALANCE_MARKERS):
        category, score = "fatal_balance", 0.0
    elif any(marker in lower for marker in FATAL_USAGE_MARKERS):
        category, score = "fatal_usage", 0.0
    elif is_timeout or probe:
        category, score = "probe_failure" if probe else "timeout", 10.0
    elif status_code in {429, 502, 503}:
        category, score = f"http_{status_code}", 25.0
    else:
        category, score = "upstream_error", 40.0
    return {
        "category": category,
        "score": score,
        "status_code": status_code,
        "first_token_ms": first_token_ms,
        "is_timeout": is_timeout,
        "message": message,
    }


def calculate_health(evidence: list[dict[str, Any]], now: datetime, ttl_seconds: int) -> dict[str, Any]:
    ordered = sorted(evidence, key=lambda item: _timestamp(item.get("occurred_at")), reverse=True)[:60]
    scores = [float(item.get("score") or 0) for item in ordered]
    short_values = scores[:10]
    if not short_values:
        short_score = long_score = health_score = None
    else:
        short_score = short_values[0]
        if len(short_values) > 1:
            short_score = short_values[0] * 0.5 + (sum(short_values[1:]) / len(short_values[1:])) * 0.5
        long_score = sum(scores) / len(scores)
        health_score = short_score * 0.7 + long_score * 0.3
    evidence_at = str(ordered[0].get("occurred_at") or "") if ordered else ""
    latest_at = _parse_datetime(evidence_at)
    fresh = latest_at is not None and latest_at >= now - timedelta(seconds=max(1, ttl_seconds))
    probe_success_at = ""
    for item in ordered:
        if item.get("is_probe_success"):
            probe_success_at = str(item.get("occurred_at") or "")
            break
    return {
        "health_score": health_score,
        "short_score": short_score,
        "long_score": long_score,
        "evidence_count": len(ordered),
        "evidence_at": evidence_at,
        "evidence_fresh": fresh,
        "latest_probe_success_at": probe_success_at,
        "evidence": ordered,
    }


def allocate_weighted_points(
    account_ids: list[int], weights: dict[int, float], total: int, minimum: int, maximum: int
) -> dict[int, int]:
    ids = sorted(set(int(value) for value in account_ids if int(value) > 0))
    if not ids:
        return {}
    minimum = max(0, int(minimum))
    maximum = max(minimum, int(maximum))
    effective_total = min(max(int(total), len(ids) * minimum), len(ids) * maximum)
    result = {account_id: minimum for account_id in ids}
    remaining = effective_total - sum(result.values())
    active = set(ids)
    while remaining > 0 and active:
        total_weight = sum(max(0.0, float(weights.get(account_id, 0))) for account_id in active)
        if total_weight <= 0:
            total_weight = float(len(active))
            normalized = {account_id: 1.0 for account_id in active}
        else:
            normalized = {account_id: max(0.0, float(weights.get(account_id, 0))) for account_id in active}
        quotas = {account_id: remaining * normalized[account_id] / total_weight for account_id in active}
        additions = {account_id: min(maximum - result[account_id], int(math.floor(quotas[account_id]))) for account_id in active}
        if not any(additions.values()):
            ranked = sorted(active, key=lambda account_id: (-(quotas[account_id] % 1), account_id))
            for account_id in ranked:
                if remaining <= 0:
                    break
                if result[account_id] < maximum:
                    result[account_id] += 1
                    remaining -= 1
            break
        for account_id, addition in additions.items():
            result[account_id] += addition
            remaining -= addition
        active = {account_id for account_id in active if result[account_id] < maximum}
    return result


def allocate_weighted_increments(
    current: dict[int, int], weights: dict[int, float], increment: int, maximum: int
) -> dict[int, int]:
    account_ids = [account_id for account_id, value in current.items() if int(value) < int(maximum)]
    if not account_ids or increment <= 0:
        return {}
    capacity = sum(max(0, int(maximum) - int(current[account_id])) for account_id in account_ids)
    target_total = min(int(increment), capacity)
    additions = {account_id: 0 for account_id in account_ids}
    remaining = target_total
    active = set(account_ids)
    while remaining > 0 and active:
        total_weight = sum(max(0.0, float(weights.get(account_id, 0))) for account_id in active)
        normalized = {
            account_id: max(0.0, float(weights.get(account_id, 0))) if total_weight > 0 else 1.0
            for account_id in active
        }
        denominator = total_weight if total_weight > 0 else float(len(active))
        quotas = {account_id: remaining * normalized[account_id] / denominator for account_id in active}
        distributed = 0
        for account_id in sorted(active):
            headroom = int(maximum) - int(current[account_id]) - additions[account_id]
            value = min(headroom, int(math.floor(quotas[account_id])))
            if value > 0:
                additions[account_id] += value
                distributed += value
        remaining -= distributed
        active = {
            account_id
            for account_id in active
            if int(current[account_id]) + additions[account_id] < int(maximum)
        }
        if distributed == 0 and active:
            for account_id in sorted(active, key=lambda value: (-(quotas[value] % 1), value)):
                if remaining <= 0:
                    break
                additions[account_id] += 1
                remaining -= 1
            active = {
                account_id
                for account_id in active
                if int(current[account_id]) + additions[account_id] < int(maximum)
            }
    return {account_id: value for account_id, value in additions.items() if value > 0}


class PlatformDispatchPolicyScheduler:
    def __init__(self, db: Database, client_factory: Callable[[], Sub2ApiAdminClient]) -> None:
        self.db = db
        self.client_factory = client_factory
        self.lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._automatic_run_task: asyncio.Task[dict[str, Any]] | None = None
        self._stopped = asyncio.Event()
        self._changed = asyncio.Event()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self.lock = asyncio.Lock()
            self._stopped = asyncio.Event()
            self._changed = asyncio.Event()
            self._automatic_run_task = None
            self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._stopped.set()
        self._changed.set()
        automatic_run_task = self._automatic_run_task
        automatic_run_was_running = bool(automatic_run_task and not automatic_run_task.done())
        if automatic_run_was_running and automatic_run_task:
            automatic_run_task.cancel()
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if automatic_run_was_running and automatic_run_task:
            with suppress(asyncio.CancelledError, Exception):
                await automatic_run_task
        self._automatic_run_task = None

    def notify_changed(self) -> None:
        self._changed.set()
        config = self.db.get_platform_dispatch_policy(POLICY_DEFAULTS)["config"]
        automatic_run_task = self._automatic_run_task
        if (
            not config.get("auto_scoring_enabled", True)
            and automatic_run_task
            and not automatic_run_task.done()
        ):
            automatic_run_task.cancel()

    async def _run_loop(self) -> None:
        while not self._stopped.is_set():
            self._changed.clear()
            automatic_run_task = asyncio.create_task(self.run_once(automatic=True))
            self._automatic_run_task = automatic_run_task
            try:
                await automatic_run_task
            except asyncio.CancelledError:
                if self._stopped.is_set():
                    raise
            except Exception as exc:
                self.db.add_log("error", "platform-dispatch-policy", f"自动调度循环失败: {exc}")
            finally:
                if self._automatic_run_task is automatic_run_task:
                    self._automatic_run_task = None
            policy = self.db.get_platform_dispatch_policy(POLICY_DEFAULTS)["config"]
            timeout = max(5, int(policy.get("probe_interval_seconds") or 60))
            try:
                await asyncio.wait_for(self._changed.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass

    async def run_once(self, *, automatic: bool = False) -> dict[str, Any]:
        if automatic:
            config = self.db.get_platform_dispatch_policy(POLICY_DEFAULTS)["config"]
            if not config.get("auto_scoring_enabled", True):
                return {"skipped": True, "message": "自动评分已关闭"}
        if self.db.has_active_platform_dispatch_job():
            return {"skipped": True, "message": "平台调度同步任务正在执行"}
        if self.lock.locked():
            raise Sub2ApiAdminError("平台调度策略正在执行", status_code=409)
        async with self.lock:
            try:
                return await self._run_once_locked()
            except asyncio.CancelledError:
                if automatic:
                    self.db.update_platform_dispatch_policy_runtime(
                        POLICY_DEFAULTS,
                        status="idle",
                        last_finished_at=utc_now(),
                        last_error="",
                        summary={"message": "自动评分已关闭"},
                    )
                raise

    async def refresh_health_evidence(
        self,
        client: Sub2ApiAdminClient,
        cache: dict[str, Any],
        progress: Callable[[str, int, int], None] | None = None,
    ) -> dict[str, Any]:
        site_url = client.site_url
        if cache.get("source_site_url") != site_url:
            raise Sub2ApiAdminError("平台调度缓存与当前 Sub2API 站点不一致", status_code=409)
        accounts = {
            int(account["id"]): account
            for account in cache.get("accounts") or []
            if isinstance(account, dict) and _optional_int(account.get("id"))
        }
        if not accounts:
            raise Sub2ApiAdminError("请先同步账号信息", status_code=409)

        config = validate_policy_config({}, self.db.get_platform_dispatch_policy(POLICY_DEFAULTS)["config"])
        ttl_seconds = int(config["probe_interval_seconds"]) * int(config["evidence_ttl_multiplier"])
        warnings = await self._refresh_evidence(
            client,
            site_url,
            accounts,
            force_full=True,
            progress=progress,
        )
        now = datetime.now(timezone.utc)
        health = {
            account_id: calculate_health(
                self.db.list_platform_dispatch_evidence(site_url, account_id), now, ttl_seconds
            )
            for account_id in accounts
        }
        await self._probe_due_accounts(
            client,
            site_url,
            accounts,
            health,
            config,
            ttl_seconds,
            force=True,
            progress=progress,
        )

        now = datetime.now(timezone.utc)
        evidence_total = 0
        for account_id, account in accounts.items():
            item = calculate_health(
                self.db.list_platform_dispatch_evidence(site_url, account_id), now, ttl_seconds
            )
            evidence_total += int(item["evidence_count"])
            public = public_dispatch_account(account, _cached_activity(cache, account_id))
            public.update(
                {
                    "health_score": _round_score(item["health_score"]),
                    "health_short_score": _round_score(item["short_score"]),
                    "health_long_score": _round_score(item["long_score"]),
                    "health_evidence_count": item["evidence_count"],
                    "health_evidence_at": item["evidence_at"] or None,
                    "health_evidence_fresh": bool(item["evidence_fresh"]),
                }
            )
            self.db.update_platform_dispatch_cached_account(public)
            self.db.upsert_platform_dispatch_account_state(
                site_url,
                account_id,
                name=str(account.get("name") or ""),
                health_score=item["health_score"],
                short_score=item["short_score"],
                long_score=item["long_score"],
                evidence_count=item["evidence_count"],
                evidence_at=item["evidence_at"] or None,
                evidence_fresh=1 if item["evidence_fresh"] else 0,
                latest_probe_success_at=item["latest_probe_success_at"] or None,
            )
        return {
            "refreshed_accounts": len(accounts),
            "evidence_count": evidence_total,
            "probe_count": len(accounts),
            "warnings": list(dict.fromkeys(warnings)),
        }

    async def _run_once_locked(self) -> dict[str, Any]:
        started_at = utc_now()
        policy_record = self.db.get_platform_dispatch_policy(POLICY_DEFAULTS)
        config = validate_policy_config({}, policy_record["config"])
        cache = self.db.get_platform_dispatch_cache()
        if not cache or not cache.get("accounts"):
            summary = {"managed_accounts": 0, "available_accounts": 0, "message": "请先同步账号"}
            self.db.update_platform_dispatch_policy_runtime(
                POLICY_DEFAULTS, status="idle", last_started_at=started_at,
                last_finished_at=utc_now(), last_error="", summary=summary,
            )
            return summary
        client = self.client_factory()
        site_url = client.site_url
        if cache.get("source_site_url") != site_url:
            raise Sub2ApiAdminError("平台调度缓存与当前 Sub2API 站点不一致", status_code=409)
        self.db.update_platform_dispatch_policy_runtime(
            POLICY_DEFAULTS, source_site_url=site_url, status="running", last_started_at=started_at, last_error="",
        )
        try:
            summary = await self._evaluate(client, cache, config)
        except Exception as exc:
            self.db.update_platform_dispatch_policy_runtime(
                POLICY_DEFAULTS, source_site_url=site_url, status="failed",
                last_finished_at=utc_now(), last_error=str(exc),
            )
            raise
        self.db.update_platform_dispatch_policy_runtime(
            POLICY_DEFAULTS, source_site_url=site_url, status="succeeded",
            last_finished_at=utc_now(), last_error="", summary=summary,
        )
        return summary

    async def _evaluate(
        self, client: Sub2ApiAdminClient, cache: dict[str, Any], config: dict[str, Any]
    ) -> dict[str, Any]:
        site_url = client.site_url
        excluded_ids = {int(value) for value in config["excluded_account_ids"]}
        managed_ids = {
            int(account["id"])
            for account in cache.get("accounts") or []
            if isinstance(account, dict) and _optional_int(account.get("id")) and int(account["id"]) not in excluded_ids
        }
        refresh_filter = cache.get("refresh_filter") or {}
        platform = str(refresh_filter.get("platform") or "")
        account_type = str(refresh_filter.get("type") or "")
        remote_accounts, groups = await asyncio.gather(
            client.list_accounts(platform=platform or None, account_type=account_type or None),
            client.list_groups(platform=platform or None),
        )
        accounts = {
            int(account["id"]): account
            for account in remote_accounts
            if isinstance(account, dict) and _optional_int(account.get("id")) in managed_ids
        }
        managed_ids.intersection_update(accounts)
        group_map = {int(group["id"]): group for group in groups if isinstance(group, dict) and _optional_int(group.get("id"))}

        warnings = await self._refresh_evidence(client, site_url, accounts)
        now = datetime.now(timezone.utc)
        ttl_seconds = int(config["probe_interval_seconds"]) * int(config["evidence_ttl_multiplier"])
        health = {
            account_id: calculate_health(
                self.db.list_platform_dispatch_evidence(site_url, account_id), now, ttl_seconds
            )
            for account_id in managed_ids
        }

        if config["enabled"]:
            await self._probe_due_accounts(client, site_url, accounts, health, config, ttl_seconds)
            health = {
                account_id: calculate_health(
                    self.db.list_platform_dispatch_evidence(site_url, account_id), now, ttl_seconds
                )
                for account_id in managed_ids
            }

        concurrency_data: dict[str, Any] | None = None
        availability_data: dict[str, Any] | None = None
        if config["enabled"]:
            realtime = await asyncio.gather(
                client.get_concurrency_stats(platform or None),
                client.get_account_availability(platform or None),
                return_exceptions=True,
            )
            if isinstance(realtime[0], Exception):
                warnings.append(f"实时并发不可用: {realtime[0]}")
            else:
                concurrency_data = realtime[0]
            if isinstance(realtime[1], Exception):
                warnings.append(f"账号可用性不可用: {realtime[1]}")
            else:
                availability_data = realtime[1]

        concurrency_by_id = _keyed_account_map((concurrency_data or {}).get("account"))
        availability_by_id = _keyed_account_map((availability_data or {}).get("account"))
        states = {item["account_id"]: item for item in self.db.list_platform_dispatch_account_states(site_url)}
        public_groups = [public_dispatch_group(group) for group in groups]
        group_rates = {
            int(group["id"]): _optional_float(group.get("rate_multiplier"))
            for group in groups
            if isinstance(group, dict) and _optional_int(group.get("id"))
        }

        available_ids: list[int] = []
        for account_id, account in accounts.items():
            item = health[account_id]
            state = states.get(account_id) or {}
            runtime_available = _runtime_available(account, availability_by_id.get(account_id))
            if runtime_available and item["evidence_fresh"] and (item["health_score"] or 0) >= config["health_threshold"]:
                available_ids.append(account_id)
            current_concurrency = concurrency_by_id.get(account_id) or {}
            public = public_dispatch_account(account, _cached_activity(cache, account_id))
            public.update(
                {
                    "current_concurrency": _optional_int(current_concurrency.get("current_in_use")) or 0,
                    "waiting_in_queue": _optional_int(current_concurrency.get("waiting_in_queue")) or 0,
                    "health_score": _round_score(item["health_score"]),
                    "health_short_score": _round_score(item["short_score"]),
                    "health_long_score": _round_score(item["long_score"]),
                    "health_evidence_count": item["evidence_count"],
                    "health_evidence_at": item["evidence_at"] or None,
                    "health_evidence_fresh": bool(item["evidence_fresh"]),
                    "decision_reason": str(state.get("decision_reason") or ""),
                    "target_concurrency": state.get("target_concurrency"),
                    "target_load_factor": state.get("target_load_factor"),
                    "last_policy_action_at": state.get("last_action_at"),
                }
            )
            self.db.update_platform_dispatch_cached_account(public)
            self.db.upsert_platform_dispatch_account_state(
                site_url,
                account_id,
                name=str(account.get("name") or ""),
                health_score=item["health_score"],
                short_score=item["short_score"],
                long_score=item["long_score"],
                evidence_count=item["evidence_count"],
                evidence_at=item["evidence_at"] or None,
                evidence_fresh=1 if item["evidence_fresh"] else 0,
                latest_probe_success_at=item["latest_probe_success_at"] or None,
            )

        status_action = ""
        if config["enabled"]:
            status_action = await self._apply_status_policy(
                client, site_url, accounts, health, available_ids, config, ttl_seconds
            )
            if config["smart_expand_enabled"] and concurrency_data is not None:
                await self._apply_concurrency_policy(
                    client, site_url, accounts, health, concurrency_by_id, group_rates, config
                )
            if config["load_factor_enabled"] or config["price_protection_enabled"]:
                await self._apply_load_policy(client, site_url, accounts, health, group_rates, config)

        total_current = sum(_optional_int(item.get("current_in_use")) or 0 for item in concurrency_by_id.values())
        total_capacity = sum(max(0, _optional_int(account.get("concurrency")) or 0) for account in accounts.values())
        summary = {
            "managed_accounts": len(accounts),
            "available_accounts": len(available_ids),
            "healthy_target_accounts": int(config["healthy_target_accounts"]),
            "minimum_available_accounts": int(config["minimum_available_accounts"]),
            "current_concurrency": total_current,
            "capacity": total_capacity,
            "configured_concurrency": int(config["total_concurrency"]),
            "status_action": status_action,
            "warnings": list(dict.fromkeys(warnings)),
            "groups": len(public_groups),
        }
        return summary

    async def _refresh_evidence(
        self,
        client: Sub2ApiAdminClient,
        site_url: str,
        accounts: dict[int, dict[str, Any]],
        *,
        force_full: bool = False,
        progress: Callable[[str, int, int], None] | None = None,
    ) -> list[str]:
        semaphore = asyncio.Semaphore(8)
        warnings: list[str] = []
        completed = 0

        async def load_source(
            account_id: int, source_kind: str
        ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
            cursor = None if force_full else self.db.get_platform_dispatch_cursor(site_url, account_id, source_kind)
            cursor_id = str((cursor or {}).get("latest_source_id") or "")
            records: list[dict[str, Any]] = []
            page = 1
            newest: dict[str, Any] | None = None
            cutoff = datetime.now(timezone.utc) - timedelta(days=30)
            while page <= 100:
                if source_kind == "usage" and hasattr(client, "list_usage_page"):
                    page_data = await client.list_usage_page(
                        account_id,
                        page=page,
                        page_size=100,
                        start_date=cutoff.date().isoformat(),
                    )
                elif source_kind == "error" and hasattr(client, "list_errors_page"):
                    page_data = await client.list_errors_page(
                        account_id,
                        page=page,
                        page_size=100,
                        time_range="30d",
                    )
                else:
                    fallback = (
                        await client.list_recent_usage(account_id, 60)
                        if source_kind == "usage"
                        else await client.list_recent_errors(account_id, 60)
                    )
                    page_data = {"records": fallback, "pages": 1}
                page_records = [item for item in page_data.get("records") or [] if isinstance(item, dict)]
                if page == 1 and page_records:
                    newest = page_records[0]
                reached_cursor = False
                for record in page_records:
                    source_id = str(record.get("id") or "")
                    occurred_at = _parse_datetime(record.get("created_at"))
                    if cursor_id and source_id == cursor_id:
                        reached_cursor = True
                        break
                    if occurred_at is not None and occurred_at < cutoff:
                        reached_cursor = True
                        break
                    records.append(record)
                    if not cursor and len(records) >= 60:
                        reached_cursor = True
                        break
                pages = _optional_int(page_data.get("pages"))
                if reached_cursor or not page_records or (pages is not None and page >= pages):
                    break
                if pages is None and len(page_records) < 100:
                    break
                page += 1
            return records, newest

        async def load(account_id: int) -> None:
            nonlocal completed
            async with semaphore:
                usage, errors = await asyncio.gather(
                    load_source(account_id, "usage"),
                    load_source(account_id, "error"),
                    return_exceptions=True,
                )
            name = str(accounts[account_id].get("name") or account_id)
            if isinstance(usage, Exception):
                warnings.append(f"账号 {name} 使用记录读取失败: {usage}")
            else:
                usage_records, usage_newest = usage
                usage_evidence: list[dict[str, Any]] = []
                for raw in usage_records:
                    activity = normalize_sub2api_usage_record(raw)
                    classified = classify_activity(activity)
                    item = {
                        "account_id": account_id,
                        "source_kind": "usage",
                        "source_id": str(activity.get("source_id")),
                        "occurred_at": activity.get("created_at") or utc_now(),
                        **classified,
                    }
                    usage_evidence.append(item)
                    if not force_full:
                        self.db.add_platform_dispatch_evidence(site_url, item)
                if force_full:
                    self.db.replace_platform_dispatch_evidence_source(
                        site_url, account_id, "usage", usage_evidence
                    )
                if usage_newest is not None:
                    self.db.save_platform_dispatch_cursor(
                        site_url,
                        account_id,
                        "usage",
                        str(usage_newest.get("id") or ""),
                        str(usage_newest.get("created_at") or ""),
                    )
            if isinstance(errors, Exception):
                warnings.append(f"账号 {name} 错误记录读取失败: {errors}")
            else:
                error_records, error_newest = errors
                error_evidence: list[dict[str, Any]] = []
                for raw in error_records:
                    activity = normalize_sub2api_error_record(raw)
                    classified = classify_activity(activity)
                    item = {
                        "account_id": account_id,
                        "source_kind": "error",
                        "source_id": str(activity.get("source_id")),
                        "occurred_at": activity.get("created_at") or utc_now(),
                        **classified,
                    }
                    error_evidence.append(item)
                    if not force_full:
                        self.db.add_platform_dispatch_evidence(site_url, item)
                if force_full:
                    self.db.replace_platform_dispatch_evidence_source(
                        site_url, account_id, "error", error_evidence
                    )
                if error_newest is not None:
                    self.db.save_platform_dispatch_cursor(
                        site_url,
                        account_id,
                        "error",
                        str(error_newest.get("id") or ""),
                        str(error_newest.get("created_at") or ""),
                    )
            completed += 1
            if progress:
                progress("evidence", completed, len(accounts))

        await asyncio.gather(*(load(account_id) for account_id in accounts))
        return warnings

    async def _probe_due_accounts(
        self,
        client: Sub2ApiAdminClient,
        site_url: str,
        accounts: dict[int, dict[str, Any]],
        health: dict[int, dict[str, Any]],
        config: dict[str, Any],
        ttl_seconds: int,
        *,
        force: bool = False,
        progress: Callable[[str, int, int], None] | None = None,
    ) -> None:
        semaphore = asyncio.Semaphore(8)
        now = datetime.now(timezone.utc)
        completed = 0

        async def probe(account_id: int) -> None:
            nonlocal completed
            account_models = config.get("account_probe_models") or {}
            account_model = str(account_models.get(str(account_id), account_models.get(account_id, "")) or "").strip()
            group_models = config.get("group_probe_models") or {}
            group_model = ""
            if not account_model:
                group_ids = public_dispatch_account(accounts[account_id], []).get("group_ids") or []
                for group_id in group_ids:
                    group_model = str(
                        group_models.get(str(group_id), group_models.get(group_id, "")) or ""
                    ).strip()
                    if group_model:
                        break
            default_model = str(config.get("default_probe_model") or "").strip()
            probe_model = account_model or group_model or default_model
            try:
                async with semaphore:
                    result = await client.probe_account(account_id, model=probe_model or None)
            except Exception as exc:
                result = {"success": False, "is_timeout": False, "message": f"账号探活失败: {exc}"}
            activity = {
                "kind": "success" if result.get("success") else "error",
                "is_error": not result.get("success"),
                "status_code": result.get("status_code"),
                "is_timeout": result.get("is_timeout"),
                "message": result.get("message") or "",
            }
            classified = classify_activity(activity, probe=not result.get("success"))
            occurred_at = utc_now()
            self.db.add_platform_dispatch_evidence(
                site_url,
                {
                    "account_id": account_id,
                    "source_kind": "probe",
                    "source_id": uuid4().hex,
                    "occurred_at": occurred_at,
                    "is_probe_success": bool(result.get("success")),
                    **classified,
                },
            )
            completed += 1
            if progress:
                progress("probe", completed, len(due))

        due: list[int] = []
        for account_id, account in accounts.items():
            item = health[account_id]
            latest = _parse_datetime(item.get("evidence_at"))
            inactive = str(account.get("status") or "inactive") != "active"
            if force or inactive or latest is None or latest < now - timedelta(seconds=max(1, ttl_seconds // 3)):
                due.append(account_id)
        await asyncio.gather(*(probe(account_id) for account_id in due))

    async def _apply_status_policy(
        self,
        client: Sub2ApiAdminClient,
        site_url: str,
        accounts: dict[int, dict[str, Any]],
        health: dict[int, dict[str, Any]],
        available_ids: list[int],
        config: dict[str, Any],
        ttl_seconds: int,
    ) -> str:
        fatal: list[tuple[int, str]] = []
        threshold: list[tuple[int, str]] = []
        for account_id, account in accounts.items():
            if str(account.get("status") or "inactive") not in {"active", "error"}:
                continue
            item = health[account_id]
            evidence = item["evidence"]
            latest_category = str(evidence[0].get("category") or "") if evidence else ""
            if latest_category.startswith("fatal_"):
                fatal.append((account_id, f"致命异常: {latest_category}"))
                continue
            recent_failures = evidence[: int(config["failure_window"])]
            failure_count = sum(
                1
                for event in recent_failures
                if event.get("is_timeout") or _optional_int(event.get("status_code")) in {429, 502, 503}
            )
            recent_slow = evidence[: int(config["slow_window"])]
            slow_count = sum(
                1
                for event in recent_slow
                if (_optional_float(event.get("first_token_ms")) or 0) > float(config["slow_first_token_ms"])
            )
            if failure_count >= int(config["failure_threshold"]) and (item["health_score"] or 0) < float(config["failure_health_threshold"]):
                threshold.append((account_id, f"最近 {config['failure_window']} 次异常达到 {failure_count} 次"))
            elif slow_count >= int(config["slow_threshold"]):
                threshold.append((account_id, f"最近 {config['slow_window']} 次慢首字达到 {slow_count} 次"))

        candidate: tuple[int, bool, str] | None = None
        if fatal:
            account_id, reason = min(fatal, key=lambda pair: ((health[pair[0]]["health_score"] or 0), pair[0]))
            candidate = (account_id, False, reason)
        elif threshold:
            account_id, reason = min(threshold, key=lambda pair: ((health[pair[0]]["health_score"] or 0), pair[0]))
            candidate = (account_id, False, reason)
        else:
            target = int(config["minimum_available_accounts"])
            reason = "可用池低于保护下限"
            if config["return_pool_enabled"] and len(available_ids) >= target:
                target = int(config["healthy_target_accounts"])
                reason = "可用池低于健康目标"
            if len(available_ids) < target:
                now = datetime.now(timezone.utc)
                recovery: list[int] = []
                for account_id, account in accounts.items():
                    item = health[account_id]
                    probe_at = _parse_datetime(item.get("latest_probe_success_at"))
                    if (
                        str(account.get("status") or "inactive") == "inactive"
                        and item["evidence_fresh"]
                        and (item["health_score"] or 0) >= float(config["health_threshold"])
                        and probe_at is not None
                        and probe_at >= now - timedelta(seconds=ttl_seconds)
                        and account.get("schedulable") is not False
                    ):
                        recovery.append(account_id)
                if recovery:
                    account_id = max(recovery, key=lambda value: ((health[value]["health_score"] or 0), -value))
                    candidate = (account_id, True, reason)
        if candidate is None:
            return ""
        account_id, enabled, reason = candidate
        account = accounts[account_id]
        try:
            updated = await client.update_account_status(account_id, enabled)
        except Exception as exc:
            self._record_action(site_url, account, "enable" if enabled else "disable", "status", account.get("status"), "active" if enabled else "inactive", reason, exc)
            return ""
        account["status"] = "active" if enabled else "inactive"
        self.db.update_platform_dispatch_cached_account(updated)
        self._record_action(site_url, account, "enable" if enabled else "disable", "status", "inactive" if enabled else "active", account["status"], reason)
        return f"{'启用' if enabled else '停用'} {account.get('name') or account_id}: {reason}"

    async def _apply_concurrency_policy(
        self,
        client: Sub2ApiAdminClient,
        site_url: str,
        accounts: dict[int, dict[str, Any]],
        health: dict[int, dict[str, Any]],
        realtime: dict[int, dict[str, Any]],
        group_rates: dict[int, float | None],
        config: dict[str, Any],
    ) -> None:
        eligible = [
            account_id
            for account_id, account in accounts.items()
            if str(account.get("status") or "") == "active"
            and health[account_id]["evidence_fresh"]
            and (health[account_id]["health_score"] or 0) >= float(config["health_threshold"])
        ]
        if not eligible:
            return
        minimum = int(config["account_min_concurrency"])
        maximum = int(config["account_max_concurrency"])
        targets = {account_id: max(0, _optional_int(accounts[account_id].get("concurrency")) or 0) for account_id in eligible}
        for account_id in eligible:
            if targets[account_id] < minimum:
                targets[account_id] = minimum
        total_capacity = sum(targets.values())
        total_in_use = sum(_optional_int((realtime.get(account_id) or {}).get("current_in_use")) or 0 for account_id in eligible)
        queue = sum(_optional_int((realtime.get(account_id) or {}).get("waiting_in_queue")) or 0 for account_id in eligible)
        load_percent = (total_in_use * 100 / total_capacity) if total_capacity > 0 else 0
        remaining = max(0, int(config["total_concurrency"]) - total_capacity)
        if remaining and (load_percent >= float(config["expand_trigger_percent"]) or queue > 0):
            increment = min(remaining, max(1, math.ceil(total_capacity * float(config["expand_step_percent"]) / 100)))
            expandable = [account_id for account_id in eligible if targets[account_id] < maximum]
            weights = {account_id: self._account_weight(accounts[account_id], health[account_id], config) for account_id in expandable}
            additions = allocate_weighted_increments(targets, weights, increment, maximum)
            for account_id, addition in additions.items():
                targets[account_id] = min(maximum, targets[account_id] + addition)
        for account_id in eligible:
            current = max(0, _optional_int(accounts[account_id].get("concurrency")) or 0)
            target = targets[account_id]
            self.db.upsert_platform_dispatch_account_state(site_url, account_id, target_concurrency=target)
            if target <= current:
                continue
            await self._write_account_field(client, site_url, accounts[account_id], "concurrency", current, target, "智能扩容")

    async def _apply_load_policy(
        self,
        client: Sub2ApiAdminClient,
        site_url: str,
        accounts: dict[int, dict[str, Any]],
        health: dict[int, dict[str, Any]],
        group_rates: dict[int, float | None],
        config: dict[str, Any],
    ) -> None:
        eligible = [
            account_id
            for account_id, account in accounts.items()
            if str(account.get("status") or "") == "active"
            and health[account_id]["evidence_fresh"]
            and (health[account_id]["health_score"] or 0) >= float(config["health_threshold"])
        ]
        if not eligible:
            return
        states = {item["account_id"]: item for item in self.db.list_platform_dispatch_account_states(site_url)}
        weights = {account_id: self._account_weight(accounts[account_id], health[account_id], config) for account_id in eligible}
        if config["load_factor_enabled"]:
            base_targets = allocate_weighted_points(
                eligible,
                weights,
                int(config["load_factor_total"]),
                int(config["account_min_load_factor"]),
                int(config["account_max_load_factor"]),
            )
        else:
            base_targets = {}
            for account_id in eligible:
                account = accounts[account_id]
                current = _effective_load_factor(account)
                baseline = _optional_int((states.get(account_id) or {}).get("baseline_load_factor")) or current
                base_targets[account_id] = baseline
                self.db.upsert_platform_dispatch_account_state(site_url, account_id, baseline_load_factor=baseline)

        now = datetime.now(timezone.utc)
        for account_id in eligible:
            account = accounts[account_id]
            target = base_targets[account_id]
            if config["price_protection_enabled"]:
                upstream_rate = _optional_float(account.get("rate_multiplier"))
                local_rates = [group_rates.get(group_id) for group_id in _account_group_ids(account)]
                local_rates = [rate for rate in local_rates if rate is not None and rate > 0]
                if upstream_rate is not None and upstream_rate > 0 and local_rates:
                    target = max(1, int(round(target * min(1.0, min(local_rates) / upstream_rate))))
            current = _effective_load_factor(account)
            self.db.upsert_platform_dispatch_account_state(site_url, account_id, target_load_factor=target)
            relative = abs(target - current) * 100 / max(1, current)
            state = states.get(account_id) or {}
            last_write = _parse_datetime(state.get("last_load_factor_write_at"))
            cooling = last_write is not None and last_write > now - timedelta(seconds=int(config["load_change_cooldown_seconds"]))
            if relative < float(config["load_change_threshold_percent"]) or cooling:
                continue
            if await self._write_account_field(client, site_url, account, "load_factor", current, target, "负载因子调权"):
                self.db.upsert_platform_dispatch_account_state(site_url, account_id, last_load_factor_write_at=utc_now())

    def _account_weight(self, account: dict[str, Any], health: dict[str, Any], config: dict[str, Any]) -> float:
        score = max(0.0, float(health.get("health_score") or 0))
        rate = _optional_float(account.get("rate_multiplier"))
        rate = rate if rate is not None and rate > 0 else 1.0
        return score * (rate ** float(config["rate_weight_exponent"]))

    async def _write_account_field(
        self,
        client: Sub2ApiAdminClient,
        site_url: str,
        account: dict[str, Any],
        field: str,
        old_value: Any,
        new_value: Any,
        reason: str,
    ) -> bool:
        try:
            updated = await client.update_account_fields(int(account["id"]), {field: new_value})
        except Exception as exc:
            self._record_action(site_url, account, "adjust", field, old_value, new_value, reason, exc)
            return False
        account[field] = new_value
        self.db.update_platform_dispatch_cached_account(updated)
        self._record_action(site_url, account, "adjust", field, old_value, new_value, reason)
        return True

    def _record_action(
        self,
        site_url: str,
        account: dict[str, Any],
        action: str,
        field: str,
        old_value: Any,
        new_value: Any,
        reason: str,
        error: Exception | None = None,
    ) -> None:
        self.db.add_platform_dispatch_action(
            site_url,
            account_id=_optional_int(account.get("id")),
            account_name=str(account.get("name") or ""),
            action=action,
            field=field,
            old_value=old_value,
            new_value=new_value,
            reason=reason,
            status="failed" if error else "succeeded",
            error=str(error or ""),
        )
        if not error:
            self.db.upsert_platform_dispatch_account_state(
                site_url,
                int(account["id"]),
                decision_reason=reason,
                last_action_at=utc_now(),
            )


def _cached_activity(cache: dict[str, Any], account_id: int) -> list[dict[str, Any]]:
    for account in cache.get("accounts") or []:
        if isinstance(account, dict) and _optional_int(account.get("id")) == account_id:
            value = account.get("recent_activity", account.get("recentActivity", []))
            return value if isinstance(value, list) else []
    return []


def _keyed_account_map(value: Any) -> dict[int, dict[str, Any]]:
    if not isinstance(value, dict):
        return {}
    result: dict[int, dict[str, Any]] = {}
    for key, item in value.items():
        if not isinstance(item, dict):
            continue
        account_id = _optional_int(item.get("account_id", key))
        if account_id:
            result[account_id] = item
    return result


def _runtime_available(account: dict[str, Any], availability: dict[str, Any] | None) -> bool:
    if availability is not None and "is_available" in availability:
        return bool(availability.get("is_available"))
    if str(account.get("status") or "inactive") != "active" or account.get("schedulable") is False:
        return False
    now = datetime.now(timezone.utc)
    for key in ("rate_limit_reset_at", "overload_until", "temp_unschedulable_until"):
        value = _parse_datetime(account.get(key))
        if value is not None and value > now:
            return False
    return True


def _effective_load_factor(account: dict[str, Any]) -> int:
    value = _optional_int(account.get("load_factor"))
    if value and value > 0:
        return value
    return max(1, _optional_int(account.get("concurrency")) or 1)


def _account_group_ids(account: dict[str, Any]) -> list[int]:
    values = account.get("group_ids", [])
    if not isinstance(values, list):
        values = [values]
    result: list[int] = []
    for value in values:
        parsed = _optional_int(value)
        if parsed and parsed not in result:
            result.append(parsed)
    return result


def _optional_int(value: Any) -> int | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _timestamp(value: Any) -> float:
    parsed = _parse_datetime(value)
    return parsed.timestamp() if parsed else 0.0


def _round_score(value: Any) -> float | None:
    parsed = _optional_float(value)
    return round(parsed, 1) if parsed is not None else None

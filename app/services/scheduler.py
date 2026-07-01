from __future__ import annotations

import asyncio
import json
from contextlib import suppress

from app.models import DEFAULT_BALANCE_UNIT, Database, actual_consumption_stats, monitor_group_to_dict
from app.models import utc_now
from app.services.alerts import handle_alert_state
from app.services.balance import query_account, query_newapi_group, query_sub2api_group
from app.services.cpa_admin import CpaAdminError, cpa_admin_client_from_db
from app.services.emailer import build_group_rate_change_email, build_reminder_email, send_email
from app.services.opencode_go import refresh_opencode_go_account

OPENCODE_GO_CPA_USAGE_THRESHOLD = 99.0


class BalanceScheduler:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._task: asyncio.Task[None] | None = None
        self._group_rate_task: asyncio.Task[None] | None = None
        self._reminder_task: asyncio.Task[None] | None = None
        self._opencode_go_task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()
        self._settings_changed = asyncio.Event()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopped.clear()
            self._task = asyncio.create_task(self._run())
        if self._group_rate_task is None or self._group_rate_task.done():
            self._stopped.clear()
            self._group_rate_task = asyncio.create_task(self._run_group_rate_loop())
        if self._reminder_task is None or self._reminder_task.done():
            self._stopped.clear()
            self._reminder_task = asyncio.create_task(self._run_reminder_loop())
        if self._opencode_go_task is None or self._opencode_go_task.done():
            self._stopped.clear()
            self._opencode_go_task = asyncio.create_task(self._run_opencode_go_loop())

    async def stop(self) -> None:
        self._stopped.set()
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        if self._group_rate_task:
            self._group_rate_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._group_rate_task
        if self._reminder_task:
            self._reminder_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._reminder_task
        if self._opencode_go_task:
            self._opencode_go_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._opencode_go_task

    async def _run(self) -> None:
        while not self._stopped.is_set():
            settings = self.db.get_general_settings()
            if settings["monitor_paused"]:
                await self._wait_for_resume()
                continue
            await query_all_accounts(self.db)
            await self._wait(settings["query_interval"])

    async def _run_group_rate_loop(self) -> None:
        while not self._stopped.is_set():
            settings = self.db.get_general_settings()
            if settings["monitor_paused"]:
                await self._wait_for_resume()
                continue
            await query_all_group_rates(self.db, notify=True)
            await self._wait(settings["group_rate_query_interval"])

    async def _run_reminder_loop(self) -> None:
        while not self._stopped.is_set():
            await send_due_reminders(self.db)
            await self._wait(30)

    async def _run_opencode_go_loop(self) -> None:
        while not self._stopped.is_set():
            settings = self.db.get_general_settings()
            if settings["monitor_paused"]:
                await self._wait_for_resume()
                continue
            await query_all_opencode_go_accounts(self.db)
            await self._wait(settings["query_interval"])

    def notify_settings_changed(self) -> None:
        self._settings_changed.set()

    async def _wait_for_resume(self) -> None:
        while not self._stopped.is_set() and self.db.get_general_settings()["monitor_paused"]:
            await self._wait(None)

    async def _wait(self, timeout: float | None) -> None:
        stopped_wait = asyncio.create_task(self._stopped.wait())
        settings_wait = asyncio.create_task(self._settings_changed.wait())
        waits = [stopped_wait, settings_wait]
        try:
            done, pending = await asyncio.wait(waits, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in pending:
                with suppress(asyncio.CancelledError):
                    await task
            for task in done:
                task.result()
            if settings_wait in done:
                self._settings_changed.clear()
        finally:
            for task in waits:
                if not task.done():
                    task.cancel()


async def send_due_reminders(db: Database, now: str | None = None) -> list[dict]:
    results = []
    attempted_at = now or utc_now()
    for reminder in db.list_due_reminders(now=now):
        try:
            subject, body = build_reminder_email(reminder)
            send_email(db.get_smtp_settings(), db.secret_key, subject, body)
            db.mark_reminder_sent(reminder["id"], attempted_at)
            db.add_log("info", "reminder", f"定时提醒已发送: {reminder['title']}")
            results.append({"id": reminder["id"], "sent": True})
        except Exception as exc:
            db.mark_reminder_failed(reminder["id"], str(exc), attempted_at)
            db.add_log("error", "reminder", f"定时提醒发送失败: {reminder['title']}: {exc}")
            results.append({"id": reminder["id"], "sent": False, "error": str(exc)})
    return results


async def query_one_account(db: Database, account_id: int) -> dict:
    account = db.get_account(account_id)
    if not account:
        db.add_log("error", "query", f"查询失败，账号不存在: {account_id}")
        return {"is_valid": False, "invalid_message": "账号不存在"}
    settings = db.get_general_settings()
    result = await query_account(account, db.secret_key, settings["request_timeout"], db.add_log)
    result["account_id"] = account_id
    result["checked_at"] = utc_now()
    if result.get("is_valid") and not str(result.get("unit") or "").strip():
        result["unit"] = DEFAULT_BALANCE_UNIT
    db.update_account_result(account_id, result)
    if result.get("is_valid"):
        db.add_log(
            "info",
            "query",
            f"{account['platform']} / {account['name']} 查询成功，余额 {result.get('remaining')} {result.get('unit') or ''}".strip(),
        )
    else:
        db.add_log(
            "error",
            "query",
            f"{account['platform']} / {account['name']} 查询失败: {result.get('invalid_message') or '未知错误'}",
        )
    try:
        handle_alert_state(db, account_id)
    except Exception as exc:
        result["extra"] = f"告警发送失败: {exc}"
        db.update_account_result(account_id, result)
        db.add_log("error", "alert", f"{account['platform']} / {account['name']} 告警发送失败: {exc}")
    consumption_stats = db.get_consumption_stats(account_id)
    actual_stats = actual_consumption_stats(consumption_stats, db.get_account(account_id))
    result["consumption_stats"] = consumption_stats
    result["consumptionStats"] = consumption_stats
    result["actual_consumption_stats"] = actual_stats
    result["actualConsumptionStats"] = actual_stats
    result["today_consumption"] = consumption_stats["today"]
    result["todayConsumption"] = result["today_consumption"]
    result["actual_today_consumption"] = actual_stats["today"]
    result["actualTodayConsumption"] = result["actual_today_consumption"]
    return result


async def query_sub2api_group_for_account(db: Database, account_id: int) -> dict:
    return await query_group_rate_for_account(db, account_id, notify=True)


async def query_group_rate_for_account(db: Database, account_id: int, notify: bool = True) -> dict:
    account = db.get_account(account_id)
    if not account:
        db.add_log("error", "query", f"查询失败，账号不存在: {account_id}")
        return {"is_valid": False, "invalid_message": "账号不存在"}
    if account["platform"] not in {"sub2Api", "newApi"}:
        db.add_log("error", "query", f"查询失败，不支持查组的账号: {account_id}")
        return {"is_valid": False, "invalid_message": "仅支持 sub2Api 或 newApi"}
    settings = db.get_general_settings()
    if account["platform"] == "sub2Api":
        result = await query_sub2api_group(account, db.secret_key, settings["request_timeout"], db.add_log)
    else:
        result = await query_newapi_group(account, db.secret_key, settings["request_timeout"], db.add_log)
    if result.get("is_valid"):
        refreshed_access_token = result.get("refreshed_access_token")
        refreshed_refresh_token = result.get("refreshed_refresh_token")
        if refreshed_access_token or refreshed_refresh_token:
            db.update_account_tokens(account_id, access_token=refreshed_access_token, refresh_token=refreshed_refresh_token)
        db.update_account_group_result(account_id, result)
        checked_at = utc_now()
        group_summaries = _monitor_group_summaries_from_result(db, account_id, result)
        record_results = []
        for item in group_summaries:
            record_result = db.record_group_rate_if_changed(
                account_id,
                item["summary"],
                checked_at,
                monitor_group_id=item["monitor_group_id"],
            )
            db.update_monitor_group_snapshot(item["monitor_group_id"], item["summary"], checked_at)
            if record_result["changed"]:
                db.update_account_group_rate_change_status(account_id, True, monitor_group_id=item["monitor_group_id"])
            record_results.append({**record_result, "monitor_group_id": item["monitor_group_id"], "group_id": item["group_id"]})
        if not record_results:
            record_results = [db.record_group_rate_if_changed(account_id, _group_summary_from_result(result), checked_at)]
            if record_results[0]["changed"]:
                db.update_account_group_rate_change_status(account_id, True)
        record_result = next((item for item in record_results if item.get("changed")), record_results[0])
        result["group_rate_record"] = record_result
        result["group_rate_records"] = record_results
        result["groupRateRecords"] = record_results
        record = record_result.get("record") or {}
        updated_name = None
        if len(db.list_monitor_groups(account_id)) <= 1:
            updated_name = db.update_account_name_rate_suffix(account_id, record.get("rate_multiplier"))
        if updated_name:
            account = db.get_account(account_id) or account
        account = db.get_account(account_id) or account
        group_rate_changed = bool(account["last_group_rate_changed"])
        result["group_rate_changed"] = group_rate_changed
        result["groupRateChanged"] = group_rate_changed
        changed_records = [item for item in record_results if item.get("changed")]
        if notify and changed_records and not account["is_eliminated"]:
            try:
                smtp = db.get_smtp_settings()
                for changed_record in changed_records:
                    changed_row = changed_record.get("record") or {}
                    subject, body = build_group_rate_change_email(
                        account,
                        str(changed_row.get("plan_name") or result.get("plan_name") or "-"),
                        changed_record["previous_rate"],
                        changed_record["current_rate"],
                        checked_at,
                    )
                    send_email(smtp, db.secret_key, subject, body)
                    db.add_log(
                        "warning",
                        "alert",
                        f"{account['platform']} / {account['name']} 分组倍率变化: {changed_record['previous_rate']} -> {changed_record['current_rate']}，已发送邮件",
                    )
            except Exception as exc:
                db.add_log("error", "alert", f"{account['platform']} / {account['name']} 分组倍率变化邮件发送失败: {exc}")
        elif notify and changed_records and account["is_eliminated"]:
            db.add_log(
                "info",
                "alert",
                f"{account['platform']} / {account['name']} 已淘汰，跳过分组倍率变化邮件",
            )
    db.add_log(
        "info" if result.get("is_valid") else "error",
        "query",
        f"{account['platform']} / {account['name']} 组查询{('成功' if result.get('is_valid') else '失败')}: {result.get('invalid_message') or result.get('plan_name') or 'OK'}",
    )
    return result


async def query_all_group_rates(db: Database, notify: bool = True) -> list[dict]:
    results = []
    for account in db.list_accounts(enabled_only=True, visible_only=True):
        if account["platform"] == "sub2Api" and (
            not account["api_key_enc"]
            or not (account["refresh_token_enc"] or account["access_token_enc"] or (account["email_enc"] and account["password_enc"]))
        ):
            db.add_log("warning", "query", f"{account['platform']} / {account['name']} 自动查组跳过: 缺少 apiKey/refreshToken/accessToken 或 email/password")
            continue
        if account["platform"] == "newApi" and (not account["access_token_enc"] or not account["user_id_enc"] or not db.list_monitor_groups(account["id"])):
            db.add_log("warning", "query", f"{account['platform']} / {account['name']} 自动查组跳过: 缺少 accessToken/userId/已选分组")
            continue
        if account["platform"] not in {"sub2Api", "newApi"}:
            continue
        results.append(await query_group_rate_for_account(db, account["id"], notify=notify))
    return results


async def query_all_accounts(db: Database) -> list[dict]:
    results = []
    for account in db.list_accounts(enabled_only=True, visible_only=True):
        results.append(await query_one_account(db, account["id"]))
    return results


async def query_opencode_go_for_account(db: Database, account_id: int, *, respect_enabled: bool = False) -> dict:
    account = db.get_opencode_go_account(account_id)
    if not account:
        db.add_log("error", "opencode-go", f"OpenCode Go 查询失败，账号不存在: {account_id}")
        return {"is_valid": False, "invalid_message": "OpenCode Go 账号不存在"}
    if respect_enabled and not account["is_enabled"]:
        db.add_log("info", "opencode-go", f"{account['name']} OpenCode Go 自动刷新已关闭，跳过")
        return {
            "is_valid": True,
            "skipped": True,
            "skip_reason": "auto_refresh_disabled",
            "account_id": account_id,
            "accountId": account_id,
            "checked_at": utc_now(),
        }
    settings = db.get_general_settings()
    lite_subscription_js_url = db.get_setting("opencode_go_lite_subscription_js_url", "")
    lite_subscription_server_id = db.get_setting("opencode_go_lite_subscription_server_id", "")
    key_list_js_url = db.get_setting("opencode_go_key_list_js_url", "")
    key_list_server_id = db.get_setting("opencode_go_key_list_server_id", "")
    result = await refresh_opencode_go_account(
        account,
        db.secret_key,
        settings["request_timeout"],
        db.add_log,
        lite_subscription_js_url=lite_subscription_js_url,
        lite_subscription_server_id=lite_subscription_server_id,
        key_list_js_url=key_list_js_url,
        key_list_server_id=key_list_server_id,
    )
    result["account_id"] = account_id
    result["accountId"] = account_id
    result["checked_at"] = result.get("checked_at") or utc_now()
    db.update_opencode_go_result(account_id, result)
    if result.get("is_valid"):
        db.add_log("info", "opencode-go", f"{account['name']} OpenCode Go 刷新成功")
        await sync_opencode_go_cpa_state(db, account, result)
    else:
        db.add_log("error", "opencode-go", f"{account['name']} OpenCode Go 刷新失败: {result.get('invalid_message') or '未知错误'}")
    return result


async def query_all_opencode_go_accounts(db: Database) -> list[dict]:
    results = []
    for account in db.list_opencode_go_accounts(enabled_only=True):
        result = await query_opencode_go_for_account(db, account["id"], respect_enabled=True)
        if not result.get("skipped"):
            results.append(result)
    return results


def _group_summary_from_result(result: dict) -> dict:
    import json

    extra = result.get("extra")
    if isinstance(extra, str):
        try:
            summary = json.loads(extra)
        except json.JSONDecodeError:
            summary = {}
    elif isinstance(extra, dict):
        summary = dict(extra)
    else:
        summary = {}
    summary["raw_json"] = extra if isinstance(extra, str) else json.dumps(summary, ensure_ascii=False, default=str)
    return summary


def _monitor_group_summaries_from_result(db: Database, account_id: int, result: dict) -> list[dict]:
    monitor_groups = [monitor_group_to_dict(row, db.secret_key) for row in db.list_monitor_groups(account_id)]
    if not monitor_groups:
        return []
    summary = _group_summary_from_result(result)
    available = result.get("available_groups") if isinstance(result.get("available_groups"), list) else None
    if available is None:
        available = summary.get("available_groups") if isinstance(summary.get("available_groups"), list) else None
    groups = summary.get("groups") if isinstance(summary.get("groups"), list) else None
    candidates = available or groups or ([summary.get("group")] if isinstance(summary.get("group"), dict) else [])
    results = []
    for monitor_group in monitor_groups:
        group_id = str(monitor_group.get("group_id") or "")
        matched = next((group for group in candidates if isinstance(group, dict) and str(group.get("id") or group.get("name") or "") == group_id), None)
        if matched is None and str(summary.get("group_id") or "") == group_id and isinstance(summary.get("group"), dict):
            matched = summary["group"]
        if matched is None:
            matched = {
                "id": group_id,
                "plan_name": monitor_group.get("plan_name") or f"分组 {group_id}",
                "effective_rate_multiplier": monitor_group.get("effective_rate_multiplier"),
            }
        group_summary = {
            "title": f"{matched.get('plan_name') or matched.get('name') or group_id} 倍率 {matched.get('effective_rate_multiplier')}",
            "group_id": group_id,
            "group": matched,
            "groups": [matched],
            "raw_json": summary.get("raw_json"),
        }
        results.append(
            {
                "monitor_group_id": monitor_group["id"],
                "group_id": group_id,
                "summary": group_summary,
            }
        )
    return results


async def sync_opencode_go_cpa_state(db: Database, account: dict, result: dict) -> None:
    weekly = _usage_percent(result.get("weekly_usage") or result.get("weeklyUsage"))
    monthly = _usage_percent(result.get("monthly_usage") or result.get("monthlyUsage"))
    if weekly is None and monthly is None:
        return
    account_id = int(account["id"])
    email = str(account["name"] or "").strip()
    provider_disabled = _nullable_bool(account["cpa_provider_disabled"])
    reenable_pending = bool(account["cpa_reenable_pending"])
    weekly_full = weekly is not None and weekly >= OPENCODE_GO_CPA_USAGE_THRESHOLD
    monthly_full = monthly is not None and monthly >= OPENCODE_GO_CPA_USAGE_THRESHOLD
    if weekly_full or monthly_full:
        if provider_disabled is True:
            return
        await _set_opencode_go_cpa_disabled(db, account_id, email, True, weekly, monthly)
        return

    weekly_recovered = weekly is not None and weekly < OPENCODE_GO_CPA_USAGE_THRESHOLD
    monthly_recovered = monthly is not None and monthly < OPENCODE_GO_CPA_USAGE_THRESHOLD
    if not (weekly_recovered and monthly_recovered):
        return
    if provider_disabled is not True:
        return
    previous_weekly = _usage_percent(account["last_weekly_usage"])
    previous_monthly = _usage_percent(account["last_monthly_usage"])
    previous_full = (
        (previous_weekly is not None and previous_weekly >= OPENCODE_GO_CPA_USAGE_THRESHOLD)
        or (previous_monthly is not None and previous_monthly >= OPENCODE_GO_CPA_USAGE_THRESHOLD)
    )
    if not (reenable_pending or previous_full):
        return
    await _set_opencode_go_cpa_disabled(db, account_id, email, False, weekly, monthly)


async def _set_opencode_go_cpa_disabled(
    db: Database,
    account_id: int,
    email: str,
    disabled: bool,
    weekly: float | None,
    monthly: float | None,
) -> None:
    action = "停用" if disabled else "启用"
    try:
        client = cpa_admin_client_from_db(db)
        result = await client.set_openai_provider_disabled(email, disabled)
    except CpaAdminError as exc:
        message = _safe_cpa_action_error(exc)
        db.update_opencode_go_cpa_state(
            account_id,
            reenable_pending=(not disabled) or None,
            error=message,
        )
        db.add_log("error", "opencode-go", f"{email} CPA 自动{action}失败，用量 {_usage_pair_text(weekly, monthly)}: {message}")
        return
    db.update_opencode_go_cpa_state(
        account_id,
        provider_disabled=disabled,
        reenable_pending=False,
        action_at=utc_now(),
        clear_error=True,
    )
    suffix = "，无需远端变更" if not result.get("changed") else ""
    db.add_log("warning" if disabled else "info", "opencode-go", f"{email} CPA 自动{action}成功，用量 {_usage_pair_text(weekly, monthly)}{suffix}")


def _usage_percent(value: object) -> float | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    if not isinstance(value, dict):
        return None
    raw = value.get("usage_percent", value.get("usagePercent"))
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _nullable_bool(value: object) -> bool | None:
    if value is None:
        return None
    return bool(value)


def _usage_pair_text(weekly: float | None, monthly: float | None) -> str:
    return f"7d {_usage_value_text(weekly)}，30d {_usage_value_text(monthly)}"


def _usage_value_text(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:g}%"


def _safe_cpa_action_error(exc: Exception) -> str:
    message = str(exc) or "未知错误"
    return message[:300]

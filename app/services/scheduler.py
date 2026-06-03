from __future__ import annotations

import asyncio
from contextlib import suppress

from app.models import Database
from app.models import utc_now
from app.services.alerts import handle_alert_state
from app.services.balance import query_account, query_newapi_group, query_sub2api_group
from app.services.emailer import build_group_rate_change_email, send_email


class BalanceScheduler:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._task: asyncio.Task[None] | None = None
        self._group_rate_task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()
        self._settings_changed = asyncio.Event()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopped.clear()
            self._task = asyncio.create_task(self._run())
        if self._group_rate_task is None or self._group_rate_task.done():
            self._stopped.clear()
            self._group_rate_task = asyncio.create_task(self._run_group_rate_loop())

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


async def query_one_account(db: Database, account_id: int) -> dict:
    account = db.get_account(account_id)
    if not account:
        db.add_log("error", "query", f"查询失败，账号不存在: {account_id}")
        return {"is_valid": False, "invalid_message": "账号不存在"}
    settings = db.get_general_settings()
    result = await query_account(account, db.secret_key, settings["request_timeout"], db.add_log)
    result["account_id"] = account_id
    result["checked_at"] = utc_now()
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
    result["today_consumption"] = db.get_today_consumption(account_id)
    result["todayConsumption"] = result["today_consumption"]
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
        db.update_account_group_result(account_id, result)
        checked_at = utc_now()
        record_result = db.record_group_rate_if_changed(account_id, _group_summary_from_result(result), checked_at)
        result["group_rate_record"] = record_result
        record = record_result.get("record") or {}
        updated_name = db.update_account_name_rate_suffix(account_id, record.get("rate_multiplier"))
        if updated_name:
            account = db.get_account(account_id) or account
        group_rate_changed = bool(account["last_group_rate_changed"])
        if record_result["changed"]:
            group_rate_changed = True
            db.update_account_group_rate_change_status(account_id, True)
        result["group_rate_changed"] = group_rate_changed
        result["groupRateChanged"] = group_rate_changed
        if notify and record_result["changed"] and not account["is_eliminated"]:
            try:
                smtp = db.get_smtp_settings()
                subject, body = build_group_rate_change_email(
                    account,
                    str(record.get("plan_name") or result.get("plan_name") or "-"),
                    record_result["previous_rate"],
                    record_result["current_rate"],
                    checked_at,
                )
                send_email(smtp, db.secret_key, subject, body)
                db.add_log(
                    "warning",
                    "alert",
                    f"{account['platform']} / {account['name']} 分组倍率变化: {record_result['previous_rate']} -> {record_result['current_rate']}，已发送邮件",
                )
            except Exception as exc:
                db.add_log("error", "alert", f"{account['platform']} / {account['name']} 分组倍率变化邮件发送失败: {exc}")
        elif notify and record_result["changed"] and account["is_eliminated"]:
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
    for account in db.list_accounts():
        if not account["is_enabled"]:
            continue
        if account["platform"] == "sub2Api" and (not account["api_key_enc"] or not account["email_enc"] or not account["password_enc"]):
            db.add_log("warning", "query", f"{account['platform']} / {account['name']} 自动查组跳过: 缺少 apiKey/email/password")
            continue
        if account["platform"] == "newApi" and (not account["access_token_enc"] or not account["user_id_enc"] or not account["key_id_enc"]):
            db.add_log("warning", "query", f"{account['platform']} / {account['name']} 自动查组跳过: 缺少 accessToken/userId/已选分组")
            continue
        if account["platform"] not in {"sub2Api", "newApi"}:
            continue
        results.append(await query_group_rate_for_account(db, account["id"], notify=notify))
    return results


async def query_all_accounts(db: Database) -> list[dict]:
    results = []
    for account in db.list_accounts():
        if account["is_enabled"]:
            results.append(await query_one_account(db, account["id"]))
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

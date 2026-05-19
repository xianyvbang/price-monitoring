from __future__ import annotations

import asyncio
from contextlib import suppress

from app.models import Database
from app.services.alerts import handle_alert_state
from app.services.balance import query_account, query_sub2api_group


class BalanceScheduler:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopped.clear()
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stopped.set()
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        while not self._stopped.is_set():
            settings = self.db.get_general_settings()
            await query_all_accounts(self.db)
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=settings["query_interval"])
            except asyncio.TimeoutError:
                pass


async def query_one_account(db: Database, account_id: int) -> dict:
    account = db.get_account(account_id)
    if not account:
        db.add_log("error", "query", f"查询失败，账号不存在: {account_id}")
        return {"is_valid": False, "invalid_message": "账号不存在"}
    settings = db.get_general_settings()
    result = await query_account(account, db.secret_key, settings["request_timeout"], db.add_log)
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
    return result


async def query_sub2api_group_for_account(db: Database, account_id: int) -> dict:
    account = db.get_account(account_id)
    if not account:
        db.add_log("error", "query", f"查询失败，账号不存在: {account_id}")
        return {"is_valid": False, "invalid_message": "账号不存在"}
    if account["platform"] != "sub2Api":
        db.add_log("error", "query", f"查询失败，非 sub2Api 账号: {account_id}")
        return {"is_valid": False, "invalid_message": "仅支持 sub2Api"}
    settings = db.get_general_settings()
    result = await query_sub2api_group(account, db.secret_key, settings["request_timeout"], db.add_log)
    db.update_account_result(account_id, result)
    db.add_log(
        "info" if result.get("is_valid") else "error",
        "query",
        f"{account['platform']} / {account['name']} 组查询{('成功' if result.get('is_valid') else '失败')}: {result.get('invalid_message') or result.get('plan_name') or 'OK'}",
    )
    try:
        handle_alert_state(db, account_id)
    except Exception as exc:
        result["extra"] = f"告警发送失败: {exc}"
        db.update_account_result(account_id, result)
        db.add_log("error", "alert", f"{account['platform']} / {account['name']} 告警发送失败: {exc}")
    return result


async def query_all_accounts(db: Database) -> list[dict]:
    results = []
    for account in db.list_accounts():
        if account["is_enabled"]:
            results.append(await query_one_account(db, account["id"]))
    return results

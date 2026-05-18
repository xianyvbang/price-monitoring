from __future__ import annotations

from typing import Any

from app.models import Database
from app.services.emailer import build_low_balance_email, send_email


def effective_threshold(account: Any, settings: dict[str, Any]) -> float:
    return float(account["threshold"] if account["threshold"] is not None else settings["default_threshold"])


def handle_alert_state(db: Database, account_id: int) -> None:
    account = db.get_account(account_id)
    if not account:
        return
    settings = db.get_general_settings()
    remaining = account["last_remaining"]
    if remaining is None or account["last_status"] != "valid":
        return
    threshold = effective_threshold(account, settings)
    if float(remaining) < threshold:
        if not account["low_balance_active"]:
            smtp = db.get_smtp_settings()
            subject, body = build_low_balance_email(account, threshold, float(remaining))
            send_email(smtp, db.secret_key, subject, body)
            db.set_alert_state(account_id, True, sent=True)
            db.add_log("warning", "alert", f"{account['platform']} / {account['name']} 余额 {remaining} 低于阈值 {threshold}，已发送邮件")
        return
    if account["low_balance_active"]:
        db.set_alert_state(account_id, False, sent=False)
        db.add_log("info", "alert", f"{account['platform']} / {account['name']} 余额恢复到阈值以上")

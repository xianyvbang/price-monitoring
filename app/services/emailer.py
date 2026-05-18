from __future__ import annotations

import smtplib
from email.message import EmailMessage
from email.utils import formataddr
from typing import Any

from app.security import decrypt_value


class EmailConfigError(RuntimeError):
    pass


def send_email(settings: Any, secret_key: str, subject: str, body: str) -> None:
    host = settings["host"]
    receiver = settings["receiver"]
    sender = settings["sender"] or settings["username"]
    if not host or not receiver or not sender:
        raise EmailConfigError("SMTP 未配置完整")

    message = EmailMessage()
    sender_name = settings["sender_name"] if "sender_name" in settings.keys() else ""
    message["From"] = formataddr((sender_name, sender)) if sender_name else sender
    message["To"] = receiver
    message["Subject"] = subject
    message.set_content(body)

    port = int(settings["port"] or 465)
    security = settings["security"] or "ssl"
    password = decrypt_value(settings["password_enc"], secret_key)

    if security == "ssl":
        server: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=15)
    else:
        server = smtplib.SMTP(host, port, timeout=15)
    try:
        if security == "starttls":
            server.starttls()
        if settings["username"]:
            server.login(settings["username"], password or "")
        server.send_message(message)
    finally:
        server.quit()


def build_low_balance_email(account: Any, threshold: float, remaining: float) -> tuple[str, str]:
    unit = account["last_unit"] or "USD"
    subject = f"余额预警: {account['name']} 余额低于阈值"
    body = "\n".join(
        [
            f"平台: {account['platform']}",
            f"账号: {account['name']}",
            f"当前余额: {remaining} {unit}",
            f"预警阈值: {threshold} {unit}",
            f"套餐: {account['last_plan_name'] or '-'}",
            f"最近查询: {account['last_checked_at'] or '-'}",
        ]
    )
    return subject, body

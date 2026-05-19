from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - keeps config usable without optional dotenv support
    load_dotenv = None

if load_dotenv:
    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)


@dataclass(frozen=True)
class AppConfig:
    database_path: str
    admin_username: str
    admin_password: str
    app_secret_key: str
    session_cookie: str


def get_config() -> AppConfig:
    return AppConfig(
        database_path=os.getenv("DATABASE_PATH", "data/app.db"),
        admin_username=os.getenv("ADMIN_USERNAME", "admin"),
        admin_password=os.getenv("ADMIN_PASSWORD", "admin123456"),
        app_secret_key=os.getenv("APP_SECRET_KEY", "change-me-to-a-long-random-secret"),
        session_cookie=os.getenv("SESSION_COOKIE", "balance_monitor_session"),
    )

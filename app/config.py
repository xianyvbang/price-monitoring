from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_APP_SECRET_KEY = "change-me-to-a-long-random-secret"
DEFAULT_SESSION_MAX_AGE_SECONDS = 7 * 24 * 60 * 60

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)


@dataclass(frozen=True)
class AppConfig:
    database_path: str
    admin_username: str
    admin_password: str
    app_secret_key: str
    session_cookie: str
    session_max_age_seconds: int


def get_config() -> AppConfig:
    return AppConfig(
        database_path=os.getenv("DATABASE_PATH", "data/app.db"),
        admin_username=os.getenv("ADMIN_USERNAME", "admin"),
        admin_password=os.getenv("ADMIN_PASSWORD", "admin123456"),
        app_secret_key=os.getenv("APP_SECRET_KEY", DEFAULT_APP_SECRET_KEY),
        session_cookie=os.getenv("SESSION_COOKIE", "balance_monitor_session"),
        session_max_age_seconds=max(60, _env_int("SESSION_MAX_AGE_SECONDS", DEFAULT_SESSION_MAX_AGE_SECONDS)),
    )


def uses_default_app_secret(secret_key: str) -> bool:
    return secret_key == DEFAULT_APP_SECRET_KEY


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default

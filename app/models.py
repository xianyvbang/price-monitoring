from __future__ import annotations

import calendar
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from app.defaults import DEFAULT_ACCOUNTS
from app.security import encrypt_value, hash_password


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


CHINA_TZ = timezone(timedelta(hours=8))
DEFAULT_BALANCE_UNIT = "USD"
BALANCE_HISTORY_MONTHS = 9
BALANCE_TREND_DAYS = 3
BALANCE_QUERY_INTERVAL_SECONDS = 5 * 60
GROUP_RATE_QUERY_INTERVAL_SECONDS = 20 * 60


def format_china_time(value: Any) -> str:
    if not value:
        return "-"
    try:
        text = str(value)
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return str(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(CHINA_TZ).strftime("%Y-%m-%d %H:%M:%S")


class Database:
    def __init__(self, path: str, secret_key: str) -> None:
        self.path = path
        self.secret_key = secret_key
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)

    @contextmanager
    def connect(self) -> Iterable[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init(self) -> None:
        with self.connect() as conn:
            accounts_existed = self._table_exists(conn, "accounts")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    session_version INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS smtp_settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    host TEXT,
                    port INTEGER,
                    username TEXT,
                    password_enc TEXT,
                    sender TEXT,
                    sender_name TEXT,
                    receiver TEXT,
                    security TEXT,
                    updated_at TEXT
                );

                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform TEXT NOT NULL CHECK (platform IN ('newApi', 'sub2Api')),
                    name TEXT NOT NULL,
                    base_url TEXT NOT NULL,
                    note TEXT,
                    recharge_url TEXT,
                    key_id_enc TEXT,
                    api_key_enc TEXT,
                    email_enc TEXT,
                    password_enc TEXT,
                    access_token_enc TEXT,
                    user_id_enc TEXT,
                    threshold REAL,
                    is_enabled INTEGER NOT NULL DEFAULT 1,
                    is_visible INTEGER NOT NULL DEFAULT 1,
                    is_eliminated INTEGER NOT NULL DEFAULT 0,
                    last_status TEXT NOT NULL DEFAULT 'never',
                    last_error TEXT,
                    last_plan_name TEXT,
                    last_remaining REAL,
                    last_unit TEXT,
                    last_total REAL,
                    last_used REAL,
                    last_extra TEXT,
                    last_group_rate_changed INTEGER NOT NULL DEFAULT 0,
                    last_checked_at TEXT,
                    low_balance_active INTEGER NOT NULL DEFAULT 0,
                    last_alert_sent_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(platform, name)
                );

                CREATE TABLE IF NOT EXISTS query_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                    is_valid INTEGER NOT NULL,
                    remaining REAL,
                    unit TEXT,
                    plan_name TEXT,
                    total REAL,
                    used REAL,
                    extra TEXT,
                    error TEXT,
                    checked_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS group_rate_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                    plan_name TEXT NOT NULL,
                    rate_multiplier REAL,
                    raw_json TEXT NOT NULL,
                    checked_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS app_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    level TEXT NOT NULL,
                    category TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_app_logs_created_at ON app_logs(created_at);
                CREATE INDEX IF NOT EXISTS idx_group_rate_records_account_checked_at
                ON group_rate_records(account_id, checked_at DESC);
                CREATE INDEX IF NOT EXISTS idx_query_records_account_checked_at
                ON query_records(account_id, checked_at DESC);
                """
            )
            self._migrate_smtp_nullable(conn)
            self._migrate_users_session_version(conn)
            self._migrate_accounts_key_id(conn)
            self._migrate_accounts_sub2api_login(conn)
            self._migrate_accounts_note(conn)
            self._migrate_accounts_recharge_url(conn)
            self._migrate_accounts_group_rate_changed(conn)
            self._migrate_accounts_visible(conn)
            self._migrate_accounts_eliminated(conn)
            self._set_default(conn, "request_timeout", "15")
            self._set_default(conn, "query_interval", str(BALANCE_QUERY_INTERVAL_SECONDS))
            self._set_default(conn, "group_rate_query_interval", str(GROUP_RATE_QUERY_INTERVAL_SECONDS))
            self._set_default(conn, "default_threshold", "5")
            self._set_default(conn, "monitor_paused", "0")
            conn.execute(
                """
                INSERT OR IGNORE INTO smtp_settings (id, updated_at)
                VALUES (1, ?)
                """,
                (utc_now(),),
            )
            if not accounts_existed:
                self._seed_default_accounts(conn)
        self.cleanup_logs()
        self.cleanup_balance_history()

    def ensure_admin(self, username: str, password: str) -> None:
        with self.connect() as conn:
            row = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO users (username, password_hash, session_version, created_at) VALUES (?, ?, ?, ?)",
                    (username, hash_password(password), 0, utc_now()),
                )

    def update_user_password(self, username: str, password: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE users SET password_hash = ?, session_version = COALESCE(session_version, 0) + 1 WHERE username = ?",
                (hash_password(password), username),
            )

    @staticmethod
    def _set_default(conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value))

    @staticmethod
    def _seed_default_accounts(conn: sqlite3.Connection) -> None:
        count = conn.execute("SELECT COUNT(*) AS count FROM accounts").fetchone()["count"]
        if count:
            return
        now = utc_now()
        for account in DEFAULT_ACCOUNTS:
            conn.execute(
                """
                INSERT INTO accounts (
                    platform, name, base_url, threshold, is_enabled, is_visible, created_at, updated_at
                )
                VALUES (?, ?, ?, NULL, 1, 1, ?, ?)
                """,
                (account["platform"], account["name"], account["base_url"], now, now),
            )

    @staticmethod
    def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()
        return row is not None

    @staticmethod
    def _migrate_smtp_nullable(conn: sqlite3.Connection) -> None:
        columns = conn.execute("PRAGMA table_info(smtp_settings)").fetchall()
        column_names = {row["name"] for row in columns}
        if "sender_name" not in column_names:
            conn.execute("ALTER TABLE smtp_settings ADD COLUMN sender_name TEXT")
            columns = conn.execute("PRAGMA table_info(smtp_settings)").fetchall()
        notnull = {row["name"]: row["notnull"] for row in columns}
        if notnull.get("port") == 0 and notnull.get("security") == 0:
            return
        conn.executescript(
            """
            CREATE TABLE smtp_settings_new (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                host TEXT,
                port INTEGER,
                username TEXT,
                password_enc TEXT,
                sender TEXT,
                sender_name TEXT,
                receiver TEXT,
                security TEXT,
                updated_at TEXT
            );

            INSERT INTO smtp_settings_new (
                id, host, port, username, password_enc, sender, sender_name, receiver, security, updated_at
            )
            SELECT id, host, port, username, password_enc, sender, sender_name, receiver, security, updated_at
            FROM smtp_settings;

            DROP TABLE smtp_settings;
            ALTER TABLE smtp_settings_new RENAME TO smtp_settings;
            """
        )

    @staticmethod
    def _migrate_accounts_key_id(conn: sqlite3.Connection) -> None:
        columns = conn.execute("PRAGMA table_info(accounts)").fetchall()
        column_names = {row["name"] for row in columns}
        if "key_id_enc" not in column_names:
            conn.execute("ALTER TABLE accounts ADD COLUMN key_id_enc TEXT")

    @staticmethod
    def _migrate_accounts_sub2api_login(conn: sqlite3.Connection) -> None:
        columns = conn.execute("PRAGMA table_info(accounts)").fetchall()
        column_names = {row["name"] for row in columns}
        if "email_enc" not in column_names:
            conn.execute("ALTER TABLE accounts ADD COLUMN email_enc TEXT")
        if "password_enc" not in column_names:
            conn.execute("ALTER TABLE accounts ADD COLUMN password_enc TEXT")

    @staticmethod
    def _migrate_accounts_note(conn: sqlite3.Connection) -> None:
        columns = conn.execute("PRAGMA table_info(accounts)").fetchall()
        column_names = {row["name"] for row in columns}
        if "note" not in column_names:
            conn.execute("ALTER TABLE accounts ADD COLUMN note TEXT")

    @staticmethod
    def _migrate_accounts_recharge_url(conn: sqlite3.Connection) -> None:
        columns = conn.execute("PRAGMA table_info(accounts)").fetchall()
        column_names = {row["name"] for row in columns}
        if "recharge_url" not in column_names:
            conn.execute("ALTER TABLE accounts ADD COLUMN recharge_url TEXT")

    @staticmethod
    def _migrate_accounts_group_rate_changed(conn: sqlite3.Connection) -> None:
        columns = conn.execute("PRAGMA table_info(accounts)").fetchall()
        column_names = {row["name"] for row in columns}
        if "last_group_rate_changed" not in column_names:
            conn.execute("ALTER TABLE accounts ADD COLUMN last_group_rate_changed INTEGER NOT NULL DEFAULT 0")

    @staticmethod
    def _migrate_accounts_visible(conn: sqlite3.Connection) -> None:
        columns = conn.execute("PRAGMA table_info(accounts)").fetchall()
        column_names = {row["name"] for row in columns}
        if "is_visible" not in column_names:
            conn.execute("ALTER TABLE accounts ADD COLUMN is_visible INTEGER NOT NULL DEFAULT 1")

    @staticmethod
    def _migrate_accounts_eliminated(conn: sqlite3.Connection) -> None:
        columns = conn.execute("PRAGMA table_info(accounts)").fetchall()
        column_names = {row["name"] for row in columns}
        if "is_eliminated" not in column_names:
            conn.execute("ALTER TABLE accounts ADD COLUMN is_eliminated INTEGER NOT NULL DEFAULT 0")

    @staticmethod
    def _migrate_users_session_version(conn: sqlite3.Connection) -> None:
        columns = conn.execute("PRAGMA table_info(users)").fetchall()
        column_names = {row["name"] for row in columns}
        if "session_version" not in column_names:
            conn.execute("ALTER TABLE users ADD COLUMN session_version INTEGER NOT NULL DEFAULT 0")

    def get_user(self, username: str) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()

    def get_general_settings(self) -> dict[str, Any]:
        with self.connect() as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
        values = {row["key"]: row["value"] for row in rows}
        return {
            "request_timeout": float(values.get("request_timeout", "15")),
            "query_interval": max(
                BALANCE_QUERY_INTERVAL_SECONDS,
                int(float(values.get("query_interval", str(BALANCE_QUERY_INTERVAL_SECONDS)))),
            ),
            "group_rate_query_interval": int(float(values.get("group_rate_query_interval", str(GROUP_RATE_QUERY_INTERVAL_SECONDS)))),
            "default_threshold": float(values.get("default_threshold", "5")),
            "monitor_paused": str(values.get("monitor_paused", "0")).strip().lower() in {"1", "true", "yes", "on"},
        }

    def update_general_settings(
        self,
        request_timeout: float,
        query_interval: int,
        default_threshold: float,
        group_rate_query_interval: int = GROUP_RATE_QUERY_INTERVAL_SECONDS,
        monitor_paused: bool | None = None,
    ) -> None:
        with self.connect() as conn:
            values = {
                "request_timeout": str(max(1.0, request_timeout)),
                "query_interval": str(max(BALANCE_QUERY_INTERVAL_SECONDS, query_interval)),
                "group_rate_query_interval": str(max(60, group_rate_query_interval)),
                "default_threshold": str(max(0.0, default_threshold)),
            }
            if monitor_paused is not None:
                values["monitor_paused"] = "1" if monitor_paused else "0"
            for key, value in values.items():
                conn.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )

    def set_monitor_paused(self, paused: bool) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES ('monitor_paused', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                ("1" if paused else "0",),
            )

    def get_smtp_settings(self) -> sqlite3.Row:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM smtp_settings WHERE id = 1").fetchone()

    def update_smtp_settings(
        self,
        host: str,
        port: Optional[int],
        username: str,
        password: str,
        sender: str,
        sender_name: str,
        receiver: str,
        security: str,
    ) -> None:
        password_enc = None
        if password:
            password_enc = encrypt_value(password, self.secret_key)
        with self.connect() as conn:
            current = conn.execute("SELECT password_enc FROM smtp_settings WHERE id = 1").fetchone()
            if not password_enc and current:
                password_enc = current["password_enc"]
            conn.execute(
                """
                UPDATE smtp_settings
                SET host = ?, port = ?, username = ?, password_enc = ?, sender = ?, sender_name = ?,
                    receiver = ?, security = ?, updated_at = ?
                WHERE id = 1
                """,
                (host, port, username, password_enc, sender, sender_name, receiver, security or None, utc_now()),
            )

    def list_accounts(
        self,
        platform: Optional[str] = None,
        enabled_only: bool = False,
        visible_only: bool = False,
    ) -> list[sqlite3.Row]:
        query = "SELECT * FROM accounts"
        conditions = []
        params: list[Any] = []
        if platform:
            conditions.append("platform = ?")
            params.append(platform)
        if enabled_only:
            conditions.append("is_enabled = 1")
        if visible_only:
            conditions.append("is_visible = 1")
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY platform, name"
        with self.connect() as conn:
            return conn.execute(query, tuple(params)).fetchall()

    def get_account(self, account_id: int) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()

    def upsert_account(self, data: dict[str, Any]) -> int:
        now = utc_now()
        platform = data["platform"]
        note = str(data.get("note") or "").strip()
        recharge_url = str(data.get("recharge_url") or "").strip()
        key_id = data.get("key_id")
        api_key = data.get("api_key")
        email = data.get("email")
        password = data.get("password")
        access_token = data.get("access_token")
        if platform == "newApi" and not access_token and api_key:
            access_token = api_key
        key_id_enc = encrypt_value(key_id, self.secret_key)
        api_key_enc = encrypt_value(api_key, self.secret_key)
        email_enc = encrypt_value(email, self.secret_key)
        password_enc = encrypt_value(password, self.secret_key)
        access_token_enc = encrypt_value(access_token, self.secret_key)
        user_id_enc = encrypt_value(data.get("user_id"), self.secret_key)
        threshold = _optional_float(data.get("threshold"))
        is_eliminated = _optional_bool(data.get("is_eliminated"))
        with self.connect() as conn:
            current = conn.execute(
                "SELECT is_enabled, is_visible, is_eliminated FROM accounts WHERE platform = ? AND name = ?",
                (platform, data["name"]),
            ).fetchone()
            is_visible = 1 if data.get("is_visible", current["is_visible"] if current else True) else 0
            is_enabled = 1 if data.get("is_enabled", current["is_enabled"] if current else True) and is_visible else 0
            effective_is_eliminated = is_eliminated if is_eliminated is not None else (current["is_eliminated"] if current else 0)
            conn.execute(
                """
                INSERT INTO accounts (
                    platform, name, base_url, note, recharge_url, key_id_enc, api_key_enc, email_enc, password_enc,
                    access_token_enc, user_id_enc,
                    threshold, is_enabled, is_visible, is_eliminated, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(platform, name) DO UPDATE SET
                    base_url = excluded.base_url,
                    note = excluded.note,
                    recharge_url = excluded.recharge_url,
                    key_id_enc = COALESCE(excluded.key_id_enc, accounts.key_id_enc),
                    api_key_enc = COALESCE(excluded.api_key_enc, accounts.api_key_enc),
                    email_enc = COALESCE(excluded.email_enc, accounts.email_enc),
                    password_enc = COALESCE(excluded.password_enc, accounts.password_enc),
                    access_token_enc = COALESCE(excluded.access_token_enc, accounts.access_token_enc),
                    user_id_enc = COALESCE(excluded.user_id_enc, accounts.user_id_enc),
                    threshold = excluded.threshold,
                    is_enabled = excluded.is_enabled,
                    is_visible = excluded.is_visible,
                    is_eliminated = excluded.is_eliminated,
                    updated_at = excluded.updated_at
                """,
                (
                    platform,
                    data["name"],
                    data["base_url"].rstrip("/"),
                    note,
                    recharge_url,
                    key_id_enc,
                    api_key_enc,
                    email_enc,
                    password_enc,
                    access_token_enc,
                    user_id_enc,
                    threshold,
                    is_enabled,
                    is_visible,
                    effective_is_eliminated,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT id FROM accounts WHERE platform = ? AND name = ?",
                (platform, data["name"]),
            ).fetchone()
            return int(row["id"])

    def update_account(self, account_id: int, data: dict[str, Any]) -> int:
        current = self.get_account(account_id)
        if not current:
            raise ValueError("账号不存在")
        now = utc_now()
        platform = data["platform"]
        note = str(data.get("note") or "").strip()
        recharge_url = str(data.get("recharge_url") or "").strip()
        key_id = data.get("key_id")
        api_key = data.get("api_key")
        email = data.get("email")
        password = data.get("password")
        access_token = data.get("access_token")
        if platform == "newApi" and not access_token and api_key:
            access_token = api_key
        key_id_enc = encrypt_value(key_id, self.secret_key)
        api_key_enc = encrypt_value(api_key, self.secret_key)
        email_enc = encrypt_value(email, self.secret_key)
        password_enc = encrypt_value(password, self.secret_key)
        access_token_enc = encrypt_value(access_token, self.secret_key)
        user_id_enc = encrypt_value(data.get("user_id"), self.secret_key)
        threshold = _optional_float(data.get("threshold"))
        is_visible = 1 if data.get("is_visible", current["is_visible"]) else 0
        is_enabled = 1 if data.get("is_enabled", current["is_enabled"]) and is_visible else 0
        is_eliminated = _optional_bool(data.get("is_eliminated"))
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE accounts
                SET platform = ?, name = ?, base_url = ?, note = ?, recharge_url = ?,
                    key_id_enc = COALESCE(?, key_id_enc),
                    api_key_enc = COALESCE(?, api_key_enc),
                    email_enc = COALESCE(?, email_enc),
                    password_enc = COALESCE(?, password_enc),
                    access_token_enc = COALESCE(?, access_token_enc),
                    user_id_enc = COALESCE(?, user_id_enc),
                    threshold = ?, is_enabled = ?, is_visible = ?, is_eliminated = COALESCE(?, is_eliminated), updated_at = ?
                WHERE id = ?
                """,
                (
                    platform,
                    data["name"],
                    data["base_url"].rstrip("/"),
                    note,
                    recharge_url,
                    key_id_enc,
                    api_key_enc,
                    email_enc,
                    password_enc,
                    access_token_enc,
                    user_id_enc,
                    threshold,
                    is_enabled,
                    is_visible,
                    is_eliminated,
                    now,
                    account_id,
                ),
            )
        return account_id

    def delete_account(self, account_id: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))

    def update_account_result(self, account_id: int, result: dict[str, Any]) -> None:
        status = "valid" if result.get("is_valid") else "invalid"
        checked_at = result.get("checked_at") or utc_now()
        history_cutoff = _months_ago(BALANCE_HISTORY_MONTHS)
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE accounts
                SET last_status = ?, last_error = ?, last_plan_name = ?, last_remaining = ?,
                    last_unit = ?, last_total = ?, last_used = ?,
                    last_checked_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    result.get("invalid_message"),
                    result.get("plan_name"),
                    result.get("remaining"),
                    result.get("unit"),
                    result.get("total"),
                    result.get("used"),
                    checked_at,
                    utc_now(),
                    account_id,
                ),
            )
            conn.execute(
                """
                INSERT INTO query_records (
                    account_id, is_valid, remaining, unit, plan_name, total, used, extra, error, checked_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account_id,
                    1 if result.get("is_valid") else 0,
                    result.get("remaining"),
                    result.get("unit"),
                    result.get("plan_name"),
                    result.get("total"),
                    result.get("used"),
                    result.get("extra"),
                    result.get("invalid_message"),
                    checked_at,
                ),
            )
            conn.execute("DELETE FROM query_records WHERE checked_at < ?", (history_cutoff,))

    def update_account_group_result(self, account_id: int, result: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE accounts
                SET last_extra = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    result.get("extra"),
                    utc_now(),
                    account_id,
                ),
            )

    def update_account_selected_group(self, account_id: int, group_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE accounts
                SET key_id_enc = ?, last_group_rate_changed = 0, updated_at = ?
                WHERE id = ?
                """,
                (encrypt_value(group_id, self.secret_key), utc_now(), account_id),
            )

    def update_account_group_rate_change_status(self, account_id: int, changed: bool) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE accounts
                SET last_group_rate_changed = ?, updated_at = ?
                WHERE id = ?
                """,
                (1 if changed else 0, utc_now(), account_id),
            )

    def update_account_enabled(self, account_id: int, is_enabled: bool) -> None:
        account = self.get_account(account_id)
        effective_enabled = bool(is_enabled and account and account["is_visible"])
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE accounts
                SET is_enabled = ?, low_balance_active = CASE WHEN ? THEN low_balance_active ELSE 0 END,
                    updated_at = ?
                WHERE id = ?
                """,
                (1 if effective_enabled else 0, 1 if effective_enabled else 0, utc_now(), account_id),
            )

    def update_account_visible(self, account_id: int, is_visible: bool) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE accounts
                SET is_visible = ?, is_enabled = CASE WHEN ? THEN is_enabled ELSE 0 END,
                    low_balance_active = CASE WHEN ? THEN low_balance_active ELSE 0 END,
                    updated_at = ?
                WHERE id = ?
                """,
                (1 if is_visible else 0, 1 if is_visible else 0, 1 if is_visible else 0, utc_now(), account_id),
            )

    def update_account_eliminated(self, account_id: int, is_eliminated: bool) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE accounts
                SET is_eliminated = ?, low_balance_active = CASE WHEN ? THEN 0 ELSE low_balance_active END,
                    updated_at = ?
                WHERE id = ?
                """,
                (1 if is_eliminated else 0, 1 if is_eliminated else 0, utc_now(), account_id),
            )

    def update_account_name_rate_suffix(self, account_id: int, rate: Any) -> Optional[str]:
        rate_value = _optional_float(rate)
        if rate_value is None:
            return None
        account = self.get_account(account_id)
        if not account:
            return None
        current_name = str(account["name"] or "").strip()
        if not current_name:
            return None
        new_name = _replace_name_rate_suffix(current_name, rate_value)
        if new_name == current_name:
            return current_name
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE accounts
                SET name = ?, updated_at = ?
                WHERE id = ?
                """,
                (new_name, utc_now(), account_id),
            )
        return new_name

    def record_group_rate_if_changed(
        self,
        account_id: int,
        group_summary: dict[str, Any],
        checked_at: str,
    ) -> dict[str, Any]:
        group = group_summary.get("group") if isinstance(group_summary.get("group"), dict) else {}
        plan_name = (
            group.get("plan_name")
            or group.get("planName")
            or group.get("name")
            or group_summary.get("active_plan_name")
            or group_summary.get("title")
            or "-"
        )
        current_rate = _optional_float(group.get("effective_rate_multiplier"))
        if not group and current_rate is None:
            return {
                "inserted": False,
                "changed": False,
                "previous_rate": None,
                "current_rate": None,
                "record": None,
            }
        raw_json = group_summary.get("raw_json")
        if not raw_json:
            raw_json = group_summary.get("extra")
        if not raw_json:
            raw_json = _json_dumps(group_summary)

        with self.connect() as conn:
            previous = conn.execute(
                """
                SELECT * FROM group_rate_records
                WHERE account_id = ?
                ORDER BY checked_at DESC, id DESC
                LIMIT 1
                """,
                (account_id,),
            ).fetchone()
            previous_rate = previous["rate_multiplier"] if previous else None
            previous_plan_name = previous["plan_name"] if previous else None
            same_plan = previous_plan_name == str(plan_name)
            changed = previous is not None and same_plan and previous_rate != current_rate
            inserted = previous is None or previous_rate != current_rate or not same_plan
            record = previous
            if inserted:
                cursor = conn.execute(
                    """
                    INSERT INTO group_rate_records (account_id, plan_name, rate_multiplier, raw_json, checked_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (account_id, str(plan_name), current_rate, raw_json, checked_at),
                )
                record = conn.execute(
                    "SELECT * FROM group_rate_records WHERE id = ?",
                    (cursor.lastrowid,),
                ).fetchone()
            return {
                "inserted": inserted,
                "changed": changed,
                "previous_rate": previous_rate,
                "current_rate": current_rate,
                "record": row_to_dict(record) if record else None,
            }

    def list_group_rate_records(self, account_id: int) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT * FROM group_rate_records
                WHERE account_id = ?
                ORDER BY checked_at DESC, id DESC
                """,
                (account_id,),
            ).fetchall()

    def list_balance_history(self, account_id: int) -> list[sqlite3.Row]:
        self.cleanup_balance_history()
        cutoff = _days_ago(BALANCE_TREND_DAYS)
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT id, account_id, remaining, unit, plan_name, checked_at
                FROM query_records
                WHERE account_id = ?
                    AND is_valid = 1
                    AND remaining IS NOT NULL
                    AND checked_at >= ?
                ORDER BY checked_at ASC, id ASC
                """,
                (account_id, cutoff),
            ).fetchall()

    def get_consumption_between(self, account_id: int, since: str, until: str | None = None) -> Optional[float]:
        self.cleanup_balance_history()
        date_filter = "AND checked_at < ?" if until else ""
        params: tuple[Any, ...] = (account_id, since, until) if until else (account_id, since)
        with self.connect() as conn:
            records = conn.execute(
                f"""
                SELECT remaining
                FROM query_records
                WHERE account_id = ?
                    AND is_valid = 1
                    AND remaining IS NOT NULL
                    AND checked_at >= ?
                    {date_filter}
                ORDER BY checked_at ASC, id ASC
                """,
                params,
            ).fetchall()
        return _sum_consumption(records)

    def get_consumption_since(self, account_id: int, since: str) -> Optional[float]:
        return self.get_consumption_between(account_id, since)

    def get_today_consumption(self, account_id: int) -> Optional[float]:
        return self.get_consumption_since(account_id, _today_start_utc())

    def get_consumption_stats(self, account_id: int) -> dict[str, Optional[float]]:
        today_start = _today_start_utc()
        this_month_start = _this_month_start_utc()
        last_month_start = _last_month_start_utc()
        return {
            "today": self.get_today_consumption(account_id),
            "yesterday": self.get_consumption_between(account_id, _yesterday_start_utc(), today_start),
            "last_24h": self.get_consumption_since(account_id, _hours_ago(24)),
            "last_7d": self.get_consumption_since(account_id, _days_ago(7)),
            "last_14d": self.get_consumption_since(account_id, _days_ago(14)),
            "this_month": self.get_consumption_since(account_id, this_month_start),
            "last_month": self.get_consumption_between(account_id, last_month_start, this_month_start),
        }

    def clear_balance_history(self, account_id: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM query_records WHERE account_id = ?", (account_id,))

    def set_alert_state(self, account_id: int, active: bool, sent: bool = False) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE accounts
                SET low_balance_active = ?, last_alert_sent_at = CASE WHEN ? THEN ? ELSE last_alert_sent_at END
                WHERE id = ?
                """,
                (1 if active else 0, 1 if sent else 0, utc_now(), account_id),
            )

    def add_log(self, level: str, category: str, message: str) -> None:
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO app_logs (level, category, message, created_at) VALUES (?, ?, ?, ?)",
                (level, category, message, now),
            )
            cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
            conn.execute("DELETE FROM app_logs WHERE created_at < ?", (cutoff,))

    def list_logs(self, limit: int | None = None, offset: int = 0) -> list[sqlite3.Row]:
        self.cleanup_logs()
        with self.connect() as conn:
            if limit is None:
                return conn.execute("SELECT * FROM app_logs ORDER BY created_at DESC, id DESC").fetchall()
            return conn.execute(
                """
                SELECT *
                FROM app_logs
                ORDER BY created_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (max(0, int(limit)), max(0, int(offset))),
            ).fetchall()

    def count_logs(self) -> int:
        self.cleanup_logs()
        with self.connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS total FROM app_logs").fetchone()
            return int(row["total"] if row else 0)

    def clear_logs(self) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM app_logs")

    def cleanup_logs(self) -> None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
        with self.connect() as conn:
            conn.execute("DELETE FROM app_logs WHERE created_at < ?", (cutoff,))

    def cleanup_balance_history(self) -> None:
        cutoff = _months_ago(BALANCE_HISTORY_MONTHS)
        with self.connect() as conn:
            conn.execute("DELETE FROM query_records WHERE checked_at < ?", (cutoff,))


def _sum_consumption(records: list[sqlite3.Row]) -> Optional[float]:
    if not records:
        return None

    previous = float(records[0]["remaining"])
    consumed = 0.0
    for record in records[1:]:
        current = float(record["remaining"])
        if current < previous:
            consumed += previous - current
        previous = current
    return round(consumed, 6)


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _optional_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    return float(value)


def _optional_bool(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    return 0 if str(value).strip().lower() in {"0", "false", "no", "off", ""} else 1


def _json_dumps(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, default=str)


def _days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def _months_ago(months: int) -> str:
    now_china = datetime.now(CHINA_TZ)
    month_index = now_china.month - months
    year = now_china.year + (month_index - 1) // 12
    month = (month_index - 1) % 12 + 1
    day = min(now_china.day, calendar.monthrange(year, month)[1])
    cutoff_china = now_china.replace(year=year, month=month, day=day)
    return cutoff_china.astimezone(timezone.utc).isoformat(timespec="seconds")


def _hours_ago(hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")


def _today_start_utc() -> str:
    return _china_day_start_utc(datetime.now(CHINA_TZ))


def _yesterday_start_utc() -> str:
    return _china_day_start_utc(datetime.now(CHINA_TZ) - timedelta(days=1))


def _this_month_start_utc() -> str:
    now_china = datetime.now(CHINA_TZ)
    month_start = now_china.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return month_start.astimezone(timezone.utc).isoformat(timespec="seconds")


def _last_month_start_utc() -> str:
    now_china = datetime.now(CHINA_TZ)
    this_month_start = now_china.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if this_month_start.month == 1:
        last_month_start = this_month_start.replace(year=this_month_start.year - 1, month=12)
    else:
        last_month_start = this_month_start.replace(month=this_month_start.month - 1)
    return last_month_start.astimezone(timezone.utc).isoformat(timespec="seconds")


def _china_day_start_utc(value: datetime) -> str:
    day_start_china = value.astimezone(CHINA_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    return day_start_china.astimezone(timezone.utc).isoformat(timespec="seconds")


def _replace_name_rate_suffix(name: str, rate: float) -> str:
    prefix, separator, suffix = name.rpartition("-")
    rate_text = _stringify_rate(rate)
    if separator and _looks_like_rate_suffix(suffix):
        return f"{prefix}-{rate_text}"
    return f"{name}-{rate_text}"


def _looks_like_rate_suffix(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    try:
        float(text)
        return True
    except ValueError:
        return False


def _stringify_rate(rate: float) -> str:
    return format(rate, "g")

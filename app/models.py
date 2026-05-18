from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from app.defaults import DEFAULT_ACCOUNTS
from app.security import encrypt_value, hash_password


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
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
                    api_key_enc TEXT,
                    access_token_enc TEXT,
                    user_id_enc TEXT,
                    threshold REAL,
                    is_enabled INTEGER NOT NULL DEFAULT 1,
                    last_status TEXT NOT NULL DEFAULT 'never',
                    last_error TEXT,
                    last_plan_name TEXT,
                    last_remaining REAL,
                    last_unit TEXT,
                    last_total REAL,
                    last_used REAL,
                    last_extra TEXT,
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

                CREATE TABLE IF NOT EXISTS app_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    level TEXT NOT NULL,
                    category TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_app_logs_created_at ON app_logs(created_at);
                """
            )
            self._migrate_smtp_nullable(conn)
            self._set_default(conn, "request_timeout", "15")
            self._set_default(conn, "query_interval", "30")
            self._set_default(conn, "default_threshold", "5")
            conn.execute(
                """
                INSERT OR IGNORE INTO smtp_settings (id, updated_at)
                VALUES (1, ?)
                """,
                (utc_now(),),
            )
            self._seed_default_accounts(conn)
        self.cleanup_logs()

    def ensure_admin(self, username: str, password: str) -> None:
        with self.connect() as conn:
            row = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
                    (username, hash_password(password), utc_now()),
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
                    platform, name, base_url, threshold, is_enabled, created_at, updated_at
                )
                VALUES (?, ?, ?, NULL, 1, ?, ?)
                """,
                (account["platform"], account["name"], account["base_url"], now, now),
            )

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

    def get_user(self, username: str) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()

    def get_general_settings(self) -> dict[str, Any]:
        with self.connect() as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
        values = {row["key"]: row["value"] for row in rows}
        return {
            "request_timeout": float(values.get("request_timeout", "15")),
            "query_interval": int(float(values.get("query_interval", "30"))),
            "default_threshold": float(values.get("default_threshold", "5")),
        }

    def update_general_settings(self, request_timeout: float, query_interval: int, default_threshold: float) -> None:
        with self.connect() as conn:
            for key, value in {
                "request_timeout": str(max(1.0, request_timeout)),
                "query_interval": str(max(30, query_interval)),
                "default_threshold": str(max(0.0, default_threshold)),
            }.items():
                conn.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
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

    def list_accounts(self, platform: Optional[str] = None) -> list[sqlite3.Row]:
        query = "SELECT * FROM accounts"
        params: tuple[Any, ...] = ()
        if platform:
            query += " WHERE platform = ?"
            params = (platform,)
        query += " ORDER BY platform, name"
        with self.connect() as conn:
            return conn.execute(query, params).fetchall()

    def get_account(self, account_id: int) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()

    def upsert_account(self, data: dict[str, Any]) -> int:
        now = utc_now()
        platform = data["platform"]
        api_key = data.get("api_key")
        access_token = data.get("access_token")
        if platform == "newApi" and not access_token and api_key:
            access_token = api_key
        api_key_enc = encrypt_value(api_key, self.secret_key)
        access_token_enc = encrypt_value(access_token, self.secret_key)
        user_id_enc = encrypt_value(data.get("user_id"), self.secret_key)
        threshold = _optional_float(data.get("threshold"))
        is_enabled = 1 if data.get("is_enabled", True) else 0
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO accounts (
                    platform, name, base_url, api_key_enc, access_token_enc, user_id_enc,
                    threshold, is_enabled, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(platform, name) DO UPDATE SET
                    base_url = excluded.base_url,
                    api_key_enc = COALESCE(excluded.api_key_enc, accounts.api_key_enc),
                    access_token_enc = COALESCE(excluded.access_token_enc, accounts.access_token_enc),
                    user_id_enc = COALESCE(excluded.user_id_enc, accounts.user_id_enc),
                    threshold = excluded.threshold,
                    is_enabled = excluded.is_enabled,
                    updated_at = excluded.updated_at
                """,
                (
                    platform,
                    data["name"],
                    data["base_url"].rstrip("/"),
                    api_key_enc,
                    access_token_enc,
                    user_id_enc,
                    threshold,
                    is_enabled,
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
        api_key = data.get("api_key")
        access_token = data.get("access_token")
        if platform == "newApi" and not access_token and api_key:
            access_token = api_key
        api_key_enc = encrypt_value(api_key, self.secret_key)
        access_token_enc = encrypt_value(access_token, self.secret_key)
        user_id_enc = encrypt_value(data.get("user_id"), self.secret_key)
        threshold = _optional_float(data.get("threshold"))
        is_enabled = 1 if data.get("is_enabled", True) else 0
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE accounts
                SET platform = ?, name = ?, base_url = ?,
                    api_key_enc = COALESCE(?, api_key_enc),
                    access_token_enc = COALESCE(?, access_token_enc),
                    user_id_enc = COALESCE(?, user_id_enc),
                    threshold = ?, is_enabled = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    platform,
                    data["name"],
                    data["base_url"].rstrip("/"),
                    api_key_enc,
                    access_token_enc,
                    user_id_enc,
                    threshold,
                    is_enabled,
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
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE accounts
                SET last_status = ?, last_error = ?, last_plan_name = ?, last_remaining = ?,
                    last_unit = ?, last_total = ?, last_used = ?, last_extra = ?,
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
                    result.get("extra"),
                    utc_now(),
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
                    utc_now(),
                ),
            )

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

    def list_logs(self) -> list[sqlite3.Row]:
        self.cleanup_logs()
        with self.connect() as conn:
            return conn.execute("SELECT * FROM app_logs ORDER BY created_at DESC, id DESC").fetchall()

    def cleanup_logs(self) -> None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
        with self.connect() as conn:
            conn.execute("DELETE FROM app_logs WHERE created_at < ?", (cutoff,))


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _optional_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    return float(value)

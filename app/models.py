from __future__ import annotations

import calendar
import hashlib
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from app.defaults import DEFAULT_ACCOUNTS
from app.security import decrypt_value, encrypt_value, hash_password


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


CHINA_TZ = timezone(timedelta(hours=8))
DEFAULT_BALANCE_UNIT = "USD"
BALANCE_HISTORY_MONTHS = 9
BALANCE_TREND_DAYS = 3
CONSUMPTION_QUERY_BATCH_SIZE = 500
REQUEST_TIMEOUT_SECONDS = 60
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


def _like_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


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
                    recharge_paid_amount REAL NOT NULL DEFAULT 1,
                    recharge_received_amount REAL NOT NULL DEFAULT 1,
                    key_id_enc TEXT,
                    api_key_enc TEXT,
                    email_enc TEXT,
                    password_enc TEXT,
                    login_extra_params_enc TEXT,
                    access_token_enc TEXT,
                    refresh_token_enc TEXT,
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
                    monitor_group_id INTEGER REFERENCES account_monitor_groups(id) ON DELETE CASCADE,
                    plan_name TEXT NOT NULL,
                    rate_multiplier REAL,
                    raw_json TEXT NOT NULL,
                    checked_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS account_monitor_groups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                    group_id_enc TEXT NOT NULL,
                    group_id_hash TEXT NOT NULL,
                    plan_name TEXT,
                    name TEXT,
                    default_rate_multiplier REAL,
                    user_rate_multiplier REAL,
                    effective_rate_multiplier REAL,
                    raw_json TEXT,
                    last_checked_at TEXT,
                    last_group_rate_changed INTEGER NOT NULL DEFAULT 0,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(account_id, group_id_hash)
                );

                CREATE TABLE IF NOT EXISTS app_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    level TEXT NOT NULL,
                    category TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    remind_at TEXT NOT NULL,
                    is_sent INTEGER NOT NULL DEFAULT 0,
                    sent_at TEXT,
                    last_error TEXT,
                    last_attempt_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS opencode_go_accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    email_enc TEXT NOT NULL,
                    password_enc TEXT,
                    recovery_email_enc TEXT,
                    storage_state_enc TEXT,
                    workspace_id TEXT,
                    api_key_enc TEXT,
                    api_key_masked TEXT,
                    last_status TEXT NOT NULL DEFAULT 'never',
                    last_error TEXT,
                    last_rolling_usage TEXT,
                    last_weekly_usage TEXT,
                    last_monthly_usage TEXT,
                    last_raw_json TEXT,
                    last_checked_at TEXT,
                    is_enabled INTEGER NOT NULL DEFAULT 0,
                    cpa_provider_disabled INTEGER,
                    cpa_provider_deleted INTEGER NOT NULL DEFAULT 0,
                    cpa_deleted_at TEXT,
                    cpa_reenable_pending INTEGER NOT NULL DEFAULT 0,
                    cpa_last_action_at TEXT,
                    cpa_last_action_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS opencode_go_usage_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL REFERENCES opencode_go_accounts(id) ON DELETE CASCADE,
                    is_valid INTEGER NOT NULL,
                    rolling_usage TEXT,
                    weekly_usage TEXT,
                    monthly_usage TEXT,
                    api_key_masked TEXT,
                    raw_json TEXT,
                    error TEXT,
                    checked_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_app_logs_created_at ON app_logs(created_at);
                CREATE INDEX IF NOT EXISTS idx_reminders_due
                ON reminders(is_sent, remind_at);
                CREATE INDEX IF NOT EXISTS idx_group_rate_records_account_checked_at
                ON group_rate_records(account_id, checked_at DESC);
                CREATE INDEX IF NOT EXISTS idx_account_monitor_groups_account_sort
                ON account_monitor_groups(account_id, sort_order, id);
                CREATE INDEX IF NOT EXISTS idx_query_records_account_checked_at
                ON query_records(account_id, checked_at DESC);
                CREATE INDEX IF NOT EXISTS idx_opencode_go_usage_account_checked_at
                ON opencode_go_usage_records(account_id, checked_at DESC);
                """
            )
            self._migrate_smtp_nullable(conn)
            self._migrate_users_session_version(conn)
            self._migrate_accounts_key_id(conn)
            self._migrate_accounts_sub2api_login(conn)
            self._migrate_accounts_login_extra_params(conn)
            self._migrate_accounts_refresh_token(conn)
            self._migrate_accounts_note(conn)
            self._migrate_accounts_recharge_url(conn)
            self._migrate_accounts_recharge_ratio(conn)
            self._migrate_accounts_group_rate_changed(conn)
            self._migrate_accounts_visible(conn)
            self._migrate_accounts_eliminated(conn)
            self._migrate_opencode_go_recovery_email(conn)
            self._migrate_opencode_go_cpa_state(conn)
            self._migrate_group_rate_records_monitor_group(conn)
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_group_rate_records_monitor_checked_at
                ON group_rate_records(monitor_group_id, checked_at DESC)
                """
            )
            self._migrate_legacy_selected_groups(conn)
            self._set_default(conn, "request_timeout", str(REQUEST_TIMEOUT_SECONDS))
            self._upgrade_request_timeout_default(conn)
            self._set_default(conn, "query_interval", str(BALANCE_QUERY_INTERVAL_SECONDS))
            self._set_default(conn, "group_rate_query_interval", str(GROUP_RATE_QUERY_INTERVAL_SECONDS))
            self._set_default(conn, "default_threshold", "5")
            self._set_default(conn, "monitor_paused", "0")
            self._set_default(conn, "opencode_go_cpa_auto_delete_enabled", "0")
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
    def _upgrade_request_timeout_default(conn: sqlite3.Connection) -> None:
        conn.execute(
            "UPDATE settings SET value = ? WHERE key = 'request_timeout' AND value = '15'",
            (str(REQUEST_TIMEOUT_SECONDS),),
        )

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
    def _migrate_accounts_login_extra_params(conn: sqlite3.Connection) -> None:
        columns = conn.execute("PRAGMA table_info(accounts)").fetchall()
        column_names = {row["name"] for row in columns}
        if "login_extra_params_enc" not in column_names:
            conn.execute("ALTER TABLE accounts ADD COLUMN login_extra_params_enc TEXT")

    @staticmethod
    def _migrate_accounts_refresh_token(conn: sqlite3.Connection) -> None:
        columns = conn.execute("PRAGMA table_info(accounts)").fetchall()
        column_names = {row["name"] for row in columns}
        if "refresh_token_enc" not in column_names:
            conn.execute("ALTER TABLE accounts ADD COLUMN refresh_token_enc TEXT")

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
    def _migrate_accounts_recharge_ratio(conn: sqlite3.Connection) -> None:
        columns = conn.execute("PRAGMA table_info(accounts)").fetchall()
        column_names = {row["name"] for row in columns}
        if "recharge_paid_amount" not in column_names:
            conn.execute("ALTER TABLE accounts ADD COLUMN recharge_paid_amount REAL NOT NULL DEFAULT 1")
        if "recharge_received_amount" not in column_names:
            conn.execute("ALTER TABLE accounts ADD COLUMN recharge_received_amount REAL NOT NULL DEFAULT 1")

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

    def _migrate_opencode_go_recovery_email(self, conn: sqlite3.Connection) -> None:
        columns = conn.execute("PRAGMA table_info(opencode_go_accounts)").fetchall()
        column_names = {row["name"] for row in columns}
        if "recovery_email_enc" not in column_names:
            conn.execute("ALTER TABLE opencode_go_accounts ADD COLUMN recovery_email_enc TEXT")
        rows = conn.execute("SELECT id, name, email_enc FROM opencode_go_accounts ORDER BY id").fetchall()
        seen_emails: set[str] = set()
        for row in rows:
            try:
                email = str(decrypt_value(row["email_enc"], self.secret_key) or "").strip()
            except Exception:
                continue
            if not email or email in seen_emails or email == row["name"]:
                continue
            seen_emails.add(email)
            conn.execute(
                """
                UPDATE opencode_go_accounts
                SET name = ?, updated_at = ?
                WHERE id = ?
                  AND NOT EXISTS (
                    SELECT 1 FROM opencode_go_accounts AS existing
                    WHERE existing.name = ? AND existing.id != ?
                  )
                """,
                (email, utc_now(), row["id"], email, row["id"]),
            )

    @staticmethod
    def _migrate_opencode_go_cpa_state(conn: sqlite3.Connection) -> None:
        columns = conn.execute("PRAGMA table_info(opencode_go_accounts)").fetchall()
        column_names = {row["name"] for row in columns}
        if "cpa_provider_disabled" not in column_names:
            conn.execute("ALTER TABLE opencode_go_accounts ADD COLUMN cpa_provider_disabled INTEGER")
        if "cpa_provider_deleted" not in column_names:
            conn.execute("ALTER TABLE opencode_go_accounts ADD COLUMN cpa_provider_deleted INTEGER NOT NULL DEFAULT 0")
        if "cpa_deleted_at" not in column_names:
            conn.execute("ALTER TABLE opencode_go_accounts ADD COLUMN cpa_deleted_at TEXT")
        if "cpa_reenable_pending" not in column_names:
            conn.execute("ALTER TABLE opencode_go_accounts ADD COLUMN cpa_reenable_pending INTEGER NOT NULL DEFAULT 0")
        if "cpa_last_action_at" not in column_names:
            conn.execute("ALTER TABLE opencode_go_accounts ADD COLUMN cpa_last_action_at TEXT")
        if "cpa_last_action_error" not in column_names:
            conn.execute("ALTER TABLE opencode_go_accounts ADD COLUMN cpa_last_action_error TEXT")

    @staticmethod
    def _migrate_group_rate_records_monitor_group(conn: sqlite3.Connection) -> None:
        columns = conn.execute("PRAGMA table_info(group_rate_records)").fetchall()
        column_names = {row["name"] for row in columns}
        if "monitor_group_id" not in column_names:
            conn.execute("ALTER TABLE group_rate_records ADD COLUMN monitor_group_id INTEGER REFERENCES account_monitor_groups(id) ON DELETE CASCADE")

    def _migrate_legacy_selected_groups(self, conn: sqlite3.Connection) -> None:
        existing = conn.execute("SELECT COUNT(*) AS count FROM account_monitor_groups").fetchone()["count"]
        if existing:
            return
        accounts = conn.execute(
            """
            SELECT id, key_id_enc
            FROM accounts
            WHERE key_id_enc IS NOT NULL AND key_id_enc != ''
            ORDER BY id
            """
        ).fetchall()
        now = utc_now()
        for account in accounts:
            try:
                group_id = decrypt_value(account["key_id_enc"], self.secret_key)
            except Exception:
                group_id = None
            if not group_id:
                continue
            conn.execute(
                """
                INSERT OR IGNORE INTO account_monitor_groups (
                    account_id, group_id_enc, group_id_hash, plan_name, name, sort_order, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    account["id"],
                    encrypt_value(group_id, self.secret_key),
                    _hash_group_id(group_id),
                    f"当前分组 {group_id}",
                    group_id,
                    now,
                    now,
                ),
            )

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
            "request_timeout": float(values.get("request_timeout", str(REQUEST_TIMEOUT_SECONDS))),
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

    def get_setting(self, key: str, default: str = "") -> str:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.connect() as conn:
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

    def list_accounts(
        self,
        platform: Optional[str] = None,
        name_query: Optional[str] = None,
        enabled_only: bool = False,
        visible_only: bool = False,
    ) -> list[sqlite3.Row]:
        query = "SELECT * FROM accounts"
        conditions = []
        params: list[Any] = []
        if platform:
            conditions.append("platform = ?")
            params.append(platform)
        if name_query:
            conditions.append("name LIKE ? ESCAPE '\\'")
            params.append(_like_pattern(name_query))
        if enabled_only:
            conditions.append("is_enabled = 1")
        if visible_only:
            conditions.append("is_visible = 1")
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY platform, name"
        with self.connect() as conn:
            return conn.execute(query, tuple(params)).fetchall()

    def list_account_summaries(
        self,
        platform: Optional[str] = None,
        name_query: Optional[str] = None,
    ) -> list[sqlite3.Row]:
        query = """
            SELECT
                id, platform, name, base_url, note, recharge_url,
                recharge_paid_amount, recharge_received_amount, key_id_enc,
                api_key_enc, email_enc, password_enc, access_token_enc,
                refresh_token_enc, user_id_enc, threshold, is_enabled, is_visible
            FROM accounts
        """
        conditions = []
        params: list[Any] = []
        if platform:
            conditions.append("platform = ?")
            params.append(platform)
        if name_query:
            conditions.append("name LIKE ? ESCAPE '\\'")
            params.append(_like_pattern(name_query))
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY platform, name"
        with self.connect() as conn:
            return conn.execute(query, tuple(params)).fetchall()

    def list_dashboard_accounts(
        self,
        platform: Optional[str] = None,
        name_query: Optional[str] = None,
        visible_only: bool = False,
    ) -> list[sqlite3.Row]:
        query = """
            SELECT
                id, platform, name, base_url, note, recharge_url,
                recharge_paid_amount, recharge_received_amount, key_id_enc,
                threshold, is_enabled, is_visible, is_eliminated,
                last_status, last_remaining, last_unit, last_total, last_used,
                last_extra, last_group_rate_changed, last_checked_at
            FROM accounts
        """
        conditions = []
        params: list[Any] = []
        if platform:
            conditions.append("platform = ?")
            params.append(platform)
        if name_query:
            conditions.append("name LIKE ? ESCAPE '\\'")
            params.append(_like_pattern(name_query))
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

    def list_monitor_groups(self, account_id: int) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT *
                FROM account_monitor_groups
                WHERE account_id = ?
                ORDER BY sort_order ASC, id ASC
                """,
                (account_id,),
            ).fetchall()

    def list_dashboard_monitor_groups(self, account_id: int) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT
                    id, group_id_enc, plan_name, name, effective_rate_multiplier,
                    last_group_rate_changed, sort_order
                FROM account_monitor_groups
                WHERE account_id = ?
                ORDER BY sort_order ASC, id ASC
                """,
                (account_id,),
            ).fetchall()

    def get_monitor_group(self, monitor_group_id: int) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM account_monitor_groups WHERE id = ?",
                (monitor_group_id,),
            ).fetchone()

    def get_monitor_group_by_group_id(self, account_id: int, group_id: str) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT *
                FROM account_monitor_groups
                WHERE account_id = ? AND group_id_hash = ?
                """,
                (account_id, _hash_group_id(group_id)),
            ).fetchone()

    def replace_account_monitor_groups(self, account_id: int, groups: list[dict[str, Any]] | list[str]) -> None:
        normalized = _normalize_monitor_groups(groups)
        now = utc_now()
        with self.connect() as conn:
            account = conn.execute("SELECT id FROM accounts WHERE id = ?", (account_id,)).fetchone()
            if not account:
                raise ValueError("账号不存在")
            existing_rows = conn.execute(
                """
                SELECT *
                FROM account_monitor_groups
                WHERE account_id = ?
                ORDER BY sort_order ASC, id ASC
                """,
                (account_id,),
            ).fetchall()
            existing_by_hash = {row["group_id_hash"]: row for row in existing_rows}
            normalized_by_hash = {_hash_group_id(item["group_id"]): item for item in normalized}
            keep_hashes = set(normalized_by_hash)
            existing_hashes = set(existing_by_hash)
            added_hashes = keep_hashes - existing_hashes
            removed_hashes = existing_hashes - keep_hashes

            if not added_hashes and not removed_hashes:
                return

            if removed_hashes:
                placeholders = ",".join("?" for _ in removed_hashes)
                conn.execute(
                    f"""
                    DELETE FROM account_monitor_groups
                    WHERE account_id = ? AND group_id_hash IN ({placeholders})
                    """,
                    (account_id, *removed_hashes),
                )

            next_sort_order = max((int(row["sort_order"]) for row in existing_rows), default=-1) + 1
            for sort_order, item in enumerate(normalized):
                group_id = item["group_id"]
                group_id_hash = _hash_group_id(group_id)
                if group_id_hash not in added_hashes:
                    continue
                summary = _monitor_group_summary(item)
                conn.execute(
                    """
                    INSERT INTO account_monitor_groups (
                        account_id, group_id_enc, group_id_hash, plan_name, name,
                        default_rate_multiplier, user_rate_multiplier, effective_rate_multiplier,
                        raw_json, sort_order, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        account_id,
                        encrypt_value(group_id, self.secret_key),
                        group_id_hash,
                        summary["plan_name"],
                        summary["name"],
                        summary["default_rate_multiplier"],
                        summary["user_rate_multiplier"],
                        summary["effective_rate_multiplier"],
                        summary["raw_json"],
                        next_sort_order,
                        now,
                        now,
                    ),
                )
                next_sort_order += 1
            kept_hashes_in_order = [
                row["group_id_hash"]
                for row in existing_rows
                if row["group_id_hash"] in keep_hashes and row["group_id_hash"] not in removed_hashes
            ]
            if kept_hashes_in_order:
                first_group = normalized_by_hash[kept_hashes_in_order[0]]["group_id"]
            else:
                first_group = normalized[0]["group_id"] if normalized else None
            conn.execute(
                """
                UPDATE accounts
                SET key_id_enc = ?, last_group_rate_changed = (
                    SELECT CASE WHEN COUNT(*) > 0 THEN 1 ELSE 0 END
                    FROM account_monitor_groups
                    WHERE account_id = accounts.id AND last_group_rate_changed = 1
                ), updated_at = ?
                WHERE id = ?
                """,
                (encrypt_value(first_group, self.secret_key), now, account_id),
            )

    def update_monitor_group_snapshot(self, monitor_group_id: int, group_summary: dict[str, Any], checked_at: str) -> None:
        group = group_summary.get("group") if isinstance(group_summary.get("group"), dict) else group_summary
        if not isinstance(group, dict):
            group = {}
        summary = _monitor_group_summary(group)
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE account_monitor_groups
                SET plan_name = COALESCE(?, plan_name),
                    name = COALESCE(?, name),
                    default_rate_multiplier = ?,
                    user_rate_multiplier = ?,
                    effective_rate_multiplier = ?,
                    raw_json = ?,
                    last_checked_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    summary["plan_name"],
                    summary["name"],
                    summary["default_rate_multiplier"],
                    summary["user_rate_multiplier"],
                    summary["effective_rate_multiplier"],
                    summary["raw_json"] or _json_dumps(group_summary),
                    checked_at,
                    utc_now(),
                    monitor_group_id,
                ),
            )

    def upsert_account(self, data: dict[str, Any]) -> int:
        now = utc_now()
        platform = data["platform"]
        note = str(data.get("note") or "").strip()
        recharge_url = str(data.get("recharge_url") or "").strip()
        recharge_paid_amount, recharge_received_amount = _recharge_ratio_values(data)
        key_id = data.get("key_id")
        api_key = data.get("api_key")
        email = data.get("email")
        password = data.get("password")
        login_extra_params_present = "login_extra_params" in data
        login_extra_params = data.get("login_extra_params")
        access_token = data.get("access_token")
        refresh_token = data.get("refresh_token")
        if platform == "newApi" and not access_token and api_key:
            access_token = api_key
        key_id_enc = encrypt_value(key_id, self.secret_key)
        api_key_enc = encrypt_value(api_key, self.secret_key)
        email_enc = encrypt_value(email, self.secret_key)
        password_enc = encrypt_value(password, self.secret_key)
        login_extra_params_enc = encrypt_value(login_extra_params, self.secret_key)
        access_token_enc = encrypt_value(access_token, self.secret_key)
        refresh_token_enc = encrypt_value(refresh_token, self.secret_key)
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
                    platform, name, base_url, note, recharge_url, recharge_paid_amount, recharge_received_amount,
                    key_id_enc, api_key_enc, email_enc, password_enc, login_extra_params_enc,
                    access_token_enc, refresh_token_enc, user_id_enc,
                    threshold, is_enabled, is_visible, is_eliminated, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(platform, name) DO UPDATE SET
                    base_url = excluded.base_url,
                    note = excluded.note,
                    recharge_url = excluded.recharge_url,
                    recharge_paid_amount = excluded.recharge_paid_amount,
                    recharge_received_amount = excluded.recharge_received_amount,
                    key_id_enc = COALESCE(excluded.key_id_enc, accounts.key_id_enc),
                    api_key_enc = COALESCE(excluded.api_key_enc, accounts.api_key_enc),
                    email_enc = COALESCE(excluded.email_enc, accounts.email_enc),
                    password_enc = COALESCE(excluded.password_enc, accounts.password_enc),
                    login_extra_params_enc = CASE
                        WHEN ? THEN excluded.login_extra_params_enc
                        ELSE accounts.login_extra_params_enc
                    END,
                    access_token_enc = COALESCE(excluded.access_token_enc, accounts.access_token_enc),
                    refresh_token_enc = COALESCE(excluded.refresh_token_enc, accounts.refresh_token_enc),
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
                    recharge_paid_amount,
                    recharge_received_amount,
                    key_id_enc,
                    api_key_enc,
                    email_enc,
                    password_enc,
                    login_extra_params_enc,
                    access_token_enc,
                    refresh_token_enc,
                    user_id_enc,
                    threshold,
                    is_enabled,
                    is_visible,
                    effective_is_eliminated,
                    now,
                    now,
                    1 if login_extra_params_present else 0,
                ),
            )
            row = conn.execute(
                "SELECT id FROM accounts WHERE platform = ? AND name = ?",
                (platform, data["name"]),
            ).fetchone()
            account_id = int(row["id"])
        if _should_replace_monitor_groups(data, allow_key_id_fallback=True):
            self.replace_account_monitor_groups(account_id, _groups_from_account_data(data))
        return account_id

    def update_account(self, account_id: int, data: dict[str, Any]) -> int:
        current = self.get_account(account_id)
        if not current:
            raise ValueError("账号不存在")
        merged = self._merge_account_patch(current, data)
        now = utc_now()
        platform = merged["platform"]
        note = str(merged.get("note") or "").strip()
        recharge_url = str(merged.get("recharge_url") or "").strip()
        recharge_paid_amount, recharge_received_amount = _recharge_ratio_values(merged)
        key_id = merged.get("key_id")
        api_key = merged.get("api_key")
        email = merged.get("email")
        password = merged.get("password")
        login_extra_params_present = "login_extra_params" in data
        login_extra_params = merged.get("login_extra_params")
        access_token = merged.get("access_token")
        refresh_token = merged.get("refresh_token")
        if platform == "newApi" and not access_token and api_key:
            access_token = api_key
        key_id_enc = encrypt_value(key_id, self.secret_key)
        api_key_enc = encrypt_value(api_key, self.secret_key)
        email_enc = encrypt_value(email, self.secret_key)
        password_enc = encrypt_value(password, self.secret_key)
        login_extra_params_enc = encrypt_value(login_extra_params, self.secret_key)
        access_token_enc = encrypt_value(access_token, self.secret_key)
        refresh_token_enc = encrypt_value(refresh_token, self.secret_key)
        user_id_enc = encrypt_value(merged.get("user_id"), self.secret_key)
        threshold = _optional_float(merged.get("threshold"))
        is_visible = 1 if merged.get("is_visible", current["is_visible"]) else 0
        is_enabled = 1 if merged.get("is_enabled", current["is_enabled"]) and is_visible else 0
        is_eliminated = _optional_bool(merged.get("is_eliminated"))
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE accounts
                SET platform = ?, name = ?, base_url = ?, note = ?, recharge_url = ?,
                    recharge_paid_amount = ?, recharge_received_amount = ?,
                    key_id_enc = COALESCE(?, key_id_enc),
                    api_key_enc = COALESCE(?, api_key_enc),
                    email_enc = COALESCE(?, email_enc),
                    password_enc = COALESCE(?, password_enc),
                    login_extra_params_enc = CASE WHEN ? THEN ? ELSE login_extra_params_enc END,
                    access_token_enc = COALESCE(?, access_token_enc),
                    refresh_token_enc = COALESCE(?, refresh_token_enc),
                    user_id_enc = COALESCE(?, user_id_enc),
                    threshold = ?, is_enabled = ?, is_visible = ?, is_eliminated = COALESCE(?, is_eliminated), updated_at = ?
                WHERE id = ?
                """,
                (
                    platform,
                    merged["name"],
                    merged["base_url"].rstrip("/"),
                    note,
                    recharge_url,
                    recharge_paid_amount,
                    recharge_received_amount,
                    key_id_enc,
                    api_key_enc,
                    email_enc,
                    password_enc,
                    1 if login_extra_params_present else 0,
                    login_extra_params_enc,
                    access_token_enc,
                    refresh_token_enc,
                    user_id_enc,
                    threshold,
                    is_enabled,
                    is_visible,
                    is_eliminated,
                    now,
                    account_id,
                ),
            )
        if _should_replace_monitor_groups(merged):
            self.replace_account_monitor_groups(account_id, _groups_from_account_data(merged))
        return account_id

    def _merge_account_patch(self, current: sqlite3.Row, data: dict[str, Any]) -> dict[str, Any]:
        merged = row_to_dict(current)
        merged.update(data)
        merged["platform"] = str(data.get("platform") or merged.get("platform") or "").strip()
        merged["name"] = str(data.get("name") or merged.get("name") or "").strip()
        merged["base_url"] = str(data.get("base_url") or merged.get("base_url") or "").strip()
        merged["note"] = str(data.get("note") if "note" in data else merged.get("note") or "").strip()
        merged["recharge_url"] = str(data.get("recharge_url") if "recharge_url" in data else merged.get("recharge_url") or "").strip()
        merged["recharge_paid_amount"] = data.get("recharge_paid_amount", merged.get("recharge_paid_amount"))
        merged["recharge_received_amount"] = data.get("recharge_received_amount", merged.get("recharge_received_amount"))
        for key in ("key_id", "api_key", "email", "password", "login_extra_params", "access_token", "refresh_token", "user_id", "threshold", "is_enabled", "is_visible", "is_eliminated"):
            if key in data:
                merged[key] = data.get(key)
        if "monitor_groups" in data:
            merged["monitor_groups"] = data.get("monitor_groups")
        if "monitor_group_ids" in data:
            merged["monitor_group_ids"] = data.get("monitor_group_ids")
        return merged

    def update_account_tokens(
        self,
        account_id: int,
        access_token: str | None = None,
        refresh_token: str | None = None,
    ) -> None:
        access_token_enc = encrypt_value(access_token, self.secret_key)
        refresh_token_enc = encrypt_value(refresh_token, self.secret_key)
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE accounts
                SET access_token_enc = COALESCE(?, access_token_enc),
                    refresh_token_enc = COALESCE(?, refresh_token_enc),
                    updated_at = ?
                WHERE id = ?
                """,
                (access_token_enc, refresh_token_enc, utc_now(), account_id),
            )

    def delete_account(self, account_id: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))

    def list_opencode_go_accounts(
        self,
        enabled_only: bool = False,
        email: str = "",
        weekly_usage_gte_99: bool = False,
        monthly_usage_gte_99: bool = False,
        limit: int | None = None,
        offset: int = 0,
        sort_by: str = "name",
        sort_order: str = "asc",
        status: str = "",
    ) -> list[sqlite3.Row]:
        query = "SELECT * FROM opencode_go_accounts"
        params: list[Any] = []
        conditions: list[str] = []
        if enabled_only:
            conditions.append("is_enabled = 1")
        if email:
            escaped_email = email.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            conditions.append("name LIKE ? ESCAPE '\\' COLLATE NOCASE")
            params.append(f"%{escaped_email}%")
        _append_opencode_go_status_filter(conditions, params, status)
        if weekly_usage_gte_99:
            conditions.append(_opencode_go_usage_filter("last_weekly_usage"))
        if monthly_usage_gte_99:
            conditions.append(_opencode_go_usage_filter("last_monthly_usage"))
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        sort_columns = {
            "name": "name",
            "created_at": "created_at",
            "updated_at": "updated_at",
            "last_checked_at": "last_checked_at",
        }
        column = sort_columns.get(str(sort_by or "").strip(), "name")
        direction = "DESC" if str(sort_order or "").strip().lower() == "desc" else "ASC"
        if column == "created_at":
            query += f" ORDER BY created_at {direction}, id {direction}"
        elif column == "last_checked_at":
            query += f" ORDER BY last_checked_at {direction}, id {direction}"
        else:
            query += f" ORDER BY {column} {direction}, id {direction}"
        if limit is not None:
            query += " LIMIT ? OFFSET ?"
            params.extend([max(1, int(limit)), max(0, int(offset))])
        with self.connect() as conn:
            return conn.execute(query, tuple(params)).fetchall()

    def count_opencode_go_accounts(
        self,
        enabled_only: bool = False,
        email: str = "",
        weekly_usage_gte_99: bool = False,
        monthly_usage_gte_99: bool = False,
        status: str = "",
    ) -> int:
        query = "SELECT COUNT(*) AS count FROM opencode_go_accounts"
        params: list[Any] = []
        conditions: list[str] = []
        if enabled_only:
            conditions.append("is_enabled = 1")
        if email:
            escaped_email = email.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            conditions.append("name LIKE ? ESCAPE '\\' COLLATE NOCASE")
            params.append(f"%{escaped_email}%")
        _append_opencode_go_status_filter(conditions, params, status)
        if weekly_usage_gte_99:
            conditions.append(_opencode_go_usage_filter("last_weekly_usage"))
        if monthly_usage_gte_99:
            conditions.append(_opencode_go_usage_filter("last_monthly_usage"))
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        with self.connect() as conn:
            row = conn.execute(query, tuple(params)).fetchone()
            return int(row["count"] if row else 0)

    def latest_successful_opencode_go_checked_at(self) -> str | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT last_checked_at
                FROM opencode_go_accounts
                WHERE last_status = 'valid' AND last_checked_at IS NOT NULL
                ORDER BY last_checked_at DESC, id DESC
                LIMIT 1
                """
            ).fetchone()
            return str(row["last_checked_at"]) if row and row["last_checked_at"] else None

    def get_opencode_go_account(self, account_id: int) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM opencode_go_accounts WHERE id = ?", (account_id,)).fetchone()

    def upsert_opencode_go_account(self, data: dict[str, Any]) -> int:
        now = utc_now()
        email = str(data.get("email") or "").strip()
        if not email:
            raise ValueError("Google 邮箱不能为空")
        name = email
        recovery_email_enc = encrypt_value(str(data.get("recovery_email") or data.get("recoveryEmail") or "").strip(), self.secret_key)
        password_enc = encrypt_value(data.get("password"), self.secret_key)
        storage_state_enc = encrypt_value(_json_dumps(data.get("storage_state")), self.secret_key) if data.get("storage_state") else None
        api_key = str(data.get("api_key") or "").strip()
        api_key_enc = encrypt_value(api_key, self.secret_key)
        api_key_masked = _mask_opencode_api_key(api_key) if api_key else data.get("api_key_masked")
        is_enabled = 1 if data.get("is_enabled", False) else 0
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO opencode_go_accounts (
                    name, email_enc, password_enc, recovery_email_enc, storage_state_enc, workspace_id,
                    api_key_enc, api_key_masked, is_enabled, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    email_enc = excluded.email_enc,
                    password_enc = COALESCE(excluded.password_enc, opencode_go_accounts.password_enc),
                    recovery_email_enc = excluded.recovery_email_enc,
                    storage_state_enc = COALESCE(excluded.storage_state_enc, opencode_go_accounts.storage_state_enc),
                    workspace_id = COALESCE(excluded.workspace_id, opencode_go_accounts.workspace_id),
                    api_key_enc = COALESCE(excluded.api_key_enc, opencode_go_accounts.api_key_enc),
                    api_key_masked = COALESCE(excluded.api_key_masked, opencode_go_accounts.api_key_masked),
                    is_enabled = excluded.is_enabled,
                    updated_at = excluded.updated_at
                """,
                (
                    name,
                    encrypt_value(email, self.secret_key),
                    password_enc,
                    recovery_email_enc,
                    storage_state_enc,
                    data.get("workspace_id") or data.get("workspaceId"),
                    api_key_enc,
                    api_key_masked,
                    is_enabled,
                    now,
                    now,
                ),
            )
            row = conn.execute("SELECT id FROM opencode_go_accounts WHERE name = ?", (name,)).fetchone()
            return int(row["id"])

    def update_opencode_go_account(self, account_id: int, data: dict[str, Any]) -> int:
        current = self.get_opencode_go_account(account_id)
        if not current:
            raise ValueError("OpenCode Go 账号不存在")
        email = str(data.get("email") or decrypt_value(current["email_enc"], self.secret_key) or "").strip()
        if not email:
            raise ValueError("Google 邮箱不能为空")
        name = email
        recovery_email_enc = encrypt_value(str(data.get("recovery_email") or data.get("recoveryEmail") or "").strip(), self.secret_key)
        password_enc = encrypt_value(data.get("password"), self.secret_key)
        storage_state_enc = None
        if "storage_state" in data:
            storage_state_enc = encrypt_value(_json_dumps(data.get("storage_state")), self.secret_key) if data.get("storage_state") else None
        api_key = str(data.get("api_key") or "").strip()
        api_key_enc = encrypt_value(api_key, self.secret_key)
        api_key_masked = _mask_opencode_api_key(api_key) if api_key else data.get("api_key_masked")
        is_enabled = 1 if data.get("is_enabled", current["is_enabled"]) else 0
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE opencode_go_accounts
                SET name = ?, email_enc = ?,
                    password_enc = COALESCE(?, password_enc),
                    recovery_email_enc = ?,
                    storage_state_enc = CASE WHEN ? THEN ? ELSE storage_state_enc END,
                    workspace_id = COALESCE(?, workspace_id),
                    api_key_enc = COALESCE(?, api_key_enc),
                    api_key_masked = COALESCE(?, api_key_masked),
                    is_enabled = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    name,
                    encrypt_value(email, self.secret_key),
                    password_enc,
                    recovery_email_enc,
                    1 if "storage_state" in data else 0,
                    storage_state_enc,
                    data.get("workspace_id") or data.get("workspaceId"),
                    api_key_enc,
                    api_key_masked,
                    is_enabled,
                    utc_now(),
                    account_id,
                ),
            )
        return account_id

    def delete_opencode_go_account(self, account_id: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM opencode_go_accounts WHERE id = ?", (account_id,))

    def update_opencode_go_session(
        self,
        account_id: int,
        storage_state: Any,
        workspace_id: str | None = None,
        status: str = "logged_in",
        error: str | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE opencode_go_accounts
                SET storage_state_enc = ?,
                    workspace_id = COALESCE(?, workspace_id),
                    last_status = ?,
                    last_error = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    encrypt_value(_json_dumps(storage_state), self.secret_key),
                    workspace_id,
                    status,
                    error,
                    utc_now(),
                    account_id,
                ),
            )

    def update_opencode_go_enabled(self, account_id: int, is_enabled: bool) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE opencode_go_accounts SET is_enabled = ?, updated_at = ? WHERE id = ?",
                (1 if is_enabled else 0, utc_now(), account_id),
            )

    def update_opencode_go_result(self, account_id: int, result: dict[str, Any]) -> None:
        checked_at = result.get("checked_at") or utc_now()
        is_valid = bool(result.get("is_valid"))
        rolling_usage = _json_dumps(result.get("rolling_usage") or result.get("rollingUsage") or {})
        weekly_usage = _json_dumps(result.get("weekly_usage") or result.get("weeklyUsage") or {})
        monthly_usage = _json_dumps(result.get("monthly_usage") or result.get("monthlyUsage") or {})
        raw_json = _json_dumps(_safe_opencode_raw(result.get("raw") or result.get("raw_json") or result))
        api_key = str(result.get("api_key") or result.get("apiKey") or "").strip()
        api_key_masked = result.get("api_key_masked") or result.get("apiKeyMasked") or (_mask_opencode_api_key(api_key) if api_key else None)
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE opencode_go_accounts
                SET last_status = ?, last_error = ?, workspace_id = COALESCE(?, workspace_id),
                    api_key_enc = COALESCE(?, api_key_enc),
                    api_key_masked = COALESCE(?, api_key_masked),
                    last_rolling_usage = ?, last_weekly_usage = ?, last_monthly_usage = ?,
                    last_raw_json = ?, last_checked_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    "valid" if is_valid else "invalid",
                    result.get("invalid_message") or result.get("error"),
                    result.get("workspace_id") or result.get("workspaceId"),
                    encrypt_value(api_key, self.secret_key),
                    api_key_masked,
                    rolling_usage,
                    weekly_usage,
                    monthly_usage,
                    raw_json,
                    checked_at,
                    utc_now(),
                    account_id,
                ),
            )
            conn.execute(
                """
                INSERT INTO opencode_go_usage_records (
                    account_id, is_valid, rolling_usage, weekly_usage, monthly_usage,
                    api_key_masked, raw_json, error, checked_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account_id,
                    1 if is_valid else 0,
                    rolling_usage,
                    weekly_usage,
                    monthly_usage,
                    api_key_masked,
                    raw_json,
                    result.get("invalid_message") or result.get("error"),
                    checked_at,
                ),
            )

    def update_opencode_go_cpa_state(
        self,
        account_id: int,
        *,
        provider_disabled: bool | None = None,
        provider_deleted: bool | None = None,
        deleted_at: str | None = None,
        clear_deleted_at: bool = False,
        reenable_pending: bool | None = None,
        action_at: str | None = None,
        error: str | None = None,
        clear_error: bool = False,
    ) -> None:
        assignments = ["updated_at = ?"]
        params: list[Any] = [utc_now()]
        if provider_disabled is not None:
            assignments.append("cpa_provider_disabled = ?")
            params.append(1 if provider_disabled else 0)
        if provider_deleted is not None:
            assignments.append("cpa_provider_deleted = ?")
            params.append(1 if provider_deleted else 0)
        if deleted_at is not None or clear_deleted_at:
            assignments.append("cpa_deleted_at = ?")
            params.append(deleted_at)
        if reenable_pending is not None:
            assignments.append("cpa_reenable_pending = ?")
            params.append(1 if reenable_pending else 0)
        if action_at is not None:
            assignments.append("cpa_last_action_at = ?")
            params.append(action_at)
        if error is not None or clear_error:
            assignments.append("cpa_last_action_error = ?")
            params.append(error)
        params.append(account_id)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE opencode_go_accounts SET {', '.join(assignments)} WHERE id = ?",
                tuple(params),
            )

    def list_opencode_go_history(self, account_id: int, limit: int = 100) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT *
                FROM opencode_go_usage_records
                WHERE account_id = ?
                ORDER BY checked_at DESC, id DESC
                LIMIT ?
                """,
                (account_id, max(1, int(limit))),
            ).fetchall()

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
        self.replace_account_monitor_groups(account_id, [{"group_id": group_id}])

    def update_account_group_rate_change_status(
        self,
        account_id: int,
        changed: bool,
        monitor_group_id: int | None = None,
        group_id: str | None = None,
    ) -> None:
        with self.connect() as conn:
            target_monitor_group_id = monitor_group_id
            if target_monitor_group_id is None and group_id:
                row = conn.execute(
                    """
                    SELECT id FROM account_monitor_groups
                    WHERE account_id = ? AND group_id_hash = ?
                    """,
                    (account_id, _hash_group_id(group_id)),
                ).fetchone()
                target_monitor_group_id = int(row["id"]) if row else None
            if target_monitor_group_id is not None:
                conn.execute(
                    """
                    UPDATE account_monitor_groups
                    SET last_group_rate_changed = ?, updated_at = ?
                    WHERE id = ? AND account_id = ?
                    """,
                    (1 if changed else 0, utc_now(), target_monitor_group_id, account_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE account_monitor_groups
                    SET last_group_rate_changed = ?, updated_at = ?
                    WHERE account_id = ?
                    """,
                    (1 if changed else 0, utc_now(), account_id),
                )
            has_monitor_groups = conn.execute(
                "SELECT 1 FROM account_monitor_groups WHERE account_id = ? LIMIT 1",
                (account_id,),
            ).fetchone()
            if not has_monitor_groups:
                conn.execute(
                    """
                    UPDATE accounts
                    SET last_group_rate_changed = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (1 if changed else 0, utc_now(), account_id),
                )
                return
            conn.execute(
                """
                UPDATE accounts
                SET last_group_rate_changed = (
                    SELECT CASE WHEN COUNT(*) > 0 THEN 1 ELSE 0 END
                    FROM account_monitor_groups
                    WHERE account_id = ? AND last_group_rate_changed = 1
                ), updated_at = ?
                WHERE id = ?
                """,
                (account_id, utc_now(), account_id),
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
        monitor_group_id: int | None = None,
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
            where_monitor = "monitor_group_id IS ?" if monitor_group_id is None else "monitor_group_id = ?"
            previous = conn.execute(
                f"""
                SELECT * FROM group_rate_records
                WHERE account_id = ? AND {where_monitor}
                ORDER BY checked_at DESC, id DESC
                LIMIT 1
                """,
                (account_id, monitor_group_id),
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
                    INSERT INTO group_rate_records (account_id, monitor_group_id, plan_name, rate_multiplier, raw_json, checked_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (account_id, monitor_group_id, str(plan_name), current_rate, raw_json, checked_at),
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

    def list_group_rate_records(self, account_id: int, monitor_group_id: int | None = None) -> list[sqlite3.Row]:
        with self.connect() as conn:
            monitor_filter = "AND monitor_group_id = ?" if monitor_group_id is not None else ""
            params: tuple[Any, ...] = (account_id, monitor_group_id) if monitor_group_id is not None else (account_id,)
            return conn.execute(
                f"""
                SELECT * FROM group_rate_records
                WHERE account_id = ?
                {monitor_filter}
                ORDER BY checked_at DESC, id DESC
                """,
                params,
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
        self.cleanup_balance_history()
        return self.get_consumption_stats_for_accounts([account_id]).get(account_id, {})

    def get_consumption_stats_for_accounts(
        self,
        account_ids: Iterable[int],
    ) -> dict[int, dict[str, Optional[float]]]:
        normalized_ids = sorted({int(account_id) for account_id in account_ids})
        if not normalized_ids:
            return {}

        today_start = _today_start_utc()
        this_month_start = _this_month_start_utc()
        last_month_start = _last_month_start_utc()
        periods = {
            "today": (today_start, None),
            "yesterday": (_yesterday_start_utc(), today_start),
            "last_24h": (_hours_ago(24), None),
            "last_7d": (_days_ago(7), None),
            "last_14d": (_days_ago(14), None),
            "this_month": (this_month_start, None),
            "last_month": (last_month_start, this_month_start),
        }
        earliest_since = min(since for since, _ in periods.values())
        records_by_account: dict[int, list[sqlite3.Row]] = {account_id: [] for account_id in normalized_ids}

        with self.connect() as conn:
            for offset in range(0, len(normalized_ids), CONSUMPTION_QUERY_BATCH_SIZE):
                batch_ids = normalized_ids[offset : offset + CONSUMPTION_QUERY_BATCH_SIZE]
                placeholders = ", ".join("?" for _ in batch_ids)
                records = conn.execute(
                    f"""
                    SELECT account_id, remaining, checked_at
                    FROM query_records
                    WHERE account_id IN ({placeholders})
                        AND is_valid = 1
                        AND remaining IS NOT NULL
                        AND checked_at >= ?
                    ORDER BY account_id ASC, checked_at ASC, id ASC
                    """,
                    (*batch_ids, earliest_since),
                ).fetchall()
                for record in records:
                    records_by_account[int(record["account_id"])].append(record)

        return {
            account_id: {
                key: _sum_consumption(
                    [
                        record
                        for record in records
                        if record["checked_at"] >= since and (until is None or record["checked_at"] < until)
                    ]
                )
                for key, (since, until) in periods.items()
            }
            for account_id, records in records_by_account.items()
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

    def create_reminder(self, title: str, content: str, remind_at: str) -> int:
        now = utc_now()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO reminders (
                    title, content, remind_at, is_sent, created_at, updated_at
                )
                VALUES (?, ?, ?, 0, ?, ?)
                """,
                (title, content, remind_at, now, now),
            )
            return int(cursor.lastrowid)

    def get_reminder(self, reminder_id: int) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,)).fetchone()

    def list_reminders(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT *
                FROM reminders
                ORDER BY remind_at ASC, id ASC
                """
            ).fetchall()

    def list_due_reminders(self, now: str | None = None, retry_seconds: int = 5 * 60) -> list[sqlite3.Row]:
        now_text = now or utc_now()
        retry_cutoff = _iso_minus(now_text, retry_seconds)
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT *
                FROM reminders
                WHERE is_sent = 0
                    AND remind_at <= ?
                    AND (last_attempt_at IS NULL OR last_attempt_at <= ?)
                ORDER BY remind_at ASC, id ASC
                """,
                (now_text, retry_cutoff),
            ).fetchall()

    def update_reminder(self, reminder_id: int, title: str, content: str, remind_at: str) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE reminders
                SET title = ?, content = ?, remind_at = ?, is_sent = 0, sent_at = NULL,
                    last_error = NULL, last_attempt_at = NULL, updated_at = ?
                WHERE id = ?
                """,
                (title, content, remind_at, utc_now(), reminder_id),
            )
        return self.get_reminder(reminder_id)

    def delete_reminder(self, reminder_id: int) -> bool:
        with self.connect() as conn:
            cursor = conn.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
            return cursor.rowcount > 0

    def mark_reminder_sent(self, reminder_id: int, sent_at: str | None = None) -> None:
        sent_time = sent_at or utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE reminders
                SET is_sent = 1, sent_at = ?, last_error = NULL, last_attempt_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (sent_time, sent_time, sent_time, reminder_id),
            )

    def mark_reminder_failed(self, reminder_id: int, error: str, attempted_at: str | None = None) -> None:
        attempt_time = attempted_at or utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE reminders
                SET last_error = ?, last_attempt_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (str(error), attempt_time, attempt_time, reminder_id),
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

    def list_logs(
        self,
        limit: int | None = None,
        offset: int = 0,
        category: str | None = None,
        message_query: str | None = None,
    ) -> list[sqlite3.Row]:
        self.cleanup_logs()
        conditions = []
        params: list[Any] = []
        if category:
            conditions.append("category = ?")
            params.append(category)
        if message_query:
            conditions.append("message LIKE ?")
            params.append(f"%{message_query}%")
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        with self.connect() as conn:
            if limit is None:
                return conn.execute(f"SELECT * FROM app_logs{where} ORDER BY created_at DESC, id DESC", tuple(params)).fetchall()
            return conn.execute(
                f"""
                SELECT *
                FROM app_logs
                {where}
                ORDER BY created_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (*params, max(0, int(limit)), max(0, int(offset))),
            ).fetchall()

    def count_logs(self, category: str | None = None, message_query: str | None = None) -> int:
        self.cleanup_logs()
        conditions = []
        params: list[Any] = []
        if category:
            conditions.append("category = ?")
            params.append(category)
        if message_query:
            conditions.append("message LIKE ?")
            params.append(f"%{message_query}%")
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        with self.connect() as conn:
            row = conn.execute(f"SELECT COUNT(*) AS total FROM app_logs{where}", tuple(params)).fetchone()
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


def reminder_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = row_to_dict(row)
    data["is_sent"] = bool(data.get("is_sent"))
    data["isSent"] = data["is_sent"]
    data["remind_at_formatted"] = format_china_time(data.get("remind_at"))
    data["remindAtFormatted"] = data["remind_at_formatted"]
    data["sent_at_formatted"] = format_china_time(data.get("sent_at"))
    data["sentAtFormatted"] = data["sent_at_formatted"]
    data["last_attempt_at_formatted"] = format_china_time(data.get("last_attempt_at"))
    data["lastAttemptAtFormatted"] = data["last_attempt_at_formatted"]
    data["remind_at_china"] = _china_datetime_input(data.get("remind_at"))
    data["remindAtChina"] = data["remind_at_china"]
    return data


def monitor_group_to_dict(row: sqlite3.Row, secret_key: str) -> dict[str, Any]:
    data = row_to_dict(row)
    group_id_enc = data.pop("group_id_enc", None)
    group_id = decrypt_value(group_id_enc, secret_key) if group_id_enc else None
    data["group_id"] = group_id
    data["groupId"] = group_id
    data["last_group_rate_changed"] = bool(data.get("last_group_rate_changed"))
    data["lastGroupRateChanged"] = data["last_group_rate_changed"]
    data["rate_multiplier"] = data.get("effective_rate_multiplier")
    data["rateMultiplier"] = data["rate_multiplier"]
    data["planName"] = data.get("plan_name")
    return data


def _hash_group_id(group_id: str) -> str:
    return hashlib.sha256(str(group_id).encode("utf-8")).hexdigest()


def _groups_from_account_data(data: dict[str, Any]) -> list[dict[str, Any]]:
    if "monitor_groups" in data:
        return _normalize_monitor_groups(data.get("monitor_groups"))
    if "monitor_group_ids" in data:
        return _normalize_monitor_groups(data.get("monitor_group_ids"))
    return _normalize_monitor_groups([data.get("key_id")])


def _should_replace_monitor_groups(data: dict[str, Any], allow_key_id_fallback: bool = False) -> bool:
    if "monitor_groups" in data or "monitor_group_ids" in data:
        return True
    return allow_key_id_fallback and data.get("key_id") not in {None, ""}


def _normalize_monitor_groups(groups: Any) -> list[dict[str, Any]]:
    if groups is None:
        return []
    if isinstance(groups, str):
        groups = _split_group_ids(groups)
    if not isinstance(groups, list):
        groups = [groups]
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in groups:
        if isinstance(item, str):
            group = {"group_id": item}
        elif isinstance(item, dict):
            group_id = item.get("group_id") or item.get("groupId") or item.get("id") or item.get("name")
            group = {**item, "group_id": group_id}
        else:
            continue
        group_id = str(group.get("group_id") or "").strip()
        if not group_id:
            continue
        group_hash = _hash_group_id(group_id)
        if group_hash in seen:
            continue
        seen.add(group_hash)
        normalized.append({**group, "group_id": group_id})
    return normalized


def _split_group_ids(value: str) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    separators = ["|", ";", "\n"]
    values = [text]
    for separator in separators:
        values = [part for value in values for part in value.split(separator)]
    return [value.strip() for value in values if value.strip()]


def _monitor_group_summary(group: dict[str, Any]) -> dict[str, Any]:
    raw_json = group.get("raw_json")
    if not raw_json:
        raw_json = group.get("rawJson")
    if not raw_json:
        raw_json = _json_dumps(group) if group else None
    default_rate = _optional_float_or_none(
        group.get("default_rate_multiplier")
        if group.get("default_rate_multiplier") is not None
        else group.get("defaultRateMultiplier")
        if group.get("defaultRateMultiplier") is not None
        else group.get("rate")
        if group.get("rate") is not None
        else group.get("ratio")
        if group.get("ratio") is not None
        else group.get("rate_multiplier")
        if group.get("rate_multiplier") is not None
        else group.get("rateMultiplier")
    )
    user_rate = _optional_float_or_none(
        group.get("user_rate_multiplier")
        if group.get("user_rate_multiplier") is not None
        else group.get("userRateMultiplier")
    )
    effective_rate = _optional_float_or_none(
        group.get("effective_rate_multiplier")
        if group.get("effective_rate_multiplier") is not None
        else group.get("effectiveRateMultiplier")
    )
    if effective_rate is None:
        effective_rate = user_rate if user_rate is not None else default_rate
    plan_name = (
        group.get("plan_name")
        or group.get("planName")
        or group.get("name")
        or group.get("desc")
        or group.get("description")
    )
    if plan_name is None:
        group_id = group.get("group_id") or group.get("groupId") or group.get("id")
        plan_name = f"当前分组 {group_id}" if group_id else None
    return {
        "plan_name": str(plan_name) if plan_name is not None else None,
        "name": str(group.get("name")) if group.get("name") is not None else None,
        "default_rate_multiplier": default_rate,
        "user_rate_multiplier": user_rate,
        "effective_rate_multiplier": effective_rate,
        "raw_json": raw_json,
    }


def _optional_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    return float(value)


def _optional_float_or_none(value: Any) -> Optional[float]:
    try:
        return _optional_float(value)
    except (TypeError, ValueError):
        return None


def _positive_float_or_default(value: Any, default: float = 1.0) -> float:
    number = _optional_float_or_none(value)
    if number is None or number <= 0:
        return default
    return number


def _recharge_ratio_values(data: dict[str, Any]) -> tuple[float, float]:
    return (
        _positive_float_or_default(data.get("recharge_paid_amount"), 1.0),
        _positive_float_or_default(data.get("recharge_received_amount"), 1.0),
    )


def _account_value(account: Any, key: str, default: Any = None) -> Any:
    if account is None:
        return default
    try:
        return account[key]
    except (KeyError, IndexError, TypeError):
        return default


def actual_consumption_amount(value: Any, account: Any) -> Optional[float]:
    amount = _optional_float_or_none(value)
    if amount is None:
        return None
    paid_amount = _positive_float_or_default(_account_value(account, "recharge_paid_amount"), 1.0)
    received_amount = _positive_float_or_default(_account_value(account, "recharge_received_amount"), 1.0)
    return round(amount * paid_amount / received_amount, 6)


def actual_consumption_stats(stats: dict[str, Any], account: Any) -> dict[str, Optional[float]]:
    return {key: actual_consumption_amount(value, account) for key, value in stats.items()}


def _optional_bool(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    return 0 if str(value).strip().lower() in {"0", "false", "no", "off", ""} else 1


def _json_dumps(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, default=str)


def _opencode_go_usage_filter(column: str) -> str:
    if column not in {"last_weekly_usage", "last_monthly_usage"}:
        raise ValueError("不支持的 OpenCode Go 用量筛选字段")
    return (
        f"CASE WHEN json_valid({column}) THEN CAST(COALESCE("
        f"json_extract({column}, '$.usage_percent'), "
        f"json_extract({column}, '$.usagePercent')) AS REAL) END >= 99"
    )


def _append_opencode_go_status_filter(conditions: list[str], params: list[Any], status: str) -> None:
    value = str(status or "").strip().lower()
    if value == "deleted":
        conditions.append("cpa_provider_deleted = 1")
        return
    if value in {"valid", "invalid", "logged_in"}:
        conditions.append("COALESCE(cpa_provider_deleted, 0) = 0")
        conditions.append("last_status = ?")
        params.append(value)
        return
    if value == "never":
        conditions.append("COALESCE(cpa_provider_deleted, 0) = 0")
        conditions.append("last_status NOT IN ('valid', 'invalid', 'logged_in')")


def _mask_opencode_api_key(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= 12:
        return text[:3] + "*" * max(0, len(text) - 6) + text[-3:]
    return text[:8] + "*" * (len(text) - 12) + text[-4:]


def _safe_opencode_raw(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: ("***" if _is_opencode_sensitive_key(str(key)) else _safe_opencode_raw(item))
            for key, item in value.items()
            if str(key) not in {"storage_state", "storageState"}
        }
    if isinstance(value, list):
        return [_safe_opencode_raw(item) for item in value]
    return value


def _is_opencode_sensitive_key(key: str) -> bool:
    normalized = key.replace("-", "_").lower()
    return normalized in {"api_key", "apikey", "key", "token", "secret"} or "password" in normalized or "token" in normalized or "secret" in normalized


def _parse_iso_datetime(value: Any) -> datetime:
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso_minus(value: Any, seconds: int) -> str:
    try:
        dt = _parse_iso_datetime(value)
    except (TypeError, ValueError):
        dt = datetime.now(timezone.utc)
    return (dt - timedelta(seconds=max(0, int(seconds)))).isoformat(timespec="seconds")


def _china_datetime_input(value: Any) -> str:
    if not value:
        return ""
    try:
        dt = _parse_iso_datetime(value)
    except (TypeError, ValueError):
        return ""
    return dt.astimezone(CHINA_TZ).strftime("%Y-%m-%dT%H:%M:%S")


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

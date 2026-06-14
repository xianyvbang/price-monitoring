# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

A FastAPI web app (in Chinese) that batch-monitors account balances on two upstream platforms — `newApi` and `sub2Api` — with SQLite persistence, admin login, a background scheduler for automatic queries, low-balance email alerts, and group-rate-change tracking. Deployed via Docker Compose.

## Commands

```bash
# Local development
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload

# Tests
pytest                          # all tests
pytest tests/test_balance.py    # one file
pytest tests/test_balance.py::test_name   # one test

# Docker
docker compose up -d --build
docker compose up -d --force-recreate
```

There is no linter or formatter configured. `pytest.ini` sets `pythonpath = .` and async fixture loop scope to `function` (tests use `pytest-asyncio`).

## Configuration

Settings come from `.env` (see `.env.example`), loaded in `app/config.py`. Two values must be changed from defaults in production:
- `APP_SECRET_KEY` — used both for session signing AND as the encryption key for all stored secrets (Fernet key is derived from it). Changing it after data exists makes previously encrypted columns undecryptable. The app logs a warning on startup if it's still the default.
- `ADMIN_PASSWORD` — seeds the admin user on first run (`Database.ensure_admin`).

`DATABASE_PATH` defaults to `data/app.db` locally; Docker overrides it to `/app/data/app.db` with a volume mount.

## Architecture

Five-layer flow, all server-rendered (Jinja2 templates in `app/templates/`, no SPA):

- **`app/main.py`** — all FastAPI routes (both HTML form endpoints and JSON `/api/*` endpoints), plus an HTTP-logging middleware that records every request/response into the `app_logs` table (with sensitive fields masked). Also holds the large `public_account` / `public_dashboard_account` serializers that flatten DB rows into both snake_case and camelCase keys for templates and the API. Auth is cookie-based via `itsdangerous` signed tokens; `current_user`/`require_user` validate the token and check `session_version` (incremented on password change to invalidate old sessions).
- **`app/models.py`** — `Database` class wrapping raw `sqlite3`. Owns the schema (created idempotently in `init()`) and a series of `_migrate_*` methods run on every startup to evolve existing DBs via `ALTER TABLE` / table-rebuild. All credential columns are stored encrypted (`*_enc`). Also contains consumption-math helpers (`_sum_consumption`, `actual_consumption_amount`) and China-timezone date-bucket helpers.
- **`app/services/balance.py`** — pure HTTP query logic against upstream platforms using `httpx`. Returns normalized result dicts via `normalize_result`. Contains the platform-specific protocols (see below) and a module-level `_SUB2API_TOKEN_CACHE` that caches sub2Api login JWTs by `base_url|email` until expiry.
- **`app/services/scheduler.py`** — `BalanceScheduler` runs two independent asyncio loops (balance queries at `query_interval`, group-rate queries at `group_rate_query_interval`), started/stopped via the FastAPI `lifespan`. `notify_settings_changed()` wakes the loops early when settings change. The orchestration functions (`query_one_account`, `query_group_rate_for_account`, `query_all_accounts`) tie balance/group queries to DB writes and alerting.
- **`app/services/alerts.py`** + **`emailer.py`** — threshold comparison and SMTP sending. Alerts are edge-triggered: `low_balance_active` flag prevents re-sending until balance recovers.

### Key domain concepts

- **Two platforms.** `newApi` uses `accessToken` + `userId` headers against `/api/user/self` (quota is divided by 500000 to get USD). `sub2Api` uses an `apiKey` bearer token against `/v1/usage` for balance; group-rate lookups additionally log in with `email`/`password` (or a pre-supplied `accessToken` for Turnstile-protected sites like 2chat.cc) to call `/api/v1/groups/available` and `/api/v1/groups/rates`.
- **Monitor groups.** An account can track multiple upstream groups via the `account_monitor_groups` table (`replace_account_monitor_groups`). The legacy single-group `key_id_enc` column is kept in sync with the first group and migrated into the table on startup. Each group's rate is snapshotted; when `effective_rate_multiplier` changes, a `group_rate_records` row is inserted and a change email is sent.
- **Consumption.** Computed from the `query_records` balance history by summing every downward delta in `remaining` (`_sum_consumption`). "Actual" consumption multiplies by `recharge_paid_amount / recharge_received_amount` to account for top-up bonus ratios. The dashboard aggregates consumption per Base URL across fixed periods plus a custom date range, all bucketed in China time (UTC+8).
- **Account flags.** `is_enabled` (auto-query on/off — forced off when `is_visible` is off), `is_visible` (dashboard display), `is_eliminated` (sorted last, suppresses alerts).

### Encryption note

`app/security.py` derives a Fernet key from `APP_SECRET_KEY` via SHA-256. `encrypt_value`/`decrypt_value` return `None` for empty input. DB upserts use `COALESCE(excluded.x_enc, x_enc)` so omitting a credential field in an update preserves the existing encrypted value rather than wiping it.

### Bulk import

`import_bulk_accounts` in `main.py` accepts either a JSON array or line-based CSV, with the CSV column count determining the field layout (see README for the exact formats). Both platforms accept many key aliases (snake_case, camelCase) normalized in `_account_from_payload`.

## Data retention

`app_logs` are pruned to the last 7 days on every write/read; `query_records` (balance history) are pruned to `BALANCE_HISTORY_MONTHS` (9 months); the balance trend endpoint only returns the last `BALANCE_TREND_DAYS` (3 days).

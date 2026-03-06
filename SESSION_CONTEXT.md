# Session Context

Last updated: 2026-03-06

Purpose: fast-start context for new coding sessions in this repo. Read this first, then open only the files relevant to the task. This file is a map, not the source of truth.

## Product

- `smm_panel` is a FastAPI-based Facebook automation panel.
- UI is server-rendered from one main template: `templates/index.html`.
- Browser automation runs through Camoufox + Playwright in `worker.py`.
- Persistence is SQLite by default via async SQLAlchemy.

## Main Entry Points

- `api.py`: FastAPI app, DB boot, schema migration helper, REST endpoints, task scheduling, warmup orchestration.
- `worker.py`: Facebook browser session management, login/checkpoint/captcha logic, warmup actions, task execution.
- `models.py`: SQLAlchemy models and enums.
- `crud.py`: small DB helpers such as proxy assignment and account upsert.
- `import_data.py`: account/proxy import parsing.
- `imap_utils.py`: IMAP provider guessing and Facebook code retrieval.
- `iproxy_utils.py`: mobile proxy rotation and external IP lookup.
- `templates/index.html`: Tailwind + Font Awesome frontend.

## Data Model Highlights

### `Account`

Important fields beyond basic credentials:

- `cookies`, `storage_state`
- `status` as `AccountStatus`
- `email_login`, `email_password`, `imap_server`
- `warmed_up_at`
- `last_checkpoint_type`
- `proxy_type`: `datacenter | residential | mobile`
- `proxy_rotation_url`

### `WarmupLog`

Stores one warmup session:

- `started_at`, `finished_at`, `duration_seconds`
- `actions_attempted`, `actions_succeeded`, `actions_failed`
- `action_log` JSON list with per-action timing and status
- `result`
- `error_message`

### Enums

- `AccountStatus`: `active`, `banned`, `shadow_banned`, `checkpoint`, `captcha_blocked`
- `CheckpointType`: `code_verification`, `face_verification`, `suspicious_login`, `account_disabled`, `unknown_checkpoint`

## Migration Pattern

- SQLite migrations are handled inline in `api.py` inside `_migrate_schema_if_needed(connection)`.
- Existing style is intentionally simple and idempotent:
  - `PRAGMA table_info(...)`
  - `ALTER TABLE ... ADD COLUMN ...` only if missing
  - `CREATE TABLE IF NOT EXISTS ...` for new tables
- If you add DB columns/tables, follow that exact pattern instead of adding Alembic.

## Recent Functional Areas

### Warmup logging

- `worker.py::FacebookBrowser.warmup()` returns a detailed result dict.
- `api.py` creates `WarmupLog` before warmup starts and updates it on both success and failure.
- Endpoint: `GET /api/accounts/{account_id}/warmup/logs?limit=20`
- UI shows warmup history for each account.

### Checkpoint typing

- `worker.py::detect_checkpoint_type()` classifies checkpoint pages from visible body text.
- Dispatcher lives in `_wait_for_checkpoint_resolution()`.
- Face verification is surfaced, not solved automatically.
- `Account.last_checkpoint_type` is exposed in API and shown in UI.

### Bulk import

- `import_data.py::detect_and_parse_line()` supports:
  - `login:password:email:email_password`
  - Turkish shop format: `facebook giriş: ... şifre: ... mail: ... mail şifre: ...`
- `POST /api/accounts/import` returns per-line results plus summary.

### Mobile proxies

- `iproxy_utils.py` contains:
  - `rotate_mobile_ip(rotation_url)`
  - `get_current_ip(proxy_url)`
- `worker.py` rotates mobile proxies before browser start and logs IP when available.
- API endpoint: `POST /api/accounts/{account_id}/rotate-ip`

## Worker Notes

- Main class: `FacebookBrowser`
- Session input payload: `AccountSessionData`
- Important behavior:
  - restores `storage_state` when available
  - checks session liveness with `_check_session_alive()`
  - can retry login once after mobile proxy rotation on checkpoint/captcha-style failures
  - logs through `logger.info / warning / error` with account context
- Screenshots for face checkpoints are stored under `./screenshots`

## API Notes

Open `api.py` first for any backend change involving routes, migrations, or account lifecycle.

Endpoints that changed recently and are likely relevant:

- `POST /api/accounts/{account_id}/warmup`
- `GET /api/accounts/{account_id}/warmup/logs`
- `POST /api/accounts/import`
- `POST /api/accounts/{account_id}/rotate-ip`
- account list/create/update flows now include checkpoint/proxy metadata

## Frontend Notes

- UI stack: Tailwind CDN + Font Awesome 5.15.4 + JetBrains Mono.
- New UI icons should use Font Awesome classes already in `templates/index.html`.
- Do not introduce emoji for status indicators in UI; use icons.
- Most frontend behavior lives inline in `templates/index.html`, so targeted edits usually happen there.

## Testing Map

Open only what matches the task:

- `tests/test_logic_comprehensive.py`: API/account lifecycle integration coverage
- `tests/test_api_hybrid.py`: API hybrid tests including import and rotate-IP flows
- `tests/test_worker_actions.py`: worker behavior, warmup, checkpoint detection
- `tests/test_import_data.py`: import parser coverage

## Verification Commands

Standard quality bar for this repo:

```bash
uvx ruff check .
uvx ty check
uv run pytest -q
```

If a task only touches a narrow area, run the focused tests first, then the full suite if feasible.

## Conventions

- Prefer concise, explicit logging.
- Use async style consistently; `asyncio.sleep()` only.
- Keep changes surgical and aligned with existing patterns.
- Before editing a feature, verify the touched area in source instead of relying only on this file.

## Suggested Minimal Read Order

For most tasks, this is enough to start:

1. `SESSION_CONTEXT.md`
2. one or two of: `api.py`, `worker.py`, `models.py`
3. the directly relevant test file

Avoid scanning the whole repo unless the task genuinely crosses multiple subsystems.

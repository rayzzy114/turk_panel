# Cookie Normalization and Cookie-Invalid Handling Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Normalize Dolphin cookies for Playwright, prevent false password fallback for cookie-only accounts, add `COOKIE_INVALID`, and expose cookie re-import in API/UI.

**Architecture:** Centralize cookie format handling in `import_data.py`, use it consistently in import/runtime/API, and update worker login flow to distinguish invalid cookies from invalid credentials. Add a small per-account cookie upload UI that surfaces conversion results and recovery affordances.

**Tech Stack:** FastAPI, SQLAlchemy, SQLite, Jinja/vanilla JS UI, pytest.

---

### Task 1: Cookie normalization helpers

**Files:**
- Modify: `import_data.py`
- Test: `tests/test_import_data.py`

**Step 1: Write failing tests**
Add tests for `detect_cookie_format`, `_normalize_samesite`, `convert_dolphin_cookies`, and `normalize_cookies`.

**Step 2: Run targeted tests**
Run: `uv run pytest tests/test_import_data.py -q`
Expected: FAIL on missing helpers/behavior.

**Step 3: Implement minimal helpers**
Add cookie format detection, Dolphin conversion, and normalization.

**Step 4: Re-run tests**
Run: `uv run pytest tests/test_import_data.py -q`
Expected: PASS.

### Task 2: Worker cookie restore order and COOKIE_INVALID path

**Files:**
- Modify: `worker.py`, `models.py`, `api.py`
- Test: `tests/test_worker_actions.py`, `tests/test_logic_comprehensive.py`

**Step 1: Write failing tests**
Cover restore order:
- storage_state before cookies
- cookies normalized before add
- cookie-only accounts do not use password fallback
- cookie-only failure marks `COOKIE_INVALID`

**Step 2: Run targeted tests**
Run: `uv run pytest tests/test_worker_actions.py tests/test_logic_comprehensive.py -q -k 'cookie or storage_state'`
Expected: FAIL.

**Step 3: Implement minimal fix**
Add `COOKIE_INVALID`, runtime ordering, and no-password cookie-only failure handling.

**Step 4: Re-run tests**
Expected: PASS.

### Task 3: Cookie upload endpoint

**Files:**
- Modify: `api.py`
- Test: `tests/test_logic_comprehensive.py` or `tests/test_api_hybrid.py`

**Step 1: Write failing tests**
Add endpoint tests for valid Dolphin input, missing `c_user`, missing `xs`, and no Facebook cookies.

**Step 2: Run targeted tests**
Run: `uv run pytest tests/test_logic_comprehensive.py -q -k 'cookie_import_endpoint'`
Expected: FAIL.

**Step 3: Implement endpoint**
Add request model, normalization, validation, save, and response summary.

**Step 4: Re-run tests**
Expected: PASS.

### Task 4: UI cookie re-import flow

**Files:**
- Modify: `templates/index.html`

**Step 1: Add status badge and direct re-import action**
Support `COOKIE_INVALID` with tooltip and button.

**Step 2: Add modal and submit flow**
Textarea -> POST `/api/accounts/{account_id}/cookies` -> show conversion results.

**Step 3: Verify manually**
Load UI, confirm modal opens, and status render does not break table layout.

### Task 5: Verification

**Files:**
- Modify if needed: `SESSION_CONTEXT.md`, `AGENTS.md`

**Step 1: Run quality checks**
- `uv run ruff check .`
- `uvx ty check`
- `uv run pytest -q`

**Step 2: Update context docs if behavior changed materially**

**Step 3: Commit**
Use a focused commit message after verification.

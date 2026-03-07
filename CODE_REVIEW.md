# SMM Hybrid Panel - Comprehensive Code Review

## 1. Architectural Integrity

### Separation of Concerns & System Design
The current system successfully establishes a basic hybrid architecture: FastAPI handles API and task queuing (via SQLite), while an `asyncio` loop running `_run_browser_task_wrapper` in the background acts as a worker.

**Critical Issues & Improvements:**
1. **Coupling of API & Worker Lifespans:**
   The `browser_worker_loop()` is spawned inside the FastAPI `lifespan` context. This means the worker scales with the API server. If you deploy multiple Uvicorn workers (e.g., `uvicorn api:app --workers 4`), you will spawn 4 concurrent browser worker loops, leading to task duplication and DB locks.
   *Improvement:* Separate `worker.py` into a standalone process (e.g., a CLI entry point `uv run python worker_main.py`) and use a proper task queue like Celery, ARQ, or at least a Redis queue instead of polling a SQLite table.
2. **SQLite Polling for Tasks:**
   The worker loop constantly polls `tasks` table with `TaskStatus.PENDING`. SQLite handles concurrency poorly. Polling introduces latency and increases I/O overhead.
   *Improvement:* If staying with SQLite, implement a robust `SELECT ... FOR UPDATE` (not supported directly in basic SQLite without specific WAL/locking handling) or transition to PostgreSQL and use `SKIP LOCKED` for fetching tasks.
3. **ACCOUNT_SELECTION_LOCK Bottleneck:**
   In `_process_browser_task`, account selection uses a global `ACCOUNT_SELECTION_LOCK`. This serializes task assignment across the entire application, limiting horizontal scalability.

## 2. Robustness & Error Handling

### Browser Automation (Playwright/Camoufox)
The `FacebookBrowser` class encapsulates Playwright logic well, with thoughtful stealth techniques (human typing, scrolling, geoip proxying).

**Critical Issues & Improvements:**
1. **Unbounded Retries and Checkpoints:**
   In `_handle_code_checkpoint`, the code attempts to wait for an IMAP email code. If it fails, it falls back to `_legacy_manual_wait()`, which can block the worker for up to 600 seconds (`FB_CHECKPOINT_WAIT_SECONDS`). With a concurrency limit of 3 (`MAX_CONCURRENT_TASKS`), hitting 3 checkpoints simultaneously completely stalls the entire worker pool for 10 minutes.
   *Improvement:* Checkpoints should pause the task and free up the worker slot. Implement async state machines or webhook callbacks for manual resolution rather than long polling.
2. **Race Conditions in Task Updates:**
   The worker updates task statuses (`TaskStatus.IN_PROGRESS`, `SUCCESS`, `ERROR`) without optimistic concurrency control. Two loops fetching the same `PENDING` task might both mark it `IN_PROGRESS`.
   *Improvement:* Update the SQL query to do an atomic update-and-return or use a version column.
3. **Transient Network Errors:**
   `FacebookBrowser._navigate_warmup` returns `False` on failure but continues execution. This can lead to subsequent actions interacting with the wrong page context or hanging.

## 3. Security Audit

**Critical Security Vulnerabilities:**
1. **Hardcoded API Key Leak:**
   In `panel_api.py`, `self.api_key = api_key or "2f27efe775f5482cccbb9e987977fb7c"` exposes a production API key.
   *Improvement:* Remove the fallback and enforce fetching from `os.getenv("MEDYABAYIM_API_KEY")`.
2. **Weak Cookie Validation (Cookie Injection / CSRF Risk):**
   In `import_data.py`, `convert_dolphin_cookies` and `normalize_cookies` check `if "facebook.com" in domain`. An attacker uploading cookies could provide `domain: "malicious-facebook.com.org"` and it would be accepted, potentially leaking session data or failing browser injections.
   *Improvement:* Use strict domain matching: `domain.endswith(".facebook.com") or domain == "facebook.com"`.
3. **Lack of Rate Limiting & DoS Vectors:**
   Endpoints like `/api/parse_comments` hit the Apify API synchronously. A malicious user could spam this endpoint, exhausting Apify credits or causing a DoS.
   *Improvement:* Add FastAPI `Slowapi` rate-limiting.

## 4. Code Quality & Maintainability

- **Duplication:** `_warmup_error_result` and `_warmup_default_result` in `api.py` duplicate dict structures.
- **Type Safety:** High quality overall. Strict types are used well. However, SQL query results sometimes rely on dynamic typing instead of Pydantic validations out-of-the-box.
- **Fat `api.py`:** `api.py` is >1300 lines long and handles routing, DB connection, schema migrations, business logic, and background tasks.
  *Improvement:* Split into `routes/`, `services/`, and `db/` directories.

## 5. Database & Migrations

**Idempotent Migration Pattern:**
The `_migrate_schema_if_needed` pattern is a pragmatic "KISS" approach for SQLite. However, `PRAGMA table_info` followed by `ALTER TABLE` is risky for complex migrations (e.g., dropping columns, renaming, altering constraints).
*Improvement:* Introduce `Alembic` for reliable, trackable schema migrations, especially if the project ever migrates to PostgreSQL.

---

## Targeted Feedback

### Cookie Normalization Logic
The logic in `import_data.py` (Dolphin vs Playwright formats) is robust in theory, but the substring domain match is dangerous (as noted in Security). Also, Playwright expects cookies in a specific format; missing the `.` prefix on domains can sometimes cause Playwright to reject them.

### Medyabayim Integration Logic
The `PanelAPI` uses `httpx.AsyncClient` correctly. However, `add_order` assumes `quantity` is always applicable. The hardcoded API key is a critical flaw.

### Strategy for Improving kh-mail IMAP/Webmail Fallback
The `_get_facebook_code_from_webmail` fallback spins up a *headless browser* just to check an inbox. This is incredibly heavy and error-prone (Cloudflare blocks, slow loads).
**Strategy:**
1. Reverse-engineer the SnappyMail/Roundcube AJAX API used by `kh-mail.com`. Use lightweight HTTP `requests`/`httpx` to authenticate and fetch emails directly via their JSON endpoints.
2. If IMAP is failing due to provider blocks, rotate proxy IPs for the IMAP connection as well, not just for Facebook.

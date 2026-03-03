# Captcha handling implementation plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Prevent the worker from reusing accounts that hit Facebook captchas by marking them `CAPTCHA_BLOCKED` and skipping them automatically.

**Architecture:** Add a new `AccountStatus` value plus a custom `AccountCaptchaError` that `_is_authorized` raises when no `c_user` cookie is present but login/captcha selectors exist. Catch that error in worker/API flows, mark the account blocked in SQLAlchemy, and leave tasks pending so another account picks up work.

**Tech Stack:** Python, FastAPI, SQLAlchemy, Playwright, pytest

---

### Task 1: Model + exception updates

**Files:**
- Modify: `models.py` (add new enum member and helper if desired)
- Create: `worker.py` (define `class AccountCaptchaError(RuntimeError): ...` near other exceptions)
- Test: `tests/test_worker_actions.py` (future task uses new error) - placeholder for tests, no change now.

**Step 1: Write the failing test**
- Add a test in `tests/test_worker_actions.py` that calls `_is_authorized()` via a fake page/context without `c_user` cookies but with a login selector; assert `AccountCaptchaError` is raised.

**Step 2: Run test to verify it fails**
- Command: `uv run pytest -q tests/test_worker_actions.py::test_is_authorized_raises_account_captcha_error`
- Expected: FAIL because `AccountCaptchaError` class/import or `_is_authorized` not raising yet.

**Step 3: Write minimal implementation**
- In `models.py`, append `CAPTCHA_BLOCKED = "captcha_blocked"` to `AccountStatus`.
- In `worker.py`, define `class AccountCaptchaError(RuntimeError): pass` near other helpers.

**Step 4: Run the test to verify it passes**
- Same pytest command; expect PASS once `_is_authorized` updated.

**Step 5: Commit**
```
git add models.py worker.py tests/test_worker_actions.py docs/plans/2026-02-28-captcha-status-plan.md
git commit -m "feat: add captcha status and error"
```

### Task 2: `_is_authorized` raises `AccountCaptchaError`

**Files:**
- Modify: `worker.py: _is_authorized` logic (check selectors, raise error instead of returning False)
- Test: `tests/test_worker_actions.py` (new tests verifying detection and log context)

**Step 1: Write failing test**
- Author a test that sets up fake page with no `c_user` but both `form[action*=login]` and `div[role="dialog"]`, calls `_is_authorized`, expects `AccountCaptchaError`.

**Step 2: Run test**
- `uv run pytest -q tests/test_worker_actions.py::test_is_authorized_detects_captcha`
- Expect FAIL until `_is_authorized` implements detection.

**Step 3: Implement minimal fix**
- In `_is_authorized`, after navigation gather cookies, login_form_count, dialog_count; if no `c_user` and either selector count>0, raise `AccountCaptchaError("captcha detected")` instead of returning False.
- Keep existing logs and checkpoint handling.

**Step 4: Verify test passes**
- Re-run pytest command; expect PASS.

**Step 5: Commit**
```
git add worker.py tests/test_worker_actions.py
git commit -m "feat: detect captcha in _is_authorized"
```

### Task 3: API/workers handle `AccountCaptchaError`

**Files:**
- Modify: `api.py` (in `process_task` and `/api/parse_comments`, catch the error, update account.status, commit, leave task pending or return structured error)
- Modify: `models.py` (if needed to import new status in worker functions)
- Test: `tests/test_api_hybrid.py` (add tests where mocked browser login raises `AccountCaptchaError` and verify status change and task retention)

**Step 1: Write failing test**
- In `tests/test_api_hybrid.py`, add a test where fake `FacebookBrowser.login()` raises `AccountCaptchaError`. Expect `process_task` leaves task pending, status `CAPTCHA_BLOCKED`, no task failure, and next account untouched.

**Step 2: Run test**
- `uv run pytest -q tests/test_api_hybrid.py::test_process_task_blocks_account_on_captcha`
- Should fail until handler implemented.

**Step 3: Minimal implementation**
- Wrap worker execution logic around try/except `AccountCaptchaError`; on catch, set `account.status = AccountStatus.CAPTCHA_BLOCKED`, `session.commit()`, log, and return early without marking task success/failure.
- Repeat similar handling inside `/api/parse_comments`, returning the structured error response and ensuring debug info flows (account status updated and committed).

**Step 4: Verify test passes**
- Re-run the pytest command; expect PASS.

**Step 5: Commit**
```
 git add api.py models.py tests/test_api_hybrid.py
 git commit -m "feat: block captcha accounts"
```

### Task 4: Parser error response tests and docs

**Files:**
- Modify: `templates/index.html` (ensure parser shows new error) if necessary
- Modify: `tests/test_api_hybrid.py` (parser endpoint test verifying structured error and `debug` etc.)

**Step 1: Write failing test**
- Add test hitting `/api/parse_comments` where mocked browser raises `AccountCaptchaError`, expecting JSON `{status:"error", warning:"account_session_invalid"}` and account status updated.

**Step 2: Run test**
- `uv run pytest -q tests/test_api_hybrid.py::test_parse_comments_endpoint_handles_captcha`
- Should fail until handler in endpoint implemented.

**Step 3: Minimal implementation**
- In `/api/parse_comments`, wrap login/parse flow in `try/except AccountCaptchaError`, update `Account.status`, commit, return error payload similar to login failure.

**Step 4: Verify test passes**
- Re-run command; expect PASS.

**Step 5: Commit**
```
 git add api.py tests/test_api_hybrid.py
 git commit -m "test: ensure parser handles captcha errors"
```

## Verification
- Run `uv run pytest -q` after all tasks to confirm entire suite passes.
- Ensure `docs/plans/2026-02-28-captcha-status-plan.md` is tracked.

Plan complete and saved to `docs/plans/2026-02-28-captcha-status-plan.md`. Two execution options:
1. Subagent-Driven (this session) – dispatch new subagents per task with reviewing between tasks.
2. Parallel Session – start a new executing-plans session with dedicated tasks.
Which approach?
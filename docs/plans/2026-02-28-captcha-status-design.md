# Captcha status handling (2026-02-28)

## Goal
Mark accounts that hit Facebook captcha/login wall as unusable (CAPTCHA_BLOCKED) and skip them automatically, ensuring the worker switches to another account instead of looping on a blocked session.

## Architecture
- Extend `AccountStatus` with `CAPTCHA_BLOCKED` so existing filters (active accounts for tasks) implicitly ignore blocked ones.
- Introduce `AccountCaptchaError` thrown by `FacebookBrowser._is_authorized` when:
  - `c_user` cookie missing and `input[name="email"]` or `div[role="dialog"]` is present after navigation.
- Handle the exception in `api.process_task` and `/api/parse_comments`:
  - Catch `AccountCaptchaError`, update the account status to `CAPTCHA_BLOCKED`, commit the DB session.
  - Leave the current task `pending` so the scheduler can retry with another live account.
  - Return a structured error for the parser endpoint (status `error`, warning `account_session_invalid`).
- Tests will cover:
  - `_is_authorized` raising `AccountCaptchaError` when conditions met.
  - `process_task` and `/api/parse_comments` reacting to the exception by updating the DB and skipping the account.

## Testing strategy
- Add unit tests that simulate the missing `c_user` + login selector to ensure `_is_authorized` fails fast with `AccountCaptchaError`. TDD cycle: write failing test first, implement behavior, refactor.
- For API/workers, mock `FacebookBrowser` to raise `AccountCaptchaError` during login/parse phases and assert that the account status transitions and tasks remain pending.

## Next steps
1. Update models and exception definitions.
2. Adjust `_is_authorized` to raise the new error based on captcha indicators.
3. Catch the error in API worker paths and update account status.
4. Add/adjust tests to cover the new behavior.

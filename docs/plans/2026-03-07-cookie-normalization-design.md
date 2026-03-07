# Cookie Normalization and Cookie-Invalid Handling Design

## Goal
Make Dolphin Anty cookie imports usable by Playwright/Camoufox, prevent false login/password fallbacks for cookie-only accounts, and expose a first-class COOKIE_INVALID recovery flow in API and UI.

## Current Problem
The panel imports Dolphin cookie exports but stores only a reduced cookie shape. At runtime, worker login logic tries cookies and then falls back to login/password. For cookie-only imports the password is often `__COOKIE_ONLY__`, so failures are misclassified as bad credentials even when the real issue is that cookie restoration failed or the browser context differs from Dolphin.

## Approved Approach
Use a single cookie normalization pipeline everywhere:
- Normalize Dolphin JSON cookies into Playwright-compatible cookies.
- Filter to Facebook cookies only.
- Reuse the same normalization path during import, runtime restore, and per-account cookie upload.

Runtime restore order in the worker:
1. Restore from `storage_state` if present.
2. Restore from normalized `cookies`.
3. If both fail and password is real, use login/password fallback.
4. If both fail and password is `__COOKIE_ONLY__`, mark account `COOKIE_INVALID` and log `Re-import cookies from Dolphin`.

## Data Model
Add `COOKIE_INVALID` to `AccountStatus`.

Existing status handling must remain backward compatible for old rows and other features. New logic introduced here must only classify cookie-only restore failures as `COOKIE_INVALID`.

## Cookie Normalization
Add in `import_data.py`:
- `_normalize_samesite(value: str) -> str`
- `detect_cookie_format(raw: list[dict]) -> str`
- `convert_dolphin_cookies(dolphin_cookies: list[dict]) -> list[dict]`
- `normalize_cookies(raw: list[dict]) -> list[dict]`

Rules:
- Keep only cookies whose domain contains `facebook.com`.
- Map Dolphin/Chrome `expirationDate` to Playwright `expires`.
- Session cookies become `expires=-1`.
- Normalize sameSite values to Playwright variants.
- Drop unknown fields.
- Log kept vs dropped counts at DEBUG.

## API
Add endpoint:
- `POST /api/accounts/{account_id}/cookies`

Behavior:
- Accept raw cookie JSON.
- Detect format.
- Normalize cookies.
- Validate required cookies:
  - must include `c_user`
  - must include `xs`
  - must include at least one of `datr` or `sb`
- Save normalized cookies on success.
- Return conversion summary.

## UI
Add per-account cookie import action and modal.
For `COOKIE_INVALID` status:
- show a dedicated status badge/icon
- show tooltip `Re-import cookies from Dolphin`
- expose direct action to open the cookie import modal from the status area

## Testing Strategy
Add tests for:
- Dolphin -> Playwright conversion
- sameSite normalization
- expiration/session cookie handling
- format detection
- cookie upload endpoint validation
- worker behavior for cookie-only accounts with failed restore
- worker ordering: storage_state first, then cookies, then fallback

## Non-Goals
- No anti-captcha work here.
- No broad rework of unrelated account states.

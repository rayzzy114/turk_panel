# Facebook Like Comment Working Baseline

Last validated: March 3, 2026

## Scope

This document locks the currently working behavior for liking a comment (`like_comment`).

Source of truth in code:
- `worker.py` (`like_comment`, `_dismiss_action_blockers`, `_human_click`)

## What was fixed

The stable fix was:

1. Do **not** call `_close_dialogs()` in `like_comment` (it can close the post modal itself).
2. Use `_dismiss_action_blockers()` only (safe popups like `Not Now`, `Şimdi değil`).
3. Search like button with dialog-first strategy and DOM-specific selector:
   - `div[role="dialog"] [data-ad-rendering-role="like_button"]`
4. Keep multilingual text fallbacks (`Beğen`, `Like`, `Нравится`).
5. Keep iframe fallback.

## Current algorithm

1. Warmup session.
2. Open target URL.
3. Close only blocking popups via `_dismiss_action_blockers`.
4. Try to find target comment container by `comment_id` in `div[role="article"]`.
5. If not reliable, fallback to like-button selectors (dialog-first, then global).
6. If needed, fallback to iframe search.
7. If button appears already in liked state (`aria-pressed=true`, `unlike/vazgeç`) -> return `True`.
8. Otherwise click via `_human_click` and return `True`.
9. If nothing found -> return `False`.

## Stable selectors

Primary selector (current best):

```css
div[role="dialog"] [data-ad-rendering-role="like_button"]
```

Fallback selectors in code:

- `[data-ad-rendering-role="like_button"]`
- `div[role="dialog"] [role="button"]:has-text("Beğen")`
- `[role="button"]:has-text("Beğen")`
- `div[role="dialog"] [role="button"]:has-text("Like")`
- `[role="button"]:has-text("Like")`
- `div[role="dialog"] [role="button"]:has-text("Нравится")`
- `[role="button"]:has-text("Нравится")`

## Regression guardrails

Do not remove these points without live re-validation:

- `_dismiss_action_blockers` in `like_comment`.
- Dialog-first selector priority.
- `data-ad-rendering-role="like_button"` selector.
- Iframe fallback.
- Already-liked detection before clicking.

## Debug checkpoints

Expected logs in healthy flow:
- `Переход к цели: ...`
- `Кнопка лайка найдена по селектору: ...`
- `Нажимаю Лайк (Deep Stealth)...`

Possible alternative healthy log:
- `Пост уже лайкнут.`

Failure marker:
- task log: `Ошибка: Кнопка лайка не найдена.`

## Verification commands

```bash
uv run pytest -q tests/test_worker_actions.py -k "like_comment"
uv run pytest -q tests/test_worker_actions.py
```

Expected: all selected tests pass.

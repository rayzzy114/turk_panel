# Facebook Reply Comment Working Baseline

Last validated: March 3, 2026

## Scope

This document locks the currently working behavior for replying to comments.

Source of truth in code:
- `worker.py` (`reply_comment`, `leave_comment`, `_dismiss_action_blockers`)

## Current implementation contract

`reply_comment` intentionally reuses the same stable pipeline as `leave_comment`.

```python
async def reply_comment(self, target_url: str, text: str) -> bool:
    return await self.leave_comment(target_url, text)
```

This is expected and correct for the current Facebook modal flow.

## Required input assumptions

1. `target_url` should point to a post/comment context where reply composer can be opened from the same modal/page.
2. `text` must be non-empty reply body.
3. Locale can be Turkish/Russian/English; selectors must stay language-agnostic first.

## Reply flow (must stay aligned with leave_comment)

1. Open target URL.
2. Close only blocking popups (`Not Now`, `Şimdi değil`) via `_dismiss_action_blockers`.
3. Find Lexical textbox (dialog-first, then global, then iframe fallback).
4. If not found, scroll dialog to bottom and retry.
5. If still not found, activate comment trigger and retry.
6. Activate editor with:
   1. `scroll_into_view_if_needed()`
   2. `wait_for(state="attached")`
   3. `wait_for(state="visible")`
   4. JS focus + focus event dispatch
   5. Physical mouse click
7. Type reply text.
8. Press `Enter`.
9. If text remains in editor, send `Control+Enter` fallback.

## Stable selector core

Primary composer selector:

```css
div[contenteditable="true"][role="textbox"][data-lexical-editor="true"]
```

Do not switch to placeholder-only selectors as primary matching.

## Regression guardrails

Do not change these without re-validating in browser:

- Do not call `_close_dialogs()` in reply path (it can close the post modal).
- Do not remove `dialog-first` lookup.
- Do not remove iframe fallback.
- Do not remove `Enter` -> `Control+Enter` fallback.
- Do not remove `wait_for(attached/visible)` before `evaluate()`.

## Debug checkpoints

Healthy logs:
- `Переход к цели: ...`
- `Поле найдено по селектору: ...`
- `Поле найдено. Активирую...`
- `Начинаю печать текста...`
- Optional: `Enter не отправил комментарий, пробую Control+Enter...`

Failure marker:
- `Ошибка: Поле ввода не найдено.`

## Verification commands

```bash
uv run pytest -q tests/test_worker_actions.py -k "reply_comment"
uv run pytest -q tests/test_worker_actions.py
```

Expected: all selected tests pass.

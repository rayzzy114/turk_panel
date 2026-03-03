# Facebook Commenting Working Baseline

Last validated: March 3, 2026

## Scope

This document fixes the known-good behavior for posting comments from `FacebookBrowser.leave_comment()`.

Source of truth in code:
- `worker.py` (`leave_comment`, `_dismiss_action_blockers`)

## Verified composer signature (Lexical)

Use this as the primary selector target:

```css
div[contenteditable="true"][role="textbox"][data-lexical-editor="true"]
```

Known real element shape (from DevTools):
- `contenteditable="true"`
- `role="textbox"`
- `data-lexical-editor="true"`
- `aria-label` can be language-dependent (`Yorum ...`, `Напишите комментарий...`, etc.)

Do not rely on placeholder text language as the primary selector.

## Required algorithm (do not simplify)

1. Open post URL.
2. Close only blocking popups (`Not Now`, `Şimdi değil`) via `_dismiss_action_blockers`.
3. Find input with selector priority:
   1. Inside modal first: `div[role="dialog"] <selector>`
   2. Then page-wide fallback selectors
   3. Then iframes (`page.frames`, excluding `main_frame`)
4. If not found, scroll modal to bottom and retry selector search.
5. If still not found, click comment trigger (`Yorum yaz|Yorum yap|Comment|Напишите`) and retry search.
6. Activate editor strictly in this order:
   1. `scroll_into_view_if_needed()`
   2. `wait_for(state="attached")`
   3. `wait_for(state="visible")`
   4. JS focus + focus event dispatch
   5. Real mouse click into bounding box
7. Type text through keyboard, then press `Enter`.
8. If text still remains in editor (`innerText.trim().length > 0`), send `Control+Enter` fallback.

## Regression risks

If any of the points below are removed, failures are likely:
- Replacing Lexical selector with text-only placeholder selector.
- Calling `_close_dialogs()` before comment entry (can close post modal).
- Dropping `wait_for(attached/visible)` before `evaluate()`.
- Removing iframe fallback.
- Sending only `Enter` without post-submit check + `Control+Enter` fallback.

## Debug log checkpoints

Expected logs during healthy flow:
- `Переход к цели: ...`
- `Поле найдено по селектору: ...`
- `Поле найдено. Активирую...`
- `Начинаю печать текста...`
- Optional: `Enter не отправил комментарий, пробую Control+Enter...`

Failure signature:
- `Ошибка: Поле ввода не найдено.`

## Quick verification command

```bash
uv run pytest -q tests/test_worker_actions.py
```

Expected result: all tests pass.

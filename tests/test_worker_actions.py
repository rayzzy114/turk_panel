from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

sys.path.append(str(Path(__file__).resolve().parents[1]))

import worker
from worker import AccountCaptchaError, AccountSessionData, FacebookBrowser


@pytest.fixture(autouse=True)
def _speedup_worker_timing(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fast_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(worker.asyncio, "sleep", _fast_sleep)
    monkeypatch.setattr(worker.random, "uniform", lambda a, b: a)
    monkeypatch.setattr(worker.random, "randint", lambda a, b: a)


@dataclass
class FakeLocator:
    click_error: Exception | None = None
    href: str | None = None
    text: str = ""
    visible: bool = True
    all_items: list["FakeLocator"] = field(default_factory=list)
    author_locator: "FakeLocator | None" = None
    text_locator: "FakeLocator | None" = None
    link_locator: "FakeLocator | None" = None
    button_locator: "FakeLocator | None" = None
    click_calls: int = 0
    typed_chars: list[str] = field(default_factory=list)
    type_delays: list[int] = field(default_factory=list)
    wait_states: list[str] = field(default_factory=list)
    evaluate_calls: list[str] = field(default_factory=list)
    evaluate_return_by_substring: dict[str, Any] = field(default_factory=dict)
    box: dict[str, float] | None = field(
        default_factory=lambda: {"x": 10.0, "y": 10.0, "width": 100.0, "height": 20.0}
    )
    scroll_calls: int = 0

    @property
    def first(self) -> "FakeLocator":
        if self.all_items:
            return self.all_items[0]
        return self

    async def click(
        self, timeout: int | None = None, force: bool | None = None
    ) -> None:
        self.click_calls += 1
        if self.click_error:
            raise self.click_error

    async def type(self, value: str, delay: int | None = None) -> None:
        self.typed_chars.append(value)
        if delay is not None:
            self.type_delays.append(delay)

    async def get_attribute(self, name: str) -> str | None:
        if name == "href":
            return self.href
        return None

    async def inner_text(self) -> str:
        return self.text

    async def is_visible(self, timeout: int | None = None) -> bool:
        return self.visible

    async def wait_for(self, state: str = "visible", timeout: int | None = None) -> None:
        self.wait_states.append(state)
        if state == "visible" and not self.visible:
            raise RuntimeError("locator is not visible")
        if state == "attached" and await self.count() == 0:
            raise RuntimeError("locator is not attached")

    async def evaluate(self, script: str) -> Any:
        self.evaluate_calls.append(script)
        for key, value in self.evaluate_return_by_substring.items():
            if key in script:
                return value
        return None

    async def scroll_into_view_if_needed(self, timeout: int | None = None) -> None:
        self.scroll_calls += 1

    async def bounding_box(self) -> dict[str, float] | None:
        return self.box

    async def all(self) -> list["FakeLocator"]:
        return self.all_items

    async def count(self) -> int:
        if self.all_items:
            return len(self.all_items)
        return 1 if self.visible else 0

    def filter(
        self,
        *,
        has_text: Any | None = None,
        visible: bool | None = None,
        has: Any | None = None,
    ) -> "FakeLocator":
        candidates = self.all_items if self.all_items else [self]
        filtered: list[FakeLocator] = []
        for item in candidates:
            matched = True
            if has_text is not None:
                text = item.text or ""
                try:
                    matched = bool(has_text.search(text))
                except Exception:
                    matched = str(has_text) in text
            if matched and visible is not None:
                matched = item.visible is visible
            if matched and has is not None:
                nested_items = getattr(has, "all_items", None)
                nested_visible = getattr(has, "visible", False)
                matched = bool(nested_items) or bool(nested_visible)
            if matched:
                filtered.append(item)
        if filtered:
            return FakeLocator(all_items=filtered)
        return FakeLocator(
            visible=False, click_error=RuntimeError("filtered locator not found")
        )

    def locator(self, selector: str) -> "FakeLocator":
        if "href*='comment_id='" in selector or 'href*="comment_id="' in selector:
            if self.link_locator:
                return self.link_locator
        if ("[role='button']" in selector or '[role="button"]' in selector) and self.button_locator:
            return self.button_locator
        if "a[role='link']" in selector or 'a[role="link"]' in selector:
            if self.author_locator:
                return self.author_locator
        if "h3 a" in selector and self.author_locator:
            return self.author_locator
        if "div[dir='auto']" in selector or 'div[dir="auto"]' in selector:
            if self.text_locator:
                return self.text_locator
        return FakeLocator(
            visible=False,
            click_error=RuntimeError(f"nested locator not found: {selector}"),
        )


class FakeKeyboard:
    def __init__(self) -> None:
        self.pressed: list[str] = []
        self.typed: list[str] = []
        self.type_delays: list[int] = []
        self.typed: list[str] = []
        self.type_delays: list[int] = []
        self.inserted: list[str] = []

    async def press(self, key: str) -> None:
        self.pressed.append(key)
        if len(key) == 1:
            if not self.typed or self.pressed[-2:] == ["Enter", key]:
                self.typed.append("")
            self.typed[-1] += key

    async def type(self, value: str, delay: int | None = None) -> None:
        self.typed.append(value)
        if delay is not None:
            self.type_delays.append(delay)

    async def insert_text(self, value: str) -> None:
        self.inserted.append(value)
        self.typed.append(value)


class FakeContext:
    def __init__(self, cookies_data: list[dict[str, Any]] | None = None) -> None:
        self._cookies_data = list(cookies_data or [])

    async def cookies(self) -> list[dict[str, Any]]:
        return list(self._cookies_data)


class FakePage:
    def __init__(
        self,
        *,
        role_locators: list[FakeLocator] | None = None,
        label_locators: list[FakeLocator] | None = None,
        articles: list[FakeLocator] | None = None,
        comment_inputs: list[FakeLocator] | None = None,
        like_buttons: list[FakeLocator] | None = None,
        comments: list[FakeLocator] | None = None,
        sort_buttons: list[FakeLocator] | None = None,
        sort_menuitems: list[FakeLocator] | None = None,
        login_form_count: int = 0,
        dialog_count: int = 0,
        url: str = "https://www.facebook.com/",
        goto_error: Exception | None = None,
        goto_result_url: str | None = None,
    ) -> None:
        self.role_locators = list(role_locators or [])
        self.label_locators = list(label_locators or [])
        self.articles = list(articles or [])
        self.comment_inputs = list(comment_inputs or [])
        self.like_buttons = list(like_buttons or [])
        self.comments = list(comments or [])
        self.sort_buttons = list(sort_buttons or [])
        self.sort_menuitems = list(sort_menuitems or [])
        self.login_form_count = login_form_count
        self.dialog_count = dialog_count
        self.goto_error = goto_error
        self.goto_result_url = goto_result_url
        self.waits: list[float] = []
        self.goto_urls: list[str] = []
        self.keyboard = FakeKeyboard()
        self.scroll_calls: int = 0
        self.mouse = self
        self.url = url
        self.mouse_clicks: list[tuple[float | None, float | None]] = []
        self.frames: list[Any] = []
        self.main_frame: Any = object()

    async def goto(
        self,
        target_url: str,
        wait_until: str,
        timeout: int | None = None,
        referer: str | None = None,
    ) -> None:
        if self.goto_error:
            raise self.goto_error
        self.goto_urls.append(target_url)
        self.url = self.goto_result_url or target_url

    async def wait_for_timeout(self, timeout_ms: float) -> None:
        self.waits.append(timeout_ms)

    async def wheel(self, delta_x: int, delta_y: int) -> None:
        self.scroll_calls += 1

    async def click(self, x: float | None = None, y: float | None = None) -> None:
        self.mouse_clicks.append((x, y))

    async def move(self, x: float, y: float, steps: int = 1) -> None:
        self.scroll_calls += steps

    async def evaluate(self, script: str) -> None:
        self.scroll_calls += 1

    def get_by_role(self, role: str, name: Any, exact: bool | None = None) -> FakeLocator:
        if self.role_locators:
            return self.role_locators.pop(0)
        return FakeLocator(click_error=RuntimeError("role locator not found"))

    def get_by_label(self, label: Any) -> FakeLocator:
        if self.label_locators:
            return self.label_locators.pop(0)
        return FakeLocator(click_error=RuntimeError("label locator not found"))

    def locator(self, selector: str) -> FakeLocator:
        if 'form[action*="login"]' in selector or "form[action*='login']" in selector:
            return FakeLocator(
                all_items=[FakeLocator() for _ in range(self.login_form_count)],
                visible=self.login_form_count > 0,
            )
        if 'div[role="dialog"]' in selector or "div[role='dialog']" in selector:
            return FakeLocator(
                all_items=[FakeLocator() for _ in range(self.dialog_count)],
                visible=self.dialog_count > 0,
            )
        if 'div[role="button"]' in selector or "div[role='button']" in selector:
            return FakeLocator(all_items=self.sort_buttons)
        if 'div[role="menuitem"]' in selector or "div[role='menuitem']" in selector:
            return FakeLocator(all_items=self.sort_menuitems)
        if "aria-label*='Comment'" in selector or 'aria-label*="Comment"' in selector:
            return FakeLocator(all_items=self.comments)
        if (
            "aria-label*='Комментарий'" in selector
            or 'aria-label*="Комментарий"' in selector
        ):
            return FakeLocator(all_items=self.comments)
        if 'a[href*="' in selector or "a[href*='" in selector:
            return FakeLocator(visible=True)
        if (
            "contenteditable" in selector
            or "data-lexical-editor" in selector
            or 'role="textbox"' in selector
            or "role='textbox'" in selector
            or "textarea" in selector
        ):
            return FakeLocator(
                all_items=self.comment_inputs,
                visible=bool(self.comment_inputs),
            )
        if (
            "data-ad-rendering-role=\"like_button\"" in selector
            or "data-ad-rendering-role='like_button'" in selector
        ):
            return FakeLocator(
                all_items=self.like_buttons,
                visible=bool(self.like_buttons),
            )
        if 'div[role="article"]' in selector or "div[role='article']" in selector:
            return FakeLocator(all_items=self.articles, visible=bool(self.articles))
        if "comment_id=" in selector:
            return FakeLocator(
                href="https://www.facebook.com/permalink.php?comment_id=123"
            )
        return FakeLocator(visible=False, click_error=RuntimeError("locator not found"))


def _create_browser_with_page(
    page: FakePage,
    *,
    cookies_data: list[dict[str, Any]] | None = None,
) -> FacebookBrowser:
    browser = FacebookBrowser(
        account=AccountSessionData(
            login="demo_user",
            password="demo_pass",
            user_agent="Mozilla/5.0 test",
        )
    )
    browser._page = page
    initial_cookies = (
        cookies_data if cookies_data is not None else [{"name": "c_user", "value": "1"}]
    )
    browser._context = FakeContext(initial_cookies)
    return browser


@pytest.mark.asyncio
async def test_like_post_returns_true_when_like_clicked() -> None:
    like_button = FakeLocator()
    page = FakePage(role_locators=[like_button])
    browser = _create_browser_with_page(page)

    result = await browser.like_post("https://example.com/post")

    assert result is True
    assert page.mouse_clicks


@pytest.mark.asyncio
async def test_like_post_returns_true_when_like_button_not_found() -> None:
    page = FakePage(
        role_locators=[FakeLocator(click_error=RuntimeError("not found"))],
        label_locators=[FakeLocator(click_error=RuntimeError("not found"))],
    )
    browser = _create_browser_with_page(page)

    result = await browser.like_post("https://example.com/post")

    assert result is True


@pytest.mark.asyncio
async def test_leave_comment_types_text_and_returns_true() -> None:
    comment_input = FakeLocator()
    page = FakePage(comment_inputs=[comment_input])
    browser = _create_browser_with_page(page)

    result = await browser.leave_comment("https://example.com/post", "Привет")

    assert result is True
    assert page.keyboard.typed == ["Привет"]
    assert all(delay > 0 for delay in page.keyboard.type_delays)
    assert "attached" in comment_input.wait_states
    assert "visible" in comment_input.wait_states
    assert any("focus" in script for script in comment_input.evaluate_calls)
    assert page.keyboard.pressed[-1] == "Enter"


@pytest.mark.asyncio
async def test_leave_comment_returns_false_when_open_comment_fails() -> None:
    page = FakePage(comment_inputs=[])
    browser = _create_browser_with_page(page)

    result = await browser.leave_comment("https://example.com/post", "Тест")

    assert result is False


@pytest.mark.asyncio
async def test_leave_comment_raises_checkpoint_error_when_redirected_to_checkpoint() -> (
    None
):
    page = FakePage(goto_result_url="https://www.facebook.com/checkpoint/8282")
    browser = _create_browser_with_page(page)

    with pytest.raises(AccountCaptchaError, match="Checkpoint"):
        await browser.leave_comment("https://example.com/post", "Тест")


@pytest.mark.asyncio
async def test_leave_comment_waits_on_checkpoint_before_failing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = FakePage(goto_result_url="https://www.facebook.com/checkpoint/8282")
    browser = _create_browser_with_page(page)

    async def _noop(*_: Any, **__: Any) -> None:
        return None

    sleeps: list[float] = []

    async def _tracked_sleep(seconds: float) -> None:
        sleeps.append(float(seconds))

    monkeypatch.setattr(FacebookBrowser, "_pre_action_warmup", _noop)
    monkeypatch.setattr(FacebookBrowser, "_dismiss_action_blockers", _noop)
    monkeypatch.setattr(worker.asyncio, "sleep", _tracked_sleep)
    monkeypatch.setenv("FB_KEEP_BROWSER_ON_CHECKPOINT", "1")
    monkeypatch.setenv("FB_CHECKPOINT_WAIT_SECONDS", "15")
    monkeypatch.setenv("FB_CHECKPOINT_POLL_SECONDS", "5")

    with pytest.raises(AccountCaptchaError, match="Checkpoint"):
        await browser.leave_comment("https://example.com/post", "Тест")

    # 1 ожидание после goto + 3 poll-а на checkpoint
    assert sleeps.count(5.0) >= 3


@pytest.mark.asyncio
async def test_leave_comment_continues_after_manual_checkpoint_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    comment_input = FakeLocator()
    page = FakePage(
        goto_result_url="https://www.facebook.com/checkpoint/8282",
        comment_inputs=[comment_input],
    )
    browser = _create_browser_with_page(page)

    async def _noop(*_: Any, **__: Any) -> None:
        return None

    async def _tracked_sleep(seconds: float) -> None:
        # Эмулируем, что пользователь подтвердил вход и FB вернул на обычную страницу.
        if float(seconds) == 1.0:
            page.url = "https://www.facebook.com/"

    monkeypatch.setattr(FacebookBrowser, "_pre_action_warmup", _noop)
    monkeypatch.setattr(FacebookBrowser, "_dismiss_action_blockers", _noop)
    monkeypatch.setattr(worker.asyncio, "sleep", _tracked_sleep)
    monkeypatch.setenv("FB_KEEP_BROWSER_ON_CHECKPOINT", "1")
    monkeypatch.setenv("FB_CHECKPOINT_WAIT_SECONDS", "10")
    monkeypatch.setenv("FB_CHECKPOINT_POLL_SECONDS", "1")

    result = await browser.leave_comment("https://example.com/post", "Тест")

    assert result is True


@pytest.mark.asyncio
async def test_leave_comment_uses_control_enter_when_enter_did_not_submit() -> None:
    comment_input = FakeLocator(
        evaluate_return_by_substring={"innerText": True},
    )
    page = FakePage(comment_inputs=[comment_input])
    browser = _create_browser_with_page(page)

    result = await browser.leave_comment("https://example.com/post", "Тест")

    assert result is True
    assert page.keyboard.pressed[-2:] == ["Enter", "Control+Enter"]


@pytest.mark.asyncio
async def test_leave_comment_with_emoji_text_uses_insert_text_fallback() -> None:
    comment_input = FakeLocator()
    page = FakePage(comment_inputs=[comment_input])
    browser = _create_browser_with_page(page)

    original_press = page.keyboard.press

    async def _emoji_unsupported_press(key: str) -> None:
        if key == "👍":
            raise RuntimeError('Keyboard.press: Unknown key: "👍"')
        await original_press(key)

    page.keyboard.press = _emoji_unsupported_press  # type: ignore[method-assign]

    result = await browser.leave_comment("https://example.com/post", "👍👍")

    assert result is True
    assert page.keyboard.inserted == ["👍", "👍"]


@pytest.mark.asyncio
async def test_like_comment_returns_true_when_like_clicked() -> None:
    like_button = FakeLocator(text="Beğen")
    article = FakeLocator(button_locator=like_button)
    page = FakePage(articles=[article])
    browser = _create_browser_with_page(page)

    result = await browser.like_comment(
        "https://example.com/permalink.php?comment_id=1"
    )

    assert result is True
    assert page.mouse_clicks


@pytest.mark.asyncio
async def test_like_comment_returns_true_when_dialog_like_button_is_found() -> None:
    page = FakePage(like_buttons=[FakeLocator(text="Beğen")])
    browser = _create_browser_with_page(page)

    result = await browser.like_comment("https://example.com/post?comment_id=920444257376774")

    assert result is True
    assert page.mouse_clicks


@pytest.mark.asyncio
async def test_like_comment_returns_false_when_like_not_found() -> None:
    page = FakePage(
        role_locators=[FakeLocator(click_error=RuntimeError("not found"))],
        label_locators=[FakeLocator(click_error=RuntimeError("not found"))],
    )
    browser = _create_browser_with_page(page)

    result = await browser.like_comment(
        "https://example.com/permalink.php?comment_id=1"
    )

    assert result is False


@pytest.mark.asyncio
async def test_reply_comment_types_text_and_submits_with_enter() -> None:
    reply_input = FakeLocator()
    page = FakePage(comment_inputs=[reply_input])
    browser = _create_browser_with_page(page)

    result = await browser.reply_comment(
        "https://example.com/permalink.php?comment_id=1", "Ответ"
    )

    assert result is True
    assert page.keyboard.typed == ["Ответ"]
    assert page.keyboard.pressed[-1] == "Enter"


@pytest.mark.asyncio
async def test_reply_comment_returns_false_when_reply_button_not_found() -> None:
    page = FakePage(
        role_locators=[FakeLocator(click_error=RuntimeError("not found"))],
        label_locators=[FakeLocator(click_error=RuntimeError("not found"))],
    )
    browser = _create_browser_with_page(page)

    result = await browser.reply_comment(
        "https://example.com/permalink.php?comment_id=1", "Ответ"
    )

    assert result is False


@pytest.mark.asyncio
async def test_is_authorized_returns_false_when_c_user_cookie_is_missing() -> None:
    page = FakePage()
    browser = _create_browser_with_page(page, cookies_data=[])

    result = await browser._is_authorized()

    assert result is False


@pytest.mark.asyncio
async def test_is_authorized_returns_false_when_login_wall_detected() -> (
    None
):
    page = FakePage(login_form_count=1, dialog_count=1)
    browser = _create_browser_with_page(page, cookies_data=[])

    result = await browser._is_authorized()

    assert result is False

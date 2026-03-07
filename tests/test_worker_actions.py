from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest

sys.path.append(str(Path(__file__).resolve().parents[1]))

import worker
from models import CheckpointType
from worker import (
    AccountCaptchaError,
    AccountInvalidCredentialsError,
    AccountSessionData,
    FacebookBrowser,
)


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

    async def wait_for(
        self, state: str = "visible", timeout: int | None = None
    ) -> None:
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
        if (
            "[role='button']" in selector or '[role="button"]' in selector
        ) and self.button_locator:
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
        feed_locators: list[FakeLocator] | None = None,
        profile_locators: list[FakeLocator] | None = None,
        reels_videos: list[FakeLocator] | None = None,
        sort_buttons: list[FakeLocator] | None = None,
        sort_menuitems: list[FakeLocator] | None = None,
        login_form_count: int = 0,
        dialog_count: int = 0,
        url: str = "https://www.facebook.com/",
        goto_error: Exception | None = None,
        goto_result_url: str | None = None,
        body_text: str = "",
        selector_locators: dict[str, FakeLocator] | None = None,
    ) -> None:
        self.role_locators = list(role_locators or [])
        self.label_locators = list(label_locators or [])
        self.articles = list(articles or [])
        self.comment_inputs = list(comment_inputs or [])
        self.like_buttons = list(like_buttons or [])
        self.comments = list(comments or [])
        self.feed_locators = list(feed_locators or [])
        self.profile_locators = list(profile_locators or [])
        self.reels_videos = list(reels_videos or [])
        self.sort_buttons = list(sort_buttons or [])
        self.sort_menuitems = list(sort_menuitems or [])
        self.login_form_count = login_form_count
        self.dialog_count = dialog_count
        self.goto_error = goto_error
        self.goto_result_url = goto_result_url
        self.body_text = body_text
        self.selector_locators = dict(selector_locators or {})
        self.waits: list[float] = []
        self.load_states: list[tuple[str, int | None]] = []
        self.goto_urls: list[str] = []
        self.keyboard = FakeKeyboard()
        self.scroll_calls: int = 0
        self.mouse = self
        self.url = url
        self.mouse_clicks: list[tuple[float | None, float | None]] = []
        self.frames: list[Any] = []
        self.main_frame: Any = object()
        self.screenshot_paths: list[str] = []

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

    async def wait_for_load_state(
        self, state: str = "load", timeout: int | None = None
    ) -> None:
        self.load_states.append((state, timeout))

    async def wheel(self, delta_x: int, delta_y: int) -> None:
        self.scroll_calls += 1

    async def click(self, x: float | None = None, y: float | None = None) -> None:
        self.mouse_clicks.append((x, y))

    async def move(self, x: float, y: float, steps: int = 1) -> None:
        self.scroll_calls += steps

    async def evaluate(self, script: str) -> None:
        self.scroll_calls += 1

    async def inner_text(self, selector: str) -> str:
        if selector == "body":
            return self.body_text
        return ""

    async def content(self) -> str:
        return self.body_text

    async def screenshot(self, path: str, full_page: bool = False) -> None:
        _ = full_page
        self.screenshot_paths.append(path)

    def get_by_role(
        self, role: str, name: Any, exact: bool | None = None
    ) -> FakeLocator:
        if self.role_locators:
            return self.role_locators.pop(0)
        return FakeLocator(click_error=RuntimeError("role locator not found"))

    def get_by_label(self, label: Any) -> FakeLocator:
        if self.label_locators:
            return self.label_locators.pop(0)
        return FakeLocator(click_error=RuntimeError("label locator not found"))

    def locator(self, selector: str) -> FakeLocator:
        for pattern, locator in self.selector_locators.items():
            if pattern in selector:
                return locator
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
        if (
            'aria-label="Like"' in selector
            or 'aria-label="Beğen"' in selector
            or 'aria-label="Нравится"' in selector
        ):
            return FakeLocator(
                all_items=self.like_buttons,
                visible=bool(self.like_buttons),
            )
        if (
            'aria-label="Comment"' in selector
            or 'aria-label="Yorum Yap"' in selector
            or 'aria-label="Комментировать"' in selector
        ):
            return FakeLocator(all_items=self.comments, visible=bool(self.comments))
        if (
            '[role="feed"]' in selector
            or "div[data-pagelet=\"FeedUnit_0\"]" in selector
            or "div[data-pagelet='FeedUnit_0']" in selector
        ):
            return FakeLocator(
                all_items=self.feed_locators,
                visible=bool(self.feed_locators),
            )
        if (
            "ProfileTimeline" in selector
            or "profile" in selector.lower()
            or "cover" in selector.lower()
        ):
            return FakeLocator(
                all_items=self.profile_locators,
                visible=bool(self.profile_locators),
            )
        if "video" in selector:
            return FakeLocator(all_items=self.reels_videos, visible=bool(self.reels_videos))
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
            'data-ad-rendering-role="like_button"' in selector
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
    browser._page = cast(Any, page)
    initial_cookies = (
        cookies_data if cookies_data is not None else [{"name": "c_user", "value": "1"}]
    )
    browser._context = cast(Any, FakeContext(initial_cookies))
    return browser


@pytest.mark.asyncio
async def test_like_post_returns_true_when_like_clicked() -> None:
    like_button = FakeLocator()
    page = FakePage(role_locators=[like_button])
    browser = _create_browser_with_page(page)

    result = await browser.like_post("https://example.com/post")

    assert result is True
    assert like_button.click_calls == 1


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
    assert like_button.click_calls == 1


@pytest.mark.asyncio
async def test_like_comment_returns_true_when_dialog_like_button_is_found() -> None:
    dialog_like = FakeLocator(text="Beğen")
    page = FakePage(like_buttons=[dialog_like])
    browser = _create_browser_with_page(page)

    result = await browser.like_comment(
        "https://example.com/post?comment_id=920444257376774"
    )

    assert result is True
    assert dialog_like.click_calls == 1


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
async def test_is_authorized_returns_false_when_login_wall_detected() -> None:
    page = FakePage(login_form_count=1, dialog_count=1)
    browser = _create_browser_with_page(page, cookies_data=[])

    result = await browser._is_authorized()

    assert result is False


@pytest.mark.asyncio
async def test_is_authorized_returns_false_for_saved_profile_login_wall() -> None:
    page = FakePage(
        body_text="Başka bir profil kullan Yeni hesap oluştur Devam",
    )
    browser = _create_browser_with_page(
        page, cookies_data=[{"name": "c_user", "value": "1"}]
    )

    result = await browser._is_authorized()

    assert result is False


@pytest.mark.asyncio
async def test_check_session_alive_returns_false_for_saved_profile_login_wall() -> None:
    page = FakePage(
        body_text="Başka bir profil kullan Yeni hesap oluştur Devam",
    )
    browser = _create_browser_with_page(
        page, cookies_data=[{"name": "c_user", "value": "1"}]
    )

    result = await browser._check_session_alive()

    assert result is False


@pytest.mark.asyncio
async def test_login_handles_saved_profile_password_modal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    continue_button = FakeLocator()
    password_input = FakeLocator()
    submit_button = FakeLocator()
    login_form = FakeLocator(visible=False)
    avatar = FakeLocator()
    page = FakePage(
        role_locators=[continue_button, submit_button],
        selector_locators={
            'input[name="email"]': FakeLocator(visible=False),
            "input[type='password']": password_input,
            'form[action*="login"]': login_form,
            'img[alt][src*="scontent"]': avatar,
        },
        body_text="Devam",
    )
    browser = _create_browser_with_page(page, cookies_data=[])

    auth_checks = iter([False, True])

    async def _fake_is_authorized() -> bool:
        return next(auth_checks)

    async def _noop_raise_if_checkpoint(stage: str) -> None:
        _ = stage
        return None

    monkeypatch.setattr(browser, "_is_authorized", _fake_is_authorized)
    monkeypatch.setattr(browser, "_raise_if_checkpoint", _noop_raise_if_checkpoint)

    await browser.login()

    assert continue_button.click_calls == 1
    assert "".join(page.keyboard.typed) == "demo_pass"


@pytest.mark.asyncio
async def test_login_does_not_treat_standard_login_page_as_saved_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    email_input = FakeLocator()
    password_input = FakeLocator()
    page = FakePage(
        selector_locators={
            'input[name="email"]': email_input,
            "input[type='password']": password_input,
            'input[name="pass"]': password_input,
        },
        body_text="Facebook'a Giriş Yap E-posta adresi veya cep telefonu numarası Şifre",
    )
    browser = _create_browser_with_page(page, cookies_data=[])

    auth_checks = iter([False, True])

    async def _fake_is_authorized() -> bool:
        return next(auth_checks)

    async def _noop_raise_if_checkpoint(stage: str) -> None:
        _ = stage
        return None

    async def _noop_raise_if_invalid_credentials(stage: str) -> None:
        _ = stage
        return None

    monkeypatch.setattr(browser, "_is_authorized", _fake_is_authorized)
    monkeypatch.setattr(browser, "_raise_if_checkpoint", _noop_raise_if_checkpoint)
    monkeypatch.setattr(
        browser, "_raise_if_invalid_credentials", _noop_raise_if_invalid_credentials
    )

    await browser.login()

    assert email_input.click_calls == 1
    assert "".join(page.keyboard.typed) == "demo_userdemo_pass"


@pytest.mark.asyncio
async def test_login_uses_placeholder_identifier_field_on_standard_login_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    email_input = FakeLocator()
    password_input = FakeLocator()
    page = FakePage(
        selector_locators={
            'input[placeholder*="E-posta"]': email_input,
            "input[type='password']": password_input,
            'input[name="pass"]': password_input,
        },
        body_text="Facebook'a Giriş Yap E-posta adresi veya cep telefonu numarası Şifre",
    )
    browser = _create_browser_with_page(page, cookies_data=[])

    auth_checks = iter([False, True])

    async def _fake_is_authorized() -> bool:
        return next(auth_checks)

    async def _noop_raise_if_checkpoint(stage: str) -> None:
        _ = stage
        return None

    async def _noop_raise_if_invalid_credentials(stage: str) -> None:
        _ = stage
        return None

    monkeypatch.setattr(browser, "_is_authorized", _fake_is_authorized)
    monkeypatch.setattr(browser, "_raise_if_checkpoint", _noop_raise_if_checkpoint)
    monkeypatch.setattr(
        browser, "_raise_if_invalid_credentials", _noop_raise_if_invalid_credentials
    )

    await browser.login()

    assert email_input.click_calls == 1
    assert "".join(page.keyboard.typed) == "demo_userdemo_pass"


@pytest.mark.asyncio
async def test_login_raises_invalid_credentials_on_wrong_password_modal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    continue_button = FakeLocator()
    password_input = FakeLocator()
    avatar = FakeLocator()
    page = FakePage(
        role_locators=[continue_button],
        selector_locators={
            'input[name="email"]': FakeLocator(visible=False),
            "input[type='password']": password_input,
            'img[alt][src*="scontent"]': avatar,
        },
        body_text="Girdiğin şifre yanlış.",
    )
    browser = _create_browser_with_page(page, cookies_data=[])

    async def _false_is_authorized() -> bool:
        return False

    async def _noop_raise_if_checkpoint(stage: str) -> None:
        _ = stage
        return None

    monkeypatch.setattr(browser, "_is_authorized", _false_is_authorized)
    monkeypatch.setattr(browser, "_raise_if_checkpoint", _noop_raise_if_checkpoint)

    with pytest.raises(AccountInvalidCredentialsError, match="rejected login credentials"):
        await browser.login()


@pytest.mark.asyncio
async def test_warmup_executes_actions_and_returns_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = FakePage()
    browser = _create_browser_with_page(page)
    calls: list[str] = []

    async def _record(name: str) -> None:
        calls.append(name)

    monkeypatch.setattr(
        browser,
        "_warmup_scroll_feed",
        lambda: _record("_warmup_scroll_feed"),
    )
    monkeypatch.setattr(
        browser,
        "_warmup_like_random_post",
        lambda: _record("_warmup_like_random_post"),
    )
    monkeypatch.setattr(
        browser,
        "_warmup_open_comments",
        lambda: _record("_warmup_open_comments"),
    )
    monkeypatch.setattr(
        browser,
        "_warmup_visit_profile",
        lambda: _record("_warmup_visit_profile"),
    )
    monkeypatch.setattr(
        browser,
        "_warmup_watch_reels",
        lambda: _record("_warmup_watch_reels"),
    )
    monkeypatch.setattr(worker.random, "choice", lambda seq: seq[0])
    async def _alive() -> bool:
        return True

    monkeypatch.setattr(browser, "_check_session_alive", _alive)

    result = await browser.warmup(duration_seconds=1)

    assert result["result"] == "completed"
    assert result["actions_attempted"] >= 1
    assert result["actions_succeeded"] >= 1
    assert result["actions_failed"] == 0
    assert isinstance(result["action_log"], list)
    assert calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body_text", "expected"),
    [
        ("güvenlik kodu girin", CheckpointType.CODE_VERIFICATION),
        ("kimliğini doğrula ve selfie yükle", CheckpointType.FACE_VERIFICATION),
        ("olağandışı giriş tespit edildi", CheckpointType.SUSPICIOUS_LOGIN),
        ("hesabın devre dışı bırakıldı", CheckpointType.ACCOUNT_DISABLED),
        ("checkpoint page without known keywords", CheckpointType.UNKNOWN_CHECKPOINT),
    ],
)
async def test_detect_checkpoint_type_classifies_page_text(
    body_text: str, expected: CheckpointType
) -> None:
    page = FakePage(body_text=body_text, url="https://www.facebook.com/checkpoint/1")
    browser = _create_browser_with_page(page)

    detected = await browser.detect_checkpoint_type()

    assert detected == expected


@pytest.mark.asyncio
async def test_detect_checkpoint_type_returns_unknown_on_read_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = FakePage(body_text="", url="https://www.facebook.com/checkpoint/1")
    browser = _create_browser_with_page(page)

    async def _broken_inner_text(selector: str) -> str:
        _ = selector
        raise RuntimeError("broken")

    async def _broken_content() -> str:
        raise RuntimeError("broken content")

    monkeypatch.setattr(page, "inner_text", _broken_inner_text)
    monkeypatch.setattr(page, "content", _broken_content)

    detected = await browser.detect_checkpoint_type()

    assert detected == CheckpointType.UNKNOWN_CHECKPOINT

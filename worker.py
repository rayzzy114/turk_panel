from __future__ import annotations

import asyncio
import logging
import os
import random
import re
from dataclasses import dataclass
from typing import Any, Final, cast

from imap_utils import get_facebook_code
from camoufox.async_api import AsyncCamoufox
from camoufox.exceptions import InvalidIP
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
)

CookieDict = dict[str, Any]
CookieList = list[CookieDict]


@dataclass(slots=True)
class ProxyConfig:
    host: str
    port: int
    user: str | None = None
    password: str | None = None

    def to_playwright_proxy(self) -> dict[str, str]:
        proxy: dict[str, str] = {"server": f"http://{self.host}:{self.port}"}
        if self.user:
            proxy["username"] = self.user
        if self.password:
            proxy["password"] = self.password
        return proxy


@dataclass(slots=True)
class AccountSessionData:
    login: str
    password: str
    user_agent: str
    cookies: CookieList | None = None
    storage_state: dict[str, Any] | None = None
    proxy: ProxyConfig | None = None
    email_login: str | None = None
    email_password: str | None = None
    imap_server: str | None = None


class AccountCaptchaError(RuntimeError):
    """Raised when Facebook blocks the session with a login wall/captcha."""


class FacebookBrowser:
    BASE_URL: Final[str] = "https://www.facebook.com/"
    DEFAULT_TIMEOUT_MS: Final[int] = 60_000
    LOGIN_TIMEOUT_MS: Final[int] = 120_000

    PATTERNS = {
        "LIKE": re.compile(r"\b(Нравится|Like|Beğen)\b", re.IGNORECASE),
    }
    CHECKPOINT_URL_RE: Final[re.Pattern[str]] = re.compile(
        r"/checkpoint/?", re.IGNORECASE
    )

    def __init__(
        self,
        account: AccountSessionData,
        headless: bool = True,
        strict_cookie_session: bool = True,
        log_callback: Any | None = None,
    ) -> None:
        self.account = account
        self.headless = headless
        self.strict_cookie_session = strict_cookie_session
        self.log_callback = log_callback
        self.logger = logging.getLogger(self.__class__.__name__)

        self._playwright: Any | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._camoufox: AsyncCamoufox | None = None

    async def _log(self, message: str) -> None:
        self.logger.info(message)
        if self.log_callback:
            await self.log_callback(message)

    @staticmethod
    def _parse_bool(value: str | None, *, default: bool = False) -> bool:
        if value is None:
            return default
        return value.strip().lower() in {"1", "true", "yes", "on"}

    async def __aenter__(self) -> FacebookBrowser:
        await self.start()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()

    async def start(self) -> None:
        try:
            launched = None
            proxy_kwargs: dict[str, Any] = {}
            if self.account.proxy:
                proxy_kwargs["proxy"] = self.account.proxy.to_playwright_proxy()
                proxy_kwargs["geoip"] = True

            # 1) Normal launch
            try:
                self._camoufox = AsyncCamoufox(
                    headless=self.headless,
                    humanize=True,
                    os=["windows", "macos"],
                    **proxy_kwargs,
                    # Camoufox takes care of stealth without patching
                )
                launched = await self._camoufox.__aenter__()
            except InvalidIP:
                # 2) Proxy IP introspection failed: retry with geoip disabled
                if self._camoufox:
                    await self._camoufox.__aexit__(None, None, None)
                fallback_kwargs = dict(proxy_kwargs)
                fallback_kwargs.pop("geoip", None)
                self.logger.warning(
                    "Camoufox geoip failed for %s. Retrying with geoip disabled.",
                    self.account.login,
                )
                self._camoufox = AsyncCamoufox(
                    headless=self.headless,
                    humanize=True,
                    os=["windows", "macos"],
                    **fallback_kwargs,
                )
                launched = await self._camoufox.__aenter__()
            except Exception as exc:
                # 3) Headed mode on server without DISPLAY: force headless retry
                if (
                    not self.headless
                    and "no DISPLAY environment variable specified" in str(exc)
                ):
                    if self._camoufox:
                        await self._camoufox.__aexit__(None, None, None)
                    self.logger.warning(
                        "DISPLAY not found for %s. Retrying in headless mode.",
                        self.account.login,
                    )
                    self._camoufox = AsyncCamoufox(
                        headless=True,
                        humanize=True,
                        os=["windows", "macos"],
                        **proxy_kwargs,
                    )
                    launched = await self._camoufox.__aenter__()
                else:
                    raise

            # Restore storage state if we have it
            context_kwargs: dict[str, Any] = {}
            if self.account.storage_state:
                context_kwargs["storage_state"] = self.account.storage_state

            # При прокси+geoip Camoufox сам подберет geo timezone/locale.
            if self.account.proxy:
                context_kwargs["permissions"] = ["notifications"]
            else:
                context_kwargs["locale"] = "tr-TR"
                context_kwargs["timezone_id"] = "Europe/Istanbul"
                context_kwargs["permissions"] = ["notifications"]

            if hasattr(launched, "new_context"):
                self._browser = cast(Browser, launched)
                self._context = await self._browser.new_context(**context_kwargs)
            else:
                self._context = cast(BrowserContext, launched)

            self._page = await self._context.new_page()
            self._page.set_default_timeout(self.DEFAULT_TIMEOUT_MS)

        except Exception as exc:
            self.logger.exception("Не удалось запустить браузер.")
            await self.close()
            raise RuntimeError("Не удалось запустить FacebookBrowser.") from exc

    @property
    def page(self) -> Page:
        if not self._page:
            raise RuntimeError("No page")
        return self._page

    async def get_storage_state(self) -> dict[str, Any] | None:
        if self._context:
            return cast(dict[str, Any], await self._context.storage_state())
        return None

    async def close(self) -> None:
        if self._context:
            try:
                await self._context.close()
            except Exception:
                pass
        if self._camoufox:
            try:
                await self._camoufox.__aexit__(None, None, None)
            except Exception:
                pass

    async def stop(self) -> None:
        """Stops browser resources. Alias for close()."""
        await self.close()

    async def _human_type(self, text: str) -> None:
        """Эмуляция человеческого набора текста: разные задержки, микропаузы."""
        for char in text:
            try:
                await self.page.keyboard.press(char)
            except Exception as exc:
                if "Unknown key" not in str(exc):
                    raise
                insert_text = getattr(self.page.keyboard, "insert_text", None)
                if callable(insert_text):
                    await insert_text(char)
                else:
                    await self.page.keyboard.type(char)
            # Базовая задержка
            delay = random.uniform(0.05, 0.15)
            # Иногда делаем "микропаузу" (человек задумался)
            if random.random() < 0.05:
                delay += random.uniform(0.5, 1.5)
            await asyncio.sleep(delay)

    async def _human_scroll(self, distance: int = 400, *, times: int | None = None) -> None:
        """Human-like scrolling via randomized step batches and pauses."""
        loops = times if times is not None else 1
        for _ in range(max(loops, 1)):
            scroll_distance = (
                random.randint(220, 620) if times is not None else distance
            )
            steps = random.randint(3, 8)
            step_distance = scroll_distance / steps
            for _ in range(steps):
                await self.page.mouse.wheel(
                    0,
                    step_distance + random.uniform(-20, 20),
                )
                await asyncio.sleep(random.uniform(0.1, 0.4))
            if times is not None:
                await asyncio.sleep(random.uniform(0.7, 1.8))

    async def _human_click(self, locator: Any, *, randomize_start: bool = False) -> None:
        """Human-paced click that relies on Camoufox native humanize trajectory."""
        _ = randomize_start  # Backward compatibility for existing callsites.
        try:
            target = locator.first
            if not await target.is_visible(timeout=3000):
                return
            await target.scroll_into_view_if_needed(timeout=3000)
            await asyncio.sleep(random.uniform(0.25, 0.8))
            await target.click(timeout=3000)
        except Exception as exc:
            self.logger.warning("Ошибка при human-клике: %s", exc)
            try:
                await locator.first.click(force=True, timeout=2000)
            except Exception:
                pass

    async def _navigate_warmup(self, url: str, action_name: str) -> bool:
        """Navigates for warmup actions and swallows transient navigation failures."""
        try:
            await self.page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=self.DEFAULT_TIMEOUT_MS,
                referer=self.BASE_URL,
            )
            await self.page.wait_for_load_state("domcontentloaded", timeout=15000)
            return True
        except Exception as exc:
            self.logger.warning("Warmup navigation failed (%s): %s", action_name, exc)
            return False

    async def _warmup_scroll_feed(self) -> None:
        """Scrolls Facebook feed to emulate passive browsing behavior."""
        try:
            if not await self._navigate_warmup(self.BASE_URL, "_warmup_scroll_feed"):
                return
            feed = self.page.locator('[role="feed"], div[data-pagelet="FeedUnit_0"]').first
            await feed.wait_for(state="visible", timeout=10_000)
            await self._human_scroll(times=random.randint(3, 7))
            await asyncio.sleep(random.uniform(2, 5))
        except Exception as exc:
            self.logger.warning("Warmup action _warmup_scroll_feed failed: %s", exc)

    async def _warmup_like_random_post(self) -> None:
        """Likes a random unliked post in feed with low frequency."""
        try:
            if not await self._navigate_warmup(self.BASE_URL, "_warmup_like_random_post"):
                return
            await self._human_scroll(times=2)
            like_buttons = await self.page.locator(
                '[aria-label="Like"][aria-pressed="false"],'
                '[aria-label="Beğen"][aria-pressed="false"],'
                '[aria-label="Нравится"][aria-pressed="false"]'
            ).all()
            if not like_buttons:
                return
            candidate = random.choice(like_buttons[:5])
            await self._human_click(candidate)
            await asyncio.sleep(random.uniform(2, 4))
        except Exception as exc:
            self.logger.warning("Warmup action _warmup_like_random_post failed: %s", exc)

    async def _warmup_open_comments(self) -> None:
        """Opens a random comment thread, scrolls briefly, then closes it."""
        try:
            if not await self._navigate_warmup(self.BASE_URL, "_warmup_open_comments"):
                return
            await self._human_scroll(times=random.randint(1, 3))
            comment_buttons = await self.page.locator(
                '[aria-label="Comment"],[aria-label="Yorum Yap"],[aria-label="Комментировать"]'
            ).all()
            if not comment_buttons:
                return
            candidate = random.choice(comment_buttons[:3])
            await self._human_click(candidate)
            await asyncio.sleep(random.uniform(3, 7))
            await self._human_scroll(times=random.randint(1, 2))
            await self.page.keyboard.press("Escape")
        except Exception as exc:
            self.logger.warning("Warmup action _warmup_open_comments failed: %s", exc)

    async def _warmup_visit_profile(self) -> None:
        """Visits own profile and performs lightweight scrolling."""
        try:
            if not await self._navigate_warmup(
                "https://www.facebook.com/me", "_warmup_visit_profile"
            ):
                return
            profile = self.page.locator(
                'div[data-pagelet="ProfileTimeline"],[data-pagelet="ProfileCover"],main'
            ).first
            await profile.wait_for(state="visible", timeout=10_000)
            await self._human_scroll(times=random.randint(2, 4))
            await asyncio.sleep(random.uniform(2, 4))
        except Exception as exc:
            self.logger.warning("Warmup action _warmup_visit_profile failed: %s", exc)

    async def _warmup_watch_reels(self) -> None:
        """Opens reels and simulates watching two consecutive videos."""
        try:
            if not await self._navigate_warmup(
                "https://www.facebook.com/reels/", "_warmup_watch_reels"
            ):
                return
            video = self.page.locator("video").first
            await video.wait_for(state="visible", timeout=10_000)
            await asyncio.sleep(random.uniform(10, 25))
            await self._human_scroll(times=1)
            await asyncio.sleep(random.uniform(5, 12))
        except Exception as exc:
            self.logger.warning("Warmup action _warmup_watch_reels failed: %s", exc)

    async def warmup(self, duration_seconds: int = 360) -> bool:
        """Runs weighted warmup actions for the configured session duration."""
        actions = [
            self._warmup_scroll_feed,
            self._warmup_scroll_feed,
            self._warmup_scroll_feed,
            self._warmup_watch_reels,
            self._warmup_watch_reels,
            self._warmup_like_random_post,
            self._warmup_like_random_post,
            self._warmup_open_comments,
            self._warmup_visit_profile,
        ]
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(duration_seconds, 1)
        while loop.time() < deadline:
            action = random.choice(actions)
            try:
                await action()
            except Exception as exc:
                self.logger.warning("Warmup action %s failed: %s", action.__name__, exc)
            await asyncio.sleep(random.uniform(8, 20))
        return True

    async def _pre_action_warmup(self) -> None:
        await self._log("Прогрев сессии на главной...")
        try:
            await self.page.goto(
                self.BASE_URL,
                wait_until="domcontentloaded",
                timeout=self.DEFAULT_TIMEOUT_MS,
            )
            await self._raise_if_checkpoint("warmup")
            await asyncio.sleep(random.uniform(6.0, 12.0))
            await self._close_dialogs()
            # Человеческий скролл
            for _ in range(random.randint(2, 4)):
                await self._human_scroll(random.randint(200, 500))
                await asyncio.sleep(random.uniform(1.0, 3.0))
        except AccountCaptchaError:
            raise
        except Exception:
            pass

    async def _post_action_simulation(self) -> None:
        await self._log("Действие выполнено. Читаю ленту...")
        try:
            await asyncio.sleep(random.uniform(4.0, 8.0))
            await self._human_scroll(random.randint(300, 600))
            await asyncio.sleep(random.uniform(10.0, 20.0))
        except Exception:
            pass

    async def login(self) -> None:
        if await self._is_authorized():
            await self._log("Авторизация успешна (storage_state/cookies активны).")
            return

        if self.account.cookies and not self.account.storage_state:
            await self._log("Имеются только куки, пробую авторизоваться через них...")
            if not self._context:
                raise RuntimeError("Browser context is not initialized")
            cookies_payload = cast(list[dict[str, Any]], self.account.cookies)
            await self._context.add_cookies(cast(Any, cookies_payload))
            if await self._is_authorized():
                await self._log("Авторизация по кукам успешна.")
                return
            await self._context.clear_cookies()

        await self._log("Сессия не найдена, вхожу по логину...")
        await self.page.goto(self.BASE_URL, wait_until="domcontentloaded")
        await asyncio.sleep(random.uniform(4, 8))

        # Человеческий ввод логина
        email_field = self.page.locator('input[name="email"]')
        await email_field.click()
        await self._human_type(self.account.login)

        await asyncio.sleep(random.uniform(1.5, 3.5))

        # Человеческий ввод пароля
        pass_field = self.page.locator('input[name="pass"]')
        await pass_field.click()
        await self._human_type(self.account.password)

        await asyncio.sleep(random.uniform(1.0, 2.5))
        await self.page.keyboard.press("Enter")

        await asyncio.sleep(random.uniform(10, 20))
        await self._raise_if_checkpoint("login")

        # Проверка на капчу/ошибку
        if await self.page.locator('form[action*="login"]').count() > 0:
            await self._log("ВНИМАНИЕ: Facebook требует проверку (капча/пароль).")
            raise AccountCaptchaError("Checkpoint detected during login.")

        if not await self._is_authorized():
            raise AccountCaptchaError(
                "Не удалось авторизоваться после ввода логина/пароля."
            )

        await self._log("Успешный вход по логину и паролю.")

    async def _close_dialogs(self) -> None:
        try:
            selectors = [
                'div[role="dialog"] div[aria-label*="Close"]',
                'button:has-text("Not Now")',
                'button:has-text("Şimdi")',
                'div[aria-label*="Kapat"]',
            ]
            for sel in selectors:
                loc = self.page.locator(sel).first
                if await loc.is_visible(timeout=500):
                    await loc.click(force=True)
        except Exception:
            pass

    async def _wait_after_action(self, seconds: float = 20.0) -> None:
        await asyncio.sleep(seconds)

    async def _dismiss_action_blockers(self) -> None:
        """Закрывает только блокирующие попапы, не трогая модалку поста."""
        selectors = [
            'button:has-text("Not Now")',
            'button:has-text("Şimdi değil")',
            'button:has-text("Şimdi Değil")',
            'button:has-text("Şimdi")',
        ]
        for sel in selectors:
            try:
                loc = self.page.locator(sel).first
                if await loc.is_visible(timeout=700):
                    await loc.click(force=True)
                    await asyncio.sleep(0.3)
            except Exception:
                continue

    async def leave_comment(self, target_url: str, text: str) -> bool:
        await self._pre_action_warmup()
        await self._log(f"Переход к цели: {target_url}")
        try:
            await self.page.goto(
                target_url,
                wait_until="domcontentloaded",
                timeout=self.LOGIN_TIMEOUT_MS,
                referer=self.BASE_URL,
            )
            await self._log("Ожидаю догрузку страницы поста...")
            wait_for_load_state = getattr(self.page, "wait_for_load_state", None)
            if callable(wait_for_load_state):
                try:
                    await wait_for_load_state("networkidle", timeout=45_000)
                except Exception:
                    pass
            await asyncio.sleep(random.uniform(18, 28))
            await self._dismiss_action_blockers()
            await self._raise_if_checkpoint("target navigation")
        except AccountCaptchaError:
            raise
        except Exception:
            return False

        input_selectors = [
            'div[contenteditable="true"][role="textbox"][data-lexical-editor="true"]',
            'div[data-lexical-editor="true"][role="textbox"]',
            'div[role="textbox"][contenteditable="true"]',
            'div[contenteditable="true"][role="textbox"]',
            "form textarea",
        ]

        async def _find_comment_input() -> Any | None:
            dialog_scoped_selectors = [
                f'div[role="dialog"] {sel}' for sel in input_selectors
            ]
            ordered_selectors = [*dialog_scoped_selectors, *input_selectors]
            for sel in ordered_selectors:
                try:
                    loc = self.page.locator(sel).first
                    if await loc.count() > 0:
                        await self._log(f"Поле найдено по селектору: {sel}")
                        return loc
                except Exception:
                    continue

            for frame in self.page.frames:
                if frame == self.page.main_frame:
                    continue
                for sel in input_selectors:
                    try:
                        loc = frame.locator(sel).first
                        if await loc.count() > 0:
                            await self._log("Поле комментария найдено внутри iframe.")
                            return loc
                    except Exception:
                        continue
            return None

        input_field = None
        for attempt in range(1, 4):
            input_field = await _find_comment_input()
            if input_field:
                break
            await self._log(
                f"Поле ещё не появилось, жду догрузку ({attempt}/3)..."
            )
            wait_for_load_state = getattr(self.page, "wait_for_load_state", None)
            if callable(wait_for_load_state):
                try:
                    await wait_for_load_state("networkidle", timeout=20_000)
                except Exception:
                    pass
            await asyncio.sleep(random.uniform(3.0, 6.0))
        if not input_field:
            try:
                post_dialog = self.page.locator('div[role="dialog"]').first
                if await post_dialog.count() > 0 and await post_dialog.is_visible(
                    timeout=1200
                ):
                    await self._log("Прокручиваю модалку поста до зоны комментариев...")
                    await post_dialog.evaluate("""
                        el => {
                            const scrollers = [el, ...el.querySelectorAll('div')]
                                .filter(node => node && node.scrollHeight > node.clientHeight);
                            for (const node of scrollers) {
                                node.scrollTop = node.scrollHeight;
                            }
                        }
                        """)
                    await asyncio.sleep(1.0)
            except Exception:
                pass
            input_field = await _find_comment_input()

        if not input_field:
            await self._log("Поле скрыто, ищу кнопку активации...")
            trig = (
                self.page.locator('div[role="button"], span, div')
                .filter(
                    has_text=re.compile(
                        r"Yorum yaz|Yorum yap|Write a comment|Comment|Напишите",
                        re.IGNORECASE,
                    )
                )
                .first
            )
            try:
                if await trig.is_visible(timeout=3000):
                    await self._log("Активирую поле ввода кликом...")
                    await self._human_click(trig)
                    await asyncio.sleep(random.uniform(2.0, 4.0))
            except Exception:
                pass
            input_field = await _find_comment_input()

        if not input_field:
            await self._log("Ошибка: Поле ввода не найдено.")
            return False

        await self._log("Поле найдено. Активирую...")
        try:
            await input_field.scroll_into_view_if_needed()
            await input_field.wait_for(state="attached", timeout=5000)
            await input_field.wait_for(state="visible", timeout=7000)
            await input_field.evaluate("""
                el => {
                    el.focus();
                    el.dispatchEvent(new Event('focus', { bubbles: true }));
                }
                """)
            await asyncio.sleep(random.uniform(1.0, 2.0))

            box = await input_field.bounding_box()
            if box:
                await self.page.mouse.click(box["x"] + 5, box["y"] + 5)

            await asyncio.sleep(1.0)
            await self.page.keyboard.press("Control+A")
            await self.page.keyboard.press("Backspace")

            await self._log("Начинаю печать текста...")
            await self._human_type(text)
            await asyncio.sleep(random.uniform(1.0, 2.0))

            await self.page.keyboard.press("Enter")
            await asyncio.sleep(1.0)

            try:
                still_has_text = bool(
                    await input_field.evaluate(
                        "el => (el.innerText || '').trim().length > 0"
                    )
                )
            except Exception:
                still_has_text = False

            if still_has_text:
                await self._log(
                    "Enter не отправил комментарий, пробую Control+Enter..."
                )
                await self.page.keyboard.press("Control+Enter")

            await self._post_action_simulation()
            return True
        except Exception as e:
            await self._log(f"Сбой при печати: {str(e)}")
            return False

    async def like_comment(self, target_url: str) -> bool:
        await self._pre_action_warmup()
        await self._log(f"Переход к цели: {target_url}")
        try:
            await self.page.goto(
                target_url, wait_until="domcontentloaded", referer=self.BASE_URL
            )
            await asyncio.sleep(random.uniform(12, 20))
            await self._dismiss_action_blockers()
            await self._raise_if_checkpoint("target navigation")
        except AccountCaptchaError:
            raise
        except Exception:
            return False

        cid = re.search(r"comment_id=(\d+)", target_url)
        cid = cid.group(1) if cid else None

        async def _is_already_liked(button: Any) -> bool:
            try:
                label = (await button.get_attribute("aria-label") or "").lower()
            except Exception:
                label = ""
            try:
                text = (await button.inner_text() or "").lower()
            except Exception:
                text = ""
            try:
                pressed = (await button.get_attribute("aria-pressed") or "").lower()
            except Exception:
                pressed = ""
            return (
                "vazgeç" in label
                or "unlike" in label
                or "vazgeç" in text
                or "unlike" in text
                or pressed == "true"
            )

        async def _find_like_button() -> Any | None:
            # 1) Пытаемся найти кнопку лайка внутри контейнера целевого комментария.
            if cid:
                try:
                    comment_container = (
                        self.page.locator('div[role="article"]')
                        .filter(has=self.page.locator(f'a[href*="{cid}"]'))
                        .filter(visible=True)
                        .first
                    )
                    if await comment_container.is_visible(timeout=2500):
                        by_text = (
                            comment_container.locator('[role="button"], a')
                            .filter(has_text=self.PATTERNS["LIKE"])
                            .first
                        )
                        if await by_text.is_visible(timeout=1500):
                            return by_text
                except Exception:
                    pass

            # 2) Диалог поста (приоритетно) и глобальные fallback-селекторы.
            like_selectors = [
                'div[role="dialog"] [data-ad-rendering-role="like_button"]',
                '[data-ad-rendering-role="like_button"]',
                'div[role="dialog"] [role="button"]:has-text("Beğen")',
                '[role="button"]:has-text("Beğen")',
                'div[role="dialog"] [role="button"]:has-text("Like")',
                '[role="button"]:has-text("Like")',
                'div[role="dialog"] [role="button"]:has-text("Нравится")',
                '[role="button"]:has-text("Нравится")',
            ]
            for selector in like_selectors:
                try:
                    candidate = self.page.locator(selector).first
                    if await candidate.is_visible(timeout=2000):
                        await self._log(
                            f"Кнопка лайка найдена по селектору: {selector}"
                        )
                        return candidate
                except Exception:
                    continue

            # 3) iframe fallback.
            for frame in self.page.frames:
                if frame == self.page.main_frame:
                    continue
                for selector in like_selectors:
                    try:
                        candidate = frame.locator(selector).first
                        if await candidate.is_visible(timeout=1500):
                            await self._log("Кнопка лайка найдена внутри iframe.")
                            return candidate
                    except Exception:
                        continue
            return None

        btn = await _find_like_button()
        if btn and await _is_already_liked(btn):
            await self._log("Пост уже лайкнут.")
            return True

        if btn:
            await self._log("Нажимаю Лайк (Deep Stealth)...")
            await self._human_click(btn, randomize_start=True)
            await self._post_action_simulation()
            return True
        return False

    async def reply_comment(self, target_url: str, text: str) -> bool:
        return await self.leave_comment(target_url, text)

    async def _is_authorized(self) -> bool:
        try:
            # Не переходим на главную если мы уже там
            if self.page.url != self.BASE_URL:
                await self.page.goto(self.BASE_URL, wait_until="domcontentloaded")
            if self.CHECKPOINT_URL_RE.search(self.page.url or ""):
                return False
            return await self._has_c_user_cookie()
        except Exception:
            return False

    async def like_post(self, target_url: str) -> bool:
        await self._pre_action_warmup()
        try:
            await self.page.goto(
                target_url, wait_until="domcontentloaded", referer=self.BASE_URL
            )
            await self._raise_if_checkpoint("target navigation")
            await asyncio.sleep(random.uniform(10, 18))
            btn = (
                self.page.get_by_role("button", name=self.PATTERNS["LIKE"], exact=True)
                .filter(visible=True)
                .first
            )
            if await btn.is_visible(timeout=5000):
                await self._human_click(btn)
                await self._post_action_simulation()
                return True
        except AccountCaptchaError:
            raise
        except Exception:
            pass
        return False

    async def _raise_if_checkpoint(self, stage: str) -> None:
        current_url = self.page.url or ""
        if self.CHECKPOINT_URL_RE.search(current_url):
            await self._log(
                f"CHECKPOINT: Facebook запросил подтверждение личности на этапе '{stage}': {current_url}"
            )
            if await self._wait_for_checkpoint_resolution():
                await self._log(
                    "CHECKPOINT: подтверждение пройдено вручную, продолжаю выполнение."
                )
                return
            raise AccountCaptchaError(
                f"Checkpoint detected during {stage}: {current_url}"
            )

    async def _has_c_user_cookie(self) -> bool:
        if not self._context:
            return False
        cookies = await self._context.cookies()
        return any(cookie.get("name") == "c_user" for cookie in cookies)

    async def _wait_for_checkpoint_resolution(self) -> bool:
        await self._log(
            "Обнаружен CHECKPOINT. Пробуем автоматическое подтверждение по почте..."
        )

        try:
            # 1. Проверяем, есть ли опция отправки кода на почту
            send_code_btn = self.page.get_by_text(
                re.compile(r"Отправить код по|Send code to|Kodu gönder", re.IGNORECASE)
            )
            if await send_code_btn.count() > 0:
                await self._log("Выбираем отправку кода на email")
                await self._human_click(send_code_btn.first)

                next_btn = self.page.get_by_text(
                    re.compile(r"Далее|Next|Devam", re.IGNORECASE)
                )
                if await next_btn.count() > 0:
                    await self._human_click(next_btn.first)
                    await self.page.wait_for_load_state("networkidle")

            # 2. Если мы на странице ввода 6/8-значного кода и у нас есть почта
            code_input = self.page.locator(
                "input[name='captcha_response'], input[name='code']"
            )
            if (
                await code_input.count() > 0
                and self.account.email_login
                and self.account.email_password
            ):
                await self._log(
                    f"Ждем письмо от Facebook на {self.account.email_login}..."
                )

                # Запускаем поиск письма
                code = await get_facebook_code(
                    email_login=self.account.email_login,
                    email_password=self.account.email_password,
                    imap_server=self.account.imap_server,
                    timeout_sec=120,
                    poll_interval_sec=10,
                )

                if code:
                    await self._log(f"Получен код подтверждения: {code}")
                    await code_input.first.focus()
                    await self._human_type(code)

                    submit_btn = self.page.get_by_text(
                        re.compile(
                            r"Отправить|Submit|Gönder|Продолжить|Continue|Devam",
                            re.IGNORECASE,
                        )
                    )
                    if await submit_btn.count() > 0:
                        await self._human_click(submit_btn.first)
                        await self.page.wait_for_load_state("networkidle")

                        # Даем время на редирект
                        await asyncio.sleep(5)

                        if not self.CHECKPOINT_URL_RE.search(self.page.url):
                            await self._log("Чекпоинт успешно пройден!")
                            return True
                else:
                    await self._log("Код не пришел на почту.")
            elif await code_input.count() > 0:
                await self._log(
                    f"Требуется код подтверждения, но данные от почты не указаны: {self.account.login}"
                )
        except Exception as e:
            await self._log(
                f"Ошибка при попытке автоматического прохождения чекпоинта: {e}"
            )

        # Fallback to manual resolution if auto fails or is not available
        keep_open = self._parse_bool(
            os.getenv("FB_KEEP_BROWSER_ON_CHECKPOINT"), default=True
        )
        if not keep_open:
            return False

        wait_seconds = int(os.getenv("FB_CHECKPOINT_WAIT_SECONDS", "600"))
        poll_seconds = max(1, int(os.getenv("FB_CHECKPOINT_POLL_SECONDS", "5")))
        attempts = max(1, wait_seconds // poll_seconds)

        await self._log(
            f"CHECKPOINT: автоматическое прохождение не удалось. Браузер оставлен открытым на {wait_seconds} сек для ручного ввода кода."
        )
        for _ in range(attempts):
            await asyncio.sleep(poll_seconds)
            current_url = self.page.url or ""
            if self.CHECKPOINT_URL_RE.search(current_url):
                continue
            if await self._has_c_user_cookie():
                return True

        await self._log(
            "CHECKPOINT: время ожидания вышло, аккаунт будет помечен как CHECKPOINT."
        )
        return False

        wait_seconds = int(os.getenv("FB_CHECKPOINT_WAIT_SECONDS", "600"))
        poll_seconds = max(1, int(os.getenv("FB_CHECKPOINT_POLL_SECONDS", "5")))
        attempts = max(1, wait_seconds // poll_seconds)

        await self._log(
            f"CHECKPOINT: браузер оставлен открытым на {wait_seconds} сек для ручного ввода кода."
        )
        for _ in range(attempts):
            await asyncio.sleep(poll_seconds)
            current_url = self.page.url or ""
            if self.CHECKPOINT_URL_RE.search(current_url):
                continue
            if await self._has_c_user_cookie():
                return True

        await self._log(
            "CHECKPOINT: время ожидания вышло, аккаунт будет помечен как CHECKPOINT."
        )
        return False

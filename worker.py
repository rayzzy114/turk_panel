from __future__ import annotations

import asyncio
import logging
import os
import random
import re
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from dataclasses import dataclass
from typing import Any, Final, cast

from models import CheckpointType
from import_data import DEFAULT_PASSWORD_PLACEHOLDER, normalize_cookies
from imap_utils import get_facebook_code
from iproxy_utils import get_current_ip, rotate_mobile_ip
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

    def to_proxy_url(self) -> str:
        """Builds a proxy URL string for outbound HTTP client usage."""
        if self.user and self.password:
            return f"http://{self.user}:{self.password}@{self.host}:{self.port}"
        return f"http://{self.host}:{self.port}"


@dataclass(slots=True)
class AccountSessionData:
    login: str
    password: str
    user_agent: str
    account_id: int | None = None
    cookies: CookieList | None = None
    storage_state: dict[str, Any] | None = None
    proxy: ProxyConfig | None = None
    email_login: str | None = None
    email_password: str | None = None
    imap_server: str | None = None
    proxy_type: str | None = None
    proxy_rotation_url: str | None = None


class AccountCaptchaError(RuntimeError):
    """Raised when Facebook blocks the session with a login wall/captcha."""


class AccountCheckpointError(AccountCaptchaError):
    """Raised when checkpoint flow is detected and not resolved automatically."""

    def __init__(
        self,
        message: str,
        *,
        checkpoint_type: CheckpointType,
        screenshot_path: str | None = None,
    ) -> None:
        super().__init__(message)
        self.checkpoint_type = checkpoint_type
        self.screenshot_path = screenshot_path


class AccountBannedError(RuntimeError):
    """Raised when Facebook indicates that the account is disabled."""


class AccountInvalidCredentialsError(RuntimeError):
    """Raised when Facebook explicitly reports invalid login credentials."""


class AccountCookieInvalidError(RuntimeError):
    """Raised when cookie-only session data cannot restore authorization."""


class FacebookBrowser:
    BASE_URL: Final[str] = "https://www.facebook.com/"
    DEFAULT_TIMEOUT_MS: Final[int] = 60_000
    LOGIN_TIMEOUT_MS: Final[int] = 120_000

    PATTERNS = {
        "LIKE": re.compile(r"\b(Нравится|Like|Beğen)\b", re.IGNORECASE),
    }
    CHECKPOINT_URL_RE: Final[re.Pattern[str]] = re.compile(
        r"/(?:checkpoint|two_step_verification(?:/authentication)?|auth_platform/codesubmit)/?",
        re.IGNORECASE,
    )
    WARMUP_ACTION_LABELS: Final[dict[str, str]] = {
        "_warmup_scroll_feed": "Прокрутка ленты",
        "_warmup_watch_reels": "Просмотр Reels",
        "_warmup_like_random_post": "Лайк случайного поста",
        "_warmup_open_comments": "Открытие комментариев",
        "_warmup_visit_profile": "Переход в профиль",
    }

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
        self._last_checkpoint_type: CheckpointType | None = None
        self._mobile_login_retry_used = False

    async def _log(self, message: str) -> None:
        self.logger.info(message)
        if self.log_callback:
            await self.log_callback(message)

    @staticmethod
    def _parse_bool(value: str | None, *, default: bool = False) -> bool:
        if value is None:
            return default
        return value.strip().lower() in {"1", "true", "yes", "on"}

    @property
    def last_checkpoint_type(self) -> CheckpointType | None:
        """Returns the most recent checkpoint type seen in this browser session."""
        return self._last_checkpoint_type

    def _is_mobile_proxy(self) -> bool:
        return (self.account.proxy_type or "").strip().lower() == "mobile"

    async def _rotate_mobile_proxy_if_needed(self, *, reason: str) -> None:
        """Rotates mobile proxy and logs resulting external IP when available."""
        if not self._is_mobile_proxy() or not self.account.proxy:
            return

        rotated = await rotate_mobile_ip(self.account.proxy_rotation_url or "")
        if not rotated:
            self.logger.warning(
                "Mobile proxy rotation failed for %s (%s).",
                self.account.login,
                reason,
            )
            return

        self.logger.info(
            "Mobile proxy rotated for %s (%s). Waiting for stabilization...",
            self.account.login,
            reason,
        )
        await asyncio.sleep(2)
        current_ip = await get_current_ip(self.account.proxy.to_proxy_url())
        if current_ip:
            self.logger.info(
                "Mobile proxy current IP for %s: %s",
                self.account.login,
                current_ip,
            )

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

            await self._rotate_mobile_proxy_if_needed(reason="before browser start")

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

    async def _type_and_verify(self, locator: Any, text: str) -> None:
        """Types into an input and verifies the DOM value, falling back to fill() on mismatch."""
        target = locator.first
        await target.click()
        try:
            await target.fill("")
        except Exception:
            pass
        await self._human_type(text)
        try:
            current_value = await target.evaluate("el => el.value || ''")
        except Exception:
            return
        if current_value == text:
            return
        self.logger.warning(
            "Input mismatch for %s: expected %r, got %r. Falling back to fill().",
            self.account.login,
            text,
            current_value,
        )
        await target.fill(text)

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

    async def warmup(self, duration_seconds: int = 360) -> dict[str, Any]:
        """
        Runs weighted warmup actions and returns detailed execution metrics.

        Returns:
            {
                "result": "completed" | "error",
                "actions_attempted": int,
                "actions_succeeded": int,
                "actions_failed": int,
                "action_log": list[dict[str, Any]],
                "error_message": str | None,
                "duration_seconds": int,
            }
        """
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

        result: dict[str, Any] = {
            "result": "unknown",
            "actions_attempted": 0,
            "actions_succeeded": 0,
            "actions_failed": 0,
            "action_log": [],
            "error_message": None,
            "duration_seconds": 0,
        }

        started_at = perf_counter()
        try:
            await self._log(
                f"Запускаю прогрев сессии на {max(duration_seconds, 1)} сек."
            )
            is_alive = await self._check_session_alive()
            if not is_alive:
                await self.login()

            loop = asyncio.get_running_loop()
            deadline = loop.time() + max(duration_seconds, 1)
            while loop.time() < deadline:
                if not await self._check_session_alive():
                    raise AccountCaptchaError(
                        "Warmup session is no longer authorized."
                    )

                action = random.choice(actions)
                action_started = perf_counter()
                action_name = action.__name__
                action_label = self.WARMUP_ACTION_LABELS.get(action_name, action_name)
                result["actions_attempted"] += 1
                action_entry: dict[str, Any] = {
                    "action": action_name,
                    "status": "ok",
                    "duration_ms": 0,
                    "error": None,
                }

                try:
                    await self._log(f"Прогрев: старт действия {action_label}.")
                    await action()
                    result["actions_succeeded"] += 1
                except Exception as exc:
                    action_entry["status"] = "failed"
                    action_entry["error"] = str(exc)
                    result["actions_failed"] += 1
                    self.logger.warning("Warmup action %s failed: %s", action_name, exc)
                finally:
                    action_entry["duration_ms"] = int(
                        max(0, (perf_counter() - action_started) * 1000)
                    )
                    cast(list[dict[str, Any]], result["action_log"]).append(action_entry)
                    if action_entry["status"] == "ok":
                        await self._log(
                            "Прогрев: "
                            f"{action_label} успешно завершено за {action_entry['duration_ms']} мс."
                        )
                    else:
                        await self._log(
                            "Прогрев: "
                            f"{action_label} завершилось ошибкой за {action_entry['duration_ms']} мс: "
                            f"{action_entry['error']}"
                        )

                await asyncio.sleep(random.uniform(8, 20))

            result["result"] = "completed"
            await self._log("Прогрев завершен успешно.")
        except Exception as exc:
            result["result"] = "error"
            result["error_message"] = str(exc)
            self.logger.warning("Warmup flow failed for %s: %s", self.account.login, exc)
            await self._log(f"Прогрев завершился ошибкой: {exc}")
        finally:
            result["duration_seconds"] = int(max(1, perf_counter() - started_at))

        return result

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

    async def _login_once(self) -> None:
        """Performs a single login attempt using session state or credentials."""
        if await self._is_authorized():
            await self._log("Авторизация успешна (storage_state/cookies активны).")
            return

        if self.account.storage_state:
            await self._log("Пробую восстановить сессию из storage_state...")
            if not self._context:
                raise RuntimeError("Browser context is not initialized")
            storage_cookies = normalize_cookies(
                cast(list[dict[str, Any]], self.account.storage_state.get("cookies", []))
            )
            if storage_cookies:
                await self._context.add_cookies(cast(Any, storage_cookies))
                if await self._is_authorized():
                    await self._log("Авторизация через storage_state успешна.")
                    return
                await self._context.clear_cookies()

        if self.account.cookies:
            await self._log("Имеются только куки, пробую авторизоваться через них...")
            if not self._context:
                raise RuntimeError("Browser context is not initialized")
            cookies_payload = normalize_cookies(cast(list[dict[str, Any]], self.account.cookies))
            if not cookies_payload:
                self.logger.warning(
                    "No valid Facebook cookies found after normalization for account %s",
                    self.account.account_id or self.account.login,
                )
            else:
                await self._context.add_cookies(cast(Any, cookies_payload))
                if await self._is_authorized():
                    await self._log("Авторизация по кукам успешна.")
                    return
                await self._context.clear_cookies()

        if self.account.password == DEFAULT_PASSWORD_PLACEHOLDER:
            await self._log("Куки не восстановили сессию. Re-import cookies from Dolphin.")
            raise AccountCookieInvalidError("Re-import cookies from Dolphin")

        await self._log("Сессия не найдена, вхожу по логину...")
        await self.page.goto(self.BASE_URL, wait_until="domcontentloaded")
        await asyncio.sleep(random.uniform(4, 8))

        if await self._handle_saved_profile_login():
            return

        # Человеческий ввод логина
        email_field = await self._find_login_identifier_field()
        await self._type_and_verify(email_field, self.account.login)

        await asyncio.sleep(random.uniform(1.5, 3.5))

        # Человеческий ввод пароля
        pass_field = self.page.locator('input[name="pass"]')
        await self._type_and_verify(pass_field, self.account.password)

        await asyncio.sleep(random.uniform(1.0, 2.5))
        await self.page.keyboard.press("Enter")
        await self._wait_for_login_outcome(
            stage="login",
            failure_message="Не удалось авторизоваться после ввода логина/пароля.",
        )
        await self._log("Успешный вход по логину и паролю.")

    async def _find_login_identifier_field(self) -> Any:
        """Finds the standard Facebook login/email/phone input on the normal login page."""
        selectors = [
            'input[name="email"]',
            'input[autocomplete="username"]',
            'input[placeholder*="E-posta"]',
            'input[aria-label*="E-posta"]',
            'input[placeholder*="email"]',
            'input[aria-label*="email"]',
            'input[placeholder*="phone"]',
            'input[aria-label*="phone"]',
        ]
        for selector in selectors:
            try:
                field = self.page.locator(selector).first
                if await field.count() > 0 and await field.is_visible(timeout=800):
                    return field
            except Exception:
                continue
        return self.page.locator('input[name="email"]').first

    async def _handle_saved_profile_login(self) -> bool:
        """Handles Facebook saved-profile login walls that ask only for the password."""
        async def _find_named_action(pattern: re.Pattern[str]) -> Any | None:
            for role in ("button", "link"):
                try:
                    locator = self.page.get_by_role(role, name=pattern).first
                    if await locator.count() > 0 and await locator.is_visible(timeout=1500):
                        return locator
                except Exception:
                    continue
            return None

        try:
            email_field = await self._find_login_identifier_field()
            if await email_field.count() > 0 and await email_field.is_visible(timeout=800):
                return False
        except Exception:
            pass

        has_profile_avatar = False
        avatar_selectors = [
            'img[alt][src*="scontent"]',
            'div[role="dialog"] img',
            'img[width="80"]',
            'img[height="80"]',
        ]
        for selector in avatar_selectors:
            try:
                avatar = self.page.locator(selector).first
                if await avatar.count() > 0 and await avatar.is_visible(timeout=800):
                    has_profile_avatar = True
                    break
            except Exception:
                continue

        try:
            body_text = (await self.page.inner_text("body") or "").lower()
        except Exception:
            try:
                body_text = (await self.page.content() or "").lower()
            except Exception:
                body_text = ""

        saved_profile_markers = [
            "başka bir profil kullan",
            "baska bir profil kullan",
            "use another profile",
            "log into another account",
        ]
        has_saved_profile_marker = any(
            marker in body_text for marker in saved_profile_markers
        )

        password_field = self.page.locator(
            "input[type='password'], input[name='pass']"
        ).first
        if not (has_profile_avatar or has_saved_profile_marker):
            return False
        if await password_field.count() == 0:
            continue_button = await _find_named_action(
                re.compile(r"Devam|Continue|Log In|Giriş Yap", re.IGNORECASE)
            )
            if continue_button is not None:
                await self._log(
                    "Обнаружен экран сохраненного профиля. Открываю форму пароля..."
                )
                await self._human_click(continue_button)
                await asyncio.sleep(random.uniform(2, 4))
            password_field = self.page.locator(
                "input[type='password'], input[name='pass']"
            ).first

        if await password_field.count() == 0 or not await password_field.is_visible(
            timeout=1500
        ):
            return False

        await self._log("Facebook запрашивает только пароль сохраненного профиля.")
        await self._type_and_verify(password_field, self.account.password)
        await asyncio.sleep(random.uniform(1.0, 2.5))

        submit_button = await _find_named_action(
            re.compile(r"Giriş Yap|Log In|Continue|Devam", re.IGNORECASE)
        )
        if submit_button is not None:
            await self._human_click(submit_button)
        else:
            await self.page.keyboard.press("Enter")

        await self._wait_for_login_outcome(
            stage="saved profile login",
            failure_message=(
                "Не удалось авторизоваться после ввода пароля сохраненного профиля."
            ),
        )
        await self._log("Успешный вход через сохраненный профиль.")
        return True

    async def _wait_for_login_outcome(
        self,
        *,
        stage: str,
        failure_message: str,
        timeout_seconds: int = 45,
        poll_seconds: int = 3,
    ) -> None:
        """Waits for the login submit to settle into success, checkpoint, or explicit failure."""
        deadline = perf_counter() + timeout_seconds
        while perf_counter() < deadline:
            await self._raise_if_checkpoint(stage)
            await self._raise_if_invalid_credentials(stage)
            if await self._is_authorized():
                return
            await asyncio.sleep(poll_seconds)

        await self._raise_if_checkpoint(stage)
        await self._raise_if_invalid_credentials(stage)
        raise AccountCaptchaError(failure_message)

    async def _raise_if_invalid_credentials(self, stage: str) -> None:
        """Raises when Facebook explicitly reports wrong password/invalid credentials."""
        try:
            body_text = (await self.page.inner_text("body") or "").lower()
        except Exception:
            try:
                body_text = (await self.page.content() or "").lower()
            except Exception:
                return

        invalid_patterns = [
            "girdiğin şifre yanlış",
            "girdigin sifre yanlis",
            "incorrect password",
            "wrong password",
            "the password that you've entered is incorrect",
            "the password you entered is incorrect",
            "yanlış şifre",
            "yanlis sifre",
        ]
        if any(pattern in body_text for pattern in invalid_patterns):
            raise AccountInvalidCredentialsError(
                f"Facebook rejected login credentials during {stage}."
            )

    async def login(self) -> None:
        """Logs in and retries once with IP rotation for mobile proxies on checkpoint/captcha."""
        try:
            await self._login_once()
            return
        except (AccountCheckpointError, AccountCaptchaError):
            if not self._is_mobile_proxy() or self._mobile_login_retry_used:
                raise

            self._mobile_login_retry_used = True
            self.logger.warning(
                "Login failed for %s under mobile proxy. Rotating IP and retrying once.",
                self.account.login,
            )
            await self._rotate_mobile_proxy_if_needed(reason="login retry after captcha")
            if self._context:
                try:
                    await self._context.clear_cookies()
                except Exception:
                    pass
            await self._login_once()

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
        if (
            not await self._check_session_alive()
            and not self.CHECKPOINT_URL_RE.search(self.page.url or "")
        ):
            await self.login()
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
        if (
            not await self._check_session_alive()
            and not self.CHECKPOINT_URL_RE.search(self.page.url or "")
        ):
            await self.login()
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

    async def _has_login_wall(self) -> bool:
        """Detects explicit Facebook login walls even if stale cookies still exist."""
        try:
            if await self.page.locator('form[action*="login"]').count() > 0:
                return True
        except Exception:
            pass

        try:
            body_text = (await self.page.inner_text("body") or "").lower()
        except Exception:
            try:
                body_text = (await self.page.content() or "").lower()
            except Exception:
                body_text = ""

        login_wall_markers = [
            "başka bir profil kullan",
            "baska bir profil kullan",
            "yeni hesap oluştur",
            "yeni hesap olustur",
            "şifreni mi unuttun",
            "sifreni mi unuttun",
            "forgotten password",
            "use another profile",
            "log into another account",
            "create new account",
        ]
        return any(marker in body_text for marker in login_wall_markers)

    async def _is_authorized(self) -> bool:
        try:
            current_url = self.page.url or ""
            if self.CHECKPOINT_URL_RE.search(current_url):
                return False
            # Не переходим на главную если мы уже там
            if current_url != self.BASE_URL:
                await self.page.goto(self.BASE_URL, wait_until="domcontentloaded")
            if self.CHECKPOINT_URL_RE.search(self.page.url or ""):
                return False
            if await self._has_login_wall():
                return False
            return await self._has_c_user_cookie()
        except Exception:
            return False

    async def _check_session_alive(self) -> bool:
        """
        Checks whether the current browser session is still authorized.

        If session appears dead, it attempts to restore cookies from storage_state.
        Returns True if session is alive or restored, otherwise False.
        """
        try:
            await self.page.goto(
                self.BASE_URL,
                wait_until="domcontentloaded",
                timeout=self.DEFAULT_TIMEOUT_MS,
            )
            if self.CHECKPOINT_URL_RE.search(self.page.url or ""):
                return False

            if await self._has_login_wall():
                return False

            if await self._has_c_user_cookie():
                return True
        except Exception as exc:
            self.logger.warning(
                "Session aliveness check failed for %s: %s",
                self.account.login,
                exc,
            )

        if not self._context or not self.account.storage_state:
            return False

        try:
            cookies = normalize_cookies(
                cast(list[dict[str, Any]], self.account.storage_state.get("cookies", []))
            )
            if cookies:
                await self._context.add_cookies(cast(Any, cookies))
            await self.page.goto(
                self.BASE_URL,
                wait_until="domcontentloaded",
                timeout=self.DEFAULT_TIMEOUT_MS,
            )
            return (
                not self.CHECKPOINT_URL_RE.search(self.page.url or "")
                and not await self._has_login_wall()
                and await self._has_c_user_cookie()
            )
        except Exception as exc:
            self.logger.warning(
                "Session restore from storage_state failed for %s: %s",
                self.account.login,
                exc,
            )
            return False

    async def like_post(self, target_url: str) -> bool:
        await self._pre_action_warmup()
        if (
            not await self._check_session_alive()
            and not self.CHECKPOINT_URL_RE.search(self.page.url or "")
        ):
            await self.login()
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
            try:
                resolved = await self._wait_for_checkpoint_resolution()
            except AccountCheckpointError:
                if self._is_mobile_proxy() and not self._mobile_login_retry_used:
                    self._mobile_login_retry_used = True
                    self.logger.info(
                        "Mobile proxy checkpoint recovery for %s at %s.",
                        self.account.login,
                        stage,
                    )
                    await self._rotate_mobile_proxy_if_needed(
                        reason=f"checkpoint recovery ({stage})"
                    )
                    await self.login()
                    return
                raise

            if resolved:
                await self._log(
                    "CHECKPOINT: подтверждение пройдено вручную, продолжаю выполнение."
                )
                return
            if self._is_mobile_proxy() and not self._mobile_login_retry_used:
                self._mobile_login_retry_used = True
                self.logger.info(
                    "Mobile proxy unresolved checkpoint recovery for %s at %s.",
                    self.account.login,
                    stage,
                )
                await self._rotate_mobile_proxy_if_needed(
                    reason=f"unresolved checkpoint ({stage})"
                )
                await self.login()
                return
            checkpoint_type = self._last_checkpoint_type or CheckpointType.UNKNOWN_CHECKPOINT
            raise AccountCheckpointError(
                f"Checkpoint detected during {stage}: {current_url}",
                checkpoint_type=checkpoint_type,
            )

    async def _has_c_user_cookie(self) -> bool:
        if not self._context:
            return False
        cookies = await self._context.cookies()
        return any(cookie.get("name") == "c_user" for cookie in cookies)

    async def detect_checkpoint_type(self) -> CheckpointType:
        """
        Classifies Facebook checkpoint subtype by visible body text.

        Returns UNKNOWN_CHECKPOINT on any parsing failure.
        """
        current_url = (self.page.url or "").lower()
        if "auth_platform/codesubmit" in current_url:
            return CheckpointType.CODE_VERIFICATION

        try:
            body_text = await self.page.inner_text("body")
        except Exception:
            try:
                body_text = await self.page.content()
            except Exception as exc:
                self.logger.warning(
                    "Failed to read checkpoint page body for %s: %s",
                    self.account.login,
                    exc,
                )
                return CheckpointType.UNKNOWN_CHECKPOINT

        normalized = (body_text or "").lower()
        snippet = normalized[:300]
        self.logger.debug(
            "Checkpoint page snippet for %s: %s",
            self.account.login,
            snippet,
        )

        patterns: list[tuple[CheckpointType, tuple[str, ...]]] = [
            (
                CheckpointType.CODE_VERIFICATION,
                (
                    "güvenlik kodu",
                    "doğrulama kodu",
                    "onay kodu",
                    "e-posta adresini kontrol et",
                    "gönderdiğimiz kodu gir",
                    "kodu gir",
                    "security code",
                    "confirmation code",
                    "check your email address",
                    "enter the code we sent",
                ),
            ),
            (
                CheckpointType.FACE_VERIFICATION,
                (
                    "kimliğini doğrula",
                    "yüzünün fotoğrafı",
                    "selfie",
                    "kimlik belgesi",
                    "confirm your identity",
                    "photo of your face",
                    "identity document",
                ),
            ),
            (
                CheckpointType.SUSPICIOUS_LOGIN,
                (
                    "olağandışı giriş",
                    "başka birinin hesabına",
                    "hesabın sana ait olduğunu onayla",
                    "kilidini açmak için",
                    "giriş detaylarını gözden geçir",
                    "unusual login",
                    "someone else",
                    "confirm this account is yours",
                    "we locked your account",
                    "review the login details",
                ),
            ),
            (
                CheckpointType.ACCOUNT_DISABLED,
                (
                    "hesabın devre dışı",
                    "engellendi",
                    "account disabled",
                    "has been blocked",
                ),
            ),
        ]
        for checkpoint_type, keywords in patterns:
            if any(keyword in normalized for keyword in keywords):
                return checkpoint_type
        return CheckpointType.UNKNOWN_CHECKPOINT

    async def _wait_for_checkpoint_resolution(self) -> bool:
        """Dispatches checkpoint handling strategy based on detected checkpoint type."""
        checkpoint_type = await self.detect_checkpoint_type()
        self._last_checkpoint_type = checkpoint_type
        self.logger.info(
            "Checkpoint type detected: %s for account %s",
            checkpoint_type.value,
            self.account.login,
        )

        if checkpoint_type == CheckpointType.CODE_VERIFICATION:
            return await self._handle_code_checkpoint()

        if checkpoint_type == CheckpointType.FACE_VERIFICATION:
            return await self._handle_face_checkpoint()

        if checkpoint_type == CheckpointType.ACCOUNT_DISABLED:
            raise AccountBannedError("Account is disabled by Facebook")

        if checkpoint_type == CheckpointType.SUSPICIOUS_LOGIN:
            return await self._handle_code_checkpoint()

        self.logger.warning(
            "Unknown checkpoint for account %s, falling back to manual wait.",
            self.account.login,
        )
        return await self._legacy_manual_wait()

    async def _handle_code_checkpoint(self) -> bool:
        """Attempts to auto-pass code checkpoint using email and falls back to manual wait."""
        await self._log(
            "Обнаружен CHECKPOINT. Пробуем автоматическое подтверждение по почте..."
        )

        async def _click_named_action(pattern: re.Pattern[str]) -> bool:
            locator_factories = [
                lambda: self.page.get_by_text(pattern).first,
                lambda: self.page.get_by_role("link", name=pattern).first,
                lambda: self.page.get_by_role("button", name=pattern).first,
                lambda: self.page.locator("a, button, [role='link'], [role='button']").filter(
                    has_text=pattern
                ).first,
            ]
            for factory in locator_factories:
                try:
                    locator = factory()
                    if await locator.count() == 0 or not await locator.is_visible(timeout=1500):
                        continue
                    try:
                        await self._human_click(locator)
                    except Exception:
                        await locator.click(force=True)
                    return True
                except Exception:
                    continue
            return False

        async def _has_invalid_code_error() -> bool:
            try:
                body_text = (await self.page.inner_text("body") or "").lower()
            except Exception:
                try:
                    body_text = (await self.page.content() or "").lower()
                except Exception:
                    return False
            invalid_code_patterns = [
                "bu kod çalışmıyor",
                "bu kod calismiyor",
                "doğru olduğundan emin ol",
                "dogru oldugundan emin ol",
                "invalid code",
                "incorrect code",
                "this code doesn't work",
                "this code does not work",
            ]
            return any(pattern in body_text for pattern in invalid_code_patterns)

        code_input_selectors = [
            "input[name='captcha_response']",
            'input[name="captcha_response"]',
            "input[name='code']",
            'input[name="code"]',
            "input[name='email']",
            'input[name="email"]',
            'input[placeholder*="Kod"]',
            'input[placeholder*="code"]',
            "input[inputmode='numeric']",
            'input[autocomplete="one-time-code"]',
        ]

        try:
            if await _click_named_action(
                re.compile(r"Отправить код по|Send code to|Kodu gönder", re.IGNORECASE)
            ):
                await self._log("Выбираем отправку кода на email")
                if await _click_named_action(
                    re.compile(r"Далее|Next|Devam", re.IGNORECASE)
                ):
                    await self.page.wait_for_load_state("networkidle")

            code_input = None
            for _ in range(3):
                for selector in code_input_selectors:
                    candidate = self.page.locator(selector).first
                    if await candidate.count() > 0 and await candidate.is_visible(timeout=1500):
                        code_input = candidate
                        break
                if code_input is not None:
                    break
                advanced = await _click_named_action(
                    re.compile(r"Başla|Start|Continue|Devam|Next|İleri", re.IGNORECASE)
                )
                if not advanced:
                    break
                await asyncio.sleep(3)

            if (
                code_input is not None
                and self.account.email_login
                and self.account.email_password
            ):
                used_codes: set[str] = set()
                resend_patterns = re.compile(
                    r"Новый код|New code|Resend|Yeni bir kod al",
                    re.IGNORECASE,
                )
                for attempt in range(1, 4):
                    if await _click_named_action(resend_patterns):
                        await self._log("Запрашиваю новый код подтверждения...")
                        await asyncio.sleep(8)

                    await self._log(
                        f"Ждем письмо от Facebook на {self.account.email_login}..."
                    )
                    code = await get_facebook_code(
                        email_login=self.account.email_login,
                        email_password=self.account.email_password,
                        imap_server=self.account.imap_server,
                        timeout_sec=120,
                        poll_interval_sec=10,
                        ignore_codes=used_codes,
                    )

                    if not code:
                        await self._log("Код не пришел на почту.")
                        continue

                    await self._log(f"Получен код подтверждения: {code}")
                    await code_input.focus()
                    await self._type_and_verify(code_input, code)
                    await asyncio.sleep(1)
                    await self.page.keyboard.press("Enter")
                    await asyncio.sleep(5)
                    if not self.CHECKPOINT_URL_RE.search(self.page.url or ""):
                        await self._log("Чекпоинт успешно пройден!")
                        return True

                    if await _has_invalid_code_error():
                        used_codes.add(code)
                        await self._log(
                            f"Facebook отклонил код подтверждения (попытка {attempt}/3)."
                        )
                        continue

                    submit_btn = self.page.get_by_text(
                        re.compile(
                            r"Отправить|Submit|Gönder|Продолжить|Continue|Devam",
                            re.IGNORECASE,
                        )
                    )
                    if await submit_btn.count() > 0:
                        await self._human_click(submit_btn.first)
                        await self.page.wait_for_load_state("networkidle")
                        await asyncio.sleep(5)
                        if not self.CHECKPOINT_URL_RE.search(self.page.url or ""):
                            await self._log("Чекпоинт успешно пройден!")
                            return True
            elif code_input is not None:
                await self._log(
                    f"Требуется код подтверждения, но данные от почты не указаны: {self.account.login}"
                )
        except Exception as exc:
            await self._log(
                f"Ошибка при попытке автоматического прохождения чекпоинта: {exc}"
            )

        return await self._legacy_manual_wait()

    async def _handle_face_checkpoint(self) -> bool:
        """
        Handles face/ID verification checkpoint.

        This checkpoint is not automatable, so it captures a screenshot and raises.
        """
        screenshots_dir = Path("./screenshots")
        screenshots_dir.mkdir(exist_ok=True)
        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        account_id = self.account.account_id or "unknown"
        screenshot_path = screenshots_dir / f"face_{account_id}_{timestamp}.png"
        try:
            await self.page.screenshot(path=str(screenshot_path), full_page=True)
        except Exception as exc:
            self.logger.warning(
                "Failed to capture face checkpoint screenshot for %s: %s",
                self.account.login,
                exc,
            )
            screenshot_path = screenshots_dir / f"face_{account_id}_{timestamp}_failed.png"

        message = (
            "Face/ID checkpoint detected. Manual verification required. "
            f"Screenshot saved to {screenshot_path}."
        )
        await self._log(message)
        raise AccountCheckpointError(
            message,
            checkpoint_type=CheckpointType.FACE_VERIFICATION,
            screenshot_path=str(screenshot_path),
        )

    async def _legacy_manual_wait(self) -> bool:
        """Legacy checkpoint fallback that waits for manual operator confirmation."""
        keep_open = self._parse_bool(
            os.getenv("FB_KEEP_BROWSER_ON_CHECKPOINT"), default=True
        )
        if not keep_open:
            return False

        wait_seconds = int(os.getenv("FB_CHECKPOINT_WAIT_SECONDS", "600"))
        poll_seconds = max(1, int(os.getenv("FB_CHECKPOINT_POLL_SECONDS", "5")))
        attempts = max(1, wait_seconds // poll_seconds)

        await self._log(
            "CHECKPOINT: автоматическое прохождение не удалось. "
            f"Браузер оставлен открытым на {wait_seconds} сек для ручного ввода кода."
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

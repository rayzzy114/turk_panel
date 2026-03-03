from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from models import Account, AccountStatus, Base, Proxy
from worker import AccountSessionData, FacebookBrowser, ProxyConfig

LOGGER = logging.getLogger("main_test")

load_dotenv()

DEFAULT_DATABASE_URL = "sqlite+aiosqlite:///./smm_panel_demo.db"
DEFAULT_TEST_POST_URL = "https://www.facebook.com/zuck/posts/10102577175875681/"
DEFAULT_TEST_COMMENT = "Test comment"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
)


def _parse_headless(raw_value: str | None) -> bool:
    if raw_value is None:
        return False
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


def _to_proxy_config(proxy: Proxy | None) -> ProxyConfig | None:
    if not proxy or not proxy.is_active:
        return None
    return ProxyConfig(
        host=proxy.host, port=proxy.port, user=proxy.user, password=proxy.password
    )


async def _save_screenshot_if_enabled(browser: FacebookBrowser, stage: str) -> None:
    screenshots_dir = os.getenv("FB_SCREENSHOT_DIR")
    if not screenshots_dir:
        return
    page = getattr(browser, "_page", None)
    screenshot = getattr(page, "screenshot", None)
    if page is None or not callable(screenshot):
        return

    output_dir = Path(screenshots_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / f"{stage}.png"
    await screenshot(path=str(destination), full_page=True)
    LOGGER.info("Скриншот сохранен: %s", destination)


async def run_main_test() -> None:
    """Сценарный тест входа и комментария через БД с реальными аккаунтами."""
    database_url = os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL)
    target_url = os.getenv("FB_TEST_POST_URL", DEFAULT_TEST_POST_URL)
    comment_text = os.getenv("FB_TEST_COMMENT", DEFAULT_TEST_COMMENT)
    headless = _parse_headless(os.getenv("FB_HEADLESS"))

    LOGGER.info("Подключаемся к БД: %s", database_url)
    LOGGER.info("Режим браузера: headless=%s", headless)
    engine = create_async_engine(database_url, echo=False)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        async with session_factory() as session:
            LOGGER.info("Ищем активный аккаунт...")
            stmt = (
                select(Account)
                .options(selectinload(Account.proxy))
                .where(Account.status == AccountStatus.ACTIVE)
                .order_by(Account.id.asc())
                .limit(1)
            )
            account = await session.scalar(stmt)
            if account is None:
                raise RuntimeError("Не найден активный аккаунт для демо.")

            LOGGER.info(
                "Найден аккаунт %s, proxy=%s",
                account.login,
                f"{account.proxy.host}:{account.proxy.port}"
                if account.proxy
                else "нет",
            )

            session_data = AccountSessionData(
                login=account.login,
                password=account.password,
                user_agent=account.user_agent
                or os.getenv("FB_USER_AGENT", DEFAULT_USER_AGENT),
                cookies=account.cookies,
                proxy=_to_proxy_config(account.proxy),
            )

            async with FacebookBrowser(
                account=session_data, headless=headless
            ) as browser:
                LOGGER.info("Пробуем логин под %s...", account.login)
                cookies = await browser.login()
                LOGGER.info("Логин успешен, сохраняем cookies в БД...")
                account.cookies = cookies
                await session.commit()
                await _save_screenshot_if_enabled(browser, "after_login")

                LOGGER.info("Переходим к посту: %s", target_url)
                LOGGER.info("Пишем коммент: %s", comment_text)
                comment_result = await browser.leave_comment(target_url, comment_text)
                if not comment_result:
                    raise RuntimeError(
                        f"Комментарий не отправлен для ссылки: {target_url}"
                    )
                await _save_screenshot_if_enabled(browser, "after_comment")
                LOGGER.info("Комментарий успешно отправлен. Пост: %s", target_url)
                LOGGER.info(
                    "Пауза 15 секунд перед закрытием браузера для визуальной проверки..."
                )
                await asyncio.sleep(15)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    asyncio.run(run_main_test())

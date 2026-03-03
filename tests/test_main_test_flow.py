from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import sys

import pytest

sys.path.append(str(Path(__file__).resolve().parents[1]))
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import main_test
from models import Account, AccountStatus, Base, Proxy


@dataclass
class _FakeBrowserRun:
    account: Any
    headless: bool
    login_result: list[dict[str, str]]
    leave_comment_calls: list[tuple[str, str]]


class FakeFacebookBrowser:
    runs: list[_FakeBrowserRun] = []
    next_login_result: list[dict[str, str]] = [
        {"name": "xs", "value": "token", "domain": ".facebook.com", "path": "/"}
    ]
    next_leave_result: bool = True

    def __init__(self, account: Any, headless: bool) -> None:
        self._run = _FakeBrowserRun(
            account=account,
            headless=headless,
            login_result=list(self.next_login_result),
            leave_comment_calls=[],
        )
        self.runs.append(self._run)

    async def __aenter__(self) -> "FakeFacebookBrowser":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def login(self) -> list[dict[str, str]]:
        return list(self._run.login_result)

    async def leave_comment(self, url: str, text: str) -> bool:
        self._run.leave_comment_calls.append((url, text))
        return self.next_leave_result


def _reset_fake_browser() -> None:
    FakeFacebookBrowser.runs = []
    FakeFacebookBrowser.next_login_result = [
        {"name": "xs", "value": "token", "domain": ".facebook.com", "path": "/"}
    ]
    FakeFacebookBrowser.next_leave_result = True


async def _seed_database(database_url: str, *, include_active: bool = True) -> None:
    engine = create_async_engine(database_url, echo=False)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        async with session_factory() as session:
            proxy = Proxy(host="127.0.0.1", port=1080, user="u", password="p")
            session.add(proxy)
            await session.flush()

            if include_active:
                session.add(
                    Account(
                        login="active_login",
                        password="active_pass",
                        cookies=[
                            {
                                "name": "c_user",
                                "value": "1",
                                "domain": ".facebook.com",
                                "path": "/",
                            }
                        ],
                        status=AccountStatus.ACTIVE,
                        user_agent="Mozilla/5.0 active",
                        proxy_id=proxy.id,
                    )
                )

            session.add(
                Account(
                    login="banned_login",
                    password="banned_pass",
                    cookies=None,
                    status=AccountStatus.BANNED,
                    user_agent="Mozilla/5.0 banned",
                    proxy_id=proxy.id,
                )
            )
            await session.commit()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_run_main_test_uses_first_active_account_and_proxy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _reset_fake_browser()
    monkeypatch.setattr(main_test, "FacebookBrowser", FakeFacebookBrowser)

    db_path = tmp_path / "demo.db"
    database_url = f"sqlite+aiosqlite:///{db_path}"
    await _seed_database(database_url, include_active=True)

    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("FB_TEST_POST_URL", "https://example.com/post")
    monkeypatch.setenv("FB_TEST_COMMENT", "Test comment")
    monkeypatch.delenv("FB_HEADLESS", raising=False)

    await main_test.run_main_test()

    assert len(FakeFacebookBrowser.runs) == 1
    run = FakeFacebookBrowser.runs[0]
    assert run.account.login == "active_login"
    assert run.account.proxy is not None
    assert run.account.proxy.host == "127.0.0.1"


@pytest.mark.asyncio
async def test_run_main_test_persists_cookies_after_login(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _reset_fake_browser()
    monkeypatch.setattr(main_test, "FacebookBrowser", FakeFacebookBrowser)
    FakeFacebookBrowser.next_login_result = [
        {"name": "xs", "value": "new_token", "domain": ".facebook.com", "path": "/"}
    ]

    db_path = tmp_path / "demo.db"
    database_url = f"sqlite+aiosqlite:///{db_path}"
    await _seed_database(database_url, include_active=True)

    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("FB_TEST_POST_URL", "https://example.com/post")
    monkeypatch.setenv("FB_TEST_COMMENT", "Test comment")

    await main_test.run_main_test()

    engine = create_async_engine(database_url, echo=False)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            account = await session.scalar(
                select(Account).where(Account.login == "active_login")
            )
        assert account is not None
        assert account.cookies == FakeFacebookBrowser.next_login_result
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_run_main_test_calls_leave_comment_with_env_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _reset_fake_browser()
    monkeypatch.setattr(main_test, "FacebookBrowser", FakeFacebookBrowser)

    db_path = tmp_path / "demo.db"
    database_url = f"sqlite+aiosqlite:///{db_path}"
    await _seed_database(database_url, include_active=True)

    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("FB_TEST_POST_URL", "https://example.com/specific-post")
    monkeypatch.setenv("FB_TEST_COMMENT", "Комментарий для демо")

    await main_test.run_main_test()

    run = FakeFacebookBrowser.runs[0]
    assert run.leave_comment_calls == [
        ("https://example.com/specific-post", "Комментарий для демо")
    ]


@pytest.mark.asyncio
async def test_run_main_test_headless_defaults_to_false(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _reset_fake_browser()
    monkeypatch.setattr(main_test, "FacebookBrowser", FakeFacebookBrowser)

    db_path = tmp_path / "demo.db"
    database_url = f"sqlite+aiosqlite:///{db_path}"
    await _seed_database(database_url, include_active=True)

    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("FB_TEST_POST_URL", "https://example.com/post")
    monkeypatch.setenv("FB_TEST_COMMENT", "Test comment")
    monkeypatch.delenv("FB_HEADLESS", raising=False)

    await main_test.run_main_test()

    run = FakeFacebookBrowser.runs[0]
    assert run.headless is False


@pytest.mark.asyncio
async def test_run_main_test_raises_when_no_active_accounts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _reset_fake_browser()
    monkeypatch.setattr(main_test, "FacebookBrowser", FakeFacebookBrowser)

    db_path = tmp_path / "demo.db"
    database_url = f"sqlite+aiosqlite:///{db_path}"
    await _seed_database(database_url, include_active=False)

    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("FB_TEST_POST_URL", "https://example.com/post")
    monkeypatch.setenv("FB_TEST_COMMENT", "Test comment")

    with pytest.raises(RuntimeError, match="активный аккаунт"):
        await main_test.run_main_test()


@pytest.mark.asyncio
async def test_run_main_test_raises_when_comment_link_not_returned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _reset_fake_browser()
    monkeypatch.setattr(main_test, "FacebookBrowser", FakeFacebookBrowser)
    FakeFacebookBrowser.next_leave_result = False

    db_path = tmp_path / "demo.db"
    database_url = f"sqlite+aiosqlite:///{db_path}"
    await _seed_database(database_url, include_active=True)

    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("FB_TEST_POST_URL", "https://example.com/post")
    monkeypatch.setenv("FB_TEST_COMMENT", "Test comment")

    with pytest.raises(RuntimeError, match="не отправлен"):
        await main_test.run_main_test()


@pytest.mark.asyncio
async def test_run_main_test_waits_15_seconds_before_browser_close(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _reset_fake_browser()
    monkeypatch.setattr(main_test, "FacebookBrowser", FakeFacebookBrowser)

    db_path = tmp_path / "demo.db"
    database_url = f"sqlite+aiosqlite:///{db_path}"
    await _seed_database(database_url, include_active=True)

    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr(main_test.asyncio, "sleep", fake_sleep)
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("FB_TEST_POST_URL", "https://example.com/post")
    monkeypatch.setenv("FB_TEST_COMMENT", "Test comment")

    await main_test.run_main_test()

    assert 15 in sleep_calls

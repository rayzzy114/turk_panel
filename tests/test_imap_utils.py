from __future__ import annotations

from collections.abc import Iterator
import sys
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).resolve().parents[1]))

import imap_utils


@pytest.mark.asyncio
async def test_get_facebook_code_falls_back_to_domain_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[str] = []
    outcomes: Iterator[object] = iter(
        [
            RuntimeError("primary host unavailable"),
            "123456",
        ]
    )

    def _fake_check(
        email_login: str,
        email_password: str,
        imap_server: str,
        ignore_codes: set[str] | None = None,
    ) -> str | None:
        _ = (email_login, email_password)
        assert ignore_codes in (None, set())
        attempts.append(imap_server)
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def _no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(imap_utils, "_check_inbox_sync", _fake_check)
    monkeypatch.setattr(imap_utils.asyncio, "sleep", _no_sleep)

    code = await imap_utils.get_facebook_code(
        email_login="user@kh-mail.com",
        email_password="secret",
        timeout_sec=1,
        poll_interval_sec=0,
    )

    assert code == "123456"
    assert attempts[:2] == ["imap.kh-mail.com", "kh-mail.com"]


@pytest.mark.asyncio
async def test_get_facebook_code_falls_back_to_webmail_for_kh_mail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[str] = []

    def _always_fail(
        email_login: str,
        email_password: str,
        imap_server: str,
        ignore_codes: set[str] | None = None,
    ) -> str | None:
        _ = (email_login, email_password)
        assert ignore_codes in (None, set())
        attempts.append(imap_server)
        raise RuntimeError("imap unavailable")

    async def _no_sleep(_: float) -> None:
        return None

    async def _fake_webmail_code(
        email_login: str,
        email_password: str,
        timeout_sec: int,
        poll_interval_sec: int,
        ignore_codes: set[str] | None = None,
    ) -> str | None:
        _ = (email_login, email_password, timeout_sec, poll_interval_sec, ignore_codes)
        return "654321"

    monkeypatch.setattr(imap_utils, "_check_inbox_sync", _always_fail)
    monkeypatch.setattr(imap_utils.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(
        imap_utils, "_get_facebook_code_from_webmail", _fake_webmail_code
    )

    code = await imap_utils.get_facebook_code(
        email_login="user@kh-mail.com",
        email_password="secret",
        timeout_sec=1,
        poll_interval_sec=0,
    )

    assert code == "654321"
    assert attempts[:2] == ["imap.kh-mail.com", "kh-mail.com"]


def test_extract_latest_facebook_code_from_webmail_text_ignores_used_codes() -> None:
    body_text = """
    Facebook
    46485944 sizin güvenlik kodunuz
    5 minutes ago
    Facebook
    81649043 sizin güvenlik kodunuz
    35 minutes ago
    """

    code = imap_utils._extract_latest_facebook_code_from_webmail_text(
        body_text,
        ignore_codes={"46485944"},
    )

    assert code == "81649043"

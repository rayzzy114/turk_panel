from __future__ import annotations

import zipfile
from pathlib import Path
import sys

import pytest

sys.path.append(str(Path(__file__).resolve().parents[1]))
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from models import Account


def test_parse_netscape_cookies_maps_tab_separated_fields_to_playwright_shape() -> None:
    from import_data import parse_netscape_cookies

    lines = [
        ".facebook.com\tTRUE\t/\tTRUE\t1791669498\tc_user\t100013332127347",
        ".facebook.com\tTRUE\t/\tTRUE\t1791669498\txs\tabc123",
    ]

    result = parse_netscape_cookies(lines)

    assert result == [
        {
            "name": "c_user",
            "value": "100013332127347",
            "domain": ".facebook.com",
            "path": "/",
        },
        {"name": "xs", "value": "abc123", "domain": ".facebook.com", "path": "/"},
    ]


def test_parse_netscape_cookies_skips_invalid_and_comment_lines() -> None:
    from import_data import parse_netscape_cookies

    lines = [
        "# Netscape HTTP Cookie File",
        "",
        ".facebook.com\tTRUE\t/\tTRUE\t1791669498\t\tempty_name",
        "broken\tline",
        ".facebook.com\tTRUE\t/\tTRUE\t1791669498\tc_user\t100010130409331",
    ]

    result = parse_netscape_cookies(lines)

    assert result == [
        {
            "name": "c_user",
            "value": "100010130409331",
            "domain": ".facebook.com",
            "path": "/",
        },
    ]


def test_parse_netscape_cookies_deduplicates_by_domain_path_name_keep_last() -> None:
    from import_data import parse_netscape_cookies

    lines = [
        ".facebook.com\tTRUE\t/\tTRUE\t1\txs\told",
        ".facebook.com\tTRUE\t/\tTRUE\t2\txs\tnew",
    ]

    result = parse_netscape_cookies(lines)

    assert result == [
        {"name": "xs", "value": "new", "domain": ".facebook.com", "path": "/"}
    ]


def test_extract_user_agent_prefers_native_ua_from_file() -> None:
    from import_data import extract_user_agent

    content = "\n".join(
        [
            "Simple Checker",
            "Browser: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
        ]
    )

    result = extract_user_agent(content)

    assert result
    assert "Mozilla/5.0" in result
    assert "Chrome/124.0.0.0" in result


def test_parse_account_text_cookie_only_uses_c_user_as_login() -> None:
    from import_data import parse_account_text

    content = "\n".join(
        [
            "Simple Checker",
            "– ID: 100013332127347",
            ".facebook.com\tTRUE\t/\tTRUE\t1791669498\tc_user\t100013332127347",
            ".facebook.com\tTRUE\t/\tTRUE\t1791669498\txs\tabc123",
        ]
    )

    result = parse_account_text(
        content=content,
        source_name="acc.txt",
        ua_fallback="Mozilla/5.0 fallback",
        password_placeholder="__COOKIE_ONLY__",
    )

    assert result is not None
    assert result.login == "100013332127347"
    assert result.password == "__COOKIE_ONLY__"
    assert result.user_agent == "Mozilla/5.0 fallback"
    assert result.gender == "ANY"
    assert result.cookies is not None
    assert len(result.cookies) == 2


def test_parse_account_text_supports_dolphin_json_format() -> None:
    from import_data import parse_account_text
    import json

    cookies_data = [
        {"name": "c_user", "value": "12345", "domain": ".facebook.com", "path": "/"},
        {"name": "xs", "value": "abc", "domain": ".facebook.com", "path": "/"},
    ]
    content = json.dumps(cookies_data)

    result = parse_account_text(
        content=content,
        source_name="dolphin.json",
        ua_fallback="Mozilla/5.0 fallback",
        password_placeholder="pw",
    )

    assert result is not None
    assert result.login == "12345"
    assert result.cookies is not None
    assert len(result.cookies) == 2
    assert result.cookies[0]["name"] == "c_user"


def test_convert_dolphin_cookies_filters_non_facebook() -> None:
    from import_data import convert_dolphin_cookies

    raw = [
        {
            "name": "c_user",
            "value": "123",
            "domain": ".facebook.com",
            "path": "/",
            "expirationDate": 1804288039.503868,
            "session": False,
            "httpOnly": True,
            "secure": True,
            "sameSite": "no_restriction",
        },
        {
            "name": "nid",
            "value": "google",
            "domain": ".google.com",
            "path": "/",
            "expirationDate": 1804288039.503868,
        },
    ]

    result = convert_dolphin_cookies(raw)

    assert result == [
        {
            "name": "c_user",
            "value": "123",
            "domain": ".facebook.com",
            "path": "/",
            "expires": 1804288039,
            "httpOnly": True,
            "secure": True,
            "sameSite": "None",
        }
    ]


def test_convert_dolphin_cookies_normalizes_samesite() -> None:
    from import_data import convert_dolphin_cookies

    raw = [
        {
            "name": "a",
            "value": "1",
            "domain": ".facebook.com",
            "path": "/",
            "expirationDate": 10.5,
            "sameSite": "no_restriction",
        },
        {
            "name": "b",
            "value": "2",
            "domain": ".facebook.com",
            "path": "/",
            "expirationDate": 11.5,
            "sameSite": "lax",
        },
        {
            "name": "c",
            "value": "3",
            "domain": ".facebook.com",
            "path": "/",
            "expirationDate": 12.5,
            "sameSite": "strict",
        },
    ]

    result = convert_dolphin_cookies(raw)

    assert [cookie["sameSite"] for cookie in result] == ["None", "Lax", "Strict"]


def test_convert_dolphin_cookies_converts_expiration_date() -> None:
    from import_data import convert_dolphin_cookies

    raw = [
        {
            "name": "persisted",
            "value": "1",
            "domain": ".facebook.com",
            "path": "/",
            "expirationDate": 1804288039.903868,
            "session": False,
        },
        {
            "name": "session_cookie",
            "value": "2",
            "domain": ".facebook.com",
            "path": "/",
            "session": True,
        },
    ]

    result = convert_dolphin_cookies(raw)

    assert result[0]["expires"] == 1804288039
    assert result[1]["expires"] == -1


def test_detect_cookie_format_dolphin() -> None:
    from import_data import detect_cookie_format

    raw = [{"name": "xs", "expirationDate": 1.0}]

    assert detect_cookie_format(raw) == "dolphin"


def test_detect_cookie_format_playwright() -> None:
    from import_data import detect_cookie_format

    raw = [{"name": "xs", "expires": 1}]

    assert detect_cookie_format(raw) == "playwright"


def test_normalize_cookies_passthrough_playwright() -> None:
    from import_data import normalize_cookies

    raw = [
        {
            "name": "xs",
            "value": "abc",
            "domain": ".facebook.com",
            "path": "/",
            "expires": 1,
            "sameSite": "None",
        },
        {
            "name": "nid",
            "value": "skip",
            "domain": ".google.com",
            "path": "/",
            "expires": 1,
            "sameSite": "Lax",
        },
    ]

    assert normalize_cookies(raw) == [raw[0]]


def test_normalize_cookies_auto_converts_dolphin() -> None:
    from import_data import normalize_cookies

    raw = [
        {
            "name": "xs",
            "value": "abc",
            "domain": ".facebook.com",
            "path": "/",
            "expirationDate": 1804288039.5,
            "sameSite": "no_restriction",
        }
    ]

    assert normalize_cookies(raw) == [
        {
            "name": "xs",
            "value": "abc",
            "domain": ".facebook.com",
            "path": "/",
            "expires": 1804288039,
            "httpOnly": False,
            "secure": False,
            "sameSite": "None",
        }
    ]


def test_convert_dolphin_cookies_empty_input() -> None:
    from import_data import convert_dolphin_cookies

    assert convert_dolphin_cookies([]) == []


def test_convert_dolphin_cookies_missing_expiration() -> None:
    from import_data import convert_dolphin_cookies

    raw = [{"name": "xs", "value": "abc", "domain": ".facebook.com", "path": "/"}]

    result = convert_dolphin_cookies(raw)

    assert result[0]["expires"] == -1


def test_normalize_samesite_unknown_value() -> None:
    from import_data import _normalize_samesite

    assert _normalize_samesite("weird") == "Lax"


def test_detect_gender_supports_male_female_and_any() -> None:
    from import_data import detect_gender

    assert detect_gender("acc_male_01.txt") == "M"
    assert detect_gender("batch.zip::женский_акк.txt") == "F"
    assert detect_gender("acc_unknown.txt") == "ANY"


def test_parse_account_text_detects_gender_from_name_when_filename_has_no_marker() -> (
    None
):
    from import_data import parse_account_text

    content = "\n".join(
        [
            "Simple Checker",
            "– Name: Serhat Dli",
            "– ID: 100059611961667",
            ".facebook.com\tTRUE\t/\tTRUE\t1791669498\tc_user\t100059611961667",
            ".facebook.com\tTRUE\t/\tTRUE\t1791669498\txs\tabc123",
        ]
    )

    result = parse_account_text(
        content=content,
        source_name="unknown_label.txt",
        ua_fallback="Mozilla/5.0 fallback",
        password_placeholder="__COOKIE_ONLY__",
    )

    assert result is not None
    assert result.gender == "M"


def test_parse_account_text_keeps_email_credentials_from_colon_format() -> None:
    from import_data import parse_account_text

    content = "61580000000000:fb_pass:mail@example.com:mail-pass-777"
    result = parse_account_text(
        content=content,
        source_name="acc.txt",
        ua_fallback="Mozilla/5.0 fallback",
        password_placeholder="pw",
    )

    assert result is not None
    assert result.login == "61580000000000"
    assert result.password == "fb_pass"
    assert result.email_login == "mail@example.com"
    assert result.email_password == "mail-pass-777"


def test_parse_turkish_account_format_valid_input() -> None:
    from import_data import parse_turkish_account_format

    text = (
        "facebook giriş: 61581112340247   şifre: l51dxqwk033e11   "
        "mail: tillielarriva36@kh-mail.com   mail şifre: 75f7797d8073"
    )

    row = parse_turkish_account_format(text)

    assert row is not None
    assert row.facebook_login == "61581112340247"
    assert row.facebook_password == "l51dxqwk033e11"
    assert row.email_login == "tillielarriva36@kh-mail.com"
    assert row.email_password == "75f7797d8073"
    assert row.imap_server == "imap.kh-mail.com"


def test_parse_turkish_account_format_missing_fields_returns_none() -> None:
    from import_data import parse_turkish_account_format

    text = "facebook giriş: 61581112340247   şifre: only_fb_password"

    assert parse_turkish_account_format(text) is None


def test_parse_turkish_account_format_handles_whitespace_and_parenthetical_notes() -> (
    None
):
    from import_data import parse_turkish_account_format

    text = """
        facebook giriş:   61581112340247
        şifre:   l51dxqwk033e11
        mail:   tillielarriva36@kh-mail.com
        mail şifre:   75f7797d8073
        (mail adresine www.kh-mail.com adresinden ulaşabilirsiniz.)
    """

    row = parse_turkish_account_format(text)

    assert row is not None
    assert row.facebook_login == "61581112340247"
    assert row.email_login == "tillielarriva36@kh-mail.com"


def test_detect_and_parse_line_prefers_turkish_format() -> None:
    from import_data import detect_and_parse_line

    line = (
        "facebook giriş: 61581112340247 şifre: l51dxqwk033e11 "
        "mail: tillielarriva36@kh-mail.com mail şifre: 75f7797d8073"
    )
    row = detect_and_parse_line(line)

    assert row is not None
    assert row.facebook_login == "61581112340247"
    assert row.email_password == "75f7797d8073"


@pytest.mark.asyncio
async def test_iter_account_sources_reads_zip_in_memory_without_extraction(
    tmp_path: Path,
) -> None:
    from import_data import iter_account_sources

    accounts_dir = tmp_path / "accounts"
    accounts_dir.mkdir()
    txt_path = accounts_dir / "plain.txt"
    txt_path.write_text("hello", encoding="utf-8")

    zip_path = accounts_dir / "batch.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("inside.txt", "from zip")
        archive.writestr("inside:Zone.Identifier", "skip me")

    before = sorted(p.name for p in accounts_dir.iterdir())

    found: list[str] = []
    async for source in iter_account_sources(str(accounts_dir)):
        found.append(source.name)

    after = sorted(p.name for p in accounts_dir.iterdir())

    assert "plain.txt" in found
    assert "batch.zip::inside.txt" in found
    assert "inside:Zone.Identifier" not in " ".join(found)
    assert before == after


@pytest.mark.asyncio
async def test_import_data_upserts_accounts_and_links_proxy(tmp_path: Path) -> None:
    from import_data import import_data

    accounts_dir = tmp_path / "accounts"
    accounts_dir.mkdir()
    sample_file = accounts_dir / "one.txt"
    sample_file.write_text(
        "\n".join(
            [
                "Simple Checker",
                "User-Agent: Mozilla/5.0 custom",
                "– ID: 100013332127347",
                ".facebook.com\tTRUE\t/\tTRUE\t1791669498\tc_user\t100013332127347",
                ".facebook.com\tTRUE\t/\tTRUE\t1791669498\txs\tabc123",
            ]
        ),
        encoding="utf-8",
    )

    db_path = tmp_path / "demo.db"
    database_url = f"sqlite+aiosqlite:///{db_path}"

    await import_data(database_url=database_url, accounts_dir=str(accounts_dir))

    sample_file.write_text(
        "\n".join(
            [
                "Simple Checker",
                "User-Agent: Mozilla/5.0 changed",
                "– ID: 100013332127347",
                ".facebook.com\tTRUE\t/\tTRUE\t1791669498\tc_user\t100013332127347",
                ".facebook.com\tTRUE\t/\tTRUE\t1791669498\txs\txyz987",
            ]
        ),
        encoding="utf-8",
    )

    await import_data(database_url=database_url, accounts_dir=str(accounts_dir))

    second_file = accounts_dir / "female_account.txt"
    second_file.write_text(
        "\n".join(
            [
                "Simple Checker",
                "User-Agent: Mozilla/5.0 custom",
                "– ID: 100099999999999",
                ".facebook.com\tTRUE\t/\tTRUE\t1791669498\tc_user\t100099999999999",
                ".facebook.com\tTRUE\t/\tTRUE\t1791669498\txs\tabc999",
            ]
        ),
        encoding="utf-8",
    )
    await import_data(database_url=database_url, accounts_dir=str(accounts_dir))

    engine = create_async_engine(database_url, echo=False)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            count_stmt = select(func.count()).select_from(Account)
            account_count = await session.scalar(count_stmt)
            account = await session.scalar(
                select(Account).where(Account.login == "100013332127347")
            )
            female_account = await session.scalar(
                select(Account).where(Account.login == "100099999999999")
            )

        assert account_count == 2
        assert account is not None
        assert account.proxy_id is not None
        assert account.user_agent == "Mozilla/5.0 changed"
        assert account.gender == "ANY"
        assert account.cookies and account.cookies[-1]["value"] == "xyz987"
        assert female_account is not None
        assert female_account.gender == "F"
    finally:
        await engine.dispose()

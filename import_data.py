from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

from dotenv import load_dotenv
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from models import Base, Proxy
from crud import upsert_account

LOGGER = logging.getLogger("import_data")

load_dotenv()

DEFAULT_DATABASE_URL = "sqlite+aiosqlite:///./smm_panel_demo.db"
DEFAULT_ACCOUNTS_DIR = "./accounts"
DEFAULT_PASSWORD_PLACEHOLDER = "__COOKIE_ONLY__"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
)
PROXY_RAW = (
    "geo.iproyal.com:12321:AVF9IqAqUIsBW8dU:"
    "G9ukazsFGLcJ3QXC_country-tr_city-istanbul_session-3HMgmqIq_lifetime-30m"
)

_UA_HINTS = ("user-agent", "user agent", "browser", "agent", "ua:")
_UA_SIGNATURE = re.compile(
    r"(mozilla/5\.0|chrome/|edg/|firefox/|safari/)", re.IGNORECASE
)
_ID_RE = re.compile(r"\bID:\s*(\d+)", re.IGNORECASE)
_NAME_RE = re.compile(r"(?:^|\n)\s*[–-]\s*Name:\s*(.+)", re.IGNORECASE)
_CRED_RE_TURKISH = re.compile(
    r"(?:facebook\s*giri[sş]:|İD:|ID:)\s*(\S+)\s*(?:[ŞşSs]ifre:|password:)\s*(\S+)",
    re.IGNORECASE,
)

_GENDER_MAP = {
    "M": {"male", "man", "men", "boy", "masc", "m", "муж", "мужской", "парень"},
    "F": {"female", "woman", "women", "girl", "fem", "f", "жен", "женский", "девушка"},
}
_COMMON_MALE_NAMES = {
    "abdullah",
    "ahmet",
    "alio",
    "ali",
    "avhan",
    "berat",
    "bolat",
    "burak",
    "dli",
    "emir",
    "enes",
    "faruk",
    "hakan",
    "ibrahim",
    "kocer",
    "mahmut",
    "maliki",
    "mehmet",
    "murat",
    "mustafa",
    "onurhan",
    "oumarou",
    "onur",
    "osman",
    "ramazan",
    "semi",
    "taylan",
    "serhat",
    "tuncer",
    "yaldiz",
    "yigit",
    "yusuf",
    "kutlu",
}
_COMMON_FEMALE_NAMES = {
    "ayse",
    "elif",
    "esra",
    "fatma",
    "gamze",
    "hatice",
    "leyla",
    "melisa",
    "meryem",
    "seda",
    "sevgi",
    "zeynep",
}


@dataclass(slots=True)
class AccountSource:
    name: str
    content: str


@dataclass(slots=True)
class ParsedAccount:
    login: str
    password: str
    cookies: list[dict[str, str]]
    user_agent: str
    gender: str


@dataclass(slots=True)
class ProxyParts:
    host: str
    port: int
    user: str | None
    password: str | None
    session_id: str | None
    name: str | None = None


def parse_proxy_string(raw: str) -> ProxyParts:
    clean = raw.strip()
    name = None
    if "|" in clean:
        name_part, clean = clean.split("|", 1)
        name = name_part.strip()
        clean = clean.strip()

    if clean.startswith("http://"):
        clean = clean[7:]
    elif clean.startswith("https://"):
        clean = clean[8:]

    parts = clean.split(":")
    if len(parts) < 2:
        raise ValueError(f"Некорректный формат прокси: {raw}")
    host = parts[0].strip()
    try:
        port = int(parts[1].strip())
    except (ValueError, IndexError):
        raise ValueError(f"Некорректный порт в прокси: {raw}")

    user = parts[2].strip() if len(parts) >= 3 and parts[2].strip() else None
    password = ":".join(parts[3:]).strip() if len(parts) >= 4 else None

    # Extract session ID (e.g., session-RjhAO2rB)
    session_id = None
    session_re = re.compile(r"(session-[a-z0-9]+)", re.IGNORECASE)

    # Search in password first (common for IPROYAL)
    if password:
        match = session_re.search(password)
        if match:
            session_id = match.group(1)

    # Fallback to user
    if not session_id and user:
        match = session_re.search(user)
        if match:
            session_id = match.group(1)

    return ProxyParts(
        host=host,
        port=port,
        user=user,
        password=password or None,
        session_id=session_id,
        name=name,
    )


def parse_netscape_cookies(lines: list[str]) -> list[dict[str, str]]:
    dedup: dict[tuple[str, str, str], dict[str, str]] = {}
    for line in lines:
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        columns = line.split("\t")
        if len(columns) != 7:
            continue

        domain = columns[0].strip()
        path = columns[2].strip() or "/"
        name = columns[5].strip()
        value = columns[6].strip()
        if not domain or not name:
            continue

        item = {"name": name, "value": value, "domain": domain, "path": path}
        dedup[(domain, path, name)] = item

    return list(dedup.values())


def extract_user_agent(text: str) -> str | None:
    fallback = None
    for line in text.splitlines():
        line = line.strip()
        lower = line.lower()
        if not _UA_SIGNATURE.search(line):
            continue

        if any(hint in lower for hint in _UA_HINTS):
            marker = re.search(r"(mozilla/5\.0.*)$", line, re.IGNORECASE)
            return marker.group(1).strip() if marker else line

        if fallback is None:
            marker = re.search(r"(mozilla/5\.0.*)$", line, re.IGNORECASE)
            fallback = marker.group(1).strip() if marker else line

    return fallback


def _parse_colon_credentials(lines: list[str]) -> tuple[str, str] | None:
    blocked_prefixes = (
        "id:",
        "name:",
        "url:",
        "friends:",
        "business:",
        "marketplace:",
        "country:",
        "cookies:",
        "user-agent:",
        "user agent:",
        "browser:",
        "ua:",
    )
    for line in lines:
        value = line.strip()
        lower = value.lower()
        if not value or "\t" in value or value.startswith("http"):
            continue
        if value.startswith("–") or lower.startswith("-"):
            continue
        if any(lower.startswith(prefix) for prefix in blocked_prefixes):
            continue
        parts = [chunk.strip() for chunk in value.split(":")]
        if len(parts) == 2:
            login, password = parts
            if login and password and " " not in login:
                return login, password
    return None


def _extract_id(text: str) -> str | None:
    match = _ID_RE.search(text)
    return match.group(1) if match else None


def detect_gender(source_name: str, display_name: str | None = None) -> str:
    text = source_name.lower().replace("::", "/")
    if display_name:
        text += " " + display_name.lower()

    tokens = set(re.split(r"[^a-zа-я0-9]+", text))

    is_male = any(
        token in _GENDER_MAP["M"] or token in _COMMON_MALE_NAMES for token in tokens
    )
    is_female = any(
        token in _GENDER_MAP["F"] or token in _COMMON_FEMALE_NAMES for token in tokens
    )

    if is_male and not is_female:
        return "M"
    if is_female and not is_male:
        return "F"
    return "ANY"


def extract_display_name(text: str) -> str | None:
    match = _NAME_RE.search(text)
    if not match:
        return None
    value = match.group(1).strip()
    return value or None


def _try_parse_json_cookies(content: str) -> ParsedAccount | None:
    try:
        data = json.loads(content)
        if isinstance(data, list) and len(data) > 0:
            cookies = []
            login = None
            for item in data:
                if not isinstance(item, dict):
                    continue
                name = item.get("name")
                value = item.get("value")
                domain = item.get("domain")
                path = item.get("path") or "/"

                if name and value:
                    cookies.append(
                        {
                            "name": str(name),
                            "value": str(value),
                            "domain": str(domain) if domain else ".facebook.com",
                            "path": str(path),
                        }
                    )
                    if name == "c_user":
                        login = str(value)

            if login and cookies:
                return ParsedAccount(
                    login=login,
                    password=DEFAULT_PASSWORD_PLACEHOLDER,
                    cookies=cookies,
                    user_agent=DEFAULT_USER_AGENT,
                    gender="ANY",
                )
    except Exception:
        pass
    return None


def parse_account_text(
    content: str,
    source_name: str,
    ua_fallback: str,
    password_placeholder: str = DEFAULT_PASSWORD_PLACEHOLDER,
) -> ParsedAccount | None:
    json_parsed = _try_parse_json_cookies(content)
    if json_parsed:
        return json_parsed

    lines = content.splitlines()
    user_agent = extract_user_agent(content) or ua_fallback

    credentials = _parse_colon_credentials(lines)
    if not credentials:
        match = _CRED_RE_TURKISH.search(content)
        if match:
            credentials = (match.group(1), match.group(2))

    cookies = parse_netscape_cookies(lines)

    login: str | None = None
    password: str | None = None
    if credentials:
        login, password = credentials

    if not login and cookies:
        c_user = next(
            (cookie["value"] for cookie in cookies if cookie["name"] == "c_user"), None
        )
        if c_user:
            login = c_user

    if not login:
        login = _extract_id(content)

    if not password:
        password = password_placeholder

    if not login:
        LOGGER.warning("Файл пропущен: %s (не найден login/ID).", source_name)
        return None

    if not cookies:
        LOGGER.info("Аккаунт %s импортирован без кук (только логин/пароль).", login)

    gender = detect_gender(source_name, extract_display_name(content))

    return ParsedAccount(
        login=login,
        password=password,
        cookies=cookies,
        user_agent=user_agent,
        gender=gender,
    )


async def iter_account_sources(accounts_dir: str) -> AsyncIterator[AccountSource]:
    path = Path(accounts_dir)

    for file_path in sorted(path.glob("*.txt")):
        if "Zone.Identifier" in file_path.name:
            continue
        yield AccountSource(
            name=file_path.name,
            content=file_path.read_text(encoding="utf-8", errors="ignore"),
        )

    for zip_path in sorted(path.glob("*.zip")):
        if "Zone.Identifier" in zip_path.name:
            continue
        with zipfile.ZipFile(zip_path) as archive:
            for member in sorted(archive.namelist()):
                if not member.endswith(".txt") or "Zone.Identifier" in member:
                    continue
                payload = archive.read(member).decode("utf-8", errors="ignore")
                yield AccountSource(name=f"{zip_path.name}::{member}", content=payload)


async def _get_or_create_proxy(session: AsyncSession, proxy_parts: ProxyParts) -> int:
    stmt = select(Proxy).where(
        Proxy.host == proxy_parts.host,
        Proxy.port == proxy_parts.port,
        Proxy.user == proxy_parts.user,
        Proxy.password == proxy_parts.password,
    )
    proxy = await session.scalar(stmt)
    if proxy:
        if proxy_parts.name and proxy.name != proxy_parts.name:
            proxy.name = proxy_parts.name
            await session.flush()
        LOGGER.info("Прокси найден: %s:%s", proxy.host, proxy.port)
        return proxy.id

    proxy = Proxy(
        name=proxy_parts.name,
        host=proxy_parts.host,
        port=proxy_parts.port,
        user=proxy_parts.user,
        password=proxy_parts.password,
        session_id=proxy_parts.session_id,
        is_active=True,
    )
    session.add(proxy)
    await session.flush()
    LOGGER.info("Прокси сохранен: %s:%s", proxy.host, proxy.port)
    return proxy.id


@dataclass(slots=True)
class ImportSummary:
    imported: int
    skipped: int
    proxy_id: int | None = None


async def import_data(
    database_url: str = DEFAULT_DATABASE_URL, accounts_dir: str = DEFAULT_ACCOUNTS_DIR
) -> ImportSummary:
    LOGGER.info("Загружаем прокси из PROXY_RAW...")
    proxy_parts = parse_proxy_string(PROXY_RAW)
    ua_fallback = os.getenv("FB_USER_AGENT", DEFAULT_USER_AGENT)
    password_placeholder = os.getenv(
        "ACCOUNT_PASSWORD_PLACEHOLDER", DEFAULT_PASSWORD_PLACEHOLDER
    )

    engine = create_async_engine(database_url, echo=False)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    imported = 0
    skipped = 0

    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        async with session_factory() as session:
            default_proxy_id = await _get_or_create_proxy(session, proxy_parts)
            async for source in iter_account_sources(accounts_dir):
                LOGGER.info("Парсим файл аккаунта: %s", source.name)
                parsed = parse_account_text(
                    content=source.content,
                    source_name=source.name,
                    ua_fallback=ua_fallback,
                    password_placeholder=password_placeholder,
                )
                if not parsed:
                    skipped += 1
                    continue

                await upsert_account(
                    session=session,
                    login=parsed.login,
                    password=parsed.password,
                    user_agent=parsed.user_agent,
                    gender=parsed.gender,
                    cookies=parsed.cookies,
                    default_proxy_id=default_proxy_id,
                )

                imported += 1
                LOGGER.info("Аккаунт импортирован: %s", parsed.login)
                # Flush to make proxy_id visible to _get_available_proxy in the next iteration
                await session.flush()

            await session.commit()
    finally:
        await engine.dispose()

    LOGGER.info("Импорт завершен: импортировано=%s, пропущено=%s", imported, skipped)
    return ImportSummary(imported=imported, skipped=skipped, proxy_id=None)


async def _main() -> None:
    database_url = os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL)
    accounts_dir = os.getenv("ACCOUNTS_DIR", DEFAULT_ACCOUNTS_DIR)
    await import_data(database_url=database_url, accounts_dir=accounts_dir)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    asyncio.run(_main())

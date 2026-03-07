from __future__ import annotations

import asyncio
import logging
import os
import random
import secrets
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast

from fastapi import Depends, FastAPI, HTTPException, Request, status, UploadFile, File
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field
from dotenv import load_dotenv
from sqlalchemy import delete, event, func, or_, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from apify_api import ApifyAPI, ApifyAPIError
from iproxy_utils import get_current_ip, rotate_mobile_ip
from panel_api import PanelAPI
from models import (
    Account,
    AccountStatus,
    Base,
    CheckpointType,
    Proxy,
    Task,
    TaskActionType,
    TaskStatus,
    WarmupLog,
)
from worker import (
    AccountBannedError,
    AccountCaptchaError,
    AccountCookieInvalidError,
    AccountCheckpointError,
    AccountInvalidCredentialsError,
    AccountSessionData,
    FacebookBrowser,
    ProxyConfig,
)
from crud import get_available_proxy_id, upsert_account
from import_data import detect_cookie_format, normalize_cookies

load_dotenv()

LOGGER = logging.getLogger("api")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./smm_panel_demo.db")
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

engine = create_async_engine(DATABASE_URL, echo=False)


if engine.url.get_backend_name() == "sqlite":

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection: Any, _: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=15000")
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
        except Exception:
            pass
        cursor.close()


SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
ALLOWED_GENDERS = {"M", "F", "ANY"}
ALLOWED_PROXY_TYPES = {"datacenter", "residential", "mobile"}
DAILY_ACTION_LIMIT = 15
SHADOW_BAN_HOURS = 72
WARMUP_RECENT_HOURS = 6
security = HTTPBasic()
_ensure_tables_lock = asyncio.Lock()
_tables_ready = False


class TaskCreate(BaseModel):
    url: str
    action_type: TaskActionType
    payload_text: str | None = None
    quantity: int = Field(default=1, ge=1, le=100_000)
    target_gender: Literal["M", "F", "ANY"] = "ANY"
    account_id: int | None = None
    target_author_id: str | None = None
    target_author_name: str | None = None


class LogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    message: str
    created_at: datetime


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, use_enum_values=True)

    id: int
    account_id: int | None
    action_type: TaskActionType
    target_url: str
    payload_text: str | None
    target_author_id: str | None
    target_author_name: str | None
    external_order_id: int | None
    target_gender: str
    status: TaskStatus
    logs: list[LogOut] = []


TaskOut.model_rebuild()


class ParseCommentsIn(BaseModel):
    url: str
    limit: int = Field(default=10, ge=1, le=100)


class AccountOut(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    id: int
    login: str
    status: str
    proxy_id: int | None
    gender: str
    daily_actions_count: int
    last_action_date: date | None
    shadow_ban_started_at: datetime | None = None
    shadow_ban_until: datetime | None = None
    warmed_up_at: datetime | None = None
    email_login: str | None = None
    email_password: str | None = None
    last_checkpoint_type: str | None = None
    proxy_type: str | None = None
    proxy_rotation_url: str | None = None


class WarmupLogOut(BaseModel):
    """Serialized warmup session log output."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    account_id: int
    started_at: datetime
    finished_at: datetime | None
    duration_seconds: int | None
    actions_attempted: int
    actions_succeeded: int
    actions_failed: int
    action_log: list[dict[str, Any]] | None
    result: str
    error_message: str | None


class AccountImportIn(BaseModel):
    """Bulk account import payload for textarea-based input."""

    raw_data: str


class AccountUpdateIn(BaseModel):
    """Account update payload."""

    email_login: str | None = None
    email_password: str | None = None
    imap_server: str | None = None
    proxy_id: int | None = None
    proxy_type: str | None = None
    proxy_rotation_url: str | None = None


class AccountCookiesIn(BaseModel):
    """Raw cookie upload payload for one account."""

    cookies: list[dict[str, Any]]


def _parse_bool(value: str | None, *, default: bool = True) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _get_service_id(action: TaskActionType) -> int:
    if action == TaskActionType.LIKE_POST:
        return 11928
    if action == TaskActionType.FOLLOW:
        return 11927
    if action == TaskActionType.LIKE_COMMENT:
        return 2057
    raise RuntimeError(f"Для action_type={action.value} нет внешнего service ID.")


def _normalize_gender(value: str | None) -> str:
    candidate = (value or "ANY").strip().upper()
    if candidate not in ALLOWED_GENDERS:
        raise ValueError(f"Некорректный gender: {value}")
    return candidate


def _normalize_target_author_id(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    return normalized


def _normalize_target_author_name(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    return normalized


def _is_account_daily_limited(account: Account, today: date | None = None) -> bool:
    check_date = today or date.today()
    return (
        account.last_action_date == check_date
        and account.daily_actions_count >= DAILY_ACTION_LIMIT
    )


def _verify_admin(credentials: HTTPBasicCredentials = Depends(security)) -> None:
    expected_login = os.getenv("ADMIN_LOGIN", "admin")
    expected_pass = os.getenv("ADMIN_PASS", "admin")
    login_ok = secrets.compare_digest(credentials.username, expected_login)
    pass_ok = secrets.compare_digest(credentials.password, expected_pass)
    if not (login_ok and pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )


def _proxy_from_account(account: Account) -> ProxyConfig | None:
    if account.proxy is None:
        return None
    return ProxyConfig(
        host=account.proxy.host,
        port=account.proxy.port,
        user=account.proxy.user,
        password=account.proxy.password,
    )


def _proxy_url_from_account(account: Account) -> str | None:
    """Builds proxy URL for outbound helper requests."""
    if account.proxy is None:
        return None
    if account.proxy.user and account.proxy.password:
        return (
            f"http://{account.proxy.user}:{account.proxy.password}"
            f"@{account.proxy.host}:{account.proxy.port}"
        )
    return f"http://{account.proxy.host}:{account.proxy.port}"


async def _ensure_tables() -> None:
    global _tables_ready

    if _tables_ready:
        return

    async with _ensure_tables_lock:
        if _tables_ready:
            return

        if engine.url.get_backend_name() == "sqlite":
            async with engine.connect() as connection:
                schema_is_current = await connection.run_sync(_sqlite_schema_is_current)
            if schema_is_current:
                _tables_ready = True
                return

        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            await connection.run_sync(_migrate_schema_if_needed)

        _tables_ready = True


def _migrate_schema_if_needed(connection: Any) -> None:
    if connection.dialect.name != "sqlite":
        return

    def add_column_if_missing(table: str, column: str, col_type: str):
        cols = connection.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
        if column not in [c[1] for c in cols]:
            LOGGER.info("Миграция: Добавляем колонку %s в таблицу %s", column, table)
            connection.exec_driver_sql(
                f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"
            )

    # Авто-миграции для новых полей (KISS)
    add_column_if_missing("proxies", "session_id", "TEXT")
    add_column_if_missing("accounts", "storage_state", "JSON")
    add_column_if_missing("proxies", "name", "TEXT")
    add_column_if_missing("accounts", "gender", "TEXT DEFAULT 'ANY'")
    add_column_if_missing("accounts", "daily_actions_count", "INTEGER DEFAULT 0")
    add_column_if_missing("accounts", "last_action_date", "DATE")
    add_column_if_missing("tasks", "external_order_id", "INTEGER")
    add_column_if_missing("tasks", "target_gender", "TEXT DEFAULT 'ANY'")
    add_column_if_missing("tasks", "target_author_id", "TEXT")
    add_column_if_missing("tasks", "target_author_name", "TEXT")
    add_column_if_missing("accounts", "email_login", "TEXT")
    add_column_if_missing("accounts", "email_password", "TEXT")
    add_column_if_missing("accounts", "imap_server", "TEXT")
    add_column_if_missing("accounts", "shadow_ban_started_at", "DATETIME")
    add_column_if_missing("accounts", "shadow_ban_until", "DATETIME")
    add_column_if_missing("accounts", "warmed_up_at", "DATETIME")
    add_column_if_missing("accounts", "last_checkpoint_type", "TEXT")
    add_column_if_missing(
        "accounts", "proxy_type", "TEXT DEFAULT 'datacenter'"
    )
    add_column_if_missing("accounts", "proxy_rotation_url", "TEXT")

    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS warmup_logs (
            id INTEGER PRIMARY KEY,
            account_id INTEGER NOT NULL,
            started_at DATETIME,
            finished_at DATETIME,
            duration_seconds INTEGER,
            actions_attempted INTEGER DEFAULT 0,
            actions_succeeded INTEGER DEFAULT 0,
            actions_failed INTEGER DEFAULT 0,
            action_log JSON,
            result TEXT DEFAULT 'unknown',
            error_message TEXT,
            FOREIGN KEY(account_id) REFERENCES accounts(id)
        )
        """
    )
    if connection.exec_driver_sql(
        "SELECT 1 FROM accounts WHERE proxy_type IS NULL OR proxy_type='' LIMIT 1"
    ).fetchone():
        connection.exec_driver_sql(
            "UPDATE accounts SET proxy_type='datacenter' WHERE proxy_type IS NULL OR proxy_type=''"
        )

    # Приведение данных к правильным значениям Enum (имена членов - верхний регистр)
    if connection.exec_driver_sql(
        """
        SELECT 1
        FROM tasks
        WHERE action_type IN (
            'like',
            'like_post',
            'follow',
            'comment',
            'comment_post',
            'like_comment',
            'reply_comment',
            'warmup'
        )
        LIMIT 1
        """
    ).fetchone():
        connection.exec_driver_sql(
            "UPDATE tasks SET action_type='LIKE_POST' WHERE action_type IN ('like', 'like_post')"
        )
        connection.exec_driver_sql(
            "UPDATE tasks SET action_type='FOLLOW' WHERE action_type IN ('follow')"
        )
        connection.exec_driver_sql(
            "UPDATE tasks SET action_type='COMMENT_POST' WHERE action_type IN ('comment', 'comment_post')"
        )
        connection.exec_driver_sql(
            "UPDATE tasks SET action_type='LIKE_COMMENT' WHERE action_type IN ('like_comment')"
        )
        connection.exec_driver_sql(
            "UPDATE tasks SET action_type='REPLY_COMMENT' WHERE action_type IN ('reply_comment')"
        )
        connection.exec_driver_sql(
            "UPDATE tasks SET action_type='WARMUP' WHERE action_type IN ('warmup')"
        )

    if connection.exec_driver_sql(
        """
        SELECT 1
        FROM tasks
        WHERE status IN ('pending', 'in_progress', 'success', 'error', 'stopped')
        LIMIT 1
        """
    ).fetchone():
        connection.exec_driver_sql(
            "UPDATE tasks SET status='PENDING' WHERE status IN ('pending')"
        )
        connection.exec_driver_sql(
            "UPDATE tasks SET status='IN_PROGRESS' WHERE status IN ('in_progress')"
        )
        connection.exec_driver_sql(
            "UPDATE tasks SET status='SUCCESS' WHERE status IN ('success')"
        )
        connection.exec_driver_sql(
            "UPDATE tasks SET status='ERROR' WHERE status IN ('error')"
        )
        connection.exec_driver_sql(
            "UPDATE tasks SET status='STOPPED' WHERE status IN ('stopped')"
        )


def _sqlite_schema_is_current(connection: Any) -> bool:
    """Returns True when the SQLite schema and required data backfills are already applied."""
    if connection.dialect.name != "sqlite":
        return False

    required_columns = {
        "proxies": {"id", "host", "port", "user", "password", "session_id", "name"},
        "accounts": {
            "id",
            "login",
            "password",
            "cookies",
            "storage_state",
            "proxy_id",
            "status",
            "gender",
            "daily_actions_count",
            "last_action_date",
            "user_agent",
            "email_login",
            "email_password",
            "imap_server",
            "shadow_ban_started_at",
            "shadow_ban_until",
            "warmed_up_at",
            "last_checkpoint_type",
            "proxy_type",
            "proxy_rotation_url",
        },
        "tasks": {
            "id",
            "account_id",
            "action_type",
            "target_url",
            "payload_text",
            "external_order_id",
            "target_gender",
            "status",
            "target_author_id",
            "target_author_name",
        },
        "logs": {"id", "task_id", "message", "created_at"},
        "warmup_logs": {
            "id",
            "account_id",
            "started_at",
            "finished_at",
            "duration_seconds",
            "actions_attempted",
            "actions_succeeded",
            "actions_failed",
            "action_log",
            "result",
            "error_message",
        },
    }

    for table_name, required in required_columns.items():
        rows = connection.exec_driver_sql(f"PRAGMA table_info({table_name})").fetchall()
        current = {row[1] for row in rows}
        if not required.issubset(current):
            return False

    if connection.exec_driver_sql(
        "SELECT 1 FROM accounts WHERE proxy_type IS NULL OR proxy_type='' LIMIT 1"
    ).fetchone():
        return False

    if connection.exec_driver_sql(
        """
        SELECT 1
        FROM tasks
        WHERE action_type IN (
            'like',
            'like_post',
            'follow',
            'comment',
            'comment_post',
            'like_comment',
            'reply_comment',
            'warmup'
        )
           OR status IN ('pending', 'in_progress', 'success', 'error', 'stopped')
        LIMIT 1
        """
    ).fetchone():
        return False

    return True


@asynccontextmanager
async def lifespan(_: FastAPI):
    await _ensure_tables()
    worker_task = asyncio.create_task(browser_worker_loop())
    yield
    worker_task.cancel()


app = FastAPI(
    title="SMM Hybrid Panel", lifespan=lifespan, dependencies=[Depends(_verify_admin)]
)


async def _get_active_account(
    session: Any,
    target_url: str,
    target_gender: str = "ANY",
    action_type: TaskActionType | None = None,
    target_author_id: str | None = None,
) -> Account:
    released = await _release_expired_shadow_bans(session)
    if released:
        await session.commit()

    normalized_target_gender = _normalize_gender(target_gender)
    normalized_author = _normalize_target_author_id(target_author_id)
    normalized_target_author_id = (
        normalized_author.lower() if normalized_author is not None else None
    )
    today = date.today()

    # 1. Находим ID аккаунтов, которые сейчас заняты любыми браузерными задачами
    busy_accounts_stmt = select(Task.account_id).where(
        Task.status == TaskStatus.IN_PROGRESS, Task.account_id.is_not(None)
    )
    busy_account_ids = (await session.execute(busy_accounts_stmt)).scalars().all()

    # 2. Исключаем тех, кто УЖЕ назначен на задачи по этому же URL (PENDING или SUCCESS)
    # ERROR intentionally is not in this list: failed task means action not completed.
    already_assigned_stmt = select(Task.account_id).where(
        Task.target_url == target_url,
        Task.status.in_(
            [TaskStatus.PENDING, TaskStatus.IN_PROGRESS, TaskStatus.SUCCESS]
        ),
        Task.account_id.is_not(None),
    )
    if action_type is not None:
        # Different actions on same URL should not block each other (reply vs like_comment_bot).
        already_assigned_stmt = already_assigned_stmt.where(
            Task.action_type == action_type
        )
    already_assigned_ids = (
        (await session.execute(already_assigned_stmt)).scalars().all()
    )

    stmt = (
        select(Account)
        .options(selectinload(Account.proxy))
        .where(Account.status == AccountStatus.ACTIVE)
    )

    # Объединяем списки исключений
    exclude_ids = set(busy_account_ids) | set(already_assigned_ids)
    if exclude_ids:
        stmt = stmt.where(Account.id.notin_(list(exclude_ids)))

    # 3. Фильтруем по лимитам и полу
    stmt = stmt.where(
        or_(
            Account.last_action_date.is_(None),
            Account.last_action_date != today,
            Account.daily_actions_count < DAILY_ACTION_LIMIT,
        )
    )
    if normalized_target_gender != "ANY":
        stmt = stmt.where(Account.gender == normalized_target_gender)
    if (
        action_type == TaskActionType.REPLY_COMMENT
        and normalized_target_author_id is not None
    ):
        stmt = stmt.where(func.lower(Account.login) != normalized_target_author_id)

    # 4. Приоритет LRU
    stmt = stmt.order_by(
        Account.last_action_date.asc(),
        Account.daily_actions_count.asc(),
        Account.id.asc(),
    ).limit(1)

    account = await session.scalar(stmt)
    if account is None:
        raise RuntimeError(
            "Не найден свободный аккаунт, который еще не выполнял действий по этой ссылке."
        )
    return account


async def _get_available_proxy_id(session: Any) -> int | None:
    return await get_available_proxy_id(session)


async def _rotate_account_proxy(session: Any, account: Account) -> None:
    """Finds a random active proxy that is NOT in use by any non-banned account."""
    new_proxy_id = await _get_available_proxy_id(session)
    if new_proxy_id:
        old_id = account.proxy_id
        account.proxy_id = new_proxy_id
        session.add(account)
        await session.commit()
        LOGGER.info(
            "РОТАЦИЯ ПРОКСИ: Аккаунт %s переключен с прокси ID %s на %s",
            account.login,
            old_id,
            new_proxy_id,
        )
    else:
        LOGGER.warning("РОТАЦИЯ ПРОКСИ: Нет свободных активных прокси для замены.")


async def _block_account_due_to_captcha(
    session: Any,
    account: Account,
    *,
    reason: str | Exception,
    checkpoint_type: CheckpointType | None = None,
) -> None:
    reason_text = str(reason).lower()
    if checkpoint_type is not None or "checkpoint" in reason_text:
        account.status = AccountStatus.CHECKPOINT
        account.last_checkpoint_type = (
            checkpoint_type.value
            if checkpoint_type is not None
            else CheckpointType.UNKNOWN_CHECKPOINT.value
        )
    else:
        account.status = AccountStatus.CAPTCHA_BLOCKED
        account.last_checkpoint_type = None
    try:
        await _rotate_account_proxy(session, account)
    except Exception as e:
        LOGGER.error("Не удалось сменить прокси при блокировке: %s", e)

    session.add(account)
    await session.commit()
    LOGGER.warning("Account %s blocked: %s", account.login, reason)


async def _mark_account_invalid_credentials(
    session: Any,
    account: Account,
    *,
    reason: str | Exception,
) -> None:
    """Marks account as unusable due to rejected login credentials."""
    account.status = AccountStatus.INVALID_CREDENTIALS
    account.last_checkpoint_type = None
    session.add(account)
    await session.commit()
    LOGGER.warning("Account %s invalid credentials: %s", account.login, reason)


async def _mark_account_cookie_invalid(
    session: Any,
    account: Account,
    *,
    reason: str | Exception,
) -> None:
    """Marks account as requiring fresh Dolphin cookie import."""
    account.status = AccountStatus.COOKIE_INVALID
    account.last_checkpoint_type = None
    session.add(account)
    await session.commit()
    LOGGER.warning("Account %s cookie invalid: %s", account.login, reason)


def _mark_account_action_success(account: Account) -> None:
    today = date.today()
    if account.last_action_date == today:
        account.daily_actions_count += 1
    else:
        account.last_action_date = today
        account.daily_actions_count = 1


def _utc_now() -> datetime:
    return datetime.now(UTC)


async def _release_expired_shadow_bans(session: Any) -> int:
    now = _utc_now()
    stmt = select(Account).where(
        Account.status == AccountStatus.SHADOW_BANNED,
        Account.shadow_ban_until.is_not(None),
        Account.shadow_ban_until <= now,
    )
    accounts = (await session.execute(stmt)).scalars().all()
    if not accounts:
        return 0

    for account in accounts:
        account.status = AccountStatus.ACTIVE
        account.shadow_ban_started_at = None
        account.shadow_ban_until = None
        session.add(account)

    return len(accounts)


MTP_SEMAPHORE = asyncio.Semaphore(5)


async def _process_provider_task_with_quantity(
    session: Any, task: Task, quantity: int, username: str | None = None
) -> None:
    async with MTP_SEMAPHORE:
        service_id = _get_service_id(task.action_type)
        if service_id <= 0:
            raise RuntimeError(
                f"Не задан service ID для action_type={task.action_type.value}."
            )
        panel = PanelAPI()
        try:
            kwargs = {}
            if username:
                kwargs["username"] = username
            order_id = await panel.add_order(
                service_id=service_id, link=task.target_url, quantity=quantity, **kwargs
            )
        finally:
            await panel.aclose()
        task.external_order_id = order_id
        task.status = TaskStatus.SUCCESS


async def _add_task_log(session: Any, task_id: int, message: str) -> None:
    from models import Log

    log = Log(task_id=task_id, message=message)
    session.add(log)
    await session.commit()


class ProxyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str | None
    host: str
    port: int
    user: str | None
    session_id: str | None
    is_active: bool
    in_use_by_account: str | None = None


class ProxyImportIn(BaseModel):
    raw_data: str  # Bulk host:port:user:pass lines


class BulkDeleteIn(BaseModel):
    ids: list[int] = Field(default_factory=list)


ACCOUNT_SELECTION_LOCK = asyncio.Lock()


async def _save_account_state(browser, account, session) -> None:
    state = await browser.get_storage_state()
    if state:
        account.storage_state = state
        cookies = state.get("cookies", [])
        if cookies:
            account.cookies = cookies
        await session.commit()


def _warmup_default_result() -> dict[str, Any]:
    """Returns a normalized empty warmup result payload."""
    return {
        "result": "error",
        "actions_attempted": 0,
        "actions_succeeded": 0,
        "actions_failed": 0,
        "action_log": [],
        "error_message": None,
        "duration_seconds": 0,
    }


def _normalize_warmup_result(warmup_raw: Any) -> dict[str, Any]:
    """Normalizes warmup return payloads to a consistent result dict."""
    if isinstance(warmup_raw, dict):
        result = _warmup_default_result()
        result.update(warmup_raw)
        result["action_log"] = list(result.get("action_log") or [])
        return result

    if warmup_raw:
        result = _warmup_default_result()
        result["result"] = "completed"
        return result

    result = _warmup_default_result()
    result["error_message"] = "warmup_failed"
    result["actions_failed"] = 1
    return result


def _warmup_error_result(error_message: str) -> dict[str, Any]:
    """Builds an error warmup payload for failed browser/login flows."""
    result = _warmup_default_result()
    result["error_message"] = error_message
    result["actions_failed"] = 1
    return result


async def _store_warmup_log(
    session: Any,
    *,
    account_id: int,
    started_at: datetime,
    warmup_result: dict[str, Any],
) -> None:
    """Persists one completed warmup session log."""
    finished_at = _utc_now()
    duration_seconds = int(max(1, (finished_at - started_at).total_seconds()))
    warmup_log = WarmupLog(
        account_id=account_id,
        started_at=started_at,
        finished_at=finished_at,
        duration_seconds=int(warmup_result.get("duration_seconds") or duration_seconds),
        actions_attempted=int(warmup_result.get("actions_attempted") or 0),
        actions_succeeded=int(warmup_result.get("actions_succeeded") or 0),
        actions_failed=int(warmup_result.get("actions_failed") or 0),
        action_log=cast(list[dict[str, Any]], warmup_result.get("action_log") or []),
        result=str(warmup_result.get("result") or "error"),
        error_message=warmup_result.get("error_message"),
    )
    session.add(warmup_log)
    await session.flush()


def _hours_since_warmup(account: Account, now: datetime) -> float | None:
    """Returns hours since last warmup for an account or None if never warmed."""
    if not account.warmed_up_at:
        return None
    warmed_up_at = account.warmed_up_at
    if warmed_up_at.tzinfo is None:
        warmed_up_at = warmed_up_at.replace(tzinfo=UTC)
    return (now - warmed_up_at).total_seconds() / 3600


async def _process_browser_task(session: Any, task: Task) -> bool:
    """Returns True if task finished (success/error/stopped), False if needs immediate retry."""
    await session.refresh(task)
    if task.status == TaskStatus.STOPPED:
        return True

    released = await _release_expired_shadow_bans(session)
    if released:
        await session.commit()

    if not task.account_id:
        async with ACCOUNT_SELECTION_LOCK:
            try:
                # Повторно проверяем статус задачи внутри лока
                await session.refresh(task)
                if task.status == TaskStatus.STOPPED:
                    return True

                account = await _get_active_account(
                    session,
                    task.target_url,
                    task.target_gender,
                    action_type=task.action_type,
                    target_author_id=task.target_author_id,
                )
                task.account_id = account.id
                await session.commit()
            except Exception as e:
                await _add_task_log(session, task.id, f"Ошибка подбора аккаунта: {e}")
                task.status = TaskStatus.ERROR
                return False
    else:
        account = await session.scalar(
            select(Account)
            .options(selectinload(Account.proxy))
            .where(Account.id == task.account_id)
        )
        if account is None:
            await _add_task_log(
                session,
                task.id,
                "Назначенный аккаунт не найден. Освобождаю задачу для нового подбора.",
            )
            task.account_id = None
            task.status = TaskStatus.PENDING
            await session.commit()
            return False

        if account.status != AccountStatus.ACTIVE:
            await _add_task_log(
                session,
                task.id,
                f"Аккаунт {account.login} имеет статус {account.status.value} и пропускается.",
            )
            if task.action_type == TaskActionType.CHECK_LOGIN:
                task.status = TaskStatus.ERROR
                await _add_task_log(
                    session,
                    task.id,
                    "Проверка входа отменена: выбранный аккаунт неактивен.",
                )
                await session.commit()
                return True
            task.account_id = None
            task.status = TaskStatus.PENDING
            await session.commit()
            return False

    session_data = AccountSessionData(
        account_id=account.id,
        login=account.login,
        password=account.password,
        user_agent=account.user_agent,
        cookies=account.cookies,
        storage_state=account.storage_state,
        proxy=_proxy_from_account(account),
        email_login=account.email_login,
        email_password=account.email_password,
        imap_server=account.imap_server,
        proxy_type=account.proxy_type,
        proxy_rotation_url=account.proxy_rotation_url,
    )
    headless = _parse_bool(os.getenv("FB_HEADLESS"), default=True)
    warmup_started_at = _utc_now() if task.action_type == TaskActionType.WARMUP else None
    warmup_log_saved = False

    try:
        await _add_task_log(session, task.id, f"Используем аккаунт {account.login}")

        async def worker_log(msg: str):
            await _add_task_log(session, task.id, msg)

        async with FacebookBrowser(
            account=session_data,
            headless=headless,
            strict_cookie_session=False,
            log_callback=worker_log,
        ) as browser:
            await browser.login()
            await _save_account_state(browser, account, session)
            await _add_task_log(session, task.id, "Вход выполнен.")

            await session.refresh(task)
            if task.status == TaskStatus.STOPPED:
                await _add_task_log(
                    session, task.id, "Задача остановлена пользователем."
                )
                return True

            if task.action_type == TaskActionType.CHECK_LOGIN:
                ok = True
            elif task.action_type == TaskActionType.WARMUP:
                warmup_result = _warmup_default_result()
                try:
                    warmup_raw = await browser.warmup(
                        duration_seconds=random.randint(300, 600)
                    )
                    warmup_result = _normalize_warmup_result(warmup_raw)
                except (AccountBannedError, AccountCaptchaError):
                    raise
                except Exception as exc:
                    LOGGER.exception(
                        "Warmup task failed for account_id=%s: %s", account.id, exc
                    )
                    warmup_result = _warmup_error_result(str(exc))
                    raise
                finally:
                    if warmup_started_at is not None:
                        await _store_warmup_log(
                            session,
                            account_id=account.id,
                            started_at=warmup_started_at,
                            warmup_result=warmup_result,
                        )
                        warmup_log_saved = True

                ok = warmup_result.get("result") == "completed"
                if ok:
                    account.warmed_up_at = _utc_now()
                    session.add(account)
                    await _save_account_state(browser, account, session)
            else:
                delay = float(random.randint(10, 25))
                await _add_task_log(
                    session, task.id, f"Антибан: пауза {int(delay)} сек..."
                )

                slept = 0
                while slept < delay:
                    await asyncio.sleep(min(5, delay - slept))
                    slept += 5
                    await session.refresh(task)
                    if task.status == TaskStatus.STOPPED:
                        await _add_task_log(
                            session, task.id, "Задача прервана во время ожидания."
                        )
                        return True

                if task.action_type == TaskActionType.LIKE_COMMENT_BOT:
                    ok = await browser.like_comment(task.target_url)
                    if not ok:
                        await _add_task_log(
                            session, task.id, "Ошибка: Кнопка лайка не найдена."
                        )
                elif task.action_type == TaskActionType.REPLY_COMMENT:
                    ok = await browser.reply_comment(
                        task.target_url, task.payload_text or ""
                    )
                    if not ok:
                        await _add_task_log(
                            session, task.id, "Ошибка: Не удалось отправить ответ."
                        )
                else:
                    ok = await browser.leave_comment(
                        task.target_url, task.payload_text or "Test"
                    )
                    if not ok:
                        await _add_task_log(
                            session, task.id, "Ошибка: Не удалось оставить комментарий."
                        )

                if ok:
                    await _save_account_state(browser, account, session)

        await session.refresh(task)
        if task.status == TaskStatus.STOPPED:
            return True

        task.status = TaskStatus.SUCCESS if ok else TaskStatus.ERROR
        await _add_task_log(
            session, task.id, f"Завершено: {'Успех' if ok else 'Ошибка'}"
        )
        if ok and task.action_type != TaskActionType.WARMUP:
            _mark_account_action_success(account)
        return True

    except AccountBannedError as exc:
        if (
            task.action_type == TaskActionType.WARMUP
            and warmup_started_at is not None
            and not warmup_log_saved
        ):
            await _store_warmup_log(
                session,
                account_id=account.id,
                started_at=warmup_started_at,
                warmup_result=_warmup_error_result(str(exc)),
            )
        account.status = AccountStatus.BANNED
        account.last_checkpoint_type = CheckpointType.ACCOUNT_DISABLED.value
        session.add(account)
        await session.commit()
        task.status = TaskStatus.ERROR
        await _add_task_log(
            session,
            task.id,
            f"АККАУНТ {account.login} ЗАБЛОКИРОВАН FACEBOOK: {exc}",
        )
        return True
    except AccountCookieInvalidError as exc:
        if (
            task.action_type == TaskActionType.WARMUP
            and warmup_started_at is not None
            and not warmup_log_saved
        ):
            await _store_warmup_log(
                session,
                account_id=account.id,
                started_at=warmup_started_at,
                warmup_result=_warmup_error_result(str(exc)),
            )
        await _mark_account_cookie_invalid(session, account, reason=str(exc))
        task.status = TaskStatus.ERROR
        if task.action_type == TaskActionType.WARMUP:
            await _add_task_log(
                session,
                task.id,
                f"Не удалось прогреть аккаунт {account.login}: Re-import cookies from Dolphin.",
            )
        elif task.action_type == TaskActionType.CHECK_LOGIN:
            await _add_task_log(
                session,
                task.id,
                f"Не удалось проверить вход для аккаунта {account.login}: Re-import cookies from Dolphin.",
            )
        else:
            await _add_task_log(
                session,
                task.id,
                f"Аккаунт {account.login} требует свежие куки: Re-import cookies from Dolphin.",
            )
        return True
    except AccountInvalidCredentialsError as exc:
        if (
            task.action_type == TaskActionType.WARMUP
            and warmup_started_at is not None
            and not warmup_log_saved
        ):
            await _store_warmup_log(
                session,
                account_id=account.id,
                started_at=warmup_started_at,
                warmup_result=_warmup_error_result(str(exc)),
            )
        await _mark_account_invalid_credentials(session, account, reason=str(exc))
        task.status = TaskStatus.ERROR
        if task.action_type == TaskActionType.WARMUP:
            await _add_task_log(
                session,
                task.id,
                f"Не удалось прогреть аккаунт {account.login}: Facebook отклонил логин или пароль.",
            )
        elif task.action_type == TaskActionType.CHECK_LOGIN:
            await _add_task_log(
                session,
                task.id,
                f"Не удалось проверить вход для аккаунта {account.login}: Facebook отклонил логин или пароль.",
            )
        else:
            await _add_task_log(
                session,
                task.id,
                f"Аккаунт {account.login} не смог войти: Facebook отклонил логин или пароль.",
            )
        return True
    except AccountCaptchaError as exc:
        if (
            task.action_type == TaskActionType.WARMUP
            and warmup_started_at is not None
            and not warmup_log_saved
        ):
            await _store_warmup_log(
                session,
                account_id=account.id,
                started_at=warmup_started_at,
                warmup_result=_warmup_error_result(str(exc)),
            )
        checkpoint_type = None
        if isinstance(exc, AccountCheckpointError):
            checkpoint_type = exc.checkpoint_type
        await _block_account_due_to_captcha(
            session,
            account,
            reason=str(exc),
            checkpoint_type=checkpoint_type,
        )
        should_retry_with_other_account = task.action_type not in {
            TaskActionType.WARMUP,
            TaskActionType.CHECK_LOGIN,
        }
        if should_retry_with_other_account:
            task.account_id = None  # Освобождаем задачу для другого аккаунта
            task.status = TaskStatus.PENDING
        else:
            task.status = TaskStatus.ERROR
        if checkpoint_type is not None or "checkpoint" in str(exc).lower():
            await _add_task_log(
                session,
                task.id,
                f"CHECKPOINT: аккаунт {account.login} отправлен на проверку Facebook.",
            )
        if should_retry_with_other_account:
            await _add_task_log(
                session,
                task.id,
                f"АККАУНТ {account.login} ВЫЛЕТЕЛ: {exc}. Ищу замену...",
            )
        elif task.action_type == TaskActionType.WARMUP:
            await _add_task_log(
                session,
                task.id,
                f"Не удалось прогреть аккаунт {account.login}: не смог зайти ({exc}).",
            )
        elif task.action_type == TaskActionType.CHECK_LOGIN:
            await _add_task_log(
                session,
                task.id,
                f"Не удалось проверить вход для аккаунта {account.login}: {exc}.",
            )
        else:
            await _add_task_log(
                session,
                task.id,
                f"АККАУНТ {account.login} ВЫЛЕТЕЛ: {exc}.",
            )
        return not should_retry_with_other_account
    except Exception as exc:
        if (
            task.action_type == TaskActionType.WARMUP
            and warmup_started_at is not None
            and not warmup_log_saved
        ):
            await _store_warmup_log(
                session,
                account_id=account.id,
                started_at=warmup_started_at,
                warmup_result=_warmup_error_result(str(exc)),
            )
        task.status = TaskStatus.ERROR
        await _add_task_log(session, task.id, f"Критическая ошибка браузера: {exc}")
        return True


async def process_provider_task(task_id: int) -> None:
    await _ensure_tables()
    async with SessionLocal() as session:
        task = await session.scalar(select(Task).where(Task.id == task_id))
        if task is None:
            return

        try:
            task.status = TaskStatus.IN_PROGRESS
            await session.commit()

            quantity = 1
            username = None
            if task.payload_text:
                parts = task.payload_text.split("|", 1)
                if parts[0].isdigit():
                    quantity = int(parts[0])
                if len(parts) > 1:
                    username = parts[1]

            await _process_provider_task_with_quantity(
                session, task, quantity, username
            )
        except Exception:
            LOGGER.exception("Ошибка выполнения задачи id=%s", task_id)
            task.status = TaskStatus.ERROR
        finally:
            await session.commit()


MAX_CONCURRENT_BROWSER_TASKS = int(os.getenv("MAX_CONCURRENT_TASKS", "3"))


async def _run_browser_task_wrapper(task_id: int, semaphore: asyncio.Semaphore):
    async with semaphore:
        try:
            async with SessionLocal() as session:
                task = await session.scalar(select(Task).where(Task.id == task_id))
                if not task:
                    return

                task.status = TaskStatus.IN_PROGRESS
                await session.commit()

                was_processed = await _process_browser_task(session, task)
                await session.commit()

                if was_processed:
                    delay = random.uniform(
                        60, 150
                    )  # Уменьшил паузу, раз мы в параллели
                    LOGGER.info(
                        "Задача %s завершена. Пауза потока %.1f сек...", task_id, delay
                    )
                    await asyncio.sleep(delay)
        except Exception:
            LOGGER.exception("Ошибка в потоке задачи %s", task_id)


async def browser_worker_loop() -> None:
    LOGGER.info(
        "Запущен параллельный воркер TURKISH PANEL (Лимит: %s)",
        MAX_CONCURRENT_BROWSER_TASKS,
    )
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_BROWSER_TASKS)
    active_tasks = set()

    while True:
        try:
            # Очищаем завершенные задачи из списка активных
            active_tasks = {t for t in active_tasks if not t.done()}

            if len(active_tasks) < MAX_CONCURRENT_BROWSER_TASKS:
                async with SessionLocal() as session:
                    released = await _release_expired_shadow_bans(session)
                    if released:
                        await session.commit()
                    # Ищем задачи, которые еще не взяты в работу
                    stmt = (
                        select(Task.id)
                        .where(
                            Task.status == TaskStatus.PENDING,
                            Task.action_type.in_(
                                [
                                    TaskActionType.COMMENT_POST,
                                    TaskActionType.LIKE_COMMENT_BOT,
                                    TaskActionType.REPLY_COMMENT,
                                    TaskActionType.CHECK_LOGIN,
                                    TaskActionType.WARMUP,
                                ]
                            ),
                        )
                        .order_by(Task.id.asc())
                        .limit(MAX_CONCURRENT_BROWSER_TASKS - len(active_tasks))
                    )
                    task_ids = (await session.execute(stmt)).scalars().all()

                    for tid in task_ids:
                        # Чтобы не запускать одну и ту же задачу дважды, пометим её как IN_PROGRESS сразу
                        task_obj = await session.get(Task, tid)
                        if task_obj:
                            task_obj.status = TaskStatus.IN_PROGRESS
                            await session.commit()

                            t = asyncio.create_task(
                                _run_browser_task_wrapper(tid, semaphore)
                            )
                            active_tasks.add(t)

            await asyncio.sleep(5)
        except asyncio.CancelledError:
            break
        except Exception:
            LOGGER.exception("Ошибка в цикле воркера")
            await asyncio.sleep(10)


@app.get("/api/proxies", response_model=list[ProxyOut])
async def get_proxies() -> list[ProxyOut]:
    async with SessionLocal() as session:
        # Get all proxies
        proxies = (await session.execute(select(Proxy))).scalars().all()

        # Get all non-banned accounts that have a proxy_id
        stmt = select(Account).where(
            Account.status != AccountStatus.BANNED, Account.proxy_id.is_not(None)
        )
        accounts = (await session.execute(stmt)).scalars().all()

        proxy_to_user = {a.proxy_id: a.login for a in accounts}

        result = []
        for p in proxies:
            out = ProxyOut.model_validate(p)
            out.in_use_by_account = proxy_to_user.get(p.id)
            result.append(out)

    return result


@app.post("/api/proxies/import")
async def import_proxies(payload: ProxyImportIn) -> dict[str, Any]:
    from import_data import parse_proxy_string

    count = 0
    async with SessionLocal() as session:
        for line in payload.raw_data.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                p = parse_proxy_string(line)
                # Проверка на дубликаты
                existing = await session.scalar(
                    select(Proxy).where(
                        Proxy.host == p.host,
                        Proxy.port == p.port,
                        Proxy.user == p.user,
                        Proxy.password == p.password,
                    )
                )
                if existing:
                    if p.name and existing.name != p.name:
                        existing.name = p.name
                    continue

                proxy = Proxy(
                    name=p.name,
                    host=p.host,
                    port=p.port,
                    user=p.user,
                    password=p.password,
                    session_id=p.session_id,
                )
                session.add(proxy)
                count += 1
            except Exception:
                continue
        await session.commit()
    return {"status": "success", "imported": count}


@app.post("/api/tasks/{task_id}/stop")
async def stop_task(task_id: int) -> dict[str, Any]:
    async with SessionLocal() as session:
        task = await session.scalar(select(Task).where(Task.id == task_id))
        if not task:
            raise HTTPException(status_code=404, detail="Задача не найдена")
        if task.status in {TaskStatus.PENDING, TaskStatus.IN_PROGRESS}:
            task.status = TaskStatus.STOPPED
            await _add_task_log(session, task.id, "СИГНАЛ ОСТАНОВКИ ПОЛУЧЕН.")
            await session.commit()
            return {"status": "success", "message": "Задача останавливается..."}
    return {"status": "error", "message": "Задачу нельзя остановить в текущем статусе"}


@app.post("/api/tasks/clear")
async def clear_tasks() -> dict[str, Any]:
    from sqlalchemy import delete

    async with SessionLocal() as session:
        await session.execute(delete(Task))
        await session.commit()
    return {"status": "success", "message": "Очередь задач очищена"}


class ProxyUpdateIn(BaseModel):
    proxy_id: int | None


class AccountEmailUpdateIn(BaseModel):
    email_login: str | None = None
    email_password: str | None = None


def _normalize_optional_str(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _normalize_proxy_type(value: str | None) -> str:
    """Normalizes proxy_type to one of supported values."""
    candidate = (value or "datacenter").strip().lower()
    if candidate not in ALLOWED_PROXY_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Некорректный proxy_type: {value}",
        )
    return candidate


def _unique_positive_ids(ids: list[int]) -> list[int]:
    seen: set[int] = set()
    result: list[int] = []
    for item in ids:
        if item <= 0:
            continue
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


async def _enqueue_warmup_task(
    session: Any,
    account: Account,
) -> dict[str, Any]:
    """Creates a queued warmup task unless the account was warmed recently."""
    if account.status != AccountStatus.ACTIVE:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Аккаунт имеет статус {account.status.value} "
                "и не может участвовать в прогреве."
            ),
        )

    hours_since = _hours_since_warmup(account, _utc_now())
    if hours_since is not None and hours_since < WARMUP_RECENT_HOURS:
        return {
            "status": "skipped",
            "reason": f"Already warmed up {hours_since:.1f}h ago",
            "warmed_up_at": account.warmed_up_at,
        }

    existing_task = await session.scalar(
        select(Task)
        .where(
            Task.account_id == account.id,
            Task.action_type == TaskActionType.WARMUP,
            Task.status.in_([TaskStatus.PENDING, TaskStatus.IN_PROGRESS]),
        )
        .order_by(Task.id.desc())
    )
    if existing_task is not None:
        return {
            "status": "queued",
            "task_id": existing_task.id,
            "message": "Прогрев уже стоит в очереди для этого аккаунта",
        }

    task = Task(
        account_id=account.id,
        action_type=TaskActionType.WARMUP,
        target_url="https://www.facebook.com/",
        payload_text=None,
        target_gender="ANY",
        status=TaskStatus.PENDING,
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    await _add_task_log(
        session,
        task.id,
        f"Прогрев аккаунта {account.login} поставлен в очередь.",
    )
    return {
        "status": "queued",
        "task_id": task.id,
        "message": "Задача прогрева добавлена в очередь",
    }


@app.put("/api/accounts/{account_id}")
async def update_account(account_id: int, payload: AccountUpdateIn) -> dict[str, Any]:
    """Updates mutable account fields including proxy metadata."""
    async with SessionLocal() as session:
        account = await session.scalar(
            select(Account).options(selectinload(Account.proxy)).where(Account.id == account_id)
        )
        if not account:
            raise HTTPException(status_code=404, detail="Аккаунт не найден")

        fields_set = payload.model_fields_set

        if "proxy_id" in fields_set:
            if payload.proxy_id is not None:
                proxy = await session.scalar(
                    select(Proxy).where(Proxy.id == payload.proxy_id)
                )
                if not proxy:
                    raise HTTPException(status_code=400, detail="Прокси не найден")

                stmt = select(Account).where(
                    Account.proxy_id == payload.proxy_id,
                    Account.id != account_id,
                    Account.status != AccountStatus.BANNED,
                )
                existing = await session.scalar(stmt)
                if existing:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"Прокси ID {payload.proxy_id} уже используется живым аккаунтом "
                            f"{existing.login}"
                        ),
                    )
            account.proxy_id = payload.proxy_id

        if "email_login" in fields_set:
            account.email_login = _normalize_optional_str(payload.email_login)
        if "email_password" in fields_set:
            account.email_password = _normalize_optional_str(payload.email_password)
        if "imap_server" in fields_set:
            account.imap_server = _normalize_optional_str(payload.imap_server)
        if "proxy_rotation_url" in fields_set:
            account.proxy_rotation_url = _normalize_optional_str(payload.proxy_rotation_url)

        if "proxy_type" in fields_set:
            account.proxy_type = _normalize_proxy_type(payload.proxy_type)
        elif not account.proxy_type:
            account.proxy_type = "datacenter"

        if account.proxy_rotation_url and account.proxy_type != "mobile":
            account.proxy_type = "mobile"

        session.add(account)
        await session.commit()
    return {"status": "success"}


@app.post("/api/accounts/{account_id}/rotate-ip")
async def rotate_account_ip(account_id: int) -> dict[str, Any]:
    """Triggers mobile proxy rotation and returns fresh external IP."""
    async with SessionLocal() as session:
        account = await session.scalar(
            select(Account).options(selectinload(Account.proxy)).where(Account.id == account_id)
        )
        if not account:
            raise HTTPException(status_code=404, detail="Аккаунт не найден")

        if (account.proxy_type or "").strip().lower() != "mobile":
            raise HTTPException(
                status_code=400,
                detail="Поворот IP доступен только для mobile proxy",
            )
        if not account.proxy_rotation_url:
            raise HTTPException(
                status_code=400,
                detail="У аккаунта не задан proxy_rotation_url",
            )
        if not account.proxy:
            raise HTTPException(
                status_code=400,
                detail="У аккаунта не назначен прокси",
            )

        rotated = await rotate_mobile_ip(account.proxy_rotation_url)
        if not rotated:
            raise HTTPException(status_code=502, detail="Не удалось повернуть IP прокси")
        proxy_url = _proxy_url_from_account(account)
        new_ip = await get_current_ip(proxy_url or "")
        return {"status": "ok", "new_ip": new_ip}


@app.put("/api/accounts/{account_id}/proxy")
async def update_account_proxy(
    account_id: int, payload: ProxyUpdateIn
) -> dict[str, Any]:
    async with SessionLocal() as session:
        account = await session.scalar(select(Account).where(Account.id == account_id))
        if not account:
            raise HTTPException(status_code=404, detail="Аккаунт не найден")

        if payload.proxy_id is not None:
            proxy = await session.scalar(
                select(Proxy).where(Proxy.id == payload.proxy_id)
            )
            if not proxy:
                raise HTTPException(status_code=400, detail="Прокси не найден")

            # Проверка: не занят ли этот прокси другим живым аккаунтом
            stmt = select(Account).where(
                Account.proxy_id == payload.proxy_id,
                Account.id != account_id,
                Account.status != AccountStatus.BANNED,
            )
            existing = await session.scalar(stmt)
            if existing:
                raise HTTPException(
                    status_code=400,
                    detail=f"Прокси ID {payload.proxy_id} уже используется живым аккаунтом {existing.login}",
                )

        account.proxy_id = payload.proxy_id
        session.add(account)
        await session.commit()
    return {"status": "success"}


@app.put("/api/accounts/{account_id}/email")
async def update_account_email(
    account_id: int, payload: AccountEmailUpdateIn
) -> dict[str, Any]:
    async with SessionLocal() as session:
        account = await session.scalar(select(Account).where(Account.id == account_id))
        if not account:
            raise HTTPException(status_code=404, detail="Аккаунт не найден")

        account.email_login = _normalize_optional_str(payload.email_login)
        account.email_password = _normalize_optional_str(payload.email_password)
        session.add(account)
        await session.commit()

    return {
        "status": "success",
        "email_login": account.email_login,
        "email_password": account.email_password,
    }


@app.post("/api/accounts/{account_id}/cookies")
async def upload_account_cookies(
    account_id: int, payload: AccountCookiesIn
) -> dict[str, Any]:
    """Validates and saves normalized Facebook cookies for one account."""
    detected_format = detect_cookie_format(payload.cookies)
    LOGGER.debug(
        "Detected cookie format for account %s: %s", account_id, detected_format
    )
    normalized = normalize_cookies(payload.cookies)
    required_present = sorted(
        {
            cookie["name"]
            for cookie in normalized
            if cookie.get("name") in {"c_user", "xs", "datr", "sb"}
        }
    )
    missing_required: list[str] = []
    if "c_user" not in required_present:
        missing_required.append("c_user")
    if "xs" not in required_present:
        missing_required.append("xs")
    if "datr" not in required_present and "sb" not in required_present:
        missing_required.extend(["datr", "sb"])

    dropped = len(payload.cookies) - len(normalized)
    if not normalized or missing_required:
        LOGGER.warning(
            "Cookie import rejected for account %s. Missing required: %s",
            account_id,
            missing_required,
        )
        raise HTTPException(
            status_code=400,
            detail={
                "status": "error",
                "detected_format": detected_format,
                "cookies_received": len(payload.cookies),
                "cookies_kept": len(normalized),
                "cookies_dropped": dropped,
                "required_present": required_present,
                "missing_required": missing_required,
            },
        )

    async with SessionLocal() as session:
        account = await session.scalar(select(Account).where(Account.id == account_id))
        if not account:
            raise HTTPException(status_code=404, detail="Аккаунт не найден")
        account.cookies = normalized
        if account.status == AccountStatus.COOKIE_INVALID:
            account.status = AccountStatus.ACTIVE
        session.add(account)
        await session.commit()

    return {
        "status": "ok",
        "detected_format": detected_format,
        "cookies_received": len(payload.cookies),
        "cookies_kept": len(normalized),
        "cookies_dropped": dropped,
        "required_present": required_present,
        "missing_required": [],
    }


@app.post("/api/accounts/bulk_delete")
async def bulk_delete_accounts(payload: BulkDeleteIn) -> dict[str, Any]:
    normalized_ids = _unique_positive_ids(payload.ids)
    if not normalized_ids:
        raise HTTPException(status_code=400, detail="Список ids пуст")

    await _ensure_tables()
    async with SessionLocal() as session:
        existing_ids = (
            (
                await session.execute(
                    select(Account.id).where(Account.id.in_(normalized_ids))
                )
            )
            .scalars()
            .all()
        )
        existing_id_set = set(existing_ids)
        if existing_id_set:
            await session.execute(
                delete(Account).where(Account.id.in_(list(existing_id_set)))
            )
            await session.commit()

    not_found = [item for item in normalized_ids if item not in existing_id_set]
    return {
        "status": "success",
        "deleted": len(existing_id_set),
        "not_found": not_found,
    }


@app.post("/api/accounts/bulk_ban")
async def bulk_ban_accounts(payload: BulkDeleteIn) -> dict[str, Any]:
    normalized_ids = _unique_positive_ids(payload.ids)
    if not normalized_ids:
        raise HTTPException(status_code=400, detail="Список ids пуст")

    await _ensure_tables()
    async with SessionLocal() as session:
        rows = (
            (
                await session.execute(
                    select(Account).where(Account.id.in_(normalized_ids))
                )
            )
            .scalars()
            .all()
        )
        existing_id_set = {row.id for row in rows}
        for account in rows:
            account.status = AccountStatus.BANNED
            account.shadow_ban_started_at = None
            account.shadow_ban_until = None
            session.add(account)
        if rows:
            await session.commit()

    not_found = [item for item in normalized_ids if item not in existing_id_set]
    return {
        "status": "success",
        "updated": len(existing_id_set),
        "not_found": not_found,
    }


@app.post("/api/accounts/bulk_shadow_ban")
async def bulk_shadow_ban_accounts(payload: BulkDeleteIn) -> dict[str, Any]:
    normalized_ids = _unique_positive_ids(payload.ids)
    if not normalized_ids:
        raise HTTPException(status_code=400, detail="Список ids пуст")

    now = _utc_now()
    ban_until = now + timedelta(hours=SHADOW_BAN_HOURS)

    await _ensure_tables()
    async with SessionLocal() as session:
        rows = (
            (
                await session.execute(
                    select(Account).where(Account.id.in_(normalized_ids))
                )
            )
            .scalars()
            .all()
        )
        existing_id_set = {row.id for row in rows}
        for account in rows:
            account.status = AccountStatus.SHADOW_BANNED
            account.shadow_ban_started_at = now
            account.shadow_ban_until = ban_until
            session.add(account)
        if rows:
            await session.commit()

    not_found = [item for item in normalized_ids if item not in existing_id_set]
    return {
        "status": "success",
        "updated": len(existing_id_set),
        "not_found": not_found,
        "shadow_ban_until": ban_until.isoformat(),
    }


@app.post("/api/accounts/bulk_shadow_unban")
async def bulk_shadow_unban_accounts(payload: BulkDeleteIn) -> dict[str, Any]:
    normalized_ids = _unique_positive_ids(payload.ids)
    if not normalized_ids:
        raise HTTPException(status_code=400, detail="Список ids пуст")

    await _ensure_tables()
    async with SessionLocal() as session:
        rows = (
            (
                await session.execute(
                    select(Account).where(Account.id.in_(normalized_ids))
                )
            )
            .scalars()
            .all()
        )
        existing_id_set = {row.id for row in rows}
        for account in rows:
            account.status = AccountStatus.ACTIVE
            account.shadow_ban_started_at = None
            account.shadow_ban_until = None
            session.add(account)
        if rows:
            await session.commit()

    not_found = [item for item in normalized_ids if item not in existing_id_set]
    return {
        "status": "success",
        "updated": len(existing_id_set),
        "not_found": not_found,
    }


@app.post("/api/accounts/bulk_unassign_proxy")
async def bulk_unassign_proxy_accounts(payload: BulkDeleteIn) -> dict[str, Any]:
    normalized_ids = _unique_positive_ids(payload.ids)
    if not normalized_ids:
        raise HTTPException(status_code=400, detail="Список ids пуст")

    await _ensure_tables()
    async with SessionLocal() as session:
        rows = (
            (
                await session.execute(
                    select(Account).where(Account.id.in_(normalized_ids))
                )
            )
            .scalars()
            .all()
        )
        existing_id_set = {row.id for row in rows}
        for account in rows:
            account.proxy_id = None
            session.add(account)
        if rows:
            await session.commit()

    not_found = [item for item in normalized_ids if item not in existing_id_set]
    return {
        "status": "success",
        "updated": len(existing_id_set),
        "not_found": not_found,
    }


@app.post("/api/accounts/bulk_check_login")
async def bulk_check_login_accounts(payload: BulkDeleteIn) -> dict[str, Any]:
    normalized_ids = _unique_positive_ids(payload.ids)
    if not normalized_ids:
        raise HTTPException(status_code=400, detail="Список ids пуст")

    await _ensure_tables()
    async with SessionLocal() as session:
        all_rows = (
            (
                await session.execute(
                    select(Account).where(Account.id.in_(normalized_ids))
                )
            )
            .scalars()
            .all()
        )
        existing_id_set = {row.id for row in all_rows}
        active_rows = [row for row in all_rows if row.status == AccountStatus.ACTIVE]
        skipped_not_active = [
            row.id for row in all_rows if row.status != AccountStatus.ACTIVE
        ]

        for account in active_rows:
            task = Task(
                account_id=account.id,
                action_type=TaskActionType.CHECK_LOGIN,
                target_url="https://www.facebook.com/",
                payload_text=None,
                target_gender="ANY",
                status=TaskStatus.PENDING,
            )
            session.add(task)
        if active_rows:
            await session.commit()

    not_found = [item for item in normalized_ids if item not in existing_id_set]
    return {
        "status": "success",
        "created_tasks": len(active_rows),
        "skipped_not_active": skipped_not_active,
        "not_found": not_found,
    }


@app.post("/api/accounts/bulk_warmup")
async def bulk_warmup_accounts(payload: BulkDeleteIn) -> dict[str, Any]:
    normalized_ids = _unique_positive_ids(payload.ids)
    if not normalized_ids:
        raise HTTPException(status_code=400, detail="Список ids пуст")

    await _ensure_tables()
    async with SessionLocal() as session:
        rows = (
            (
                await session.execute(
                    select(Account).where(Account.id.in_(normalized_ids))
                )
            )
            .scalars()
            .all()
        )
        existing_ids = {row.id for row in rows}
        active_rows = [row for row in rows if row.status == AccountStatus.ACTIVE]
        skipped_not_active = [row.id for row in rows if row.status != AccountStatus.ACTIVE]

    not_found = [item for item in normalized_ids if item not in existing_ids]
    queued = 0
    skipped_recent = 0

    async with SessionLocal() as session:
        for account in active_rows:
            result = await _enqueue_warmup_task(session, account)
            if result.get("status") == "queued":
                queued += 1
            elif result.get("status") == "skipped":
                skipped_recent += 1

    return {
        "status": "success",
        "queued": queued,
        "skipped_recent": skipped_recent,
        "skipped_not_active": skipped_not_active,
        "not_found": not_found,
    }


@app.post("/api/proxies/bulk_delete")
async def bulk_delete_proxies(payload: BulkDeleteIn) -> dict[str, Any]:
    normalized_ids = _unique_positive_ids(payload.ids)
    if not normalized_ids:
        raise HTTPException(status_code=400, detail="Список ids пуст")

    await _ensure_tables()
    async with SessionLocal() as session:
        existing_ids = (
            (
                await session.execute(
                    select(Proxy.id).where(Proxy.id.in_(normalized_ids))
                )
            )
            .scalars()
            .all()
        )
        existing_id_set = set(existing_ids)
        if existing_id_set:
            await session.execute(
                delete(Proxy).where(Proxy.id.in_(list(existing_id_set)))
            )
            await session.commit()

    not_found = [item for item in normalized_ids if item not in existing_id_set]
    return {
        "status": "success",
        "deleted": len(existing_id_set),
        "not_found": not_found,
    }


@app.post("/api/accounts/{account_id}/ban")
async def mark_account_banned(account_id: int) -> dict[str, Any]:
    async with SessionLocal() as session:
        account = await session.scalar(select(Account).where(Account.id == account_id))
        if not account:
            raise HTTPException(status_code=404, detail="Аккаунт не найден")

        account.status = AccountStatus.BANNED
        account.shadow_ban_started_at = None
        account.shadow_ban_until = None
        session.add(account)
        await session.commit()
    return {
        "status": "success",
        "message": "Аккаунт помечен как заблокированный (Banned)",
    }


@app.post("/api/accounts/{account_id}/shadow_ban")
async def mark_account_shadow_banned(account_id: int) -> dict[str, Any]:
    now = _utc_now()
    ban_until = now + timedelta(hours=SHADOW_BAN_HOURS)
    async with SessionLocal() as session:
        account = await session.scalar(select(Account).where(Account.id == account_id))
        if not account:
            raise HTTPException(status_code=404, detail="Аккаунт не найден")

        account.status = AccountStatus.SHADOW_BANNED
        account.shadow_ban_started_at = now
        account.shadow_ban_until = ban_until
        session.add(account)
        await session.commit()
    return {
        "status": "success",
        "message": f"Аккаунт помечен как теневой бан на {SHADOW_BAN_HOURS}ч",
        "shadow_ban_until": ban_until.isoformat(),
    }


@app.post("/api/accounts/{account_id}/shadow_unban")
async def mark_account_shadow_unbanned(account_id: int) -> dict[str, Any]:
    async with SessionLocal() as session:
        account = await session.scalar(select(Account).where(Account.id == account_id))
        if not account:
            raise HTTPException(status_code=404, detail="Аккаунт не найден")

        account.status = AccountStatus.ACTIVE
        account.shadow_ban_started_at = None
        account.shadow_ban_until = None
        session.add(account)
        await session.commit()
    return {
        "status": "success",
        "message": "Теневой бан снят вручную",
    }


@app.delete("/api/accounts/{account_id}")
async def delete_account(account_id: int) -> dict[str, Any]:
    from sqlalchemy import delete

    async with SessionLocal() as session:
        account = await session.scalar(select(Account).where(Account.id == account_id))
        if not account:
            raise HTTPException(status_code=404, detail="Аккаунт не найден")
        await session.execute(delete(Account).where(Account.id == account_id))
        await session.commit()
    return {"status": "success", "message": f"Аккаунт #{account_id} удален"}


@app.post("/api/accounts/{account_id}/check_login")
async def check_account_login(account_id: int) -> dict[str, Any]:
    async with SessionLocal() as session:
        account = await session.scalar(select(Account).where(Account.id == account_id))
        if not account:
            raise HTTPException(status_code=404, detail="Аккаунт не найден")
        if account.status != AccountStatus.ACTIVE:
            raise HTTPException(
                status_code=400,
                detail=f"Аккаунт имеет статус {account.status.value} и не может участвовать в проверке входа.",
            )

        task = Task(
            account_id=account.id,
            action_type=TaskActionType.CHECK_LOGIN,
            target_url="https://www.facebook.com/",
            payload_text=None,
            target_gender="ANY",
            status=TaskStatus.PENDING,
        )
        session.add(task)
        await session.commit()
    return {"status": "success", "message": "Задача на проверку входа добавлена"}


async def _warmup_account_impl(account_id: int) -> dict[str, Any]:
    await _ensure_tables()
    async with SessionLocal() as session:
        account = await session.scalar(
            select(Account)
            .options(selectinload(Account.proxy))
            .where(Account.id == account_id)
        )
        if not account:
            raise HTTPException(status_code=404, detail="Account not found")
        return await _enqueue_warmup_task(session, account)


@app.post("/api/accounts/{account_id}/warmup")
async def warmup_account(account_id: int) -> dict[str, Any]:
    return await _warmup_account_impl(account_id)


@app.post("/accounts/{account_id}/warmup")
async def warmup_account_compat(account_id: int) -> dict[str, Any]:
    return await _warmup_account_impl(account_id)


@app.get("/api/accounts/{account_id}/warmup/logs", response_model=list[WarmupLogOut])
async def get_warmup_logs(account_id: int, limit: int = 20) -> list[WarmupLogOut]:
    """Returns latest warmup logs for account ordered by start time descending."""
    bounded_limit = min(max(limit, 1), 100)
    async with SessionLocal() as session:
        account = await session.scalar(select(Account).where(Account.id == account_id))
        if not account:
            raise HTTPException(status_code=404, detail="Аккаунт не найден")
        rows = (
            (
                await session.execute(
                    select(WarmupLog)
                    .where(WarmupLog.account_id == account_id)
                    .order_by(WarmupLog.started_at.desc())
                    .limit(bounded_limit)
                )
            )
            .scalars()
            .all()
        )
    return [WarmupLogOut.model_validate(row) for row in rows]


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "index.html", {})


@app.get("/api/accounts", response_model=list[AccountOut])
async def get_accounts() -> list[AccountOut]:
    await _ensure_tables()
    today = date.today()
    async with SessionLocal() as session:
        released = await _release_expired_shadow_bans(session)
        if released:
            await session.commit()
        rows = (
            (await session.execute(select(Account).order_by(Account.id.asc())))
            .scalars()
            .all()
        )
    result: list[AccountOut] = []
    for row in rows:
        status_value = row.status.value
        if row.status == AccountStatus.ACTIVE and _is_account_daily_limited(
            row, today=today
        ):
            status_value = "limit"
        result.append(
            AccountOut.model_validate(
                {
                    "id": row.id,
                    "login": row.login,
                    "status": status_value,
                    "proxy_id": row.proxy_id,
                    "gender": row.gender,
                    "daily_actions_count": row.daily_actions_count,
                    "last_action_date": row.last_action_date,
                    "shadow_ban_started_at": row.shadow_ban_started_at,
                    "shadow_ban_until": row.shadow_ban_until,
                    "warmed_up_at": row.warmed_up_at,
                    "email_login": row.email_login,
                    "email_password": row.email_password,
                    "last_checkpoint_type": row.last_checkpoint_type,
                    "proxy_type": row.proxy_type,
                    "proxy_rotation_url": row.proxy_rotation_url,
                }
            )
        )
    return result


@app.get("/api/tasks", response_model=list[TaskOut])
async def get_tasks() -> list[TaskOut]:
    await _ensure_tables()
    async with SessionLocal() as session:
        stmt = select(Task).options(selectinload(Task.logs)).order_by(Task.id.desc())
        rows = (await session.execute(stmt)).scalars().all()
    return [TaskOut.model_validate(row) for row in rows]


@app.get("/api/balance")
async def get_balance() -> dict[str, Any]:
    panel = PanelAPI()
    try:
        return await panel.get_balance()
    finally:
        await panel.aclose()


@app.post("/api/tasks", response_model=TaskOut, status_code=201)
async def create_task(payload: TaskCreate) -> TaskOut | list[TaskOut]:
    await _ensure_tables()
    provider_actions = {
        TaskActionType.LIKE_POST,
        TaskActionType.FOLLOW,
        TaskActionType.LIKE_COMMENT,
    }
    target_gender = _normalize_gender(payload.target_gender)
    target_author_id = _normalize_target_author_id(payload.target_author_id)
    target_author_name = _normalize_target_author_name(payload.target_author_name)
    if payload.action_type != TaskActionType.REPLY_COMMENT:
        target_author_id = None
        target_author_name = None

    tasks_to_return = []

    async with SessionLocal() as session:
        if payload.action_type in provider_actions:
            encoded_payload = str(payload.quantity)
            if payload.payload_text:
                encoded_payload += f"|{payload.payload_text}"

            task = Task(
                account_id=None,
                action_type=payload.action_type,
                target_url=payload.url,
                payload_text=encoded_payload,
                target_gender=target_gender,
                status=TaskStatus.PENDING,
            )
            session.add(task)
            await session.commit()
            await session.refresh(task)
            tasks_to_return.append(task)
            asyncio.create_task(process_provider_task(task.id))
        else:
            # Browser task: N tasks
            comments_list = []
            if payload.payload_text and payload.action_type in {
                TaskActionType.COMMENT_POST,
                TaskActionType.REPLY_COMMENT,
            }:
                # Разделяем по переносам строк, игнорируем пустые
                comments_list = [
                    line.strip()
                    for line in payload.payload_text.split("\n")
                    if line.strip()
                ]

            for i in range(payload.quantity):
                if comments_list:
                    current_text = comments_list[i % len(comments_list)]
                else:
                    current_text = payload.payload_text

                # Если создаем много задач, лучше использовать авто-подбор для каждой,
                # чтобы они не улетели с одного аккаунта.
                # Если же задача одна, используем выбранный вручную (если есть).
                assigned_account_id = (
                    payload.account_id if payload.quantity == 1 else None
                )
                manual_account = None
                if assigned_account_id is not None:
                    manual_account = await session.scalar(
                        select(Account).where(Account.id == assigned_account_id)
                    )
                    if manual_account is None:
                        raise HTTPException(
                            status_code=404, detail="Аккаунт не найден"
                        )
                    if manual_account.status != AccountStatus.ACTIVE:
                        raise HTTPException(
                            status_code=400,
                            detail=f"Аккаунт имеет статус {manual_account.status.value} и не может участвовать в задаче.",
                        )

                if (
                    payload.action_type == TaskActionType.REPLY_COMMENT
                    and assigned_account_id is not None
                    and target_author_id is not None
                ):
                    if (
                        manual_account is not None
                        and manual_account.login.strip().lower()
                        == target_author_id.lower()
                    ):
                        raise HTTPException(
                            status_code=400,
                            detail="Нельзя назначить аккаунт-автора комментария на reply.",
                        )

                task = Task(
                    account_id=assigned_account_id,
                    action_type=payload.action_type,
                    target_url=payload.url,
                    payload_text=current_text,
                    target_author_id=target_author_id,
                    target_author_name=target_author_name,
                    target_gender=target_gender,
                    status=TaskStatus.PENDING,
                )
                session.add(task)
            await session.commit()

            stmt = (
                select(Task)
                .options(selectinload(Task.logs))
                .order_by(Task.id.desc())
                .limit(payload.quantity)
            )
            created_tasks = (await session.execute(stmt)).scalars().all()
            tasks_to_return.extend(reversed(created_tasks))

    return TaskOut.model_validate(tasks_to_return[0])


@app.post("/api/proxies/{proxy_id}/toggle")
async def toggle_proxy(proxy_id: int) -> dict[str, Any]:
    async with SessionLocal() as session:
        proxy = await session.scalar(select(Proxy).where(Proxy.id == proxy_id))
        if not proxy:
            raise HTTPException(status_code=404, detail="Прокси не найден")
        proxy.is_active = not proxy.is_active
        await session.commit()
        return {"status": "success", "is_active": proxy.is_active}


@app.delete("/api/proxies/{proxy_id}")
async def delete_proxy(proxy_id: int) -> dict[str, Any]:
    from sqlalchemy import delete

    await _ensure_tables()
    async with SessionLocal() as session:
        proxy = await session.scalar(select(Proxy).where(Proxy.id == proxy_id))
        if not proxy:
            raise HTTPException(status_code=404, detail="Прокси не найден")

        await session.execute(delete(Proxy).where(Proxy.id == proxy_id))
        await session.commit()

    return {"status": "success", "message": "Прокси удален"}


@app.post("/api/accounts/import")
async def import_accounts(payload: AccountImportIn) -> dict[str, Any]:
    """Imports accounts from textarea lines with per-line parse results."""
    from import_data import DEFAULT_USER_AGENT, detect_and_parse_line

    ua_fallback = os.getenv("FB_USER_AGENT", DEFAULT_USER_AGENT)
    rows = payload.raw_data.splitlines()
    line_results: list[dict[str, Any]] = []
    imported = 0
    failed = 0

    async with SessionLocal() as session:
        for index, raw_line in enumerate(rows, start=1):
            line = raw_line.strip()
            if not line:
                continue

            parsed = detect_and_parse_line(line)
            if parsed is None:
                failed += 1
                line_results.append(
                    {"line": index, "status": "failed", "reason": "parse_error"}
                )
                continue

            try:
                await upsert_account(
                    session=session,
                    login=parsed.facebook_login,
                    password=parsed.facebook_password,
                    user_agent=ua_fallback,
                    gender="ANY",
                    cookies=None,
                    email_login=parsed.email_login,
                    email_password=parsed.email_password,
                    imap_server=parsed.imap_server,
                    proxy_type=parsed.proxy_type,
                    proxy_rotation_url=parsed.proxy_rotation_url,
                )
                await session.commit()
                imported += 1
                line_results.append(
                    {
                        "line": index,
                        "status": "ok",
                        "login": parsed.facebook_login,
                    }
                )
            except Exception as exc:
                await session.rollback()
                failed += 1
                line_results.append(
                    {
                        "line": index,
                        "status": "failed",
                        "reason": str(exc),
                    }
                )

    return {"imported": imported, "failed": failed, "lines": line_results}


@app.post("/api/accounts/upload")
async def upload_accounts(files: list[UploadFile] = File(...)) -> dict[str, Any]:
    from import_data import (
        parse_account_text,
        DEFAULT_USER_AGENT,
        DEFAULT_PASSWORD_PLACEHOLDER,
    )

    ua_fallback = os.getenv("FB_USER_AGENT", DEFAULT_USER_AGENT)
    password_placeholder = os.getenv(
        "ACCOUNT_PASSWORD_PLACEHOLDER", DEFAULT_PASSWORD_PLACEHOLDER
    )

    imported = 0
    skipped = 0

    async with SessionLocal() as session:
        for file in files:
            content_bytes = await file.read()
            content = content_bytes.decode("utf-8", errors="ignore")

            parsed = parse_account_text(
                content=content,
                source_name=file.filename or "uploaded.txt",
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
                email_login=parsed.email_login,
                email_password=parsed.email_password,
                imap_server=parsed.imap_server,
            )

            imported += 1
            # Сохраняем после каждого аккаунта, чтобы прокси помечался как занятый для следующего в цикле
            await session.commit()

    return {"status": "success", "imported": imported, "skipped": skipped}


@app.post("/api/parse_comments")
async def parse_comments(payload: ParseCommentsIn) -> dict[str, Any]:
    apify: ApifyAPI | None = None
    try:
        apify = ApifyAPI(results_limit=payload.limit)
        comments, debug = await apify.run_facebook_comments_scraper(payload.url)
        return {"status": "success", "comments": comments, "debug": debug}
    except ApifyAPIError as exc:
        return {
            "status": "error",
            "warning": "provider_error",
            "message": str(exc),
            "comments": [],
            "debug": exc.debug,
        }
    except Exception as exc:
        return {
            "status": "error",
            "warning": "provider_error",
            "message": f"Apify request failed: {exc}",
            "comments": [],
            "debug": [],
        }
    finally:
        if apify is not None:
            await apify.aclose()

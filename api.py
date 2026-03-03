from __future__ import annotations

import asyncio
import logging
import os
import random
import secrets
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request, status, UploadFile, File
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field
from dotenv import load_dotenv
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from apify_api import ApifyAPI, ApifyAPIError
from mtp_api import MtpAPI
from models import Account, AccountStatus, Base, Task, TaskActionType, TaskStatus, Proxy
from worker import AccountCaptchaError, AccountSessionData, FacebookBrowser, ProxyConfig
from crud import get_available_proxy_id, upsert_account

load_dotenv()

LOGGER = logging.getLogger("api")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./smm_panel_demo.db")
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

engine = create_async_engine(DATABASE_URL, echo=False)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
ALLOWED_GENDERS = {"M", "F", "ANY"}
DAILY_ACTION_LIMIT = 15
security = HTTPBasic()


class TaskCreate(BaseModel):
    url: str
    action_type: TaskActionType
    payload_text: str | None = None
    quantity: int = Field(default=1, ge=1, le=100_000)
    target_gender: Literal["M", "F", "ANY"] = "ANY"
    account_id: int | None = None


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


def _parse_bool(value: str | None, *, default: bool = True) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _get_service_id(action: TaskActionType) -> int:
    if action == TaskActionType.LIKE_POST:
        return int(os.getenv("MORETHAN_LIKE_ID", "0"))
    if action == TaskActionType.FOLLOW:
        return int(os.getenv("MORETHAN_FOLLOW_ID", "0"))
    if action == TaskActionType.LIKE_COMMENT:
        return int(os.getenv("MORETHAN_LIKE_COMMENT_ID", "0"))
    raise RuntimeError(f"Для action_type={action.value} нет внешнего service ID.")


def _normalize_gender(value: str | None) -> str:
    candidate = (value or "ANY").strip().upper()
    if candidate not in ALLOWED_GENDERS:
        raise ValueError(f"Некорректный gender: {value}")
    return candidate


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


async def _ensure_tables() -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        await connection.run_sync(_migrate_schema_if_needed)


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
    add_column_if_missing("proxies", "name", "TEXT")
    add_column_if_missing("accounts", "gender", "TEXT DEFAULT 'ANY'")
    add_column_if_missing("accounts", "daily_actions_count", "INTEGER DEFAULT 0")
    add_column_if_missing("accounts", "last_action_date", "DATE")
    add_column_if_missing("tasks", "external_order_id", "INTEGER")
    add_column_if_missing("tasks", "target_gender", "TEXT DEFAULT 'ANY'")

    # Приведение данных к правильным значениям Enum (имена членов - верхний регистр)
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


@asynccontextmanager
async def lifespan(_: FastAPI):
    await _ensure_tables()
    worker_task = asyncio.create_task(browser_worker_loop())
    yield
    worker_task.cancel()


app = FastAPI(
    title="SMM Hybrid Panel", lifespan=lifespan, dependencies=[Depends(_verify_admin)]
)


async def _get_active_account(session: Any, target_url: str, target_gender: str = "ANY") -> Account:
    normalized_target_gender = _normalize_gender(target_gender)
    today = date.today()

    # 1. Находим ID аккаунтов, которые сейчас заняты любыми браузерными задачами
    busy_accounts_stmt = select(Task.account_id).where(
        Task.status == TaskStatus.IN_PROGRESS, Task.account_id.is_not(None)
    )
    busy_account_ids = (await session.execute(busy_accounts_stmt)).scalars().all()

    # 2. Исключаем тех, кто УЖЕ назначен на задачи по этому же URL (PENDING или SUCCESS)
    already_assigned_stmt = select(Task.account_id).where(
        Task.target_url == target_url,
        Task.status.in_([TaskStatus.PENDING, TaskStatus.IN_PROGRESS, TaskStatus.SUCCESS]),
        Task.account_id.is_not(None)
    )
    already_assigned_ids = (await session.execute(already_assigned_stmt)).scalars().all()

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

    # 4. Приоритет LRU
    stmt = stmt.order_by(
        Account.last_action_date.asc(),
        Account.daily_actions_count.asc(),
        Account.id.asc(),
    ).limit(1)

    account = await session.scalar(stmt)
    if account is None:
        raise RuntimeError("Не найден свободный аккаунт, который еще не выполнял действий по этой ссылке.")
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
    session: Any, account: Account, *, reason: str | Exception
) -> None:
    account.status = AccountStatus.CAPTCHA_BLOCKED
    try:
        await _rotate_account_proxy(session, account)
    except Exception as e:
        LOGGER.error("Не удалось сменить прокси при блокировке: %s", e)

    session.add(account)
    await session.commit()
    LOGGER.warning("Account %s blocked: %s", account.login, reason)


def _mark_account_action_success(account: Account) -> None:
    today = date.today()
    if account.last_action_date == today:
        account.daily_actions_count += 1
    else:
        account.last_action_date = today
        account.daily_actions_count = 1


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
        mtp = MtpAPI()
        try:
            kwargs = {}
            if username:
                kwargs["username"] = username
            order_id = await mtp.add_order(
                service_id=service_id, link=task.target_url, quantity=quantity, **kwargs
            )
        finally:
            await mtp.aclose()
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


ACCOUNT_SELECTION_LOCK = asyncio.Lock()


async def _process_browser_task(session: Any, task: Task) -> bool:
    """Returns True if task finished (success/error/stopped), False if needs immediate retry."""
    await session.refresh(task)
    if task.status == TaskStatus.STOPPED:
        return True

    if not task.account_id:
        async with ACCOUNT_SELECTION_LOCK:
            try:
                # Повторно проверяем статус задачи внутри лока
                await session.refresh(task)
                if task.status == TaskStatus.STOPPED:
                    return True

                account = await _get_active_account(
                    session, task.target_url, task.target_gender
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

    session_data = AccountSessionData(
        login=account.login,
        password=account.password,
        user_agent=account.user_agent,
        cookies=account.cookies,
        proxy=_proxy_from_account(account),
    )
    headless = _parse_bool(os.getenv("FB_HEADLESS"), default=True)

    try:
        await _add_task_log(session, task.id, f"Используем аккаунт {account.login}")
        
        async def worker_log(msg: str):
            await _add_task_log(session, task.id, msg)

        async with FacebookBrowser(
            account=session_data, headless=headless, strict_cookie_session=False,
            log_callback=worker_log
        ) as browser:
            cookies = await browser.login()
            account.cookies = cookies
            await session.commit()
            await _add_task_log(session, task.id, "Вход выполнен.")

            await session.refresh(task)
            if task.status == TaskStatus.STOPPED:
                await _add_task_log(
                    session, task.id, "Задача остановлена пользователем."
                )
                return True

            if task.action_type == TaskActionType.CHECK_LOGIN:
                ok = True
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
                        await _add_task_log(session, task.id, "Ошибка: Кнопка лайка не найдена.")
                elif task.action_type == TaskActionType.REPLY_COMMENT:
                    ok = await browser.reply_comment(
                        task.target_url, task.payload_text or ""
                    )
                    if not ok:
                        await _add_task_log(session, task.id, "Ошибка: Не удалось отправить ответ.")
                else:
                    ok = await browser.leave_comment(
                        task.target_url, task.payload_text or "Test"
                    )
                    if not ok:
                        await _add_task_log(session, task.id, "Ошибка: Не удалось оставить комментарий.")

        await session.refresh(task)
        if task.status == TaskStatus.STOPPED:
            return True

        task.status = TaskStatus.SUCCESS if ok else TaskStatus.ERROR
        await _add_task_log(
            session, task.id, f"Завершено: {'Успех' if ok else 'Ошибка'}"
        )
        if ok:
            _mark_account_action_success(account)
        return True

    except AccountCaptchaError as exc:
        await _block_account_due_to_captcha(session, account, reason=str(exc))
        task.account_id = None  # Освобождаем задачу для другого аккаунта
        await _add_task_log(
            session, task.id, f"АККАУНТ {account.login} ВЫЛЕТЕЛ: {exc}. Ищу замену..."
        )
        return False  # Сигнал воркеру: попробовать другой аккаунт немедленно
    except Exception as exc:
        await _add_task_log(session, task.id, f"Критическая ошибка браузера: {exc}")
        task.status = TaskStatus.ERROR
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
                    delay = random.uniform(60, 150)  # Уменьшил паузу, раз мы в параллели
                    LOGGER.info("Задача %s завершена. Пауза потока %.1f сек...", task_id, delay)
                    await asyncio.sleep(delay)
        except Exception:
            LOGGER.exception("Ошибка в потоке задачи %s", task_id)


async def browser_worker_loop() -> None:
    LOGGER.info("Запущен параллельный воркер TURKISH PANEL (Лимит: %s)", MAX_CONCURRENT_BROWSER_TASKS)
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_BROWSER_TASKS)
    active_tasks = set()

    while True:
        try:
            # Очищаем завершенные задачи из списка активных
            active_tasks = {t for t in active_tasks if not t.done()}

            if len(active_tasks) < MAX_CONCURRENT_BROWSER_TASKS:
                async with SessionLocal() as session:
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
                            
                            t = asyncio.create_task(_run_browser_task_wrapper(tid, semaphore))
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


@app.post("/api/accounts/{account_id}/ban")
async def mark_account_banned(account_id: int) -> dict[str, Any]:
    async with SessionLocal() as session:
        account = await session.scalar(select(Account).where(Account.id == account_id))
        if not account:
            raise HTTPException(status_code=404, detail="Аккаунт не найден")

        account.status = AccountStatus.BANNED
        session.add(account)
        await session.commit()
    return {
        "status": "success",
        "message": "Аккаунт помечен как заблокированный (Banned)",
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


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "index.html", {})


@app.get("/api/accounts", response_model=list[AccountOut])
async def get_accounts() -> list[AccountOut]:
    await _ensure_tables()
    today = date.today()
    async with SessionLocal() as session:
        rows = (
            (await session.execute(select(Account).order_by(Account.id.asc())))
            .scalars()
            .all()
        )
    result: list[AccountOut] = []
    for row in rows:
        status_value = (
            "limit" if _is_account_daily_limited(row, today=today) else row.status.value
        )
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
    mtp = MtpAPI()
    try:
        return await mtp.get_balance()
    finally:
        await mtp.aclose()


@app.post("/api/tasks", response_model=TaskOut, status_code=201)
async def create_task(payload: TaskCreate) -> TaskOut | list[TaskOut]:
    await _ensure_tables()
    provider_actions = {
        TaskActionType.LIKE_POST,
        TaskActionType.FOLLOW,
        TaskActionType.LIKE_COMMENT,
    }
    target_gender = _normalize_gender(payload.target_gender)

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
                assigned_account_id = payload.account_id if payload.quantity == 1 else None

                task = Task(
                    account_id=assigned_account_id,
                    action_type=payload.action_type,
                    target_url=payload.url,
                    payload_text=current_text,
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

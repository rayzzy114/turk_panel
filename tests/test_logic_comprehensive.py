from __future__ import annotations
import asyncio
from datetime import timedelta
import pytest
import httpx
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from models import (
    Base,
    Account,
    AccountStatus,
    Log,
    Proxy,
    Task,
    TaskStatus,
    TaskActionType,
)
from worker import AccountCaptchaError, AccountInvalidCredentialsError
import api as api_module
import importlib


import pytest_asyncio


def _auth_headers() -> dict[str, str]:
    return {"Authorization": "Basic YWRtaW46YWRtaW4="}  # admin:admin


@pytest_asyncio.fixture
async def setup_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    database_url = f"sqlite+aiosqlite:///{db_file}"
    monkeypatch.setenv("DATABASE_URL", database_url)

    engine = create_async_engine(database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    yield session_factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_ensure_tables_runs_migrations_once_for_concurrent_calls(
    tmp_path, monkeypatch
):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'ensure_once.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)

    importlib.reload(api_module)
    migration_calls = 0

    def fake_migrate(_: object) -> None:
        nonlocal migration_calls
        migration_calls += 1

    monkeypatch.setattr(api_module, "_migrate_schema_if_needed", fake_migrate)

    await asyncio.gather(api_module._ensure_tables(), api_module._ensure_tables())
    await api_module._ensure_tables()

    assert migration_calls == 1

    await api_module.engine.dispose()


@pytest.mark.asyncio
async def test_ensure_tables_skips_migrations_when_schema_is_current(
    tmp_path, monkeypatch
):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'ensure_current.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)

    engine = create_async_engine(database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()

    importlib.reload(api_module)
    migration_calls = 0

    def fake_migrate(_: object) -> None:
        nonlocal migration_calls
        migration_calls += 1

    monkeypatch.setattr(api_module, "_migrate_schema_if_needed", fake_migrate)

    await api_module._ensure_tables()

    assert migration_calls == 0

    await api_module.engine.dispose()


@pytest.mark.asyncio
async def test_account_daily_limit_selection(setup_db):
    """Test that _get_active_account respects DAILY_ACTION_LIMIT."""
    async_session = setup_db
    async with async_session() as session:
        # Account 1: At limit today
        acc1 = Account(
            login="limited",
            password="p",
            status=AccountStatus.ACTIVE,
            daily_actions_count=15,  # LIMIT is 15
            last_action_date=api_module.date.today(),
            user_agent="ua",
        )
        # Account 2: Not at limit
        acc2 = Account(
            login="free",
            password="p",
            status=AccountStatus.ACTIVE,
            daily_actions_count=5,
            last_action_date=api_module.date.today(),
            user_agent="ua",
        )
        session.add_all([acc1, acc2])
        await session.commit()

        # Selection should pick acc2
        selected = await api_module._get_active_account(session, "http://test.com")
        assert selected.login == "free"

        # If we update acc2 to be limited too
        acc2.daily_actions_count = 15
        session.add(acc2)
        await session.commit()

        with pytest.raises(
            RuntimeError,
            match="Не найден свободный аккаунт, который еще не выполнял действий по этой ссылке",
        ):
            await api_module._get_active_account(session, "http://test.com")


@pytest.mark.asyncio
async def test_gender_filtering_selection(setup_db):
    """Test that _get_active_account respects gender targeting."""
    async_session = setup_db
    async with async_session() as session:
        m = Account(
            login="male",
            password="p",
            status=AccountStatus.ACTIVE,
            gender="M",
            user_agent="ua",
        )
        f = Account(
            login="female",
            password="p",
            status=AccountStatus.ACTIVE,
            gender="F",
            user_agent="ua",
        )
        session.add_all([m, f])
        await session.commit()

        sel_m = await api_module._get_active_account(
            session, target_url="http://fb.com", target_gender="M"
        )
        assert sel_m.login == "male"

        sel_f = await api_module._get_active_account(
            session, target_url="http://fb.com", target_gender="F"
        )
        assert sel_f.login == "female"

        sel_any = await api_module._get_active_account(
            session, target_url="http://fb.com", target_gender="ANY"
        )
        assert sel_any.login in ["male", "female"]


@pytest.mark.asyncio
async def test_create_multiple_tasks(setup_db):
    """Test that quantity > 1 creates multiple browser tasks."""
    importlib.reload(api_module)
    transport = httpx.ASGITransport(app=api_module.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        # Create 3 comment tasks
        resp = await client.post(
            "/api/tasks",
            json={
                "url": "https://fb.com/1",
                "action_type": "comment_post",
                "payload_text": "Comment A\nComment B",
                "quantity": 3,
            },
            headers=_auth_headers(),
        )
        assert resp.status_code == 201

        # Verify 3 tasks exist in DB
        tasks_resp = await client.get("/api/tasks", headers=_auth_headers())
        tasks = tasks_resp.json()
        assert len(tasks) == 3
        # Check round-robin payload
        payloads = [t["payload_text"] for t in tasks]
        # order is desc by ID, so [T3, T2, T1]
        # T1: Comment A, T2: Comment B, T3: Comment A
        assert "Comment A" in payloads
        assert "Comment B" in payloads
        assert payloads.count("Comment A") == 2


@pytest.mark.asyncio
async def test_upload_accounts_updates_existing(setup_db):
    """Test that uploading account with existing login updates it."""
    async_session = setup_db
    async with async_session() as session:
        acc = Account(
            login="user1", password="old", status=AccountStatus.BANNED, user_agent="ua"
        )
        session.add(acc)
        await session.commit()

    importlib.reload(api_module)
    transport = httpx.ASGITransport(app=api_module.app)

    files = [("files", ("acc.txt", b"user1:new_pass", "text/plain"))]
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        await client.post("/api/accounts/upload", files=files, headers=_auth_headers())

        acc_resp = await client.get("/api/accounts", headers=_auth_headers())
        accounts = acc_resp.json()
        assert len(accounts) == 1
        assert accounts[0]["login"] == "user1"
        # We can't check password via API but we can check status was reset to active
        assert accounts[0]["status"] == "active"


@pytest.mark.asyncio
async def test_upload_accounts_saves_email_credentials(setup_db):
    importlib.reload(api_module)
    transport = httpx.ASGITransport(app=api_module.app)

    files = [
        (
            "files",
            (
                "acc_with_mail.txt",
                b"61580000000999:fb_pass:mail@example.com:mail-pass-777",
                "text/plain",
            ),
        )
    ]
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        upload_resp = await client.post(
            "/api/accounts/upload", files=files, headers=_auth_headers()
        )
        assert upload_resp.status_code == 200

        acc_resp = await client.get("/api/accounts", headers=_auth_headers())
        assert acc_resp.status_code == 200
        account = next(
            item for item in acc_resp.json() if item["login"] == "61580000000999"
        )
        assert account["email_login"] == "mail@example.com"
        assert account["email_password"] == "mail-pass-777"


@pytest.mark.asyncio
async def test_delete_proxy_unlinks_from_account(setup_db):
    """Test that deleting a proxy sets account.proxy_id to null."""
    async_session = setup_db
    async with async_session() as session:
        p = Proxy(host="1.1.1.1", port=80, is_active=True)
        session.add(p)
        await session.commit()
        await session.refresh(p)
        p_id = p.id

        acc = Account(login="user1", password="p", proxy_id=p_id, user_agent="ua")
        session.add(acc)
        await session.commit()

    importlib.reload(api_module)
    transport = httpx.ASGITransport(app=api_module.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        # Delete proxy
        resp = await client.delete(f"/api/proxies/{p_id}", headers=_auth_headers())
        assert resp.status_code == 200

        # Check account
        acc_resp = await client.get("/api/accounts", headers=_auth_headers())
        accounts = acc_resp.json()
        assert accounts[0]["proxy_id"] is None


@pytest.mark.asyncio
async def test_import_proxies_skips_duplicates(setup_db):
    """Test that importing the same proxy twice doesn't create a duplicate."""
    importlib.reload(api_module)
    transport = httpx.ASGITransport(app=api_module.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        raw_data = "1.1.1.1:80:u:p\n1.1.1.1:80:u:p\nNewName|1.1.1.1:80:u:p"

        # First import
        resp = await client.post(
            "/api/proxies/import", json={"raw_data": raw_data}, headers=_auth_headers()
        )
        assert resp.status_code == 200
        # It should import only 1, because 2nd and 3rd are duplicates of 1st
        assert resp.json()["imported"] == 1

        # Verify only 1 proxy in DB
        proxies_resp = await client.get("/api/proxies", headers=_auth_headers())
        proxies = proxies_resp.json()
        assert len(proxies) == 1
        assert (
            proxies[0]["name"] == "NewName"
        )  # Name should be updated from the last duplicate


@pytest.mark.asyncio
async def test_stop_task_logic(setup_db):
    """Test that a task can be stopped and worker respects the signal."""
    async_session = setup_db

    # 1. Create a task
    async with async_session() as session:
        task = Task(
            action_type=TaskActionType.COMMENT_POST,
            target_url="http://fb.com",
            status=TaskStatus.PENDING,
            target_gender="ANY",
        )
        session.add(task)
        await session.commit()
        await session.refresh(task)
        task_id = task.id

    importlib.reload(api_module)
    transport = httpx.ASGITransport(app=api_module.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        # 2. Stop the task via API
        resp = await client.post(f"/api/tasks/{task_id}/stop", headers=_auth_headers())
        assert resp.status_code == 200
        assert resp.json()["status"] == "success"

        # 3. Verify status in DB
        tasks_resp = await client.get("/api/tasks", headers=_auth_headers())
        tasks = tasks_resp.json()
        target = next(t for t in tasks if t["id"] == task_id)
        assert target["status"] == "stopped"

    # 4. Verify worker logic (simulated)
    async with async_session() as session:
        task_obj = await session.get(Task, task_id)
        # _process_browser_task returns True (finished) immediately if status is STOPPED
        finished = await api_module._process_browser_task(session, task_obj)
        assert finished is True
        assert task_obj.status == TaskStatus.STOPPED


@pytest.mark.asyncio
async def test_clear_tasks_logic(setup_db):
    """Test that all tasks are deleted when clear_tasks is called."""
    async_session = setup_db

    async with async_session() as session:
        # 1. Create multiple tasks
        t1 = Task(
            action_type=TaskActionType.LIKE_POST,
            target_url="http://fb.com/1",
            status=TaskStatus.SUCCESS,
            target_gender="ANY",
        )
        t2 = Task(
            action_type=TaskActionType.FOLLOW,
            target_url="http://fb.com/2",
            status=TaskStatus.ERROR,
            target_gender="ANY",
        )
        session.add_all([t1, t2])
        await session.commit()

    importlib.reload(api_module)
    transport = httpx.ASGITransport(app=api_module.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        # 2. Verify tasks exist
        resp = await client.get("/api/tasks", headers=_auth_headers())
        assert len(resp.json()) == 2

        # 3. Call clear_tasks
        resp = await client.post("/api/tasks/clear", headers=_auth_headers())
        assert resp.status_code == 200
        assert resp.json()["status"] == "success"

        # 4. Verify tasks are gone
        resp = await client.get("/api/tasks", headers=_auth_headers())
        assert len(resp.json()) == 0


@pytest.mark.asyncio
async def test_block_account_marks_checkpoint_status_for_checkpoint_reason(setup_db):
    async_session = setup_db
    async with async_session() as session:
        acc = Account(
            login="checkpoint_user",
            password="p",
            status=AccountStatus.ACTIVE,
            user_agent="ua",
        )
        session.add(acc)
        await session.commit()

        await api_module._block_account_due_to_captcha(
            session, acc, reason="Checkpoint detected after target navigation"
        )
        await session.refresh(acc)

        assert acc.status == AccountStatus.CHECKPOINT


@pytest.mark.asyncio
async def test_mark_account_invalid_credentials_sets_special_status(setup_db):
    async_session = setup_db
    async with async_session() as session:
        acc = Account(
            login="bad_creds_user",
            password="p",
            status=AccountStatus.ACTIVE,
            user_agent="ua",
        )
        session.add(acc)
        await session.commit()

        await api_module._mark_account_invalid_credentials(
            session,
            acc,
            reason="wrong password",
        )
        await session.refresh(acc)

        assert acc.status == AccountStatus.INVALID_CREDENTIALS
        assert acc.last_checkpoint_type is None


@pytest.mark.asyncio
async def test_process_browser_task_does_not_fail_on_post_action_state_save(
    setup_db, monkeypatch
):
    async_session = setup_db

    async with async_session() as session:
        account = Account(
            login="worker_user",
            password="p",
            status=AccountStatus.ACTIVE,
            user_agent="ua",
        )
        session.add(account)
        await session.commit()
        await session.refresh(account)

        task = Task(
            account_id=account.id,
            action_type=TaskActionType.COMMENT_POST,
            target_url="https://www.facebook.com/share/p/demo",
            payload_text="ok",
            status=TaskStatus.IN_PROGRESS,
            target_gender="ANY",
        )
        session.add(task)
        await session.commit()
        await session.refresh(task)
        task_id = task.id

        class StubBrowser:
            def __init__(
                self,
                account,
                headless,
                strict_cookie_session,
                log_callback,
            ) -> None:
                self._closed = False
                self.log_callback = log_callback

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb) -> None:
                self._closed = True

            async def login(self) -> None:
                await self.log_callback(
                    "Авторизация успешна (storage_state/cookies активны)."
                )

            async def leave_comment(self, target_url: str, text: str) -> bool:
                return True

            async def like_comment(self, target_url: str) -> bool:
                return True

            async def reply_comment(self, target_url: str, text: str) -> bool:
                return True

            async def get_storage_state(self):
                if self._closed:
                    raise RuntimeError(
                        "BrowserContext.storage_state: Target page, context or browser has been closed"
                    )
                return {"cookies": [{"name": "c_user", "value": "1"}]}

        async def _fast_sleep(_: float) -> None:
            return None

        monkeypatch.setattr(api_module, "FacebookBrowser", StubBrowser)
        monkeypatch.setattr(api_module.asyncio, "sleep", _fast_sleep)
        monkeypatch.setattr(api_module.random, "randint", lambda a, b: a)

        task_obj = await session.get(Task, task_id)
        finished = await api_module._process_browser_task(session, task_obj)
        await session.refresh(task_obj)
        log_rows = await session.scalars(select(Log).where(Log.task_id == task_id))
        messages = [row.message for row in log_rows]

        assert finished is True
        assert task_obj.status == TaskStatus.SUCCESS
        assert any("Завершено: Успех" in msg for msg in messages)
        assert not any("Критическая ошибка браузера:" in msg for msg in messages)


@pytest.mark.asyncio
async def test_get_active_account_allows_other_action_type_for_same_url(setup_db):
    async_session = setup_db
    async with async_session() as session:
        acc = Account(
            login="shared_actor",
            password="p",
            status=AccountStatus.ACTIVE,
            user_agent="ua",
        )
        session.add(acc)
        await session.commit()
        await session.refresh(acc)

        done_reply = Task(
            account_id=acc.id,
            action_type=TaskActionType.REPLY_COMMENT,
            target_url="https://fb.com/comment/1",
            payload_text="ok",
            target_gender="ANY",
            status=TaskStatus.SUCCESS,
        )
        session.add(done_reply)
        await session.commit()

        selected = await api_module._get_active_account(
            session,
            target_url="https://fb.com/comment/1",
            target_gender="ANY",
            action_type=TaskActionType.LIKE_COMMENT_BOT,
        )

        assert selected.id == acc.id


@pytest.mark.asyncio
async def test_get_active_account_skips_comment_author_for_reply_comment(setup_db):
    async_session = setup_db
    async with async_session() as session:
        author = Account(
            login="author_42",
            password="p",
            status=AccountStatus.ACTIVE,
            user_agent="ua",
        )
        other = Account(
            login="other_42",
            password="p",
            status=AccountStatus.ACTIVE,
            user_agent="ua",
        )
        session.add_all([author, other])
        await session.commit()

        selected = await api_module._get_active_account(
            session,
            target_url="https://fb.com/comment/2",
            target_gender="ANY",
            action_type=TaskActionType.REPLY_COMMENT,
            target_author_id="author_42",
        )

        assert selected.login == "other_42"


@pytest.mark.asyncio
async def test_bulk_delete_accounts_endpoint_deletes_only_selected(setup_db):
    async_session = setup_db
    async with async_session() as session:
        acc1 = Account(
            login="bulk_user_1",
            password="p",
            status=AccountStatus.ACTIVE,
            user_agent="ua",
        )
        acc2 = Account(
            login="bulk_user_2",
            password="p",
            status=AccountStatus.ACTIVE,
            user_agent="ua",
        )
        acc3 = Account(
            login="bulk_user_3",
            password="p",
            status=AccountStatus.ACTIVE,
            user_agent="ua",
        )
        session.add_all([acc1, acc2, acc3])
        await session.commit()
        await session.refresh(acc1)
        await session.refresh(acc2)
        await session.refresh(acc3)

        selected_ids = [acc1.id, acc3.id, 999999]

    importlib.reload(api_module)
    transport = httpx.ASGITransport(app=api_module.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        resp = await client.post(
            "/api/accounts/bulk_delete",
            json={"ids": selected_ids},
            headers=_auth_headers(),
        )
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["status"] == "success"
        assert payload["deleted"] == 2
        assert 999999 in payload["not_found"]

        accounts_resp = await client.get("/api/accounts", headers=_auth_headers())
        assert accounts_resp.status_code == 200
        remaining_logins = [a["login"] for a in accounts_resp.json()]
        assert remaining_logins == ["bulk_user_2"]


@pytest.mark.asyncio
async def test_bulk_delete_proxies_endpoint_deletes_only_selected(setup_db):
    async_session = setup_db
    async with async_session() as session:
        p1 = Proxy(host="11.11.11.11", port=80, is_active=True)
        p2 = Proxy(host="22.22.22.22", port=80, is_active=True)
        p3 = Proxy(host="33.33.33.33", port=80, is_active=True)
        session.add_all([p1, p2, p3])
        await session.commit()
        await session.refresh(p1)
        await session.refresh(p2)
        await session.refresh(p3)

        acc = Account(
            login="bulk_proxy_holder",
            password="p",
            status=AccountStatus.ACTIVE,
            user_agent="ua",
            proxy_id=p1.id,
        )
        session.add(acc)
        await session.commit()

        selected_ids = [p1.id, p3.id, 777777]

    importlib.reload(api_module)
    transport = httpx.ASGITransport(app=api_module.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        resp = await client.post(
            "/api/proxies/bulk_delete",
            json={"ids": selected_ids},
            headers=_auth_headers(),
        )
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["status"] == "success"
        assert payload["deleted"] == 2
        assert 777777 in payload["not_found"]

        proxies_resp = await client.get("/api/proxies", headers=_auth_headers())
        assert proxies_resp.status_code == 200
        hosts = [p["host"] for p in proxies_resp.json()]
        assert hosts == ["22.22.22.22"]

        accounts_resp = await client.get("/api/accounts", headers=_auth_headers())
        assert accounts_resp.status_code == 200
        assert accounts_resp.json()[0]["proxy_id"] is None


@pytest.mark.asyncio
async def test_create_reply_task_saves_target_author_metadata(setup_db):
    async_session = setup_db
    async with async_session() as session:
        acc = Account(
            login="meta_user",
            password="p",
            status=AccountStatus.ACTIVE,
            user_agent="ua",
        )
        session.add(acc)
        await session.commit()

    importlib.reload(api_module)
    transport = httpx.ASGITransport(app=api_module.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        resp = await client.post(
            "/api/tasks",
            json={
                "url": "https://fb.com/comment/target",
                "action_type": "reply_comment",
                "payload_text": "hello",
                "quantity": 1,
                "target_author_id": "author_99",
                "target_author_name": "Author Name",
            },
            headers=_auth_headers(),
        )
        assert resp.status_code == 201

    async with api_module.SessionLocal() as session:
        task = await session.scalar(
            select(Task).where(Task.target_url == "https://fb.com/comment/target")
        )
        assert task is not None
        assert hasattr(task, "target_author_id")
        assert hasattr(task, "target_author_name")
        assert task.target_author_id == "author_99"
        assert task.target_author_name == "Author Name"


@pytest.mark.asyncio
async def test_bulk_ban_accounts_endpoint_marks_selected(setup_db):
    async_session = setup_db
    async with async_session() as session:
        acc1 = Account(
            login="bulk_ban_1",
            password="p",
            status=AccountStatus.ACTIVE,
            user_agent="ua",
        )
        acc2 = Account(
            login="bulk_ban_2",
            password="p",
            status=AccountStatus.ACTIVE,
            user_agent="ua",
        )
        acc3 = Account(
            login="bulk_ban_3",
            password="p",
            status=AccountStatus.ACTIVE,
            user_agent="ua",
        )
        session.add_all([acc1, acc2, acc3])
        await session.commit()
        await session.refresh(acc1)
        await session.refresh(acc2)
        await session.refresh(acc3)

    importlib.reload(api_module)
    transport = httpx.ASGITransport(app=api_module.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        resp = await client.post(
            "/api/accounts/bulk_ban",
            json={"ids": [acc1.id, acc3.id, 404040]},
            headers=_auth_headers(),
        )
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["status"] == "success"
        assert payload["updated"] == 2
        assert 404040 in payload["not_found"]

        accounts_resp = await client.get("/api/accounts", headers=_auth_headers())
        rows = {item["id"]: item for item in accounts_resp.json()}
        assert rows[acc1.id]["status"] == "banned"
        assert rows[acc3.id]["status"] == "banned"
        assert rows[acc2.id]["status"] == "active"


@pytest.mark.asyncio
async def test_bulk_shadow_ban_and_bulk_shadow_unban_endpoints(setup_db):
    async_session = setup_db
    async with async_session() as session:
        acc1 = Account(
            login="bulk_shadow_1",
            password="p",
            status=AccountStatus.ACTIVE,
            user_agent="ua",
        )
        acc2 = Account(
            login="bulk_shadow_2",
            password="p",
            status=AccountStatus.ACTIVE,
            user_agent="ua",
        )
        session.add_all([acc1, acc2])
        await session.commit()
        await session.refresh(acc1)
        await session.refresh(acc2)

    importlib.reload(api_module)
    transport = httpx.ASGITransport(app=api_module.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        resp = await client.post(
            "/api/accounts/bulk_shadow_ban",
            json={"ids": [acc1.id, acc2.id]},
            headers=_auth_headers(),
        )
        assert resp.status_code == 200
        assert resp.json()["updated"] == 2

        accounts_resp = await client.get("/api/accounts", headers=_auth_headers())
        rows = {item["id"]: item for item in accounts_resp.json()}
        assert rows[acc1.id]["status"] == "shadow_banned"
        assert rows[acc2.id]["status"] == "shadow_banned"
        assert rows[acc1.id]["shadow_ban_until"] is not None

        unban_resp = await client.post(
            "/api/accounts/bulk_shadow_unban",
            json={"ids": [acc2.id]},
            headers=_auth_headers(),
        )
        assert unban_resp.status_code == 200
        assert unban_resp.json()["updated"] == 1

        accounts_resp = await client.get("/api/accounts", headers=_auth_headers())
        rows = {item["id"]: item for item in accounts_resp.json()}
        assert rows[acc1.id]["status"] == "shadow_banned"
        assert rows[acc2.id]["status"] == "active"
        assert rows[acc2.id]["shadow_ban_until"] is None


@pytest.mark.asyncio
async def test_bulk_check_login_accounts_creates_tasks_for_selected_ids(setup_db):
    async_session = setup_db
    async with async_session() as session:
        acc1 = Account(
            login="bulk_check_1",
            password="p",
            status=AccountStatus.ACTIVE,
            user_agent="ua",
        )
        acc2 = Account(
            login="bulk_check_2",
            password="p",
            status=AccountStatus.ACTIVE,
            user_agent="ua",
        )
        session.add_all([acc1, acc2])
        await session.commit()
        await session.refresh(acc1)
        await session.refresh(acc2)

    importlib.reload(api_module)
    transport = httpx.ASGITransport(app=api_module.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        resp = await client.post(
            "/api/accounts/bulk_check_login",
            json={"ids": [acc1.id, 123456]},
            headers=_auth_headers(),
        )
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["status"] == "success"
        assert payload["created_tasks"] == 1
        assert 123456 in payload["not_found"]

        tasks_resp = await client.get("/api/tasks", headers=_auth_headers())
        tasks = tasks_resp.json()
        matching = [
            t
            for t in tasks
            if t["action_type"] == "check_login" and t["account_id"] == acc1.id
        ]
        assert len(matching) == 1


@pytest.mark.asyncio
async def test_bulk_check_login_skips_non_active_accounts(setup_db):
    async_session = setup_db
    async with async_session() as session:
        active_acc = Account(
            login="bulk_check_active",
            password="p",
            status=AccountStatus.ACTIVE,
            user_agent="ua",
        )
        banned_acc = Account(
            login="bulk_check_banned",
            password="p",
            status=AccountStatus.BANNED,
            user_agent="ua",
        )
        session.add_all([active_acc, banned_acc])
        await session.commit()
        await session.refresh(active_acc)
        await session.refresh(banned_acc)

    importlib.reload(api_module)
    transport = httpx.ASGITransport(app=api_module.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        resp = await client.post(
            "/api/accounts/bulk_check_login",
            json={"ids": [active_acc.id, banned_acc.id]},
            headers=_auth_headers(),
        )
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["created_tasks"] == 1
        assert banned_acc.id in payload["skipped_not_active"]


@pytest.mark.asyncio
async def test_check_login_rejects_non_active_account(setup_db):
    async_session = setup_db
    async with async_session() as session:
        banned_acc = Account(
            login="single_check_banned",
            password="p",
            status=AccountStatus.BANNED,
            user_agent="ua",
        )
        session.add(banned_acc)
        await session.commit()
        await session.refresh(banned_acc)

    importlib.reload(api_module)
    transport = httpx.ASGITransport(app=api_module.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        resp = await client.post(
            f"/api/accounts/{banned_acc.id}/check_login",
            headers=_auth_headers(),
        )
        assert resp.status_code == 400
        assert "не может участвовать" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_shadow_ban_and_manual_unban_endpoints(setup_db):
    async_session = setup_db
    async with async_session() as session:
        acc = Account(
            login="shadow_user",
            password="p",
            status=AccountStatus.ACTIVE,
            user_agent="ua",
        )
        session.add(acc)
        await session.commit()
        await session.refresh(acc)
        acc_id = acc.id

    importlib.reload(api_module)
    transport = httpx.ASGITransport(app=api_module.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        ban_resp = await client.post(
            f"/api/accounts/{acc_id}/shadow_ban", headers=_auth_headers()
        )
        assert ban_resp.status_code == 200
        assert ban_resp.json()["status"] == "success"

        accounts_resp = await client.get("/api/accounts", headers=_auth_headers())
        row = next(a for a in accounts_resp.json() if a["id"] == acc_id)
        assert row["status"] == "shadow_banned"
        assert row["shadow_ban_started_at"] is not None
        assert row["shadow_ban_until"] is not None

        unban_resp = await client.post(
            f"/api/accounts/{acc_id}/shadow_unban", headers=_auth_headers()
        )
        assert unban_resp.status_code == 200
        assert unban_resp.json()["status"] == "success"

        accounts_resp = await client.get("/api/accounts", headers=_auth_headers())
        row = next(a for a in accounts_resp.json() if a["id"] == acc_id)
        assert row["status"] == "active"
        assert row["shadow_ban_started_at"] is None
        assert row["shadow_ban_until"] is None


@pytest.mark.asyncio
async def test_get_active_account_skips_shadow_banned_account(setup_db):
    async_session = setup_db
    async with async_session() as session:
        shadow = Account(
            login="shadow_skip",
            password="p",
            status=AccountStatus.SHADOW_BANNED,
            user_agent="ua",
        )
        active = Account(
            login="active_pick",
            password="p",
            status=AccountStatus.ACTIVE,
            user_agent="ua",
        )
        session.add_all([shadow, active])
        await session.commit()

        selected = await api_module._get_active_account(
            session, target_url="https://fb.com/post/1"
        )
        assert selected.login == "active_pick"


@pytest.mark.asyncio
async def test_expired_shadow_ban_auto_releases_on_accounts_endpoint(setup_db):
    async_session = setup_db
    async with async_session() as session:
        acc = Account(
            login="expired_shadow",
            password="p",
            status=AccountStatus.SHADOW_BANNED,
            shadow_ban_started_at=api_module._utc_now() - timedelta(hours=73),
            shadow_ban_until=api_module._utc_now() - timedelta(hours=1),
            user_agent="ua",
        )
        session.add(acc)
        await session.commit()
        await session.refresh(acc)
        acc_id = acc.id

    importlib.reload(api_module)
    transport = httpx.ASGITransport(app=api_module.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        accounts_resp = await client.get("/api/accounts", headers=_auth_headers())
        assert accounts_resp.status_code == 200
        row = next(a for a in accounts_resp.json() if a["id"] == acc_id)
        assert row["status"] == "active"
        assert row["shadow_ban_started_at"] is None
        assert row["shadow_ban_until"] is None


@pytest.mark.asyncio
async def test_process_browser_task_unassigns_non_active_preset_account(
    setup_db, monkeypatch
):
    async_session = setup_db
    async with async_session() as session:
        shadow = Account(
            login="shadow_busy",
            password="p",
            status=AccountStatus.SHADOW_BANNED,
            shadow_ban_started_at=api_module._utc_now(),
            shadow_ban_until=api_module._utc_now() + timedelta(hours=72),
            user_agent="ua",
        )
        session.add(shadow)
        await session.commit()
        await session.refresh(shadow)

        task = Task(
            account_id=shadow.id,
            action_type=TaskActionType.COMMENT_POST,
            target_url="https://fb.com/post/2",
            payload_text="text",
            status=TaskStatus.PENDING,
            target_gender="ANY",
        )
        session.add(task)
        await session.commit()
        await session.refresh(task)
        task_id = task.id

        class _UnexpectedBrowser:
            def __init__(self, *args, **kwargs) -> None:
                raise AssertionError("Browser should not start for shadow_banned account")

        monkeypatch.setattr(api_module, "FacebookBrowser", _UnexpectedBrowser)

        finished = await api_module._process_browser_task(session, task)
        await session.refresh(task)

        logs = await session.scalars(select(Log).where(Log.task_id == task_id))
        messages = [row.message for row in logs]

        assert finished is False
        assert task.account_id is None
        assert any("shadow_banned" in msg for msg in messages)


@pytest.mark.asyncio
async def test_warmup_account_endpoint_queues_task_and_processing_persists_logs(
    setup_db, monkeypatch
):
    async_session = setup_db
    async with async_session() as session:
        acc = Account(
            login="warmup_ok",
            password="p",
            status=AccountStatus.ACTIVE,
            user_agent="ua",
        )
        session.add(acc)
        await session.commit()
        await session.refresh(acc)
        acc_id = acc.id

    class StubBrowser:
        def __init__(
            self,
            account,
            headless: bool = True,
            strict_cookie_session: bool = True,
            log_callback=None,
        ) -> None:
            self.account = account
            self.log_callback = log_callback

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def login(self) -> None:
            if self.log_callback is not None:
                await self.log_callback("Авторизация успешна для прогрева.")
            return None

        async def warmup(self, duration_seconds: int = 360) -> dict[str, object]:
            assert 300 <= duration_seconds <= 600
            if self.log_callback is not None:
                await self.log_callback("Прогрев: старт действия Лента.")
                await self.log_callback("Прогрев: Лента успешно завершено за 100 мс.")
            return {
                "result": "completed",
                "actions_attempted": 1,
                "actions_succeeded": 1,
                "actions_failed": 0,
                "action_log": [
                    {"action": "_warmup_scroll_feed", "status": "ok", "duration_ms": 100}
                ],
                "error_message": None,
                "duration_seconds": 12,
            }

        async def get_storage_state(self):
            return {"cookies": [{"name": "c_user", "value": "1"}]}

    monkeypatch.setattr(api_module, "FacebookBrowser", StubBrowser)

    importlib.reload(api_module)
    monkeypatch.setattr(api_module, "FacebookBrowser", StubBrowser)
    transport = httpx.ASGITransport(app=api_module.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        resp = await client.post(
            f"/api/accounts/{acc_id}/warmup",
            headers=_auth_headers(),
        )
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["status"] == "queued"
        task_id = payload["task_id"]

        alias_resp = await client.post(
            f"/accounts/{acc_id}/warmup",
            headers=_auth_headers(),
        )
        assert alias_resp.status_code == 200
        alias_payload = alias_resp.json()
        assert alias_payload["status"] == "queued"

        tasks_resp = await client.get("/api/tasks", headers=_auth_headers())
        assert tasks_resp.status_code == 200
        queued_task = next(item for item in tasks_resp.json() if item["id"] == task_id)
        assert queued_task["action_type"] == TaskActionType.WARMUP.value
        assert queued_task["status"] == TaskStatus.PENDING.value
        assert any("поставлен в очередь" in log["message"].lower() for log in queued_task["logs"])

    async with async_session() as session:
        task_obj = await session.get(Task, task_id)
        assert task_obj is not None

        finished = await api_module._process_browser_task(session, task_obj)
        await session.refresh(task_obj)
        account = await session.get(Account, acc_id)
        assert account is not None
        await session.refresh(account)

        task_logs = (
            await session.scalars(select(Log).where(Log.task_id == task_id))
        ).all()
        messages = [row.message for row in task_logs]

        assert finished is True
        assert task_obj.status == TaskStatus.SUCCESS
        assert any("Прогрев" in msg for msg in messages)
        assert any("Лента" in msg for msg in messages)

    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        accounts_resp = await client.get("/api/accounts", headers=_auth_headers())
        row = next(item for item in accounts_resp.json() if item["id"] == acc_id)
        assert row["warmed_up_at"] is not None

        logs_resp = await client.get(
            f"/api/accounts/{acc_id}/warmup/logs?limit=5",
            headers=_auth_headers(),
        )
        assert logs_resp.status_code == 200
        logs_payload = logs_resp.json()
        assert len(logs_payload) >= 1
        latest = logs_payload[0]
        assert latest["result"] == "completed"
        assert latest["actions_attempted"] == 1
        assert latest["actions_succeeded"] == 1
        assert latest["actions_failed"] == 0
        assert latest["action_log"][0]["action"] == "_warmup_scroll_feed"


@pytest.mark.asyncio
async def test_bulk_warmup_endpoint_creates_visible_warmup_tasks(setup_db):
    async_session = setup_db
    async with async_session() as session:
        acc = Account(
            login="bulk_warmup_visible",
            password="p",
            status=AccountStatus.ACTIVE,
            user_agent="ua",
        )
        session.add(acc)
        await session.commit()
        await session.refresh(acc)
        acc_id = acc.id

    importlib.reload(api_module)
    transport = httpx.ASGITransport(app=api_module.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        resp = await client.post(
            "/api/accounts/bulk_warmup",
            json={"ids": [acc_id]},
            headers=_auth_headers(),
        )
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["queued"] == 1

        tasks_resp = await client.get("/api/tasks", headers=_auth_headers())
        assert tasks_resp.status_code == 200
        tasks = tasks_resp.json()
        warmup_task = next((item for item in tasks if item["account_id"] == acc_id), None)
        assert warmup_task is not None
        assert warmup_task["action_type"] == TaskActionType.WARMUP.value
        assert warmup_task["status"] in {
            TaskStatus.PENDING.value,
            TaskStatus.IN_PROGRESS.value,
        }


@pytest.mark.asyncio
async def test_warmup_account_endpoint_errors_for_missing_or_non_active(
    setup_db, monkeypatch
):
    async_session = setup_db
    async with async_session() as session:
        banned = Account(
            login="warmup_banned",
            password="p",
            status=AccountStatus.BANNED,
            user_agent="ua",
        )
        checkpoint = Account(
            login="warmup_checkpoint",
            password="p",
            status=AccountStatus.CHECKPOINT,
            user_agent="ua",
        )
        session.add_all([banned, checkpoint])
        await session.commit()
        await session.refresh(banned)
        await session.refresh(checkpoint)

    class FailIfUsedBrowser:
        def __init__(self, *args, **kwargs) -> None:
            raise AssertionError("Browser must not be created for non-active accounts")

    monkeypatch.setattr(api_module, "FacebookBrowser", FailIfUsedBrowser)
    importlib.reload(api_module)
    monkeypatch.setattr(api_module, "FacebookBrowser", FailIfUsedBrowser)
    transport = httpx.ASGITransport(app=api_module.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        missing_resp = await client.post(
            "/api/accounts/999999/warmup",
            headers=_auth_headers(),
        )
        assert missing_resp.status_code == 404

        banned_resp = await client.post(
            f"/api/accounts/{banned.id}/warmup",
            headers=_auth_headers(),
        )
        assert banned_resp.status_code == 400
        assert "не может участвовать" in banned_resp.json()["detail"]

        checkpoint_resp = await client.post(
            f"/api/accounts/{checkpoint.id}/warmup",
            headers=_auth_headers(),
        )
        assert checkpoint_resp.status_code == 400
        assert "не может участвовать" in checkpoint_resp.json()["detail"]


@pytest.mark.asyncio
async def test_warmup_account_endpoint_skips_when_recently_warmed(
    setup_db, monkeypatch
):
    async_session = setup_db
    async with async_session() as session:
        acc = Account(
            login="warmup_recent",
            password="p",
            status=AccountStatus.ACTIVE,
            user_agent="ua",
            warmed_up_at=api_module._utc_now() - timedelta(hours=2),
        )
        session.add(acc)
        await session.commit()
        await session.refresh(acc)

    class FailIfUsedBrowser:
        def __init__(self, *args, **kwargs) -> None:
            raise AssertionError("Browser must not be created on recent warmup skip")

    monkeypatch.setattr(api_module, "FacebookBrowser", FailIfUsedBrowser)
    importlib.reload(api_module)
    monkeypatch.setattr(api_module, "FacebookBrowser", FailIfUsedBrowser)
    transport = httpx.ASGITransport(app=api_module.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        resp = await client.post(
            f"/api/accounts/{acc.id}/warmup",
            headers=_auth_headers(),
        )
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["status"] == "skipped"
        assert "Already warmed up" in payload["reason"]


@pytest.mark.asyncio
async def test_warmup_account_endpoint_reuses_existing_inflight_task(setup_db):
    async_session = setup_db
    async with async_session() as session:
        acc = Account(
            login="warmup_dedupe",
            password="p",
            status=AccountStatus.ACTIVE,
            user_agent="ua",
        )
        session.add(acc)
        await session.commit()
        await session.refresh(acc)

        task = Task(
            account_id=acc.id,
            action_type=TaskActionType.WARMUP,
            target_url="https://www.facebook.com/",
            payload_text=None,
            status=TaskStatus.PENDING,
            target_gender="ANY",
        )
        session.add(task)
        await session.commit()
        await session.refresh(task)
        task_id = task.id

    importlib.reload(api_module)
    transport = httpx.ASGITransport(app=api_module.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        resp = await client.post(
            f"/api/accounts/{acc.id}/warmup",
            headers=_auth_headers(),
        )
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["status"] == "queued"
        assert payload["task_id"] == task_id

    async with async_session() as session:
        tasks = (
            await session.scalars(
                select(Task).where(
                    Task.account_id == acc.id,
                    Task.action_type == TaskActionType.WARMUP,
                )
            )
        ).all()
        assert len(tasks) == 1


@pytest.mark.asyncio
async def test_process_browser_task_requeues_warmup_when_assigned_account_missing(
    setup_db,
):
    async_session = setup_db
    async with async_session() as session:
        task = Task(
            account_id=999999,
            action_type=TaskActionType.WARMUP,
            target_url="https://www.facebook.com/",
            payload_text=None,
            status=TaskStatus.IN_PROGRESS,
            target_gender="ANY",
        )
        session.add(task)
        await session.commit()
        await session.refresh(task)

        finished = await api_module._process_browser_task(session, task)
        await session.refresh(task)

        assert finished is False
        assert task.status == TaskStatus.PENDING
        assert task.account_id is None


@pytest.mark.asyncio
async def test_warmup_account_persists_error_log_on_failure(setup_db, monkeypatch):
    async_session = setup_db
    async with async_session() as session:
        acc = Account(
            login="warmup_fails",
            password="p",
            status=AccountStatus.ACTIVE,
            user_agent="ua",
        )
        session.add(acc)
        await session.commit()
        await session.refresh(acc)
        acc_id = acc.id

    class FailingBrowser:
        def __init__(
            self,
            account,
            headless: bool = True,
            strict_cookie_session: bool = True,
            log_callback=None,
        ) -> None:
            self.account = account
            self.log_callback = log_callback

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def login(self) -> None:
            raise AccountCaptchaError("login exploded")

    monkeypatch.setattr(api_module, "FacebookBrowser", FailingBrowser)
    importlib.reload(api_module)
    monkeypatch.setattr(api_module, "FacebookBrowser", FailingBrowser)
    transport = httpx.ASGITransport(app=api_module.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        warmup_resp = await client.post(
            f"/api/accounts/{acc_id}/warmup",
            headers=_auth_headers(),
        )
        assert warmup_resp.status_code == 200
        payload = warmup_resp.json()
        assert payload["status"] == "queued"
        task_id = payload["task_id"]

    async with async_session() as session:
        task = await session.get(Task, task_id)
        assert task is not None
        finished = await api_module._process_browser_task(session, task)
        await session.refresh(task)
        assert finished is True
        assert task.status == TaskStatus.ERROR
        task_logs = (
            await session.scalars(select(Log).where(Log.task_id == task_id))
        ).all()
        messages = [row.message for row in task_logs]
        assert any("Не удалось прогреть аккаунт" in message for message in messages)
        assert all("Ищу замену" not in message for message in messages)

    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        logs_resp = await client.get(
            f"/api/accounts/{acc_id}/warmup/logs?limit=5",
            headers=_auth_headers(),
        )
        assert logs_resp.status_code == 200
        logs_payload = logs_resp.json()
        assert len(logs_payload) >= 1
        assert logs_payload[0]["result"] == "error"
        assert "login exploded" in (logs_payload[0]["error_message"] or "")


@pytest.mark.asyncio
async def test_warmup_invalid_credentials_marks_account_special_status(
    setup_db, monkeypatch
):
    async_session = setup_db
    async with async_session() as session:
        acc = Account(
            login="warmup_invalid_creds",
            password="p",
            status=AccountStatus.ACTIVE,
            user_agent="ua",
        )
        session.add(acc)
        await session.commit()
        await session.refresh(acc)
        acc_id = acc.id

    class InvalidCredsBrowser:
        def __init__(
            self,
            account,
            headless: bool = True,
            strict_cookie_session: bool = True,
            log_callback=None,
        ) -> None:
            self.account = account
            self.log_callback = log_callback

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def login(self) -> None:
            raise AccountInvalidCredentialsError("wrong password")

    monkeypatch.setattr(api_module, "FacebookBrowser", InvalidCredsBrowser)
    importlib.reload(api_module)
    monkeypatch.setattr(api_module, "FacebookBrowser", InvalidCredsBrowser)
    transport = httpx.ASGITransport(app=api_module.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        warmup_resp = await client.post(
            f"/api/accounts/{acc_id}/warmup",
            headers=_auth_headers(),
        )
        assert warmup_resp.status_code == 200
        task_id = warmup_resp.json()["task_id"]

    async with async_session() as session:
        task = await session.get(Task, task_id)
        assert task is not None
        finished = await api_module._process_browser_task(session, task)
        await session.refresh(task)
        account = await session.get(Account, acc_id)
        assert account is not None
        await session.refresh(account)
        task_logs = (
            await session.scalars(select(Log).where(Log.task_id == task_id))
        ).all()
        messages = [row.message for row in task_logs]

        assert finished is True
        assert task.status == TaskStatus.ERROR
        assert account.status == AccountStatus.INVALID_CREDENTIALS
        assert any("Facebook отклонил логин или пароль" in message for message in messages)


@pytest.mark.asyncio
async def test_get_accounts_returns_email_credentials_fields(setup_db):
    async_session = setup_db
    async with async_session() as session:
        acc = Account(
            login="mail_fields_user",
            password="p",
            status=AccountStatus.ACTIVE,
            user_agent="ua",
            email_login="mail@example.com",
            email_password="mail-pass-123",
        )
        session.add(acc)
        await session.commit()
        await session.refresh(acc)
        acc_id = acc.id

    importlib.reload(api_module)
    transport = httpx.ASGITransport(app=api_module.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        resp = await client.get("/api/accounts", headers=_auth_headers())
        assert resp.status_code == 200
        row = next(item for item in resp.json() if item["id"] == acc_id)
        assert row["email_login"] == "mail@example.com"
        assert row["email_password"] == "mail-pass-123"


@pytest.mark.asyncio
async def test_update_account_email_endpoint_updates_and_clears_values(setup_db):
    async_session = setup_db
    async with async_session() as session:
        acc = Account(
            login="mail_update_user",
            password="p",
            status=AccountStatus.ACTIVE,
            user_agent="ua",
        )
        session.add(acc)
        await session.commit()
        await session.refresh(acc)
        acc_id = acc.id

    importlib.reload(api_module)
    transport = httpx.ASGITransport(app=api_module.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        save_resp = await client.put(
            f"/api/accounts/{acc_id}/email",
            json={
                "email_login": "checkpoint@example.com",
                "email_password": "mail-pass-123",
            },
            headers=_auth_headers(),
        )
        assert save_resp.status_code == 200
        save_payload = save_resp.json()
        assert save_payload["status"] == "success"
        assert save_payload["email_login"] == "checkpoint@example.com"
        assert save_payload["email_password"] == "mail-pass-123"

        clear_resp = await client.put(
            f"/api/accounts/{acc_id}/email",
            json={"email_login": "   ", "email_password": ""},
            headers=_auth_headers(),
        )
        assert clear_resp.status_code == 200
        clear_payload = clear_resp.json()
        assert clear_payload["email_login"] is None
        assert clear_payload["email_password"] is None

        acc_resp = await client.get("/api/accounts", headers=_auth_headers())
        row = next(item for item in acc_resp.json() if item["id"] == acc_id)
        assert row["email_login"] is None
        assert row["email_password"] is None

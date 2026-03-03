from __future__ import annotations
import pytest
import httpx
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from models import Base, Account, AccountStatus, Proxy, Task, TaskStatus, TaskActionType
import api as api_module
import importlib
from pathlib import Path


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

        with pytest.raises(RuntimeError, match="Не найден свободный аккаунт, который еще не выполнял действий по этой ссылке"):
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

        sel_m = await api_module._get_active_account(session, target_url="http://fb.com", target_gender="M")
        assert sel_m.login == "male"

        sel_f = await api_module._get_active_account(session, target_url="http://fb.com", target_gender="F")
        assert sel_f.login == "female"

        sel_any = await api_module._get_active_account(session, target_url="http://fb.com", target_gender="ANY")
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


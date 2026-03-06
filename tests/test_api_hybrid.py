from __future__ import annotations

import importlib
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import api as api_module
from models import Account, AccountStatus, Base, Task, TaskActionType, TaskStatus, Proxy


def _auth_headers() -> dict[str, str]:
    import base64

    auth_str = "admin:admin"
    auth_bytes = auth_str.encode("ascii")
    base64_auth = base64.b64encode(auth_bytes).decode("ascii")
    return {"Authorization": f"Basic {base64_auth}"}


async def _seed_db(db_url: str) -> None:
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    async_session = async_sessionmaker(engine, expire_on_commit=False)
    async with async_session() as session:
        acc = Account(
            login="active_user",
            password="password",
            user_agent="Mozilla/5.0",
            status=AccountStatus.ACTIVE,
            gender="M",
        )
        session.add(acc)
        await session.commit()
    await engine.dispose()


@pytest.mark.asyncio
async def test_create_tasks_mass_creation(monkeypatch, tmp_path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'mass.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    await _seed_db(database_url)
    importlib.reload(api_module)

    # Мокаем воркер, чтобы не ждать паузы
    monkeypatch.setattr(api_module, "process_provider_task", lambda x: None)

    transport = httpx.ASGITransport(app=api_module.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        # Создаем 3 комментария
        response = await client.post(
            "/api/tasks",
            json={
                "url": "https://fb.com/post1",
                "action_type": "comment_post",
                "payload_text": "nice",
                "quantity": 3,
            },
            headers=_auth_headers(),
        )
        assert response.status_code == 201

        async with api_module.SessionLocal() as session:
            tasks = (await session.execute(select(Task))).scalars().all()
            assert len(tasks) == 3
            for t in tasks:
                assert t.status == TaskStatus.PENDING
                assert t.action_type == TaskActionType.COMMENT_POST


@pytest.mark.asyncio
async def test_update_account_proxy_exclusivity(monkeypatch, tmp_path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'proxy_excl.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)

    engine = create_async_engine(database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    async_session = async_sessionmaker(engine, expire_on_commit=False)
    async with async_session() as session:
        p = Proxy(host="1.1.1.1", port=80, is_active=True)
        session.add(p)
        await session.flush()

        acc1 = Account(
            login="user1",
            password="p",
            user_agent="ua",
            status=AccountStatus.ACTIVE,
            proxy_id=p.id,
        )
        acc2 = Account(
            login="user2",
            password="p",
            user_agent="ua",
            status=AccountStatus.ACTIVE,
            proxy_id=None,
        )
        session.add_all([acc1, acc2])
        await session.commit()

        acc1_id, acc2_id, p_id = acc1.id, acc2.id, p.id

    importlib.reload(api_module)
    transport = httpx.ASGITransport(app=api_module.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        # Try to assign same proxy to acc2
        response = await client.put(
            f"/api/accounts/{acc2_id}/proxy",
            json={"proxy_id": p_id},
            headers=_auth_headers(),
        )
        assert response.status_code == 400
        assert "уже используется" in response.json()["detail"]

        # Mark acc1 as banned
        await client.post(f"/api/accounts/{acc1_id}/ban", headers=_auth_headers())

        # Now it should be possible
        response = await client.put(
            f"/api/accounts/{acc2_id}/proxy",
            json={"proxy_id": p_id},
            headers=_auth_headers(),
        )
        assert response.status_code == 200
    await engine.dispose()


@pytest.mark.asyncio
async def test_upload_accounts_assigns_unique_proxies(monkeypatch, tmp_path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'upload_proxies.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)

    engine = create_async_engine(database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async_session = async_sessionmaker(engine, expire_on_commit=False)
    async with async_session() as session:
        p1 = Proxy(host="1.1.1.1", port=80, is_active=True)
        p2 = Proxy(host="2.2.2.2", port=80, is_active=True)
        session.add_all([p1, p2])
        await session.commit()

    importlib.reload(api_module)
    transport = httpx.ASGITransport(app=api_module.app)

    # Simulate uploading 2 files
    files = [
        ("files", ("acc1.txt", b"user1:pass1", "text/plain")),
        ("files", ("acc2.txt", b"user2:pass2", "text/plain")),
    ]

    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/accounts/upload", files=files, headers=_auth_headers()
        )
        assert response.status_code == 200
        assert response.json()["imported"] == 2

        # Verify they have different proxies
        acc_resp = await client.get("/api/accounts", headers=_auth_headers())
        accounts = acc_resp.json()
        proxy_ids = [a["proxy_id"] for a in accounts]
        assert len(set(proxy_ids)) == 2
        assert None not in proxy_ids

    await engine.dispose()


@pytest.mark.asyncio
async def test_parse_comments_with_limit(monkeypatch, tmp_path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'parse.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    await _seed_db(database_url)
    importlib.reload(api_module)

    class FakeApify:
        def __init__(self, results_limit=None, **kwargs):
            self.results_limit = results_limit

        async def run_facebook_comments_scraper(self, url):
            return [
                {
                    "author": "User",
                    "text": "Hi",
                    "comment_url": "url",
                    "likes_count": 0,
                    "replies_count": 0,
                    "replies": [],
                    "date": "",
                }
            ], ["debug"]

        async def aclose(self):
            pass

    monkeypatch.setattr(api_module, "ApifyAPI", FakeApify)

    transport = httpx.ASGITransport(app=api_module.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/parse_comments",
            json={"url": "https://fb.com/p", "limit": 5},
            headers=_auth_headers(),
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert len(data["comments"]) == 1


@pytest.mark.asyncio
async def test_accounts_import_endpoint_returns_per_line_results(monkeypatch, tmp_path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'import_lines.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    await _seed_db(database_url)
    importlib.reload(api_module)

    transport = httpx.ASGITransport(app=api_module.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/accounts/import",
            json={
                "raw_data": "\n".join(
                    [
                        "facebook giriş: 61581112340247 şifre: fbpass mail: user@example.com mail şifre: mailpass",
                        "broken line",
                    ]
                )
            },
            headers=_auth_headers(),
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["imported"] == 1
        assert payload["failed"] == 1
        assert len(payload["lines"]) == 2
        assert payload["lines"][0]["status"] == "ok"
        assert payload["lines"][1]["status"] == "failed"


@pytest.mark.asyncio
async def test_rotate_ip_endpoint_for_mobile_proxy(monkeypatch, tmp_path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'rotate_ip.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)

    engine = create_async_engine(database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async_session = async_sessionmaker(engine, expire_on_commit=False)
    async with async_session() as session:
        proxy = Proxy(host="1.1.1.1", port=8080, user="u", password="p", is_active=True)
        session.add(proxy)
        await session.flush()
        account = Account(
            login="mobile_user",
            password="p",
            user_agent="ua",
            status=AccountStatus.ACTIVE,
            proxy_id=proxy.id,
            proxy_type="mobile",
            proxy_rotation_url="https://rotate.example.com",
        )
        session.add(account)
        await session.commit()
        await session.refresh(account)

    async def _rotate(_: str) -> bool:
        return True

    async def _ip(_: str) -> str:
        return "2.2.2.2"

    monkeypatch.setattr(api_module, "rotate_mobile_ip", _rotate)
    monkeypatch.setattr(api_module, "get_current_ip", _ip)
    importlib.reload(api_module)
    monkeypatch.setattr(api_module, "rotate_mobile_ip", _rotate)
    monkeypatch.setattr(api_module, "get_current_ip", _ip)

    transport = httpx.ASGITransport(app=api_module.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.post(
            f"/api/accounts/{account.id}/rotate-ip",
            headers=_auth_headers(),
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "ok"
        assert payload["new_ip"] == "2.2.2.2"

    await engine.dispose()

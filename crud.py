from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from models import Account, AccountStatus, Proxy

LOGGER = logging.getLogger("crud")


async def get_available_proxy_id(session: AsyncSession) -> int | None:
    """Finds a random active proxy that is NOT in use by any non-banned account."""
    busy_stmt = select(Account.proxy_id).where(
        Account.status != AccountStatus.BANNED, Account.proxy_id.is_not(None)
    )
    busy_proxy_ids = (await session.execute(busy_stmt)).scalars().all()

    stmt = (
        select(Proxy)
        .where(Proxy.is_active.is_(True), Proxy.id.notin_(busy_proxy_ids))
        .order_by(func.random())
        .limit(1)
    )

    new_proxy = await session.scalar(stmt)
    return new_proxy.id if new_proxy else None


async def upsert_account(
    session: AsyncSession,
    login: str,
    password: str,
    user_agent: str,
    gender: str = "ANY",
    cookies: list[dict[str, Any]] | None = None,
    default_proxy_id: int | None = None,
) -> Account:
    """Updates an existing account or creates a new one with an available proxy."""
    account = await session.scalar(select(Account).where(Account.login == login))

    if account is None:
        proxy_id = await get_available_proxy_id(session) or default_proxy_id
        account = Account(
            login=login,
            password=password,
            cookies=cookies,
            proxy_id=proxy_id,
            status=AccountStatus.ACTIVE,
            gender=gender,
            user_agent=user_agent,
        )
        session.add(account)
    else:
        account.password = password
        account.cookies = cookies
        if account.proxy_id is None:
            account.proxy_id = await get_available_proxy_id(session) or default_proxy_id
        account.status = AccountStatus.ACTIVE
        account.gender = gender
        account.user_agent = user_agent

    await session.flush()
    return account

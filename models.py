from __future__ import annotations

import enum
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    func,
)
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(AsyncAttrs, DeclarativeBase):
    """Базовый класс для декларативных моделей."""


class AccountStatus(str, enum.Enum):
    ACTIVE = "active"
    BANNED = "banned"
    CHECKPOINT = "checkpoint"
    CAPTCHA_BLOCKED = "captcha_blocked"


class TaskActionType(str, enum.Enum):
    LIKE_POST = "like_post"
    FOLLOW = "follow"
    COMMENT_POST = "comment_post"
    LIKE_COMMENT = "like_comment"
    LIKE_COMMENT_BOT = "like_comment_bot"
    REPLY_COMMENT = "reply_comment"
    CHECK_LOGIN = "check_login"


class TaskStatus(str, enum.Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUCCESS = "success"
    ERROR = "error"
    STOPPED = "stopped"


class Proxy(Base):
    __tablename__ = "proxies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    user: Mapped[str | None] = mapped_column(String(255), nullable=True)
    password: Mapped[str | None] = mapped_column(String(255), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    accounts: Mapped[list["Account"]] = relationship(back_populates="proxy")


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    login: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    password: Mapped[str] = mapped_column(String(255), nullable=False)
    cookies: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    storage_state: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    proxy_id: Mapped[int | None] = mapped_column(
        ForeignKey("proxies.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[AccountStatus] = mapped_column(
        SAEnum(AccountStatus, name="account_status"),
        nullable=False,
        default=AccountStatus.ACTIVE,
        server_default=AccountStatus.ACTIVE.value,
    )
    gender: Mapped[str] = mapped_column(
        String(3), nullable=False, default="ANY", server_default="ANY"
    )
    daily_actions_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_action_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    user_agent: Mapped[str] = mapped_column(String(512), nullable=False)

    proxy: Mapped[Proxy | None] = relationship(back_populates="accounts")
    tasks: Mapped[list["Task"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=True
    )
    action_type: Mapped[TaskActionType] = mapped_column(
        SAEnum(TaskActionType, name="task_action_type"),
        nullable=False,
    )
    target_url: Mapped[str] = mapped_column(String(1000), nullable=False)
    payload_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_order_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_gender: Mapped[str] = mapped_column(
        String(3), nullable=False, default="ANY", server_default="ANY"
    )
    status: Mapped[TaskStatus] = mapped_column(
        SAEnum(TaskStatus, name="task_status"),
        nullable=False,
        default=TaskStatus.PENDING,
        server_default=TaskStatus.PENDING.value,
    )

    account: Mapped[Account | None] = relationship(back_populates="tasks")
    logs: Mapped[list["Log"]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )


class Log(Base):
    __tablename__ = "logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    task: Mapped[Task] = relationship(back_populates="logs")

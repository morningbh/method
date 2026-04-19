"""ORM models for Method.

Matches SQLite DDL in design spec §2.1 (users, login_codes, sessions,
approval_tokens). research_requests and uploaded_files are M3 and not
implemented here.

Re-exports Base so init_db() finds all tables registered via Base.metadata.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

__all__ = ["ApprovalToken", "Base", "LoginCode", "Session", "User"]


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','active','rejected')",
            name="ck_users_status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class LoginCode(Base):
    __tablename__ = "login_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=False
    )
    # sha256 of 6-digit code + per-row salt.
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    salt: Mapped[str] = mapped_column(String(32), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=False
    )
    # sha256 of cookie token.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ApprovalToken(Base):
    __tablename__ = "approval_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=False
    )
    # sha256 of admin email token.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


# Indexes required by spec §2.1.
Index("idx_sessions_token", Session.token_hash)
Index("idx_login_codes_user", LoginCode.user_id, LoginCode.expires_at)

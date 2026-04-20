"""ORM models for Method.

Matches SQLite DDL in design spec §2.1 (users, login_codes, sessions,
approval_tokens) and Task 3.1 design (research_requests, uploaded_files).

Re-exports Base so init_db() finds all tables registered via Base.metadata.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

__all__ = [
    "ApprovalToken",
    "Base",
    "Comment",
    "LoginCode",
    "ResearchRequest",
    "Session",
    "UploadedFile",
    "User",
]


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


class ResearchRequest(Base):
    __tablename__ = "research_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','running','done','failed')",
            name="ck_research_requests_status",
        ),
    )

    # ULID string primary key — 26-char Crockford base32.
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=False
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    # Absolute path to the generated plan markdown (set when status='done').
    plan_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Non-empty when status='failed' (enforced at router level — HARNESS §1).
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Claude model tag, e.g. 'claude-opus-4-7'.
    model: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class UploadedFile(Base):
    __tablename__ = "uploaded_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(
        Text, ForeignKey("research_requests.id"), nullable=False
    )
    # User-supplied filename; display only, never joined into a fs path.
    original_name: Mapped[str] = mapped_column(Text, nullable=False)
    stored_path: Mapped[str] = mapped_column(Text, nullable=False)
    extracted_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    mime_type: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class Comment(Base):
    """Issue #4 — selection-anchored comments on a research plan + AI replies.

    See ``docs/design/issue-4-feature-b-comments.md`` §2 for the contract.
    """

    __tablename__ = "comments"
    __table_args__ = (
        CheckConstraint(
            "author IN ('user','ai')",
            name="ck_comments_author",
        ),
        CheckConstraint(
            "ai_status IS NULL OR ai_status IN "
            "('pending','streaming','done','failed')",
            name="ck_comments_ai_status",
        ),
    )

    # ULID primary key — 26-char Crockford base32 string.
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    request_id: Mapped[str] = mapped_column(
        Text, ForeignKey("research_requests.id"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=False
    )
    # AI replies point at their user comment's id; user comments are NULL.
    parent_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("comments.id"), nullable=True
    )
    author: Mapped[str] = mapped_column(Text, nullable=False)
    anchor_text: Mapped[str] = mapped_column(Text, nullable=False)
    anchor_before: Mapped[str] = mapped_column(Text, nullable=False)
    anchor_after: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    # NULL for user comments; one of pending/streaming/done/failed for AI.
    ai_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Non-empty when ai_status='failed' (HARNESS §1 parity).
    ai_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Per-reply Anthropic API cost; only set when ai_status='done'.
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # Soft-delete marker — list/get queries always filter `deleted_at IS NULL`.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


# Indexes required by spec §2.1.
Index("idx_sessions_token", Session.token_hash)
Index("idx_login_codes_user", LoginCode.user_id, LoginCode.expires_at)
# Index required by Task 3.1 design §6.1 — history list queries.
Index(
    "idx_requests_user_created",
    ResearchRequest.user_id,
    ResearchRequest.created_at.desc(),
)
# Indexes required by Issue #4 design §2.
Index("idx_comments_request_created", Comment.request_id, Comment.created_at)
Index("idx_comments_parent", Comment.parent_id)

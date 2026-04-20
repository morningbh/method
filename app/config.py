"""Application settings loaded from environment / .env via pydantic-settings."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Admin / identity
    admin_email: str
    # Email domains whose new registrations auto-activate (comma-separated, lowercased).
    # Example: "xvc.com,projectstar.ai"
    auto_approved_domains: str = ""

    # SMTP
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    smtp_from: str
    smtp_from_name: str

    # Public base URL (used for approval / login links)
    base_url: str

    # Claude CLI
    claude_bin: str
    claude_model: str
    claude_timeout_sec: int
    claude_concurrency: int

    # Comment AI (Issue #4 — Feature B). Empty string falls back to
    # ``claude_model`` (default Opus 4.7). Per design §5, keep an env
    # override as a cost escape hatch without changing code.
    comment_model: str = ""
    claude_comment_timeout_sec: int = 60

    # Paths
    db_path: str
    upload_dir: str
    plan_dir: str
    log_dir: str

    # Auth / sessions
    session_secret: str
    session_ttl_days: int
    login_code_ttl_min: int
    approval_token_ttl_days: int

    # E2E tests
    e2e_test_user_email: str


settings = Settings()

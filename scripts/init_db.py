"""First-time deploy: create all tables defined on Base.metadata.

Usage:
    python scripts/init_db.py

Reads DB_PATH from environment / .env via app.config.settings.
"""
from __future__ import annotations

import asyncio

from app import models  # noqa: F401  (register model tables on Base.metadata)
from app.db import init_db


def main() -> None:
    asyncio.run(init_db())
    print("init_db: done")


if __name__ == "__main__":
    main()

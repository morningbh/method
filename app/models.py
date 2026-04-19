"""ORM model registry.

Re-exports Base so init_db() finds all tables registered via subclasses.
Future milestones add User, Session, LoginCode, etc. here.
"""
from app.db import Base

__all__ = ["Base"]

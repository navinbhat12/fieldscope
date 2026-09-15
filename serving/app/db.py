"""Database engine.

A small pool on purpose: the deploy target has ~1 GB of RAM shared with
Postgres itself, and each Postgres backend costs memory. This service is not
expecting concurrency that would justify more.
"""

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from .config import Settings


def make_engine(settings: Settings) -> Engine:
    return create_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_pool_size,
        pool_pre_ping=True,
        future=True,
    )

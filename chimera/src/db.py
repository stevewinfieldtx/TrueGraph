from __future__ import annotations
import logging
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine, AsyncConnection
from contextlib import asynccontextmanager

from .config import settings
from .models import metadata

log = logging.getLogger(__name__)

_engine = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            settings.async_db_url,
            pool_size=5,
            max_overflow=10,
            echo=False,
        )
    return _engine


@asynccontextmanager
async def get_conn() -> AsyncConnection:
    async with get_engine().connect() as conn:
        yield conn


async def create_tables() -> None:
    """Create all tables and indexes if they do not exist."""
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
        for stmt in [
            "CREATE INDEX IF NOT EXISTS idx_shadow_log_user ON shadow_log(user_email)",
            "CREATE INDEX IF NOT EXISTS idx_shadow_log_scored_at ON shadow_log(scored_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_shadow_log_flagged ON shadow_log(flagged) WHERE flagged = TRUE",
            "CREATE INDEX IF NOT EXISTS idx_shadow_log_sender ON shadow_log(sender_email)",
            "CREATE INDEX IF NOT EXISTS idx_graph_subs_user ON graph_subscriptions(user_email)",
        ]:
            await conn.execute(sa.text(stmt))
    log.info("Database tables and indexes ready")

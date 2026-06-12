from __future__ import annotations
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .config import settings
from .db import create_tables
from .webhook import router as webhook_router
from .admin import router as admin_router
from . import subscription_manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

RENEWAL_CHECK_INTERVAL = 3600  # seconds between renewal checks


async def _renewal_loop() -> None:
    """
    Background task: check for expiring Graph subscriptions every hour and renew them.
    Subscriptions expire in ~3 days; we renew when < 12 hours remain.
    """
    while True:
        try:
            await asyncio.sleep(RENEWAL_CHECK_INTERVAL)
            count = await subscription_manager.renew_expiring_subscriptions()
            if count:
                log.info("Renewal loop: renewed %d subscription(s)", count)
        except asyncio.CancelledError:
            break
        except Exception:
            log.exception("Renewal loop error — will retry next cycle")


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info(
        "TrueGraph starting | mode=%s threshold=%.2f cpa=%s",
        settings.mode, settings.flag_threshold, settings.cpa_base_url,
    )
    await create_tables()
    # Run one renewal pass immediately on startup in case we were down during expiry window
    try:
        renewed = await subscription_manager.renew_expiring_subscriptions()
        if renewed:
            log.info("Startup renewal: renewed %d subscription(s)", renewed)
    except Exception:
        log.exception("Startup renewal failed (non-fatal)")

    renewal_task = asyncio.create_task(_renewal_loop())
    yield
    renewal_task.cancel()
    try:
        await renewal_task
    except asyncio.CancelledError:
        pass
    log.info("TrueGraph shutdown complete")


app = FastAPI(
    title="TrueGraph",
    description=(
        "Microsoft Graph change notification handler for Chimera Secured. "
        "Watches enrolled inboxes, scores incoming email against the claimed sender's "
        "Communication Personality Profile, and logs results in shadow mode."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(webhook_router)
app.include_router(admin_router)


@app.get("/health", tags=["ops"])
async def health():
    return {"status": "ok", "mode": settings.mode, "cpa": settings.cpa_base_url}


@app.get("/", tags=["ops"])
async def root():
    return {
        "service": "TrueGraph",
        "version": "0.1.0",
        "docs": "/docs",
        "dashboard": "/admin/dashboard",
        "health": "/health",
    }

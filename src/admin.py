from __future__ import annotations
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse

from .config import settings
from .db import get_conn
from .models import graph_subscriptions, shadow_log, tenants, FeedbackIn, TenantIn
from . import subscription_manager

log = logging.getLogger(__name__)
router = APIRouter(prefix="/admin")

_DASHBOARD_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "static", "dashboard.html"
)


# ---- Subscription management ----

@router.post("/subscribe")
async def subscribe(body: TenantIn):
    """Register a mailbox for monitoring. Creates a Graph subscription on the inbox."""
    result = await subscription_manager.subscribe_user(body.user_email, body.cpa_tenant_id)
    return result


@router.delete("/subscribe/{user_email:path}")
async def unsubscribe(user_email: str):
    """Cancel all monitoring for a mailbox."""
    count = await subscription_manager.unsubscribe_user(user_email)
    return {"user_email": user_email, "subscriptions_removed": count}


@router.get("/subscriptions")
async def list_subscriptions():
    """List all active Graph subscriptions."""
    async with get_conn() as conn:
        result = await conn.execute(
            sa.select(graph_subscriptions)
            .where(graph_subscriptions.c.active == True)
            .order_by(graph_subscriptions.c.expiration_dt)
        )
        rows = result.fetchall()
    return [dict(r._mapping) for r in rows]


@router.post("/renew")
async def trigger_renewal():
    """Manually trigger subscription renewal (normally runs automatically every hour)."""
    count = await subscription_manager.renew_expiring_subscriptions()
    return {"renewed": count}


# ---- Shadow log ----

@router.get("/shadow-log")
async def get_shadow_log(
    days: int = Query(7, ge=1, le=90, description="Look-back window in days"),
    flagged_only: bool = Query(False),
    user_email: Optional[str] = Query(None),
    sender_email: Optional[str] = Query(None),
    limit: int = Query(200, ge=1, le=1000),
):
    """
    Return shadow-mode scored email records.
    No email bodies are stored — only sender, score, flags, and feedback.
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)
    filters = [shadow_log.c.scored_at >= since]
    if flagged_only:
        filters.append(shadow_log.c.flagged == True)
    if user_email:
        filters.append(shadow_log.c.user_email == user_email)
    if sender_email:
        filters.append(shadow_log.c.sender_email == sender_email)

    async with get_conn() as conn:
        result = await conn.execute(
            sa.select(shadow_log)
            .where(sa.and_(*filters))
            .order_by(shadow_log.c.scored_at.desc())
            .limit(limit)
        )
        rows = result.fetchall()
    return [dict(r._mapping) for r in rows]


@router.get("/stats")
async def get_stats(days: int = Query(7, ge=1, le=90)):
    """Summary statistics for the shadow log window."""
    since = datetime.now(timezone.utc) - timedelta(days=days)

    async with get_conn() as conn:
        total = (await conn.execute(
            sa.select(sa.func.count()).select_from(shadow_log)
            .where(shadow_log.c.scored_at >= since)
        )).scalar() or 0

        flagged = (await conn.execute(
            sa.select(sa.func.count()).select_from(shadow_log)
            .where(sa.and_(shadow_log.c.scored_at >= since, shadow_log.c.flagged == True))
        )).scalar() or 0

        no_cpp = (await conn.execute(
            sa.select(sa.func.count()).select_from(shadow_log)
            .where(sa.and_(
                shadow_log.c.scored_at >= since,
                shadow_log.c.tw_source == "no_cpp",
            ))
        )).scalar() or 0

        tp = (await conn.execute(
            sa.select(sa.func.count()).select_from(shadow_log)
            .where(sa.and_(shadow_log.c.scored_at >= since, shadow_log.c.feedback == "TP"))
        )).scalar() or 0

        fp = (await conn.execute(
            sa.select(sa.func.count()).select_from(shadow_log)
            .where(sa.and_(shadow_log.c.scored_at >= since, shadow_log.c.feedback == "FP"))
        )).scalar() or 0

        active_subs = (await conn.execute(
            sa.select(sa.func.count()).select_from(graph_subscriptions)
            .where(graph_subscriptions.c.active == True)
        )).scalar() or 0

    return {
        "window_days": days,
        "total_scored": total,
        "flagged": flagged,
        "passed": total - flagged,
        "flag_rate": round(flagged / total, 4) if total else 0.0,
        "no_cpp_senders": no_cpp,
        "feedback_tp": tp,
        "feedback_fp": fp,
        "active_subscriptions": active_subs,
        "mode": settings.mode,
        "flag_threshold": settings.flag_threshold,
    }


# ---- Feedback ----

@router.post("/feedback/{log_id}")
async def submit_feedback(log_id: int, body: FeedbackIn):
    """
    Submit admin feedback on a shadow log entry.
    TP = true positive (correctly flagged), FP = false positive (should have passed),
    inconclusive = unclear.
    """
    if body.feedback not in ("TP", "FP", "inconclusive"):
        raise HTTPException(400, detail="feedback must be one of: TP, FP, inconclusive")

    async with get_conn() as conn:
        result = await conn.execute(
            sa.update(shadow_log)
            .where(shadow_log.c.id == log_id)
            .values(feedback=body.feedback)
            .returning(shadow_log.c.id)
        )
        row = result.fetchone()
        await conn.commit()

    if not row:
        raise HTTPException(404, detail="Log entry not found")
    return {"id": log_id, "feedback": body.feedback}


# ---- Dashboard ----

@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    """Serve the admin dashboard UI."""
    if os.path.exists(_DASHBOARD_PATH):
        with open(_DASHBOARD_PATH, encoding="utf-8") as f:
            return HTMLResponse(f.read())
    return HTMLResponse("<h1>dashboard.html not found</h1>", status_code=404)

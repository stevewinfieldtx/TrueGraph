from __future__ import annotations
import asyncio
import logging
from datetime import datetime, timezone

import sqlalchemy as sa
from fastapi import APIRouter, BackgroundTasks, Request, Response

from .config import settings
from .db import get_conn
from .models import graph_subscriptions, shadow_log
from . import graph_client
from .scorer import score_email

log = logging.getLogger(__name__)
router = APIRouter()


# ---- Validation handshake ----

@router.get("/webhook")
async def graph_validation(validationToken: str = None):
    """
    Graph subscription validation.
    When you call POST /subscriptions, Graph immediately GETs this endpoint
    with ?validationToken=<random>. We must echo it back as text/plain within 10 seconds.
    """
    if validationToken:
        log.info("Graph validation handshake — subscription confirmed")
        return Response(content=validationToken, media_type="text/plain", status_code=200)
    return Response(status_code=400)


# ---- Notification receiver ----

@router.post("/webhook")
async def receive_notification(request: Request, background_tasks: BackgroundTasks):
    """
    Receive Graph change notifications for new inbox messages.
    Graph expects 202 within a few seconds — we ack immediately and
    dispatch processing to a background task so we never block here.
    """
    try:
        body = await request.json()
    except Exception:
        return Response(status_code=400)

    for notification in body.get("value", []):
        # Validate client state to reject spoofed notifications
        if notification.get("clientState") != settings.webhook_client_state:
            log.warning(
                "Rejected notification with wrong clientState: %r",
                notification.get("clientState"),
            )
            continue
        background_tasks.add_task(_process_notification, notification)

    # 202 = "received, will process" — Graph requires this
    return Response(status_code=202)


# ---- Background processing ----

async def _process_notification(notification: dict) -> None:
    """
    Full pipeline for one Graph notification:
    1. Resolve which mailbox this subscription belongs to
    2. Fetch the actual message from Graph
    3. Score against the claimed sender's CPP
    4. Write to shadow_log (no email body stored)
    5. Take action based on MODE (shadow = log only)
    """
    subscription_id = notification.get("subscriptionId", "")
    resource = notification.get("resource", "")

    # Extract message ID from resource path
    # e.g. /Users/{userId}/Messages/{messageId}
    message_id = resource.rstrip("/").split("/")[-1]
    if not message_id:
        log.warning("Could not extract message_id from resource: %s", resource)
        return

    # Look up which inbox owner this subscription belongs to
    inbox_owner = await _inbox_owner_for_subscription(subscription_id)
    if not inbox_owner:
        log.warning("No tenant found for subscription_id=%s", subscription_id)
        return

    try:
        # Fetch message body + sender (blocking I/O → thread pool)
        msg = await asyncio.to_thread(
            graph_client.fetch_message, inbox_owner, message_id
        )
        sender_email = msg["sender_email"]
        body = msg["body"]
        subject_hash = msg.get("subject_hash")

        if not body.strip():
            log.debug("Empty body for %s in %s — skipping", message_id, inbox_owner)
            return

        # Score against sender's CPP
        score = await score_email(
            inbox_owner=inbox_owner,
            sender_email=sender_email,
            body=body,
        )

        p_authentic = score["p_authentic"]
        confidence = score["confidence"]
        tw_bucket = score["tw_bucket_used"]
        tw_source = score["tw_source"]
        flagged = p_authentic < settings.flag_threshold

        # Write to shadow_log — no body or subject text stored
        await _write_log(
            subscription_id=subscription_id,
            message_id=message_id,
            inbox_owner=inbox_owner,
            sender_email=sender_email,
            subject_hash=subject_hash,
            p_authentic=p_authentic,
            confidence=confidence,
            tw_bucket=tw_bucket,
            tw_source=tw_source,
            flagged=flagged,
        )

        level = log.warning if flagged else log.debug
        level(
            "%s %s->%s p_authentic=%.3f conf=%.2f src=%s mode=%s",
            "FLAG" if flagged else "PASS",
            sender_email, inbox_owner, p_authentic, confidence, tw_source, settings.mode,
        )

        # Shadow mode: log only, no user-facing action
        # warn / enforce mode: implement here when ready
        if flagged and settings.mode != "shadow":
            log.info(
                "Mode=%s: action hooks not yet implemented — treating as shadow",
                settings.mode,
            )

    except Exception:
        log.exception(
            "Error processing message_id=%s inbox=%s", message_id, inbox_owner
        )


async def _inbox_owner_for_subscription(subscription_id: str) -> str | None:
    async with get_conn() as conn:
        result = await conn.execute(
            sa.select(graph_subscriptions.c.user_email).where(
                graph_subscriptions.c.subscription_id == subscription_id
            )
        )
        row = result.fetchone()
    return row.user_email if row else None


async def _write_log(
    subscription_id, message_id, inbox_owner, sender_email,
    subject_hash, p_authentic, confidence, tw_bucket, tw_source, flagged,
) -> None:
    async with get_conn() as conn:
        await conn.execute(
            sa.insert(shadow_log).values(
                scored_at=datetime.now(timezone.utc),
                subscription_id=subscription_id,
                message_id=message_id,
                user_email=inbox_owner,
                sender_email=sender_email,
                subject_hash=subject_hash,
                p_authentic=p_authentic,
                confidence=confidence,
                tw_bucket=tw_bucket,
                tw_source=tw_source,
                flagged=flagged,
                mode=settings.mode,
                action_taken="none",
            )
        )
        await conn.commit()

from __future__ import annotations
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

from .config import settings
from .db import get_conn
from .models import graph_subscriptions, tenants
from . import graph_client

log = logging.getLogger(__name__)

# Renew subscriptions expiring within this window
RENEW_WINDOW_HOURS = 12


async def subscribe_user(user_email: str, cpa_tenant_id: str = "default") -> dict:
    """
    Register a mailbox for monitoring:
    1. Upsert into tenants table
    2. Create a Graph change notification subscription on the user's inbox
    3. Store subscription record in DB
    Returns subscription summary dict.
    """
    if not settings.notification_url:
        raise RuntimeError(
            "NOTIFICATION_URL is not set. Deploy TrueGraph first, then set the env var."
        )

    # Upsert tenant record
    async with get_conn() as conn:
        await conn.execute(sa.text(
            """
            INSERT INTO tenants (user_email, cpa_tenant_id, enrolled_at, active)
            VALUES (:email, :tid, NOW(), TRUE)
            ON CONFLICT (user_email) DO UPDATE
                SET active = TRUE,
                    cpa_tenant_id = EXCLUDED.cpa_tenant_id,
                    enrolled_at = NOW()
            """
        ), {"email": user_email, "tid": cpa_tenant_id})
        await conn.commit()

    # Create Graph subscription (synchronous MSAL/httpx call → thread pool)
    sub = await asyncio.to_thread(
        graph_client.create_subscription,
        user_email,
        settings.notification_url,
        settings.webhook_client_state,
    )

    expiration_dt = datetime.fromisoformat(
        sub["expirationDateTime"].replace("Z", "+00:00")
    )

    # Persist subscription
    async with get_conn() as conn:
        await conn.execute(
            sa.insert(graph_subscriptions).values(
                subscription_id=sub["id"],
                user_email=user_email,
                resource=sub["resource"],
                expiration_dt=expiration_dt,
                client_state=settings.webhook_client_state,
                created_at=datetime.now(timezone.utc),
                active=True,
            )
        )
        await conn.commit()

    log.info("Subscribed %s — subscription_id=%s expires=%s", user_email, sub["id"], expiration_dt)
    return {
        "subscription_id": sub["id"],
        "user_email": user_email,
        "resource": sub["resource"],
        "expires": expiration_dt.isoformat(),
    }


async def unsubscribe_user(user_email: str) -> int:
    """
    Cancel all active subscriptions for a user and mark tenant inactive.
    Returns count of subscriptions removed.
    """
    async with get_conn() as conn:
        result = await conn.execute(
            sa.select(graph_subscriptions.c.subscription_id).where(
                sa.and_(
                    graph_subscriptions.c.user_email == user_email,
                    graph_subscriptions.c.active == True,
                )
            )
        )
        sub_ids = [r.subscription_id for r in result.fetchall()]

    for sid in sub_ids:
        try:
            await asyncio.to_thread(graph_client.delete_subscription, sid)
        except Exception as exc:
            log.warning("Could not delete subscription %s from Graph: %s", sid, exc)
        async with get_conn() as conn:
            await conn.execute(
                sa.update(graph_subscriptions)
                .where(graph_subscriptions.c.subscription_id == sid)
                .values(active=False)
            )
            await conn.commit()

    async with get_conn() as conn:
        await conn.execute(
            sa.update(tenants)
            .where(tenants.c.user_email == user_email)
            .values(active=False)
        )
        await conn.commit()

    log.info("Unsubscribed %s — removed %d subscription(s)", user_email, len(sub_ids))
    return len(sub_ids)


async def renew_expiring_subscriptions() -> int:
    """
    Renew all active subscriptions expiring within RENEW_WINDOW_HOURS.
    Called by the background loop every hour.
    Returns count of subscriptions renewed.
    """
    cutoff = datetime.now(timezone.utc) + timedelta(hours=RENEW_WINDOW_HOURS)

    async with get_conn() as conn:
        result = await conn.execute(
            sa.select(graph_subscriptions).where(
                sa.and_(
                    graph_subscriptions.c.active == True,
                    graph_subscriptions.c.expiration_dt <= cutoff,
                )
            )
        )
        rows = result.fetchall()

    renewed = 0
    for row in rows:
        try:
            updated = await asyncio.to_thread(
                graph_client.renew_subscription, row.subscription_id
            )
            new_exp = datetime.fromisoformat(
                updated["expirationDateTime"].replace("Z", "+00:00")
            )
            async with get_conn() as conn:
                await conn.execute(
                    sa.update(graph_subscriptions)
                    .where(graph_subscriptions.c.subscription_id == row.subscription_id)
                    .values(
                        expiration_dt=new_exp,
                        renewed_at=datetime.now(timezone.utc),
                    )
                )
                await conn.commit()
            log.info("Renewed %s (now expires %s)", row.subscription_id, new_exp)
            renewed += 1
        except Exception as exc:
            log.error("Failed to renew subscription %s: %s", row.subscription_id, exc)

    return renewed

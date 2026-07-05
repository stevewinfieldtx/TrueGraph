from __future__ import annotations
import logging
import httpx
from .config import settings

log = logging.getLogger(__name__)


async def score_email(
    inbox_owner: str,
    sender_email: str,
    body: str,
) -> dict:
    """
    Score an incoming email against the claimed sender's CPP.

    BEC detection logic:
        An email arrives in inbox_owner's mailbox claiming to be from sender_email.
        We ask: "does this email body sound like sender_email actually wrote it?"
        If p_authentic is low, the sender's style doesn't match — possible impersonation.

    CPA /score parameters:
        user_email    = sender_email   (whose CPP to check against)
        recipient_email = inbox_owner  (used for TW bucket selection in the CPP)

    If sender_email has no enrolled CPP, tw_source = "no_cpp" and confidence is low.
    We still log this — "unknown sender" is itself a signal for BEC triage.
    """
    headers = {"Content-Type": "application/json"}
    if settings.cpa_api_key:
        headers["X-API-Key"] = settings.cpa_api_key

    payload = {
        "tenant_id": settings.cpa_tenant_id,
        "user_email": sender_email,    # whose writing profile to check
        "recipient_email": inbox_owner,  # who received it (TW bucket context)
        "email_body": body,
    }

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{settings.cpa_base_url}/score",
            json=payload,
            headers=headers,
        )
        resp.raise_for_status()

    result = resp.json()
    log.debug(
        "scored %s->%s p_authentic=%.3f confidence=%.2f source=%s",
        sender_email, inbox_owner,
        result["p_authentic"], result["confidence"], result["tw_source"],
    )
    return result

from __future__ import annotations
import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from msal import ConfidentialClientApplication

from .config import settings

log = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPES = ["https://graph.microsoft.com/.default"]

# Graph allows max 4230 min for mail subscriptions; we use slightly less
SUBSCRIPTION_LIFETIME_MINUTES = 4200

_msal_app: Optional[ConfidentialClientApplication] = None


def _get_msal_app() -> ConfidentialClientApplication:
    global _msal_app
    if _msal_app is None:
        _msal_app = ConfidentialClientApplication(
            client_id=settings.azure_client_id,
            client_credential=settings.azure_client_secret,
            authority=f"https://login.microsoftonline.com/{settings.azure_tenant_id}",
        )
    return _msal_app


def _get_token() -> str:
    result = _get_msal_app().acquire_token_for_client(scopes=GRAPH_SCOPES)
    if "access_token" not in result:
        raise RuntimeError(
            f"Graph token error: {result.get('error_description', result.get('error'))}"
        )
    return result["access_token"]


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_get_token()}",
        "Content-Type": "application/json",
    }


def _strip_html(html: str) -> str:
    """Quick HTML-to-text for Outlook message bodies."""
    text = re.sub(r"<(style|script)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(p|div|tr|li)>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    for entity, char in [("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                         ("&quot;", '"'), ("&#39;", "'")]:
        text = text.replace(entity, char)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return re.sub(r"[ \t]+", " ", text).strip()


def _expiration_str(minutes: int = SUBSCRIPTION_LIFETIME_MINUTES) -> str:
    dt = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.0000000Z")


# ---- Message fetching ----

def fetch_message(user_email: str, message_id: str) -> dict:
    """
    Fetch a single inbox message by ID.
    Returns: {sender_email, body, subject, subject_hash}
    No body text is stored — caller scores and discards.
    """
    url = f"{GRAPH_BASE}/users/{user_email}/messages/{message_id}"
    params = {"$select": "sender,body,subject,receivedDateTime"}
    with httpx.Client(timeout=30) as client:
        resp = client.get(url, headers=_headers(), params=params)
        resp.raise_for_status()
        msg = resp.json()

    sender_email = (
        msg.get("sender", {}).get("emailAddress", {}).get("address", "unknown@unknown")
    )
    body_obj = msg.get("body", {})
    body = body_obj.get("content", "")
    if body_obj.get("contentType", "").lower() == "html":
        body = _strip_html(body)

    subject = msg.get("subject", "")
    return {
        "sender_email": sender_email.lower().strip(),
        "body": body,
        "subject": subject,
        "subject_hash": hashlib.sha256(subject.encode()).hexdigest()[:16] if subject else None,
    }


# ---- Subscription management ----

def create_subscription(user_email: str, notification_url: str, client_state: str) -> dict:
    """
    Create a Graph change notification subscription watching a user's inbox.
    Returns the full Graph subscription object.
    """
    payload = {
        "changeType": "created",
        "notificationUrl": notification_url,
        "resource": f"/users/{user_email}/mailFolders/inbox/messages",
        "expirationDateTime": _expiration_str(),
        "clientState": client_state,
    }
    with httpx.Client(timeout=30) as client:
        resp = client.post(f"{GRAPH_BASE}/subscriptions", headers=_headers(), json=payload)
        resp.raise_for_status()
    sub = resp.json()
    log.info("Created subscription %s for %s", sub["id"], user_email)
    return sub


def renew_subscription(subscription_id: str) -> dict:
    """Extend an existing subscription's expiration. Returns updated subscription."""
    with httpx.Client(timeout=30) as client:
        resp = client.patch(
            f"{GRAPH_BASE}/subscriptions/{subscription_id}",
            headers=_headers(),
            json={"expirationDateTime": _expiration_str()},
        )
        resp.raise_for_status()
    sub = resp.json()
    log.info("Renewed subscription %s → expires %s", subscription_id, sub.get("expirationDateTime"))
    return sub


def delete_subscription(subscription_id: str) -> None:
    """Cancel a Graph subscription. Tolerates 404 (already expired/deleted)."""
    with httpx.Client(timeout=30) as client:
        resp = client.delete(
            f"{GRAPH_BASE}/subscriptions/{subscription_id}",
            headers=_headers(),
        )
        if resp.status_code not in (200, 204, 404):
            resp.raise_for_status()
    log.info("Deleted subscription %s", subscription_id)


def list_active_subscriptions() -> list[dict]:
    """List all Graph subscriptions visible to this app registration."""
    with httpx.Client(timeout=30) as client:
        resp = client.get(f"{GRAPH_BASE}/subscriptions", headers=_headers())
        resp.raise_for_status()
    return resp.json().get("value", [])

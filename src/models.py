from __future__ import annotations
from datetime import datetime
from typing import Optional
import sqlalchemy as sa
from pydantic import BaseModel

metadata = sa.MetaData()

tenants = sa.Table(
    "tenants",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("user_email", sa.Text, nullable=False, unique=True),
    sa.Column("cpa_tenant_id", sa.Text, nullable=False, default="default"),
    sa.Column("enrolled_at", sa.DateTime(timezone=True)),
    sa.Column("active", sa.Boolean, default=True, nullable=False),
)

graph_subscriptions = sa.Table(
    "graph_subscriptions",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("subscription_id", sa.Text, nullable=False, unique=True),
    sa.Column("user_email", sa.Text, nullable=False),
    sa.Column("resource", sa.Text, nullable=False),
    sa.Column("expiration_dt", sa.DateTime(timezone=True), nullable=False),
    sa.Column("client_state", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True)),
    sa.Column("renewed_at", sa.DateTime(timezone=True)),
    sa.Column("active", sa.Boolean, default=True, nullable=False),
)

shadow_log = sa.Table(
    "shadow_log",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("scored_at", sa.DateTime(timezone=True)),
    sa.Column("subscription_id", sa.Text),
    sa.Column("message_id", sa.Text, nullable=False),
    # user_email = inbox owner (who we're protecting)
    sa.Column("user_email", sa.Text, nullable=False),
    # sender_email = claimed sender (whose CPP we scored against)
    sa.Column("sender_email", sa.Text, nullable=False),
    # subject stored as hash only — no plaintext email content in this table
    sa.Column("subject_hash", sa.Text),
    sa.Column("p_authentic", sa.Float, nullable=False),
    sa.Column("confidence", sa.Float, nullable=False),
    sa.Column("tw_bucket", sa.Text, nullable=False),
    sa.Column("tw_source", sa.Text, nullable=False),
    sa.Column("flagged", sa.Boolean, nullable=False),
    sa.Column("mode", sa.Text, nullable=False, default="shadow"),
    sa.Column("action_taken", sa.Text, default="none"),
    # Admin feedback: TP | FP | inconclusive
    sa.Column("feedback", sa.Text),
)


# ---- Pydantic schemas ----

class TenantIn(BaseModel):
    user_email: str
    cpa_tenant_id: str = "default"


class TenantOut(BaseModel):
    id: int
    user_email: str
    cpa_tenant_id: str
    enrolled_at: Optional[datetime]
    active: bool


class SubscriptionOut(BaseModel):
    id: int
    subscription_id: str
    user_email: str
    expiration_dt: datetime
    active: bool


class ShadowLogEntry(BaseModel):
    id: int
    scored_at: Optional[datetime]
    user_email: str
    sender_email: str
    subject_hash: Optional[str]
    p_authentic: float
    confidence: float
    tw_bucket: str
    tw_source: str
    flagged: bool
    mode: str
    action_taken: Optional[str]
    feedback: Optional[str]


class FeedbackIn(BaseModel):
    feedback: str  # TP | FP | inconclusive

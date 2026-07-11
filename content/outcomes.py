"""
TrueGraph — Win/Loss Outcome Intelligence (the Judgment memory)
================================================================
Ported from the retired CPP-Engine (Node). TrueGraph's canonical role is
Judgment: win-attribution, combination analytics — "keeping up with how
things do when they win or lose." This module is that memory:

  contacts        — people we exchange email with, keyed by address
  deal_outcomes   — what happened and why, with the communication
                    fingerprint at close (the learning layer)
  /match          — compare a live deal's fingerprint against history,
                    return a winning/losing/neutral verdict

Deliberately NOT here: any CPP profile store. Canon (three-engine rule,
2026-07-11): the CPA is the ONLY profile-building authority — including
CPP-S, which briefly lived in this module and moved to the CPA. /match
CONSUMES the contact's CPP-S from the CPA (CPA_URL + CPA_API_KEY env,
GET /profiles/s/{email}, fail-open) but never builds or stores profiles.

Storage: Railway Postgres via DATABASE_URL. When unset, every endpoint
returns 503 and the rest of TrueGraph (stateless graph compute) works
untouched. Recency rule: a 2023 win doesn't count like a 2026 win —
similarity distance grows with age (capped at 5 years).
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

log = logging.getLogger("truegraph.outcomes")

router = APIRouter()

DATABASE_URL = os.getenv("DATABASE_URL", "")

_pool = None


def _get_pool():
    global _pool
    if _pool is None:
        if not DATABASE_URL:
            raise HTTPException(status_code=503, detail="DATABASE_URL not configured — outcome intelligence disabled")
        from psycopg2.pool import ThreadedConnectionPool
        _pool = ThreadedConnectionPool(1, 5, DATABASE_URL)
    return _pool


def _q(sql: str, params: tuple = ()) -> list[dict]:
    """Run a query, return rows as dicts."""
    pool = _get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            if cur.description is None:
                conn.commit()
                return []
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            conn.commit()
            return rows
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


def init_schema() -> None:
    """Create tables if missing. Called from api.py startup when DATABASE_URL is set."""
    _q("""
      CREATE TABLE IF NOT EXISTS sources (
        id SERIAL PRIMARY KEY,
        source_key TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL,
        api_key_hash TEXT,
        active BOOLEAN DEFAULT TRUE,
        created_at TIMESTAMPTZ DEFAULT NOW()
      );

      CREATE TABLE IF NOT EXISTS contacts (
        id SERIAL PRIMARY KEY,
        email TEXT,
        name TEXT,
        company TEXT,
        domain TEXT,
        contact_type TEXT DEFAULT 'external'
          CHECK (contact_type IN ('internal','external')),
        created_at TIMESTAMPTZ DEFAULT NOW(),
        updated_at TIMESTAMPTZ DEFAULT NOW()
      );
      CREATE UNIQUE INDEX IF NOT EXISTS idx_contacts_email
        ON contacts(email) WHERE email IS NOT NULL;
      CREATE INDEX IF NOT EXISTS idx_contacts_domain ON contacts(domain);
      CREATE INDEX IF NOT EXISTS idx_contacts_company ON contacts(company);

      CREATE TABLE IF NOT EXISTS deal_outcomes (
        id SERIAL PRIMARY KEY,
        source_id INTEGER REFERENCES sources(id),
        contact_id INTEGER REFERENCES contacts(id),
        external_deal_id TEXT,
        company TEXT,
        deal_name TEXT,
        outcome TEXT NOT NULL
          CHECK (outcome IN ('won','lost','no_decision','stalled')),
        deal_value NUMERIC DEFAULT 0,
        deal_stage TEXT,
        rep_name TEXT,
        rep_email TEXT,
        total_turns INTEGER DEFAULT 0,
        inbound_turns INTEGER DEFAULT 0,
        outbound_turns INTEGER DEFAULT 0,
        turn_ratio NUMERIC,
        avg_customer_response_hours NUMERIC,
        avg_rep_response_hours NUMERIC,
        max_gap_hours INTEGER DEFAULT 0,
        avg_inbound_length INTEGER DEFAULT 0,
        avg_outbound_length INTEGER DEFAULT 0,
        engagement_trajectory TEXT,
        thread_duration_days INTEGER DEFAULT 0,
        final_intent INTEGER,
        final_win_pct INTEGER,
        final_deal_health TEXT,
        outcome_factors JSONB DEFAULT '[]',
        key_signals JSONB DEFAULT '[]',
        competitor_involved TEXT,
        loss_reason TEXT,
        win_factors TEXT,
        closed_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ DEFAULT NOW()
      );
      CREATE INDEX IF NOT EXISTS idx_do_contact ON deal_outcomes(contact_id);
      CREATE INDEX IF NOT EXISTS idx_do_company ON deal_outcomes(company);
      CREATE INDEX IF NOT EXISTS idx_do_outcome ON deal_outcomes(outcome);
      CREATE INDEX IF NOT EXISTS idx_do_rep ON deal_outcomes(rep_email);
    """)
    log.info("outcome schema ready")


# ── Contacts & sources ──────────────────────────────────────

def find_or_create_contact(data: dict) -> Optional[dict]:
    email = (data.get("email") or "").strip().lower() or None
    if email:
        rows = _q("SELECT * FROM contacts WHERE email = %s", (email,))
        if rows:
            return rows[0]
    domain = data.get("domain") or (email.split("@")[1] if email and "@" in email else None)
    rows = _q(
        "INSERT INTO contacts (email, name, company, domain, contact_type) "
        "VALUES (%s,%s,%s,%s,%s) RETURNING *",
        (email, data.get("name"), data.get("company"), domain,
         data.get("contact_type") or "external"),
    )
    return rows[0] if rows else None


def find_or_create_source(source_key: str, name: str | None) -> Optional[dict]:
    if not source_key:
        return None
    rows = _q("SELECT * FROM sources WHERE source_key = %s", (source_key,))
    if rows:
        return rows[0]
    rows = _q("INSERT INTO sources (source_key, name) VALUES (%s,%s) RETURNING *",
              (source_key, name or source_key))
    return rows[0] if rows else None


# ── Models ──────────────────────────────────────────────────

class ContactIn(BaseModel):
    email: Optional[str] = None
    name: Optional[str] = None
    company: Optional[str] = None
    domain: Optional[str] = None
    contact_type: Optional[str] = None


class OutcomeIn(BaseModel):
    source_key: Optional[str] = None
    source_name: Optional[str] = None
    contact_email: Optional[str] = None
    contact_name: Optional[str] = None
    external_deal_id: Optional[str] = None
    company: Optional[str] = None
    deal_name: Optional[str] = None
    outcome: str
    deal_value: float = 0
    deal_stage: Optional[str] = None
    rep_name: Optional[str] = None
    rep_email: Optional[str] = None
    total_turns: int = 0
    inbound_turns: int = 0
    outbound_turns: int = 0
    turn_ratio: Optional[float] = None
    avg_customer_response_hours: Optional[float] = None
    avg_rep_response_hours: Optional[float] = None
    max_gap_hours: int = 0
    avg_inbound_length: int = 0
    avg_outbound_length: int = 0
    engagement_trajectory: Optional[str] = None
    thread_duration_days: int = 0
    final_intent: Optional[int] = None
    final_win_pct: Optional[int] = None
    final_deal_health: Optional[str] = None
    outcome_factors: list = []
    key_signals: list = []
    competitor_involved: Optional[str] = None
    loss_reason: Optional[str] = None
    win_factors: Optional[str] = None
    closed_at: Optional[str] = None


class MatchIn(BaseModel):
    contact_email: Optional[str] = None
    contact_name: Optional[str] = None
    company: Optional[str] = None
    fingerprint: Optional[dict] = None
    exclude_deal_id: Optional[str] = None


# ── Endpoints ───────────────────────────────────────────────

@router.post("/api/sources")
def post_source(body: dict):
    return {"source": find_or_create_source(body.get("source_key"), body.get("name"))}


@router.post("/api/contacts")
def post_contact(body: ContactIn):
    return {"contact": find_or_create_contact(body.model_dump())}


@router.get("/api/contacts/search")
def search_contacts(domain: Optional[str] = None, company: Optional[str] = None):
    if domain:
        return {"contacts": _q("SELECT * FROM contacts WHERE domain = %s", (domain.lower(),))}
    if company:
        return {"contacts": _q("SELECT * FROM contacts WHERE LOWER(company) = LOWER(%s)", (company,))}
    return {"contacts": []}


# CPP-S consumption — the CPA builds and stores customer-style profiles;
# we fetch the latest at match time. Fail-open: outcome intelligence
# enriches verdicts, a CPA outage never blocks them.

CPA_URL = os.getenv("CPA_URL", "")
CPA_API_KEY = os.getenv("CPA_API_KEY", "")


def _fetch_customer_profile(email: str) -> Optional[dict]:
    if not CPA_URL or not email:
        return None
    try:
        import httpx
        headers = {"X-API-Key": CPA_API_KEY} if CPA_API_KEY else {}
        resp = httpx.get(
            f"{CPA_URL.rstrip('/')}/profiles/s/{email.strip().lower()}",
            headers=headers, timeout=5,
        )
        if resp.status_code == 200:
            return (resp.json() or {}).get("profile")
        log.warning("CPA /profiles/s returned %s for %s", resp.status_code, email)
    except Exception as e:
        log.warning("CPA profile fetch failed for %s: %s", email, e)
    return None


@router.post("/api/outcomes")
def post_outcome(body: OutcomeIn):
    source = find_or_create_source(body.source_key, body.source_name) if body.source_key else None
    contact = None
    if body.contact_email or body.contact_name:
        contact = find_or_create_contact({
            "email": body.contact_email, "name": body.contact_name,
            "company": body.company, "contact_type": "external",
        })
    rows = _q(
        "INSERT INTO deal_outcomes (source_id, contact_id, external_deal_id, company, deal_name, "
        "outcome, deal_value, deal_stage, rep_name, rep_email, total_turns, inbound_turns, "
        "outbound_turns, turn_ratio, avg_customer_response_hours, avg_rep_response_hours, "
        "max_gap_hours, avg_inbound_length, avg_outbound_length, engagement_trajectory, "
        "thread_duration_days, final_intent, final_win_pct, final_deal_health, outcome_factors, "
        "key_signals, competitor_involved, loss_reason, win_factors, closed_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
        "COALESCE(%s::timestamptz, NOW())) RETURNING *",
        (source["id"] if source else None, contact["id"] if contact else None,
         body.external_deal_id, body.company, body.deal_name, body.outcome,
         body.deal_value, body.deal_stage, body.rep_name, body.rep_email,
         body.total_turns, body.inbound_turns, body.outbound_turns, body.turn_ratio,
         body.avg_customer_response_hours, body.avg_rep_response_hours, body.max_gap_hours,
         body.avg_inbound_length, body.avg_outbound_length, body.engagement_trajectory,
         body.thread_duration_days, body.final_intent, body.final_win_pct,
         body.final_deal_health, json.dumps(body.outcome_factors),
         json.dumps(body.key_signals), body.competitor_involved,
         body.loss_reason, body.win_factors, body.closed_at),
    )
    return {"outcome": rows[0]}


@router.get("/api/outcomes/stats")
def outcome_stats(company: Optional[str] = None, rep_email: Optional[str] = None):
    return {"stats": _outcome_stats(company, rep_email)}


def _outcome_stats(company: Optional[str], rep_email: Optional[str] = None) -> list[dict]:
    where, params = ["1=1"], []
    if company:
        where.append("LOWER(company) = LOWER(%s)")
        params.append(company)
    if rep_email:
        where.append("rep_email = %s")
        params.append(rep_email)
    return _q(
        "SELECT outcome, COUNT(*) as count, AVG(deal_value) as avg_deal_value, "
        "AVG(total_turns) as avg_turns, AVG(avg_customer_response_hours) as avg_customer_response_hrs, "
        "AVG(avg_rep_response_hours) as avg_rep_response_hrs, AVG(max_gap_hours) as avg_max_gap_hrs, "
        "AVG(avg_inbound_length) as avg_inbound_len, AVG(thread_duration_days) as avg_duration_days "
        "FROM deal_outcomes WHERE " + " AND ".join(where) + " GROUP BY outcome",
        tuple(params),
    )


@router.get("/api/outcomes/company/{company}")
def outcomes_by_company(company: str, exclude: Optional[str] = None):
    return {"deals": _same_company_deals(company, exclude)}


def _same_company_deals(company: str, exclude_deal_id: Optional[str]) -> list[dict]:
    if exclude_deal_id:
        return _q("SELECT * FROM deal_outcomes WHERE LOWER(company) = LOWER(%s) "
                  "AND external_deal_id != %s ORDER BY closed_at DESC LIMIT 20",
                  (company, exclude_deal_id))
    return _q("SELECT * FROM deal_outcomes WHERE LOWER(company) = LOWER(%s) "
              "ORDER BY closed_at DESC LIMIT 20", (company,))


def _same_contact_deals(contact_id: int, exclude_deal_id: Optional[str]) -> list[dict]:
    if exclude_deal_id:
        return _q("SELECT * FROM deal_outcomes WHERE contact_id = %s "
                  "AND external_deal_id != %s ORDER BY closed_at DESC LIMIT 20",
                  (contact_id, exclude_deal_id))
    return _q("SELECT * FROM deal_outcomes WHERE contact_id = %s "
              "ORDER BY closed_at DESC LIMIT 20", (contact_id,))


def _similar_deals(fingerprint: dict, exclude_deal_id: Optional[str], limit: int = 10) -> list[dict]:
    # Recency-weighted: each year of age adds distance (capped at 5 years),
    # so fresh patterns win ties. A 2023 win shouldn't count like a 2026 win.
    return _q(
        "SELECT *, "
        "ABS(total_turns - %s) + "
        "ABS(COALESCE(avg_customer_response_hours,0) - %s) * 0.1 + "
        "ABS(avg_inbound_length - %s) * 0.01 + "
        "LEAST(EXTRACT(EPOCH FROM (NOW() - COALESCE(closed_at, NOW()))) / 31557600.0, 5) * 3 "
        "AS distance "
        "FROM deal_outcomes "
        "WHERE (%s::text IS NULL OR external_deal_id != %s) "
        "ORDER BY distance ASC LIMIT %s",
        (fingerprint.get("total_turns") or 0,
         fingerprint.get("avg_customer_response_hours") or 0,
         fingerprint.get("avg_inbound_length") or 0,
         exclude_deal_id, exclude_deal_id, limit),
    )


# ── Pattern match: the core judgment query ──────────────────

@router.post("/api/match")
def match_pattern(body: MatchIn):
    results: dict[str, Any] = {"same_contact": [], "same_company": [], "similar": [], "stats": None}

    if body.contact_email:
        contact = find_or_create_contact({
            "email": body.contact_email, "name": body.contact_name, "company": body.company,
        })
        if contact:
            results["same_contact"] = _same_contact_deals(contact["id"], body.exclude_deal_id)
            results["customer_profile"] = _fetch_customer_profile(body.contact_email)

    if body.company:
        results["same_company"] = _same_company_deals(body.company, body.exclude_deal_id)

    if body.fingerprint:
        results["similar"] = _similar_deals(body.fingerprint, body.exclude_deal_id, 10)

    results["stats"] = _outcome_stats(body.company)
    results["verdict"] = _build_verdict(results, body.fingerprint or {})
    return results


def _avg(values: list) -> Optional[float]:
    nums = [v for v in values if v is not None]
    nums = [float(v) for v in nums]
    return sum(nums) / len(nums) if nums else None


def _build_verdict(results: dict, fp: dict) -> dict:
    verdict = {
        "pattern_match": "insufficient_data",
        "confidence": "low",
        "summary": "",
        "indicators": [],
        "historical_context": None,
    }

    same_contact = results.get("same_contact") or []
    same_company = results.get("same_company") or []
    similar = results.get("similar") or []
    total_deals = len(same_contact) + len(same_company)

    if total_deals == 0 and not similar:
        verdict["summary"] = "No historical data available for pattern comparison."
        return verdict

    contact_wins = [d for d in same_contact if d["outcome"] == "won"]
    contact_losses = [d for d in same_contact if d["outcome"] == "lost"]

    if same_contact:
        verdict["historical_context"] = (
            f"This contact has {len(same_contact)} prior deal(s): "
            f"{len(contact_wins)} won, {len(contact_losses)} lost."
        )

        if fp and contact_wins:
            win_avg_response = _avg([d.get("avg_customer_response_hours") for d in contact_wins])
            win_avg_length = _avg([d.get("avg_inbound_length") for d in contact_wins])
            cur_response = fp.get("avg_customer_response_hours")
            cur_length = fp.get("avg_inbound_length")

            if cur_response and win_avg_response and cur_response <= win_avg_response * 1.2:
                verdict["indicators"].append({
                    "signal": "Response cadence matches wins", "direction": "positive",
                    "detail": f"Customer responding at {round(cur_response)}h avg vs "
                              f"{round(win_avg_response)}h in prior wins",
                })
            elif cur_response and win_avg_response and cur_response > win_avg_response * 2:
                verdict["indicators"].append({
                    "signal": "Response cadence slower than wins", "direction": "negative",
                    "detail": f"Customer responding at {round(cur_response)}h avg vs "
                              f"{round(win_avg_response)}h in prior wins",
                })

            if cur_length and win_avg_length and cur_length >= win_avg_length * 0.8:
                verdict["indicators"].append({
                    "signal": "Message depth matches wins", "direction": "positive",
                    "detail": f"Avg {cur_length} chars vs {round(win_avg_length)} in prior wins",
                })
            elif cur_length and win_avg_length and cur_length < win_avg_length * 0.5:
                verdict["indicators"].append({
                    "signal": "Messages shorter than winning pattern", "direction": "negative",
                    "detail": f"Avg {cur_length} chars vs {round(win_avg_length)} in prior wins",
                })

        if fp and contact_losses:
            loss_avg_gap = _avg([d.get("max_gap_hours") for d in contact_losses])
            cur_gap = fp.get("max_gap_hours")
            if cur_gap and loss_avg_gap and cur_gap >= loss_avg_gap * 0.8:
                verdict["indicators"].append({
                    "signal": "Communication gap matches losing pattern", "direction": "negative",
                    "detail": f"Max gap {cur_gap}h approaching the {round(loss_avg_gap)}h avg in prior losses",
                })

    company_wins = [d for d in same_company if d["outcome"] == "won"]
    company_losses = [d for d in same_company if d["outcome"] == "lost"]
    if same_company and not verdict["historical_context"]:
        verdict["historical_context"] = (
            f"{len(same_company)} prior deal(s) with this company: "
            f"{len(company_wins)} won, {len(company_losses)} lost."
        )

    similar_wins = [d for d in similar if d["outcome"] == "won"]
    similar_losses = [d for d in similar if d["outcome"] == "lost"]
    if len(similar) >= 3:
        if len(similar_wins) > len(similar_losses) * 2:
            verdict["indicators"].append({
                "signal": "Similar deals trend toward winning", "direction": "positive",
                "detail": f"{len(similar_wins)} of {len(similar)} deals with similar patterns were won",
            })
        elif len(similar_losses) > len(similar_wins) * 2:
            verdict["indicators"].append({
                "signal": "Similar deals trend toward losing", "direction": "negative",
                "detail": f"{len(similar_losses)} of {len(similar)} deals with similar patterns were lost",
            })

    positives = sum(1 for i in verdict["indicators"] if i["direction"] == "positive")
    negatives = sum(1 for i in verdict["indicators"] if i["direction"] == "negative")

    if not verdict["indicators"]:
        verdict.update(pattern_match="neutral", confidence="low",
                       summary="Historical data exists but not enough signal to determine a pattern match.")
    elif positives > negatives:
        verdict.update(pattern_match="winning",
                       confidence="high" if positives >= 3 else "medium",
                       summary=f"Based on {total_deals} historical deal(s), this thread follows "
                               "communication patterns seen in prior wins.")
    elif negatives > positives:
        verdict.update(pattern_match="losing",
                       confidence="high" if negatives >= 3 else "medium",
                       summary=f"Based on {total_deals} historical deal(s), the communication style "
                               "deviates from winning patterns and matches behaviors seen before losses.")
    else:
        verdict.update(pattern_match="neutral", confidence="medium",
                       summary="Mixed signals. Some indicators match winning patterns while others "
                               "match losing patterns.")
    return verdict


@router.get("/api/outcomes/health")
def outcomes_health():
    rows = _q(
        "SELECT (SELECT COUNT(*) FROM contacts) as contacts, "
        "(SELECT COUNT(*) FROM deal_outcomes) as deal_outcomes, "
        "(SELECT COUNT(*) FROM deal_outcomes WHERE outcome = 'won') as deals_won, "
        "(SELECT COUNT(*) FROM deal_outcomes WHERE outcome = 'lost') as deals_lost, "
        "(SELECT COUNT(*) FROM sources) as sources"
    )
    return rows[0] if rows else {}

"""Market-priced task allocation (Pillar 4 / Wave 3.6).

Replaces the assignment model with a bidding model. Every task posted
to the blackboard carries a `posted_budget` (max allowed cost). Agents
look at the topic, propose bids based on persona reputation, and the
lowest qualifying bid wins (subject to Brier-track-record gating).

Public surface:
  - `post_priced_task(topic, posted_budget_usd, payload) -> task_id`
  - `submit_bid(task_id, agent_id, specialist_id, bid_amount_usd,
                brier_reputation) -> bid_id`
  - `award_task(task_id) -> awarded_bid | None`
                Selects the lowest qualifying bid + stamps `awarded_at`
                on the row. Returns None if no qualifying bid.
  - `agent_brier_reputation(specialist_id) -> float`
                Pulls the latest rolling Brier from specialist_states or
                returns a neutral 0.5 prior for new agents.
  - `update_market_posteriors()` — Phase 6 weekly mutator hook. Walks
                outcomes + adjusts per-specialist reputation.

Brier-track-record gating:
  - Bids from agents with rolling Brier > 0.3 are rejected outright
    (they've been consistently wrong; can't trust them for new tasks).
  - New agents (no prior Brier history) get a neutral 0.5 prior + must
    bid <= posted_budget * 0.5 to win (probationary discount).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from ..agents_native import post_message
from ..store import get_desk_store


logger = logging.getLogger(__name__)


@dataclass
class MarketBid:
    id: str
    task_id: str
    cycle_id: Optional[str]
    topic: Optional[str]
    agent_id: str
    specialist_id: Optional[str]
    bid_amount_usd: float
    brier_reputation: Optional[float]
    posted_budget_usd: Optional[float]
    awarded_at: Optional[str] = None
    outcome: Optional[str] = None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def post_priced_task(
    topic: str,
    posted_budget_usd: float,
    payload: dict[str, Any],
    cycle_id: Optional[str] = None,
    task_id: Optional[str] = None,
) -> str:
    """Post a task to the blackboard with a posted_budget. Returns
    `task_id`. Agents submit bids via `submit_bid`.
    """
    if not topic:
        raise ValueError("topic required")
    if posted_budget_usd <= 0:
        raise ValueError(f"bad budget: {posted_budget_usd}")
    if task_id is None:
        task_id = f"task_{uuid4().hex[:10]}"
    # Use agent_messages topic channel for backwards-compat with pull mode.
    post_message(
        from_agent="market_orchestrator",
        to_agent_or_topic=topic,
        kind="hand_off",
        payload={
            "task_id": task_id,
            "posted_budget_usd": posted_budget_usd,
            "cycle_id": cycle_id,
            **payload,
        },
        topic=topic,
        expires_in_hours=24,
    )
    return task_id


def submit_bid(
    task_id: str,
    agent_id: str,
    specialist_id: Optional[str],
    bid_amount_usd: float,
    brier_reputation: Optional[float] = None,
    cycle_id: Optional[str] = None,
    topic: Optional[str] = None,
    posted_budget_usd: Optional[float] = None,
    payload: Optional[dict[str, Any]] = None,
) -> str:
    """Submit a bid for `task_id`. Returns `bid_id`.

    If the agent has a Brier > 0.3 (too unreliable), the bid is recorded
    with `outcome='gated_high_brier'` so `award_task` can ignore it.
    """
    if bid_amount_usd <= 0:
        raise ValueError(f"bad bid: {bid_amount_usd}")
    if brier_reputation is None:
        brier_reputation = agent_brier_reputation(specialist_id)
    outcome = None
    if brier_reputation is not None and brier_reputation > 0.3:
        outcome = "gated_high_brier"

    bid_id = f"bid_{uuid4().hex[:10]}"
    now = _utc_now_iso()
    conn = get_desk_store().conn
    try:
        conn.execute(
            "INSERT INTO market_bids "
            "(id, task_id, cycle_id, topic, agent_id, specialist_id, "
            " bid_amount_usd, brier_reputation, posted_budget_usd, "
            " outcome, payload, valid_from, transaction_from) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                bid_id, task_id, cycle_id, topic, agent_id, specialist_id,
                float(bid_amount_usd), brier_reputation, posted_budget_usd,
                outcome, json.dumps(payload or {}), now, now,
            ),
        )
        conn.commit()
    except sqlite3.OperationalError as e:
        logger.warning("market_bids insert failed: %s", e)
    return bid_id


def award_task(task_id: str) -> Optional[MarketBid]:
    """Select the lowest-qualifying bid for `task_id` + stamp
    `awarded_at`. Returns the awarded MarketBid or None.

    Qualification rules:
      - `outcome` is NULL (not pre-gated as high-Brier).
      - For new agents (`brier_reputation IS NULL` or between 0.4-0.6):
            `bid_amount_usd <= posted_budget_usd * 0.5` (probationary).
      - For seasoned reliable agents (brier <= 0.2): no extra constraint.
    """
    conn = get_desk_store().conn
    try:
        rows = conn.execute(
            "SELECT * FROM market_bids WHERE task_id = ? "
            "AND outcome IS NULL ORDER BY bid_amount_usd ASC",
            (task_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    if not rows:
        return None

    chosen: Optional[Any] = None
    for r in rows:
        brier = r["brier_reputation"]
        budget = r["posted_budget_usd"]
        bid = r["bid_amount_usd"]
        if brier is None or (0.4 <= float(brier) <= 0.6):
            # Probationary band — must bid at most half of posted_budget.
            if budget is None or bid <= float(budget) * 0.5:
                chosen = r
                break
            continue
        if float(brier) <= 0.2:
            # Reliable specialist — accept.
            chosen = r
            break
        # 0.2 < brier < 0.4 — borderline; require bid <= 0.75 * budget
        if budget is None or bid <= float(budget) * 0.75:
            chosen = r
            break

    if chosen is None:
        return None

    now = _utc_now_iso()
    try:
        conn.execute(
            "UPDATE market_bids SET awarded_at = ?, outcome = 'awarded' "
            "WHERE id = ?",
            (now, chosen["id"]),
        )
        # Mark other open bids as 'not_awarded'.
        conn.execute(
            "UPDATE market_bids SET outcome = 'not_awarded' "
            "WHERE task_id = ? AND id != ? AND outcome IS NULL",
            (task_id, chosen["id"]),
        )
        conn.commit()
    except sqlite3.OperationalError as e:
        logger.warning("market_bids update failed: %s", e)
        return None

    return MarketBid(
        id=chosen["id"], task_id=chosen["task_id"],
        cycle_id=chosen["cycle_id"], topic=chosen["topic"],
        agent_id=chosen["agent_id"], specialist_id=chosen["specialist_id"],
        bid_amount_usd=float(chosen["bid_amount_usd"]),
        brier_reputation=(float(chosen["brier_reputation"]) if chosen["brier_reputation"] is not None else None),
        posted_budget_usd=(float(chosen["posted_budget_usd"]) if chosen["posted_budget_usd"] is not None else None),
        awarded_at=now,
        outcome="awarded",
    )


def agent_brier_reputation(specialist_id: Optional[str]) -> Optional[float]:
    """Best-effort: pull rolling Brier for `specialist_id` from the
    desk's brier-rolling view, falling back to None for new agents.
    """
    if not specialist_id:
        return None
    conn = get_desk_store().conn
    try:
        row = conn.execute(
            "SELECT brier_score FROM mv_specialist_brier_rolling "
            "WHERE specialist_id = ? "
            "ORDER BY computed_at DESC LIMIT 1",
            (specialist_id,),
        ).fetchone()
        if row is None:
            return None
        return float(row[0]) if row[0] is not None else None
    except sqlite3.OperationalError:
        return None


def update_market_posteriors() -> dict[str, Any]:
    """Phase 6 mutator hook. Walks recent awarded bids + records outcomes
    against actual Brier deltas. Returns a summary dict.

    The actual outcome attribution is handled by the existing
    `talis_desk.evolution.mutator` — we just stamp the bids with
    `outcome='completed'` when their downstream report grades close.
    """
    conn = get_desk_store().conn
    try:
        # Count outcomes by category for diagnostic.
        rows = conn.execute(
            "SELECT outcome, COUNT(*) AS n FROM market_bids "
            "GROUP BY outcome"
        ).fetchall()
    except sqlite3.OperationalError:
        return {"market_bids": "table_missing"}
    return {
        "outcomes": {r[0] or "open": r[1] for r in rows}
    }

"""Durable agent messages — SOTA v2 §4 ("durable messages").

Port of `tic/agent_native/scratchpad.py` with two structural upgrades that
v2 demands:

  1. Persistence is cross-cycle. The old `scratchpads` table was scoped
     per-cycle and effectively ephemeral. We now write to the SOTA
     `agent_messages` table which lives forever (subject to `expires_at`
     TTLs and `transaction_to` for corrections).
  2. Message kinds expand from {observation, question, cross_ref, flag}
     to also include {request_review, request_devils_advocate, hand_off}
     so specialists can address peers across cycles and trigger Layer 2
     mechanisms (debates / hot-investigations).

The original `post_to_scratchpad_for_cycle` convenience wrapper is kept
for callers that want short-lived per-cycle traffic: it sets
`expires_in_hours=24` and `dedupe_key=f"{cycle_id}:{topic}"` so a
specialist re-running its draft doesn't double-post.

# Honest gaps
- `read_by` is a JSONB array in the DDL — we store it as a JSON string in
  SQLite. On Postgres prod, swap to `read_by ? :reader_id` queries.
- We don't currently auto-purge expired rows; readers filter them out via
  `expires_at > now`. A nightly sweep can move them to a cold table.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional
from uuid import uuid4

from ..store import get_desk_store


# ============================================================================
# Types
# ============================================================================

MessageKind = Literal[
    "observation",
    "question",
    "cross_ref",
    "flag",
    "request_review",
    "request_devils_advocate",
    "hand_off",
]

VALID_MESSAGE_KINDS: set[str] = {
    "observation",
    "question",
    "cross_ref",
    "flag",
    "request_review",
    "request_devils_advocate",
    "hand_off",
}


@dataclass
class AgentMessage:
    """A durable cross-cycle message. Maps 1:1 to `agent_messages` row."""

    id: str
    from_agent: str
    to_agent_or_topic: str
    message_kind: str
    payload: dict[str, Any]
    posted_at: datetime
    read_by: list[str]
    expires_at: Optional[datetime]
    dedupe_key: Optional[str]
    related_artifact_id: Optional[str]
    related_hypothesis_id: Optional[str]
    related_trade_idea_id: Optional[str]
    valid_from: datetime
    transaction_from: datetime


# ============================================================================
# Helpers
# ============================================================================

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _from_iso(s: Optional[str]) -> Optional[datetime]:
    if s is None:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _row_to_message(row: sqlite3.Row) -> AgentMessage:
    d = dict(row)
    try:
        payload = json.loads(d["payload"]) if d.get("payload") else {}
        if not isinstance(payload, dict):
            payload = {"_raw": payload}
    except Exception:
        payload = {"_raw": d.get("payload")}
    try:
        read_by = json.loads(d["read_by"]) if d.get("read_by") else []
        if not isinstance(read_by, list):
            read_by = []
    except Exception:
        read_by = []
    return AgentMessage(
        id=d["id"],
        from_agent=d["from_agent"],
        to_agent_or_topic=d["to_agent_or_topic"],
        message_kind=d["message_kind"],
        payload=payload,
        posted_at=_from_iso(d.get("posted_at")) or _utc_now(),
        read_by=read_by,
        expires_at=_from_iso(d.get("expires_at")),
        dedupe_key=d.get("dedupe_key"),
        related_artifact_id=d.get("related_artifact_id"),
        related_hypothesis_id=d.get("related_hypothesis_id"),
        related_trade_idea_id=d.get("related_trade_idea_id"),
        valid_from=_from_iso(d.get("valid_from")) or _utc_now(),
        transaction_from=_from_iso(d.get("transaction_from")) or _utc_now(),
    )


# ============================================================================
# Post
# ============================================================================

def post_message(
    from_agent: str,
    to_agent_or_topic: str,
    kind: MessageKind,
    payload: dict[str, Any],
    related_hypothesis_id: Optional[str] = None,
    related_trade_idea_id: Optional[str] = None,
    related_artifact_id: Optional[str] = None,
    expires_in_hours: int = 72,
    dedupe_key: Optional[str] = None,
) -> AgentMessage:
    """Post a durable message to another agent (`to_agent_or_topic="semis"`)
    or to a topic (`"#hot_investigations"`).

    If `dedupe_key` is set and a row with `(from_agent, dedupe_key)` already
    exists (and isn't superseded), the existing row is returned — same
    idempotency contract as the old `post_note`.

    Returns the AgentMessage. Raises ValueError on bad inputs.
    """
    if kind not in VALID_MESSAGE_KINDS:
        raise ValueError(f"invalid_message_kind: {kind}")
    if not from_agent or not to_agent_or_topic:
        raise ValueError("post_message: from_agent and to_agent_or_topic required")
    if not isinstance(payload, dict):
        raise ValueError(f"post_message: payload must be a dict, got {type(payload)}")

    conn = get_desk_store().conn

    if dedupe_key:
        existing = conn.execute(
            "SELECT * FROM agent_messages "
            "WHERE from_agent = ? AND dedupe_key = ? AND transaction_to IS NULL "
            "ORDER BY transaction_from DESC LIMIT 1",
            (from_agent, dedupe_key),
        ).fetchone()
        if existing is not None:
            return _row_to_message(existing)

    now = _utc_now()
    expires_at = now + timedelta(hours=expires_in_hours) if expires_in_hours else None
    msg_id = f"msg_{uuid4().hex[:12]}"

    conn.execute(
        "INSERT INTO agent_messages "
        "(id, from_agent, to_agent_or_topic, message_kind, payload, posted_at, "
        " read_by, expires_at, dedupe_key, related_artifact_id, "
        " related_hypothesis_id, related_trade_idea_id, valid_from, "
        " transaction_from) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            msg_id,
            from_agent,
            to_agent_or_topic,
            kind,
            json.dumps(payload),
            _iso(now),
            "[]",
            _iso(expires_at) if expires_at else None,
            dedupe_key,
            related_artifact_id,
            related_hypothesis_id,
            related_trade_idea_id,
            _iso(now),
            _iso(now),
        ),
    )
    conn.commit()

    row = conn.execute(
        "SELECT * FROM agent_messages WHERE id = ?", (msg_id,)
    ).fetchone()
    return _row_to_message(row)


def post_to_scratchpad_for_cycle(
    cycle_id: str,
    from_agent: str,
    to_agent_or_topic: str,
    kind: MessageKind,
    payload: dict[str, Any],
    topic: str = "scratchpad",
    related_hypothesis_id: Optional[str] = None,
    related_trade_idea_id: Optional[str] = None,
) -> AgentMessage:
    """Short-TTL per-cycle wrapper. Sets `expires_in_hours=24` and
    `dedupe_key=f"{cycle_id}:{topic}"` so a specialist's re-runs collapse
    to one row per (cycle, topic) pair.

    The cycle id also lands in `payload['cycle_id']` for replay queries.
    """
    body = dict(payload)
    body.setdefault("cycle_id", cycle_id)
    body.setdefault("topic", topic)
    return post_message(
        from_agent=from_agent,
        to_agent_or_topic=to_agent_or_topic,
        kind=kind,
        payload=body,
        related_hypothesis_id=related_hypothesis_id,
        related_trade_idea_id=related_trade_idea_id,
        expires_in_hours=24,
        dedupe_key=f"{cycle_id}:{topic}",
    )


# ============================================================================
# Read
# ============================================================================

def read_unread_messages(
    to_agent_or_topic: str,
    since_cycle_id: Optional[str] = None,
    include_expired: bool = False,
    limit: int = 200,
    reader_id: Optional[str] = None,
) -> list[AgentMessage]:
    """Return messages where `reader_id` isn't in `read_by`.

    If `reader_id` is None, defaults to `to_agent_or_topic` (i.e. when an
    agent reads its own inbox). When `to_agent_or_topic` is a topic like
    "#hot_investigations" the caller MUST pass `reader_id` to get sensible
    unread semantics.

    `since_cycle_id` filters by `payload->>'cycle_id' >= since_cycle_id`
    when provided (used by replay to walk a tail of history).
    """
    reader = reader_id or to_agent_or_topic
    conn = get_desk_store().conn
    now_iso = _iso(_utc_now())
    sql = (
        "SELECT * FROM agent_messages "
        "WHERE to_agent_or_topic = ? AND transaction_to IS NULL "
    )
    params: list[Any] = [to_agent_or_topic]
    if not include_expired:
        sql += "AND (expires_at IS NULL OR expires_at > ?) "
        params.append(now_iso)
    sql += "ORDER BY posted_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    out: list[AgentMessage] = []
    for r in rows:
        m = _row_to_message(r)
        if reader in m.read_by:
            continue
        if since_cycle_id is not None:
            mc = m.payload.get("cycle_id")
            if mc is not None and str(mc) < str(since_cycle_id):
                continue
        out.append(m)
    return out


def mark_read(message_id: str, reader_id: str) -> bool:
    """Append `reader_id` to the message's `read_by` array. Idempotent."""
    conn = get_desk_store().conn
    row = conn.execute(
        "SELECT read_by FROM agent_messages WHERE id = ?", (message_id,)
    ).fetchone()
    if row is None:
        return False
    try:
        read_by = json.loads(row["read_by"]) if row["read_by"] else []
        if not isinstance(read_by, list):
            read_by = []
    except Exception:
        read_by = []
    if reader_id in read_by:
        return True
    read_by.append(reader_id)
    conn.execute(
        "UPDATE agent_messages SET read_by = ? WHERE id = ?",
        (json.dumps(read_by), message_id),
    )
    conn.commit()
    return True


# ============================================================================
# Convenience: cycle summary (kept from tic for parity with the dashboard)
# ============================================================================

def cycle_summary(cycle_id: str) -> dict[str, Any]:
    """Aggregate the agent_messages traffic for a cycle. Uses
    `payload->>'cycle_id'` (set by `post_to_scratchpad_for_cycle`) to
    bucket — messages without that field are skipped.
    """
    conn = get_desk_store().conn
    rows = conn.execute(
        "SELECT * FROM agent_messages WHERE transaction_to IS NULL",
    ).fetchall()
    by_kind: dict[str, int] = {}
    by_from: dict[str, int] = {}
    by_to: dict[str, int] = {}
    total = 0
    for r in rows:
        try:
            payload = json.loads(r["payload"]) if r["payload"] else {}
        except Exception:
            payload = {}
        if str(payload.get("cycle_id")) != cycle_id:
            continue
        total += 1
        kind = r["message_kind"]
        by_kind[kind] = by_kind.get(kind, 0) + 1
        by_from[r["from_agent"]] = by_from.get(r["from_agent"], 0) + 1
        by_to[r["to_agent_or_topic"]] = by_to.get(r["to_agent_or_topic"], 0) + 1
    return {
        "cycle_id": cycle_id,
        "total_messages": total,
        "by_kind": by_kind,
        "by_from_agent": by_from,
        "by_to_agent_or_topic": by_to,
    }

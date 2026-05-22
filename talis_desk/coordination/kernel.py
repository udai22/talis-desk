"""Typed coordination helpers for the v5 research kernel.

The old desk mostly coordinated through specialist order and durable
`agent_messages`. The v5 kernel needs stricter contracts: every task,
claim, verifier decision, coverage mark, and failure reason should be
machine-readable and auditable.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from ..store import get_desk_store


JsonDict = dict[str, Any]


def post_task(
    *,
    topic: str,
    title: str,
    description: str = "",
    cycle_id: Optional[str] = None,
    priority: float = 0.0,
    budget_usd: float = 0.0,
    ttl_seconds: Optional[int] = None,
    input_schema: Optional[JsonDict] = None,
    allowed_tools: Optional[list[str]] = None,
    evidence_requirements: Optional[list[str]] = None,
    promotion_criteria: Optional[JsonDict] = None,
    kill_criteria: Optional[JsonDict] = None,
    coverage_cell_key: Optional[str] = None,
    parent_task_id: Optional[str] = None,
    payload: Optional[JsonDict] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> str:
    """Post a schedulable task contract and matching blackboard event."""
    db = conn or get_desk_store().conn
    task_id = _id("task")
    now = _now()
    db.execute(
        """
        INSERT INTO task_contracts (
            id, cycle_id, topic, title, description, status, priority,
            budget_usd, ttl_seconds, input_schema_json, allowed_tools_json,
            evidence_requirements_json, promotion_criteria_json,
            kill_criteria_json, coverage_cell_key, parent_task_id, posted_at,
            payload, valid_from, transaction_from
        ) VALUES (?, ?, ?, ?, ?, 'posted', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task_id,
            cycle_id,
            topic,
            title,
            description,
            float(priority),
            float(budget_usd),
            ttl_seconds,
            _json(input_schema or {}),
            _json(allowed_tools or []),
            _json(evidence_requirements or []),
            _json(promotion_criteria or {}),
            _json(kill_criteria or {}),
            coverage_cell_key,
            parent_task_id,
            now,
            _json(payload or {}),
            now,
            now,
        ),
    )
    append_blackboard_event(
        event_type="task.posted",
        cycle_id=cycle_id,
        topic=topic,
        task_id=task_id,
        payload={
            "title": title,
            "budget_usd": budget_usd,
            "ttl_seconds": ttl_seconds,
            "coverage_cell_key": coverage_cell_key,
        },
        conn=db,
    )
    return task_id


def claim_task(
    task_id: str,
    *,
    agent_id: str,
    specialist_id: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> bool:
    """Atomically claim a posted task.

    Returns False if another agent already claimed/completed/killed it.
    """
    db = conn or get_desk_store().conn
    now = _now()
    cur = db.execute(
        """
        UPDATE task_contracts
        SET status = 'claimed',
            owner_agent_id = ?,
            owner_specialist_id = ?,
            claimed_at = ?
        WHERE id = ? AND status = 'posted'
        """,
        (agent_id, specialist_id, now, task_id),
    )
    if cur.rowcount != 1:
        return False
    row = db.execute(
        "SELECT cycle_id, topic FROM task_contracts WHERE id = ?",
        (task_id,),
    ).fetchone()
    append_blackboard_event(
        event_type="task.claimed",
        cycle_id=_row_get(row, "cycle_id"),
        topic=_row_get(row, "topic"),
        task_id=task_id,
        agent_id=agent_id,
        specialist_id=specialist_id,
        payload={},
        conn=db,
    )
    return True


def start_task(
    task_id: str,
    *,
    agent_id: Optional[str] = None,
    specialist_id: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> bool:
    return _transition_task(
        task_id,
        from_statuses={"claimed"},
        to_status="running",
        event_type="task.started",
        agent_id=agent_id,
        specialist_id=specialist_id,
        conn=conn,
    )


def complete_task(
    task_id: str,
    *,
    agent_id: Optional[str] = None,
    specialist_id: Optional[str] = None,
    payload: Optional[JsonDict] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> bool:
    return _transition_task(
        task_id,
        from_statuses={"claimed", "running"},
        to_status="completed",
        event_type="task.completed",
        agent_id=agent_id,
        specialist_id=specialist_id,
        payload=payload,
        completed=True,
        conn=conn,
    )


def fail_task(
    task_id: str,
    *,
    agent_id: Optional[str] = None,
    specialist_id: Optional[str] = None,
    reason: str = "",
    payload: Optional[JsonDict] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> bool:
    return _transition_task(
        task_id,
        from_statuses={"posted", "claimed", "running"},
        to_status="failed",
        event_type="task.failed",
        agent_id=agent_id,
        specialist_id=specialist_id,
        payload={"reason": reason, **(payload or {})},
        completed=True,
        conn=conn,
    )


def kill_task(
    task_id: str,
    *,
    agent_id: Optional[str] = None,
    specialist_id: Optional[str] = None,
    reason: str = "",
    payload: Optional[JsonDict] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> bool:
    return _transition_task(
        task_id,
        from_statuses={"posted", "claimed", "running"},
        to_status="killed",
        event_type="task.killed",
        agent_id=agent_id,
        specialist_id=specialist_id,
        payload={"reason": reason, **(payload or {})},
        completed=True,
        conn=conn,
    )


def promote_task(
    task_id: str,
    *,
    agent_id: Optional[str] = None,
    specialist_id: Optional[str] = None,
    payload: Optional[JsonDict] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> bool:
    return _transition_task(
        task_id,
        from_statuses={"claimed", "running", "completed"},
        to_status="promoted",
        event_type="task.promoted",
        agent_id=agent_id,
        specialist_id=specialist_id,
        payload=payload,
        completed=True,
        conn=conn,
    )


def expire_task(
    task_id: str,
    *,
    agent_id: Optional[str] = "system:ttl_sweeper",
    specialist_id: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> bool:
    return _transition_task(
        task_id,
        from_statuses={"posted", "claimed", "running"},
        to_status="expired",
        event_type="task.expired",
        agent_id=agent_id,
        specialist_id=specialist_id,
        completed=True,
        conn=conn,
    )


def expire_overdue_tasks(
    *,
    now_iso: Optional[str] = None,
    limit: int = 500,
    conn: Optional[sqlite3.Connection] = None,
) -> int:
    """Expire tasks whose posted_at + ttl_seconds is in the past."""
    db = conn or get_desk_store().conn
    now = now_iso or _now()
    rows = db.execute(
        """
        SELECT id FROM task_contracts
        WHERE status IN ('posted','claimed','running')
          AND ttl_seconds IS NOT NULL
          AND datetime(posted_at, '+' || ttl_seconds || ' seconds') <= datetime(?)
        ORDER BY posted_at ASC
        LIMIT ?
        """,
        (now, int(limit)),
    ).fetchall()
    n = 0
    for row in rows:
        if expire_task(_row_get(row, "id"), conn=db):
            n += 1
    return n


def tally_votes(
    claim_id: str,
    *,
    task_id: Optional[str] = None,
    min_families: int = 3,
    threshold: int = 2,
    conn: Optional[sqlite3.Connection] = None,
) -> JsonDict:
    """Return the practical verifier-gate decision for a claim.

    This is not formal Byzantine consensus. It is the desk's measured
    evidence gate: pass/fail/needs_review/abstain votes across model
    families, requiring `threshold` agreeing votes.
    """
    db = conn or get_desk_store().conn
    where = "claim_id = ? AND transaction_to IS NULL"
    params: list[Any] = [claim_id]
    if task_id is not None:
        where += " AND task_id = ?"
        params.append(task_id)
    rows = db.execute(
        f"""
        SELECT vote, model_family, confidence
        FROM claim_votes
        WHERE {where}
        ORDER BY voted_at DESC
        """,
        tuple(params),
    ).fetchall()
    counts = {"pass": 0, "fail": 0, "abstain": 0, "needs_review": 0}
    families: set[str] = set()
    confidences: list[float] = []
    for r in rows:
        vote = str(_row_get(r, "vote") or "abstain")
        if vote in counts:
            counts[vote] += 1
        family = _row_get(r, "model_family")
        if family:
            families.add(str(family))
        conf = _row_get(r, "confidence")
        if conf is not None:
            try:
                confidences.append(float(conf))
            except Exception:
                pass
    if counts["pass"] >= threshold and counts["pass"] > counts["fail"]:
        decision = "promote"
    elif counts["fail"] >= threshold and counts["fail"] > counts["pass"]:
        decision = "reject"
    elif counts["needs_review"] > 0:
        decision = "needs_review"
    else:
        decision = "abstain"
    flags: list[str] = []
    if len(families) < min_families:
        flags.append("partial_model_family_quorum")
    if not rows:
        flags.append("no_votes")
    return {
        "claim_id": claim_id,
        "task_id": task_id,
        "decision": decision,
        "counts": counts,
        "n_votes": len(rows),
        "families": sorted(families),
        "avg_confidence": (
            sum(confidences) / len(confidences) if confidences else None
        ),
        "quality_flags": flags,
    }


def append_blackboard_event(
    *,
    event_type: str,
    cycle_id: Optional[str] = None,
    topic: Optional[str] = None,
    task_id: Optional[str] = None,
    claim_id: Optional[str] = None,
    parent_event_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    specialist_id: Optional[str] = None,
    payload: Optional[JsonDict] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> str:
    """Append a typed blackboard event."""
    db = conn or get_desk_store().conn
    event_id = _id("bbe")
    now = _now()
    db.execute(
        """
        INSERT INTO blackboard_events (
            id, event_type, cycle_id, topic, task_id, claim_id,
            parent_event_id, agent_id, specialist_id, payload,
            occurred_at, valid_from, transaction_from
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            event_type,
            cycle_id,
            topic,
            task_id,
            claim_id,
            parent_event_id,
            agent_id,
            specialist_id,
            _json(payload or {}),
            now,
            now,
            now,
        ),
    )
    return event_id


def _transition_task(
    task_id: str,
    *,
    from_statuses: set[str],
    to_status: str,
    event_type: str,
    agent_id: Optional[str] = None,
    specialist_id: Optional[str] = None,
    payload: Optional[JsonDict] = None,
    completed: bool = False,
    conn: Optional[sqlite3.Connection] = None,
) -> bool:
    db = conn or get_desk_store().conn
    now = _now()
    placeholders = ",".join("?" for _ in from_statuses)
    params: list[Any] = [to_status]
    set_clause = "status = ?"
    if completed:
        set_clause += ", completed_at = ?"
        params.append(now)
    params.extend(sorted(from_statuses))
    params.append(task_id)
    cur = db.execute(
        f"""
        UPDATE task_contracts
        SET {set_clause}
        WHERE status IN ({placeholders}) AND id = ?
        """,
        tuple(params),
    )
    if cur.rowcount != 1:
        return False
    row = db.execute(
        "SELECT cycle_id, topic FROM task_contracts WHERE id = ?",
        (task_id,),
    ).fetchone()
    append_blackboard_event(
        event_type=event_type,
        cycle_id=_row_get(row, "cycle_id"),
        topic=_row_get(row, "topic"),
        task_id=task_id,
        agent_id=agent_id,
        specialist_id=specialist_id,
        payload=payload or {},
        conn=db,
    )
    return True


def record_claim_vote(
    *,
    claim_id: str,
    verifier_agent_id: str,
    vote: str,
    task_id: Optional[str] = None,
    cycle_id: Optional[str] = None,
    verifier_specialist_id: Optional[str] = None,
    model_family: Optional[str] = None,
    confidence: Optional[float] = None,
    rationale: str = "",
    citation_ids: Optional[list[str]] = None,
    evidence_hash: Optional[str] = None,
    payload: Optional[JsonDict] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> str:
    """Record one independent verifier vote for a proposed claim."""
    if vote not in {"pass", "fail", "abstain", "needs_review"}:
        raise ValueError(f"invalid claim vote: {vote!r}")
    db = conn or get_desk_store().conn
    vote_id = _id("vote")
    now = _now()
    db.execute(
        """
        INSERT INTO claim_votes (
            id, claim_id, task_id, cycle_id, verifier_agent_id,
            verifier_specialist_id, model_family, vote, confidence,
            rationale, citation_ids, evidence_hash, voted_at, payload,
            valid_from, transaction_from
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            vote_id,
            claim_id,
            task_id,
            cycle_id,
            verifier_agent_id,
            verifier_specialist_id,
            model_family,
            vote,
            confidence,
            rationale,
            _json(citation_ids or []),
            evidence_hash,
            now,
            _json(payload or {}),
            now,
            now,
        ),
    )
    append_blackboard_event(
        event_type="verification.vote",
        cycle_id=cycle_id,
        task_id=task_id,
        claim_id=claim_id,
        agent_id=verifier_agent_id,
        specialist_id=verifier_specialist_id,
        payload={"vote": vote, "confidence": confidence},
        conn=db,
    )
    return vote_id


def touch_coverage_cell(
    *,
    entity: Optional[str],
    horizon: Optional[str],
    lens: Optional[str],
    source: Optional[str] = None,
    bias_mode: Optional[str] = None,
    theme: Optional[str] = None,
    promoted: bool = False,
    killed: bool = False,
    novelty_score: Optional[float] = None,
    density_score: Optional[float] = None,
    expected_value_usd: Optional[float] = None,
    payload: Optional[JsonDict] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> str:
    """Upsert the anti-duplication cell used by scout allocation."""
    db = conn or get_desk_store().conn
    key = coverage_cell_key(
        entity=entity,
        horizon=horizon,
        lens=lens,
        source=source,
        bias_mode=bias_mode,
        theme=theme,
    )
    now = _now()
    db.execute(
        """
        INSERT INTO coverage_cells (
            cell_key, entity, horizon, lens, source, bias_mode, theme,
            n_samples, n_promoted, n_killed, novelty_score, density_score,
            expected_value_usd, last_sampled_at, last_promoted_at, payload,
            valid_from, transaction_from
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(cell_key) DO UPDATE SET
            n_samples = n_samples + 1,
            n_promoted = n_promoted + excluded.n_promoted,
            n_killed = n_killed + excluded.n_killed,
            novelty_score = COALESCE(excluded.novelty_score, coverage_cells.novelty_score),
            density_score = COALESCE(excluded.density_score, coverage_cells.density_score),
            expected_value_usd = COALESCE(excluded.expected_value_usd, coverage_cells.expected_value_usd),
            last_sampled_at = excluded.last_sampled_at,
            last_promoted_at = COALESCE(excluded.last_promoted_at, coverage_cells.last_promoted_at),
            payload = excluded.payload
        """,
        (
            key,
            entity,
            horizon,
            lens,
            source,
            bias_mode,
            theme,
            1 if promoted else 0,
            1 if killed else 0,
            novelty_score,
            density_score,
            expected_value_usd,
            now,
            now if promoted else None,
            _json(payload or {}),
            now,
            now,
        ),
    )
    return key


def attribute_failure(
    *,
    artifact_kind: str,
    artifact_id: str,
    failure_kind: str,
    cycle_id: Optional[str] = None,
    task_id: Optional[str] = None,
    specialist_id: Optional[str] = None,
    severity: str = "yellow",
    rationale: str = "",
    source_event_id: Optional[str] = None,
    payload: Optional[JsonDict] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> str:
    """Record why a task/claim/report/thesis died or was downgraded."""
    if severity not in {"green", "yellow", "red"}:
        raise ValueError(f"invalid severity: {severity!r}")
    db = conn or get_desk_store().conn
    failure_id = _id("fail")
    now = _now()
    db.execute(
        """
        INSERT INTO failure_attributions (
            id, artifact_kind, artifact_id, cycle_id, task_id, specialist_id,
            failure_kind, severity, rationale, source_event_id, payload,
            attributed_at, valid_from, transaction_from
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            failure_id,
            artifact_kind,
            artifact_id,
            cycle_id,
            task_id,
            specialist_id,
            failure_kind,
            severity,
            rationale,
            source_event_id,
            _json(payload or {}),
            now,
            now,
            now,
        ),
    )
    append_blackboard_event(
        event_type="failure.attributed",
        cycle_id=cycle_id,
        task_id=task_id,
        agent_id=specialist_id,
        specialist_id=specialist_id,
        payload={
            "failure_id": failure_id,
            "artifact_kind": artifact_kind,
            "artifact_id": artifact_id,
            "failure_kind": failure_kind,
            "severity": severity,
        },
        conn=db,
    )
    return failure_id


def coverage_cell_key(
    *,
    entity: Optional[str],
    horizon: Optional[str],
    lens: Optional[str],
    source: Optional[str] = None,
    bias_mode: Optional[str] = None,
    theme: Optional[str] = None,
) -> str:
    """Stable key for an entity x horizon x lens x source x bias cell."""
    parts = [
        _norm(entity),
        _norm(horizon),
        _norm(lens),
        _norm(source),
        _norm(bias_mode),
        _norm(theme),
    ]
    raw = "|".join(parts)
    digest = hashlib.sha256(raw.encode()).hexdigest()[:10]
    readable = "__".join(p for p in parts[:3] if p and p != "any") or "market"
    return f"cell_{readable}_{digest}"


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _norm(value: Optional[str]) -> str:
    text = str(value or "any").strip().lower()
    out = "".join(ch if ch.isalnum() else "_" for ch in text)
    while "__" in out:
        out = out.replace("__", "_")
    return out.strip("_") or "any"


def _row_get(row: Any, key: str) -> Any:
    if row is None:
        return None
    try:
        return row[key]
    except Exception:
        return None

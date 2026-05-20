"""Persistence helpers for `ResearchReport` rows.

Bitemporal append-only writes against the desk.db `research_reports`
table (migration v5). Mirrors the patterns used in
`talis_desk.trade_ideas.model._insert_row` and
`talis_desk.trade_ideas.candidates.emit_*`.
"""
from __future__ import annotations

import sqlite3
import warnings
from datetime import datetime
from typing import Any, Iterable, Optional

from .model import (
    ResearchReport,
    _canonical_json,
    _iso,
    _utc_now,
)


def _conn_from_context(context: Any) -> sqlite3.Connection:
    """Resolve the desk.db connection. Prefer the caller's explicit
    `context.conn` / `context.desk_store`; else fall back to the singleton
    DeskStore (the canonical pattern other emit helpers follow)."""
    if context is not None and getattr(context, "conn", None) is not None:
        return context.conn  # type: ignore[no-any-return]
    if context is not None and getattr(context, "desk_store", None) is not None:
        return context.desk_store.conn
    from ..store import get_desk_store
    return get_desk_store().conn


def _existing_row(conn: sqlite3.Connection, report_id: str) -> Optional[dict[str, Any]]:
    row = conn.execute(
        "SELECT id FROM research_reports WHERE id = ?",
        (report_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def emit_research_report(
    report: ResearchReport,
    context: Any = None,
) -> ResearchReport:
    """Insert one ResearchReport row, bitemporally.

    Idempotent on `report.id` — re-emitting the same id is a no-op
    (returns the report unchanged). Callers that want a *revision* should
    mint a fresh id (`new_report_id()`) and stash `supersedes_id` in
    `report.payload` so the lineage is auditable.
    """
    conn = _conn_from_context(context)
    if _existing_row(conn, report.id) is not None:
        return report

    now_dt = _utc_now()
    valid_from = report.valid_from or now_dt
    transaction_from = report.transaction_from or now_dt
    revised_at = report.revised_at or now_dt

    try:
        conn.execute(
            "INSERT INTO research_reports ("
            "id, specialist_id, cycle_id, hypothesis_id, instrument, "
            "report_kind, title, abstract, body_md, edge_thesis, "
            "contradicting_evidence, citation_claim_ids, citation_tool_call_ids, "
            "primary_artifact_id, confidence, novelty_score, quality_flags, "
            "reviewer_turns, adversarial_severity, revised_at, cost_usd, "
            "payload, valid_from, valid_to, transaction_from, transaction_to"
            ") VALUES ("
            "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?, ?, ?"
            ")",
            (
                report.id,
                report.specialist_id,
                report.cycle_id,
                report.hypothesis_id or None,
                report.instrument or None,
                report.report_kind,
                report.title[:120],
                (report.abstract or "")[:400],
                report.body_md or "",
                report.edge_thesis or "",
                _canonical_json(list(report.contradicting_evidence or [])),
                _canonical_json(list(report.citation_claim_ids or [])),
                _canonical_json(list(report.citation_tool_call_ids or [])),
                report.primary_artifact_id,
                float(report.confidence or 0.0),
                (float(report.novelty_score)
                 if report.novelty_score is not None else None),
                _canonical_json(list(report.quality_flags or [])),
                _canonical_json(list(report.reviewer_turns or [])),
                report.adversarial_severity,
                _iso(revised_at),
                float(report.cost_usd or 0.0),
                _canonical_json(dict(report.payload or {})),
                _iso(valid_from),
                None,
                _iso(transaction_from),
                None,
            ),
        )
    except Exception as e:  # pragma: no cover - best-effort persist
        warnings.warn(f"emit_research_report: insert failed for {report.id}: {e}")
        raise

    return report


# ============================================================================
# Read helpers — used by the brief composer + ad-hoc audits.
# ============================================================================

def _row_to_report_dict(row: sqlite3.Row) -> dict[str, Any]:
    import json
    d = dict(row)
    for k in (
        "contradicting_evidence",
        "citation_claim_ids",
        "citation_tool_call_ids",
        "quality_flags",
        "reviewer_turns",
        "payload",
    ):
        v = d.get(k)
        if isinstance(v, str):
            try:
                d[k] = json.loads(v)
            except Exception:
                d[k] = v
    return d


def fetch_reports_for_cycle(
    cycle_ids: Iterable[str],
    conn: Optional[sqlite3.Connection] = None,
    *,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """All research_reports rows tied to any of `cycle_ids`, newest first.

    Ranked for the brief's TOC: severity green > yellow > red, then
    confidence DESC, then novelty_score DESC NULLS LAST, then transaction
    time DESC.
    """
    if conn is None:
        from ..store import get_desk_store
        conn = get_desk_store().conn
    ids = [c for c in (cycle_ids or []) if c]
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    try:
        rows = conn.execute(
            f"SELECT * FROM research_reports "
            f"WHERE cycle_id IN ({placeholders}) "
            f"AND transaction_to IS NULL "
            f"ORDER BY "
            # green (0) < yellow (1) < red (2)
            f"  CASE adversarial_severity "
            f"    WHEN 'green' THEN 0 WHEN 'yellow' THEN 1 ELSE 2 END ASC, "
            f"  confidence DESC, "
            f"  COALESCE(novelty_score, -1) DESC, "
            f"  transaction_from DESC "
            f"LIMIT ?",
            (*ids, int(limit)),
        ).fetchall()
    except sqlite3.Error as e:
        warnings.warn(f"fetch_reports_for_cycle failed: {e}")
        return []
    return [_row_to_report_dict(r) for r in rows]


def fetch_reports_by_kind(
    kind: str,
    since: datetime,
    conn: Optional[sqlite3.Connection] = None,
    *,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """All reports of a given kind with `transaction_from >= since`."""
    if conn is None:
        from ..store import get_desk_store
        conn = get_desk_store().conn
    try:
        rows = conn.execute(
            "SELECT * FROM research_reports "
            "WHERE report_kind = ? "
            "AND transaction_to IS NULL "
            "AND transaction_from >= ? "
            "ORDER BY transaction_from DESC LIMIT ?",
            (kind, _iso(since), int(limit)),
        ).fetchall()
    except sqlite3.Error as e:
        warnings.warn(f"fetch_reports_by_kind failed: {e}")
        return []
    return [_row_to_report_dict(r) for r in rows]


__all__ = [
    "emit_research_report",
    "fetch_reports_for_cycle",
    "fetch_reports_by_kind",
]

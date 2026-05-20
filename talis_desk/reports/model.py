"""ResearchReport — the desk's adversarial-pipeline output artifact.

Every surviving hypothesis (supported / candidate-promoted / watchlist /
blocked) becomes a `ResearchReport` after the three-stage LLM pipeline
(researcher -> adversarial critic -> revision) in
`talis_desk.reports.pipeline`. The daily brief composes its
narrative + table-of-contents from these rows instead of stitching
together raw hypotheses + ideas; the report carries the body_md, the
adversarial transcript, and the citation lineage.

# Why a dedicated artifact

  - Hypotheses + trade ideas are *structured* records; they carry the
    edge claim, citations, and sizing but no prose. Pre-pipeline, the
    brief had to template prose from those records and the result read
    like a status report, not research.
  - A serious institutional research piece needs the *adversarial trace*
    (critic verdict + revision) attached for audit. Stashing that on
    `trade_ideas.payload` would muddy the trade-idea contract.
  - 70+ reports/day means we need a dedicated index + bitemporal
    append-only contract on the reports table (per the schema migration
    in v5).

# Bitemporal contract

Every write goes through `emit_research_report` which inserts a new row
with `valid_from` + `transaction_from` stamped to `now()`. No mutation —
revisions append a fresh row with `supersedes` (recorded inside
`payload`) so the lineage is auditable.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Optional


# ============================================================================
# Enumerations
# ============================================================================

#: All report kinds the pipeline can emit. Maps roughly to which survivor
#: triggered the pipeline:
#:   trade_idea       — a TradeIdea was emitted (status=published)
#:   watchlist        — only a WatchlistSetup row was produced
#:   blocked_thesis   — a BlockedIdea row OR the critic graded RED (abandoned)
#:   regime_change    — hypothesis encodes a regime-shift claim
#:   anomaly_flag     — hypothesis encodes a one-off anomaly
#:   rotation_call    — relative-value / sector rotation
#:   vol_arb          — volatility-arb / funding-arb structure
#:   pair_trade       — explicit long/short pair
ReportKind = Literal[
    "trade_idea", "watchlist", "regime_change", "anomaly_flag",
    "rotation_call", "vol_arb", "pair_trade", "blocked_thesis",
]

#: Critic verdicts. green = ship; yellow = revise then ship; red =
#: thesis is fundamentally flawed (the revision stage may abandon, in
#: which case `quality_flags` carries `pipeline_abandoned`).
AdversarialSeverity = Literal["green", "yellow", "red"]


# ============================================================================
# Dataclass — per spec the caller assembles + passes to emit_research_report
# ============================================================================

@dataclass
class ResearchReport:
    """One adversarial-pipeline research report.

    Append-only: a revision is a NEW row (not an in-place mutation). The
    `payload` dict carries any free-form extras (revision lineage, raw
    LLM stage outputs, etc.).
    """

    id: str
    specialist_id: str
    cycle_id: str
    hypothesis_id: str            # FK to the surviving hypothesis
    instrument: str               # e.g. "BTC", "HYPE", "ETH-2y-curve"
    report_kind: ReportKind
    title: str                    # <=120 chars, indexable headline
    abstract: str                 # <=400 chars, TL;DR
    body_md: str                  # 600-1500 word markdown body
    edge_thesis: str              # 1-2 sentence edge claim
    contradicting_evidence: list[dict[str, Any]]  # cited contradictions
    citation_claim_ids: list[str]            # tic.db claim_ids referenced
    citation_tool_call_ids: list[str]        # desk.db tool_call_log refs
    primary_artifact_id: Optional[str]       # ti_/wls_/blk_ if applicable
    confidence: float                        # 0..1
    novelty_score: Optional[float]           # set by score_novelty when avail
    quality_flags: list[str]
    reviewer_turns: list[dict[str, Any]]     # full 3-stage transcript
    adversarial_severity: AdversarialSeverity
    revised_at: datetime
    cost_usd: float                          # total LLM cost incl. all stages
    valid_from: datetime
    transaction_from: datetime
    payload: dict[str, Any] = field(default_factory=dict)


# ============================================================================
# Helpers
# ============================================================================

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def new_report_id() -> str:
    return f"rpt_{uuid.uuid4().hex[:12]}"


__all__ = [
    "ResearchReport",
    "ReportKind",
    "AdversarialSeverity",
    "new_report_id",
]

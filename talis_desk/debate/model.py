"""Debate Pydantic models — DDL parity with v2 §3 (lines 257-277).

The on-disk shape is JSON-encoded into the `debates.argument_payload` and
`debates.verdict` columns; this module is the canonical Pydantic
representation that runner.py uses.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator


TriggerKind = Literal[
    "high_confidence",      # claim posterior_prob > 0.75 or trade_idea conf >= 0.7
    "short_horizon",        # horizon <= 24h
    "high_stakes",          # impact_score >= 0.7 or size implication
    "contradiction",        # source_conflict / quality_flags conflict
    "cross_specialist",     # cross_specialist_contradiction
]


DebateStatus = Literal[
    "open",                 # opened, no arguments yet
    "awaiting_arguments",   # one participant has submitted, awaiting second
    "judged",               # both submitted + judge has rendered verdict
    "applied",              # verdict applied (loser's state mutated)
    "expired",              # due_at passed without verdict
]


# ============================================================================
# DebateArgument
# ============================================================================

class DebateArgument(BaseModel):
    """One side of a debate. ≤200 words; citations resolve to claim/tool_call ids.

    v2 §6 protocol step 4 (line 689): each specialist posts a structured
    argument: thesis + cited evidence + falsifiable crux.
    """

    debate_id: str
    agent_id: str
    persona_version: str
    argument_md: str = Field(..., description="Markdown argument, <=200 words.")
    citation_ids: list[str] = Field(
        default_factory=list,
        description="claim_ids + tool_call_ids + hypothesis_ids cited in argument.",
    )
    falsifiable_crux: str = Field(
        ...,
        description=(
            "One sentence: what concrete observation would change this opinion? "
            "(v2 §6 step 4 'falsifiable crux')"
        ),
    )
    posted_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )

    @field_validator("argument_md")
    @classmethod
    def _check_word_count(cls, v: str) -> str:
        word_count = len(v.split())
        if word_count > 200:
            raise ValueError(
                f"argument_md must be <=200 words, got {word_count} "
                f"(this is v2 §6 step 4 requirement)"
            )
        return v

    @field_validator("falsifiable_crux")
    @classmethod
    def _check_crux(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("falsifiable_crux is required (one sentence)")
        # Strict cap: 60 words.
        if len(v.split()) > 60:
            raise ValueError(
                f"falsifiable_crux should be one sentence (<=60 words), "
                f"got {len(v.split())}"
            )
        return v


# ============================================================================
# DebateVerdict
# ============================================================================

class DebateVerdict(BaseModel):
    """The judge's structured verdict. v2 §6 step 6.

    Shape demanded of the judge LLM output JSON; if the model returns
    free-form text, runner attempts a best-effort parse and falls back to
    a tie if extraction fails.
    """

    debate_id: str
    winner: Optional[str] = None  # agent_id, or None for tie
    confidence: float = Field(..., description="Judge's confidence in this verdict (0..1)")
    rationale: str = Field(
        ...,
        description=(
            "Short paragraph (<=150 words). Cite the deciding evidence "
            "by claim_id / tool_call_id."
        ),
    )
    follow_up_action: Optional[dict[str, Any]] = Field(
        default=None,
        description=(
            "Optional action to apply. Examples: "
            "{'type':'downgrade_claim','target_id':'cl_x','new_prob':0.58}, "
            "{'type':'supersede_hypothesis','target_id':'hyp_y',"
            "'replacement_text':'...'}, "
            "{'type':'cut_size_pct','target_idea_id':'ti_z','factor':0.6}."
        ),
    )
    required_new_tool_calls: list[str] = Field(
        default_factory=list,
        description=(
            "Tool URIs the judge wants run before next cycle to settle "
            "remaining uncertainty."
        ),
    )
    judge_uncertainty: Optional[str] = Field(
        None,
        description="If verdict was close to a tie, why.",
    )
    judge_model: str
    judge_provider: str
    later_brier: Optional[float] = Field(
        None,
        description=(
            "Filled by Phase 7 once the underlying claim/idea resolves. "
            "Per v2 §6 line 691 — judge reliability is graded on Brier."
        ),
    )

    @field_validator("confidence")
    @classmethod
    def _confidence_in_unit(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError(f"confidence must be in [0,1], got {v}")
        return v


# ============================================================================
# Debate (the top-level row)
# ============================================================================

class Debate(BaseModel):
    """One debate. Mirrors `debates` 1:1.

    DDL columns: id, cycle_id, trigger_kind, trigger_id, participants[],
    judge_model, judge_provider, status, due_at, argument_payload (JSONB),
    verdict (JSONB), winner, judge_confidence, later_brier, supersedes,
    valid_from, valid_to, transaction_from, transaction_to.

    `arguments` is serialized into `argument_payload.arguments`;
    `verdict` is serialized into the top-level `verdict` JSONB column.
    """

    id: str = Field(default_factory=lambda: f"deb_{uuid4().hex[:12]}")
    cycle_id: str
    trigger_kind: TriggerKind
    trigger_id: str = Field(
        ...,
        description="ID of the triggering claim / hypothesis / trade_idea.",
    )
    participants: list[str] = Field(
        ...,
        description="Specialist ids on each side of the debate. Exactly two.",
    )
    judge_model: str
    judge_provider: str
    status: DebateStatus = "open"
    opened_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    due_at: datetime
    arguments: list[DebateArgument] = Field(default_factory=list)
    verdict: Optional[DebateVerdict] = None
    supersedes: Optional[str] = None
    valid_from: datetime
    valid_to: Optional[datetime] = None
    transaction_from: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    transaction_to: Optional[datetime] = None

    @field_validator("participants")
    @classmethod
    def _exactly_two_participants(cls, v: list[str]) -> list[str]:
        if len(v) != 2:
            raise ValueError(
                f"participants must have exactly 2 specialist_ids (got {len(v)})"
            )
        if v[0] == v[1]:
            raise ValueError("participants must be two distinct specialists")
        return v

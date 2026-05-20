"""Trade Idea Pydantic model + validation + emit pipeline.

Layer 4 of the SOTA Desk Architecture v2 (wiki/SOTA_DESK_ARCHITECTURE.md §2,
lines 109-137 and §7 lines 130-134). This is the primary artifact of the
desk — every other layer exists to produce or improve the trade-idea book.

# Why the schema is this rigid

A trade idea has to be:
  - Resolvable against HL mark/orderbook (entry/stop/target are concrete
    prices, slippage assumptions are an enumerated bucket, time_horizon is
    explicit).
  - Scored on PnL after the fact (resolver fills realized_pnl_pct,
    benchmark_return_pct, contributed_alpha_pct, brier).
  - Auditable end-to-end (claim_ids / hypothesis_ids / tool_call_ids /
    debate_ids cite the evidence; contradicting_evidence shows the agent
    looked at the other side).
  - Bitemporally consistent (valid_from / transaction_from + supersedes;
    resolver writes new rows via the supersedes pattern rather than mutating).
  - Sized conservatively (quarter-Kelly default, 2x leverage cap, 25-50 bps
    of model book per idea until S-tier is declared).

# Honest gaps

  - `entry.market_assumption` is a coarse bucket (tight_ladder / liquid /
    thin_book / illiquid). Per-coin spread modeling lands in a later phase.
  - Quarter-Kelly cap is conservative; once we have 30d Brier signal we'll
    raise per-specialist via persona evolution.
  - `confluence_score` is provided by the specialist; the resolver doesn't
    re-score it. A future phase can cross-check confluence vs realized.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
import warnings
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


# ============================================================================
# Constants — kept here so the validator + emitter both see the same numbers.
# v2 lines 130-134 (validation gates) and line 132 (sizing caps).
# ============================================================================

MARKET_ASSUMPTION_VALUES: tuple[str, ...] = (
    "tight_ladder", "liquid", "thin_book", "illiquid",
)

#: 25-50 bps of model book per idea until S-tier (v2 line 132).
MIN_RISK_PCT = 0.001   # 10 bps lower bound for live ideas (paper smaller is fine in payload)
MAX_RISK_PCT = 0.0050  # 50 bps upper bound until S-tier

MAX_KELLY_FRACTION = 0.25     # quarter-Kelly cap
MAX_LEVERAGE_CAP = 2.0        # 2x leverage cap unless human-raised

CONFIDENCE_REQUIRING_CONTRADICTION = 0.7  # v2 line 131: >=1 contradiction at >=0.7

#: Allowed time_horizon tokens (canonical strings). The resolver maps these
#: to a max-window in hours; arbitrary values rejected at validation.
TIME_HORIZON_HOURS: dict[str, int] = {
    "intraday": 8,
    "12h": 12,
    "1d": 24,
    "3d": 72,
    "7d": 168,
    "14d": 336,
    "30d": 720,
}


# ============================================================================
# Sub-models — exact shapes from wiki/SOTA_DESK_ARCHITECTURE.md §7
# ============================================================================

class SizingPlan(BaseModel):
    """Per v2 line 116, 132: risk pct + notional cap + Kelly fraction + lev cap."""
    risk_pct: float = Field(..., description="% of model book risked (e.g. 0.0030 = 30 bps)")
    notional_cap_usd: Optional[float] = Field(
        None, description="Absolute USD notional cap; None = uncapped beyond risk_pct."
    )
    kelly_fraction: float = Field(
        0.25, description="Fraction of full Kelly; default quarter-Kelly (0.25)."
    )
    leverage_cap: float = Field(
        2.0, description="Max leverage applied to the idea (2x default cap)."
    )


class EntryPlan(BaseModel):
    """Per v2 line 119, 133: trigger + limit/market + slippage + invalidation."""
    trigger: str = Field(
        ...,
        description=(
            "Human description of trigger: 'market', 'limit @ <px>', "
            "'on funding normalization', 'on OFI flip', etc."
        ),
    )
    limit_px: Optional[float] = Field(
        None, description="Limit price if `trigger` references one; else None for market."
    )
    market_assumption: Literal[
        "tight_ladder", "liquid", "thin_book", "illiquid",
    ] = Field(
        ...,
        description=(
            "Slippage bucket. tight_ladder = top-of-book very deep + tight spread; "
            "liquid = normal HL majors; thin_book = mid-cap perp w/ widening at depth; "
            "illiquid = small HIP-3 listings, expect significant slippage."
        ),
    )
    invalidation: str = Field(
        ...,
        description=(
            "Conditions that kill the idea before entry "
            "(e.g. 'cancel if BTC closes below 76k', 'cancel after 4h if no trigger')."
        ),
    )


class StopPlan(BaseModel):
    """Per v2 line 120: hard stop level + max_loss_usd. v2 line 132 sizing gate."""
    px: float = Field(..., description="Stop price; resolver compares HL mark vs this.")
    max_loss_usd: float = Field(
        ...,
        description="Computed pre-emit: (entry_px - stop_px) * size_units * sign.",
    )
    stop_kind: Literal["hard", "trailing", "time"] = Field(
        "hard",
        description=(
            "hard = fixed px; trailing = ratchet w/ favorable moves; "
            "time = exit at expires_at regardless of px."
        ),
    )


class TargetPlan(BaseModel):
    """Per v2 line 121: ONE target; dynamic trailing handles scaling."""
    px: float = Field(..., description="Take-profit price.")
    take_profit_pct: float = Field(
        ..., description="Implied move in pct from entry to target."
    )


class ContradictionItem(BaseModel):
    """Per v2 line 124, 131: at least one required when confidence >= 0.7."""
    claim_id: str = Field(..., description="Citation of the conflicting claim/hypothesis/forecast.")
    reason: str = Field(..., description="One-sentence why this contradicts the idea.")
    weight: float = Field(
        0.5,
        description="Strength of the contradiction (0..1). 1.0 = decisive counter.",
    )


# ============================================================================
# Main model — TradeIdea
# ============================================================================

class TradeIdea(BaseModel):
    """One scored trade idea — the primary artifact of the desk (v2 §2 Layer 4).

    See `validate_trade_idea(idea)` for the full set of hard gates. The
    schema mirrors `wiki/SOTA_DESK_ARCHITECTURE.md` v2 lines 113-127 and
    `trade_ideas` table DDL (lines 217-255).
    """

    # ---- Identity ---------------------------------------------------------
    id: str = Field(default_factory=lambda: f"ti_{uuid.uuid4().hex[:12]}")
    cycle_id: str
    specialist_id: str
    persona_version: str = Field(
        ...,
        description="Persona version that emitted (carried into `payload` on insert).",
    )

    # ---- Instrument + structure -------------------------------------------
    instrument: str
    venue: str = "hyperliquid"
    direction: Literal["long", "short", "flat", "spread"]

    # ---- Sizing / entry / stop / target -----------------------------------
    sizing: SizingPlan
    entry: EntryPlan
    stop: StopPlan
    target: Optional[TargetPlan] = None
    time_horizon: str

    # ---- Edge -------------------------------------------------------------
    edge_thesis: str
    claim_ids: list[str] = Field(default_factory=list)
    hypothesis_ids: list[str] = Field(default_factory=list)
    forecast_ids: list[str] = Field(default_factory=list)
    debate_ids: list[str] = Field(default_factory=list)
    tool_call_ids: list[str] = Field(default_factory=list)

    # ---- Contradiction (REQUIRED for high-confidence) ---------------------
    contradicting_evidence: list[ContradictionItem] = Field(default_factory=list)
    confluence_score: float = 0.0
    confidence: float

    # ---- Lifecycle --------------------------------------------------------
    status: Literal[
        "draft", "published", "open", "closed", "expired", "invalidated",
    ] = "draft"
    published_at: Optional[datetime] = None
    expires_at: datetime

    # ---- Playbook link ----------------------------------------------------
    playbook_id: Optional[str] = None

    # ---- Resolver-filled (None until resolved) ----------------------------
    realized_outcome: Optional[dict] = None
    realized_pnl_pct: Optional[float] = None
    realized_return_after_fees_pct: Optional[float] = None
    benchmark_return_pct: Optional[float] = None
    contributed_alpha_pct: Optional[float] = None
    brier: Optional[float] = None
    resolver_run_id: Optional[str] = None

    # ---- Bitemporal -------------------------------------------------------
    supersedes: Optional[str] = None
    valid_from: datetime
    valid_to: Optional[datetime] = None
    transaction_from: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    transaction_to: Optional[datetime] = None
    payload: dict = Field(default_factory=dict)

    # -- Validators ---------------------------------------------------------

    @field_validator("confidence")
    @classmethod
    def _confidence_in_unit(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError(f"confidence must be in [0, 1], got {v}")
        return v

    @field_validator("confluence_score")
    @classmethod
    def _confluence_nonneg(cls, v: float) -> float:
        if v < 0:
            raise ValueError(f"confluence_score must be >= 0, got {v}")
        return v

    @field_validator("time_horizon")
    @classmethod
    def _time_horizon_known(cls, v: str) -> str:
        if v not in TIME_HORIZON_HOURS:
            raise ValueError(
                f"unknown time_horizon {v!r}; expected one of "
                f"{sorted(TIME_HORIZON_HOURS.keys())}"
            )
        return v

    @model_validator(mode="after")
    def _valid_dates_tz_normalized(self) -> "TradeIdea":
        """Ensure all datetimes are UTC-aware (the SQLite ISO format we write
        elsewhere is always tz-aware)."""
        for fname in ("valid_from", "transaction_from", "expires_at",
                      "published_at", "valid_to", "transaction_to"):
            v = getattr(self, fname, None)
            if isinstance(v, datetime) and v.tzinfo is None:
                object.__setattr__(self, fname, v.replace(tzinfo=timezone.utc))
        return self


class TradeIdeaDraft(BaseModel):
    """Loose pre-validation shape used by `emit_trade_idea`.

    Same fields as `TradeIdea` but with `status` defaulting to 'draft' and
    `published_at` always None. Convert with `.to_trade_idea()`.
    """
    cycle_id: str
    specialist_id: str
    persona_version: str
    instrument: str
    venue: str = "hyperliquid"
    direction: Literal["long", "short", "flat", "spread"]
    sizing: SizingPlan
    entry: EntryPlan
    stop: StopPlan
    target: Optional[TargetPlan] = None
    time_horizon: str
    edge_thesis: str
    claim_ids: list[str] = Field(default_factory=list)
    hypothesis_ids: list[str] = Field(default_factory=list)
    forecast_ids: list[str] = Field(default_factory=list)
    debate_ids: list[str] = Field(default_factory=list)
    tool_call_ids: list[str] = Field(default_factory=list)
    contradicting_evidence: list[ContradictionItem] = Field(default_factory=list)
    confluence_score: float = 0.0
    confidence: float
    expires_at: datetime
    playbook_id: Optional[str] = None
    valid_from: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    payload: dict = Field(default_factory=dict)

    def to_trade_idea(self) -> TradeIdea:
        return TradeIdea(
            cycle_id=self.cycle_id,
            specialist_id=self.specialist_id,
            persona_version=self.persona_version,
            instrument=self.instrument,
            venue=self.venue,
            direction=self.direction,
            sizing=self.sizing,
            entry=self.entry,
            stop=self.stop,
            target=self.target,
            time_horizon=self.time_horizon,
            edge_thesis=self.edge_thesis,
            claim_ids=list(self.claim_ids),
            hypothesis_ids=list(self.hypothesis_ids),
            forecast_ids=list(self.forecast_ids),
            debate_ids=list(self.debate_ids),
            tool_call_ids=list(self.tool_call_ids),
            contradicting_evidence=list(self.contradicting_evidence),
            confluence_score=self.confluence_score,
            confidence=self.confidence,
            status="draft",
            expires_at=self.expires_at,
            playbook_id=self.playbook_id,
            valid_from=self.valid_from,
            payload=dict(self.payload),
        )


# ============================================================================
# Validation report
# ============================================================================

@dataclass
class ValidationReport:
    """Outcome of `validate_trade_idea`."""
    ok: bool
    errors: list[str]
    warnings: list[str]

    def to_payload(self) -> dict[str, Any]:
        return {"ok": self.ok, "errors": list(self.errors),
                "warnings": list(self.warnings)}


# ============================================================================
# Validation gates — exact v2 §7 lines 130-134
# ============================================================================

def validate_trade_idea(idea: TradeIdea) -> ValidationReport:
    """Hard gates per v2 lines 130-134. Returns a structured report.

    Gates checked (in order):
      1. instrument / direction / entry / stop / sizing / time_horizon / target_or_invalidation
      2. edge_thesis non-empty AND >=1 claim_id OR hypothesis_id citation
      3. >=1 contradicting_evidence when confidence >= 0.7
      4. sizing.kelly_fraction <= 0.25
      5. sizing.leverage_cap <= 2.0 unless payload.human_raised_leverage=True
      6. sizing.risk_pct in [0.001, 0.0050] until S-tier
      7. stop.max_loss_usd > 0
      8. entry.market_assumption ∈ {tight_ladder, liquid, thin_book, illiquid}
      9. expires_at honors time_horizon (no 30d idea expiring in 1h, etc.)
    """
    errors: list[str] = []
    warns: list[str] = []

    # -- Gate 1: presence ---------------------------------------------------
    if not (idea.instrument or "").strip():
        errors.append("gate1_missing_instrument")
    if idea.direction not in ("long", "short", "flat", "spread"):
        errors.append(f"gate1_bad_direction:{idea.direction}")
    if idea.entry is None:
        errors.append("gate1_missing_entry")
    if idea.stop is None:
        errors.append("gate1_missing_stop")
    if idea.sizing is None:
        errors.append("gate1_missing_sizing")
    if not (idea.time_horizon or "").strip():
        errors.append("gate1_missing_time_horizon")
    # target OR invalidation — v2 line 131: "target/invalidation"
    if idea.target is None and not (idea.entry and (idea.entry.invalidation or "").strip()):
        errors.append("gate1_missing_target_and_invalidation")

    # -- Gate 2: edge_thesis citations --------------------------------------
    if not (idea.edge_thesis or "").strip():
        errors.append("gate2_edge_thesis_empty")
    if not (idea.claim_ids or idea.hypothesis_ids):
        errors.append("gate2_edge_thesis_missing_citation_claim_or_hypothesis")

    # -- Gate 3: contradiction required at high confidence ------------------
    if idea.confidence >= CONFIDENCE_REQUIRING_CONTRADICTION:
        if not idea.contradicting_evidence:
            errors.append(
                f"gate3_contradiction_required_at_confidence_{idea.confidence:.2f}"
            )

    # -- Gate 4: Kelly fraction cap -----------------------------------------
    if idea.sizing is not None:
        if idea.sizing.kelly_fraction > MAX_KELLY_FRACTION + 1e-9:
            errors.append(
                f"gate4_kelly_fraction_above_quarter:{idea.sizing.kelly_fraction}"
            )

    # -- Gate 5: leverage cap (unless human raised) -------------------------
    if idea.sizing is not None:
        human_raised = bool(idea.payload.get("human_raised_leverage", False))
        if idea.sizing.leverage_cap > MAX_LEVERAGE_CAP + 1e-9 and not human_raised:
            errors.append(
                f"gate5_leverage_cap_above_2x:{idea.sizing.leverage_cap}"
            )

    # -- Gate 6: risk_pct band ----------------------------------------------
    if idea.sizing is not None:
        if not (MIN_RISK_PCT - 1e-12 <= idea.sizing.risk_pct <= MAX_RISK_PCT + 1e-12):
            errors.append(
                f"gate6_risk_pct_out_of_band:{idea.sizing.risk_pct} "
                f"(allowed {MIN_RISK_PCT}-{MAX_RISK_PCT} until S-tier)"
            )

    # -- Gate 7: stop max_loss_usd > 0 --------------------------------------
    if idea.stop is not None and not (idea.stop.max_loss_usd > 0):
        errors.append(f"gate7_max_loss_usd_not_positive:{idea.stop.max_loss_usd}")

    # -- Gate 8: market_assumption enumerated -------------------------------
    if idea.entry is not None:
        if idea.entry.market_assumption not in MARKET_ASSUMPTION_VALUES:
            errors.append(
                f"gate8_market_assumption_invalid:{idea.entry.market_assumption}"
            )

    # -- Gate 9: expires_at vs time_horizon --------------------------------
    if idea.time_horizon in TIME_HORIZON_HOURS:
        horizon_hours = TIME_HORIZON_HOURS[idea.time_horizon]
        # `published_at` only exists on TradeIdea (post-emission); for Draft
        # we anchor on valid_from. Use getattr to support both shapes.
        anchor = getattr(idea, "published_at", None) or idea.valid_from
        anchor_utc = anchor if anchor.tzinfo else anchor.replace(tzinfo=timezone.utc)
        exp_utc = (idea.expires_at if idea.expires_at.tzinfo
                   else idea.expires_at.replace(tzinfo=timezone.utc))
        delta_hours = (exp_utc - anchor_utc).total_seconds() / 3600.0
        if delta_hours < horizon_hours * 0.5:
            errors.append(
                f"gate9_expires_at_too_soon: time_horizon={idea.time_horizon} "
                f"({horizon_hours}h) but expires_at delta={delta_hours:.2f}h"
            )
        if delta_hours > horizon_hours * 2.5:
            warns.append(
                f"warn_expires_at_unusually_far: time_horizon={idea.time_horizon} "
                f"({horizon_hours}h) but expires_at delta={delta_hours:.2f}h"
            )

    # -- Hygienic checks (warnings, not errors) -----------------------------
    if idea.direction in ("long", "short") and idea.stop is not None and idea.entry is not None:
        # Soft check: stop should be on the loss side
        if idea.entry.limit_px is not None:
            ep = idea.entry.limit_px
            if idea.direction == "long" and idea.stop.px >= ep:
                warns.append("warn_stop_above_entry_for_long")
            if idea.direction == "short" and idea.stop.px <= ep:
                warns.append("warn_stop_below_entry_for_short")
            if idea.target is not None:
                if idea.direction == "long" and idea.target.px <= ep:
                    warns.append("warn_target_below_entry_for_long")
                if idea.direction == "short" and idea.target.px >= ep:
                    warns.append("warn_target_above_entry_for_short")

    if not idea.tool_call_ids:
        warns.append("warn_no_tool_call_citations")

    return ValidationReport(ok=not errors, errors=errors, warnings=warns)


# ============================================================================
# Emit — write to desk.db / tool_call_log / agent_messages
# ============================================================================

# Optional type alias — the existing AgentContext lives in tool_atlas.atlas.
# We import lazily so this module doesn't force a tool_atlas init on import.

def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _conn_from_context(context: Any) -> sqlite3.Connection:
    """Resolve the desk.db connection. Prefer an explicit context.desk_store /
    context.conn handle; fall back to the singleton DeskStore.
    """
    if context is not None and getattr(context, "conn", None) is not None:
        return context.conn  # type: ignore[no-any-return]
    if context is not None and getattr(context, "desk_store", None) is not None:
        return context.desk_store.conn
    from ..store import get_desk_store
    return get_desk_store().conn


def emit_trade_idea(
    draft: TradeIdeaDraft | TradeIdea,
    context: Any,
    *,
    write_tool_call_log: bool = True,
    write_agent_message: bool = True,
) -> TradeIdea:
    """Validate and persist a trade idea.

    Behavior:
      - Convert TradeIdeaDraft -> TradeIdea if needed.
      - Run `validate_trade_idea`. On failure: keep status='draft', stash the
        validation report into `payload.validation_report`, write the row,
        DO NOT post a trade_book message, return the idea.
      - On success: set status='published', published_at=now(), write the row,
        log a tool_call (synthetic emit call), and post an agent_messages
        row to topic 'trade_book' with the new idea_id.

    Args:
      draft: TradeIdeaDraft or TradeIdea (TradeIdea is converted to a draft
             internally to ensure consistent state).
      context: object with .cycle_id and .specialist_id (typically the
               `AgentContext` from `tool_atlas.atlas`). May also carry a
               `.conn` or `.desk_store` to override the singleton db.
      write_tool_call_log: write a synthetic tool_call_log row for audit.
      write_agent_message: post to topic 'trade_book' on success.

    Returns:
      The persisted (or failed-to-validate) TradeIdea.
    """
    if isinstance(draft, TradeIdea):
        # Reset lifecycle fields so emit is idempotent on a fresh draft.
        idea = TradeIdea(
            cycle_id=draft.cycle_id,
            specialist_id=draft.specialist_id,
            persona_version=draft.persona_version,
            instrument=draft.instrument,
            venue=draft.venue,
            direction=draft.direction,
            sizing=draft.sizing,
            entry=draft.entry,
            stop=draft.stop,
            target=draft.target,
            time_horizon=draft.time_horizon,
            edge_thesis=draft.edge_thesis,
            claim_ids=list(draft.claim_ids),
            hypothesis_ids=list(draft.hypothesis_ids),
            forecast_ids=list(draft.forecast_ids),
            debate_ids=list(draft.debate_ids),
            tool_call_ids=list(draft.tool_call_ids),
            contradicting_evidence=list(draft.contradicting_evidence),
            confluence_score=draft.confluence_score,
            confidence=draft.confidence,
            status="draft",
            expires_at=draft.expires_at,
            playbook_id=draft.playbook_id,
            valid_from=draft.valid_from,
            payload=dict(draft.payload),
        )
    else:
        idea = draft.to_trade_idea()

    report = validate_trade_idea(idea)
    now_dt = datetime.now(timezone.utc)
    idea.payload = {**(idea.payload or {}), "validation_report": report.to_payload()}

    if report.ok:
        idea.status = "published"
        idea.published_at = now_dt
    else:
        idea.status = "draft"

    conn = _conn_from_context(context)
    _insert_row(conn, idea)

    if write_tool_call_log:
        _log_emission(conn, idea, context, now_dt, ok=report.ok)

    if write_agent_message and report.ok:
        _post_trade_book(conn, idea, context, now_dt)

    return idea


def _insert_row(conn: sqlite3.Connection, idea: TradeIdea) -> None:
    """Insert one trade_ideas row. JSON columns are canonicalized."""
    target_json = (
        _canonical_json(idea.target.model_dump()) if idea.target is not None else None
    )
    realized_outcome_json = (
        _canonical_json(idea.realized_outcome)
        if idea.realized_outcome is not None else None
    )
    conn.execute(
        "INSERT INTO trade_ideas ("
        "id, cycle_id, specialist_id, instrument, venue, direction, "
        "sizing, entry, stop, target, time_horizon, edge_thesis, "
        "claim_ids, contradicting_evidence, confluence_score, confidence, "
        "hypothesis_ids, forecast_ids, debate_ids, playbook_id, "
        "tool_call_ids, status, published_at, expires_at, "
        "realized_outcome, realized_pnl_pct, realized_return_after_fees_pct, "
        "benchmark_return_pct, contributed_alpha_pct, brier, resolver_run_id, "
        "supersedes, valid_from, valid_to, transaction_from, transaction_to, "
        "payload"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            idea.id, idea.cycle_id, idea.specialist_id, idea.instrument,
            idea.venue, idea.direction,
            _canonical_json(idea.sizing.model_dump()),
            _canonical_json(idea.entry.model_dump()),
            _canonical_json(idea.stop.model_dump()),
            target_json,
            idea.time_horizon, idea.edge_thesis,
            _canonical_json(idea.claim_ids),
            _canonical_json([c.model_dump() for c in idea.contradicting_evidence]),
            idea.confluence_score, idea.confidence,
            _canonical_json(idea.hypothesis_ids),
            _canonical_json(idea.forecast_ids),
            _canonical_json(idea.debate_ids),
            idea.playbook_id,
            _canonical_json(idea.tool_call_ids),
            idea.status,
            _iso(idea.published_at) if idea.published_at else None,
            _iso(idea.expires_at),
            realized_outcome_json,
            idea.realized_pnl_pct,
            idea.realized_return_after_fees_pct,
            idea.benchmark_return_pct,
            idea.contributed_alpha_pct,
            idea.brier,
            idea.resolver_run_id,
            idea.supersedes,
            _iso(idea.valid_from),
            _iso(idea.valid_to) if idea.valid_to else None,
            _iso(idea.transaction_from),
            _iso(idea.transaction_to) if idea.transaction_to else None,
            _canonical_json(idea.payload or {}),
        ),
    )


def _log_emission(
    conn: sqlite3.Connection,
    idea: TradeIdea,
    context: Any,
    now_dt: datetime,
    ok: bool,
) -> None:
    """Synthetic tool_call_log row capturing the emit event itself."""
    args = {
        "idea_id": idea.id,
        "instrument": idea.instrument,
        "direction": idea.direction,
        "confidence": idea.confidence,
        "status": idea.status,
    }
    args_json = _canonical_json(args)
    args_hash = _sha256_hex(args_json)
    result_payload = {"ok": ok, "status": idea.status, "id": idea.id}
    result_json = _canonical_json(result_payload)
    result_hash = _sha256_hex(result_json)
    log_id = "tc_" + uuid.uuid4().hex[:24]
    cycle_id = getattr(context, "cycle_id", idea.cycle_id) or idea.cycle_id
    specialist_id = getattr(context, "specialist_id", idea.specialist_id) or idea.specialist_id
    investigation_id = getattr(context, "investigation_id", None)
    started = _iso(now_dt)
    try:
        conn.execute(
            "INSERT INTO tool_call_log ("
            "id, cycle_id, investigation_id, specialist_id, tool_uri, "
            "tool_version, args_hash, args_json, result_hash, result_summary, "
            "started_at, finished_at, duration_ms, cost_usd, valid_from, "
            "transaction_from, cited_in_ids"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                log_id, cycle_id, investigation_id, specialist_id,
                "tic://tool/builtin/emit_trade_idea@v1", "v1",
                args_hash, args_json, result_hash, result_json[:500],
                started, started, 0, 0.0, started, started,
                _canonical_json([idea.id]),
            ),
        )
    except Exception as e:  # pragma: no cover - best-effort audit row
        warnings.warn(f"emit_trade_idea: tool_call_log insert failed: {e}")


def _post_trade_book(
    conn: sqlite3.Connection,
    idea: TradeIdea,
    context: Any,
    now_dt: datetime,
) -> None:
    """Post the new idea to topic 'trade_book' via agent_messages."""
    msg_id = "msg_" + uuid.uuid4().hex[:24]
    payload = {
        "idea_id": idea.id,
        "instrument": idea.instrument,
        "direction": idea.direction,
        "time_horizon": idea.time_horizon,
        "confidence": idea.confidence,
        "expires_at": _iso(idea.expires_at),
        "specialist_id": idea.specialist_id,
        "persona_version": idea.persona_version,
        "edge_thesis_excerpt": (idea.edge_thesis or "")[:200],
    }
    from_agent = getattr(context, "specialist_id", idea.specialist_id) or idea.specialist_id
    started = _iso(now_dt)
    try:
        conn.execute(
            "INSERT INTO agent_messages ("
            "id, from_agent, to_agent_or_topic, message_kind, payload, "
            "posted_at, related_trade_idea_id, valid_from, transaction_from, "
            "dedupe_key"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                msg_id, from_agent, "trade_book", "trade_idea_published",
                _canonical_json(payload), started, idea.id,
                started, started,
                f"emit:{idea.id}",
            ),
        )
    except Exception as e:  # pragma: no cover - best-effort message post
        warnings.warn(f"emit_trade_idea: agent_messages insert failed: {e}")

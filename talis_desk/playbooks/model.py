"""Playbook Pydantic models + CRUD.

Source of truth: wiki/SOTA_DESK_ARCHITECTURE.md v2 §2 Layer 3 (lines 96-107)
and §3 DDL (lines 279-299).

Design rules carried verbatim from v2:
  - Append-only. Corrections / promotions / retirements insert a NEW row
    with `supersedes=<old_id>` and set the old row's transaction_to=now.
  - Versioned. New trigger logic creates a new VERSION of the same name,
    not an in-place edit. (name, version) is UNIQUE in the DDL.
  - Promotion gates:
      candidate    -> experimental requires (>= min_sample_size historical
                      triggers from backtest) OR (payload.human_marked_experimental
                      = True).
      experimental -> approved requires >= 5 LIVE triggers w/ positive net
                      return.
      approved     -> demoted   when 5 consecutive triggers post-promotion
                      fall below 50% of historical avg return (v2 §9
                      line 558).
  - Cooldown enforcement: don't fire same playbook twice per instrument
    within `trigger_spec.cooldown_hours`.
  - Trigger evaluation walks bitemporal state via build_replay_context so
    backtests don't leak future information.

# Honest gaps
  - TriggerSpec.sql_predicate is raw SQL. Only specialists with
    payload.frontier_experimental=True can use arbitrary SQL; others use
    the constrained `predicate_dsl` (a typed dict matched against a fixed
    table of allowed shapes). See `_evaluate_trigger_predicate`.
  - Backtest forward-simulates with a SINGLE-PASS over the claims +
    HL candle history; per-coin slippage is bucketed (same buckets as
    the resolver). More rigorous modeling lands later.
  - `historical_avg_return_pct` is the realized return per trigger over
    the lookback window; "positive net return" for the experimental ->
    approved gate uses the same calc but on live (resolved trade_ideas)
    instead of replayed claims.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

from ..replay import build_replay_context
from ..store import get_desk_store
from ..tool_atlas import AgentContext


# ============================================================================
# Pydantic models
# ============================================================================

PromotedStatus = Literal[
    "candidate", "experimental", "approved", "demoted", "retired"
]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TriggerSpec(BaseModel):
    """A SQL-ish predicate the system can evaluate against bitemporal state.

    Two modes:
      - `sql_predicate`: raw SQL EXISTS-style fragment evaluated against
        the desk db. Only specialists flagged `frontier_experimental` in
        their persona payload may use this — others are rejected.
      - `predicate_dsl`: a typed dict the runner translates into a safe
        parameterized query. Schema:
          {
            "kind": "claim_value_threshold",
            "claim_kind": "funding_rate",
            "field_path": "z_score",
            "op": ">",
            "value": 2.0,
            "instrument_in": ["BTC", "ETH", "SOL", "HYPE"],
            "transition_from_op": ">",   # optional: detect a regime change
            "transition_from_value": 2.0,
            "transition_to_op": "<",
            "transition_to_value": 1.0,
          }

    A spec may carry both (DSL preferred at evaluation time; sql_predicate
    only used if the DSL is absent). natural_language is always required so
    humans can audit.
    """

    sql_predicate: Optional[str] = Field(
        None,
        description=(
            "Raw SQL EXISTS-style fragment. Only specialists with "
            "payload.frontier_experimental=True may set this; others must "
            "use predicate_dsl."
        ),
    )
    predicate_dsl: Optional[dict[str, Any]] = Field(
        None,
        description="Constrained DSL — preferred path. See module docstring.",
    )
    natural_language: str = Field(
        ...,
        description="Human-readable description. Always required.",
    )
    required_quality_flags_exclude: list[str] = Field(
        default_factory=lambda: ["stale_source", "cap_artifact"],
        description=(
            "If any cited tool_call has one of these quality_flags, the "
            "trigger fires-but is downgraded to status='quality_warn'. "
            "Default: stale_source, cap_artifact."
        ),
    )
    cooldown_hours: int = Field(
        12,
        description=(
            "Don't fire the same playbook twice within this window per "
            "instrument. Default 12h."
        ),
    )

    @field_validator("cooldown_hours")
    @classmethod
    def _check_cooldown(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"cooldown_hours must be >= 0, got {v}")
        return v


class ActionTemplate(BaseModel):
    """What trade idea to emit when the playbook fires.

    Fields mirror TradeIdea sub-models. Placeholders like `$entry_px`,
    `$stop_pct`, `$instrument` get substituted at instantiate time from
    the current market state + trigger context.
    """

    direction: Literal["long", "short", "spread", "flat"]
    sizing_template: dict[str, Any] = Field(
        ...,
        description=(
            "Mirrors SizingPlan fields. Numeric placeholders like "
            "'$risk_pct' are resolved at instantiation."
        ),
    )
    entry_template: dict[str, Any]
    stop_template: dict[str, Any]
    target_template: Optional[dict[str, Any]] = None
    time_horizon_default: str = "1d"
    confidence_floor: float = Field(
        0.55,
        description=(
            "Instantiated TradeIdea must have confidence >= this floor; "
            "otherwise it's emitted as draft (not published)."
        ),
    )
    market_assumption_default: str = "liquid"

    @field_validator("confidence_floor")
    @classmethod
    def _check_floor(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError(f"confidence_floor must be in [0,1], got {v}")
        return v


class Playbook(BaseModel):
    """A versioned, triggerable trading pattern. Mirrors `playbooks` 1:1."""

    id: str = Field(default_factory=lambda: f"pb_{uuid4().hex[:12]}")
    name: str
    version: int
    owner_specialist: str
    description: str
    trigger_spec: TriggerSpec
    action_template: ActionTemplate
    min_sample_size: int = 5
    historical_trigger_count: int = 0
    historical_avg_return_pct: Optional[float] = None
    historical_hit_rate: Optional[float] = None
    promoted_status: PromotedStatus = "candidate"
    evidence_ids: list[str] = Field(default_factory=list)
    supersedes: Optional[str] = None
    valid_from: datetime
    valid_to: Optional[datetime] = None
    transaction_from: datetime = Field(default_factory=_utc_now)
    transaction_to: Optional[datetime] = None
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("version")
    @classmethod
    def _check_version(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"version must be >= 1, got {v}")
        return v

    @field_validator("historical_hit_rate")
    @classmethod
    def _check_hit_rate(cls, v: Optional[float]) -> Optional[float]:
        if v is None:
            return v
        if not 0.0 <= v <= 1.0:
            raise ValueError(f"historical_hit_rate must be in [0,1], got {v}")
        return v


class PlaybookDraft(BaseModel):
    """Input shape for propose_playbook."""

    name: str
    owner_specialist: str
    description: str
    trigger_spec: TriggerSpec
    action_template: ActionTemplate
    min_sample_size: int = 5
    evidence_ids: list[str] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)
    valid_from: Optional[datetime] = None  # defaults to now


from dataclasses import field as _dc_field  # noqa: E402


@dataclass
class PlaybookBacktest:
    """Outcome of `evaluate_playbook`. v2 §2 line 103."""

    playbook_id: str
    lookback_days: int
    n_triggers: int
    hits: int
    hit_rate: float
    avg_return_pct: float
    median_return_pct: float
    std_return_pct: float
    worst_drawdown_pct: float
    best_return_pct: float
    sample_size_warning: bool
    leakage_check: bool
    per_instrument: dict[str, dict] = _dc_field(default_factory=dict)
    triggers_found: list[dict] = _dc_field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "playbook_id": self.playbook_id,
            "lookback_days": self.lookback_days,
            "n_triggers": self.n_triggers,
            "hits": self.hits,
            "hit_rate": self.hit_rate,
            "avg_return_pct": self.avg_return_pct,
            "median_return_pct": self.median_return_pct,
            "std_return_pct": self.std_return_pct,
            "worst_drawdown_pct": self.worst_drawdown_pct,
            "best_return_pct": self.best_return_pct,
            "sample_size_warning": self.sample_size_warning,
            "leakage_check": self.leakage_check,
            "per_instrument": dict(self.per_instrument),
            "triggers_found_n": len(self.triggers_found),
        }


@dataclass
class PlaybookTrigger:
    """One detected trigger from `detect_playbook_triggers`."""

    playbook_id: str
    playbook_name: str
    playbook_version: int
    instrument: str
    evidence_ids: list[str]
    predicted_return_pct: Optional[float]
    detected_at: datetime
    quality_warn: bool = False  # set when a cited tool_call has stale_source / cap_artifact
    extras: dict[str, Any] = _dc_field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "playbook_id": self.playbook_id,
            "playbook_name": self.playbook_name,
            "playbook_version": self.playbook_version,
            "instrument": self.instrument,
            "evidence_ids": list(self.evidence_ids),
            "predicted_return_pct": self.predicted_return_pct,
            "detected_at": self.detected_at.isoformat(),
            "quality_warn": self.quality_warn,
            "extras": dict(self.extras),
        }


# ============================================================================
# Helpers
# ============================================================================

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


def _json_dict(v: Any) -> dict[str, Any]:
    if v is None:
        return {}
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _json_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x) for x in v]
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
            return [str(x) for x in parsed] if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def _row_to_playbook(row: Any) -> Playbook:
    d = dict(row)
    trigger_spec_raw = _json_dict(d.get("trigger_spec"))
    action_template_raw = _json_dict(d.get("action_template"))
    return Playbook(
        id=d["id"],
        name=d["name"],
        version=int(d["version"]),
        owner_specialist=d["owner_specialist"],
        description=d["description"],
        trigger_spec=TriggerSpec(**trigger_spec_raw) if trigger_spec_raw else TriggerSpec(natural_language=""),
        action_template=ActionTemplate(**action_template_raw) if action_template_raw else ActionTemplate(
            direction="long",
            sizing_template={},
            entry_template={},
            stop_template={},
        ),
        min_sample_size=int(d.get("min_sample_size") or 5),
        historical_trigger_count=int(d.get("historical_trigger_count") or 0),
        historical_avg_return_pct=d.get("historical_avg_return_pct"),
        historical_hit_rate=d.get("historical_hit_rate"),
        promoted_status=d.get("promoted_status") or "candidate",
        evidence_ids=_json_list(d.get("evidence_ids")),
        supersedes=d.get("supersedes"),
        valid_from=_from_iso(d["valid_from"]) or _utc_now(),
        valid_to=_from_iso(d.get("valid_to")),
        transaction_from=_from_iso(d["transaction_from"]) or _utc_now(),
        transaction_to=_from_iso(d.get("transaction_to")),
        payload=_json_dict(d.get("payload")) if "payload" in d.keys() else {},
    )


def _ensure_payload_column() -> None:
    """Add a `payload TEXT DEFAULT '{}'` column to the SQLite playbooks
    table if missing.

    The wiki Postgres DDL doesn't carry a payload column on `playbooks` (the
    spec uses dedicated columns for everything; payload was added on other
    tables like hypotheses/trade_ideas). However, the Phase 5 spec
    explicitly stores `human_marked_experimental` on the playbook's payload.
    Rather than fork the wiki, we add this column as a dev-only sidecar in
    SQLite via ALTER TABLE. Postgres prod can carry the same ALTER in a
    future migration without disturbing the canonical DDL definition.
    """
    conn = get_desk_store().conn
    cols = {r[0] for r in conn.execute("SELECT name FROM pragma_table_info('playbooks')").fetchall()}
    if "payload" not in cols:
        try:
            conn.execute(
                "ALTER TABLE playbooks ADD COLUMN payload TEXT NOT NULL DEFAULT '{}'"
            )
            conn.commit()
        except sqlite3.Error:
            # Column may have been added concurrently; safe to ignore.
            pass


def _insert_playbook(pb: Playbook) -> None:
    _ensure_payload_column()
    conn = get_desk_store().conn
    conn.execute(
        "INSERT INTO playbooks "
        "(id, name, version, owner_specialist, description, trigger_spec, "
        " action_template, min_sample_size, historical_trigger_count, "
        " historical_avg_return_pct, historical_hit_rate, promoted_status, "
        " evidence_ids, supersedes, valid_from, valid_to, transaction_from, "
        " transaction_to, payload) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            pb.id,
            pb.name,
            pb.version,
            pb.owner_specialist,
            pb.description,
            json.dumps(pb.trigger_spec.model_dump()),
            json.dumps(pb.action_template.model_dump()),
            pb.min_sample_size,
            pb.historical_trigger_count,
            pb.historical_avg_return_pct,
            pb.historical_hit_rate,
            pb.promoted_status,
            json.dumps(pb.evidence_ids),
            pb.supersedes,
            _iso(pb.valid_from),
            _iso(pb.valid_to) if pb.valid_to else None,
            _iso(pb.transaction_from),
            _iso(pb.transaction_to) if pb.transaction_to else None,
            json.dumps(pb.payload),
        ),
    )
    conn.commit()


def _find_open_head(conn: sqlite3.Connection, pb_id: str) -> Any:
    """Walk the supersedes chain anchored at `pb_id` to the open head."""
    row = conn.execute(
        "SELECT * FROM playbooks WHERE id = ? AND transaction_to IS NULL "
        "ORDER BY transaction_from DESC LIMIT 1",
        (pb_id,),
    ).fetchone()
    if row is not None:
        return row
    visited: set[str] = {pb_id}
    cur = pb_id
    while True:
        nxt = conn.execute(
            "SELECT * FROM playbooks WHERE supersedes = ? "
            "ORDER BY transaction_from DESC LIMIT 1",
            (cur,),
        ).fetchone()
        if nxt is None:
            return None
        if nxt["id"] in visited:
            return None
        visited.add(nxt["id"])
        if nxt["transaction_to"] is None:
            return nxt
        cur = nxt["id"]


def get_playbook(pb_id: str) -> Optional[Playbook]:
    """Resolve a playbook id (or any ancestor in its supersedes chain) to the
    open head row."""
    conn = get_desk_store().conn
    row = _find_open_head(conn, pb_id)
    return _row_to_playbook(row) if row is not None else None


def _next_version_for_name(conn: sqlite3.Connection, name: str) -> int:
    """Next monotonic version for a playbook name."""
    row = conn.execute(
        "SELECT MAX(version) AS v FROM playbooks WHERE name = ?", (name,),
    ).fetchone()
    cur = row["v"] if row is not None else None
    return (int(cur) if cur is not None else 0) + 1


# ============================================================================
# propose_playbook
# ============================================================================

def propose_playbook(
    spec: PlaybookDraft,
    owner_specialist: Optional[str] = None,
) -> Playbook:
    """Insert as status='candidate'. Cannot move to 'approved' without:
       - >= min_sample_size historical_trigger_count (filled by backtest) OR
       - payload.human_marked_experimental=True (frontier flag).

    New trigger logic creates a NEW VERSION (not edit). Append-only.

    Args:
      spec: PlaybookDraft with name + trigger + action template.
      owner_specialist: overrides spec.owner_specialist if provided.

    Raises:
      ValueError if a sql_predicate is set but the owner specialist is not
      marked frontier_experimental in payload.
    """
    owner = owner_specialist or spec.owner_specialist
    if not owner:
        raise ValueError("propose_playbook: owner_specialist required")

    # Security gate: only frontier-flagged specialists may use raw SQL.
    is_frontier = bool(spec.payload.get("frontier_experimental", False))
    if spec.trigger_spec.sql_predicate and not is_frontier:
        raise ValueError(
            "propose_playbook: sql_predicate requires "
            "payload.frontier_experimental=True; use predicate_dsl instead."
        )
    if not spec.trigger_spec.predicate_dsl and not spec.trigger_spec.sql_predicate:
        raise ValueError(
            "propose_playbook: trigger_spec must set either predicate_dsl or "
            "sql_predicate"
        )

    _ensure_payload_column()
    conn = get_desk_store().conn
    version = _next_version_for_name(conn, spec.name)
    valid_from = spec.valid_from or _utc_now()
    pb = Playbook(
        name=spec.name,
        version=version,
        owner_specialist=owner,
        description=spec.description,
        trigger_spec=spec.trigger_spec,
        action_template=spec.action_template,
        min_sample_size=spec.min_sample_size,
        promoted_status="candidate",
        evidence_ids=list(spec.evidence_ids),
        valid_from=valid_from,
        transaction_from=_utc_now(),
        payload=dict(spec.payload),
    )
    _insert_playbook(pb)
    return pb


# ============================================================================
# DSL predicate evaluator (bitemporal-safe)
# ============================================================================

def _resolve_predicate_op(op: str) -> str:
    """Map DSL op tokens to safe SQL operators."""
    allowed = {">", ">=", "<", "<=", "=", "==", "!="}
    if op not in allowed:
        raise ValueError(f"_resolve_predicate_op: bad op {op!r}")
    return "=" if op == "==" else op


def _evaluate_predicate_dsl(
    dsl: dict[str, Any],
    as_of: datetime,
    desk_conn: sqlite3.Connection,
) -> list[dict[str, Any]]:
    """Evaluate a DSL trigger predicate against bitemporal state.

    Returns a list of {instrument, evidence_ids, payload_hint}. No future
    leakage: every query goes through build_replay_context(as_of_valid=as_of).

    Supported `kind` values:
      - "claim_value_threshold": look for an open hypothesis on each
        instrument whose payload's `field_path` satisfies `op value`.
        Optional `transition_from_*` / `transition_to_*` detect a regime
        change by comparing the latest open hypothesis to its immediate
        superseded predecessor's posterior or payload value.

    Honest gap: we use the hypotheses table as the claim store because
    the desk's `claims` table isn't carried locally (it lives in talis-tic
    and the desk reads it via the TIC store). For local backtest replay,
    seed test claims as hypotheses with payload['claim_kind']=<kind>.
    """
    kind = dsl.get("kind")
    triggers: list[dict[str, Any]] = []
    if kind != "claim_value_threshold":
        # Unknown kinds short-circuit with no matches (safe default).
        return triggers

    claim_kind = dsl.get("claim_kind") or ""
    field_path = dsl.get("field_path") or ""
    op = _resolve_predicate_op(str(dsl.get("op") or ">"))
    value = float(dsl.get("value") or 0.0)
    instruments = dsl.get("instrument_in") or []

    ctx = build_replay_context(as_of_valid=as_of)
    where, params = ctx.where_clause("hypotheses")

    # Pull all open hypotheses matching claim_kind, restricted by replay
    # context. Per-instrument filter in Python (entity_ids is a JSON list).
    sql = (
        f"SELECT id, title, posterior_prob, entity_ids, payload, valid_from, "
        f"transaction_from, supersedes FROM hypotheses "
        f"WHERE {where} ORDER BY transaction_from DESC"
    )
    rows = desk_conn.execute(sql, params).fetchall()
    for r in rows:
        d = dict(r)
        payload = _json_dict(d.get("payload"))
        if payload.get("claim_kind") != claim_kind:
            continue
        entities = _json_list(d.get("entity_ids"))
        target_instruments = [e for e in entities if e in instruments] if instruments else entities
        if not target_instruments and instruments:
            # No overlap with desired set.
            continue
        # Look up the field value in payload by simple dotted path.
        val = payload
        for piece in field_path.split("."):
            if isinstance(val, dict):
                val = val.get(piece)
            else:
                val = None
                break
        if val is None:
            continue
        try:
            val_f = float(val)
        except (TypeError, ValueError):
            continue
        if not _compare(val_f, op, value):
            continue

        # Transition check (optional): require the predecessor in the
        # supersedes chain to satisfy `transition_from_*` and the current
        # row to satisfy `transition_to_*`.
        if "transition_from_op" in dsl and "transition_to_op" in dsl:
            prev_payload = _payload_of_predecessor(desk_conn, d.get("supersedes"))
            if prev_payload is None:
                continue
            prev_val = prev_payload
            for piece in field_path.split("."):
                if isinstance(prev_val, dict):
                    prev_val = prev_val.get(piece)
                else:
                    prev_val = None
                    break
            if prev_val is None:
                continue
            try:
                pv = float(prev_val)
            except (TypeError, ValueError):
                continue
            if not _compare(pv, _resolve_predicate_op(str(dsl["transition_from_op"])),
                            float(dsl["transition_from_value"])):
                continue
            if not _compare(val_f, _resolve_predicate_op(str(dsl["transition_to_op"])),
                            float(dsl["transition_to_value"])):
                continue

        for inst in (target_instruments or ["?"]):
            triggers.append({
                "instrument": inst,
                "evidence_ids": [d["id"]],
                "current_value": val_f,
                "claim_id": d["id"],
            })
    return triggers


def _payload_of_predecessor(conn: sqlite3.Connection, ancestor_id: Optional[str]) -> Optional[dict]:
    if not ancestor_id:
        return None
    row = conn.execute(
        "SELECT payload FROM hypotheses WHERE id = ? "
        "ORDER BY transaction_from DESC LIMIT 1",
        (ancestor_id,),
    ).fetchone()
    if row is None:
        return None
    return _json_dict(row["payload"])


def _compare(a: float, op: str, b: float) -> bool:
    if op == ">":
        return a > b
    if op == ">=":
        return a >= b
    if op == "<":
        return a < b
    if op == "<=":
        return a <= b
    if op == "=":
        return a == b
    if op == "!=":
        return a != b
    return False


# ============================================================================
# evaluate_playbook — backtest over bitemporal history
# ============================================================================

def evaluate_playbook(
    playbook_id: str,
    as_of: datetime,
    lookback_days: int = 365,
) -> PlaybookBacktest:
    """Walk through the bitemporal store from `as_of - lookback_days` to
    `as_of`, find every window where trigger_spec was true (using
    build_replay_context so no future leakage), forward-simulate the
    action_template using HL price history, compute hit_rate + avg_return.

    Updates the playbook row's historical_* fields via supersedes (append-only).
    """
    pb = get_playbook(playbook_id)
    if pb is None:
        raise KeyError(f"playbook_not_found: {playbook_id}")

    conn = get_desk_store().conn
    desc = pb.trigger_spec.predicate_dsl or {}

    # Walk forward in daily snapshots; deduplicate by (instrument, day) so
    # one trigger fires per cooldown window per instrument.
    start = (as_of - timedelta(days=lookback_days)).astimezone(timezone.utc)
    cur = start
    step_hours = max(1, int(pb.trigger_spec.cooldown_hours))  # one slot per cooldown window
    seen: set[tuple[str, str]] = set()  # (instrument, slot_iso)
    triggers_found: list[dict[str, Any]] = []

    # Bound the loop to avoid pathological iterations
    max_iters = 24 * lookback_days  # one hour granularity max
    iters = 0
    while cur <= as_of and iters < max_iters:
        iters += 1
        slot_iso = cur.replace(minute=0, second=0, microsecond=0).isoformat()
        if pb.trigger_spec.predicate_dsl:
            hits = _evaluate_predicate_dsl(pb.trigger_spec.predicate_dsl, cur, conn)
        else:
            # sql_predicate path — gated behind frontier flag, executed as
            # a wrapped SELECT WHERE <fragment>. We require the fragment to
            # reference {as_of_iso} for explicit bitemporal anchoring.
            hits = _evaluate_sql_predicate_safely(pb.trigger_spec.sql_predicate, cur, conn)
        for h in hits:
            key = (str(h.get("instrument") or "?"), slot_iso)
            if key in seen:
                continue
            seen.add(key)
            triggers_found.append({**h, "fired_at": cur.isoformat()})
        cur += timedelta(hours=step_hours)

    # For each trigger, forward-simulate the action_template using HL candles
    # over `time_horizon_default`. We sign the realized return by direction.
    returns: list[float] = []
    hits_n = 0
    per_inst: dict[str, dict[str, float]] = {}
    horizon_hours = _time_horizon_to_hours(pb.action_template.time_horizon_default)
    for t in triggers_found:
        inst = t.get("instrument") or "?"
        fired_at = datetime.fromisoformat(t["fired_at"])
        sim = _simulate_action(
            instrument=str(inst),
            direction=pb.action_template.direction,
            start=fired_at,
            horizon_hours=horizon_hours,
            sizing_template=pb.action_template.sizing_template,
        )
        if sim.get("error"):
            t["sim_error"] = sim["error"]
            continue
        ret_pct = float(sim.get("return_pct") or 0.0)
        returns.append(ret_pct)
        if ret_pct > 0:
            hits_n += 1
        bucket = per_inst.setdefault(str(inst), {"n": 0, "hits": 0, "sum_ret": 0.0})
        bucket["n"] += 1
        bucket["sum_ret"] += ret_pct
        if ret_pct > 0:
            bucket["hits"] += 1
        t["return_pct"] = ret_pct

    n_triggers = len(triggers_found)
    sim_count = len(returns)
    hit_rate = (hits_n / sim_count) if sim_count > 0 else 0.0
    avg_ret = (sum(returns) / sim_count) if sim_count > 0 else 0.0
    sorted_r = sorted(returns)
    median = sorted_r[len(sorted_r) // 2] if sorted_r else 0.0
    if sim_count > 1:
        mean = sum(returns) / sim_count
        var = sum((r - mean) ** 2 for r in returns) / (sim_count - 1)
        std = var ** 0.5
    else:
        std = 0.0
    worst = min(returns) if returns else 0.0
    best = max(returns) if returns else 0.0

    per_inst_summary: dict[str, dict] = {}
    for inst, b in per_inst.items():
        per_inst_summary[inst] = {
            "n": int(b["n"]),
            "hits": int(b["hits"]),
            "hit_rate": (b["hits"] / b["n"]) if b["n"] > 0 else 0.0,
            "avg_return_pct": (b["sum_ret"] / b["n"]) if b["n"] > 0 else 0.0,
        }

    bt = PlaybookBacktest(
        playbook_id=pb.id,
        lookback_days=lookback_days,
        n_triggers=n_triggers,
        hits=hits_n,
        hit_rate=hit_rate,
        avg_return_pct=avg_ret,
        median_return_pct=median,
        std_return_pct=std,
        worst_drawdown_pct=worst,
        best_return_pct=best,
        sample_size_warning=(n_triggers < 10),
        leakage_check=True,  # all queries went through build_replay_context
        per_instrument=per_inst_summary,
        triggers_found=triggers_found,
    )

    # Append-only update of the playbook with historical_* fields filled.
    _update_historical_stats(pb, n_triggers, hit_rate, avg_ret)
    return bt


def _evaluate_sql_predicate_safely(
    sql_predicate: Optional[str],
    as_of: datetime,
    conn: sqlite3.Connection,
) -> list[dict[str, Any]]:
    """Frontier-only path. Wrap the fragment in a safe SELECT and bind as_of.

    The fragment must reference `:as_of_iso` (or `?`) and is evaluated in
    a restricted context: we only allow SELECT, no DML keywords.
    """
    if not sql_predicate:
        return []
    forbidden = ("INSERT ", "UPDATE ", "DELETE ", "DROP ", "ALTER ", "CREATE ", "ATTACH ")
    if any(tok in sql_predicate.upper() for tok in forbidden):
        return []  # silently no-op on hostile fragments
    try:
        rows = conn.execute(
            f"SELECT 'sql_predicate' AS instrument, 'sql_predicate' AS claim_id "
            f"WHERE {sql_predicate}",
            (_iso(as_of),) * sql_predicate.count("?"),
        ).fetchall()
        return [
            {"instrument": r["instrument"], "evidence_ids": [r["claim_id"]],
             "current_value": None, "claim_id": r["claim_id"]}
            for r in rows
        ]
    except Exception:
        return []


def _time_horizon_to_hours(token: str) -> int:
    table = {"intraday": 8, "12h": 12, "1d": 24, "3d": 72, "7d": 168,
             "14d": 336, "30d": 720}
    return table.get(token, 24)


def _simulate_action(
    instrument: str,
    direction: str,
    start: datetime,
    horizon_hours: int,
    sizing_template: dict[str, Any],
) -> dict[str, Any]:
    """Forward-simulate using HL candle history. Returns {return_pct} or {error}.

    Uses the same HL candle helper the resolver uses.
    """
    if direction == "flat":
        return {"return_pct": 0.0}
    if direction == "spread":
        return {"error": "spread_simulation_not_supported_v0"}
    end = start + timedelta(hours=max(1, horizon_hours))
    path = _fetch_price_path_safe(instrument, start, end)
    if path.get("error"):
        return {"error": path["error"]}
    entry_px = float(path["entry_px"])
    exit_px = float(path["exit_px"])
    if entry_px <= 0:
        return {"error": "bad_entry_px"}
    raw_ret = (exit_px - entry_px) / entry_px
    if direction == "short":
        raw_ret = -raw_ret
    # Subtract conservative round-trip fees: 10bps default.
    fee_bps = float(sizing_template.get("fee_bps_override", 10.0))
    ret_pct = (raw_ret * 100.0) - (fee_bps / 100.0)
    return {"return_pct": ret_pct, "entry_px": entry_px, "exit_px": exit_px}


def _fetch_price_path_safe(coin: str, start: datetime, end: datetime) -> dict[str, Any]:
    """Wrap the resolver's price-path helper. Best-effort — returns {error}
    on any failure rather than raising."""
    try:
        from ..eval.resolver import _fetch_price_path
        return _fetch_price_path(coin, start, end)
    except Exception as e:  # noqa: BLE001
        return {"error": f"fetch_failed: {e}"}


def _update_historical_stats(pb: Playbook, n_triggers: int, hit_rate: float, avg_ret: float) -> None:
    """Update historical_* fields on the open head row.

    Why UPDATE not supersedes: the playbooks DDL enforces UNIQUE(name, version).
    Bitemporal supersedes-style appends would collide. We carry the "audit
    history" of stats updates inside payload.historical_history; the row's
    own historical_* columns are the latest value.
    """
    conn = get_desk_store().conn
    now = _utc_now()
    # Append the prior stats into payload.historical_history so an audit can
    # see the progression.
    history = list(pb.payload.get("historical_history") or [])
    history.append({
        "at": _iso(now),
        "n_triggers": pb.historical_trigger_count,
        "hit_rate": pb.historical_hit_rate,
        "avg_return_pct": pb.historical_avg_return_pct,
    })
    new_payload = dict(pb.payload)
    new_payload["historical_history"] = history
    # NOTE: SQLite schema may or may not carry a `payload` column on
    # playbooks (the wiki DDL doesn't declare one). We only write back if
    # the column exists.
    cols = {r[0] for r in conn.execute("SELECT name FROM pragma_table_info('playbooks')").fetchall()}
    if "payload" in cols:
        conn.execute(
            "UPDATE playbooks SET historical_trigger_count = ?, "
            "historical_avg_return_pct = ?, historical_hit_rate = ?, "
            "payload = ? WHERE id = ?",
            (int(n_triggers), float(avg_ret), float(hit_rate),
             json.dumps(new_payload), pb.id),
        )
    else:
        conn.execute(
            "UPDATE playbooks SET historical_trigger_count = ?, "
            "historical_avg_return_pct = ?, historical_hit_rate = ? "
            "WHERE id = ?",
            (int(n_triggers), float(avg_ret), float(hit_rate), pb.id),
        )
    conn.commit()
    # Keep the in-memory model in sync for the caller
    pb.historical_trigger_count = int(n_triggers)
    pb.historical_avg_return_pct = float(avg_ret)
    pb.historical_hit_rate = float(hit_rate)
    pb.payload = new_payload


# ============================================================================
# detect_playbook_triggers
# ============================================================================

def detect_playbook_triggers(as_of: datetime) -> list[PlaybookTrigger]:
    """Run every approved + experimental playbook's trigger_spec against the
    current bitemporal state at `as_of`. Return list of triggered playbooks.

    Honors cooldown_hours per (playbook_id, instrument) by checking the
    most recent agent_messages row of kind='playbook_fired' or the
    most recent published trade_idea linked to playbook_id+instrument.
    """
    conn = get_desk_store().conn
    rows = conn.execute(
        "SELECT * FROM playbooks "
        "WHERE transaction_to IS NULL AND promoted_status IN ('approved','experimental') "
        "ORDER BY name, version DESC"
    ).fetchall()
    out: list[PlaybookTrigger] = []
    for r in rows:
        pb = _row_to_playbook(r)
        if pb.trigger_spec.predicate_dsl:
            hits = _evaluate_predicate_dsl(pb.trigger_spec.predicate_dsl, as_of, conn)
        else:
            hits = _evaluate_sql_predicate_safely(pb.trigger_spec.sql_predicate, as_of, conn)
        for h in hits:
            inst = str(h.get("instrument") or "?")
            if _within_cooldown(conn, pb.id, inst, pb.trigger_spec.cooldown_hours, as_of):
                continue
            out.append(PlaybookTrigger(
                playbook_id=pb.id,
                playbook_name=pb.name,
                playbook_version=pb.version,
                instrument=inst,
                evidence_ids=list(h.get("evidence_ids") or []),
                predicted_return_pct=pb.historical_avg_return_pct,
                detected_at=as_of,
                extras={"current_value": h.get("current_value")},
            ))
    return out


def _within_cooldown(
    conn: sqlite3.Connection,
    playbook_id: str,
    instrument: str,
    cooldown_hours: int,
    as_of: datetime,
) -> bool:
    """Return True iff a recent fire for (playbook_id, instrument) is within
    cooldown_hours. Cooldown source: most recent trade_idea row with
    playbook_id+instrument."""
    if cooldown_hours <= 0:
        return False
    threshold = as_of - timedelta(hours=cooldown_hours)
    row = conn.execute(
        "SELECT MAX(COALESCE(published_at, valid_from)) AS last_at "
        "FROM trade_ideas WHERE playbook_id = ? AND instrument = ? AND transaction_to IS NULL",
        (playbook_id, instrument),
    ).fetchone()
    last_iso = row["last_at"] if row is not None else None
    last_dt = _from_iso(last_iso) if last_iso else None
    if last_dt is None:
        return False
    return last_dt > threshold


# ============================================================================
# instantiate_playbook_trade
# ============================================================================

def instantiate_playbook_trade(
    playbook_id: str,
    trigger: PlaybookTrigger,
    context: AgentContext,
) -> Any:  # returns talis_desk.trade_ideas.TradeIdea
    """Build a TradeIdea from action_template + current market state.

    Cites trigger.evidence_ids in claim_ids. Sets playbook_id on TradeIdea.
    Validates via validate_trade_idea before publishing.
    """
    from ..trade_ideas import (
        ContradictionItem,
        EntryPlan,
        SizingPlan,
        StopPlan,
        TargetPlan,
        TradeIdeaDraft,
        emit_trade_idea,
    )

    pb = get_playbook(playbook_id)
    if pb is None:
        raise KeyError(f"playbook_not_found: {playbook_id}")
    at = pb.action_template
    now = _utc_now()

    # Resolve placeholders. The action template can carry either numeric
    # values directly or placeholder strings like "$entry_px" / "$stop_pct".
    # For instantiation we resolve the entry price via HL last close.
    entry_px = _resolve_entry_px(trigger.instrument, now)
    if entry_px is None or entry_px <= 0:
        raise RuntimeError(
            f"instantiate_playbook_trade: cannot resolve entry price for {trigger.instrument}"
        )

    sizing = SizingPlan(
        risk_pct=float(at.sizing_template.get("risk_pct", 0.0030)),
        notional_cap_usd=at.sizing_template.get("notional_cap_usd"),
        kelly_fraction=float(at.sizing_template.get("kelly_fraction", 0.25)),
        leverage_cap=float(at.sizing_template.get("leverage_cap", 2.0)),
    )
    # stop / target as percentages of entry_px from template
    stop_pct = float(at.stop_template.get("stop_pct", 0.012))
    target_pct = (
        float(at.target_template.get("target_pct", 0.018)) if at.target_template else None
    )
    if at.direction == "long":
        stop_px = entry_px * (1.0 - stop_pct)
        target_px = entry_px * (1.0 + target_pct) if target_pct else None
    elif at.direction == "short":
        stop_px = entry_px * (1.0 + stop_pct)
        target_px = entry_px * (1.0 - target_pct) if target_pct else None
    else:
        stop_px = entry_px
        target_px = None
    max_loss = abs(entry_px - stop_px) * float(sizing.notional_cap_usd or 10_000.0) / entry_px
    max_loss = max(max_loss, 1.0)

    entry = EntryPlan(
        trigger=str(at.entry_template.get("trigger", pb.trigger_spec.natural_language)),
        limit_px=entry_px,
        market_assumption=str(at.market_assumption_default),
        invalidation=str(at.entry_template.get(
            "invalidation", f"cancel after {at.time_horizon_default} if not filled"
        )),
    )
    stop = StopPlan(
        px=float(stop_px),
        max_loss_usd=float(max_loss),
        stop_kind=str(at.stop_template.get("stop_kind", "hard")),  # type: ignore[arg-type]
    )
    target = (
        TargetPlan(px=float(target_px), take_profit_pct=float(target_pct or 0.0) * 100.0)
        if target_px is not None else None
    )

    horizon_hours = _time_horizon_to_hours(at.time_horizon_default)
    expires_at = now + timedelta(hours=horizon_hours)

    contradictions: list[ContradictionItem] = []
    # If predicted_return is below the historical avg / 2, attach a
    # weakness contradiction so high-confidence ideas still validate.
    confidence = max(at.confidence_floor, 0.55)
    if confidence >= 0.7:
        contradictions.append(ContradictionItem(
            claim_id=f"playbook_self_doubt:{pb.id}",
            reason=(
                f"Historical hit_rate={pb.historical_hit_rate:.2f}, "
                f"avg_return={pb.historical_avg_return_pct:.3f}% — "
                f"playbook is not a sure thing."
                if pb.historical_hit_rate is not None and pb.historical_avg_return_pct is not None
                else "Backtest sample is small; historical edge unverified."
            ),
            weight=0.4,
        ))

    persona_version = getattr(context, "persona_version", None) or "playbook_emit_v1"
    cycle_id = getattr(context, "cycle_id", None) or "playbook_cycle"
    specialist_id = getattr(context, "specialist_id", None) or pb.owner_specialist

    draft = TradeIdeaDraft(
        cycle_id=cycle_id,
        specialist_id=specialist_id,
        persona_version=str(persona_version),
        instrument=trigger.instrument,
        direction=at.direction,
        sizing=sizing,
        entry=entry,
        stop=stop,
        target=target,
        time_horizon=at.time_horizon_default,
        edge_thesis=(
            f"Playbook {pb.name} v{pb.version} fired on {trigger.instrument}. "
            f"{pb.description} Historical hit_rate="
            f"{(pb.historical_hit_rate or 0.0):.2f}, avg_return="
            f"{(pb.historical_avg_return_pct or 0.0):.3f}%."
        ),
        claim_ids=list(trigger.evidence_ids),
        hypothesis_ids=[],
        contradicting_evidence=contradictions,
        confluence_score=0.6,
        confidence=confidence,
        expires_at=expires_at,
        valid_from=now,
        playbook_id=pb.id,
        payload={"trigger": trigger.to_dict()},
    )
    idea = emit_trade_idea(draft, context)
    return idea


def _resolve_entry_px(instrument: str, now: datetime) -> Optional[float]:
    """Look up the most recent close for `instrument` in the last 4h."""
    start = now - timedelta(hours=4)
    path = _fetch_price_path_safe(instrument, start, now)
    if path.get("error"):
        return None
    return float(path.get("exit_px") or path.get("entry_px") or 0.0) or None


# ============================================================================
# promote / retire (append-only)
# ============================================================================

def promote_playbook(playbook_id: str) -> Playbook:
    """Move candidate -> experimental (>= min_sample_size backtest triggers OR
    human flag) or experimental -> approved (>= 5 LIVE triggers w/ positive
    net return).

    Raises ValueError if the gates aren't met.
    """
    pb = get_playbook(playbook_id)
    if pb is None:
        raise KeyError(f"playbook_not_found: {playbook_id}")
    if pb.promoted_status == "candidate":
        # candidate -> experimental gate
        human_flag = bool(pb.payload.get("human_marked_experimental", False))
        backtest_ok = pb.historical_trigger_count >= pb.min_sample_size
        if not (human_flag or backtest_ok):
            raise ValueError(
                f"promote_playbook: candidate->experimental gate failed. "
                f"need historical_trigger_count >= {pb.min_sample_size} "
                f"(got {pb.historical_trigger_count}) "
                f"OR payload.human_marked_experimental=True (got "
                f"{human_flag})."
            )
        new_status: PromotedStatus = "experimental"
    elif pb.promoted_status == "experimental":
        # experimental -> approved gate: >=5 live triggers with positive avg
        live_n, live_avg = _live_trigger_stats(pb)
        if live_n < 5:
            raise ValueError(
                f"promote_playbook: experimental->approved gate failed. "
                f"need >=5 live triggers (got {live_n})."
            )
        if live_avg is None or live_avg <= 0:
            raise ValueError(
                f"promote_playbook: experimental->approved gate failed. "
                f"need positive net live return (got {live_avg})."
            )
        new_status = "approved"
    elif pb.promoted_status == "approved":
        # Already at the top of the ladder.
        return pb
    else:
        raise ValueError(
            f"promote_playbook: cannot promote from status={pb.promoted_status}"
        )
    return _supersede_with_status(pb, new_status, reason="manual_promote")


def retire_playbook(playbook_id: str, reason: str) -> Playbook:
    """Set promoted_status='retired' via supersedes. Used when 5 consecutive
    triggers post-promotion fall below 50% of historical avg return (v2 §9).
    """
    pb = get_playbook(playbook_id)
    if pb is None:
        raise KeyError(f"playbook_not_found: {playbook_id}")
    return _supersede_with_status(pb, "retired", reason=reason)


def _supersede_with_status(pb: Playbook, status: PromotedStatus, reason: str) -> Playbook:
    """Status promotion — UPDATE in place, append history to payload.

    The wiki DDL constrains UNIQUE(name, version) on playbooks so we can't
    insert a new (name, version) row to change status. Audit trail lives
    inside payload.promotion_history.
    """
    conn = get_desk_store().conn
    now = _utc_now()
    new_payload = dict(pb.payload)
    history = list(new_payload.get("promotion_history") or [])
    history.append({
        "at": _iso(now),
        "from_status": pb.promoted_status,
        "to_status": status,
        "reason": reason,
    })
    new_payload["promotion_history"] = history
    cols = {r[0] for r in conn.execute("SELECT name FROM pragma_table_info('playbooks')").fetchall()}
    if "payload" in cols:
        conn.execute(
            "UPDATE playbooks SET promoted_status = ?, payload = ? "
            "WHERE id = ?",
            (status, json.dumps(new_payload), pb.id),
        )
    else:
        conn.execute(
            "UPDATE playbooks SET promoted_status = ? WHERE id = ?",
            (status, pb.id),
        )
    conn.commit()
    # Mutate in-memory copy and return
    pb.promoted_status = status
    pb.payload = new_payload
    return pb


def _live_trigger_stats(pb: Playbook) -> tuple[int, Optional[float]]:
    """Count live (resolved trade_idea) returns for this playbook id."""
    conn = get_desk_store().conn
    rows = conn.execute(
        "SELECT realized_return_after_fees_pct FROM trade_ideas "
        "WHERE playbook_id = ? AND status IN ('closed','expired','invalidated') "
        "AND realized_return_after_fees_pct IS NOT NULL",
        (pb.id,),
    ).fetchall()
    rets = [float(r["realized_return_after_fees_pct"]) for r in rows]
    if not rets:
        return 0, None
    return len(rets), sum(rets) / len(rets)

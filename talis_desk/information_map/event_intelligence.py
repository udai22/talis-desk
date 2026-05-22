"""Market-event intelligence bundles.

Hyperview-style products surface the event row. Talis needs the event row plus
the data underneath the interpretation: actor identity, liquidity context,
derivatives positioning, historical analogs, scenarios, and watch triggers.
This module keeps that bundle typed, persisted, and convertible into an
information string for the map.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from ..store import get_desk_store
from .store import InformationString


DATA_CATEGORIES = {
    "event_fact",
    "actor_profile",
    "liquidity_context",
    "derivatives_context",
    "historical_analog",
    "scenario",
    "watch_trigger",
    "raw_source",
}

EVENT_INTELLIGENCE_KEYWORDS = (
    "unstake",
    "unstaking",
    "stake",
    "staking",
    "unlock",
    "vesting",
    "validator",
    "whale",
    "deposit",
    "withdraw",
    "withdrawal",
    "transfer",
    "bridge",
    "cex",
    "exchange",
    "mint",
    "burn",
    "buyback",
    "insider",
    "block trade",
)


@dataclass
class EventDataPoint:
    """One atomic datapoint behind a market-event read."""

    category: str
    label: str
    value: Any = ""
    numeric_value: Optional[float] = None
    unit: str = ""
    source_ref: str = ""
    confidence: float = 0.5
    observed_at: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    data_point_id: Optional[str] = None


@dataclass
class ActorProfile:
    label: str = ""
    address: str = ""
    cluster_id: str = ""
    actor_type: str = ""
    confidence: float = 0.5
    prior_behavior: list[str] = field(default_factory=list)
    source_refs: list[str] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class EventScenario:
    name: str
    probability: float
    thesis: str
    expected_outcome: str
    trigger: str
    invalidator: str = ""
    source_refs: list[str] = field(default_factory=list)


@dataclass
class WatchTrigger:
    kind: str
    description: str
    horizon: str
    direction: str = ""
    severity: str = "yellow"
    source_refs: list[str] = field(default_factory=list)
    status: str = "active"


@dataclass
class EventIntelligenceQuality:
    score: float
    flags: list[str] = field(default_factory=list)
    n_data_points: int = 0

    @property
    def passed(self) -> bool:
        blockers = {
            "missing_entity",
            "missing_event_type",
            "missing_amount",
            "missing_event_time",
            "missing_source_refs",
            "missing_liquidity_context",
            "missing_watch_triggers",
        }
        return self.score >= 0.72 and not blockers.intersection(self.flags)


@dataclass
class MarketEventIntelligenceBundle:
    """Complete data bundle for one market-moving event."""

    cycle_id: str
    entity: str
    event_type: str
    asset: str = ""
    protocol: str = ""
    event_time: str = ""
    source_time_basis: str = "event_time"
    amount: Optional[float] = None
    amount_unit: str = ""
    notional_usd: Optional[float] = None
    actor: ActorProfile = field(default_factory=ActorProfile)
    summary: str = ""
    base_case: str = ""
    bull_case: str = ""
    bear_case: str = ""
    kill_signal: str = ""
    directional_bias: str = "neutral"
    severity_score: float = 0.5
    intelligence_score: float = 0.5
    liquidity_context: list[EventDataPoint] = field(default_factory=list)
    derivatives_context: list[EventDataPoint] = field(default_factory=list)
    historical_analogs: list[EventDataPoint] = field(default_factory=list)
    raw_sources: list[EventDataPoint] = field(default_factory=list)
    scenarios: list[EventScenario] = field(default_factory=list)
    watch_triggers: list[WatchTrigger] = field(default_factory=list)
    source_refs: list[str] = field(default_factory=list)
    quality_flags: list[str] = field(default_factory=list)
    raw_event: dict[str, Any] = field(default_factory=dict)
    bundle_id: Optional[str] = None


def score_event_intelligence(bundle: MarketEventIntelligenceBundle) -> EventIntelligenceQuality:
    """Score whether the event has enough data to support an intelligent read."""
    flags: list[str] = []
    if not bundle.entity:
        flags.append("missing_entity")
    if not bundle.event_type:
        flags.append("missing_event_type")
    if bundle.amount is None:
        flags.append("missing_amount")
    if not bundle.event_time:
        flags.append("missing_event_time")
    if not bundle.source_refs:
        flags.append("missing_source_refs")
    if not (bundle.actor.label or bundle.actor.address or bundle.actor.cluster_id):
        flags.append("missing_actor_profile")
    if not bundle.liquidity_context:
        flags.append("missing_liquidity_context")
    if not bundle.derivatives_context:
        flags.append("missing_derivatives_context")
    if not bundle.historical_analogs:
        flags.append("missing_historical_analogs")
    if not bundle.scenarios:
        flags.append("missing_scenarios")
    if not bundle.watch_triggers:
        flags.append("missing_watch_triggers")
    if bundle.directional_bias not in {"bullish", "bearish", "neutral", "mixed", "unknown"}:
        flags.append("bad_directional_bias")

    n_points = len(_all_data_points(bundle))
    score = 0.0
    score += 0.18 if bundle.entity and bundle.event_type and bundle.amount is not None else 0.0
    score += 0.12 if bundle.event_time else 0.0
    score += 0.13 if bundle.actor.label or bundle.actor.address or bundle.actor.cluster_id else 0.0
    score += 0.16 if bundle.liquidity_context else 0.0
    score += 0.12 if bundle.derivatives_context else 0.0
    score += 0.08 if bundle.historical_analogs else 0.0
    score += 0.09 if bundle.scenarios else 0.0
    score += 0.08 if bundle.watch_triggers else 0.0
    score += min(0.09, 0.015 * len(bundle.source_refs))
    score += min(0.05, 0.005 * n_points)
    return EventIntelligenceQuality(
        score=round(min(1.0, score), 3),
        flags=sorted(set(flags + bundle.quality_flags)),
        n_data_points=n_points,
    )


def persist_event_intelligence(
    bundle: MarketEventIntelligenceBundle,
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> str:
    """Persist the bundle and every supporting datapoint."""
    db = conn or get_desk_store().conn
    now = datetime.now(timezone.utc).isoformat()
    quality = score_event_intelligence(bundle)
    bundle.quality_flags = sorted(set(bundle.quality_flags + quality.flags))
    bundle.intelligence_score = quality.score
    bundle_id = bundle.bundle_id or _bundle_id(bundle)
    bundle.bundle_id = bundle_id
    db.execute(
        """
        INSERT OR REPLACE INTO market_event_intelligence (
            id, cycle_id, event_type, entity, asset, protocol, event_time,
            source_time_basis, actor_label, actor_address, actor_cluster_id,
            actor_type, amount, amount_unit, notional_usd, severity_score,
            intelligence_score, directional_bias, summary, base_case, bull_case,
            bear_case, kill_signal, source_refs_json, quality_flags,
            raw_event_json, created_at, valid_from, transaction_from,
            transaction_to
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
        """,
        (
            bundle_id,
            bundle.cycle_id,
            bundle.event_type,
            bundle.entity,
            bundle.asset or bundle.entity,
            bundle.protocol,
            bundle.event_time,
            bundle.source_time_basis,
            bundle.actor.label,
            bundle.actor.address,
            bundle.actor.cluster_id,
            bundle.actor.actor_type,
            bundle.amount,
            bundle.amount_unit,
            bundle.notional_usd,
            bundle.severity_score,
            bundle.intelligence_score,
            bundle.directional_bias,
            bundle.summary,
            bundle.base_case,
            bundle.bull_case,
            bundle.bear_case,
            bundle.kill_signal,
            json.dumps(bundle.source_refs),
            json.dumps(bundle.quality_flags),
            json.dumps(bundle.raw_event, sort_keys=True),
            now,
            now,
            now,
        ),
    )
    for point in _all_data_points(bundle):
        _persist_data_point(db, bundle_id, bundle.cycle_id, point, now)
    for trigger in bundle.watch_triggers:
        _persist_watch_trigger(db, bundle_id, bundle.cycle_id, trigger, now)
    db.commit()
    return bundle_id


def event_intelligence_to_information_string(
    bundle: MarketEventIntelligenceBundle,
) -> InformationString:
    """Convert a complete event bundle into a scout information string."""
    amount = _format_amount(bundle.amount, bundle.amount_unit)
    actor = bundle.actor.label or bundle.actor.address or bundle.actor.cluster_id or "unknown actor"
    event_label = f"{bundle.event_type} {amount}".strip()
    thesis = bundle.summary or (
        f"{bundle.entity} {event_label} by {actor} matters only through its "
        "post-event routing, liquidity absorption, and derivative positioning."
    )
    expected = bundle.base_case or bundle.bear_case or bundle.bull_case or (
        "Watch whether the event routes to exchange/liquidity venues or is absorbed without forced selling."
    )
    kill = bundle.kill_signal or _default_kill_signal(bundle)
    evidence = sorted(set(([bundle.bundle_id] if bundle.bundle_id else []) + bundle.source_refs + [
        p.source_ref for p in _all_data_points(bundle) if p.source_ref
    ]))
    depth_layers = [
        {"layer": 1, "claim": f"{bundle.event_type} scheduled/observed for {amount}."},
        {"layer": 2, "claim": f"Actor context: {actor}."},
        {"layer": 3, "claim": "Liquidity and derivatives context determine whether supply becomes impact."},
        {"layer": 4, "claim": "Scenario triggers separate sell pressure from benign restake/hold behavior."},
        {"layer": 5, "claim": kill},
    ]
    return InformationString(
        title=f"{bundle.entity} {bundle.event_type}: {actor}"[:140],
        thesis=thesis[:2000],
        mechanism=(bundle.base_case or bundle.bear_case or bundle.bull_case or thesis)[:2000],
        expected_outcome=expected[:1000],
        time_horizon=_event_horizon(bundle),
        time_scale=_event_horizon(bundle),
        event_time_start=bundle.event_time,
        event_time_end=bundle.event_time,
        source_time_basis=bundle.source_time_basis,
        kill_signal=kill[:1000],
        extends_or_contradicts="new",
        would_change_decision=score_event_intelligence(bundle).passed,
        expires_at=bundle.watch_triggers[0].horizon if bundle.watch_triggers else "",
        crowdedness=0.45,
        conviction=max(0.0, min(1.0, bundle.intelligence_score)),
        novelty_score=0.72,
        entities_chain=[x for x in [bundle.entity, actor, bundle.event_type, "liquidity_absorption"] if x],
        depth_layers=depth_layers,
        evidence_refs=evidence,
        temporal_confidence=max(0.0, min(1.0, bundle.intelligence_score)),
        quality_flags=[
            "from_market_event_intelligence",
            *(bundle.quality_flags or []),
        ],
    )


def load_event_intelligence(
    *,
    cycle_id: str = "",
    entity: str = "",
    event_type: str = "",
    limit: int = 50,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict[str, Any]]:
    db = conn or get_desk_store().conn
    clauses: list[str] = []
    params: list[Any] = []
    if cycle_id:
        clauses.append("cycle_id = ?")
        params.append(cycle_id)
    if entity:
        clauses.append("entity = ?")
        params.append(entity)
    if event_type:
        clauses.append("event_type = ?")
        params.append(event_type)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(int(limit))
    rows = db.execute(
        f"""
        SELECT * FROM market_event_intelligence
        {where}
        ORDER BY intelligence_score DESC, event_time ASC, created_at DESC
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        for key in ("source_refs_json", "quality_flags"):
            try:
                d[key] = json.loads(d.get(key) or "[]")
            except Exception:
                d[key] = []
        try:
            d["raw_event_json"] = json.loads(d.get("raw_event_json") or "{}")
        except Exception:
            d["raw_event_json"] = {}
        out.append(d)
    return out


def normalize_event_intelligence(raw: Any, *, cycle_id: str = "") -> Optional[MarketEventIntelligenceBundle]:
    """Normalize loose tool/model output into an event intelligence bundle."""
    if not isinstance(raw, dict):
        return None
    entity = _text(raw.get("entity") or raw.get("asset") or raw.get("coin"))
    event_type = _text(raw.get("event_type") or raw.get("kind") or raw.get("type"))
    if not entity or not event_type:
        return None
    actor_raw = raw.get("actor") or raw.get("actor_profile") or {}
    bundle = MarketEventIntelligenceBundle(
        cycle_id=_text(raw.get("cycle_id") or cycle_id),
        entity=entity,
        event_type=event_type,
        asset=_text(raw.get("asset") or entity),
        protocol=_text(raw.get("protocol") or raw.get("venue")),
        event_time=_text(raw.get("event_time") or raw.get("event_at") or raw.get("time")),
        amount=_as_float_or_none(raw.get("amount") or raw.get("token_amount")),
        amount_unit=_text(raw.get("amount_unit") or raw.get("unit") or entity),
        notional_usd=_as_float_or_none(raw.get("notional_usd") or raw.get("usd_value")),
        actor=_actor_profile(actor_raw),
        summary=_text(raw.get("summary")),
        base_case=_text(raw.get("base_case")),
        bull_case=_text(raw.get("bull_case")),
        bear_case=_text(raw.get("bear_case")),
        kill_signal=_text(raw.get("kill_signal")),
        directional_bias=_text(raw.get("directional_bias") or "neutral"),
        severity_score=_clamp01(raw.get("severity_score"), 0.5),
        intelligence_score=_clamp01(raw.get("intelligence_score"), 0.5),
        liquidity_context=_data_points(raw.get("liquidity_context"), "liquidity_context"),
        derivatives_context=_data_points(raw.get("derivatives_context"), "derivatives_context"),
        historical_analogs=_data_points(raw.get("historical_analogs"), "historical_analog"),
        raw_sources=_data_points(raw.get("raw_sources") or raw.get("sources"), "raw_source"),
        scenarios=_scenarios(raw.get("scenarios")),
        watch_triggers=_watch_triggers(raw.get("watch_triggers") or raw.get("triggers")),
        source_refs=_string_list(raw.get("source_refs") or raw.get("evidence_refs")),
        quality_flags=_string_list(raw.get("quality_flags")),
        raw_event=dict(raw.get("raw_event") or raw),
        bundle_id=_text(raw.get("bundle_id") or raw.get("id")) or None,
    )
    if not bundle.source_refs:
        bundle.source_refs = _collect_source_refs(bundle)
    return bundle


def event_intelligence_from_tool_evidence(
    *,
    cycle_id: str,
    entity: str,
    horizon: str = "",
    lens: str = "",
    tool_evidence: list[dict[str, Any]],
    max_bundles: int = 4,
) -> list[MarketEventIntelligenceBundle]:
    """Recover market-event bundles directly from persisted tool evidence.

    This is the anti-waste path: a scout may have real event data even if the
    LLM omits the optional `event_intelligence` object. We keep the raw row and
    attach any timeseries/metadata context we can see so the event remains
    queryable and can still become an information string.
    """
    if not tool_evidence:
        return []
    entity = _text(entity)
    liquidity, derivatives, historical = _context_points_from_tool_evidence(tool_evidence)
    bundles: list[MarketEventIntelligenceBundle] = []
    seen: set[str] = set()
    for ev in tool_evidence:
        result = ev.get("result")
        if result is None:
            result = _maybe_json(ev.get("summary"))
        for row in _event_rows(result):
            if not isinstance(row, dict):
                continue
            row_text = _json_or_text(row).lower()
            row_entity = _text(
                row.get("entity")
                or row.get("ticker")
                or row.get("asset")
                or row.get("coin")
                or _metadata_value(row, "entity", "ticker", "asset", "coin")
                or entity
            )
            if entity and row_entity and row_entity.upper() != entity.upper() and entity.lower() not in row_text:
                continue
            if not _looks_like_event_intelligence(row_text, lens):
                continue
            source_ref = _source_ref_for_evidence(ev)
            bundle = _bundle_from_event_row(
                row,
                cycle_id=cycle_id,
                entity=row_entity or entity,
                horizon=horizon,
                source_ref=source_ref,
                liquidity_context=liquidity,
                derivatives_context=derivatives,
                historical_analogs=historical,
            )
            if bundle is None:
                continue
            key = _bundle_id(bundle)
            if key in seen:
                continue
            seen.add(key)
            bundle.bundle_id = key
            bundles.append(bundle)
            if len(bundles) >= max_bundles:
                return bundles
    return bundles


def _persist_data_point(db: sqlite3.Connection, bundle_id: str, cycle_id: str, point: EventDataPoint, now: str) -> str:
    point_id = point.data_point_id or _data_point_id(bundle_id, point)
    point.data_point_id = point_id
    category = point.category if point.category in DATA_CATEGORIES else "raw_source"
    db.execute(
        """
        INSERT OR REPLACE INTO market_event_data_points (
            id, bundle_id, cycle_id, category, label, value_text,
            numeric_value, unit, source_ref, confidence, observed_at,
            payload_json, created_at, valid_from, transaction_from,
            transaction_to
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
        """,
        (
            point_id,
            bundle_id,
            cycle_id,
            category,
            point.label[:240],
            _json_or_text(point.value)[:2000],
            point.numeric_value,
            point.unit,
            point.source_ref,
            _clamp01(point.confidence, 0.5),
            point.observed_at,
            json.dumps(point.payload, sort_keys=True),
            now,
            now,
            now,
        ),
    )
    return point_id


def _persist_watch_trigger(db: sqlite3.Connection, bundle_id: str, cycle_id: str, trigger: WatchTrigger, now: str) -> str:
    trigger_id = "metr_" + hashlib.sha256(
        f"{bundle_id}|{trigger.kind}|{trigger.description}|{trigger.horizon}".encode()
    ).hexdigest()[:16]
    db.execute(
        """
        INSERT OR REPLACE INTO market_event_watch_triggers (
            id, bundle_id, cycle_id, trigger_kind, description, horizon,
            direction, severity, source_refs_json, status, created_at,
            valid_from, transaction_from, transaction_to
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
        """,
        (
            trigger_id,
            bundle_id,
            cycle_id,
            trigger.kind,
            trigger.description[:1000],
            trigger.horizon,
            trigger.direction,
            trigger.severity,
            json.dumps(trigger.source_refs),
            trigger.status,
            now,
            now,
            now,
        ),
    )
    return trigger_id


def _bundle_from_event_row(
    row: dict[str, Any],
    *,
    cycle_id: str,
    entity: str,
    horizon: str,
    source_ref: str,
    liquidity_context: list[EventDataPoint],
    derivatives_context: list[EventDataPoint],
    historical_analogs: list[EventDataPoint],
) -> Optional[MarketEventIntelligenceBundle]:
    amount, amount_unit = _event_amount(row, entity)
    event_type = _event_type(row)
    event_time = _event_time(row)
    if not event_type:
        return None
    source_refs = sorted(set([x for x in [
        source_ref,
        _text(row.get("source")),
        _text(row.get("source_ref")),
        _text(row.get("tool_call_log_id")),
    ] if x]))
    actor = _event_actor(row, source_refs)
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    raw_liquidity = _metadata_context_points(metadata, "liquidity_context", source_ref)
    raw_derivatives = _metadata_context_points(metadata, "derivatives_context", source_ref)
    bundle_liquidity = raw_liquidity + _clone_points(liquidity_context, source_ref=source_ref)
    bundle_derivatives = raw_derivatives + _clone_points(derivatives_context, source_ref=source_ref)
    bundle_historical = _clone_points(historical_analogs, source_ref=source_ref)
    flags: list[str] = ["from_tool_evidence_fallback"]
    if not bundle_liquidity:
        flags.append("fallback_missing_liquidity_context")
    if not bundle_derivatives:
        flags.append("fallback_missing_derivatives_context")
    if not bundle_historical:
        flags.append("fallback_missing_historical_analogs")
    amount_label = _format_amount(amount, amount_unit or entity)
    actor_label = actor.label or actor.address or actor.cluster_id or "unknown actor"
    summary = _text(row.get("headline") or row.get("summary") or row.get("title")) or (
        f"{entity} {event_type} {amount_label} by {actor_label} requires route and absorption monitoring."
    )
    base_case = _text(row.get("base_case")) or (
        f"Base case is not the row itself; it is whether {actor_label} routes the {amount_label} to sellable liquidity."
    )
    return MarketEventIntelligenceBundle(
        cycle_id=cycle_id,
        entity=entity,
        event_type=event_type,
        asset=_text(row.get("asset") or row.get("coin") or entity),
        protocol=_text(row.get("protocol") or row.get("venue") or _metadata_value(row, "protocol", "venue")),
        event_time=event_time,
        amount=amount,
        amount_unit=amount_unit or entity,
        notional_usd=_as_float_or_none(
            row.get("notional_usd")
            or row.get("usd_value")
            or _metadata_value(row, "notional_usd", "usd_value", "value_usd")
        ),
        actor=actor,
        summary=summary,
        base_case=base_case,
        bear_case=_text(row.get("bear_case")) or (
            f"Bearish if {actor_label} sends {entity} to a CEX/bridge while liquidity is thin or perps are crowded long."
        ),
        bull_case=_text(row.get("bull_case")) or (
            f"Bullish if the tokens remain idle/restaked and price absorbs the unlock without rising sell pressure."
        ),
        kill_signal=_text(row.get("kill_signal")) or (
            f"No CEX deposit, bridge, sell route, or liquidity/derivative stress after {event_type} inside {horizon or 'the watch window'}."
        ),
        directional_bias=_text(row.get("directional_bias") or "mixed"),
        severity_score=_clamp01(row.get("impact_score") or row.get("severity_score"), 0.55),
        liquidity_context=bundle_liquidity[:12],
        derivatives_context=bundle_derivatives[:12],
        historical_analogs=bundle_historical[:8],
        raw_sources=[
            EventDataPoint(
                "raw_source",
                "event_row",
                row,
                source_ref=source_ref,
                observed_at=event_time,
                confidence=0.8,
            )
        ],
        scenarios=_default_scenarios(entity, event_type, amount_label, actor_label, source_refs),
        watch_triggers=_default_watch_triggers(entity, horizon, source_refs),
        source_refs=source_refs,
        quality_flags=flags,
        raw_event=row,
    )


def _event_rows(result: Any) -> list[dict[str, Any]]:
    if not isinstance(result, dict):
        return []
    for key in ("events", "rows", "items", "data"):
        raw = result.get(key)
        if isinstance(raw, list):
            return [x for x in raw if isinstance(x, dict)]
    if result.get("event_type") or result.get("headline"):
        return [result]
    return []


def _context_points_from_tool_evidence(
    tool_evidence: list[dict[str, Any]],
) -> tuple[list[EventDataPoint], list[EventDataPoint], list[EventDataPoint]]:
    liquidity: list[EventDataPoint] = []
    derivatives: list[EventDataPoint] = []
    historical: list[EventDataPoint] = []
    for ev in tool_evidence or []:
        result = ev.get("result")
        if result is None:
            result = _maybe_json(ev.get("summary"))
        source_ref = _source_ref_for_evidence(ev)
        if not isinstance(result, dict):
            continue
        for point in result.get("points") or []:
            if not isinstance(point, dict):
                continue
            metric = _text(point.get("metric") or point.get("label") or point.get("name"))
            category = _metric_category(metric)
            if not category:
                continue
            data_point = EventDataPoint(
                category=category,
                label=metric,
                value=point.get("value", point),
                numeric_value=_as_float_or_none(point.get("value")),
                source_ref=source_ref,
                observed_at=_text(point.get("ts") or point.get("time") or point.get("observed_at")),
                confidence=0.65,
                payload=point,
            )
            if category == "liquidity_context":
                liquidity.append(data_point)
            elif category == "derivatives_context":
                derivatives.append(data_point)
            elif category == "historical_analog":
                historical.append(data_point)
        metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        liquidity.extend(_metadata_context_points(metadata, "liquidity_context", source_ref))
        derivatives.extend(_metadata_context_points(metadata, "derivatives_context", source_ref))
    return _dedupe_points(liquidity)[:12], _dedupe_points(derivatives)[:12], _dedupe_points(historical)[:8]


def _metadata_context_points(metadata: dict[str, Any], category: str, source_ref: str) -> list[EventDataPoint]:
    if not isinstance(metadata, dict):
        return []
    if category == "liquidity_context":
        needles = ("volume", "depth", "liquidity", "float", "supply", "market_cap", "turnover", "tvl")
    elif category == "derivatives_context":
        needles = ("funding", "open_interest", "oi", "perp", "basis", "liquidation", "leverage")
    else:
        needles = ()
    out: list[EventDataPoint] = []
    for key, value in metadata.items():
        lower = str(key).lower()
        if not any(n in lower for n in needles):
            continue
        out.append(EventDataPoint(
            category=category,
            label=str(key),
            value=value,
            numeric_value=_as_float_or_none(value),
            source_ref=source_ref,
            confidence=0.65,
            payload={"metadata_key": key},
        ))
    return out


def _event_type(row: dict[str, Any]) -> str:
    raw = _text(
        row.get("event_type")
        or row.get("kind")
        or row.get("type")
        or _metadata_value(row, "event_type", "kind", "type")
    ).lower()
    text = _json_or_text(row).lower()
    if raw:
        return raw
    for keyword in EVENT_INTELLIGENCE_KEYWORDS:
        if keyword in text:
            return keyword.replace(" ", "_")
    return ""


def _event_time(row: dict[str, Any]) -> str:
    return _text(
        row.get("event_time")
        or row.get("event_at")
        or row.get("time")
        or row.get("ts")
        or row.get("timestamp")
        or _metadata_value(row, "event_time", "time", "timestamp")
    )


def _event_amount(row: dict[str, Any], entity: str) -> tuple[Optional[float], str]:
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    amount = _as_float_or_none(
        row.get("amount")
        or row.get("token_amount")
        or row.get("quantity")
        or row.get("qty")
        or row.get("size")
        or metadata.get("amount")
        or metadata.get("token_amount")
        or metadata.get("quantity")
        or metadata.get("qty")
        or metadata.get("size")
    )
    unit = _text(
        row.get("amount_unit")
        or row.get("unit")
        or row.get("asset")
        or metadata.get("amount_unit")
        or metadata.get("unit")
        or metadata.get("asset")
        or entity
    )
    if amount is not None:
        return amount, unit
    parsed_amount, parsed_unit = _extract_amount_from_text(_json_or_text(row), entity)
    return parsed_amount, parsed_unit or unit


def _event_actor(row: dict[str, Any], source_refs: list[str]) -> ActorProfile:
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    label = _text(
        row.get("actor")
        or row.get("label")
        or row.get("address_label")
        or metadata.get("actor")
        or metadata.get("label")
        or metadata.get("address_label")
        or metadata.get("from_label")
        or metadata.get("wallet_label")
    )
    address = _text(
        row.get("address")
        or row.get("wallet")
        or row.get("from_address")
        or metadata.get("address")
        or metadata.get("wallet")
        or metadata.get("from_address")
    )
    cluster = _text(metadata.get("cluster_id") or metadata.get("cluster"))
    actor_type = _text(metadata.get("actor_type") or metadata.get("type"))
    return ActorProfile(
        label=label,
        address=address,
        cluster_id=cluster,
        actor_type=actor_type,
        confidence=0.68 if (label or address or cluster) else 0.45,
        prior_behavior=_string_list(metadata.get("prior_behavior") or metadata.get("prior_behaviors")),
        source_refs=source_refs,
        payload=metadata,
    )


def _default_scenarios(
    entity: str,
    event_type: str,
    amount_label: str,
    actor_label: str,
    source_refs: list[str],
) -> list[EventScenario]:
    return [
        EventScenario(
            "benign_absorption",
            0.40,
            f"{actor_label} keeps/restakes the {amount_label}; the event does not become sellable supply.",
            f"{entity} absorbs the event with limited spot impact.",
            "No CEX/bridge/sell route in the first watch window.",
            source_refs=source_refs,
        ),
        EventScenario(
            "sellable_supply",
            0.35,
            f"{event_type} turns into exchange-routable supply from {actor_label}.",
            f"{entity} faces spot pressure and possible derivative de-risking.",
            "CEX deposit, bridge, or large transfer after the event.",
            "Tokens remain idle/restaked and liquidity absorbs the flow.",
            source_refs,
        ),
        EventScenario(
            "reflexive_squeeze",
            0.25,
            "The crowd over-weights the event row; no sell route appears while shorts/liquidity chase.",
            f"{entity} can squeeze higher if the feared supply does not materialize.",
            "No transfer plus improving order-book/derivatives context.",
            "Confirmed sell route or deteriorating depth.",
            source_refs,
        ),
    ]


def _default_watch_triggers(entity: str, horizon: str, source_refs: list[str]) -> list[WatchTrigger]:
    short = "T+90m" if horizon in {"tick", "minute", "intraday", "1d", ""} else horizon
    return [
        WatchTrigger(
            "cex_or_bridge_route",
            f"Alert if {entity} moves to a CEX-labeled wallet, bridge, or other sellable venue.",
            short,
            "bearish",
            "red",
            source_refs,
        ),
        WatchTrigger(
            "absorption_or_restake",
            f"Alert if {entity} remains idle/restaked and liquidity absorbs the event without stress.",
            "T+2h" if short == "T+90m" else horizon,
            "bullish",
            "green",
            source_refs,
        ),
    ]


def _looks_like_event_intelligence(text: str, lens: str = "") -> bool:
    return any(keyword in text for keyword in EVENT_INTELLIGENCE_KEYWORDS)


def _metric_category(metric: str) -> str:
    lower = metric.lower()
    if any(x in lower for x in ("volume", "depth", "liquidity", "orderbook", "float", "supply", "turnover", "tvl")):
        return "liquidity_context"
    if any(x in lower for x in ("funding", "open_interest", "oi", "perp", "basis", "liquidation", "leverage")):
        return "derivatives_context"
    if any(x in lower for x in ("prior", "historical", "return", "drawdown", "volatility")):
        return "historical_analog"
    return ""


def _source_ref_for_evidence(ev: dict[str, Any]) -> str:
    return _text(ev.get("tool_call_log_id") or ev.get("uri") or ev.get("source") or ev.get("id"))


def _metadata_value(row: dict[str, Any], *keys: str) -> Any:
    metadata = row.get("metadata")
    if not isinstance(metadata, dict):
        return None
    for key in keys:
        if key in metadata:
            return metadata[key]
    return None


def _extract_amount_from_text(text: str, entity: str) -> tuple[Optional[float], str]:
    entity = re.escape(entity or "")
    patterns = [
        rf"([\d,.]+)\s*([kKmMbB])?\s*({entity})\b" if entity else "",
        r"([\d,.]+)\s*([kKmMbB])?\s*([A-Z]{2,12})\b",
    ]
    for pattern in [p for p in patterns if p]:
        match = re.search(pattern, text)
        if not match:
            continue
        raw = match.group(1).replace(",", "")
        try:
            amount = float(raw)
        except Exception:
            continue
        suffix = (match.group(2) or "").lower()
        if suffix == "k":
            amount *= 1_000
        elif suffix == "m":
            amount *= 1_000_000
        elif suffix == "b":
            amount *= 1_000_000_000
        return amount, match.group(3)
    return None, ""


def _clone_points(points: list[EventDataPoint], *, source_ref: str) -> list[EventDataPoint]:
    out: list[EventDataPoint] = []
    for point in points:
        out.append(EventDataPoint(
            category=point.category,
            label=point.label,
            value=point.value,
            numeric_value=point.numeric_value,
            unit=point.unit,
            source_ref=point.source_ref or source_ref,
            confidence=point.confidence,
            observed_at=point.observed_at,
            payload=dict(point.payload or {}),
        ))
    return out


def _dedupe_points(points: list[EventDataPoint]) -> list[EventDataPoint]:
    out: list[EventDataPoint] = []
    seen: set[tuple[str, str, str]] = set()
    for point in points:
        key = (point.category, point.label, _json_or_text(point.value))
        if key in seen:
            continue
        seen.add(key)
        out.append(point)
    return out


def _maybe_json(raw: Any) -> Any:
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return None


def _all_data_points(bundle: MarketEventIntelligenceBundle) -> list[EventDataPoint]:
    points = [
        EventDataPoint("event_fact", "event_type", bundle.event_type, source_ref=_first(bundle.source_refs)),
        EventDataPoint("event_fact", "amount", bundle.amount, numeric_value=bundle.amount, unit=bundle.amount_unit, source_ref=_first(bundle.source_refs)),
        EventDataPoint("event_fact", "event_time", bundle.event_time, source_ref=_first(bundle.source_refs)),
    ]
    if bundle.notional_usd is not None:
        points.append(EventDataPoint("event_fact", "notional_usd", bundle.notional_usd, numeric_value=bundle.notional_usd, unit="USD", source_ref=_first(bundle.source_refs)))
    if bundle.actor.label or bundle.actor.address or bundle.actor.cluster_id:
        points.extend([
            EventDataPoint("actor_profile", "actor_label", bundle.actor.label, source_ref=_first(bundle.actor.source_refs or bundle.source_refs), confidence=bundle.actor.confidence),
            EventDataPoint("actor_profile", "actor_address", bundle.actor.address, source_ref=_first(bundle.actor.source_refs or bundle.source_refs), confidence=bundle.actor.confidence),
            EventDataPoint("actor_profile", "actor_type", bundle.actor.actor_type, source_ref=_first(bundle.actor.source_refs or bundle.source_refs), confidence=bundle.actor.confidence),
        ])
        for i, behavior in enumerate(bundle.actor.prior_behavior[:8]):
            points.append(EventDataPoint("actor_profile", f"prior_behavior_{i+1}", behavior, source_ref=_first(bundle.actor.source_refs or bundle.source_refs), confidence=bundle.actor.confidence))
    for scenario in bundle.scenarios:
        points.append(EventDataPoint(
            "scenario",
            scenario.name,
            scenario.thesis,
            numeric_value=scenario.probability,
            unit="probability",
            source_ref=_first(scenario.source_refs or bundle.source_refs),
            confidence=scenario.probability,
            payload={
                "expected_outcome": scenario.expected_outcome,
                "trigger": scenario.trigger,
                "invalidator": scenario.invalidator,
            },
        ))
    for trigger in bundle.watch_triggers:
        points.append(EventDataPoint(
            "watch_trigger",
            trigger.kind,
            trigger.description,
            source_ref=_first(trigger.source_refs or bundle.source_refs),
            payload={"horizon": trigger.horizon, "direction": trigger.direction, "severity": trigger.severity},
        ))
    points.extend(bundle.liquidity_context)
    points.extend(bundle.derivatives_context)
    points.extend(bundle.historical_analogs)
    points.extend(bundle.raw_sources)
    return [p for p in points if p.label and p.value not in (None, "")]


def _collect_source_refs(bundle: MarketEventIntelligenceBundle) -> list[str]:
    refs: list[str] = []
    refs.extend(bundle.actor.source_refs)
    for point in _all_data_points(bundle):
        if point.source_ref:
            refs.append(point.source_ref)
    for scenario in bundle.scenarios:
        refs.extend(scenario.source_refs)
    for trigger in bundle.watch_triggers:
        refs.extend(trigger.source_refs)
    return sorted(set(refs))


def _actor_profile(raw: Any) -> ActorProfile:
    if not isinstance(raw, dict):
        return ActorProfile(label=_text(raw))
    return ActorProfile(
        label=_text(raw.get("label") or raw.get("name")),
        address=_text(raw.get("address") or raw.get("wallet")),
        cluster_id=_text(raw.get("cluster_id") or raw.get("cluster")),
        actor_type=_text(raw.get("actor_type") or raw.get("type")),
        confidence=_clamp01(raw.get("confidence"), 0.5),
        prior_behavior=_string_list(raw.get("prior_behavior") or raw.get("prior_behaviors")),
        source_refs=_string_list(raw.get("source_refs") or raw.get("evidence_refs")),
        payload=dict(raw.get("payload") or {}),
    )


def _data_points(raw: Any, category: str) -> list[EventDataPoint]:
    if raw is None:
        return []
    if isinstance(raw, dict):
        items = [{"label": k, "value": v} for k, v in raw.items()]
    elif isinstance(raw, list):
        items = raw
    else:
        items = [{"label": category, "value": raw}]
    out: list[EventDataPoint] = []
    for item in items:
        if isinstance(item, EventDataPoint):
            out.append(item)
            continue
        if not isinstance(item, dict):
            out.append(EventDataPoint(category=category, label=category, value=item))
            continue
        out.append(EventDataPoint(
            category=_text(item.get("category") or category),
            label=_text(item.get("label") or item.get("name") or item.get("metric")),
            value=item.get("value", item.get("text", "")),
            numeric_value=_as_float_or_none(item.get("numeric_value") or item.get("number")),
            unit=_text(item.get("unit")),
            source_ref=_text(item.get("source_ref") or item.get("source") or item.get("tool_call_log_id")),
            confidence=_clamp01(item.get("confidence"), 0.5),
            observed_at=_text(item.get("observed_at") or item.get("time")),
            payload=dict(item.get("payload") or {}),
            data_point_id=_text(item.get("data_point_id") or item.get("id")) or None,
        ))
    return [p for p in out if p.label]


def _scenarios(raw: Any) -> list[EventScenario]:
    if not isinstance(raw, list):
        return []
    out: list[EventScenario] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = _text(item.get("name") or item.get("label") or "scenario")
        thesis = _text(item.get("thesis") or item.get("description"))
        if not thesis:
            continue
        out.append(EventScenario(
            name=name,
            probability=_clamp01(item.get("probability"), 0.33),
            thesis=thesis,
            expected_outcome=_text(item.get("expected_outcome")),
            trigger=_text(item.get("trigger")),
            invalidator=_text(item.get("invalidator") or item.get("kill_signal")),
            source_refs=_string_list(item.get("source_refs") or item.get("evidence_refs")),
        ))
    return out


def _watch_triggers(raw: Any) -> list[WatchTrigger]:
    if not isinstance(raw, list):
        return []
    out: list[WatchTrigger] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        desc = _text(item.get("description") or item.get("trigger"))
        if not desc:
            continue
        out.append(WatchTrigger(
            kind=_text(item.get("kind") or item.get("trigger_kind") or "market_event"),
            description=desc,
            horizon=_text(item.get("horizon") or item.get("time_window")),
            direction=_text(item.get("direction") or item.get("confirm_direction")),
            severity=_text(item.get("severity") or "yellow"),
            source_refs=_string_list(item.get("source_refs") or item.get("evidence_refs")),
            status=_text(item.get("status") or "active"),
        ))
    return out


def _bundle_id(bundle: MarketEventIntelligenceBundle) -> str:
    raw = "|".join((
        bundle.cycle_id,
        bundle.entity,
        bundle.event_type,
        bundle.event_time,
        bundle.actor.label or bundle.actor.address or bundle.actor.cluster_id,
        str(bundle.amount or ""),
    ))
    return "mev_" + hashlib.sha256(raw.encode()).hexdigest()[:16]


def _data_point_id(bundle_id: str, point: EventDataPoint) -> str:
    raw = f"{bundle_id}|{point.category}|{point.label}|{_json_or_text(point.value)}|{point.source_ref}"
    return "medp_" + hashlib.sha256(raw.encode()).hexdigest()[:16]


def _event_horizon(bundle: MarketEventIntelligenceBundle) -> str:
    for trigger in bundle.watch_triggers:
        if trigger.horizon:
            return trigger.horizon
    return "intraday" if bundle.event_time else "1d"


def _default_kill_signal(bundle: MarketEventIntelligenceBundle) -> str:
    actor = bundle.actor.label or bundle.actor.address or "actor"
    return (
        f"{actor} does not transfer, sell, restake, bridge, or otherwise create "
        f"observable {bundle.entity} liquidity impact inside the watch window."
    )


def _format_amount(amount: Optional[float], unit: str) -> str:
    if amount is None:
        return unit
    if abs(amount) >= 1_000_000:
        text = f"{amount / 1_000_000:.2f}M"
    elif abs(amount) >= 1_000:
        text = f"{amount / 1_000:.1f}K"
    else:
        text = f"{amount:g}"
    return f"{text} {unit}".strip()


def _json_or_text(raw: Any) -> str:
    if isinstance(raw, (dict, list)):
        return json.dumps(raw, sort_keys=True)
    return _text(raw)


def _first(items: list[str]) -> str:
    return items[0] if items else ""


def _string_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(x).strip() for x in raw if str(x).strip()]


def _text(raw: Any) -> str:
    return str(raw or "").strip()


def _as_float_or_none(raw: Any) -> Optional[float]:
    try:
        if raw in (None, ""):
            return None
        return float(raw)
    except Exception:
        return None


def _clamp01(raw: Any, default: float) -> float:
    try:
        val = float(raw)
    except Exception:
        val = default
    return max(0.0, min(1.0, val))

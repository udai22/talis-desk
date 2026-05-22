"""Node intelligence snapshots.

Event intelligence answers: "what happened?"
Node intelligence answers: "what does our indexed node/Hydromancer view know
about the actors, flows, state, and tape around it?"
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from ..store import get_desk_store
from .store import InformationString


NODE_OBSERVATION_CATEGORIES = {
    "hydromancer_leaderboard",
    "wallet_quality",
    "wallet_trade",
    "wallet_order_quality",
    "wallet_state",
    "builder_flow",
    "onchain_event",
    "market_state",
    "node_reject_corpus",
    "raw_source",
}


@dataclass
class NodeObservation:
    category: str
    label: str
    value: Any = ""
    actor: str = ""
    numeric_value: Optional[float] = None
    unit: str = ""
    source_ref: str = ""
    source_family: str = ""
    confidence: float = 0.5
    observed_at: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    observation_id: Optional[str] = None


@dataclass
class NodeActor:
    wallet: str
    label: str = ""
    actor_type: str = ""
    quality_score: float = 0.5
    realized_pnl_usd: Optional[float] = None
    volume_usd: Optional[float] = None
    win_rate_pct: Optional[float] = None
    reject_rate_pct: Optional[float] = None
    source_refs: list[str] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class NodeIntelligenceQuality:
    score: float
    flags: list[str] = field(default_factory=list)
    n_observations: int = 0
    source_families: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        blockers = {
            "missing_entity",
            "missing_observations",
            "missing_source_refs",
            "missing_hydromancer",
            "missing_our_hl_node",
            "missing_actor_quality",
            "missing_flow_or_state",
            "missing_market_state",
            "unresolved_source_refs",
            "model_source_unresolved",
        }
        return self.score >= 0.72 and not blockers.intersection(self.flags)


@dataclass
class NodeToolProposal:
    tool_name: str
    purpose: str
    source_family: str
    trigger: str
    input_shape: dict[str, Any] = field(default_factory=dict)
    promotion_gate: dict[str, Any] = field(default_factory=dict)
    priority: str = "medium"


@dataclass
class NodeIntelligenceSnapshot:
    cycle_id: str
    entity: str
    chain: str = "hyperliquid"
    protocol: str = "hyperliquid"
    as_of: str = ""
    summary: str = ""
    edge_summary: str = ""
    observations: list[NodeObservation] = field(default_factory=list)
    actors: list[NodeActor] = field(default_factory=list)
    source_refs: list[str] = field(default_factory=list)
    source_families: list[str] = field(default_factory=list)
    coverage: dict[str, Any] = field(default_factory=dict)
    node_score: float = 0.0
    quality_flags: list[str] = field(default_factory=list)
    raw_payload: dict[str, Any] = field(default_factory=dict)
    snapshot_id: Optional[str] = None


def score_node_intelligence(snapshot: NodeIntelligenceSnapshot) -> NodeIntelligenceQuality:
    flags: list[str] = []
    if not snapshot.entity:
        flags.append("missing_entity")
    if not snapshot.observations:
        flags.append("missing_observations")
    if not snapshot.source_refs:
        flags.append("missing_source_refs")
    families = sorted(set(snapshot.source_families or [
        obs.source_family for obs in snapshot.observations if obs.source_family
    ]))
    categories = {obs.category for obs in snapshot.observations}
    if "hydromancer" not in families:
        flags.append("missing_hydromancer")
    if "our_hl_node" not in families:
        flags.append("missing_our_hl_node")
    if not ({"wallet_quality", "hydromancer_leaderboard"} & categories):
        flags.append("missing_actor_quality")
    if not ({"wallet_state", "builder_flow", "onchain_event"} & categories):
        flags.append("missing_flow_or_state")
    if "market_state" not in categories:
        flags.append("missing_market_state")

    n_obs = len(snapshot.observations)
    n_actor_obs = sum(1 for obs in snapshot.observations if obs.actor)
    score = 0.0
    score += 0.16 if snapshot.entity else 0.0
    score += min(0.18, 0.045 * len(snapshot.source_refs))
    score += min(0.18, 0.06 * len(families))
    score += 0.14 if "hydromancer" in families else 0.0
    score += 0.12 if "our_hl_node" in families else 0.0
    score += min(0.12, 0.04 * len(categories))
    score += min(0.12, 0.02 * n_actor_obs)
    score += min(0.08, 0.008 * n_obs)
    score += 0.05 if snapshot.summary and snapshot.edge_summary else 0.0
    return NodeIntelligenceQuality(
        score=round(min(1.0, score), 3),
        flags=sorted(set(flags + snapshot.quality_flags)),
        n_observations=n_obs,
        source_families=families,
    )


def propose_node_discovery_tools(snapshot: NodeIntelligenceSnapshot) -> list[NodeToolProposal]:
    """Turn coverage holes into candidate node tools for agents to build/grade."""
    flags = set(score_node_intelligence(snapshot).flags)
    proposals: list[NodeToolProposal] = []
    if "missing_our_hl_node" in flags:
        proposals.append(NodeToolProposal(
            tool_name="hl_node_reject_burst_reader",
            purpose="Read local HL-node order-status/reject deltas by wallet/coin to identify noisy or toxic flow before it becomes a headline.",
            source_family="our_hl_node",
            trigger="node_intelligence_missing_our_hl_node",
            input_shape={"coin": snapshot.entity, "lookback_minutes": 90, "wallets": ["0x..."]},
            promotion_gate={"min_reject_events": 5, "max_latency_s": 30, "requires_raw_offsets": True},
            priority="high",
        ))
    if "missing_hydromancer" in flags:
        proposals.append(NodeToolProposal(
            tool_name="hydromancer_actor_quality_bulk",
            purpose="Batch Hydromancer leaderboard/PnL/order quality for candidate wallets before spending deep-dive calls.",
            source_family="hydromancer",
            trigger="node_intelligence_missing_hydromancer",
            input_shape={"wallets": ["0x..."], "window_days": 30},
            promotion_gate={"n_wallets": 10, "has_pnl": True, "has_order_quality": True},
            priority="high",
        ))
    if "missing_flow_or_state" in flags:
        proposals.append(NodeToolProposal(
            tool_name="wallet_route_state_monitor",
            purpose="Track actor wallet state transitions from event time through CEX/bridge/restake/idle routes.",
            source_family="our_hl_node",
            trigger="event_has_amount_without_route_state",
            input_shape={"wallet": "0x...", "asset": snapshot.entity, "event_time": "ISO8601"},
            promotion_gate={"has_route_classification": True, "has_followup_trigger": True},
            priority="high",
        ))
    if "missing_market_state" in flags:
        proposals.append(NodeToolProposal(
            tool_name="node_market_absorption_window",
            purpose="Join order-book depth, funding/OI, fills, and liquidation context around a node event to classify absorption vs stress.",
            source_family="hydromancer",
            trigger="node_event_missing_market_state",
            input_shape={"coin": snapshot.entity, "event_time": "ISO8601", "window_minutes": 120},
            promotion_gate={"has_depth": True, "has_derivatives": True, "has_before_after": True},
            priority="medium",
        ))
    if any("mempool" in f for f in flags) or snapshot.chain.lower() in {"hyperevm", "ethereum", "evm"}:
        proposals.append(NodeToolProposal(
            tool_name="hyperevm_mempool_actor_watch",
            purpose="Watch pending contract/router/CEX interactions for known actors before they settle into events.",
            source_family="mempool",
            trigger="evm_chain_or_mempool_gap",
            input_shape={"addresses": ["0x..."], "contracts": ["0x..."], "asset": snapshot.entity},
            promotion_gate={"dedupe_by_tx_hash": True, "settlement_reconciliation": True},
            priority="medium",
        ))
    return proposals


def persist_node_intelligence(
    snapshot: NodeIntelligenceSnapshot,
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> str:
    db = conn or get_desk_store().conn
    now = datetime.now(timezone.utc).isoformat()
    snapshot.as_of = snapshot.as_of or now
    quality = score_node_intelligence(snapshot)
    snapshot.node_score = quality.score
    snapshot.quality_flags = sorted(set(snapshot.quality_flags + quality.flags))
    snapshot.source_families = quality.source_families
    snapshot_id = snapshot.snapshot_id or _snapshot_id(snapshot)
    snapshot.snapshot_id = snapshot_id
    db.execute(
        """
        INSERT OR REPLACE INTO node_intelligence_snapshots (
            id, cycle_id, entity, chain, protocol, as_of, summary,
            edge_summary, node_score, source_refs_json, source_families_json,
            coverage_json, actor_summaries_json, quality_flags, raw_payload_json,
            created_at, valid_from, transaction_from, transaction_to
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
        """,
        (
            snapshot_id,
            snapshot.cycle_id,
            snapshot.entity,
            snapshot.chain,
            snapshot.protocol,
            snapshot.as_of,
            snapshot.summary,
            snapshot.edge_summary,
            snapshot.node_score,
            json.dumps(snapshot.source_refs),
            json.dumps(snapshot.source_families),
            json.dumps(snapshot.coverage, sort_keys=True),
            json.dumps([_actor_payload(a) for a in snapshot.actors], sort_keys=True),
            json.dumps(snapshot.quality_flags),
            json.dumps(snapshot.raw_payload, sort_keys=True),
            now,
            now,
            now,
        ),
    )
    for obs in snapshot.observations:
        _persist_observation(db, snapshot_id, snapshot.cycle_id, obs, now)
    db.commit()
    return snapshot_id


def load_node_intelligence(
    *,
    cycle_id: str = "",
    entity: str = "",
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
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(int(limit))
    rows = db.execute(
        f"""
        SELECT * FROM node_intelligence_snapshots
        {where}
        ORDER BY node_score DESC, as_of DESC
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        for key in (
            "source_refs_json",
            "source_families_json",
            "coverage_json",
            "actor_summaries_json",
            "quality_flags",
            "raw_payload_json",
        ):
            default = {} if key in {"coverage_json", "raw_payload_json"} else []
            try:
                d[key] = json.loads(d.get(key) or json.dumps(default))
            except Exception:
                d[key] = default
        out.append(d)
    return out


def normalize_node_intelligence(raw: Any, *, cycle_id: str = "") -> Optional[NodeIntelligenceSnapshot]:
    if not isinstance(raw, dict):
        return None
    entity = _text(raw.get("entity") or raw.get("asset") or raw.get("coin"))
    if not entity:
        return None
    observations = [_observation_from_raw(x) for x in (raw.get("observations") or []) if isinstance(x, dict)]
    observations = [x for x in observations if x is not None]
    actors = [_actor_from_raw(x) for x in (raw.get("actors") or raw.get("actor_summaries") or []) if isinstance(x, dict)]
    actors = [x for x in actors if x is not None]
    source_refs = _string_list(raw.get("source_refs") or raw.get("evidence_refs"))
    source_families = _string_list(raw.get("source_families"))
    if not source_refs:
        source_refs = sorted(set(obs.source_ref for obs in observations if obs.source_ref))
    if not source_families:
        source_families = sorted(set(obs.source_family for obs in observations if obs.source_family))
    snapshot = NodeIntelligenceSnapshot(
        cycle_id=_text(raw.get("cycle_id") or cycle_id),
        entity=entity,
        chain=_text(raw.get("chain") or "hyperliquid"),
        protocol=_text(raw.get("protocol") or "hyperliquid"),
        as_of=_text(raw.get("as_of")),
        summary=_text(raw.get("summary")),
        edge_summary=_text(raw.get("edge_summary")),
        observations=observations,
        actors=actors,
        source_refs=source_refs,
        source_families=source_families,
        coverage=dict(raw.get("coverage") or {}),
        quality_flags=_string_list(raw.get("quality_flags")),
        raw_payload=dict(raw.get("raw_payload") or raw),
        snapshot_id=_text(raw.get("snapshot_id") or raw.get("id")) or None,
    )
    quality = score_node_intelligence(snapshot)
    snapshot.node_score = quality.score
    snapshot.quality_flags = sorted(set(snapshot.quality_flags + quality.flags))
    return snapshot


def node_intelligence_to_information_string(
    snapshot: NodeIntelligenceSnapshot,
) -> InformationString:
    quality = score_node_intelligence(snapshot)
    refs = sorted(set(([snapshot.snapshot_id] if snapshot.snapshot_id else []) + snapshot.source_refs))
    top_actor = snapshot.actors[0].wallet if snapshot.actors else ""
    thesis = snapshot.summary or (
        f"{snapshot.entity} node intelligence has {len(snapshot.observations)} observations "
        f"across {', '.join(quality.source_families) or 'node sources'}."
    )
    mechanism = snapshot.edge_summary or (
        "Hydromancer/node actor quality, wallet state, builder/on-chain flow, and market state together determine whether the tape is informed or noisy."
    )
    return InformationString(
        title=f"{snapshot.entity} node intelligence"[:140],
        thesis=thesis[:2000],
        mechanism=mechanism[:2000],
        expected_outcome=(
            "Route verifier budget toward actors/flows with real node evidence; ignore unsupported screenshots or generic whale chatter."
        ),
        time_horizon="intraday",
        time_scale="intraday",
        source_time_basis="ingestion_time",
        kill_signal=(
            "Hydromancer/node observations go stale, source health fails, or follow-up state contradicts the actor/flow read."
        ),
        extends_or_contradicts="new",
        would_change_decision=quality.passed,
        expires_at="next node refresh",
        crowdedness=0.35,
        conviction=max(0.0, min(1.0, snapshot.node_score or quality.score)),
        novelty_score=0.78,
        entities_chain=[x for x in [snapshot.entity, "hydromancer", "our_hl_node", top_actor] if x],
        depth_layers=[
            {"layer": 1, "claim": "Raw node/Hydromancer observations are preserved."},
            {"layer": 2, "claim": "Actor quality separates informed wallets from noisy flow."},
            {"layer": 3, "claim": "Wallet state and builder/on-chain flow identify impact path."},
            {"layer": 4, "claim": "Market state determines absorption vs reflexive stress."},
            {"layer": 5, "claim": "Source health and follow-up node changes are the kill signal."},
        ],
        evidence_refs=refs,
        temporal_confidence=max(0.0, min(1.0, quality.score)),
        quality_flags=["from_node_intelligence", *snapshot.quality_flags],
    )


def node_intelligence_from_tool_evidence(
    *,
    cycle_id: str,
    entity: str,
    horizon: str = "",
    lens: str = "",
    tool_evidence: list[dict[str, Any]],
) -> Optional[NodeIntelligenceSnapshot]:
    observations: list[NodeObservation] = []
    actors: dict[str, NodeActor] = {}
    source_refs: list[str] = []
    families: list[str] = []
    raw_by_ref: dict[str, Any] = {}
    for ev in tool_evidence or []:
        if ev.get("ok") is False:
            continue
        result = ev.get("result")
        if result is None:
            result = _maybe_json(ev.get("summary"))
        if not isinstance(result, dict):
            continue
        source_ref = _source_ref_for_evidence(ev)
        source_family = _source_family(ev, result)
        if source_ref:
            source_refs.append(source_ref)
            raw_by_ref[source_ref] = result
        if source_family:
            families.append(source_family)
        observations.extend(_observations_from_result(
            result,
            entity=entity,
            source_ref=source_ref,
            source_family=source_family,
            actors=actors,
        ))
    observations = _dedupe_observations(observations)
    if not observations:
        return None
    categories: dict[str, int] = {}
    for obs in observations:
        categories[obs.category] = categories.get(obs.category, 0) + 1
    source_families = sorted(set(
        families + [obs.source_family for obs in observations if obs.source_family]
    ))
    actor_list = sorted(
        actors.values(),
        key=lambda a: (
            a.quality_score,
            a.realized_pnl_usd or 0.0,
            a.volume_usd or 0.0,
        ),
        reverse=True,
    )[:20]
    summary = _summary(entity, observations, actor_list, source_families)
    edge_summary = _edge_summary(entity, observations, actor_list)
    snapshot = NodeIntelligenceSnapshot(
        cycle_id=cycle_id,
        entity=entity,
        as_of=datetime.now(timezone.utc).isoformat(),
        summary=summary,
        edge_summary=edge_summary,
        observations=observations,
        actors=actor_list,
        source_refs=sorted(set(source_refs)),
        source_families=source_families,
        coverage={
            "categories": categories,
            "source_families": source_families,
            "n_actors": len(actor_list),
            "horizon": horizon,
            "lens": lens,
        },
        raw_payload=raw_by_ref,
    )
    quality = score_node_intelligence(snapshot)
    snapshot.node_score = quality.score
    snapshot.quality_flags = quality.flags
    proposals = propose_node_discovery_tools(snapshot)
    if proposals:
        snapshot.coverage["tool_proposals"] = [_proposal_payload(p) for p in proposals]
    return snapshot


def _observations_from_result(
    result: dict[str, Any],
    *,
    entity: str,
    source_ref: str,
    source_family: str,
    actors: dict[str, NodeActor],
) -> list[NodeObservation]:
    out: list[NodeObservation] = []
    leaders = _list_value(result, "leaders")
    for row in leaders[:25]:
        wallet = _wallet(row)
        pnl = _as_float_or_none(row.get("realized_pnl_usd") or row.get("total_pnl_usd") or row.get("totalPnl"))
        vol = _as_float_or_none(row.get("volume_usd") or row.get("volumeTraded"))
        win = _as_float_or_none(row.get("win_rate_pct") or row.get("winRate"))
        rank = int(row.get("rank") or len(out) + 1)
        if wallet:
            _merge_actor(actors, wallet, source_ref, realized_pnl_usd=pnl, volume_usd=vol, win_rate_pct=win, quality_score=max(0.5, 1.0 - rank / 100.0), payload=row)
        out.append(NodeObservation(
            "hydromancer_leaderboard",
            f"leader_rank_{rank}",
            value=row,
            actor=wallet,
            numeric_value=pnl,
            unit="USD",
            source_ref=source_ref,
            source_family=source_family,
            confidence=0.85,
            payload=row,
        ))

    if result.get("wallet"):
        wallet = _text(result.get("wallet")).lower()
        pnl = _as_float_or_none(result.get("total_pnl_usd") or result.get("total_realized_pnl_usd"))
        vol = _as_float_or_none(result.get("volume_usd"))
        win = _as_float_or_none(result.get("win_rate_pct"))
        reject = _as_float_or_none(result.get("reject_rate_pct"))
        _merge_actor(actors, wallet, source_ref, realized_pnl_usd=pnl, volume_usd=vol, win_rate_pct=win, reject_rate_pct=reject, payload=result)
        out.append(NodeObservation(
            "wallet_quality" if pnl is not None or win is not None else "wallet_order_quality",
            "wallet_summary",
            value={k: result.get(k) for k in ("total_pnl_usd", "total_realized_pnl_usd", "win_rate_pct", "volume_usd", "reject_rate_pct", "n_orders", "n_rejected")},
            actor=wallet,
            numeric_value=pnl if pnl is not None else reject,
            unit="USD" if pnl is not None else "pct",
            source_ref=source_ref,
            source_family=source_family,
            confidence=0.78,
            payload=result,
        ))

    for trade in _list_value(result, "trades")[:50]:
        wallet = _text(result.get("wallet")).lower()
        coin = _text(trade.get("coin"))
        if entity and coin and coin.upper() != entity.upper():
            continue
        out.append(NodeObservation(
            "wallet_trade",
            f"{coin or entity}_completed_trade",
            value=trade,
            actor=wallet,
            numeric_value=_as_float_or_none(trade.get("net_pnl_usd")),
            unit="USD",
            source_ref=source_ref,
            source_family=source_family,
            confidence=0.72,
            observed_at=_text(trade.get("close_time") or trade.get("open_time")),
            payload=trade,
        ))

    for state in _list_value(result, "states")[:60]:
        wallet = _wallet(state)
        out.append(NodeObservation(
            "wallet_state",
            "clearinghouse_state",
            value=state,
            actor=wallet,
            numeric_value=_as_float_or_none(state.get("notional_position_usd") or state.get("account_value_usd")),
            unit="USD",
            source_ref=source_ref,
            source_family=source_family,
            confidence=0.78,
            payload=state,
        ))

    builder_keys = ("fills_sample", "top_users_by_volume")
    if any(result.get(k) for k in builder_keys) or "builder" in result:
        out.append(NodeObservation(
            "builder_flow",
            "builder_flow_summary",
            value={
                "builder": result.get("builder"),
                "total_volume_usd": result.get("total_volume_usd"),
                "total_builder_fee_usd": result.get("total_builder_fee_usd"),
                "unique_users": result.get("unique_users"),
                "top_coins": result.get("top_coins"),
                "top_users_by_volume": result.get("top_users_by_volume"),
            },
            numeric_value=_as_float_or_none(result.get("total_volume_usd")),
            unit="USD",
            source_ref=source_ref,
            source_family=source_family,
            confidence=0.75,
            payload=result,
        ))
        for user, vol in _pairs(result.get("top_users_by_volume"))[:10]:
            _merge_actor(actors, user.lower(), source_ref, volume_usd=_as_float_or_none(vol), payload={"builder_volume_usd": vol})

    for event in _list_value(result, "events")[:50]:
        text = _json_or_text(event).upper()
        if entity and entity.upper() not in text:
            continue
        out.append(NodeObservation(
            "onchain_event",
            _text(event.get("event_type") or event.get("kind") or "event"),
            value=event,
            actor=_wallet(event),
            numeric_value=_as_float_or_none(event.get("amount") or event.get("notional_usd")),
            unit=_text(event.get("amount_unit") or event.get("unit") or entity),
            source_ref=source_ref,
            source_family=source_family,
            confidence=0.70,
            observed_at=_text(event.get("event_time") or event.get("time") or event.get("ts")),
            payload=event,
        ))

    for point in _list_value(result, "points")[:80]:
        metric = _text(point.get("metric") or point.get("label") or point.get("name"))
        if not metric:
            continue
        out.append(NodeObservation(
            "market_state",
            metric,
            value=point.get("value", point),
            numeric_value=_as_float_or_none(point.get("value")),
            source_ref=source_ref,
            source_family=source_family,
            confidence=0.64,
            observed_at=_text(point.get("ts") or point.get("time") or point.get("observed_at")),
            payload=point,
        ))

    if source_family == "our_hl_node" and (
        result.get("status_counts") or result.get("top_reject_reasons")
    ):
        wallet = _text(result.get("wallet")).lower()
        out.append(NodeObservation(
            "node_reject_corpus",
            "reject_profile",
            value={
                "reject_rate_pct": result.get("reject_rate_pct"),
                "status_counts": result.get("status_counts"),
                "top_reject_reasons": result.get("top_reject_reasons"),
            },
            actor=wallet,
            numeric_value=_as_float_or_none(result.get("reject_rate_pct")),
            unit="pct",
            source_ref=source_ref,
            source_family="our_hl_node",
            confidence=0.78,
            payload=result,
        ))
    return out


def _persist_observation(db: sqlite3.Connection, snapshot_id: str, cycle_id: str, obs: NodeObservation, now: str) -> str:
    obs_id = obs.observation_id or _observation_id(snapshot_id, obs)
    obs.observation_id = obs_id
    category = obs.category if obs.category in NODE_OBSERVATION_CATEGORIES else "raw_source"
    db.execute(
        """
        INSERT OR REPLACE INTO node_intelligence_observations (
            id, snapshot_id, cycle_id, category, label, actor,
            value_text, numeric_value, unit, source_ref, source_family,
            confidence, observed_at, payload_json, created_at, valid_from,
            transaction_from, transaction_to
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
        """,
        (
            obs_id,
            snapshot_id,
            cycle_id,
            category,
            obs.label[:240],
            obs.actor,
            _json_or_text(obs.value)[:3000],
            obs.numeric_value,
            obs.unit,
            obs.source_ref,
            obs.source_family,
            _clamp01(obs.confidence, 0.5),
            obs.observed_at,
            json.dumps(obs.payload, sort_keys=True),
            now,
            now,
            now,
        ),
    )
    return obs_id


def _merge_actor(
    actors: dict[str, NodeActor],
    wallet: str,
    source_ref: str,
    *,
    realized_pnl_usd: Optional[float] = None,
    volume_usd: Optional[float] = None,
    win_rate_pct: Optional[float] = None,
    reject_rate_pct: Optional[float] = None,
    quality_score: float = 0.55,
    payload: Optional[dict[str, Any]] = None,
) -> None:
    wallet = _text(wallet).lower()
    if not wallet:
        return
    actor = actors.get(wallet) or NodeActor(wallet=wallet)
    actor.quality_score = max(actor.quality_score, _clamp01(quality_score, 0.55))
    actor.realized_pnl_usd = _prefer_abs(actor.realized_pnl_usd, realized_pnl_usd)
    actor.volume_usd = _prefer_abs(actor.volume_usd, volume_usd)
    actor.win_rate_pct = win_rate_pct if win_rate_pct is not None else actor.win_rate_pct
    actor.reject_rate_pct = reject_rate_pct if reject_rate_pct is not None else actor.reject_rate_pct
    if source_ref and source_ref not in actor.source_refs:
        actor.source_refs.append(source_ref)
    if payload:
        actor.payload.update(payload)
    actors[wallet] = actor


def _observation_from_raw(raw: dict[str, Any]) -> Optional[NodeObservation]:
    label = _text(raw.get("label") or raw.get("name") or raw.get("metric"))
    if not label:
        return None
    return NodeObservation(
        category=_text(raw.get("category") or "raw_source"),
        label=label,
        value=raw.get("value", raw.get("text", "")),
        actor=_text(raw.get("actor") or raw.get("wallet") or raw.get("address")).lower(),
        numeric_value=_as_float_or_none(raw.get("numeric_value") or raw.get("number")),
        unit=_text(raw.get("unit")),
        source_ref=_text(raw.get("source_ref") or raw.get("source") or raw.get("tool_call_log_id")),
        source_family=_text(raw.get("source_family")),
        confidence=_clamp01(raw.get("confidence"), 0.5),
        observed_at=_text(raw.get("observed_at") or raw.get("time") or raw.get("ts")),
        payload=dict(raw.get("payload") or {}),
        observation_id=_text(raw.get("observation_id") or raw.get("id")) or None,
    )


def _actor_from_raw(raw: dict[str, Any]) -> Optional[NodeActor]:
    wallet = _text(raw.get("wallet") or raw.get("address") or raw.get("actor")).lower()
    if not wallet:
        return None
    return NodeActor(
        wallet=wallet,
        label=_text(raw.get("label")),
        actor_type=_text(raw.get("actor_type") or raw.get("type")),
        quality_score=_clamp01(raw.get("quality_score"), 0.5),
        realized_pnl_usd=_as_float_or_none(raw.get("realized_pnl_usd")),
        volume_usd=_as_float_or_none(raw.get("volume_usd")),
        win_rate_pct=_as_float_or_none(raw.get("win_rate_pct")),
        reject_rate_pct=_as_float_or_none(raw.get("reject_rate_pct")),
        source_refs=_string_list(raw.get("source_refs") or raw.get("evidence_refs")),
        payload=dict(raw.get("payload") or {}),
    )


def _summary(entity: str, observations: list[NodeObservation], actors: list[NodeActor], families: list[str]) -> str:
    cats: dict[str, int] = {}
    for obs in observations:
        cats[obs.category] = cats.get(obs.category, 0) + 1
    top_actor = actors[0].wallet[:10] + "..." if actors else "no dominant actor"
    return (
        f"{entity} node map has {len(observations)} observations across "
        f"{', '.join(families) or 'unknown sources'}; top actor signal is {top_actor}. "
        f"Coverage: {', '.join(f'{k}={v}' for k, v in sorted(cats.items()))}."
    )


def _edge_summary(entity: str, observations: list[NodeObservation], actors: list[NodeActor]) -> str:
    best = actors[0] if actors else None
    if best:
        pnl = f"${best.realized_pnl_usd:,.0f}" if best.realized_pnl_usd is not None else "unknown PnL"
        return (
            f"{entity} should be interpreted through actor quality first: "
            f"{best.wallet[:10]}... has {pnl}, {best.win_rate_pct or 0:.1f}% win rate, "
            f"and {best.reject_rate_pct if best.reject_rate_pct is not None else 'unknown'}% reject rate."
        )
    return (
        f"{entity} node edge depends on whether Hydromancer/wallet state and our node reject/on-chain tape agree."
    )


def _snapshot_id(snapshot: NodeIntelligenceSnapshot) -> str:
    raw = "|".join((
        snapshot.cycle_id,
        snapshot.entity,
        snapshot.as_of,
        ",".join(sorted(snapshot.source_refs)),
        str(len(snapshot.observations)),
    ))
    return "nint_" + hashlib.sha256(raw.encode()).hexdigest()[:16]


def _observation_id(snapshot_id: str, obs: NodeObservation) -> str:
    raw = f"{snapshot_id}|{obs.category}|{obs.label}|{obs.actor}|{_json_or_text(obs.value)}|{obs.source_ref}"
    return "nobs_" + hashlib.sha256(raw.encode()).hexdigest()[:16]


def _actor_payload(actor: NodeActor) -> dict[str, Any]:
    return {
        "wallet": actor.wallet,
        "label": actor.label,
        "actor_type": actor.actor_type,
        "quality_score": actor.quality_score,
        "realized_pnl_usd": actor.realized_pnl_usd,
        "volume_usd": actor.volume_usd,
        "win_rate_pct": actor.win_rate_pct,
        "reject_rate_pct": actor.reject_rate_pct,
        "source_refs": actor.source_refs,
        "payload": actor.payload,
    }


def _proposal_payload(proposal: NodeToolProposal) -> dict[str, Any]:
    return {
        "tool_name": proposal.tool_name,
        "purpose": proposal.purpose,
        "source_family": proposal.source_family,
        "trigger": proposal.trigger,
        "input_shape": proposal.input_shape,
        "promotion_gate": proposal.promotion_gate,
        "priority": proposal.priority,
    }


def _source_ref_for_evidence(ev: dict[str, Any]) -> str:
    return _text(ev.get("tool_call_log_id") or ev.get("uri") or ev.get("source") or ev.get("id"))


def _source_family(ev: dict[str, Any], result: dict[str, Any]) -> str:
    text = " ".join([
        str(ev.get("uri") or ""),
        str(ev.get("args") or ""),
        str(result.get("action") or ""),
        str(result.get("source") or ""),
    ]).lower()
    if any(tok in text for tok in ("farm_grok_x_alpha", "grok", "x_search", "xai", "twitter", "x.com")):
        return "grok_x_alpha"
    if "hydromancer" in text or any(k in result for k in ("leaders", "builder", "fills_sample")):
        return "hydromancer"
    if (
        "hl_reject_corpus" in text
        or "our_hl_node" in text
        or "tokyo node" in text
        or "node_reject" in text
        or ("node" in text and "reject" in text)
    ):
        return "our_hl_node"
    if "query_events_recent" in text or "whale" in text or "onchain" in text:
        return "event_store"
    if "query_timeseries" in text:
        return "timeseries_store"
    return "unknown"


def _list_value(raw: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = raw.get(key)
    if isinstance(value, list):
        return [x for x in value if isinstance(x, dict)]
    return []


def _pairs(raw: Any) -> list[tuple[str, Any]]:
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            out.append((str(item[0]), item[1]))
        elif isinstance(item, dict):
            user = item.get("user") or item.get("wallet") or item.get("address")
            val = item.get("volume_usd") or item.get("value") or item.get("notional")
            if user:
                out.append((str(user), val))
    return out


def _wallet(raw: dict[str, Any]) -> str:
    return _text(raw.get("wallet") or raw.get("user") or raw.get("address") or raw.get("from_address")).lower()


def _dedupe_observations(observations: list[NodeObservation]) -> list[NodeObservation]:
    out: list[NodeObservation] = []
    seen: set[tuple[str, str, str, str]] = set()
    for obs in observations:
        key = (obs.category, obs.label, obs.actor, _json_or_text(obs.value))
        if key in seen:
            continue
        seen.add(key)
        out.append(obs)
    return out


def _prefer_abs(old: Optional[float], new: Optional[float]) -> Optional[float]:
    if new is None:
        return old
    if old is None:
        return new
    return new if abs(new) > abs(old) else old


def _json_or_text(raw: Any) -> str:
    if isinstance(raw, (dict, list)):
        return json.dumps(raw, sort_keys=True)
    return _text(raw)


def _maybe_json(raw: Any) -> Any:
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return None


def _text(raw: Any) -> str:
    return str(raw or "").strip()


def _string_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(x).strip() for x in raw if str(x).strip()]


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

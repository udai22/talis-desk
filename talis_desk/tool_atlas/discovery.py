"""Analysis-wide tool discovery proposals.

Any analysis artifact can say: "I could not answer this because a tool is
missing or weak." Those proposals are persisted, iterated, and graded before a
tool is admitted into the live atlas.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from ..store import get_desk_store


@dataclass
class AnalysisToolProposal:
    cycle_id: str
    artifact_kind: str
    artifact_id: str
    entity: str = ""
    horizon: str = ""
    lens: str = ""
    proposal_kind: str = "new_tool"
    tool_name: str = ""
    purpose: str = ""
    source_family: str = ""
    trigger: str = ""
    input_shape: dict[str, Any] = field(default_factory=dict)
    promotion_gate: dict[str, Any] = field(default_factory=dict)
    eval_plan: dict[str, Any] = field(default_factory=dict)
    priority: str = "medium"
    status: str = "proposed"
    parent_proposal_id: str = ""
    iteration: int = 0
    created_by: str = "analysis_tool_discovery"
    quality_flags: list[str] = field(default_factory=list)
    proposal_id: Optional[str] = None


@dataclass(frozen=True)
class AnalysisToolProposalQuality:
    score: float
    flags: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        blockers = {
            "missing_purpose",
            "missing_eval_plan",
            "missing_promotion_gate",
            "missing_expected_edge",
            "low_expected_info_value",
            "would_not_change_decision",
        }
        return self.score >= 0.70 and not any(flag in blockers for flag in self.flags)


def evaluate_analysis_tool_proposal(proposal: AnalysisToolProposal | dict[str, Any]) -> AnalysisToolProposalQuality:
    """Score whether a proposed analysis tool is worth engineering attention.

    This is deliberately deterministic: tool creation can be suggested by any
    agent, but promotion pressure should come from clear source need, expected
    map edge, expected information value, and an eval plan.
    """
    d = proposal if isinstance(proposal, dict) else proposal.__dict__
    flags: list[str] = []
    purpose = str(d.get("purpose") or "").strip()
    source_family = str(d.get("source_family") or "").strip()
    trigger = str(d.get("trigger") or "").strip()
    proposal_kind = str(d.get("proposal_kind") or "").strip()
    priority = str(d.get("priority") or "medium").lower()
    input_shape = _as_dict(d.get("input_shape") or d.get("input_shape_json"))
    promotion_gate = _as_dict(d.get("promotion_gate") or d.get("promotion_gate_json"))
    eval_plan = _as_dict(d.get("eval_plan") or d.get("eval_plan_json"))
    expected_edge = str(promotion_gate.get("expected_edge") or d.get("expected_edge") or "").strip()
    expected_info_value = _bounded_float(
        promotion_gate.get("expected_info_value", d.get("expected_info_value")),
        default=None,
    )
    would_change_decision = _optional_bool(
        promotion_gate.get("would_change_decision", d.get("would_change_decision"))
    )

    if not purpose:
        flags.append("missing_purpose")
    if not source_family:
        flags.append("missing_source_family")
    if not trigger:
        flags.append("missing_trigger")
    if not input_shape:
        flags.append("missing_input_shape")
    if not promotion_gate:
        flags.append("missing_promotion_gate")
    if not eval_plan:
        flags.append("missing_eval_plan")
    if proposal_kind == "next_tool_call" or trigger == "scout_tool_request":
        if not expected_edge:
            flags.append("missing_expected_edge")
        if expected_info_value is None:
            flags.append("missing_expected_info_value")
        elif expected_info_value < 0.35:
            flags.append("low_expected_info_value")
        if would_change_decision is False:
            flags.append("would_not_change_decision")
    if priority == "low":
        flags.append("low_priority")

    score = 0.0
    score += 0.14 if purpose else 0.0
    score += 0.10 if source_family else 0.0
    score += 0.08 if trigger else 0.0
    score += 0.12 if input_shape else 0.0
    score += 0.16 if promotion_gate else 0.0
    score += 0.16 if eval_plan else 0.0
    score += 0.10 if expected_edge else 0.0
    score += 0.10 if expected_info_value is not None else 0.0
    score += 0.08 if (expected_info_value is not None and expected_info_value >= 0.55) else 0.0
    score += 0.06 if would_change_decision is True else 0.0
    if "low_expected_info_value" in flags or "would_not_change_decision" in flags:
        score -= 0.18
    if priority == "low":
        score -= 0.10
    return AnalysisToolProposalQuality(
        score=round(max(0.0, min(1.0, score)), 3),
        flags=sorted(set(flags)),
    )


def propose_tools_from_quality_flags(
    *,
    cycle_id: str,
    artifact_kind: str,
    artifact_id: str,
    entity: str = "",
    horizon: str = "",
    lens: str = "",
    quality_flags: list[str],
) -> list[AnalysisToolProposal]:
    flags = {str(flag) for flag in quality_flags if str(flag).strip()}
    proposals: list[AnalysisToolProposal] = []
    if any("missing_liquidity_context" in f for f in flags):
        proposals.append(_proposal(
            cycle_id, artifact_kind, artifact_id, entity, horizon, lens,
            tool_name="liquidity_absorption_context",
            purpose="Join event amount with order-book depth, spot/perp volume, float/supply, and before/after absorption.",
            source_family="market_microstructure",
            trigger="missing_liquidity_context",
            input_shape={"asset": entity, "event_time": "ISO8601", "window_minutes": 120},
            promotion_gate={"has_depth": True, "has_volume": True, "has_before_after": True},
            eval_plan={"fixture_types": ["unlock", "unstake", "large_transfer"], "min_pass_rate": 0.8},
            priority="high",
        ))
    if any("missing_derivatives_context" in f or "missing_market_state" in f for f in flags):
        proposals.append(_proposal(
            cycle_id, artifact_kind, artifact_id, entity, horizon, lens,
            tool_name="derivatives_positioning_context",
            purpose="Attach funding, OI, liquidations, basis, and crowding state around a catalyst or flow event.",
            source_family="derivatives",
            trigger="missing_derivatives_or_market_state",
            input_shape={"asset": entity, "lookback_hours": 168},
            promotion_gate={"has_funding": True, "has_oi": True, "has_liquidations_or_basis": True},
            eval_plan={"compare_against": ["coinalyze", "hydromancer", "hl_node"], "max_staleness_s": 900},
            priority="high",
        ))
    if any("missing_historical_analogs" in f for f in flags):
        proposals.append(_proposal(
            cycle_id, artifact_kind, artifact_id, entity, horizon, lens,
            tool_name="historical_event_analog_miner",
            purpose="Find prior same-actor/same-event/same-market-state outcomes and summarize forward returns plus route behavior.",
            source_family="historical_store",
            trigger="missing_historical_analogs",
            input_shape={"asset": entity, "event_type": "string", "actor": "0x..."},
            promotion_gate={"n_analogs": 3, "has_forward_returns": True, "has_route_labels": True},
            eval_plan={"golden_fixtures": 10, "min_precision": 0.75},
        ))
    if any("missing_source_refs" in f or "unresolved_evidence_refs" in f for f in flags):
        proposals.append(_proposal(
            cycle_id, artifact_kind, artifact_id, entity, horizon, lens,
            tool_name="evidence_ref_resolver",
            purpose="Resolve source refs to tool_call_log row, endpoint, raw key path, source timestamp, and cached raw snippet.",
            source_family="provenance",
            trigger="missing_or_unresolved_source_refs",
            input_shape={"source_refs": ["tc_..."]},
            promotion_gate={"resolves_tool_call_log": True, "has_raw_key_path": True},
            eval_plan={"min_resolution_rate": 0.95},
            priority="high",
        ))
    if any("missing_hydromancer" in f for f in flags):
        proposals.append(_proposal(
            cycle_id, artifact_kind, artifact_id, entity, horizon, lens,
            tool_name="hydromancer_actor_quality_bulk",
            purpose="Batch Hydromancer PnL/order/leaderboard quality for candidate actors before deep-dive spend.",
            source_family="hydromancer",
            trigger="missing_hydromancer",
            input_shape={"wallets": ["0x..."], "window_days": 30},
            promotion_gate={"has_pnl": True, "has_order_quality": True, "uses_batching": True},
            eval_plan={"max_weight": 100, "min_wallets": 10},
            priority="high",
        ))
    if any("missing_our_hl_node" in f for f in flags):
        proposals.append(_proposal(
            cycle_id, artifact_kind, artifact_id, entity, horizon, lens,
            tool_name="hl_node_stream_reader",
            purpose="Read our HL-node order/fill/reject/state deltas with raw offsets so source truth is independent of third-party aggregators.",
            source_family="our_hl_node",
            trigger="missing_our_hl_node",
            input_shape={"asset": entity, "wallets": ["0x..."], "lookback_minutes": 90},
            promotion_gate={"has_raw_offsets": True, "max_latency_s": 30, "dedupes_events": True},
            eval_plan={"fixtures": ["reject_burst", "fill_sweep", "wallet_route"], "min_pass_rate": 0.85},
            priority="high",
        ))
    if any("missing_mempool" in f or "mempool_gap" in f for f in flags):
        proposals.append(_proposal(
            cycle_id, artifact_kind, artifact_id, entity, horizon, lens,
            tool_name="hyperevm_mempool_actor_watch",
            purpose="Watch pending HypereVM/router/CEX interactions for known actors, then reconcile pending txs to settled events.",
            source_family="mempool",
            trigger="missing_mempool_coverage",
            input_shape={"addresses": ["0x..."], "contracts": ["0x..."], "asset": entity},
            promotion_gate={"dedupe_by_tx_hash": True, "settlement_reconciliation": True},
            eval_plan={"fixtures": ["pending_to_settled_transfer"], "min_pass_rate": 0.85},
            priority="high",
        ))
    return _dedupe(proposals)


def iterate_tool_proposal(
    parent: AnalysisToolProposal,
    *,
    critique_flags: list[str],
    improvement_note: str,
    eval_plan_delta: Optional[dict[str, Any]] = None,
    promotion_gate_delta: Optional[dict[str, Any]] = None,
) -> AnalysisToolProposal:
    eval_plan = dict(parent.eval_plan)
    eval_plan.update(eval_plan_delta or {})
    gate = dict(parent.promotion_gate)
    gate.update(promotion_gate_delta or {})
    return AnalysisToolProposal(
        cycle_id=parent.cycle_id,
        artifact_kind=parent.artifact_kind,
        artifact_id=parent.artifact_id,
        entity=parent.entity,
        horizon=parent.horizon,
        lens=parent.lens,
        proposal_kind="iterate_tool",
        tool_name=parent.tool_name,
        purpose=f"{parent.purpose} Improvement: {improvement_note}",
        source_family=parent.source_family,
        trigger=parent.trigger,
        input_shape=dict(parent.input_shape),
        promotion_gate=gate,
        eval_plan=eval_plan,
        priority=parent.priority,
        status="proposed",
        parent_proposal_id=parent.proposal_id or _proposal_id(parent),
        iteration=int(parent.iteration) + 1,
        created_by=parent.created_by,
        quality_flags=sorted(set([*parent.quality_flags, *critique_flags])),
    )


def persist_analysis_tool_proposals(
    proposals: list[AnalysisToolProposal],
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> list[str]:
    db = conn or get_desk_store().conn
    now = datetime.now(timezone.utc).isoformat()
    ids: list[str] = []
    for proposal in proposals:
        quality = evaluate_analysis_tool_proposal(proposal)
        proposal.quality_flags = sorted(set([
            *proposal.quality_flags,
            f"tool_proposal_quality:{quality.score:.2f}",
            *[f"tool_proposal_{flag}" for flag in quality.flags[:6]],
        ]))
        pid = proposal.proposal_id or _proposal_id(proposal)
        proposal.proposal_id = pid
        db.execute(
            """
            INSERT OR REPLACE INTO analysis_tool_proposals (
                id, cycle_id, artifact_kind, artifact_id, entity, horizon,
                lens, proposal_kind, tool_name, purpose, source_family,
                trigger, input_shape_json, promotion_gate_json, eval_plan_json,
                priority, status, parent_proposal_id, iteration, created_by,
                quality_flags, created_at, valid_from, transaction_from,
                transaction_to
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                pid,
                proposal.cycle_id,
                proposal.artifact_kind,
                proposal.artifact_id,
                proposal.entity,
                proposal.horizon,
                proposal.lens,
                proposal.proposal_kind,
                proposal.tool_name,
                proposal.purpose,
                proposal.source_family,
                proposal.trigger,
                json.dumps(proposal.input_shape, sort_keys=True),
                json.dumps(proposal.promotion_gate, sort_keys=True),
                json.dumps(proposal.eval_plan, sort_keys=True),
                proposal.priority,
                proposal.status,
                proposal.parent_proposal_id,
                int(proposal.iteration),
                proposal.created_by,
                json.dumps(proposal.quality_flags),
                now,
                now,
                now,
            ),
        )
        ids.append(pid)
    db.commit()
    return ids


def load_analysis_tool_proposals(
    *,
    cycle_id: str = "",
    status: str = "",
    limit: int = 100,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict[str, Any]]:
    db = conn or get_desk_store().conn
    clauses: list[str] = []
    params: list[Any] = []
    if cycle_id:
        clauses.append("cycle_id = ?")
        params.append(cycle_id)
    if status:
        clauses.append("status = ?")
        params.append(status)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(int(limit))
    rows = db.execute(
        f"""
        SELECT * FROM analysis_tool_proposals
        {where}
        ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
                 iteration DESC, created_at DESC
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        d = dict(row)
        for key, default in (
            ("input_shape_json", {}),
            ("promotion_gate_json", {}),
            ("eval_plan_json", {}),
            ("quality_flags", []),
        ):
            try:
                d[key] = json.loads(d.get(key) or json.dumps(default))
            except Exception:
                d[key] = default
        out.append(d)
    return out


def _proposal(
    cycle_id: str,
    artifact_kind: str,
    artifact_id: str,
    entity: str,
    horizon: str,
    lens: str,
    **kwargs: Any,
) -> AnalysisToolProposal:
    return AnalysisToolProposal(
        cycle_id=cycle_id,
        artifact_kind=artifact_kind,
        artifact_id=artifact_id,
        entity=entity,
        horizon=horizon,
        lens=lens,
        **kwargs,
    )


def _dedupe(proposals: list[AnalysisToolProposal]) -> list[AnalysisToolProposal]:
    out: list[AnalysisToolProposal] = []
    seen: set[tuple[str, str, str]] = set()
    for proposal in proposals:
        key = (proposal.tool_name, proposal.trigger, proposal.artifact_id)
        if key in seen:
            continue
        seen.add(key)
        out.append(proposal)
    return out


def _proposal_id(proposal: AnalysisToolProposal) -> str:
    raw = "|".join((
        proposal.cycle_id,
        proposal.artifact_kind,
        proposal.artifact_id,
        proposal.tool_name,
        proposal.trigger,
        str(proposal.iteration),
        proposal.parent_proposal_id,
    ))
    return "atp_" + hashlib.sha256(raw.encode()).hexdigest()[:16]


def _as_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _bounded_float(raw: Any, *, default: float | None) -> float | None:
    if raw in (None, ""):
        return default
    try:
        value = float(raw)
    except Exception:
        return default
    return max(0.0, min(1.0, value))


def _optional_bool(raw: Any) -> bool | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in {"true", "1", "yes", "y"}:
        return True
    if text in {"false", "0", "no", "n"}:
        return False
    return None


__all__ = [
    "AnalysisToolProposal",
    "AnalysisToolProposalQuality",
    "evaluate_analysis_tool_proposal",
    "iterate_tool_proposal",
    "load_analysis_tool_proposals",
    "persist_analysis_tool_proposals",
    "propose_tools_from_quality_flags",
]

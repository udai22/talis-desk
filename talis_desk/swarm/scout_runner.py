"""Tier 1 — DeepSeek Flash scout swarm.

Runs `n` scouts in parallel via `asyncio.gather(...)` with a semaphore
concurrency cap (default 50 — avoids provider rate limits). Each scout:
  - Receives one `SeedCell` (entity x horizon x lens x bias)
  - Makes a single Flash-tier LLM call (~$0.0002 each)
  - Writes one hypothesis row to `hypotheses`
  - Posts one message to `bb_topic:scout_output:<lens>:<entity>`

Total cost target for 1000 scouts: <$0.25
Total wall time target: <5 min.

NO STUBS. If the Flash provider chain is exhausted, the scout returns a
ScoutOutput with `error` set and `quality_flag=['scout_provider_unavailable']`.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from ..agents_native import post_message
from ..coordination import (
    append_blackboard_event,
    attribute_failure,
    claim_task,
    complete_task,
    fail_task,
    start_task,
)
from .._tic_config import ensure_tic_on_path as _ensure_tic_on_path
from ..agent_harness import (
    HarnessPolicy,
    classify_tool_error as _harness_classify_tool_error,
    compact_tool_result as _harness_compact_tool_result,
    dispatch_harness_tool,
    filter_fulfilled_tool_requests as _harness_filter_fulfilled_tool_requests,
    normalize_tool_requests as _harness_normalize_tool_requests,
    short_text as _harness_short_text,
    summarize_tool_result as _harness_summarize_tool_result,
)
from ..information_map import (
    EventDataPoint,
    InformationString,
    MarketEventIntelligenceBundle,
    NodeIntelligenceSnapshot,
    apply_adversarial_review,
    choose_market_evolve_prompt_variant,
    data_substrate_to_information_string,
    event_intelligence_from_tool_evidence,
    event_intelligence_to_information_string,
    node_intelligence_from_tool_evidence,
    node_intelligence_to_information_string,
    normalize_information_string,
    normalize_event_intelligence,
    normalize_node_intelligence,
    persist_event_intelligence,
    persist_information_strings,
    persist_node_intelligence,
    score_node_intelligence,
    select_information_context,
    review_information_string,
)
from ..information_map.deep_scout_prompt import (
    build_deep_scout_system_prompt,
    score_deep_scout_output,
)
from ..store import get_desk_store
from ..tool_atlas.discovery import (
    AnalysisToolProposal,
    normalize_analysis_tool_proposal_contract,
    persist_analysis_tool_proposals,
    propose_tools_from_quality_flags,
)
from .seed_generator import LENS_TOOL_TERMS, SeedCell, record_coverage


logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------

# Provider fallback chain — Flash first (cheap), then increasingly capable
# (still cheap) tiers. Same pattern as runner._chat_sync.
DEFAULT_SCOUT_MODEL = "deepseek:v4-flash"
DEFAULT_SCOUT_FALLBACK = "anthropic:claude-haiku-4-5"

# Per-scout token budget. DeepSeek v4-flash is a reasoning model that
# burns 500-1000 tokens on internal reasoning before producing visible
# content; sub-1000 budgets return empty content. We give it room.
SCOUT_MAX_TOKENS = 8000

# Default concurrency cap.
DEFAULT_CONCURRENCY = 50

# Default per-cycle scout cost cap (kill switch).
DEFAULT_COST_CAP_USD = 1.0

# Even when the whole atlas is available, a Tier-1 scout should not spray calls.
# The prompt/evidence loop scales because each cell gets a tiny, relevant slice.
SCOUT_EVIDENCE_HARD_CAP = 8

# A scout is allowed a tiny model-mediated tool loop, not an open-ended agent
# session. This gives it Claude-Code-like observe/request/revise behavior while
# preserving replayability and cost discipline at 1,000+ scout scale.
SCOUT_TOOL_ITERATION_HARD_CAP = 2
SCOUT_TOOL_RETRY_MAX = 1


# ----------------------------------------------------------------------
# Output dataclass
# ----------------------------------------------------------------------

@dataclass
class ScoutOutput:
    seed_id: str
    scout_id: str
    cycle_id: str
    entity: str
    lens: str
    horizon: str
    bias_mode: str
    hypothesis_text: str
    confidence: float
    rationale_brief: str
    suggested_tools: list[str]
    tool_requests: list[dict[str, Any]] = field(default_factory=list)
    information_strings: list[InformationString] = field(default_factory=list)
    information_string_ids: list[str] = field(default_factory=list)
    event_intelligence_ids: list[str] = field(default_factory=list)
    node_intelligence_ids: list[str] = field(default_factory=list)
    tool_proposal_ids: list[str] = field(default_factory=list)
    tool_evidence: list[dict[str, Any]] = field(default_factory=list)
    tool_iteration_count: int = 0
    calendar_trigger: Optional[dict[str, Any]] = None
    calendar_severity: Optional[str] = None
    task_id: Optional[str] = None
    hypothesis_id: Optional[str] = None
    model_used: str = ""
    provider: str = ""
    prompt_variant: str = ""
    cost_usd: float = 0.0
    elapsed_s: float = 0.0
    quality_flags: list[str] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class RouteContractAlignment:
    score: float
    flags: list[str] = field(default_factory=list)
    addressed_edges: list[str] = field(default_factory=list)
    missed_edges: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.score >= 0.60 and not any(
            flag in self.flags
            for flag in (
                "route_contract_missing_output",
                "route_contract_no_edge_moved",
                "route_contract_success_gate_missing",
            )
        )


# ----------------------------------------------------------------------
# Prompt construction
# ----------------------------------------------------------------------

def _prompt_variant_for_seed(seed: SeedCell) -> str:
    """Route prompt architecture by receptive-field type.

    The prompt lab promotes a small policy map rather than one global prompt:
    temporal contexts need event-time discipline, seam lenses need network
    connection finding, frontier cells need adversarial alpha pressure, and
    ordinary cells need local causal depth.

    TALIS_SCOUT_PROMPT_VARIANT can force one variant during experiments.
    TALIS_SCOUT_PROMPT_POLICY can point at a prompt-tournament promotion JSON.
    """
    override = os.environ.get("TALIS_SCOUT_PROMPT_VARIANT", "").strip()
    if override:
        return override
    payload_variant = str((seed.payload or {}).get("prompt_variant") or "").strip()
    if payload_variant:
        return payload_variant
    try:
        return choose_market_evolve_prompt_variant(seed)
    except Exception:
        pass
    policy = _prompt_policy()
    lens = (seed.lens or "").lower()
    theme = (seed.theme or "").lower()
    horizon = (seed.horizon or "").lower()
    bias = (seed.bias_mode or "").lower()

    if horizon in {"tick", "second", "minute", "hour", "intraday"}:
        return policy["temporal_context"]
    if lens in {"filing", "headline", "headlines", "catalyst", "material_info"}:
        return policy["primary_source"]
    if lens in {"rotation", "on_chain", "money_velocity", "smart_money", "macro", "factor"}:
        return policy["seam_lenses"]
    if any(tok in theme for tok in ("flow", "rotation", "liquidity", "velocity", "vehicle")):
        return policy["seam_lenses"]
    if bias in {"frontier", "contrarian"}:
        return policy["fast_alpha"]
    if lens in {"catalyst", "filing", "microstructure", "options_flow", "vol_surface", "anomaly"}:
        return policy["default"]
    return policy["default"]


def _prompt_policy() -> dict[str, str]:
    default = {
        "default": "depth_ladder_v1",
        "seam_lenses": "mycelial_network_v1",
        "fast_alpha": "adversarial_alpha_v1",
        "temporal_context": "temporal_pyramid_v1",
        "primary_source": "depth_ladder_v1",
    }
    path = os.environ.get("TALIS_SCOUT_PROMPT_POLICY", "").strip()
    if not path:
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        policy = raw.get("recommended_default_policy") or raw
        if isinstance(policy, dict):
            out = dict(default)
            for key in default:
                value = str(policy.get(key) or "").strip()
                if value:
                    out[key] = value
            return out
    except Exception as e:
        logger.info("prompt policy load skipped: %s", e)
    return default


def _apply_prompt_contract_pressure(system_prompt: str, seed: SeedCell) -> str:
    payload = seed.payload or {}
    pressure = str(payload.get("prompt_contract_pressure") or "normal").lower()
    min_strings = _int_payload(payload.get("prompt_min_information_strings"), default=1, lo=0, hi=3)
    require_mechanism = bool(payload.get("prompt_require_mechanism", True))
    require_kill_signal = bool(payload.get("prompt_require_kill_signal", True))
    require_evidence_refs = bool(payload.get("prompt_require_evidence_refs", True))
    if pressure not in {"raise", "strict", "high"} and min_strings <= 1:
        return system_prompt
    requirements = [
        "\n\n<market_evolve_prompt_contract>",
        f"contract_pressure: {pressure}",
        f"minimum_information_strings: {min_strings}",
        "This is an evolved policy requirement, not commentary. Before returning, self-check the JSON against it.",
    ]
    if min_strings > 1:
        requirements.append(f"- Return at least {min_strings} valid information_strings unless the cell is genuinely empty.")
    if require_mechanism:
        requirements.append("- Reject your own string if mechanism is missing or only narrative.")
    if require_kill_signal:
        requirements.append("- Reject your own string if kill_signal is missing or not observable.")
    if require_evidence_refs:
        requirements.append("- Prefer evidence_refs/source refs from provided tool evidence; mark uncertainty instead of inventing refs.")
    requirements.append("- If you cannot satisfy this contract, return empty hypothesis/information_strings and explain the data gap in rationale_brief.")
    requirements.append("</market_evolve_prompt_contract>")
    return system_prompt + "\n".join(requirements)


def _int_payload(raw: Any, *, default: int, lo: int, hi: int) -> int:
    try:
        value = int(raw)
    except Exception:
        value = default
    return max(lo, min(hi, value))


def _build_user_prompt(
    seed: SeedCell,
    tool_evidence: Optional[list[dict[str, Any]]] = None,
) -> str:
    pieces = [
        f"entity={seed.entity}",
        f"horizon={seed.horizon}",
        f"lens={seed.lens}",
        f"bias_mode={seed.bias_mode}",
    ]
    if seed.theme:
        pieces.append(f"theme={seed.theme}")
    tool_candidates = _prompt_tool_candidates(seed, tool_evidence)
    calendar_trigger = seed.payload.get("calendar_trigger") if seed.payload else None
    calendar_block = ""
    if calendar_trigger:
        try:
            trigger_text = json.dumps(calendar_trigger, sort_keys=True)[:1200]
        except Exception:
            trigger_text = str(calendar_trigger)[:1200]
        calendar_block = (
            "\n\ncritical_calendar_context:\n  "
            + trigger_text
            + "\n\nThis calendar context is source-derived and must be "
              "researched directly in the hypothesis."
        )
        kind = str(calendar_trigger.get("kind") or "").lower()
        if kind == "earnings":
            calendar_block += (
                "\n\nearnings_hypothesis_rules:\n"
                "  - Do not compare share price directly to EPS/revenue "
                "consensus values; the units differ.\n"
                "  - Formulate the edge as a reaction test around EPS/revenue "
                "surprise, guidance, revisions, options-implied move, "
                "positioning, or post-earnings drift.\n"
                "  - Include a falsification window for price, volatility, "
                "or estimate-revision response."
            )
    evidence_block = ""
    if tool_evidence:
        lines = []
        for ev in tool_evidence[:4]:
            lines.append(
                f"- {ev.get('uri')} ok={ev.get('ok')} "
                f"tool_call_log_id={ev.get('tool_call_log_id')} "
                f"summary={str(ev.get('summary') or '')[:500]}"
            )
        evidence_block = "\n\ntool_evidence:\n  " + "\n  ".join(lines)
    prior_block = ""
    try:
        scoped = select_information_context(
            entity=seed.entity,
            theme=seed.theme,
            lens=seed.lens,
            horizon=seed.horizon,
            limit=6,
        )
        if scoped:
            prior_lines = []
            for p in scoped:
                prior_lines.append(
                    f"- {p.get('id')} entity={p.get('entity')} theme={p.get('theme')} "
                    f"lens={p.get('lens')} horizon={p.get('horizon')} "
                    f"conviction={float(p.get('conviction') or 0):.2f} "
                    f"novelty={float(p.get('novelty_score') or 0):.2f} "
                    f"thesis={str(p.get('thesis') or '')[:260]}"
                )
            prior_block = "\n\nprior_information_strings:\n  " + "\n  ".join(prior_lines)
    except Exception:
        prior_block = ""
    route_contract_block = _alpha_geometry_route_contract_block(seed)
    as_of = datetime.now(timezone.utc).isoformat()
    return (
        f"as_of_utc={as_of}\n"
        "Freshness rule: for intraday/1d/1w horizons, do not use old historical dates as live catalyst evidence unless explicitly labeled as regime context.\n\n"
        "Cell:\n  " + "\n  ".join(pieces) +
        calendar_block +
        evidence_block +
        prior_block +
        route_contract_block +
        "\n\natlas_policy:\n  Scouts may use any read-only approved atlas tool/source before the LLM call, "
        "under the evidence budget. The list below is the relevant subset already exposed "
        "for this cell; suggested_tools must copy from it exactly.\n"
        "\n\nallowed_tool_candidates:\n  " +
        "\n  ".join(str(t) for t in tool_candidates[:12]) +
        "\n\nReturn one JSON object only."
    )


def _alpha_geometry_route_contract_block(seed: SeedCell) -> str:
    payload = seed.payload or {}
    is_shape_route = (
        str(payload.get("source") or "") in {"alpha_geometry_route", "market_map_governor", "frontier_llm_governor"}
        or bool(payload.get("alpha_geometry_route_directive"))
        or bool(payload.get("alpha_geometry_action"))
        or bool(payload.get("market_map_alpha_geometry_context"))
    )
    if not is_shape_route:
        return ""
    contract = {
        "source": payload.get("source"),
        "source_cycle_id": payload.get("alpha_geometry_source_cycle_id"),
        "cell_key": payload.get("alpha_geometry_cell_key"),
        "route_task_id": payload.get("alpha_geometry_route_task_id"),
        "route_directive": payload.get("alpha_geometry_route_directive"),
        "action": payload.get("alpha_geometry_action"),
        "owner": payload.get("alpha_geometry_action_owner"),
        "priority": payload.get("alpha_geometry_action_priority"),
        "reason": payload.get("alpha_geometry_action_reason") or payload.get("why_this_seed_exists"),
        "missing_edges": payload.get("alpha_geometry_missing_edges") or payload.get("expected_edges") or [],
        "success_gate": payload.get("alpha_geometry_success_gate"),
        "suggested_next_tools": payload.get("alpha_geometry_suggested_next_tools") or payload.get("suggested_tools") or [],
        "global_shape": payload.get("alpha_geometry_global_shape"),
    }
    market_map_context = payload.get("market_map_alpha_geometry_context")
    if isinstance(market_map_context, dict):
        action_plan = (
            market_map_context.get("action_plan")
            if isinstance(market_map_context.get("action_plan"), dict)
            else {}
        )
        contract["market_map_alpha_geometry_context"] = {
            "status": market_map_context.get("status"),
            "source": market_map_context.get("source"),
            "cortex_prompt_hint": action_plan.get("cortex_prompt_hint") or market_map_context.get("cortex_prompt_hint"),
            "top_actions": [
                {
                    "action": row.get("action"),
                    "cell_key": row.get("cell_key"),
                    "success_gate": row.get("success_gate"),
                    "missing_edges": row.get("missing_edges"),
                }
                for row in (market_map_context.get("top_actions") or action_plan.get("actions") or [])[:3]
                if isinstance(row, dict)
            ],
        }
    compact = {
        key: value
        for key, value in contract.items()
        if value not in (None, "", [], {})
    }
    if not compact:
        return ""
    try:
        rendered = json.dumps(compact, sort_keys=True, ensure_ascii=True)[:2400]
    except Exception:
        rendered = str(compact)[:2400]
    return (
        "\n\nalpha_geometry_route_contract:\n  "
        + rendered
        + "\n\nshape_route_rules:\n"
        "  - This seed exists because the market map shape requested follow-up work.\n"
        "  - Use the native shape-reader evidence and the allowed tools to satisfy or falsify the success_gate.\n"
        "  - Your information_strings should explicitly change, confirm, contradict, or kill the missing_edges.\n"
        "  - If the edge cannot be moved with current tools, request the missing tool/source with expected_edge and eval_plan.\n"
    )


def _route_contract_for_seed(seed: SeedCell) -> dict[str, Any]:
    payload = seed.payload or {}
    is_shape_route = (
        str(payload.get("source") or "") in {"alpha_geometry_route", "market_map_governor", "frontier_llm_governor"}
        or bool(payload.get("alpha_geometry_route_directive"))
        or bool(payload.get("alpha_geometry_action"))
        or bool(payload.get("market_map_alpha_geometry_context"))
    )
    if not is_shape_route:
        return {}
    return {
        "source": payload.get("source"),
        "source_cycle_id": payload.get("alpha_geometry_source_cycle_id"),
        "cell_key": payload.get("alpha_geometry_cell_key"),
        "route_task_id": payload.get("alpha_geometry_route_task_id"),
        "route_directive": payload.get("alpha_geometry_route_directive"),
        "action": payload.get("alpha_geometry_action"),
        "owner": payload.get("alpha_geometry_action_owner"),
        "priority": payload.get("alpha_geometry_action_priority"),
        "reason": payload.get("alpha_geometry_action_reason") or payload.get("why_this_seed_exists"),
        "missing_edges": payload.get("alpha_geometry_missing_edges") or payload.get("expected_edges") or [],
        "success_gate": payload.get("alpha_geometry_success_gate"),
        "suggested_next_tools": payload.get("alpha_geometry_suggested_next_tools") or payload.get("suggested_tools") or [],
        "global_shape": payload.get("alpha_geometry_global_shape"),
    }


def _evaluate_route_contract_alignment(
    *,
    seed: SeedCell,
    parsed: dict[str, Any],
    information_strings: list[InformationString],
    tool_requests: list[dict[str, Any]],
) -> RouteContractAlignment:
    contract = _route_contract_for_seed(seed)
    missing_edges = [str(x) for x in (contract.get("missing_edges") or []) if str(x).strip()]
    if not contract:
        return RouteContractAlignment(score=1.0, flags=["route_contract_not_applicable"])
    flags: list[str] = ["route_contract_applicable"]
    if not information_strings and not tool_requests:
        return RouteContractAlignment(
            score=0.0,
            flags=[*flags, "route_contract_missing_output"],
            missed_edges=missing_edges,
        )
    text = _route_alignment_text(
        parsed=parsed,
        information_strings=information_strings,
        tool_requests=tool_requests,
    )
    addressed: list[str] = []
    missed: list[str] = []
    for edge in missing_edges:
        if _edge_is_addressed(edge, text, tool_requests):
            addressed.append(edge)
        else:
            missed.append(edge)
    if missing_edges and not addressed:
        flags.append("route_contract_no_edge_moved")
    elif missed:
        flags.append("route_contract_partial_edges")
    elif missing_edges:
        flags.append("route_contract_all_edges_addressed")
    else:
        flags.append("route_contract_no_missing_edges")

    gate = str(contract.get("success_gate") or "")
    gate_addressed = not gate or _success_gate_is_addressed(gate, text, tool_requests)
    if gate_addressed:
        flags.append("route_contract_success_gate_addressed")
    else:
        flags.append("route_contract_success_gate_missing")

    request_edges = {
        str(req.get("expected_edge") or "")
        for req in tool_requests
        if str(req.get("expected_edge") or "").strip()
    }
    if request_edges & set(missing_edges):
        flags.append("route_contract_tool_request_for_missing_edge")

    edge_score = (
        1.0
        if not missing_edges
        else len(addressed) / max(1.0, float(len(missing_edges)))
    )
    score = edge_score * 0.75 + (0.25 if gate_addressed else 0.0)
    return RouteContractAlignment(
        score=round(max(0.0, min(1.0, score)), 3),
        flags=sorted(set(flags)),
        addressed_edges=addressed,
        missed_edges=missed,
    )


def _route_alignment_text(
    *,
    parsed: dict[str, Any],
    information_strings: list[InformationString],
    tool_requests: list[dict[str, Any]],
) -> str:
    chunks: list[str] = [
        str(parsed.get("hypothesis") or ""),
        str(parsed.get("rationale_brief") or ""),
    ]
    for info in information_strings:
        chunks.extend([
            info.title,
            info.thesis,
            info.mechanism,
            info.expected_outcome,
            info.kill_signal,
            " ".join(str(x) for x in info.entities_chain),
            " ".join(
                str(layer.get("claim") or layer)
                for layer in info.depth_layers
                if isinstance(layer, dict)
            ),
            " ".join(info.quality_flags),
        ])
    for req in tool_requests:
        chunks.extend([
            str(req.get("tool_uri") or ""),
            str(req.get("tool_name") or ""),
            str(req.get("why") or ""),
            str(req.get("expected_edge") or ""),
            str(req.get("fallback_if_denied") or ""),
        ])
    return " ".join(chunks).lower().replace("_", " ")


def _edge_is_addressed(
    edge: str,
    text: str,
    tool_requests: list[dict[str, Any]],
) -> bool:
    normalized_edge = edge.lower().replace("_", " ")
    if normalized_edge in text:
        return True
    for req in tool_requests:
        if str(req.get("expected_edge") or "").strip().lower() == edge.lower():
            return True
    tokens = [
        token
        for token in normalized_edge.replace("->", " ").replace("|", " ").split()
        if len(token) >= 4 and token not in {"edge", "cell", "claim"}
    ]
    if not tokens:
        return False
    hits = sum(1 for token in tokens if token in text)
    return hits >= max(1, min(len(tokens), 2))


def _success_gate_is_addressed(
    gate: str,
    text: str,
    tool_requests: list[dict[str, Any]],
) -> bool:
    gate_text = gate.lower().replace("_", " ")
    if gate_text in text:
        return True
    request_text = " ".join(
        " ".join(str(req.get(key) or "") for key in ("why", "expected_edge", "fallback_if_denied"))
        for req in tool_requests
    ).lower().replace("_", " ")
    if gate_text in request_text:
        return True
    gate_tokens = [
        token.strip(".,;:()[]")
        for token in gate_text.split()
        if len(token) >= 5 and token not in {"string", "strings", "source", "sources"}
    ]
    if not gate_tokens:
        return False
    joined = f"{text} {request_text}"
    hits = sum(1 for token in gate_tokens if token in joined)
    return hits >= max(1, min(2, len(gate_tokens)))


def _route_edge_slug(edge: str) -> str:
    cleaned = "".join(
        ch.lower() if ch.isalnum() else "_"
        for ch in str(edge or "")
    )
    parts = [part for part in cleaned.split("_") if part]
    return "_".join(parts[:8])[:96] or "unknown"


def _prompt_tool_candidates(
    seed: SeedCell,
    tool_evidence: Optional[list[dict[str, Any]]] = None,
) -> list[str]:
    candidates: list[str] = []
    for raw in (seed.payload or {}).get("tool_candidates") or []:
        value = str(raw or "").strip()
        if value:
            candidates.append(value)
    for raw in (seed.payload or {}).get("expanded_tool_candidates") or []:
        value = str(raw or "").strip()
        if value:
            candidates.append(value)
    for ev in tool_evidence or []:
        value = str(ev.get("uri") or "").strip()
        if value:
            candidates.append(value)
    return list(dict.fromkeys(candidates))


def _run_seed_evidence_tools(
    seed: SeedCell,
    cycle_id: str,
    scout_id: str,
    max_tools: int = 2,
) -> list[dict[str, Any]]:
    """Run a small schema-safe evidence slice before the scout LLM call.

    This intentionally covers only high-confidence argument shapes. Unknown
    tool schemas stay in the candidate menu for the LLM to suggest, but are
    not invoked here.
    """
    explicit_candidates = list((seed.payload or {}).get("tool_candidates") or [])
    candidates = _expanded_tool_candidate_rows(
        seed=seed,
        cycle_id=cycle_id,
        explicit_candidates=explicit_candidates,
    )
    if not candidates:
        return []
    try:
        from ..tool_atlas import AgentContext, dispatch_uri
    except Exception as e:
        logger.info("scout evidence dispatcher unavailable: %s", e)
        return []

    out: list[dict[str, Any]] = []
    effective_max_tools = max(max_tools, 4) if _seed_is_eventish(seed) else max_tools
    try:
        requested = int((seed.payload or {}).get("max_evidence_tools") or 0)
    except Exception:
        requested = 0
    if requested > 0:
        effective_max_tools = max(effective_max_tools, requested)
    effective_max_tools = min(SCOUT_EVIDENCE_HARD_CAP, effective_max_tools)
    context = AgentContext(
        cycle_id=cycle_id,
        specialist_id="tier1_scout",
        investigation_id=scout_id,
    )
    for candidate in candidates:
        uri = str(candidate.get("tool_uri") or "")
        if not uri:
            continue
        args = _infer_tool_args_for_candidate(uri, seed, candidate)
        if args is None:
            continue
        out.append(_dispatch_scout_tool(uri, args, context))
        if len(out) >= effective_max_tools:
            break
    return out


def _run_requested_tool_calls(
    requests: list[dict[str, Any]],
    *,
    seed: SeedCell,
    cycle_id: str,
    scout_id: str,
    existing_evidence: list[dict[str, Any]],
    max_new: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Execute model-requested follow-up tool calls inside the scout harness.

    Existing-tool requests are only dispatchable if they survived
    `_normalize_tool_requests`, which keeps them atlas-bound. Missing tools or
    calls blocked by budget are returned as deferred proposals for the tool
    creation/evaluation layer.
    """
    if max_new <= 0 or not requests:
        return [], list(requests)
    try:
        from ..tool_atlas import AgentContext, dispatch_uri
    except Exception as e:
        logger.info("scout requested-tool dispatcher unavailable: %s", e)
        return [], list(requests)

    context = AgentContext(
        cycle_id=cycle_id,
        specialist_id="tier1_scout",
        investigation_id=scout_id,
    )
    candidates = _expanded_tool_candidate_rows(
        seed=seed,
        cycle_id=cycle_id,
        explicit_candidates=list((seed.payload or {}).get("tool_candidates") or []),
    )
    row_by_uri = {
        str(row.get("tool_uri") or ""): row
        for row in candidates
        if row.get("tool_uri")
    }
    allowed_uris = set(_prompt_tool_candidates(seed, existing_evidence))
    seen = {
        json.dumps({
            "uri": ev.get("uri"),
            "args": ev.get("args") or {},
        }, sort_keys=True, default=str)
        for ev in existing_evidence
        if ev.get("uri")
    }
    out: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    ranked_requests = sorted(requests, key=lambda r: _priority_rank(r.get("priority")))
    for idx, req in enumerate(ranked_requests):
        uri = str(req.get("tool_uri") or "").strip()
        if not uri:
            deferred.append(req)
            continue
        if uri not in allowed_uris or not uri.startswith(("tic://tool/", "tic://source/")):
            deferred.append({
                **req,
                "tool_uri": "",
                "why": f"{req.get('why') or 'Scout requested follow-up evidence.'} [denied_unapproved_tool_uri:{uri}]",
                "priority": "low",
            })
            continue
        args = dict(req.get("args") or {})
        if not args:
            args = _infer_tool_args_for_candidate(uri, seed, row_by_uri.get(uri)) or {}
        key = json.dumps({"uri": uri, "args": args}, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        ev = _dispatch_scout_tool(
            uri,
            args,
            context,
            phase="tool_request_iteration",
            requested_by_model=True,
            request_why=req.get("why"),
            expected_edge=req.get("expected_edge"),
            expected_info_value=req.get("expected_info_value"),
            would_change_decision=req.get("would_change_decision"),
            fallback_if_denied=req.get("fallback_if_denied"),
        )
        out.append(ev)
        if len(out) >= max_new:
            deferred.extend(ranked_requests[idx + 1:])
            break
    return out, deferred


def _priority_rank(raw: Any) -> int:
    value = _priority(raw)
    return {"high": 0, "medium": 1, "low": 2}.get(value, 1)


def _dispatch_scout_tool(
    uri: str,
    args: dict[str, Any],
    context: Any,
    *,
    phase: str = "evidence",
    requested_by_model: bool = False,
    request_why: str | None = None,
    expected_edge: str | None = None,
    expected_info_value: float | None = None,
    would_change_decision: bool | None = None,
    fallback_if_denied: str | None = None,
) -> dict[str, Any]:
    """Dispatch a read-only scout tool through the shared desk harness."""
    return dispatch_harness_tool(
        uri,
        args,
        context,
        policy=_scout_harness_policy(),
        phase=phase,
        requested_by_model=requested_by_model,
        request_why=request_why,
        expected_edge=expected_edge,
        expected_info_value=expected_info_value,
        would_change_decision=would_change_decision,
        fallback_if_denied=fallback_if_denied,
    )


def _scout_harness_policy() -> HarnessPolicy:
    return HarnessPolicy(
        evidence_hard_cap=SCOUT_EVIDENCE_HARD_CAP,
        max_tool_iterations=SCOUT_TOOL_ITERATION_HARD_CAP,
        max_retries=SCOUT_TOOL_RETRY_MAX,
    )


def _scout_tool_iteration_limit(seed: SeedCell) -> int:
    payload = seed.payload or {}
    raw = payload.get("max_tool_iterations", payload.get("tool_iteration_limit"))
    if raw is not None:
        try:
            return max(0, min(SCOUT_TOOL_ITERATION_HARD_CAP, int(raw)))
        except Exception:
            pass
    return 2 if _seed_is_eventish(seed) else 1


def _iteration_user_prompt(
    *,
    seed: SeedCell,
    tool_evidence: list[dict[str, Any]],
    previous_parsed: dict[str, Any],
    iteration: int,
) -> str:
    request_lines = []
    for req in _normalize_tool_requests(
        previous_parsed.get("tool_requests"),
        allowed_tools=_prompt_tool_candidates(seed, tool_evidence),
    )[:4]:
        request_lines.append(
            f"- {req.get('tool_uri') or req.get('tool_name')} "
            f"priority={req.get('priority')} edge={req.get('expected_edge')} "
            f"why={str(req.get('why') or '')[:240]}"
        )
    previous_hypothesis = str(previous_parsed.get("hypothesis") or "")[:320]
    return (
        _build_user_prompt(seed, tool_evidence=tool_evidence)
        + "\n\nscout_tool_iteration:\n"
        + f"  iteration={iteration}\n"
        + "  The harness executed your permitted follow-up tool requests above. "
        "Now revise from the full evidence set and return the final strict JSON. "
        "Do not repeat already satisfied tool_requests; only include tool_requests "
        "for still-missing evidence or genuinely missing tools.\n"
        + f"  previous_hypothesis={previous_hypothesis}\n"
        + ("  previous_requests:\n  " + "\n  ".join(request_lines) if request_lines else "")
    )


def _expanded_tool_candidate_rows(
    *,
    seed: SeedCell,
    cycle_id: str,
    explicit_candidates: list[str],
    max_candidates: int = 48,
) -> list[dict[str, Any]]:
    """Return explicit seed tools plus relevant read-only atlas tools/sources."""
    explicit = [str(uri or "").strip() for uri in explicit_candidates if str(uri or "").strip()]
    explicit_set = set(explicit)
    atlas_rows: list[dict[str, Any]] = []
    if not (seed.payload or {}).get("disable_atlas_expansion"):
        try:
            from .. import tool_atlas

            snapshot = tool_atlas.get_atlas_snapshot_for_cycle(cycle_id)
            atlas_rows = [row for row in snapshot.rows if isinstance(row, dict)]
        except Exception as e:
            logger.debug("scout atlas expansion skipped: %s", e)
            atlas_rows = []

    row_by_uri = {
        str(row.get("tool_uri") or ""): row
        for row in atlas_rows
        if row.get("tool_uri")
    }
    out: list[dict[str, Any]] = []
    for uri in explicit:
        row = dict(row_by_uri.get(uri) or {})
        row.setdefault("tool_uri", uri)
        row.setdefault("tool_name", uri.rsplit("/", 1)[-1].split("@", 1)[0])
        row.setdefault("status", "explicit")
        row.setdefault("permission_scope", "read_only")
        out.append(row)

    extras = [
        row for row in atlas_rows
        if str(row.get("tool_uri") or "") not in explicit_set
        and _atlas_row_is_scout_callable(row)
    ]
    ranked = sorted(
        extras,
        key=lambda row: _atlas_candidate_score(row, seed),
        reverse=True,
    )
    out.extend(row for row in ranked if _atlas_candidate_score(row, seed) > 0)
    deduped: dict[str, dict[str, Any]] = {}
    for row in out:
        uri = str(row.get("tool_uri") or "")
        if uri and uri not in deduped:
            deduped[uri] = row
    return list(deduped.values())[:max_candidates]


def _atlas_row_is_scout_callable(row: dict[str, Any]) -> bool:
    uri = str(row.get("tool_uri") or "")
    if not uri.startswith(("tic://tool/", "tic://source/")):
        return False
    status = str(row.get("status") or "active").lower()
    if status not in {"active", "explicit"}:
        return False
    scope = str(row.get("permission_scope") or "read_only").lower()
    return scope in {"read_only", "read", "safe_read"}


def _atlas_candidate_score(row: dict[str, Any], seed: SeedCell) -> float:
    uri = str(row.get("tool_uri") or "").lower()
    name = str(row.get("tool_name") or "").lower()
    description = str(row.get("description") or "").lower()
    provider = str(row.get("provider") or "").lower()
    kind = str(row.get("kind") or "").lower()
    deps = " ".join(str(x).lower() for x in _as_list(row.get("source_dependencies")))
    haystack = " ".join([uri, name, description, provider, kind, deps])
    terms = _seed_tool_terms(seed)
    payload = seed.payload or {}
    score = 0.0
    for term in terms:
        if term and term in haystack:
            score += 2.0
    if kind == "source":
        score += 0.8
        score += _source_family_route_score(
            _source_family_for_candidate(row),
            seed,
        )
    if kind in {"builtin", "hydromancer", "learned", "external"}:
        score += 0.6
    if "jarvis" in haystack or provider == "jarvis-trading-engine":
        if seed.entity.upper() in {"HYPE", "BTC", "ETH", "SOL"}:
            score += 1.2
        if seed.lens in {"on_chain", "smart_money", "microstructure", "money_velocity", "catalyst"}:
            score += 1.4
        if any(tok in haystack for tok in ("hyperliquid", "node", "hydromancer", "executor", "strategy")):
            score += 1.0
    if kind == "learned" and payload.get("prefer_learned_tools"):
        try:
            score += max(0.5, min(4.0, float(payload.get("learned_tool_priority_boost") or 1.0)))
        except Exception:
            score += 1.0
    if provider in {"parallel.ai", "perplexity"}:
        score += _web_route_score(seed)
    if _seed_is_eventish(seed):
        for token in ("hydromancer", "wallet", "builder", "reject", "mempool", "clearinghouse", "source", "hl_"):
            if token in haystack:
                score += 1.4
    if seed.horizon in {"tick", "second", "minute", "hour", "intraday", "1d"}:
        for token in ("timeseries", "orderbook", "depth", "funding", "tape", "flow", "micro"):
            if token in haystack:
                score += 1.0
    if seed.entity and seed.entity.lower() in haystack:
        score += 1.0
    return score


def _seed_tool_terms(seed: SeedCell) -> list[str]:
    words: list[str] = []
    for raw in [
        seed.entity,
        seed.lens,
        seed.theme,
        seed.horizon,
        seed.bias_mode,
    ]:
        text = str(raw or "").lower().replace("_", " ")
        words.extend(part for part in text.split() if len(part) >= 3)
    words.extend(LENS_TOOL_TERMS.get(seed.lens, []))
    return list(dict.fromkeys(words))


def _infer_tool_args_for_candidate(
    uri: str,
    seed: SeedCell,
    row: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    if uri.startswith("tic://source/"):
        return _infer_source_args(uri, seed, row)
    args = _infer_tool_args(uri, seed)
    if args is not None:
        return args
    if not row:
        return None
    schema = row.get("schema_json") or {}
    if isinstance(schema, str):
        try:
            schema = json.loads(schema)
        except Exception:
            schema = {}
    if not isinstance(schema, dict):
        return None
    required = [str(x) for x in schema.get("required") or []]
    props = schema.get("properties") or {}
    if not isinstance(props, dict):
        props = {}
    generic = _generic_args_from_schema(props, seed)
    if required:
        return generic if all(key in generic for key in required) else None
    # Tools without required args can be safely sampled; generic args are added
    # when obvious, otherwise an empty read is usually the lowest-waste probe.
    return generic or {}


def _generic_args_from_schema(props: dict[str, Any], seed: SeedCell) -> dict[str, Any]:
    args: dict[str, Any] = {}
    query = " ".join(
        str(x)
        for x in [seed.entity, seed.theme, seed.lens, seed.horizon]
        if x
    )
    for key in props:
        lower = key.lower()
        if lower in {"ticker", "symbol", "entity", "entity_symbol", "coin", "asset"}:
            args[key] = seed.entity
        elif lower in {"tickers", "symbols", "coins", "assets"}:
            args[key] = [seed.entity]
        elif lower in {"query", "q", "search_query"} and query:
            args[key] = query
        elif lower in {"objective", "goal"} and query:
            args[key] = (
                f"Find fresh, source-diverse evidence for {query}. "
                "Prioritize primary sources, current data, and contradictions."
            )
        elif lower == "focus":
            args[key] = str(seed.lens or seed.theme or seed.entity or "markets")
        elif lower in {"limit", "max_results", "top_n", "n"}:
            args[key] = 10
        elif lower in {"max_markets", "max_hits", "max_filings", "feed_limit", "per_ticker_limit"}:
            args[key] = 20
        elif lower in {"lookback_hours", "hours"}:
            args[key] = 168 if _seed_is_eventish(seed) else 72
        elif lower in {"lookback_days", "days", "window_days"}:
            args[key] = 7 if _seed_is_eventish(seed) else 30
        elif lower in {"lookahead_days", "lookforward_days"}:
            args[key] = 14
    return args


def _infer_source_args(
    uri: str,
    seed: SeedCell,
    row: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Typed args for direct `fetch_live(source=...)` atlas rows.

    Unknown sources remain `{}` because most TIC ingesters are safe global reads.
    For high-signal parameterized sources, pass the tightest slice we know how to
    express so 1,000 scouts do not all fetch the same broad universe.
    """
    slug = _source_slug_from_candidate(uri, row)
    if slug == "hl_l4_micro":
        slug = "l4_micro"
    entity = str(seed.entity or "").upper()
    query = _seed_query(seed)
    crypto = entity in {"BTC", "ETH", "SOL", "HYPE"}
    equity_like = bool(entity) and entity.isalnum() and len(entity) <= 8 and not crypto
    eventish = _seed_is_eventish(seed)
    lookback_hours = 168 if eventish else 72

    if slug in {"l4_micro", "coinalyze"} and crypto:
        return {"coins": [entity]}
    if slug in {"deribit_options", "deribit_dvol", "realized_vol"} and crypto:
        if slug == "realized_vol":
            return {"coins": [entity], "lookbacks": [7, 30]}
        return {"coins": [entity]}
    if slug == "pyth_prices" and entity:
        return {"symbols": [entity]}
    if slug == "yahoo_options" and equity_like:
        return {"tickers": [entity], "expiry_count": 3}
    if slug in {
        "analyst_estimates",
        "corporate_actions",
        "insider_trades",
        "institutional_ownership",
        "sec_fundamentals",
        "finmodel_full",
        "news_tickers",
        "asksurf_news",
    } and entity:
        args: dict[str, Any] = {"tickers": [entity]}
        if slug == "news_tickers":
            args["lookback_hours"] = lookback_hours
        if slug == "asksurf_news":
            args.update({"feed_limit": 25, "per_ticker_limit": 8})
        return args
    if slug == "sec_recent_8k":
        return {"lookback_hours": lookback_hours, "max_filings": 50}
    if slug == "sec_edgar_search" and query:
        return {
            "query": query,
            "forms": ["8-K", "10-Q", "10-K", "S-1", "13F-HR"],
            "lookback_days": 14 if eventish else 30,
            "max_hits": 50,
        }
    if slug == "polymarket_search" and query:
        return {"query": query, "max_markets": 10}
    if slug == "polymarket_top":
        return {"limit": 20}
    if slug in {"gdelt_geopolitical", "gdelt_tone"}:
        theme = _gdelt_theme(seed)
        args = {"themes": [theme], "timespan": "7d" if slug == "gdelt_tone" else "24h"}
        if slug == "gdelt_geopolitical":
            args["top_n_per_theme"] = 5
        return args
    if slug in {"econ_events_full", "finnhub_econ"}:
        return {"lookahead_days": 14, "lookback_days": 3}
    if slug == "treasury_auctions":
        return {"lookback_days": 14, "lookforward_days": 30}
    if slug == "finnhub_earnings":
        return {"lookahead_days": 14}
    if slug == "fed_h41":
        return {"weeks_back": 8}
    if slug == "defillama_stables":
        return {"top_n": 15}
    if slug == "whales":
        return {"min_notional_usd": 100_000, "top_n_wallets": 25}
    if slug == "hydromancer":
        return {"top_n_pnl_leaders": 50, "enrich_top_n": 10, "lookback_hours_outcomes": 24}
    if slug == "asksurf_mindshare":
        return {"top_n": 20}
    if slug == "astro_cycles":
        return {}
    if slug == "funding_arb_candidates":
        return {"min_spread_pct": 2.0, "lookback_hours": 6}
    if slug == "cross_asset_corr":
        return {"lookback_days": 60}
    if slug == "hl_reject_corpus":
        return {"lookback_hours": 24}
    return {}


def _source_slug_from_candidate(uri: str, row: Optional[dict[str, Any]] = None) -> str:
    if row and row.get("tool_name"):
        return str(row.get("tool_name") or "")
    try:
        path = uri.split("tic://source/", 1)[1].split("?", 1)[0]
    except Exception:
        return uri.rsplit("/", 1)[-1].split("@", 1)[0]
    if "/" in path:
        return path.split("/", 1)[1]
    return path


def _source_family_for_candidate(row: dict[str, Any]) -> str:
    slug = str(row.get("tool_name") or "")
    if not slug:
        deps = _as_list(row.get("source_dependencies"))
        slug = str(deps[0]) if deps else str(row.get("tool_uri") or "")
    text = " ".join([
        slug,
        str(row.get("description") or ""),
        str(row.get("provider") or ""),
    ]).lower()
    if any(tok in text for tok in ("farm_grok_x_alpha", "grok", "x_search", "xai", "twitter", "x.com")):
        return "grok_x_alpha"
    if any(tok in text for tok in ("hl_", "hyperliquid", "funding", "perp", "l4", "coinalyze", "pyth", "coingecko")):
        return "crypto_market_microstructure"
    if any(tok in text for tok in ("hydromancer", "wallet", "whale", "nansen", "onchain", "stablecoin", "dex")):
        return "onchain_wallet_actor"
    if any(tok in text for tok in ("option", "vol", "skew", "deribit", "occ", "dvol")):
        return "options_vol"
    if any(tok in text for tok in ("macro", "fed", "fomc", "treasury", "fred", "gdp", "eia", "dol", "ism", "ofr")):
        return "macro_official"
    if any(tok in text for tok in ("sec", "edgar", "earnings", "analyst", "fundamental", "insider", "institutional", "corporate")):
        return "equity_fundamental_filings"
    if any(tok in text for tok in ("news", "gdelt", "social", "sentiment", "mindshare", "asksurf")):
        return "news_social_attention"
    if any(tok in text for tok in ("polymarket", "outcome", "prediction")):
        return "prediction_markets"
    if any(tok in text for tok in ("weather", "shipping", "freight", "mobility", "cdc", "nhtsa", "usda", "nasa", "cms")):
        return "real_economy_alt"
    if any(tok in text for tok in ("regulatory", "congress", "lobby", "patent", "visa", "grant", "crunchbase", "spending")):
        return "regulatory_innovation_gov"
    if any(tok in text for tok in ("astro", "celestial", "sunspot", "lunar", "planetary")):
        return "celestial_cycles"
    return "general"


def _source_family_route_score(family: str, seed: SeedCell) -> float:
    lens = str(seed.lens or "")
    asset_crypto = str(seed.entity or "").upper() in {"BTC", "ETH", "SOL", "HYPE"}
    weights: dict[str, dict[str, float]] = {
        "macro": {"macro_official": 3.0, "real_economy_alt": 1.4, "news_social_attention": 0.8},
        "microstructure": {"crypto_market_microstructure": 3.0, "options_vol": 1.0, "grok_x_alpha": 0.8},
        "options_flow": {"options_vol": 3.2, "crypto_market_microstructure": 0.8},
        "vol_surface": {"options_vol": 3.2, "crypto_market_microstructure": 0.8},
        "smart_money": {"onchain_wallet_actor": 3.2, "crypto_market_microstructure": 1.6, "grok_x_alpha": 1.7, "news_social_attention": 0.6},
        "on_chain": {"onchain_wallet_actor": 3.2, "crypto_market_microstructure": 1.8, "grok_x_alpha": 1.5, "news_social_attention": 0.8},
        "sentiment": {"grok_x_alpha": 3.4, "news_social_attention": 3.0, "prediction_markets": 1.2},
        "catalyst": {"equity_fundamental_filings": 2.2, "grok_x_alpha": 2.4, "news_social_attention": 2.0, "prediction_markets": 1.0},
        "filing": {"equity_fundamental_filings": 3.2, "regulatory_innovation_gov": 0.8},
        "polymarket": {"prediction_markets": 3.2, "grok_x_alpha": 1.8, "news_social_attention": 1.4},
        "rotation": {"macro_official": 1.4, "crypto_market_microstructure": 1.0, "news_social_attention": 0.8},
        "factor": {"equity_fundamental_filings": 1.8, "macro_official": 1.0},
        "money_velocity": {"macro_official": 2.2, "onchain_wallet_actor": 1.8},
        "structural": {"regulatory_innovation_gov": 1.8, "real_economy_alt": 1.6, "grok_x_alpha": 1.3, "macro_official": 1.2},
        "anomaly": {"grok_x_alpha": 1.8, "celestial_cycles": 1.2, "news_social_attention": 1.4, "crypto_market_microstructure": 1.0},
    }
    score = weights.get(lens, {}).get(family, 0.0)
    if asset_crypto and family in {"crypto_market_microstructure", "onchain_wallet_actor"}:
        score += 0.8
    if asset_crypto and family == "grok_x_alpha" and lens in {"sentiment", "catalyst", "on_chain", "smart_money", "anomaly"}:
        score += 0.6
    if family == "celestial_cycles" and lens in {"structural", "macro", "anomaly"}:
        score += 0.8
    return score


def _web_route_score(seed: SeedCell) -> float:
    if seed.lens in {"sentiment", "catalyst", "filing", "polymarket", "structural", "anomaly"}:
        return 1.6
    if _seed_is_eventish(seed):
        return 0.9
    return 0.2


def _seed_query(seed: SeedCell) -> str:
    return " ".join(
        str(x)
        for x in [seed.entity, seed.theme, seed.lens, seed.horizon]
        if x
    ).strip()


def _gdelt_theme(seed: SeedCell) -> str:
    text = " ".join([str(seed.theme or ""), str(seed.lens or ""), str(seed.entity or "")]).lower()
    if any(tok in text for tok in ("fed", "rates", "inflation", "macro", "treasury")):
        return "ECON_INTEREST_RATES"
    if any(tok in text for tok in ("war", "conflict", "geopolitical", "tariff", "china")):
        return "CONFLICT"
    if any(tok in text for tok in ("energy", "oil", "gas", "eia")):
        return "ENERGY"
    if any(tok in text for tok in ("ai", "semiconductor", "chip", "capex")):
        return "TECHNOLOGY"
    return "ECON_STOCKMARKET"


def _as_list(raw: Any) -> list[Any]:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, tuple):
        return list(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return parsed
        except Exception:
            return [raw]
    return []


def _infer_tool_args(uri: str, seed: SeedCell) -> Optional[dict[str, Any]]:
    """Best-effort args for safe read-only tools with stable schemas."""
    slug = uri.split("/")[-1].split("@", 1)[0]
    entity = seed.entity.replace("_", " ")
    ticker = seed.entity if seed.entity.isalnum() and len(seed.entity) <= 8 else None
    eventish = _seed_is_eventish(seed)
    payload = seed.payload or {}
    if slug == "plan_alpha_geometry_actions":
        cycle_id = (
            payload.get("alpha_geometry_source_cycle_id")
            or payload.get("cycle_id")
            or ((payload.get("market_map_alpha_geometry_context") or {}).get("cycle_id")
                if isinstance(payload.get("market_map_alpha_geometry_context"), dict) else None)
        )
        if not cycle_id:
            return None
        args: dict[str, Any] = {
            "cycle_id": str(cycle_id),
            "limit": int(payload.get("shape_reader_limit") or 64),
        }
        if isinstance(payload.get("geometry_weights"), dict):
            args["geometry_weights"] = payload["geometry_weights"]
        if isinstance(payload.get("routing_thresholds"), dict):
            args["routing_thresholds"] = payload["routing_thresholds"]
        return args
    if slug == "farm_grok_x_alpha":
        return {
            "entity": seed.entity,
            "horizon": seed.horizon,
            "lens": seed.lens,
            "query": _seed_query(seed),
            "max_candidates": 8,
            "allow_live": os.environ.get("TALIS_ALLOW_LIVE_GROK_X_ALPHA") == "1",
        }
    if slug == "query_events_recent":
        if seed.lens == "catalyst":
            event_types = ["earnings", "filing", "macro_release", "news"]
        elif seed.lens == "macro":
            event_types = ["macro_release"]
            ticker = None
        elif seed.lens == "filing":
            event_types = ["filing"]
        elif eventish:
            # Keep this broad. Event-intelligence rows are often vendor-
            # specific (`whale_move`, `unlock`, `unstake`) and strict filters
            # are an easy way to waste the scout's only evidence slice.
            event_types = None
        else:
            event_types = None
        return {
            "event_types": event_types,
            "lookback_hours": 168 if eventish else 72,
            "ticker": ticker,
            "limit": 10,
        }
    if slug == "query_timeseries":
        if not seed.entity:
            return None
        return {
            "entity_symbol": seed.entity,
            "metric_prefix": "",
            "lookback_hours": 168 if eventish else 72,
            "limit": 80 if eventish else 40,
        }
    if slug in {"jarvis_intelligence_surfaces", "jarvis_surface_search"}:
        query = _seed_query(seed)
        if slug == "jarvis_surface_search":
            return {"query": query or seed.entity or seed.lens, "limit": 8}
        return {
            "query": query,
            "entity": seed.entity,
            "lens": seed.lens,
            "limit": 8,
        }
    if slug == "get_hl_pnl_leaderboard" and eventish:
        return {
            "window_days": 7,
            "sort_by": "realized_pnl",
            "top_n": 25,
            "min_volume_usd": 100_000,
        }
    if slug == "get_builder_fills" and eventish:
        return {
            "lookback_hours": 24,
            "limit": 1000,
        }
    wallet = _seed_wallet(seed)
    if slug == "get_wallet_pnl_summary" and wallet:
        return {"wallet_address": wallet, "lookback_days": 30}
    if slug == "get_wallet_completed_trades" and wallet:
        return {"wallet_address": wallet, "lookback_days": 30, "limit": 100}
    if slug == "get_wallet_historical_orders" and wallet:
        return {"wallet_address": wallet, "lookback_days": 14, "include_rejects": True}
    if slug == "batch_get_clearinghouse_states" and wallet:
        return {"wallets": [wallet]}
    if slug == "hydromancer_query" and eventish:
        return {"action": "allMids", "params": {}, "paginate_all": False}
    if slug == "hl_reject_corpus" and eventish:
        wallet = _seed_wallet(seed)
        return {
            "coin": seed.entity,
            "lookback_minutes": 90,
            "wallets": [wallet] if wallet else [],
            "limit": 500,
        }
    if slug == "compute_rotation_velocity":
        return {"lookback_days": 365}
    if slug == "get_econ_event_today":
        return {}
    if slug == "get_fomc_next_event":
        return {}
    return None


def _summarize_tool_result(result: Any) -> str:
    return _harness_summarize_tool_result(result)


def _classify_tool_error(error: Any) -> dict[str, Any]:
    return _harness_classify_tool_error(error).to_dict()


def _compact_tool_result(result: Any, *, depth: int = 0) -> Any:
    """Keep enough raw data for later event reconstruction without huge rows."""
    return _harness_compact_tool_result(result, depth=depth)


def _short_text(raw: Any, limit: int) -> str:
    return _harness_short_text(raw, limit)


def _allowed_evidence_refs(tool_evidence: list[dict[str, Any]]) -> list[str]:
    refs: list[str] = []
    for ev in tool_evidence or []:
        for key in ("tool_call_log_id", "uri", "source", "id"):
            value = str(ev.get(key) or "").strip()
            if value:
                refs.append(value)
    return list(dict.fromkeys(refs))


def _successful_tool_call_ids(tool_evidence: list[dict[str, Any]]) -> list[str]:
    return [
        str(ev.get("tool_call_log_id"))
        for ev in tool_evidence or []
        if ev.get("ok") and ev.get("tool_call_log_id")
    ]


def _seed_is_eventish(seed: SeedCell) -> bool:
    text = " ".join([
        str(seed.lens or ""),
        str(seed.theme or ""),
        str(seed.bias_mode or ""),
        json.dumps(seed.payload or {}, default=str),
    ]).lower()
    return any(
        token in text
        for token in (
            "on_chain",
            "smart_money",
            "flow",
            "unstake",
            "staking",
            "unlock",
            "validator",
            "deposit",
            "withdraw",
            "bridge",
            "wallet",
            "whale",
            "cex",
            "vesting",
            "token_unlock",
        )
    )


def _seed_wallet(seed: SeedCell) -> str:
    payload = seed.payload or {}
    for key in ("wallet_address", "wallet", "actor_address", "address"):
        value = str(payload.get(key) or "").strip()
        if value.startswith("0x") and len(value) >= 10:
            return value
    return ""


def _event_bundles_for_scout(
    *,
    parsed: dict[str, Any],
    seed: SeedCell,
    cycle_id: str,
    tool_evidence: list[dict[str, Any]],
) -> list[MarketEventIntelligenceBundle]:
    raw = parsed.get("event_intelligence")
    raw_items: list[Any]
    if isinstance(raw, list):
        raw_items = raw
    elif isinstance(raw, dict):
        raw_items = [raw]
    else:
        raw_items = []

    bundles: list[MarketEventIntelligenceBundle] = []
    evidence_refs = _allowed_evidence_refs(tool_evidence)
    for item in raw_items[:4]:
        bundle = normalize_event_intelligence(item, cycle_id=cycle_id)
        if bundle is None:
            continue
        _complete_event_bundle_from_scout(
            bundle,
            seed=seed,
            cycle_id=cycle_id,
            tool_evidence=tool_evidence,
            evidence_refs=evidence_refs,
            source_flag="from_model_event_intelligence",
        )
        bundles.append(bundle)
    if bundles:
        return bundles
    if not _seed_is_eventish(seed):
        return []
    fallback_bundles = event_intelligence_from_tool_evidence(
        cycle_id=cycle_id,
        entity=seed.entity,
        horizon=seed.horizon,
        lens=seed.lens,
        tool_evidence=tool_evidence,
    )
    for bundle in fallback_bundles:
        _complete_event_bundle_from_scout(
            bundle,
            seed=seed,
            cycle_id=cycle_id,
            tool_evidence=tool_evidence,
            evidence_refs=evidence_refs,
            source_flag="from_tool_evidence_fallback",
        )
    return fallback_bundles


def _complete_event_bundle_from_scout(
    bundle: MarketEventIntelligenceBundle,
    *,
    seed: SeedCell,
    cycle_id: str,
    tool_evidence: list[dict[str, Any]],
    evidence_refs: list[str],
    source_flag: str,
) -> None:
    bundle.cycle_id = bundle.cycle_id or cycle_id
    bundle.entity = bundle.entity or seed.entity
    bundle.asset = bundle.asset or seed.entity
    bundle.amount_unit = bundle.amount_unit or seed.entity
    if evidence_refs:
        bundle.source_refs = sorted(set([*bundle.source_refs, *evidence_refs]))
    if hasattr(bundle.actor, "source_refs") and evidence_refs:
        bundle.actor.source_refs = sorted(set([*bundle.actor.source_refs, *evidence_refs]))
    if tool_evidence and not bundle.raw_sources:
        bundle.raw_sources.append(EventDataPoint(
            "raw_source",
            "scout_tool_evidence",
            [
                {
                    "uri": ev.get("uri"),
                    "args": ev.get("args"),
                    "ok": ev.get("ok"),
                    "tool_call_log_id": ev.get("tool_call_log_id"),
                    "result": ev.get("result"),
                    "error": ev.get("error"),
                }
                for ev in tool_evidence[:4]
            ],
            source_ref=evidence_refs[0] if evidence_refs else "",
            confidence=0.7,
        ))
    bundle.quality_flags = sorted(set([*bundle.quality_flags, source_flag]))


def _node_snapshots_for_scout(
    *,
    parsed: dict[str, Any],
    seed: SeedCell,
    cycle_id: str,
    tool_evidence: list[dict[str, Any]],
) -> list[NodeIntelligenceSnapshot]:
    raw = parsed.get("node_intelligence")
    raw_items: list[Any]
    if isinstance(raw, list):
        raw_items = raw
    elif isinstance(raw, dict):
        raw_items = [raw]
    else:
        raw_items = []
    snapshots: list[NodeIntelligenceSnapshot] = []
    evidence_refs = _allowed_evidence_refs(tool_evidence)
    for item in raw_items[:2]:
        snapshot = normalize_node_intelligence(item, cycle_id=cycle_id)
        if snapshot is None:
            continue
        _complete_node_snapshot_from_scout(
            snapshot,
            seed=seed,
            cycle_id=cycle_id,
            evidence_refs=evidence_refs,
            source_flag="from_model_node_intelligence",
        )
        if "unresolved_source_refs" in snapshot.quality_flags:
            continue
        snapshots.append(snapshot)
    if snapshots:
        return snapshots
    if not _seed_is_eventish(seed):
        return []
    fallback = node_intelligence_from_tool_evidence(
        cycle_id=cycle_id,
        entity=seed.entity,
        horizon=seed.horizon,
        lens=seed.lens,
        tool_evidence=tool_evidence,
    )
    if fallback is None:
        return []
    _complete_node_snapshot_from_scout(
        fallback,
        seed=seed,
        cycle_id=cycle_id,
        evidence_refs=evidence_refs,
        source_flag="from_tool_evidence_node_intelligence",
    )
    return [fallback]


def _complete_node_snapshot_from_scout(
    snapshot: NodeIntelligenceSnapshot,
    *,
    seed: SeedCell,
    cycle_id: str,
    evidence_refs: list[str],
    source_flag: str,
) -> None:
    snapshot.cycle_id = snapshot.cycle_id or cycle_id
    snapshot.entity = snapshot.entity or seed.entity
    if evidence_refs:
        snapshot.source_refs = sorted(set([*snapshot.source_refs, *evidence_refs]))
    if source_flag == "from_model_node_intelligence":
        allowed = set(evidence_refs)
        claimed = set(snapshot.source_refs)
        claimed.update(obs.source_ref for obs in snapshot.observations if obs.source_ref)
        for actor in snapshot.actors:
            claimed.update(actor.source_refs)
        unsupported = sorted(ref for ref in claimed if ref and ref not in allowed)
        if unsupported or not allowed:
            snapshot.quality_flags.append("unresolved_source_refs")
            snapshot.coverage["unresolved_source_refs"] = unsupported
            snapshot.observations = [
                obs for obs in snapshot.observations
                if obs.source_ref and obs.source_ref in allowed
            ]
            snapshot.source_refs = sorted(set(
                ref for ref in snapshot.source_refs
                if ref and ref in allowed
            ))
            for actor in snapshot.actors:
                actor.source_refs = sorted(set(
                    ref for ref in actor.source_refs
                    if ref and ref in allowed
                ))
            if not snapshot.observations or not snapshot.source_refs:
                snapshot.quality_flags.append("model_source_unresolved")
    snapshot.quality_flags = sorted(set([*snapshot.quality_flags, source_flag]))


def _persist_tool_proposals_for_flags(
    *,
    cycle_id: str,
    artifact_kind: str,
    artifact_id: str,
    seed: SeedCell,
    quality_flags: list[str],
) -> list[str]:
    proposals = propose_tools_from_quality_flags(
        cycle_id=cycle_id,
        artifact_kind=artifact_kind,
        artifact_id=artifact_id,
        entity=seed.entity,
        horizon=seed.horizon,
        lens=seed.lens,
        quality_flags=quality_flags,
    )
    if not proposals:
        return []
    try:
        return persist_analysis_tool_proposals(proposals)
    except Exception as e:
        logger.debug("tool proposal persist failed: %s", e)
        return []


def _analysis_tool_proposal_from_node_dict(
    raw: dict[str, Any],
    *,
    cycle_id: str,
    artifact_kind: str,
    artifact_id: str,
    seed: SeedCell,
) -> AnalysisToolProposal:
    tool_name = str(raw.get("tool_name") or "node_discovery_tool")
    purpose = str(raw.get("purpose") or "").strip()
    source_family = str(raw.get("source_family") or "").strip()
    trigger = str(raw.get("trigger") or "").strip()
    if not purpose:
        purpose = (
            "Resolve a node-intelligence coverage gap with sourced observations, "
            "raw offsets, and fixture-backed promotion evidence."
        )
    if not source_family:
        source_family = _source_family_from_request(tool_name, "", purpose)
    if not trigger:
        trigger = "node_intelligence_coverage_gap"
    input_shape = dict(raw.get("input_shape") or {})
    if not input_shape:
        input_shape = {
            "entity": seed.entity,
            "horizon": seed.horizon,
            "lens": seed.lens,
            "source_family": source_family,
        }
    promotion_gate = dict(raw.get("promotion_gate") or {})
    promotion_gate.setdefault(
        "expected_edge",
        f"{source_family} -> {seed.entity}/{seed.horizon}/{seed.lens} node-intelligence map edge",
    )
    promotion_gate.setdefault("expected_info_value", 0.65)
    promotion_gate.setdefault("would_change_decision", True)
    promotion_gate.setdefault("must_emit_source_timestamp_or_rejection", True)
    eval_plan = dict(raw.get("eval_plan") or {})
    if not eval_plan:
        eval_plan = {
            "fixture_source": "node_intelligence_coverage_gap",
            "fixture_types": [trigger, source_family],
            "min_pass_rate": 0.80,
            "must_link_artifact_id": artifact_id,
        }
    proposal = AnalysisToolProposal(
        cycle_id=cycle_id,
        artifact_kind=artifact_kind,
        artifact_id=artifact_id,
        entity=seed.entity,
        horizon=seed.horizon,
        lens=seed.lens,
        proposal_kind="new_tool",
        tool_name=tool_name,
        purpose=purpose,
        source_family=source_family,
        trigger=trigger,
        input_shape=input_shape,
        promotion_gate=promotion_gate,
        eval_plan=eval_plan,
        priority=str(raw.get("priority") or "medium"),
        created_by="node_intelligence_discovery",
        quality_flags=[
            "from_node_intelligence_coverage_gap",
            "node_tool_proposal_eval_plan_normalized",
        ],
    )
    return normalize_analysis_tool_proposal_contract(
        proposal,
        reason="node_intelligence_generated",
    )


def _normalize_tool_requests(
    raw: Any,
    *,
    allowed_tools: list[str],
) -> list[dict[str, Any]]:
    return _harness_normalize_tool_requests(raw, allowed_tools=allowed_tools)


def _analysis_tool_proposals_from_requests(
    requests: list[dict[str, Any]],
    *,
    cycle_id: str,
    artifact_kind: str,
    artifact_id: str,
    seed: SeedCell,
) -> list[AnalysisToolProposal]:
    proposals: list[AnalysisToolProposal] = []
    for req in requests:
        tool_uri = str(req.get("tool_uri") or "")
        tool_name = str(req.get("tool_name") or "requested_tool")
        purpose = str(req.get("why") or "Scout requested follow-up evidence.")
        expected_edge = str(req.get("expected_edge") or "")
        input_shape = dict(req.get("args") or {})
        if tool_uri:
            input_shape = {"tool_uri": tool_uri, "args": input_shape}
        proposals.append(AnalysisToolProposal(
            cycle_id=cycle_id,
            artifact_kind=artifact_kind,
            artifact_id=artifact_id,
            entity=seed.entity,
            horizon=seed.horizon,
            lens=seed.lens,
            proposal_kind="next_tool_call" if tool_uri else "new_tool",
            tool_name=tool_name,
            purpose=purpose,
            source_family=_source_family_from_request(tool_name, tool_uri, purpose),
            trigger="scout_tool_request",
            input_shape=input_shape,
            promotion_gate={
                "expected_edge": expected_edge,
                "expected_info_value": req.get("expected_info_value"),
                "would_change_decision": req.get("would_change_decision"),
                "fallback_if_denied": req.get("fallback_if_denied"),
                "requested_by_scout": True,
                "must_improve_information_string_score": True,
            },
            eval_plan={
                "run_on_next_budget_gate": True,
                "compare_new_edge_yield": True,
                "max_retries": 1,
            },
            priority=_priority(req.get("priority")),
            created_by="tier1_scout_tool_request",
            quality_flags=["from_scout_tool_request"],
        ))
    return proposals


def _filter_fulfilled_tool_requests(
    requests: list[dict[str, Any]],
    *,
    tool_evidence: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return _harness_filter_fulfilled_tool_requests(
        requests,
        tool_evidence=tool_evidence,
    )


def _source_family_from_request(tool_name: str, tool_uri: str, purpose: str) -> str:
    text = " ".join([tool_name, tool_uri, purpose]).lower()
    if any(tok in text for tok in ("farm_grok_x_alpha", "grok", "x_search", "xai", "twitter", "x.com")):
        return "grok_x_alpha"
    if any(tok in text for tok in ("hydromancer", "wallet", "whale", "builder", "clearinghouse")):
        return "hydromancer"
    if any(tok in text for tok in ("mempool", "pending", "router")):
        return "mempool"
    if any(tok in text for tok in ("hl_node", "reject", "fill", "order")):
        return "our_hl_node"
    if any(tok in text for tok in ("option", "vol", "funding", "oi", "derivative", "coinalyze")):
        return "derivatives"
    if any(tok in text for tok in ("sec", "filing", "earnings", "analyst", "fundamental")):
        return "fundamentals_filings"
    if any(tok in text for tok in ("news", "social", "gdelt", "mindshare", "parallel", "web")):
        return "news_attention"
    if any(tok in text for tok in ("astro", "celestial", "sunspot", "lunar")):
        return "celestial_cycles"
    return "requested_followup"


def _priority(raw: Any) -> str:
    value = str(raw or "medium").lower()
    return value if value in {"high", "medium", "low"} else "medium"


# ----------------------------------------------------------------------
# Single scout execution
# ----------------------------------------------------------------------

async def _run_one_scout(
    seed: SeedCell,
    cycle_id: str,
    model: str,
    fallback: str,
    cost_counter: dict[str, float],
    cost_cap: float,
) -> ScoutOutput:
    scout_id = f"scout_{uuid4().hex[:10]}"
    t0 = time.perf_counter()
    seed_payload = seed.payload or {}
    out = ScoutOutput(
        seed_id=seed.seed_id,
        scout_id=scout_id,
        cycle_id=cycle_id,
        entity=seed.entity,
        lens=seed.lens,
        horizon=seed.horizon,
        bias_mode=seed.bias_mode,
        hypothesis_text="",
        confidence=0.0,
        rationale_brief="",
        suggested_tools=[],
        calendar_trigger=seed_payload.get("calendar_trigger"),
        calendar_severity=(
            str(seed_payload.get("calendar_severity") or "").lower() or None
        ),
        prompt_variant=_prompt_variant_for_seed(seed),
    )
    task_id = str(seed_payload.get("task_id") or "")
    out.task_id = task_id or None
    if task_id:
        claimed = claim_task(
            task_id,
            agent_id=scout_id,
            specialist_id="tier1_scout",
        )
        if not claimed:
            out.error = "task_claim_failed"
            out.quality_flags.append("task_claim_failed")
            out.elapsed_s = time.perf_counter() - t0
            return out
        start_task(
            task_id,
            agent_id=scout_id,
            specialist_id="tier1_scout",
        )

    # Kill switch — collective scout cost cap.
    if cost_counter.get("total", 0.0) >= cost_cap:
        out.error = "scout_swarm_cost_cap_reached"
        out.quality_flags.append("scout_cost_cap")
        if task_id:
            fail_task(
                task_id,
                agent_id=scout_id,
                specialist_id="tier1_scout",
                reason=out.error,
            )
            attribute_failure(
                artifact_kind="task",
                artifact_id=task_id,
                failure_kind="scout_cost_cap",
                cycle_id=cycle_id,
                task_id=task_id,
                specialist_id="tier1_scout",
                severity="yellow",
                rationale="Tier-1 scout cost cap reached before this task could run.",
            )
        out.elapsed_s = time.perf_counter() - t0
        return out

    tool_evidence = _run_seed_evidence_tools(seed, cycle_id, scout_id)
    out.tool_evidence = tool_evidence
    tool_cost = sum(float(ev.get("cost_usd") or 0.0) for ev in tool_evidence)
    if tool_cost:
        out.cost_usd += tool_cost
        cost_counter["total"] = cost_counter.get("total", 0.0) + tool_cost

    user_prompt = _build_user_prompt(seed, tool_evidence=tool_evidence)
    system_prompt = _apply_prompt_contract_pressure(
        build_deep_scout_system_prompt(out.prompt_variant),
        seed,
    )
    out.quality_flags.append(f"prompt_variant:{out.prompt_variant}")
    _ensure_tic_on_path()
    try:
        from tic.desk.models import chat as _chat  # type: ignore
    except Exception as e:
        out.error = f"tic_models_import_failed: {e}"
        out.quality_flags.append("scout_provider_unavailable")
        if task_id:
            fail_task(task_id, agent_id=scout_id, specialist_id="tier1_scout", reason=out.error)
            attribute_failure(
                artifact_kind="task",
                artifact_id=task_id,
                failure_kind="provider_unavailable",
                cycle_id=cycle_id,
                task_id=task_id,
                specialist_id="tier1_scout",
                severity="red",
                rationale=out.error,
            )
        out.elapsed_s = time.perf_counter() - t0
        return out

    try:
        res = await _chat(
            model, system_prompt, user_prompt,
            max_tokens=SCOUT_MAX_TOKENS, fallback=fallback,
        )
    except Exception as e:
        out.error = f"chat_failed: {type(e).__name__}: {e}"
        out.quality_flags.append("scout_provider_unavailable")
        if task_id:
            fail_task(task_id, agent_id=scout_id, specialist_id="tier1_scout", reason=out.error)
            attribute_failure(
                artifact_kind="task",
                artifact_id=task_id,
                failure_kind="provider_unavailable",
                cycle_id=cycle_id,
                task_id=task_id,
                specialist_id="tier1_scout",
                severity="red",
                rationale=out.error,
            )
        out.elapsed_s = time.perf_counter() - t0
        return out

    text = (res.get("text") or "").strip()
    out.model_used = res.get("model_used", model)
    out.provider = res.get("provider", "?")
    if res.get("error"):
        out.error = res["error"]
        out.quality_flags.append("scout_provider_error")
    # Estimate cost (rough): Flash-tier ~$0.0002/scout. Use 600 tok cap.
    llm_cost = _flash_cost_estimate(out.model_used)
    out.cost_usd += llm_cost
    cost_counter["total"] = cost_counter.get("total", 0.0) + llm_cost

    # Parse strict JSON. If parse fails, drop with quality flag.
    parsed = _extract_first_json(text)
    critical_calendar = out.calendar_severity == "critical"
    calendar_seed = bool(out.calendar_trigger)
    deferred_tool_requests: list[dict[str, Any]] = []
    if isinstance(parsed, dict):
        for iteration in range(1, _scout_tool_iteration_limit(seed) + 1):
            if cost_counter.get("total", 0.0) >= cost_cap:
                out.quality_flags.append("scout_tool_iteration_cost_cap")
                break
            requests = _normalize_tool_requests(
                parsed.get("tool_requests"),
                allowed_tools=_prompt_tool_candidates(seed, tool_evidence),
            )
            if not requests:
                break
            remaining_evidence_budget = SCOUT_EVIDENCE_HARD_CAP - len(tool_evidence)
            if remaining_evidence_budget <= 0:
                deferred_tool_requests.extend(requests)
                out.quality_flags.append("scout_tool_iteration_evidence_cap")
                break
            try:
                requested_per_round = int((seed.payload or {}).get("max_followup_tools_per_iteration") or 0)
            except Exception:
                requested_per_round = 0
            if requested_per_round <= 0:
                requested_per_round = 2 if _seed_is_eventish(seed) else 1
            per_round_cap = max(1, min(4, requested_per_round))
            followup_evidence, deferred = _run_requested_tool_calls(
                requests,
                seed=seed,
                cycle_id=cycle_id,
                scout_id=scout_id,
                existing_evidence=tool_evidence,
                max_new=min(remaining_evidence_budget, per_round_cap),
            )
            deferred_tool_requests.extend(deferred)
            if not followup_evidence:
                out.quality_flags.append("scout_tool_iteration_no_dispatchable_requests")
                break
            tool_evidence = [*tool_evidence, *followup_evidence]
            out.tool_evidence = tool_evidence
            out.tool_iteration_count = iteration
            out.quality_flags.append(f"scout_tool_iteration:{iteration}")
            tool_cost = sum(float(ev.get("cost_usd") or 0.0) for ev in followup_evidence)
            if tool_cost:
                out.cost_usd += tool_cost
                cost_counter["total"] = cost_counter.get("total", 0.0) + tool_cost
            user_prompt = _iteration_user_prompt(
                seed=seed,
                tool_evidence=tool_evidence,
                previous_parsed=parsed,
                iteration=iteration,
            )
            try:
                res = await _chat(
                    model,
                    system_prompt,
                    user_prompt,
                    max_tokens=SCOUT_MAX_TOKENS,
                    fallback=fallback,
                )
            except Exception as e:
                out.quality_flags.append(f"scout_tool_iteration_chat_failed:{type(e).__name__}")
                break
            text = (res.get("text") or "").strip()
            out.model_used = res.get("model_used", out.model_used or model)
            out.provider = res.get("provider", out.provider or "?")
            if res.get("error"):
                out.quality_flags.append("scout_tool_iteration_provider_error")
            llm_cost = _flash_cost_estimate(out.model_used)
            out.cost_usd += llm_cost
            cost_counter["total"] = cost_counter.get("total", 0.0) + llm_cost
            next_parsed = _extract_first_json(text)
            if isinstance(next_parsed, dict):
                parsed = next_parsed
            else:
                out.quality_flags.append("scout_tool_iteration_json_unparseable_kept_previous")
                break
    if isinstance(parsed, dict) and deferred_tool_requests:
        final_requests = parsed.get("tool_requests") if isinstance(parsed.get("tool_requests"), list) else []
        parsed["tool_requests"] = [*deferred_tool_requests, *final_requests]
    if not isinstance(parsed, dict) and calendar_seed:
        # Calendar tasks are too important to lose to a cheap-model
        # formatting miss. Use one real fallback-model repair attempt, still
        # with strict no-stub semantics. Critical misses are attributed red;
        # high/medium misses still get the retry but remain yellow if they die.
        try:
            retry = await _chat(
                fallback,
                system_prompt,
                user_prompt + "\n\nYour previous answer was not parseable JSON. "
                "Return only the JSON object requested above.",
                max_tokens=SCOUT_MAX_TOKENS,
                fallback=None,
            )
            retry_text = (retry.get("text") or "").strip()
            retry_parsed = _extract_first_json(retry_text)
            retry_model = retry.get("model_used", fallback)
            retry_cost = _flash_cost_estimate(retry_model)
            out.cost_usd += retry_cost
            cost_counter["total"] = cost_counter.get("total", 0.0) + retry_cost
            if isinstance(retry_parsed, dict):
                parsed = retry_parsed
                out.model_used = retry_model
                out.provider = retry.get("provider", out.provider)
                out.quality_flags.append("calendar_json_retry")
        except Exception as e:
            logger.info("critical calendar scout JSON retry failed: %s", e)
    if not isinstance(parsed, dict) and not calendar_seed and cost_counter.get("total", 0.0) < cost_cap:
        retry_model_name = fallback or model
        try:
            retry = await _chat(
                retry_model_name,
                system_prompt,
                user_prompt + "\n\nYour previous answer was not parseable JSON. "
                "Return only the strict JSON object requested above. No markdown, no prose.",
                max_tokens=SCOUT_MAX_TOKENS,
                fallback=None,
            )
            retry_text = (retry.get("text") or "").strip()
            retry_parsed = _extract_first_json(retry_text)
            retry_model = retry.get("model_used", retry_model_name)
            retry_cost = _flash_cost_estimate(retry_model)
            out.cost_usd += retry_cost
            cost_counter["total"] = cost_counter.get("total", 0.0) + retry_cost
            out.quality_flags.append("scout_json_retry")
            if isinstance(retry_parsed, dict):
                parsed = retry_parsed
                out.model_used = retry_model
                out.provider = retry.get("provider", out.provider)
                out.quality_flags.append("scout_json_retry_success")
            else:
                out.quality_flags.append("scout_json_retry_unparseable")
        except Exception as e:
            out.quality_flags.append(f"scout_json_retry_failed:{type(e).__name__}")
            logger.info("scout %s: JSON retry failed: %s", scout_id, e)
    if not isinstance(parsed, dict):
        out.error = "scout_json_unparseable"
        out.quality_flags.append("scout_json_unparseable")
        if task_id:
            fail_task(task_id, agent_id=scout_id, specialist_id="tier1_scout", reason=out.error)
            attribute_failure(
                artifact_kind="task",
                artifact_id=task_id,
                failure_kind=(
                    "critical_calendar_json_unparseable"
                    if critical_calendar else "json_unparseable"
                ),
                cycle_id=cycle_id,
                task_id=task_id,
                specialist_id="tier1_scout",
                severity="red" if critical_calendar else "yellow",
                rationale="Scout returned unparseable JSON.",
            )
        out.elapsed_s = time.perf_counter() - t0
        return out
    prompt_quality = score_deep_scout_output(
        parsed,
        allowed_tools=_prompt_tool_candidates(seed, tool_evidence),
        allowed_evidence_refs=_allowed_evidence_refs(tool_evidence),
    )
    out.quality_flags.append(f"prompt_quality:{prompt_quality.score:.2f}")
    out.quality_flags.extend(f"prompt_{flag}" for flag in prompt_quality.flags[:4])
    out.hypothesis_text = (parsed.get("hypothesis") or "").strip()[:280]
    try:
        out.confidence = float(parsed.get("confidence") or 0.0)
    except Exception:
        out.confidence = 0.0
    out.confidence = max(0.0, min(1.0, out.confidence))
    out.rationale_brief = (parsed.get("rationale_brief") or "").strip()[:200]
    sugg = parsed.get("suggested_tools") or []
    if isinstance(sugg, list):
        allowed_suggestions = set(_prompt_tool_candidates(seed, tool_evidence))
        filtered = [
            str(x)
            for x in sugg
            if isinstance(x, str) and str(x) in allowed_suggestions
        ]
        if len(filtered) != len([x for x in sugg if isinstance(x, str)]):
            out.quality_flags.append("suggested_tools_filtered")
        out.suggested_tools = filtered[:6]
    out.tool_requests = _normalize_tool_requests(
        parsed.get("tool_requests"),
        allowed_tools=_prompt_tool_candidates(seed, tool_evidence),
    )
    out.tool_requests = _filter_fulfilled_tool_requests(
        out.tool_requests,
        tool_evidence=out.tool_evidence,
    )
    if out.tool_requests:
        try:
            request_proposals = _analysis_tool_proposals_from_requests(
                out.tool_requests,
                cycle_id=cycle_id,
                artifact_kind="scout_output",
                artifact_id=scout_id,
                seed=seed,
            )
            out.tool_proposal_ids.extend(persist_analysis_tool_proposals(request_proposals))
            out.quality_flags.append("scout_tool_requests_persisted")
        except Exception as e:
            out.quality_flags.append("scout_tool_requests_persist_failed")
            logger.debug("scout %s: tool request persist failed: %s", scout_id, e)
    raw_strings = parsed.get("information_strings") or []
    if isinstance(raw_strings, list):
        for raw in raw_strings[:3]:
            info = normalize_information_string(raw)
            if info is not None:
                out.information_strings.append(info)
    event_bundles = _event_bundles_for_scout(
        parsed=parsed,
        seed=seed,
        cycle_id=cycle_id,
        tool_evidence=out.tool_evidence,
    )
    for bundle in event_bundles:
        try:
            event_id = persist_event_intelligence(bundle)
            out.event_intelligence_ids.append(event_id)
            info = event_intelligence_to_information_string(bundle)
            if event_id not in info.evidence_refs:
                info.evidence_refs.append(event_id)
            out.information_strings.append(info)
            out.tool_proposal_ids.extend(_persist_tool_proposals_for_flags(
                cycle_id=cycle_id,
                artifact_kind="market_event_intelligence",
                artifact_id=event_id,
                seed=seed,
                quality_flags=bundle.quality_flags,
            ))
        except Exception as e:
            out.quality_flags.append("event_intelligence_persist_failed")
            logger.warning("scout %s: event intelligence persist failed: %s", scout_id, e)
    node_snapshots = _node_snapshots_for_scout(
        parsed=parsed,
        seed=seed,
        cycle_id=cycle_id,
        tool_evidence=out.tool_evidence,
    )
    for snapshot in node_snapshots:
        try:
            node_id = persist_node_intelligence(snapshot)
            out.node_intelligence_ids.append(node_id)
            node_quality = score_node_intelligence(snapshot)
            if node_quality.passed:
                info = node_intelligence_to_information_string(snapshot)
                if node_id not in info.evidence_refs:
                    info.evidence_refs.append(node_id)
                out.information_strings.append(info)
            else:
                out.quality_flags.append("node_intelligence_not_promoted")
            node_proposals = [
                _analysis_tool_proposal_from_node_dict(
                    raw,
                    cycle_id=cycle_id,
                    artifact_kind="node_intelligence",
                    artifact_id=node_id,
                    seed=seed,
                )
                for raw in (snapshot.coverage.get("tool_proposals") or [])
                if isinstance(raw, dict)
            ]
            generic_proposals = propose_tools_from_quality_flags(
                cycle_id=cycle_id,
                artifact_kind="node_intelligence",
                artifact_id=node_id,
                entity=seed.entity,
                horizon=seed.horizon,
                lens=seed.lens,
                quality_flags=snapshot.quality_flags,
            )
            out.tool_proposal_ids.extend(persist_analysis_tool_proposals(
                [*node_proposals, *generic_proposals]
            ))
        except Exception as e:
            out.quality_flags.append("node_intelligence_persist_failed")
            logger.warning("scout %s: node intelligence persist failed: %s", scout_id, e)
    try:
        substrate_info = data_substrate_to_information_string(
            cycle_id=cycle_id,
            scout_id=scout_id,
            entity=seed.entity,
            horizon=seed.horizon,
            lens=seed.lens,
            tool_evidence=out.tool_evidence,
            allowed_tools=_prompt_tool_candidates(seed, tool_evidence),
        )
        if substrate_info is not None:
            out.information_strings.append(substrate_info)
            out.quality_flags.append("data_substrate_map_string_emitted")
    except Exception as e:
        out.quality_flags.append("data_substrate_map_string_failed")
        logger.debug("scout %s: data substrate map string failed: %s", scout_id, e)
    out.tool_proposal_ids.extend(_persist_tool_proposals_for_flags(
        cycle_id=cycle_id,
        artifact_kind="scout_output",
        artifact_id=scout_id,
        seed=seed,
        quality_flags=out.quality_flags,
    ))
    if out.information_strings:
        reviewed: list[InformationString] = []
        for info in out.information_strings:
            review = review_information_string(
                info,
                tool_evidence=out.tool_evidence,
                stage="post_scout",
            )
            out.quality_flags.append(f"adversarial_review:{review.decision}")
            if review.decision == "reject":
                out.quality_flags.append("adversarial_rejected_string")
                continue
            reviewed.append(apply_adversarial_review(info, review))
        out.information_strings = reviewed
    route_alignment = _evaluate_route_contract_alignment(
        seed=seed,
        parsed=parsed,
        information_strings=out.information_strings,
        tool_requests=out.tool_requests,
    )
    if "route_contract_not_applicable" not in route_alignment.flags:
        out.quality_flags.append(f"route_contract_alignment:{route_alignment.score:.2f}")
        out.quality_flags.extend(route_alignment.flags[:6])
        out.quality_flags.append(
            "route_contract_satisfied"
            if route_alignment.passed else "route_contract_failed"
        )
        for edge in route_alignment.addressed_edges[:3]:
            out.quality_flags.append(f"route_contract_edge_addressed:{_route_edge_slug(edge)}")
        for edge in route_alignment.missed_edges[:3]:
            out.quality_flags.append(f"route_contract_edge_missed:{_route_edge_slug(edge)}")
        for info in out.information_strings:
            info.quality_flags = sorted(set([
                *info.quality_flags,
                "route_contract_satisfied" if route_alignment.passed else "route_contract_failed",
                f"route_contract_alignment:{route_alignment.score:.2f}",
            ]))
    if not out.information_strings and out.hypothesis_text:
        # Do not fabricate graph memory from a scout that failed to emit a
        # valid information string. The hypothesis row remains inspectable,
        # but the persistent information map only stores real strings.
        out.quality_flags.append("no_information_strings_persisted")
    if out.information_strings:
        try:
            out.information_string_ids = persist_information_strings(
                cycle_id=out.cycle_id,
                scout_id=out.scout_id,
                seed_id=out.seed_id,
                entity=out.entity,
                theme=seed.theme,
                horizon=out.horizon,
                lens=out.lens,
                bias_mode=out.bias_mode,
                strings=out.information_strings,
                coverage_cell_key=seed.payload.get("coverage_cell_key"),
                source_tool_call_ids=_successful_tool_call_ids(out.tool_evidence),
                model_used=out.model_used,
                provider=out.provider,
                cost_usd=out.cost_usd,
            )
        except Exception as e:
            out.quality_flags.append("information_string_persist_failed")
            logger.warning("scout %s: information string persist failed: %s", scout_id, e)
    if not out.hypothesis_text:
        out.quality_flags.append("scout_empty_hypothesis")
        if task_id:
            fail_task(task_id, agent_id=scout_id, specialist_id="tier1_scout", reason="empty_hypothesis")
            attribute_failure(
                artifact_kind="task",
                artifact_id=task_id,
                failure_kind=(
                    "critical_calendar_empty_hypothesis"
                    if critical_calendar else "empty_hypothesis"
                ),
                cycle_id=cycle_id,
                task_id=task_id,
                specialist_id="tier1_scout",
                severity="red" if critical_calendar else "yellow",
                rationale="Scout produced no falsifiable hypothesis.",
            )

    # Persist hypothesis row + topic message — these are best-effort.
    try:
        out.hypothesis_id = _persist_hypothesis(out, seed)
    except Exception as e:
        logger.warning("scout %s: hypothesis persist failed: %s", scout_id, e)
    try:
        _post_scout_topic_message(out)
    except Exception as e:
        logger.warning("scout %s: topic post failed: %s", scout_id, e)
    try:
        record_coverage(cycle_id, seed, scout_id, published=False)
    except Exception as e:
        logger.debug("scout %s: coverage record failed: %s", scout_id, e)

    if out.hypothesis_text and task_id:
        append_blackboard_event(
            event_type="claim.proposed",
            cycle_id=cycle_id,
            topic=f"bb_topic:scout_output:{out.lens}:{out.entity}",
            task_id=task_id,
            claim_id=out.hypothesis_id or out.scout_id,
            agent_id=scout_id,
            specialist_id="tier1_scout",
            payload={
                "hypothesis_id": out.hypothesis_id,
                "hypothesis": out.hypothesis_text,
                "confidence": out.confidence,
                "suggested_tools": out.suggested_tools,
                "tool_requests": out.tool_requests,
                "prompt_variant": out.prompt_variant,
                "tool_evidence": out.tool_evidence,
                "information_string_ids": out.information_string_ids,
                "event_intelligence_ids": out.event_intelligence_ids,
                "node_intelligence_ids": out.node_intelligence_ids,
                "tool_proposal_ids": out.tool_proposal_ids,
                "calendar_trigger": out.calendar_trigger,
                "calendar_severity": out.calendar_severity,
            },
        )
        complete_task(
            task_id,
            agent_id=scout_id,
            specialist_id="tier1_scout",
            payload={"hypothesis_id": out.hypothesis_id, "scout_id": scout_id},
        )

    out.elapsed_s = time.perf_counter() - t0
    return out


def _extract_first_json(text: str) -> Any:
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):
        lines = s.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        s = "\n".join(lines).strip()
    start = s.find("{")
    if start < 0:
        return None
    decoder = json.JSONDecoder()
    try:
        obj, _ = decoder.raw_decode(s[start:])
        return obj
    except Exception:
        pass
    # Last-resort balanced scan that respects quoted strings.
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(s)):
        ch = s[i]
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                blob = s[start:i + 1]
                try:
                    return json.loads(blob)
                except Exception:
                    return None
    return None


def _flash_cost_estimate(model: str) -> float:
    """Per-call cost in USD using the model's avg $/Mtok. Coarse but
    enough for kill-switch accounting."""
    rates = {
        "deepseek:v4-flash": 0.6,
        "deepseek:v4-pro": 1.2,
        "anthropic:claude-haiku-4-5": 1.2,
        "anthropic:claude-sonnet-4-6": 6.0,
        "openai:gpt-4o": 8.0,
    }
    rate = rates.get(model, 1.0)  # $/Mtok
    # ~ (in+out tokens) average. Flash scout = ~800 tokens roundtrip.
    return rate * 800 / 1_000_000


def _persist_hypothesis(out: ScoutOutput, seed: SeedCell) -> Optional[str]:
    """Insert one hypothesis row + return hypothesis_id.

    Matches the SOTA hypotheses schema: (id, cycle_id, specialist_id,
    title, hypothesis_text, status, posterior_prob, heat_score,
    novelty_score, entity_ids, source_ids, claim_ids, tool_call_ids,
    valid_from, transaction_from, payload).
    """
    if not out.hypothesis_text:
        return None
    conn = get_desk_store().conn
    cols = {r[1] for r in conn.execute("PRAGMA table_info(hypotheses)")}
    if not cols:
        return None
    hyp_id = f"hyp_{uuid4().hex[:10]}"
    now = datetime.now(timezone.utc).isoformat()
    # Build a tolerant insert — match SOTA schema first, fall back to
    # legacy column names if missing.
    title = out.hypothesis_text[:140]
    fields: dict[str, Any] = {
        "id": hyp_id,
        "specialist_id": "tier1_scout",
        "cycle_id": out.cycle_id,
        "title": title,
        "hypothesis_text": out.hypothesis_text,
        "text": out.hypothesis_text,  # legacy
        "instrument": out.entity,  # legacy
        "horizon": out.horizon,  # legacy
        "status": "active",
        "posterior_prob": out.confidence,
        "prior": out.confidence,  # legacy
        "posterior": out.confidence,  # legacy
        "heat_score": out.confidence,
        "novelty_score": 0.5,
        "entity_ids": json.dumps([out.entity]),
        "source_ids": json.dumps([]),
        "claim_ids": json.dumps([]),
        "tool_call_ids": json.dumps([]),
        "valid_from": now,
        "transaction_from": now,
        "payload": json.dumps({
            "scout_id": out.scout_id,
            "task_id": out.task_id,
            "seed_id": out.seed_id,
            "entity": out.entity,
            "lens": out.lens,
            "bias_mode": out.bias_mode,
            "horizon": out.horizon,
            "rationale_brief": out.rationale_brief,
            "suggested_tools": out.suggested_tools,
            "tool_evidence": out.tool_evidence,
            "information_string_ids": out.information_string_ids,
            "event_intelligence_ids": out.event_intelligence_ids,
            "node_intelligence_ids": out.node_intelligence_ids,
            "tool_proposal_ids": out.tool_proposal_ids,
            "calendar_trigger": out.calendar_trigger,
            "calendar_severity": out.calendar_severity,
            "prompt_variant": out.prompt_variant,
            "model_used": out.model_used,
            "provider": out.provider,
            "quality_flags": out.quality_flags,
            "tier": "scout",
        }),
    }
    insertable = {k: v for k, v in fields.items() if k in cols}
    if "id" not in insertable:
        return None
    placeholders = ",".join("?" * len(insertable))
    col_names = ",".join(insertable.keys())
    try:
        conn.execute(
            f"INSERT INTO hypotheses ({col_names}) VALUES ({placeholders})",
            tuple(insertable.values()),
        )
        conn.commit()
    except sqlite3.OperationalError as e:
        logger.debug("hypothesis insert error: %s", e)
        return None
    return hyp_id


def _post_scout_topic_message(out: ScoutOutput) -> None:
    topic = f"bb_topic:scout_output:{out.lens}:{out.entity}"
    post_message(
        from_agent=f"scout:{out.scout_id}",
        to_agent_or_topic=topic,
        kind="observation",
        payload={
            "scout_id": out.scout_id,
            "seed_id": out.seed_id,
            "task_id": out.task_id,
            "cycle_id": out.cycle_id,
            "hypothesis_id": out.hypothesis_id,
            "hypothesis": out.hypothesis_text,
            "confidence": out.confidence,
            "entity": out.entity,
            "horizon": out.horizon,
            "lens": out.lens,
            "bias_mode": out.bias_mode,
            "rationale_brief": out.rationale_brief,
            "suggested_tools": out.suggested_tools,
            "tool_evidence": out.tool_evidence,
            "information_string_ids": out.information_string_ids,
            "event_intelligence_ids": out.event_intelligence_ids,
            "node_intelligence_ids": out.node_intelligence_ids,
            "tool_proposal_ids": out.tool_proposal_ids,
            "calendar_trigger": out.calendar_trigger,
            "calendar_severity": out.calendar_severity,
            "prompt_variant": out.prompt_variant,
            "model_used": out.model_used,
            "provider": out.provider,
            "quality_flags": out.quality_flags,
        },
        related_hypothesis_id=out.hypothesis_id,
        expires_in_hours=72,
        topic=topic,
    )


# ----------------------------------------------------------------------
# Public entry point
# ----------------------------------------------------------------------

async def _async_run_scouts(
    seeds: list[SeedCell],
    cycle_id: str,
    model: str,
    fallback: str,
    concurrency: int,
    cost_cap: float,
) -> list[ScoutOutput]:
    sem = asyncio.Semaphore(max(1, concurrency))
    cost_counter: dict[str, float] = {"total": 0.0}

    async def _bounded(seed: SeedCell) -> ScoutOutput:
        async with sem:
            return await _run_one_scout(
                seed, cycle_id, model, fallback,
                cost_counter, cost_cap,
            )

    tasks = [asyncio.create_task(_bounded(s)) for s in seeds]
    return await asyncio.gather(*tasks, return_exceptions=False)


def run_scouts(
    seeds: list[SeedCell],
    cycle_id: str,
    model: str = DEFAULT_SCOUT_MODEL,
    fallback: str = DEFAULT_SCOUT_FALLBACK,
    concurrency: int = DEFAULT_CONCURRENCY,
    cost_cap_usd: float = DEFAULT_COST_CAP_USD,
) -> list[ScoutOutput]:
    """Synchronous entry point — runs the scout swarm to completion.

    Handles being called from inside an event loop by trampolining to a
    worker-thread `asyncio.run`. Returns one ScoutOutput per seed.
    """
    if not seeds:
        return []

    async def _go() -> list[ScoutOutput]:
        return await _async_run_scouts(
            seeds, cycle_id, model, fallback, concurrency, cost_cap_usd,
        )

    try:
        asyncio.get_running_loop()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            return ex.submit(asyncio.run, _go()).result()
    except RuntimeError:
        return asyncio.run(_go())

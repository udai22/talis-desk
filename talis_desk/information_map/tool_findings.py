"""Map arbitrary tool/source receipts into durable information strings.

The scout layer is allowed to use many tools and sources, but the output only
matters if those receipts enter the information map. This module turns a
tool-evidence packet into a compact causal string: what surfaces were touched,
which typed edges appeared, which gaps remain, and which source refs support
the claim.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from .data_substrate import summarize_data_substrate
from .store import InformationString


def data_substrate_to_information_string(
    *,
    cycle_id: str,
    scout_id: str,
    entity: str,
    horizon: str,
    lens: str,
    tool_evidence: Iterable[dict[str, Any]],
    allowed_tools: Iterable[str] = (),
) -> Optional[InformationString]:
    """Create one map-ready string from all successful tool/source receipts.

    This is intentionally one compact string per scout rather than one string
    per receipt. It lets the map remember coverage and missing edges without
    flooding synthesis with raw logs.
    """
    evidence = [
        item for item in tool_evidence
        if isinstance(item, dict) and item.get("ok")
    ]
    if not evidence:
        return None
    summary = summarize_data_substrate(
        evidence,
        allowed_tools=allowed_tools,
        entity=entity,
        horizon=horizon,
        lens=lens,
    )
    touched = [row for row in summary.touched if row.touched]
    if not touched:
        return None
    refs = _evidence_refs(evidence)
    surfaces = [row.surface.title for row in touched]
    surface_keys = [row.surface.key for row in touched]
    edge_text = "; ".join(summary.connection_edges[:5]) or "no cross-surface edge yet"
    expansion_text = "; ".join(
        f"{exp.title} -> {exp.expected_edge}"
        for exp in summary.expansions[:4]
    ) or "no high-priority expansion gap"
    observed_at = datetime.now(timezone.utc).isoformat()
    surface_phrase = ", ".join(surfaces[:5])
    title_entity = entity or "Market cell"
    return InformationString(
        title=f"{title_entity} tool/source map coverage",
        thesis=(
            f"{title_entity} scout evidence touched {len(touched)}/"
            f"{summary.total_surfaces} data surfaces ({surface_phrase}) and "
            f"formed typed map edges: {edge_text}."
        )[:2000],
        mechanism=(
            "Approved tool/source receipts are converted into a durable map "
            "string so synthesis can compare coverage, verifier agents can "
            "attack the source-backed edges, and tool builders can target the "
            f"missing edges: {expansion_text}."
        )[:2000],
        expected_outcome=(
            "Attention should prioritize claims whose evidence spans multiple "
            "surfaces and dispatch next calls against the missing edge list "
            "instead of spending randomly."
        ),
        time_horizon=horizon,
        time_scale=horizon,
        observed_at=observed_at,
        source_time_basis="tool_call_time",
        kill_signal=(
            "The map coverage string should be downgraded if receipts are "
            "stale, source health fails, or expansion calls contradict the "
            "formed edge."
        ),
        extends_or_contradicts="extends",
        would_change_decision=True,
        crowdedness=0.35,
        conviction=_coverage_conviction(len(touched), len(summary.connection_edges)),
        novelty_score=0.58 if summary.expansions else 0.46,
        entities_chain=[x for x in [entity, *surface_keys, "information_map"] if x],
        depth_layers=[
            {
                "layer": 1,
                "claim": f"Receipts touched surfaces: {', '.join(surface_keys)}.",
            },
            {
                "layer": 2,
                "claim": f"Typed edges formed: {edge_text}.",
            },
            {
                "layer": 3,
                "claim": f"Missing edges route next tool calls: {expansion_text}.",
            },
            {
                "layer": 4,
                "claim": "The information map stores the receipts, nodes, and causal chain for synthesis/verifier routing.",
            },
        ],
        evidence_refs=refs,
        temporal_confidence=0.72,
        quality_flags=[
            "from_tool_source_expansion",
            f"data_surface_coverage:{len(touched)}/{summary.total_surfaces}",
            f"cycle:{cycle_id}",
            f"scout:{scout_id}",
        ],
    )


def _evidence_refs(evidence: Iterable[dict[str, Any]]) -> list[str]:
    refs: list[str] = []
    for item in evidence:
        for key in ("tool_call_log_id", "uri", "source", "id"):
            value = str(item.get(key) or "").strip()
            if value:
                refs.append(value)
    return list(dict.fromkeys(refs))


def _coverage_conviction(n_surfaces: int, n_edges: int) -> float:
    score = 0.45 + min(0.24, 0.06 * n_surfaces) + min(0.16, 0.04 * n_edges)
    return round(min(0.85, score), 3)

"""Search objective and anti-waste rubric for the scout layer.

DeepSeek Flash gives the desk cheap breadth. This module defines what that
breadth is for: finding decision-changing causal strings across the market,
then rejecting shallow or duplicate observations before they consume higher
tier attention.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
from statistics import median
from typing import Any


ACCEPTANCE_THRESHOLD = 0.72
SCALE_GATE_MIN_MEDIAN_SCORE = 0.74
SCALE_GATE_MIN_PASS_RATE = 0.70
SCALE_GATE_MAX_INVENTED_TOOL_RATE = 0.05


MARKET_SEARCH_OBJECTIVE = (
    "Find decision-changing information strings: causal chains where new or "
    "underweighted information can move an asset, theme, flow cohort, or "
    "cross-asset relationship before the market fully reprices it. A useful "
    "string is not a headline summary. It links entity -> mechanism -> second "
    "order implication -> observable outcome -> kill signal, with freshness "
    "and evidence."
)


WHAT_WE_ARE_LOOKING_FOR = [
    "A concrete causal mechanism, not a label like 'AI beneficiary' or 'risk-on'.",
    "A non-obvious second or third order link across assets, cohorts, suppliers, flows, or policy.",
    "A time-bounded observable outcome that a verifier can check.",
    "A kill signal that would invalidate the chain quickly.",
    "Evidence references from tools, filings, events, source library, order-flow state, or prior strings.",
    "Fresh event-time discipline: the source date and stale point are explicit and compatible with the horizon.",
    "A crowding/novelty judgment: is this already obvious, or still under-mapped?",
    "Continuity with memory: new, extends, contradicts, or abandons an existing trail.",
]

DEPTH_LADDER = [
    "Layer 1: What changed? Identify the new information, flow, price behavior, filing, calendar event, or anomaly.",
    "Layer 2: Who is directly affected? Tie the change to one asset, cohort, balance sheet, wallet group, sector, or curve point.",
    "Layer 3: Who is indirectly affected? Trace supplier/customer/substitution/forced-flow/positioning consequences.",
    "Layer 4: What is reflexive? Explain how price action, volatility, leverage, dealer hedging, social attention, or liquidity can amplify or negate it.",
    "Layer 5: What kills it? State the earliest observable signal that the chain is wrong or already priced.",
]

SEAM_TYPES = [
    "asset-class seam: equity <-> rates <-> FX <-> commodities <-> crypto",
    "participant seam: retail <-> hedge funds <-> dealers <-> corporates <-> sovereigns <-> on-chain cohorts",
    "time-horizon seam: intraday flow <-> 1w catalyst <-> 1q fundamentals <-> structural theme",
    "data-source seam: filing/headline <-> market microstructure <-> options <-> source-library prior",
    "attention seam: what Twitter/news now sees vs what the data already implied earlier",
]


REJECT_IF = [
    "It only restates news without a mechanism.",
    "It would not change a watchlist, verification task, or position decision.",
    "It lacks a falsifiable expected outcome or kill signal.",
    "It invents tools, citations, prices, filings, or data it did not see.",
    "It duplicates an existing string without extending or contradicting it.",
    "It is generic macro commentary that cannot be attached to entities and horizons.",
    "It uses stale historical evidence as if it were a live catalyst.",
]

INHIBITIONS = [
    "Do not summarize a headline; resolve the causal mechanism or return no string.",
    "Do not emit a single-asset take if the better alpha is at a seam between assets, horizons, participants, or data sources.",
    "Do not treat absence of evidence as evidence; mark low conviction or return no string.",
    "Do not invent tools, citations, prices, filings, wallet behavior, or consensus data.",
    "Do not pass output until every strong string has outcome, kill signal, freshness, and evidence refs.",
    "Do not use old dates as live evidence. Historical facts are allowed only when labeled as regime context.",
]


@dataclass
class StringRubricResult:
    score: float
    flags: list[str] = field(default_factory=list)
    components: dict[str, float] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        blockers = {
            "missing_thesis",
            "missing_mechanism",
            "missing_expected_outcome",
            "missing_kill_signal",
            "missing_time_horizon",
            "missing_entities_chain",
            "missing_depth_layers",
            "missing_evidence_refs",
            "would_not_change_decision",
        }
        return self.score >= ACCEPTANCE_THRESHOLD and not blockers.intersection(self.flags)


@dataclass
class PromptScaleGate:
    median_score: float
    pass_rate: float
    invented_tool_rate: float
    n: int
    passed: bool
    flags: list[str] = field(default_factory=list)


def format_search_objective_for_prompt() -> str:
    """Compact prompt block used by all scout variants."""
    lines = [
        "What we are looking for:",
        MARKET_SEARCH_OBJECTIVE,
        "",
        "Accept strings that have:",
    ]
    lines.extend(f"- {x}" for x in WHAT_WE_ARE_LOOKING_FOR)
    lines.extend(["", "Depth ladder for every strong string:"])
    lines.extend(f"- {x}" for x in DEPTH_LADDER)
    lines.extend(["", "The desk beats human specialization by hunting seams:"])
    lines.extend(f"- {x}" for x in SEAM_TYPES)
    lines.extend(["", "Reject strings if:"])
    lines.extend(f"- {x}" for x in REJECT_IF)
    lines.extend(["", "Inhibitions:"])
    lines.extend(f"- {x}" for x in INHIBITIONS)
    return "\n".join(lines)


def score_information_string(raw: Any) -> StringRubricResult:
    """Score one raw information string against the desk's search objective."""
    if not isinstance(raw, dict):
        return StringRubricResult(score=0.0, flags=["string_not_object"])

    flags: list[str] = []
    components: dict[str, float] = {}

    thesis = _text(raw.get("thesis") or raw.get("hypothesis"))
    mechanism = _text(raw.get("mechanism"))
    expected = _text(raw.get("expected_outcome"))
    kill = _text(raw.get("kill_signal"))
    horizon = _text(raw.get("time_horizon") or raw.get("horizon"))
    expires = _text(raw.get("expires_at"))
    relation = _text(raw.get("extends_or_contradicts")).lower()
    entities = raw.get("entities_chain") or raw.get("entities") or []
    depth = raw.get("depth_layers") or []
    evidence = raw.get("evidence_refs") or raw.get("citations") or []
    time_scale = _text(raw.get("time_scale"))
    event_start = _text(raw.get("event_time_start"))
    event_end = _text(raw.get("event_time_end"))
    observed_at = _text(raw.get("observed_at"))
    source_time_basis = _text(raw.get("source_time_basis"))

    if not thesis:
        flags.append("missing_thesis")
    if not mechanism:
        flags.append("missing_mechanism")
    if not expected:
        flags.append("missing_expected_outcome")
    if not kill:
        flags.append("missing_kill_signal")
    if not horizon:
        flags.append("missing_time_horizon")
    if not expires:
        flags.append("missing_expires_at")
    if relation not in {"new", "extends", "contradicts", "abandons"}:
        flags.append("bad_relation")
    if not isinstance(entities, list) or len([e for e in entities if _text(e)]) < 1:
        flags.append("missing_entities_chain")
    if not isinstance(depth, list) or len(depth) < 1:
        flags.append("missing_depth_layers")
    if not isinstance(evidence, list) or len([e for e in evidence if _text(e)]) < 1:
        flags.append("missing_evidence_refs")
    if not time_scale or not source_time_basis:
        flags.append("missing_temporal_metadata")

    would_change = raw.get("would_change_decision", True)
    if would_change is False:
        flags.append("would_not_change_decision")

    conviction = _clamp01(raw.get("conviction"), default=0.0)
    novelty = _clamp01(raw.get("novelty_score"), default=0.0)
    crowdedness = _clamp01(raw.get("crowdedness"), default=0.5)
    if novelty < 0.25 and crowdedness > 0.75:
        flags.append("obvious_and_crowded")

    stale_flags = _freshness_flags(
        horizon=horizon,
        expires_at=expires,
        event_start=event_start,
        event_end=event_end,
        observed_at=observed_at,
        text_blob=" ".join([thesis, mechanism, expected, kill]),
    )
    flags.extend(stale_flags)

    mechanism_score = 1.0 if mechanism and len(mechanism.split()) >= 5 else 0.35 if mechanism else 0.0
    falsifiability_score = (
        (0.35 if expected else 0.0)
        + (0.35 if kill else 0.0)
        + (0.20 if horizon else 0.0)
        + (0.10 if expires else 0.0)
    )
    graph_score = min(1.0, 0.25 * max(0, len(entities)) + 0.20 * max(0, len(depth)))
    depth_score = min(1.0, len(depth) / 5.0)
    evidence_score = min(1.0, 0.55 * max(0, len(evidence)))
    temporal_score = 1.0
    if "missing_temporal_metadata" in flags:
        temporal_score -= 0.30
    if "stale_date_reference" in flags:
        temporal_score -= 0.45
    if "stale_expiry" in flags:
        temporal_score -= 0.35
    temporal_score = max(0.0, temporal_score)
    novelty_score = max(0.0, min(1.0, novelty * 0.7 + (1.0 - crowdedness) * 0.3))
    decision_score = 1.0 if would_change is not False else 0.0

    components.update({
        "mechanism": mechanism_score,
        "falsifiability": falsifiability_score,
        "graph": graph_score,
        "depth": depth_score,
        "evidence": evidence_score,
        "temporal": temporal_score,
        "novelty": novelty_score,
        "conviction": conviction,
        "decision_value": decision_score,
    })
    score = (
        0.16 * mechanism_score
        + 0.18 * falsifiability_score
        + 0.10 * graph_score
        + 0.08 * depth_score
        + 0.13 * evidence_score
        + 0.12 * temporal_score
        + 0.10 * novelty_score
        + 0.08 * conviction
        + 0.05 * decision_score
    )
    return StringRubricResult(
        score=round(min(1.0, score), 3),
        flags=sorted(set(flags)),
        components={k: round(v, 3) for k, v in components.items()},
    )


def evaluate_prompt_scale_gate(evaluations: list[dict[str, Any]]) -> PromptScaleGate:
    """Decide whether a prompt variant is good enough to scale up."""
    if not evaluations:
        return PromptScaleGate(
            median_score=0.0,
            pass_rate=0.0,
            invented_tool_rate=1.0,
            n=0,
            passed=False,
            flags=["no_evaluations"],
        )
    scores = [float(e.get("score") or 0.0) for e in evaluations]
    passes = [bool(e.get("passed")) for e in evaluations]
    invented = [
        "invented_tool" in set(e.get("flags") or [])
        for e in evaluations
    ]
    median_score = float(median(scores))
    pass_rate = sum(passes) / len(passes)
    invented_rate = sum(invented) / len(invented)
    flags: list[str] = []
    if median_score < SCALE_GATE_MIN_MEDIAN_SCORE:
        flags.append("median_score_too_low")
    if pass_rate < SCALE_GATE_MIN_PASS_RATE:
        flags.append("pass_rate_too_low")
    if invented_rate > SCALE_GATE_MAX_INVENTED_TOOL_RATE:
        flags.append("invented_tool_rate_too_high")
    return PromptScaleGate(
        median_score=round(median_score, 3),
        pass_rate=round(pass_rate, 3),
        invented_tool_rate=round(invented_rate, 3),
        n=len(evaluations),
        passed=not flags,
        flags=flags,
    )


def _text(raw: Any) -> str:
    return str(raw or "").strip()


def _freshness_flags(
    *,
    horizon: str,
    expires_at: str,
    event_start: str,
    event_end: str,
    observed_at: str,
    text_blob: str,
) -> list[str]:
    flags: list[str] = []
    now = datetime.now(timezone.utc)
    horizon_l = horizon.lower()
    short_horizon = horizon_l in {
        "tick", "second", "minute", "hour", "intraday", "1d", "1w", "one day", "one week"
    }
    expiry_dt = _parse_isoish(expires_at)
    if expiry_dt is not None and expiry_dt < now:
        flags.append("stale_expiry")
    event_dt = _parse_isoish(event_end or event_start or observed_at)
    if short_horizon and event_dt is not None and (now - event_dt).days > 45:
        flags.append("stale_date_reference")

    # Cheap models often smuggle stale catalysts into text while leaving event
    # fields blank. Penalize explicit old years on short-horizon strings unless
    # the text labels the date as historical/regime context.
    years = {int(y) for y in re.findall(r"\b(20[0-9]{2})\b", text_blob or "")}
    context_words = {"historical", "regime", "baseline", "prior cycle", "backtest"}
    is_context = any(word in (text_blob or "").lower() for word in context_words)
    if short_horizon and not is_context:
        old_years = [y for y in years if y < now.year]
        if old_years:
            flags.append("stale_date_reference")
    return sorted(set(flags))


def _parse_isoish(value: str) -> datetime | None:
    if not value:
        return None
    raw = value.strip()
    if not raw or raw.lower() in {"unknown", "na", "n/a"}:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _clamp01(raw: Any, *, default: float) -> float:
    try:
        value = float(raw)
    except Exception:
        value = default
    return max(0.0, min(1.0, value))

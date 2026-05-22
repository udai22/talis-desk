"""Adversarial calibration for information-map artifacts.

This is not a philosophical critic. It is a small hard gate that prevents the
map from becoming a beautiful display of model self-confidence. The gate checks
whether a string's evidence is alive, whether the causal contract is complete,
and how independent the supporting source families are before graph scores are
allowed to compound.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .store import InformationString


@dataclass
class AdversarialReview:
    decision: str
    calibrated_scores: dict[str, float] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)
    required_actions: list[dict[str, Any]] = field(default_factory=list)
    rationale: str = ""


def review_information_string(
    info: InformationString,
    *,
    tool_evidence: list[dict[str, Any]],
    stage: str = "post_scout",
) -> AdversarialReview:
    """Review one string before it contaminates graph strength.

    Decisions are intentionally conservative:
    - `allow`: complete contract and at least some source-backed support.
    - `downgrade`: usable memory, but model scores are too confident.
    - `quarantine`: keep for audit/context, but do not let it look strong.
    - `reject`: structurally empty only.
    """
    flags: list[str] = []
    actions: list[dict[str, Any]] = []
    if not info.thesis:
        return AdversarialReview(
            decision="reject",
            calibrated_scores=_scores(0.0, 0.0, 0.0, 0.0),
            flags=["empty_thesis"],
            rationale="String has no thesis.",
        )

    ok_refs, failed_refs, source_families = _evidence_sets(tool_evidence)
    refs = [str(x) for x in (info.evidence_refs or []) if str(x).strip()]
    failed_used = sorted(set(refs) & failed_refs)
    if failed_used:
        flags.append("failed_call_as_evidence")
        for ref in failed_used:
            actions.append({"kind": "strip_evidence_ref", "ref": ref})

    supported_refs = sorted(set(refs) & ok_refs)
    if refs and not supported_refs and ok_refs:
        flags.append("no_supported_tool_refs")
    if not refs:
        flags.append("missing_evidence_refs")
    if not info.mechanism:
        flags.append("missing_mechanism")
    if not info.expected_outcome:
        flags.append("missing_expected_outcome")
    if not info.kill_signal:
        flags.append("missing_kill_signal")
    if not info.depth_layers:
        flags.append("missing_depth_layers")

    supporting_families = _families_for_refs(tool_evidence, supported_refs)
    family_count = len(supporting_families or source_families)
    for family in sorted(supporting_families or source_families):
        if family and family != "unknown":
            flags.append(f"source_family:{family}")
    evidence_quality = _evidence_quality(
        refs=refs,
        supported_refs=supported_refs,
        family_count=family_count,
        failed_used=failed_used,
    )
    independence = min(1.0, family_count / 3.0)
    conviction = min(
        float(info.conviction or 0.0),
        0.25 + 0.55 * evidence_quality + 0.15 * independence,
    )
    novelty = min(
        float(info.novelty_score or 0.0),
        0.35 + 0.45 * evidence_quality + 0.12 * independence,
    )
    attention = (
        conviction * 0.45
        + novelty * 0.30
        + (1.0 - float(info.crowdedness or 0.5)) * 0.15
        + min(0.10, 0.04 * len(supported_refs))
    )

    if "failed_call_as_evidence" in flags:
        decision = "downgrade"
    elif evidence_quality < 0.25 or {"missing_mechanism", "missing_kill_signal"} <= set(flags):
        decision = "quarantine"
    elif evidence_quality < 0.55 or family_count < 2:
        decision = "downgrade"
    else:
        decision = "allow"

    flags.append(f"adversarial_decision:{decision}")
    return AdversarialReview(
        decision=decision,
        calibrated_scores=_scores(conviction, novelty, attention, independence, evidence_quality),
        flags=sorted(set(flags)),
        required_actions=actions,
        rationale=(
            f"{stage}: evidence_quality={evidence_quality:.2f}, "
            f"independence={independence:.2f}, supported_refs={len(supported_refs)}."
        ),
    )


def apply_adversarial_review(
    info: InformationString,
    review: AdversarialReview,
) -> InformationString:
    """Mutate an InformationString in-place with calibrated scores/flags."""
    strip_refs = {
        str(action.get("ref"))
        for action in review.required_actions
        if action.get("kind") == "strip_evidence_ref"
    }
    if strip_refs:
        info.evidence_refs = [ref for ref in info.evidence_refs if ref not in strip_refs]
    scores = review.calibrated_scores
    if "conviction" in scores:
        info.conviction = _clamp01(scores["conviction"])
    if "novelty" in scores:
        info.novelty_score = _clamp01(scores["novelty"])
    if review.decision == "quarantine":
        info.would_change_decision = False
        info.conviction = min(info.conviction, 0.35)
        info.novelty_score = min(info.novelty_score, 0.45)
    info.quality_flags = sorted(set([
        *info.quality_flags,
        *review.flags,
        "adversarial_reviewed",
    ]))
    return info


def _evidence_sets(tool_evidence: list[dict[str, Any]]) -> tuple[set[str], set[str], set[str]]:
    ok_refs: set[str] = set()
    failed_refs: set[str] = set()
    families: set[str] = set()
    for item in tool_evidence or []:
        if not isinstance(item, dict):
            continue
        refs = {
            str(item.get("tool_call_log_id") or "").strip(),
            str(item.get("uri") or "").strip(),
            str(item.get("source") or "").strip(),
            str(item.get("id") or "").strip(),
        }
        refs = {ref for ref in refs if ref}
        if item.get("ok"):
            ok_refs.update(refs)
            family = _source_family(item)
            if family:
                families.add(family)
        else:
            failed_refs.update(refs)
    return ok_refs, failed_refs, families


def _families_for_refs(tool_evidence: list[dict[str, Any]], supported_refs: list[str]) -> set[str]:
    wanted = set(supported_refs)
    out: set[str] = set()
    for item in tool_evidence or []:
        refs = {
            str(item.get("tool_call_log_id") or "").strip(),
            str(item.get("uri") or "").strip(),
            str(item.get("source") or "").strip(),
            str(item.get("id") or "").strip(),
        }
        if wanted & refs:
            family = _source_family(item)
            if family:
                out.add(family)
    return out


def _source_family(item: dict[str, Any]) -> str:
    uri = str(item.get("uri") or "").lower()
    result = item.get("result") if isinstance(item.get("result"), dict) else {}
    source = str(result.get("source") or item.get("source") or "").lower()
    text = " ".join([uri, source, str(item.get("summary") or "").lower()])
    if "hydromancer" in text or "wallet" in text or "builder" in text:
        return "hydromancer"
    if "hl_reject" in text or "our_hl_node" in text:
        return "our_hl_node"
    if "l4" in text or "funding" in text or "orderbook" in text or "coinalyze" in text:
        return "market_microstructure"
    if "web_search" in text or "parallel_search" in text or "perplexity" in text:
        return "web"
    if "sec" in text or "filing" in text or "analyst" in text:
        return "fundamentals_filings"
    if "macro" in text or "fred" in text or "treasury" in text or "fomc" in text:
        return "macro_official"
    if "astro" in text or "celestial" in text or "sunspot" in text:
        return "celestial_cycles"
    return "unknown"


def _evidence_quality(
    *,
    refs: list[str],
    supported_refs: list[str],
    family_count: int,
    failed_used: list[str],
) -> float:
    if not refs:
        return 0.10
    score = 0.20
    score += min(0.38, 0.19 * len(supported_refs))
    score += min(0.30, 0.10 * family_count)
    if failed_used:
        score -= 0.25
    return _clamp01(score)


def _scores(
    conviction: float,
    novelty: float,
    attention: float,
    independence: float,
    evidence_quality: float = 0.0,
) -> dict[str, float]:
    return {
        "conviction": round(_clamp01(conviction), 4),
        "novelty": round(_clamp01(novelty), 4),
        "attention": round(_clamp01(attention), 4),
        "independence": round(_clamp01(independence), 4),
        "evidence_quality": round(_clamp01(evidence_quality), 4),
    }


def _clamp01(raw: Any) -> float:
    try:
        value = float(raw)
    except Exception:
        value = 0.0
    return max(0.0, min(1.0, value))


__all__ = [
    "AdversarialReview",
    "apply_adversarial_review",
    "review_information_string",
]

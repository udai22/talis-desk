"""Bridge information-map syntheses back into the swarm pipeline."""
from __future__ import annotations

import hashlib
from typing import Any

from ..information_map import InformationSynthesis, load_promoted_candidates
from .scout_runner import ScoutOutput


def promoted_scouts_from_synthesis(
    synthesis: InformationSynthesis,
    *,
    existing_scouts: list[ScoutOutput],
    max_items: int = 6,
) -> list[ScoutOutput]:
    """Convert synthesis promotions into verifier-compatible scout outputs.

    The verifier and analyst tiers already know how to handle `ScoutOutput`.
    Rather than creating a parallel path, promoted map confluences are wrapped
    as synthetic scout outputs and marked with source string ids.
    """
    existing_text = {_fingerprint(s.hypothesis_text) for s in existing_scouts if s.hypothesis_text}
    raw_candidates = []
    if getattr(synthesis, "candidate_ids", None):
        try:
            raw_candidates = load_promoted_candidates(
                synthesis_id=synthesis.synthesis_id,
                limit=max_items,
            )
        except Exception:
            raw_candidates = []
    if not raw_candidates:
        raw_candidates = synthesis.promoted_hypotheses[: max(0, int(max_items))]

    out: list[ScoutOutput] = []
    for raw in raw_candidates:
        if not isinstance(raw, dict):
            continue
        status = str(raw.get("status") or "queued_verifier")
        if status != "queued_verifier":
            continue
        hypothesis = str(raw.get("hypothesis") or "").strip()[:280]
        if not hypothesis:
            continue
        fp = _fingerprint(hypothesis)
        if fp in existing_text:
            continue
        existing_text.add(fp)
        source_ids = _string_list(raw.get("source_string_ids"))
        candidate_id = str(raw.get("id") or raw.get("candidate_id") or "")
        scout_id = "iscout_" + hashlib.sha256(
            f"{synthesis.synthesis_id}|{candidate_id}|{hypothesis}|{source_ids}".encode()
        ).hexdigest()[:12]
        confidence = _clamp01(raw.get("confidence"), 0.55)
        out.append(ScoutOutput(
            seed_id=synthesis.synthesis_id,
            scout_id=scout_id,
            cycle_id=synthesis.cycle_id,
            entity=str(raw.get("entity") or "UNKNOWN")[:80],
            lens=str(raw.get("lens") or "synthesis")[:80],
            horizon=str(raw.get("horizon") or "1d")[:80],
            bias_mode=str(raw.get("bias_mode") or "frontier")[:80],
            hypothesis_text=hypothesis,
            confidence=confidence,
            rationale_brief=str(raw.get("rationale_brief") or synthesis.summary)[:200],
            suggested_tools=_string_list(raw.get("suggested_tools"))[:6],
            information_string_ids=source_ids,
            hypothesis_id=candidate_id or None,
            model_used=synthesis.model_used,
            provider=synthesis.provider,
            cost_usd=0.0,
            quality_flags=[
                "promoted_from_information_synthesis",
                f"synthesis_id:{synthesis.synthesis_id}",
                *(["queryable_promoted_candidate"] if candidate_id else []),
            ],
        ))
    return out


def _fingerprint(text: str) -> str:
    return " ".join(str(text).lower().split())


def _string_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(x) for x in raw if str(x).strip()]


def _clamp01(raw: Any, default: float) -> float:
    try:
        val = float(raw)
    except Exception:
        val = default
    return max(0.0, min(1.0, val))


__all__ = ["promoted_scouts_from_synthesis"]

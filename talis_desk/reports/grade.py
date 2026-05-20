"""Stage 6 — Grade and score the report.

ONE Sonnet call. Scores the final polished report on 5 dimensions
(each 0..10), plus an overall mean. Reports with `overall_score < 7.0`
get `below_grade_threshold` added to `quality_flags` so the brief
composer can surface them in a "Reports flagged for review" section.

# Why grading happens AFTER polish

Polish is purely cosmetic. Grading judges the substance + the prose at
their final state — which is what the human reader actually sees.

# No stubs

If every provider in the chain fails, returns a `ReportGrade` with
`overall_score=0.0`, `model_used=''`, and a quality flag `grade_unavailable`.
The pipeline records this and continues — the report still ships, but
the brief flags it.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from .._tic_config import ensure_tic_on_path as _ensure_tic_on_path
from .comparables import ComparablesPack
from .dossier import EvidenceDossier


logger = logging.getLogger(__name__)


_GRADE_FALLBACK_CHAIN: list[str] = [
    "anthropic:claude-sonnet-4-6",
    "anthropic:claude-opus-4-7",
    "openai:gpt-5.5",
    "xai:grok-4",
    "deepseek:v4-pro",
    "anthropic:claude-haiku-4-5",
    "moonshot:v1-32k",
]


_COST_PER_MTOK: dict[str, float] = {
    "anthropic:claude-opus-4-7": 18.0,
    "anthropic:claude-sonnet-4-6": 6.0,
    "anthropic:claude-haiku-4-5": 1.2,
    "openai:gpt-5.5": 12.0,
    "openai:gpt-4o": 8.0,
    "xai:grok-4": 7.0,
    "deepseek:v4-pro": 1.2,
    "moonshot:v1-32k": 2.0,
}


def _build_chain(primary: str) -> list[str]:
    seen: set[str] = {primary}
    chain: list[str] = [primary]
    for m in _GRADE_FALLBACK_CHAIN:
        if m not in seen:
            chain.append(m)
            seen.add(m)
    return chain


def _estimate_cost(model: str, system: str, user: str, completion: str) -> float:
    rate = _COST_PER_MTOK.get(model, 6.0)
    in_tokens = (len(system or "") + len(user or "")) / 4.0
    out_tokens = len(completion or "") / 4.0
    total = in_tokens + out_tokens
    if total <= 0:
        return 0.0
    return (total / 1_000_000.0) * rate


def _run_chat_sync(
    chat_fn: Any, model: str, system: str, user: str,
    *, max_tokens: Optional[int] = None,
) -> dict[str, Any]:
    async def _do() -> dict[str, Any]:
        return await chat_fn(model, system, user, max_tokens=max_tokens,
                              fallback=None)
    try:
        asyncio.get_running_loop()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(asyncio.run, _do())
            return fut.result(timeout=120)
    except RuntimeError:
        return asyncio.run(_do())


def _parse_json(text: str) -> Optional[dict[str, Any]]:
    text = (text or "").strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    fences = re.findall(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL)
    for chunk in fences:
        try:
            obj = json.loads(chunk.strip())
            if isinstance(obj, dict):
                return obj
        except Exception:
            continue
    start = text.find("{")
    end = text.rfind("}")
    if 0 <= start < end:
        try:
            obj = json.loads(text[start:end + 1])
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    return None


# ============================================================================
# Result type
# ============================================================================


#: Reports scoring below this threshold get `below_grade_threshold` in
#: their quality_flags. The brief composer surfaces them separately.
DEFAULT_GRADE_THRESHOLD = 7.0


@dataclass
class ReportGrade:
    """Output of `grade_report`. All scores in [0, 10]."""
    clarity_score: float = 0.0
    evidence_depth_score: float = 0.0
    novelty_vs_consensus_score: float = 0.0
    falsifiability_score: float = 0.0
    sizing_rigor_score: float = 0.0
    overall_score: float = 0.0
    grader_rationale: str = ""
    cost_usd: float = 0.0
    model_used: str = ""
    quality_flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "clarity_score": self.clarity_score,
            "evidence_depth_score": self.evidence_depth_score,
            "novelty_vs_consensus_score": self.novelty_vs_consensus_score,
            "falsifiability_score": self.falsifiability_score,
            "sizing_rigor_score": self.sizing_rigor_score,
            "overall_score": self.overall_score,
            "grader_rationale": self.grader_rationale,
            "cost_usd": self.cost_usd,
            "model_used": self.model_used,
            "quality_flags": list(self.quality_flags),
        }


# ============================================================================
# Prompt
# ============================================================================


_GRADE_SYSTEM = (
    "You are the head of research at an institutional desk. Score the "
    "report below on 5 dimensions (each 0..10, integers OK).\n\n"
    "  - clarity_score: prose quality + structure. Is the TL;DR sharp? "
    "Are sections well-organized?\n"
    "  - evidence_depth_score: citation density + dossier coverage. Is "
    "every numerical claim cited? Does the report use the dossier?\n"
    "  - novelty_vs_consensus_score: divergence from street + meaningful "
    "insight. Does it tell us something a JPM/Goldman note wouldn't?\n"
    "  - falsifiability_score: is the forward statement testable + "
    "specific (date + threshold)?\n"
    "  - sizing_rigor_score: is sizing (Kelly fraction, risk%, "
    "max drawdown) self-consistent with the stated conviction?\n\n"
    "Then output `overall_score` as the MEAN of the 5 (one decimal).\n"
    "Then `grader_rationale` <= 300 chars summarizing the verdict.\n\n"
    "Output STRICT JSON ONLY:\n"
    "{\n"
    '  "clarity_score": <0..10>,\n'
    '  "evidence_depth_score": <0..10>,\n'
    '  "novelty_vs_consensus_score": <0..10>,\n'
    '  "falsifiability_score": <0..10>,\n'
    '  "sizing_rigor_score": <0..10>,\n'
    '  "overall_score": <mean of the 5, one decimal>,\n'
    '  "grader_rationale": "<<= 300 chars>"\n'
    "}\n\n"
    "Output JSON ONLY. No prose outside the JSON. Be HONEST — a "
    "mediocre report should score 5-6, a strong report 7-8, an "
    "exceptional report 9-10."
)


def _build_user_prompt(
    body_md: str,
    dossier_summary: dict[str, Any],
    comparables_summary: dict[str, Any],
) -> str:
    out: list[str] = []
    out.append("## Report body")
    out.append(body_md.strip()[:8000])
    out.append("")
    out.append("## Dossier summary (for context)")
    out.append(
        f"- n_claims_pulled: {dossier_summary.get('n_claims_pulled')}\n"
        f"- n_unique_sources: {dossier_summary.get('n_unique_sources')}\n"
        f"- quality_flags: {dossier_summary.get('quality_flags')}"
    )
    out.append("")
    out.append("## Comparables summary (for context)")
    n_analogs = len(comparables_summary.get("historical_analogs") or [])
    out.append(
        f"- n_historical_analogs: {n_analogs}\n"
        f"- edge_durability_days: {comparables_summary.get('edge_durability_days')}\n"
        f"- confidence_in_edge: {comparables_summary.get('confidence_in_edge')}"
    )
    out.append("")
    out.append("## Instruction\nScore per the schema. JSON only.")
    return "\n".join(out)


def _clamp(x: Any, lo: float = 0.0, hi: float = 10.0) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    return max(lo, min(hi, v))


# ============================================================================
# Public entry
# ============================================================================


def grade_report(
    body_md: str,
    dossier: EvidenceDossier,
    comparables: ComparablesPack,
    *,
    model: str = "anthropic:claude-sonnet-4-6",
    max_tokens: int = 800,
    threshold: float = DEFAULT_GRADE_THRESHOLD,
) -> ReportGrade:
    """Grade the final polished report. NEVER raises — on total chain
    failure returns a zeroed grade with `grade_unavailable` flag."""
    try:
        _ensure_tic_on_path()
        from tic.desk.models import chat as _chat  # type: ignore
    except Exception as e:  # noqa: BLE001
        logger.info("grade_report: tic.desk.models.chat unavailable: %s", e)
        return ReportGrade(quality_flags=["grade_unavailable"])

    system = _GRADE_SYSTEM
    user = _build_user_prompt(
        body_md,
        {
            "n_claims_pulled": dossier.n_claims_pulled,
            "n_unique_sources": dossier.n_unique_sources,
            "quality_flags": dossier.quality_flags,
        },
        {
            "historical_analogs": comparables.historical_analogs,
            "edge_durability_days": comparables.edge_durability_days,
            "confidence_in_edge": comparables.confidence_in_edge,
        },
    )
    chain = _build_chain(model)

    for m in chain:
        try:
            res = _run_chat_sync(_chat, m, system, user, max_tokens=max_tokens)
        except Exception as e:  # noqa: BLE001
            logger.info("grade_report: %s chat_call_failed: %s", m, e)
            continue
        text = (res.get("text") or "").strip()
        err = res.get("error")
        if err or not text:
            continue
        parsed = _parse_json(text)
        if parsed is None:
            continue
        clarity = _clamp(parsed.get("clarity_score"))
        depth = _clamp(parsed.get("evidence_depth_score"))
        novelty = _clamp(parsed.get("novelty_vs_consensus_score"))
        fals = _clamp(parsed.get("falsifiability_score"))
        sizing = _clamp(parsed.get("sizing_rigor_score"))
        # Trust the model's reported overall if sane, else recompute.
        overall_raw = parsed.get("overall_score")
        overall = _clamp(overall_raw)
        recomputed = (clarity + depth + novelty + fals + sizing) / 5.0
        # If the model's overall is wildly off the mean, use the
        # recomputed value (defends against bogus arithmetic).
        if abs(overall - recomputed) > 1.0:
            overall = recomputed
        if overall <= 0.0 and recomputed > 0.0:
            overall = recomputed
        overall = _clamp(overall)

        rationale = str(parsed.get("grader_rationale") or "")[:300]
        model_used = res.get("model_used") or m
        cost = _estimate_cost(model_used, system, user, text)
        flags: list[str] = []
        if overall < threshold:
            flags.append("below_grade_threshold")
        return ReportGrade(
            clarity_score=clarity,
            evidence_depth_score=depth,
            novelty_vs_consensus_score=novelty,
            falsifiability_score=fals,
            sizing_rigor_score=sizing,
            overall_score=overall,
            grader_rationale=rationale,
            cost_usd=cost,
            model_used=model_used,
            quality_flags=flags,
        )

    return ReportGrade(quality_flags=["grade_unavailable"])


__all__ = [
    "ReportGrade",
    "grade_report",
    "DEFAULT_GRADE_THRESHOLD",
]

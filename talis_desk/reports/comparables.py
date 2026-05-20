"""Stage 1 — Comparables pack.

ONE LLM call (walks the full multi-provider fallback chain). Takes the
hypothesis + the stage-0 evidence dossier and produces:

  * historical_analogs   — 3 best historical analogs (date range, similarity,
                             outcome, key differences, citation_claim_ids)
  * consensus_position   — what the street currently thinks about this setup
  * our_divergence       — where we differ + why our edge is real
  * edge_durability_days — how long the edge likely lasts
  * confidence_in_edge   — 0..1

This stage is what makes our reports SUPERSEDE a JPM/Goldman desk note:
we explicitly model "here's what consensus thinks, here's why we're
different, here's the historical precedent". No JPM PM is doing the
analog hunt with a 5-year crypto+macro corpus on demand.

# No stubs

If every provider in the chain fails, raises `ComparablesUnavailableError`.
The pipeline catches it and degrades gracefully — the researcher draft
still runs with an EMPTY comparables pack and quality_flag
`comparables_unavailable` is recorded.
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
from .dossier import EvidenceDossier, render_dossier_markdown


logger = logging.getLogger(__name__)


# ============================================================================
# Fallback chain — Sonnet first (cheaper than Opus and good enough for
# structured analog matching), then Opus, then the cheaper providers.
# ============================================================================

_COMPARABLES_FALLBACK_CHAIN: list[str] = [
    "anthropic:claude-sonnet-4-6",
    "anthropic:claude-opus-4-7",
    "openai:gpt-5.5",
    "xai:grok-4",
    "deepseek:v4-pro",
    "anthropic:claude-haiku-4-5",
    "moonshot:v1-32k",
    "perplexity:sonar-pro",
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
    "perplexity:sonar-pro": 5.0,
}


def _build_provider_chain(primary: str) -> list[str]:
    seen: set[str] = {primary}
    chain: list[str] = [primary]
    for m in _COMPARABLES_FALLBACK_CHAIN:
        if m not in seen:
            chain.append(m)
            seen.add(m)
    return chain


def _estimate_cost_usd(model: str, system: str, user: str, completion: str) -> float:
    rate = _COST_PER_MTOK.get(model, 6.0)
    in_tokens = (len(system or "") + len(user or "")) / 4.0
    out_tokens = len(completion or "") / 4.0
    total = in_tokens + out_tokens
    if total <= 0:
        return 0.0
    return (total / 1_000_000.0) * rate


# ============================================================================
# Result types + error
# ============================================================================


class ComparablesUnavailableError(RuntimeError):
    """Raised when every provider in the comparables fallback chain has
    failed. The pipeline catches this and continues with an empty pack."""


@dataclass
class ComparablesPack:
    """Output of `find_comparables`."""
    historical_analogs: list[dict[str, Any]] = field(default_factory=list)
    consensus_position: str = ""
    our_divergence: str = ""
    edge_durability_days: int = 0
    confidence_in_edge: float = 0.0
    cost_usd: float = 0.0
    model_used: str = ""
    quality_flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "historical_analogs": self.historical_analogs,
            "consensus_position": self.consensus_position,
            "our_divergence": self.our_divergence,
            "edge_durability_days": self.edge_durability_days,
            "confidence_in_edge": self.confidence_in_edge,
            "cost_usd": self.cost_usd,
            "model_used": self.model_used,
            "quality_flags": list(self.quality_flags),
        }


# ============================================================================
# Chat bridge + JSON parser
# ============================================================================


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
            return fut.result(timeout=180)
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
# Prompt builders
# ============================================================================


_COMPARABLES_SYSTEM = (
    "You are a senior quant analyst with access to 5 years of crypto and "
    "macro history. Given the hypothesis and the evidence dossier below, "
    "produce a structured comparables analysis.\n\n"
    "Your job is to do three things JPM/Goldman/Citadel desks do NOT do "
    "well in their morning notes:\n"
    "  1. Identify the 3 BEST historical analogs (same setup, even if "
    "different instrument). For each: date_range, similarity_score 0-1, "
    "outcome_summary (what actually happened to price/funding/etc over "
    "the next ~14d), key_differences (why THIS time may diverge), and "
    "citation_claim_ids from the dossier.\n"
    "  2. State the CONSENSUS institutional position on this setup. What "
    "is the street already pricing? What does flow look like?\n"
    "  3. State OUR DIVERGENCE — where we differ from consensus and why "
    "the edge is real (not just contrarian for its own sake).\n"
    "End with edge_durability_days (integer — how long the edge likely "
    "lasts before consensus catches up) and confidence_in_edge (0-1).\n\n"
    "Output STRICT JSON ONLY. Schema:\n"
    "{\n"
    '  "historical_analogs": [\n'
    "    {\n"
    '      "date_range": "<YYYY-MM-DD..YYYY-MM-DD>",\n'
    '      "similarity_score": <float 0..1>,\n'
    '      "outcome_summary": "<1-2 sentence what actually happened>",\n'
    '      "key_differences": "<1-2 sentence why this time may diverge>",\n'
    '      "citation_claim_ids": ["<claim_id from dossier>", ...]\n'
    "    },\n"
    "    ... (exactly 3 analogs)\n"
    "  ],\n"
    '  "consensus_position": "<2-3 sentence description of what the street '
    "thinks>\",\n"
    '  "our_divergence": "<2-3 sentence description of where we differ + '
    "why our edge is real>\",\n"
    '  "edge_durability_days": <int>,\n'
    '  "confidence_in_edge": <float 0..1>\n'
    "}\n\n"
    "Output JSON ONLY. No prose outside the JSON. If you cannot find 3 "
    "genuinely similar analogs in the dossier, fill the array with as "
    "many as you can justify (1 or 2) and set "
    "confidence_in_edge accordingly — do NOT fabricate analogs."
)


def _build_user_prompt(
    hypothesis: dict[str, Any], dossier: EvidenceDossier,
) -> str:
    out: list[str] = []
    out.append("## Hypothesis")
    out.append(f"- id: {hypothesis.get('id')}")
    out.append(f"- title: {hypothesis.get('title')}")
    posterior = hypothesis.get("posterior_prob")
    out.append(f"- posterior: {posterior}")
    out.append(f"- entities: {list(hypothesis.get('entity_ids') or [])}")
    text = hypothesis.get("hypothesis_text") or ""
    if text:
        out.append(f"- text: {text[:1200]}")
    out.append("")
    out.append("## Evidence dossier (stage-0 pull from tic.db)")
    out.append(render_dossier_markdown(dossier))
    out.append("")
    out.append(
        "## Instruction\nProduce the comparables JSON per the schema. "
        "Cite claim_ids from the dossier — do NOT invent claim ids."
    )
    return "\n".join(out)


# ============================================================================
# Coercion + validation
# ============================================================================


def _coerce_analog(raw: Any) -> Optional[dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    sim = raw.get("similarity_score")
    try:
        sim_f = float(sim) if sim is not None else 0.0
    except (TypeError, ValueError):
        sim_f = 0.0
    sim_f = max(0.0, min(1.0, sim_f))
    cids = raw.get("citation_claim_ids") or []
    if not isinstance(cids, list):
        cids = []
    cids_clean = [str(c) for c in cids if c][:8]
    return {
        "date_range": str(raw.get("date_range") or "")[:60],
        "similarity_score": sim_f,
        "outcome_summary": str(raw.get("outcome_summary") or "")[:400],
        "key_differences": str(raw.get("key_differences") or "")[:400],
        "citation_claim_ids": cids_clean,
    }


def _coerce_pack(raw: dict[str, Any]) -> dict[str, Any]:
    analogs_raw = raw.get("historical_analogs") or []
    analogs: list[dict[str, Any]] = []
    if isinstance(analogs_raw, list):
        for a in analogs_raw[:5]:
            coerced = _coerce_analog(a)
            if coerced is not None:
                analogs.append(coerced)

    dur = raw.get("edge_durability_days")
    try:
        dur_i = int(dur) if dur is not None else 0
    except (TypeError, ValueError):
        dur_i = 0
    dur_i = max(0, min(365, dur_i))

    conf = raw.get("confidence_in_edge")
    try:
        conf_f = float(conf) if conf is not None else 0.0
    except (TypeError, ValueError):
        conf_f = 0.0
    conf_f = max(0.0, min(1.0, conf_f))

    return {
        "historical_analogs": analogs,
        "consensus_position": str(raw.get("consensus_position") or "")[:800],
        "our_divergence": str(raw.get("our_divergence") or "")[:800],
        "edge_durability_days": dur_i,
        "confidence_in_edge": conf_f,
    }


# ============================================================================
# Public entry point
# ============================================================================


def find_comparables(
    hypothesis: dict[str, Any],
    dossier: EvidenceDossier,
    *,
    model: str = "anthropic:claude-sonnet-4-6",
    max_tokens: int = 1800,
) -> ComparablesPack:
    """Run the comparables LLM stage. Walks the full multi-provider chain.

    Raises `ComparablesUnavailableError` if every provider fails. The
    pipeline catches this and continues with an empty pack + a quality
    flag — we NEVER fabricate analogs.
    """
    _ensure_tic_on_path()
    try:
        from tic.desk.models import chat as _chat  # type: ignore
    except Exception as e:  # noqa: BLE001
        raise ComparablesUnavailableError(
            f"tic.desk.models.chat unavailable: {e!s}"
        ) from e

    system = _COMPARABLES_SYSTEM
    user = _build_user_prompt(hypothesis, dossier)
    chain = _build_provider_chain(model)

    last_error: Optional[str] = None
    for m in chain:
        try:
            res = _run_chat_sync(_chat, m, system, user, max_tokens=max_tokens)
        except Exception as e:  # noqa: BLE001
            last_error = f"{m}: chat_call_failed: {e!s}"
            continue
        text = (res.get("text") or "").strip()
        err = res.get("error")
        if err:
            last_error = f"{m}: {err}"
            continue
        if not text:
            last_error = f"{m}: empty_completion"
            continue
        parsed = _parse_json(text)
        if parsed is None:
            last_error = f"{m}: unparseable_json"
            continue
        try:
            coerced = _coerce_pack(parsed)
        except Exception as e:  # noqa: BLE001
            last_error = f"{m}: coercion_failed: {e!s}"
            continue
        model_used = res.get("model_used") or m
        cost = _estimate_cost_usd(model_used, system, user, text)
        flags: list[str] = []
        if not coerced["historical_analogs"]:
            flags.append("comparables_no_analogs")
        elif len(coerced["historical_analogs"]) < 3:
            flags.append("comparables_partial_analogs")
        if not coerced["consensus_position"]:
            flags.append("comparables_no_consensus")
        if not coerced["our_divergence"]:
            flags.append("comparables_no_divergence")
        return ComparablesPack(
            historical_analogs=coerced["historical_analogs"],
            consensus_position=coerced["consensus_position"],
            our_divergence=coerced["our_divergence"],
            edge_durability_days=coerced["edge_durability_days"],
            confidence_in_edge=coerced["confidence_in_edge"],
            cost_usd=cost,
            model_used=model_used,
            quality_flags=flags,
        )

    raise ComparablesUnavailableError(
        f"All {len(chain)} providers failed for comparables stage. "
        f"Last error: {last_error}"
    )


def empty_pack() -> ComparablesPack:
    """An empty pack — used when the comparables stage is skipped after
    a chain failure (the pipeline degrades gracefully)."""
    return ComparablesPack(quality_flags=["comparables_unavailable"])


def render_comparables_markdown(pack: ComparablesPack) -> str:
    """Render the pack as markdown for the researcher prompt."""
    if not pack.historical_analogs and not pack.consensus_position:
        return "### Comparables pack (stage-1)\n\n_(empty — comparables stage unavailable)_"
    out: list[str] = []
    out.append("### Comparables pack (stage-1)")
    out.append(
        f"- model: {pack.model_used or '?'} | "
        f"edge_durability_days: {pack.edge_durability_days} | "
        f"confidence_in_edge: {pack.confidence_in_edge:.2f}"
    )
    if pack.quality_flags:
        out.append(f"- quality_flags: {pack.quality_flags}")
    out.append("")
    if pack.historical_analogs:
        out.append("#### Historical analogs")
        for i, a in enumerate(pack.historical_analogs, 1):
            cids = a.get("citation_claim_ids") or []
            refs = " ".join(f"[claim:{c}]" for c in cids[:4])
            out.append(
                f"{i}. **{a.get('date_range','?')}** "
                f"(sim={a.get('similarity_score', 0):.2f}) {refs}\n"
                f"   - outcome: {a.get('outcome_summary','')}\n"
                f"   - key differences: {a.get('key_differences','')}"
            )
        out.append("")
    if pack.consensus_position:
        out.append("#### Consensus position (what the street thinks)")
        out.append(pack.consensus_position)
        out.append("")
    if pack.our_divergence:
        out.append("#### Our divergence (why our edge is real)")
        out.append(pack.our_divergence)
        out.append("")
    return "\n".join(out)


__all__ = [
    "ComparablesPack",
    "ComparablesUnavailableError",
    "find_comparables",
    "empty_pack",
    "render_comparables_markdown",
]

"""Tier 2 — analyst pool (specialist personas as constitutional voices).

The 21 existing specialist personas (macro_regime, microstructure, ...)
are wired in here as constitutional voices. The swarm wraps them; it
does NOT edit their persona.py or prompt.md.

For each verified scout output:
  - Route it to the specialist whose `topic_affinity` best matches the
    lens (macro -> macro_regime, options_flow -> options_vol, etc.)
  - Have that specialist run a focused investigation around the
    scout's hypothesis (sub-cycle of the existing 6-stage pipeline,
    but scoped to ONE hypothesis at a time so cost is bounded)
  - Persist the resulting hypothesis-level draft + posterior

Routing uses `topic_affinity` patterns + best-effort fallback to the
generalist `macro_regime` when no specialist matches.

Cost target: ≤$1.50 total Tier 2.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from ..agents_native import post_message
from ..coordination import append_blackboard_event, attribute_failure
from .._tic_config import ensure_tic_on_path as _ensure_tic_on_path
from ..store import get_desk_store
from .scout_runner import ScoutOutput
from .verifier_council import VerifierVerdict


logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Lens -> Specialist routing map.
# ----------------------------------------------------------------------

LENS_TO_SPECIALIST: dict[str, str] = {
    "macro": "macro_regime",
    "microstructure": "microstructure",
    "options_flow": "options_vol",
    "smart_money": "smart_money",
    "sentiment": "sentiment_event",
    "rotation": "rrg_rotation",
    "factor": "factor_strategy",
    "vol_surface": "vol_quant",
    "catalyst": "catalyst_tracker",
    "filing": "material_info_surveillance",
    "polymarket": "polymarket_divergence",
    "anomaly": "anomaly_scanner",
    "on_chain": "smart_money",
    "money_velocity": "money_rotation",
    "structural": "structural_thiel_lky",
}

DEFAULT_FALLBACK_SPECIALIST = "macro_regime"

# How many top-confidence verified scouts to actually run through Tier 2.
# Hard cap so a runaway scout swarm doesn't blow Tier 2 budget.
DEFAULT_ANALYST_TOPK = 50

# Model used by the analyst draft step.
ANALYST_MODEL = "anthropic:claude-sonnet-4-6"
ANALYST_FALLBACK = "deepseek:v4-pro"
ANALYST_MAX_TOKENS = 1500


# ----------------------------------------------------------------------
# Output dataclass
# ----------------------------------------------------------------------

@dataclass
class AnalystOutput:
    scout_id: str
    hypothesis_id: Optional[str]
    cycle_id: str
    specialist_id: str
    entity: str
    horizon: str
    lens: str
    draft_md: str
    posterior: float
    edge_thesis: str
    contradicting_evidence: str
    suggested_tools: list[str]
    confidence: float
    quality_flags: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    elapsed_s: float = 0.0
    error: Optional[str] = None


# ----------------------------------------------------------------------
# Routing
# ----------------------------------------------------------------------

def _route_lens_to_specialist(lens: str) -> str:
    return LENS_TO_SPECIALIST.get(lens, DEFAULT_FALLBACK_SPECIALIST)


def _load_specialist_persona(specialist_id: str) -> Optional[dict[str, Any]]:
    """Look up the latest specialist_states row (persona kind) for the
    specialist. Returns the persona dict or None."""
    try:
        conn = get_desk_store().conn
        row = conn.execute(
            "SELECT * FROM specialist_states "
            "WHERE specialist_id = ? AND state_kind = 'persona' "
            "AND transaction_to IS NULL "
            "ORDER BY transaction_from DESC LIMIT 1",
            (specialist_id,),
        ).fetchone()
        if row is None:
            return None
        # state_json holds the SpecialistPersona payload as JSON.
        try:
            persona = json.loads(row["state_json"]) if row["state_json"] else {}
        except Exception:
            persona = {}
        return {
            "specialist_id": row["specialist_id"],
            "persona_version": row["persona_version"],
            "persona": persona,
        }
    except Exception as e:
        logger.warning("persona load failed for %s: %s", specialist_id, e)
        return None


# ----------------------------------------------------------------------
# Analyst draft prompt
# ----------------------------------------------------------------------

ANALYST_SYSTEM_TEMPLATE = (
    "You are the {specialist_id} specialist on the Talis research desk. "
    "Your constitutional voice + scope is:\n\n{persona_system_prompt}\n\n"
    "A Tier-1 scout proposed a hypothesis and a 3-of-3 verifier council "
    "graduated it to you. Produce a focused investigation draft.\n\n"
    "Return STRICT JSON only:\n"
    "{{\n"
    '  "draft_md": "<2-4 paragraph markdown investigation: setup, mechanism, '
    'falsification test, position sizing thought, 1+ specific data points or '
    'tool calls you would run>",\n'
    '  "posterior": 0.0,  // 0..1 posterior probability after analyst review\n'
    '  "edge_thesis": "<one sentence: what is the edge>",\n'
    '  "contradicting_evidence": "<one sentence: what would kill this thesis>",\n'
    '  "suggested_tools": ["<tic.tool.uri>", ...],\n'
    '  "confidence": 0.0  // 0..1 analyst confidence in the draft\n'
    "}}\n\n"
    "Constraints:\n"
    "- Stay in scope (your specialist persona constrains what you cover).\n"
    "- Cite at least one concrete data point or tool URI in the draft_md.\n"
    "- Use the bias_mode to orient: contrarian fades consensus, etc.\n"
    "- NO STUBS. If the hypothesis is out of your scope, set posterior=0.5 "
    "and explain in draft_md that you would route this to a different "
    "specialist."
)


def _build_analyst_user_prompt(
    scout: ScoutOutput, verdict: VerifierVerdict,
) -> str:
    parts = [
        f"Scout hypothesis: {scout.hypothesis_text}",
        f"Entity: {scout.entity}",
        f"Horizon: {scout.horizon}",
        f"Lens: {scout.lens}",
        f"Bias mode: {scout.bias_mode}",
        f"Scout confidence: {scout.confidence:.2f}",
        f"Scout rationale: {scout.rationale_brief}",
        f"Scout suggested tools: {scout.suggested_tools}",
        f"Verifier votes: " + ", ".join(
            f"{v.family}={v.verdict}" for v in verdict.votes
        ),
        "",
        "Produce your investigation draft as strict JSON.",
    ]
    return "\n".join(parts)


# ----------------------------------------------------------------------
# Single analyst execution
# ----------------------------------------------------------------------

async def _run_one_analyst(
    scout: ScoutOutput, verdict: VerifierVerdict,
) -> AnalystOutput:
    t0 = time.perf_counter()
    specialist_id = _route_lens_to_specialist(scout.lens)
    out = AnalystOutput(
        scout_id=scout.scout_id,
        hypothesis_id=scout.hypothesis_id,
        cycle_id=scout.cycle_id,
        specialist_id=specialist_id,
        entity=scout.entity,
        horizon=scout.horizon,
        lens=scout.lens,
        draft_md="",
        posterior=0.5,
        edge_thesis="",
        contradicting_evidence="",
        suggested_tools=[],
        confidence=0.0,
    )

    persona = _load_specialist_persona(specialist_id)
    if persona is None:
        out.quality_flags.append("specialist_persona_missing")
        out.error = f"persona_missing:{specialist_id}"
        out.elapsed_s = time.perf_counter() - t0
        return out

    persona_obj = persona.get("persona") or {}
    sys_prompt = persona_obj.get("system_prompt") or ""
    if not sys_prompt:
        out.quality_flags.append("specialist_system_prompt_empty")
        out.error = f"empty_system_prompt:{specialist_id}"
        out.elapsed_s = time.perf_counter() - t0
        return out

    system = ANALYST_SYSTEM_TEMPLATE.format(
        specialist_id=specialist_id,
        persona_system_prompt=sys_prompt[:6000],  # cap to keep context bounded
    )
    user = _build_analyst_user_prompt(scout, verdict)

    _ensure_tic_on_path()
    try:
        from tic.desk.models import chat as _chat  # type: ignore
    except Exception as e:
        out.quality_flags.append("analyst_provider_unavailable")
        out.error = f"tic_models_import_failed: {e}"
        out.elapsed_s = time.perf_counter() - t0
        return out

    try:
        res = await _chat(
            ANALYST_MODEL, system, user,
            max_tokens=ANALYST_MAX_TOKENS, fallback=ANALYST_FALLBACK,
        )
    except Exception as e:
        out.quality_flags.append("analyst_chat_failed")
        out.error = f"chat_failed: {type(e).__name__}: {e}"
        out.elapsed_s = time.perf_counter() - t0
        return out

    text = (res.get("text") or "").strip()
    used = res.get("model_used", ANALYST_MODEL)
    err = res.get("error")
    # Cost estimate: Sonnet ~ $6/Mtok, ~3000 token roundtrip
    cost_rate = 6.0 if "sonnet" in used else (1.2 if "deepseek" in used else 4.0)
    out.cost_usd = cost_rate * 3000 / 1_000_000

    parsed = _extract_first_json(text)
    if not isinstance(parsed, dict):
        out.quality_flags.append("analyst_json_unparseable")
        out.error = err or "analyst_json_unparseable"
        out.elapsed_s = time.perf_counter() - t0
        return out

    out.draft_md = (parsed.get("draft_md") or "").strip()
    try:
        out.posterior = float(parsed.get("posterior") or 0.5)
    except Exception:
        out.posterior = 0.5
    out.posterior = max(0.0, min(1.0, out.posterior))
    out.edge_thesis = (parsed.get("edge_thesis") or "").strip()[:280]
    out.contradicting_evidence = (
        parsed.get("contradicting_evidence") or ""
    ).strip()[:280]
    sugg = parsed.get("suggested_tools") or []
    if isinstance(sugg, list):
        out.suggested_tools = [str(x) for x in sugg[:6]]
    try:
        out.confidence = float(parsed.get("confidence") or 0.0)
    except Exception:
        out.confidence = 0.0
    out.confidence = max(0.0, min(1.0, out.confidence))

    if not out.draft_md:
        out.quality_flags.append("analyst_empty_draft")

    # Post to analyst output topic + update hypothesis row.
    try:
        _persist_analyst_output(out)
    except Exception as e:
        logger.warning("analyst persist failed: %s", e)

    out.elapsed_s = time.perf_counter() - t0
    return out


def _persist_analyst_output(out: AnalystOutput) -> None:
    topic = f"bb_topic:analyst_output:{out.lens}:{out.entity}"
    post_message(
        from_agent=f"analyst:{out.specialist_id}",
        to_agent_or_topic=topic,
        kind="observation",
        payload={
            "scout_id": out.scout_id,
            "hypothesis_id": out.hypothesis_id,
            "cycle_id": out.cycle_id,
            "specialist_id": out.specialist_id,
            "entity": out.entity,
            "horizon": out.horizon,
            "lens": out.lens,
            "posterior": out.posterior,
            "confidence": out.confidence,
            "edge_thesis": out.edge_thesis,
            "contradicting_evidence": out.contradicting_evidence,
            "draft_excerpt": out.draft_md[:600],
            "quality_flags": out.quality_flags,
        },
        related_hypothesis_id=out.hypothesis_id,
        expires_in_hours=72,
        topic=topic,
    )
    append_blackboard_event(
        event_type="analysis.promoted",
        cycle_id=out.cycle_id,
        topic=topic,
        task_id=None,
        claim_id=out.hypothesis_id,
        agent_id=f"analyst:{out.specialist_id}",
        specialist_id=out.specialist_id,
        payload={
            "scout_id": out.scout_id,
            "hypothesis_id": out.hypothesis_id,
            "posterior": out.posterior,
            "confidence": out.confidence,
            "edge_thesis": out.edge_thesis,
            "quality_flags": out.quality_flags,
        },
    )
    # Best-effort hypothesis posterior update.
    if out.hypothesis_id:
        try:
            conn = get_desk_store().conn
            conn.execute(
                "UPDATE hypotheses SET posterior_prob = ?, heat_score = ? WHERE id = ?",
                (out.posterior, out.confidence, out.hypothesis_id),
            )
            conn.commit()
        except Exception:
            try:
                conn.execute(
                    "UPDATE hypotheses SET posterior = ? WHERE id = ?",
                    (out.posterior, out.hypothesis_id),
                )
                conn.commit()
            except Exception:
                pass


def _attribute_analyst_error(out: AnalystOutput) -> None:
    if not out.error:
        return
    try:
        attribute_failure(
            artifact_kind="hypothesis",
            artifact_id=out.hypothesis_id or out.scout_id,
            failure_kind=out.quality_flags[-1] if out.quality_flags else "analyst_error",
            cycle_id=out.cycle_id,
            specialist_id=out.specialist_id,
            severity="yellow",
            rationale=out.error,
            payload={
                "entity": out.entity,
                "horizon": out.horizon,
                "lens": out.lens,
            },
        )
    except Exception:
        pass


# ----------------------------------------------------------------------
# Public entry point
# ----------------------------------------------------------------------

def _select_topk(
    verdicts: list[VerifierVerdict],
    scouts: list[ScoutOutput],
    k: int,
) -> list[tuple[ScoutOutput, VerifierVerdict]]:
    """Pick the top-k verified scouts by (n_approve, scout.confidence).

    Hypotheses that didn't graduate (decision != approve) are filtered out.
    """
    by_scout: dict[str, ScoutOutput] = {s.scout_id: s for s in scouts}
    approved = [(by_scout.get(v.scout_id), v) for v in verdicts if v.decision == "approve"]
    approved = [(s, v) for s, v in approved if s is not None]
    approved.sort(
        key=lambda pair: (pair[1].n_approve, pair[0].confidence),
        reverse=True,
    )
    return approved[:k]


async def _async_run_analyst_pool(
    scouts: list[ScoutOutput],
    verdicts: list[VerifierVerdict],
    topk: int,
) -> list[AnalystOutput]:
    selected = _select_topk(verdicts, scouts, topk)
    sem = asyncio.Semaphore(10)  # cap concurrent analyst calls

    async def _bounded(pair: tuple[ScoutOutput, VerifierVerdict]) -> AnalystOutput:
        s, v = pair
        async with sem:
            return await _run_one_analyst(s, v)

    results = await asyncio.gather(*[_bounded(p) for p in selected])
    for out in results:
        _attribute_analyst_error(out)
    return results


def run_analyst_pool(
    scouts: list[ScoutOutput],
    verdicts: list[VerifierVerdict],
    topk: int = DEFAULT_ANALYST_TOPK,
) -> list[AnalystOutput]:
    """Run Tier 2: route each verified hypothesis to its constitutional
    specialist + produce a focused investigation draft.

    Returns one AnalystOutput per top-k verified scout.
    """
    if not scouts or not verdicts:
        return []

    async def _go() -> list[AnalystOutput]:
        return await _async_run_analyst_pool(scouts, verdicts, topk)

    try:
        asyncio.get_running_loop()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            return ex.submit(asyncio.run, _go()).result()
    except RuntimeError:
        return asyncio.run(_go())


def _extract_first_json(text: str) -> Any:
    if not text:
        return None
    s = text
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(s)):
        ch = s[i]
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

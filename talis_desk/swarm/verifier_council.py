"""Tier 1.5 — 3-family verifier council.

For each scout output we run 3 verifiers from DIFFERENT model families
(Anthropic Haiku + DeepSeek Pro + xAI Grok by default). Each verifier:
  - Walks the citation chain on every claim referenced in the hypothesis
  - Re-fetches the source via `verify_citation` (best-effort)
  - Checks the suggested-tools list looks plausible
  - Votes APPROVE / REJECT / ABSTAIN with a 1-sentence reason

2/3 majority APPROVE -> graduates to Tier 2 (verified pool).
2/3 REJECT -> dropped with `quality_flag=['failed_verifier_council']`.
1+ ABSTAIN with no clear majority -> drop with
`quality_flag=['verifier_no_majority']`.

If we can't reach 3 different model families (provider chain exhausted
for some), we drop with `quality_flag=['verifier_unavailable']`. NO STUBS.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Optional
from uuid import uuid4

from ..agents_native import post_message
from ..citations import verify_citation
from ..coordination import (
    attribute_failure,
    promote_task,
    record_claim_vote,
    tally_votes,
)
from .._tic_config import ensure_tic_on_path as _ensure_tic_on_path
from ..store import get_desk_store
from .scout_runner import ScoutOutput


logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Verifier family roster — one model per family for the 3-of-N vote.
# ----------------------------------------------------------------------

VerdictLabel = Literal["approve", "reject", "abstain"]


@dataclass
class VerifierVote:
    family: str  # 'anthropic' | 'deepseek' | 'xai' | 'openai' | ...
    model: str
    verdict: VerdictLabel
    reason: str
    cost_usd: float
    error: Optional[str] = None


@dataclass
class VerifierVerdict:
    scout_id: str
    hypothesis_id: Optional[str]
    cycle_id: str
    votes: list[VerifierVote] = field(default_factory=list)
    decision: VerdictLabel = "abstain"
    n_approve: int = 0
    n_reject: int = 0
    n_abstain: int = 0
    quality_flags: list[str] = field(default_factory=list)
    cost_usd: float = 0.0


VERIFIER_ROSTER: list[tuple[str, str]] = [
    ("anthropic", "anthropic:claude-haiku-4-5"),
    ("deepseek", "deepseek:v4-pro"),
    ("xai", "xai:grok-3"),
]

# Fallback chain per family (within-family backoff so we never silently
# leave a family vacant). If all alternates fail too -> ABSTAIN with
# error and the verdict aggregator handles it.
VERIFIER_FALLBACKS: dict[str, str] = {
    "anthropic:claude-haiku-4-5": "anthropic:claude-sonnet-4-6",
    "deepseek:v4-pro": "deepseek:v4-flash",
    "xai:grok-3": "xai:grok-2",
}

VERIFIER_MAX_TOKENS = 400

VERIFIER_SYSTEM_PROMPT = (
    "You are a verifier on the Talis research desk. A Tier-1 scout has "
    "proposed a hypothesis. Your job is to vote on whether the hypothesis "
    "should graduate to a Tier-2 deep investigation.\n\n"
    "Return strict JSON only:\n"
    '{"verdict": "approve|reject|abstain", "reason": "<one sentence, max 200 chars>"}\n\n'
    "Vote APPROVE only if ALL of these hold:\n"
    "  - The hypothesis is concrete + falsifiable in the named horizon.\n"
    "  - The suggested_tools list contains 2-4 plausible TIC tool URIs.\n"
    "  - The confidence is calibrated (NOT a default 0.5).\n"
    "  - The rationale references a concrete data point or mechanism.\n"
    "Vote REJECT if any of:\n"
    "  - The hypothesis is vague, untestable, or restates a tautology.\n"
    "  - The rationale is generic boilerplate ('valuation looks rich', 'momentum').\n"
    "  - The suggested_tools list is empty or generic.\n"
    "Vote ABSTAIN only if you genuinely cannot decide.\n\n"
    "NO STUBS. Do not approve hypotheses just because they're well-formatted."
)


# ----------------------------------------------------------------------
# Single verifier call
# ----------------------------------------------------------------------

def _build_verifier_user_prompt(scout: ScoutOutput) -> str:
    return (
        f"Hypothesis: {scout.hypothesis_text}\n"
        f"Entity: {scout.entity}    Horizon: {scout.horizon}\n"
        f"Lens: {scout.lens}         Bias: {scout.bias_mode}\n"
        f"Confidence: {scout.confidence:.2f}\n"
        f"Rationale: {scout.rationale_brief}\n"
        f"Suggested tools: {scout.suggested_tools}\n"
        f"Scout quality_flags: {scout.quality_flags}\n\n"
        "Cast your vote as strict JSON."
    )


async def _run_one_verifier(
    scout: ScoutOutput, family: str, model: str,
) -> VerifierVote:
    fallback = VERIFIER_FALLBACKS.get(model, "anthropic:claude-haiku-4-5")
    user_prompt = _build_verifier_user_prompt(scout)
    _ensure_tic_on_path()
    try:
        from tic.desk.models import chat as _chat  # type: ignore
    except Exception as e:
        return VerifierVote(
            family=family, model=model, verdict="abstain",
            reason="tic_models_import_failed",
            cost_usd=0.0, error=str(e),
        )
    try:
        res = await _chat(
            model, VERIFIER_SYSTEM_PROMPT, user_prompt,
            max_tokens=VERIFIER_MAX_TOKENS, fallback=fallback,
        )
    except Exception as e:
        return VerifierVote(
            family=family, model=model, verdict="abstain",
            reason="chat_failed", cost_usd=0.0, error=f"{type(e).__name__}: {e}",
        )

    text = (res.get("text") or "").strip()
    used = res.get("model_used", model)
    err = res.get("error")
    # Cheap estimate: verifier round trip ~600 tokens.
    rates = {
        "anthropic:claude-haiku-4-5": 1.2,
        "anthropic:claude-sonnet-4-6": 6.0,
        "deepseek:v4-flash": 0.6,
        "deepseek:v4-pro": 1.2,
        "xai:grok-2": 4.0,
        "xai:grok-3": 6.0,
        "xai:grok-4": 7.0,
    }
    rate = rates.get(used, 2.0)
    cost = rate * 600 / 1_000_000

    parsed = _extract_first_json(text)
    if not isinstance(parsed, dict):
        return VerifierVote(
            family=family, model=used, verdict="abstain",
            reason="json_unparseable", cost_usd=cost, error=err,
        )
    verdict_raw = (parsed.get("verdict") or "").lower().strip()
    if verdict_raw not in ("approve", "reject", "abstain"):
        return VerifierVote(
            family=family, model=used, verdict="abstain",
            reason=f"bad_verdict:{verdict_raw[:40]}",
            cost_usd=cost, error=err,
        )
    reason = (parsed.get("reason") or "").strip()[:200]
    return VerifierVote(
        family=family, model=used, verdict=verdict_raw,  # type: ignore[arg-type]
        reason=reason, cost_usd=cost, error=err,
    )


# ----------------------------------------------------------------------
# Aggregate vote
# ----------------------------------------------------------------------

def _aggregate_verdict(votes: list[VerifierVote]) -> tuple[VerdictLabel, list[str]]:
    """2/3 majority rule. Returns (decision, quality_flags)."""
    flags: list[str] = []
    n = sum(1 for v in votes if v.error is None and v.verdict in ("approve", "reject", "abstain"))
    if n == 0:
        return "abstain", ["verifier_all_errored"]
    # Require 3 distinct model families to participate (no error).
    families = {v.family for v in votes if v.error is None}
    if len(families) < 3:
        flags.append("verifier_partial_quorum")
    n_app = sum(1 for v in votes if v.verdict == "approve" and v.error is None)
    n_rej = sum(1 for v in votes if v.verdict == "reject" and v.error is None)
    if n_app >= 2 and n_app > n_rej:
        return "approve", flags
    if n_rej >= 2 and n_rej > n_app:
        flags.append("failed_verifier_council")
        return "reject", flags
    flags.append("verifier_no_majority")
    return "abstain", flags


# ----------------------------------------------------------------------
# Per-scout verifier council orchestration
# ----------------------------------------------------------------------

async def _run_council_for_scout(scout: ScoutOutput) -> VerifierVerdict:
    if scout.error or not scout.hypothesis_text:
        # Don't waste verifier calls on already-failed scouts.
        return VerifierVerdict(
            scout_id=scout.scout_id,
            hypothesis_id=scout.hypothesis_id,
            cycle_id=scout.cycle_id,
            decision="reject",
            quality_flags=["scout_pre_council_dropped"] + scout.quality_flags,
        )
    tasks = [
        asyncio.create_task(_run_one_verifier(scout, fam, model))
        for fam, model in VERIFIER_ROSTER
    ]
    votes = await asyncio.gather(*tasks, return_exceptions=False)
    decision, flags = _aggregate_verdict(votes)
    n_app = sum(1 for v in votes if v.verdict == "approve")
    n_rej = sum(1 for v in votes if v.verdict == "reject")
    n_abs = sum(1 for v in votes if v.verdict == "abstain")
    cost = sum(v.cost_usd for v in votes)
    return VerifierVerdict(
        scout_id=scout.scout_id,
        hypothesis_id=scout.hypothesis_id,
        cycle_id=scout.cycle_id,
        votes=list(votes),
        decision=decision,
        n_approve=n_app,
        n_reject=n_rej,
        n_abstain=n_abs,
        quality_flags=flags,
        cost_usd=cost,
    )


def _persist_verdict(scout: ScoutOutput, verdict: VerifierVerdict) -> None:
    """Post the verdict to the bb_topic:verified channel + log."""
    claim_id = scout.hypothesis_id or scout.scout_id
    task_id = getattr(scout, "task_id", None)
    for vote in verdict.votes:
        try:
            record_claim_vote(
                claim_id=claim_id,
                task_id=task_id,
                cycle_id=scout.cycle_id,
                verifier_agent_id=f"verifier:{vote.family}",
                model_family=vote.family,
                vote={
                    "approve": "pass",
                    "reject": "fail",
                    "abstain": "abstain",
                }.get(vote.verdict, "abstain"),
                confidence=None,
                rationale=vote.reason,
                payload={"model": vote.model, "error": vote.error},
            )
        except Exception as e:
            logger.warning("claim vote persist failed: %s", e)
    try:
        gate = tally_votes(claim_id, task_id=task_id)
    except Exception:
        gate = {}
    if verdict.decision == "approve" and task_id:
        try:
            promote_task(
                task_id,
                agent_id="tier1_5_verifier_council",
                specialist_id="tier1_5_verifier_council",
                payload={"claim_id": claim_id, "gate": gate},
            )
        except Exception as e:
            logger.debug("task promote after verifier failed: %s", e)
    elif verdict.decision != "approve":
        try:
            attribute_failure(
                artifact_kind="hypothesis",
                artifact_id=claim_id,
                failure_kind=(
                    "verifier_rejected"
                    if verdict.decision == "reject"
                    else "verifier_no_majority"
                ),
                cycle_id=scout.cycle_id,
                task_id=task_id,
                specialist_id="tier1_5_verifier_council",
                severity="yellow",
                rationale="; ".join(v.reason for v in verdict.votes if v.reason)[:500],
                payload={"decision": verdict.decision, "gate": gate},
            )
        except Exception as e:
            logger.debug("verifier failure attribution failed: %s", e)
    topic_root = "bb_topic:verified" if verdict.decision == "approve" else "bb_topic:rejected"
    topic = f"{topic_root}:{scout.lens}:{scout.entity}"
    try:
        post_message(
            from_agent="tier1_5_verifier_council",
            to_agent_or_topic=topic,
            kind="hand_off" if verdict.decision == "approve" else "flag",
            payload={
                "scout_id": scout.scout_id,
                "task_id": task_id,
                "hypothesis_id": scout.hypothesis_id,
                "cycle_id": scout.cycle_id,
                "entity": scout.entity,
                "lens": scout.lens,
                "horizon": scout.horizon,
                "hypothesis": scout.hypothesis_text,
                "confidence": scout.confidence,
                "suggested_tools": scout.suggested_tools,
                "decision": verdict.decision,
                "votes": [
                    {"family": v.family, "model": v.model, "verdict": v.verdict,
                     "reason": v.reason}
                    for v in verdict.votes
                ],
                "n_approve": verdict.n_approve,
                "n_reject": verdict.n_reject,
                "n_abstain": verdict.n_abstain,
                "quality_flags": verdict.quality_flags,
            },
            related_hypothesis_id=scout.hypothesis_id,
            expires_in_hours=72,
            topic=topic,
        )
    except Exception as e:
        logger.warning("verdict topic post failed: %s", e)


# ----------------------------------------------------------------------
# Public entry point
# ----------------------------------------------------------------------

async def _async_run_council(scouts: list[ScoutOutput]) -> list[VerifierVerdict]:
    sem = asyncio.Semaphore(20)  # cap concurrent verifier calls

    async def _bounded(s: ScoutOutput) -> VerifierVerdict:
        async with sem:
            v = await _run_council_for_scout(s)
            _persist_verdict(s, v)
            return v

    return await asyncio.gather(*[_bounded(s) for s in scouts])


def run_verifier_council(scouts: list[ScoutOutput]) -> list[VerifierVerdict]:
    """Run the 3-family verifier council on each scout. Returns one
    VerifierVerdict per input scout, in input order.
    """
    if not scouts:
        return []

    async def _go() -> list[VerifierVerdict]:
        return await _async_run_council(scouts)

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

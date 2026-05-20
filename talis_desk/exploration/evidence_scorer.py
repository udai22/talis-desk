"""Real LLM-driven evidence scorer.

Replaces the synthetic `_posterior_delta` envelope hook that
`exploration/bfs.py::score_hot_signal` relies on with a calibrated,
hypothesis-aware judgment from an LLM. Given a tool result and the
hypothesis under investigation, the scorer returns a signed posterior
delta, contradiction flag, citation_claim_ids (when the payload carries
any), and a one-line rationale.

# Design contract

  * ONE LLM call per evidence. Cheap model (Haiku) by default; the
    multi-provider fallback chain matches `judge.py` / `composer.py`.
  * NO STUBS. If every provider in the chain fails or returns
    unparseable JSON, raise `EvidenceUnavailableError` so the BFS loop
    can skip the result instead of silently using a fake delta.
  * Posterior delta magnitude capped at +/-0.3 per single evidence so no
    one tool call can dominate the posterior.
  * Quality flags propagate: if `source_health` reports any cited
    source as `stale` / `never_succeeded`, the scorer adds
    `'stale_source'` to `quality_flags`.

# Why a separate module

`exploration/bfs.py` already mixes graph-walking, hot-signal scoring,
sub-investigation spawning, and posterior updates. Folding a multi-
provider LLM fallback chain into that file would make it harder to
test and harder to swap models. Keeping the scorer in its own module
matches the brief composer / debate judge layout and gives the BFS
loop a single import surface.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Optional


# ============================================================================
# Ensure the sibling `tic` package is importable for chat() — same pattern
# debate/judge.py and brief/composer.py use.
#
# Codex finding #16: centralized in `talis_desk._tic_config`.
# ============================================================================

from .._tic_config import ensure_tic_on_path as _ensure_tic_on_path  # noqa: E402


# ============================================================================
# Result type
# ============================================================================

@dataclass
class EvidenceScore:
    """One scored evidence row."""
    posterior_delta: float           # signed, [-0.3, +0.3]
    citation_claim_ids: list[str]    # claim_ids the payload carries
    contradicts: bool                # True if this evidence opposes the hypothesis
    confidence: float                # 0..1, scorer's own certainty
    rationale: str                   # <=300 chars
    quality_flags: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    model_used: str = ""
    # Per-call irrelevance flag encoded as 0.0 or 1.0 so the BFS can
    # average across a hypothesis's scored evidence to get the "share of
    # irrelevant calls". 1.0 means the LLM judged the tool result
    # off-topic / generic and zeroed delta with low confidence — i.e.
    # the call should NOT be counted as a genuine update. The brief
    # composer surfaces a quality_flag when this share is high.
    irrelevant_evidence_share: float = 0.0


def irrelevant_evidence_share(scores: "list[EvidenceScore]") -> float:
    """Aggregate helper: fraction of `scores` flagged irrelevant.

    Returns 0.0 on empty input. Intended for the BFS / brief composer to
    surface a per-hypothesis quality flag when too many tool calls were
    off-topic to be a meaningful Bayesian update.
    """
    if not scores:
        return 0.0
    total = sum(float(getattr(s, "irrelevant_evidence_share", 0.0)) for s in scores)
    return round(total / len(scores), 3)


# ============================================================================
# Errors
# ============================================================================

class EvidenceUnavailableError(RuntimeError):
    """Raised when every provider in the evidence-scorer fallback chain has
    failed. The BFS loop should skip this evidence (NOT fabricate a
    posterior delta) when this raises."""


# ============================================================================
# Constants — single-evidence delta cap + cost table
# ============================================================================

#: Cap |posterior_delta| at this much per single evidence. Per the codex
#: review (finding #4), one tool result must not dominate the posterior.
MAX_DELTA_PER_EVIDENCE = 0.30

#: Rationale truncation budget. Anything longer is clipped.
RATIONALE_MAX_CHARS = 300

#: Multi-provider fallback chain — primary then cheap-to-heavy. Mirrors
#: judge.py / composer.py. Never stubs.
_EVIDENCE_FALLBACK_CHAIN: list[str] = [
    "anthropic:claude-haiku-4-5",
    "anthropic:claude-sonnet-4-6",
    "anthropic:claude-opus-4-7",
    "openai:gpt-5.5",
    "deepseek:v4-pro",
    "xai:grok-4",
    "moonshot:v1-32k",
    "perplexity:sonar-pro",
]

#: Cost per million-token estimate (rough). Same table as runner.py.
_COST_PER_MTOK: dict[str, float] = {
    "anthropic:claude-haiku-4-5": 1.2,
    "anthropic:claude-sonnet-4-6": 6.0,
    "anthropic:claude-opus-4-7": 18.0,
    "openai:gpt-5.5": 12.0,
    "xai:grok-4": 7.0,
    "deepseek:v4-pro": 1.2,
    "moonshot:v1-32k": 2.0,
    "perplexity:sonar-pro": 5.0,
}


def _build_provider_chain(primary: str) -> list[str]:
    """Caller's primary model first, then the rest of the canonical chain
    with duplicates removed."""
    seen: set[str] = {primary}
    chain: list[str] = [primary]
    for m in _EVIDENCE_FALLBACK_CHAIN:
        if m not in seen:
            chain.append(m)
            seen.add(m)
    return chain


def _estimate_cost_usd(model: str, system: str, user: str, completion: str) -> float:
    """Coarse cost estimate (4 chars/token). Used for cycle accounting,
    not billing."""
    rate = _COST_PER_MTOK.get(model, 4.0)
    in_tokens = (len(system or "") + len(user or "")) / 4.0
    out_tokens = len(completion or "") / 4.0
    total = in_tokens + out_tokens
    if total <= 0:
        return 0.0
    return (total / 1_000_000.0) * rate


# ============================================================================
# Prompt
# ============================================================================

EVIDENCE_SYSTEM_PROMPT = (
    "You are an evidence scorer for a quantitative crypto research desk. "
    "A specialist is investigating a hypothesis and just ran a tool. Your "
    "job is to read the tool result and decide:\n"
    "  1. relevance: is the tool result DIRECTLY relevant to a numeric or "
    "factual claim made in the hypothesis? Read the hypothesis carefully — "
    "what does it specifically claim? Then read the payload. Two outcomes:\n"
    "       (a) RELEVANT — payload speaks to the specific claim (matching "
    "instrument, timeframe, regime, metric). Score the delta as if you "
    "were doing a small Bayesian update.\n"
    "       (b) IRRELEVANT — payload is off-topic, generic, an empty/error "
    "response, or about a different asset/timeframe than the hypothesis "
    "concerns. In this case you MUST return `delta=0.0`, "
    "`confidence<=0.20`, `direction='neutral'`, AND `irrelevant=true`. Do "
    "NOT default to small-negative just because the payload is noisy or "
    "ambiguous — that contaminates the posterior. Neutral-with-low-"
    "confidence is the correct response when the evidence simply doesn't "
    "bear on the claim.\n"
    "  2. direction (only if relevant): 'support' (result makes the "
    "hypothesis more likely), 'contradict' (less likely), or 'neutral' (no "
    "informative update).\n"
    "  3. magnitude: a signed posterior delta in [-0.30, +0.30]. Positive "
    "= supports; negative = contradicts. 0.0 = neutral. Be calibrated: "
    "0.05-0.10 for weak signals, 0.10-0.20 for clear ones, 0.20-0.30 only "
    "for decisive evidence. If irrelevant, delta MUST be 0.0.\n"
    "  4. confidence: your own certainty in the score (0..1). 0.5 = "
    "could-go-either-way; 0.9 = highly confident. If irrelevant, "
    "confidence <= 0.20.\n"
    "  5. citation_claim_ids: any claim_id / id / record_id strings you "
    "see inside the tool result payload that this score cites. Empty list "
    "is fine if the payload has none.\n"
    "  6. rationale: <=300 characters explaining WHY this is support / "
    "contradict / neutral / irrelevant, citing one specific number or "
    "fact from the payload (or noting what's missing).\n\n"
    "Output STRICT JSON (no prose outside the JSON):\n"
    "{\n"
    '  "irrelevant": true | false,\n'
    '  "direction": "support" | "contradict" | "neutral",\n'
    '  "posterior_delta": -0.30..0.30,\n'
    '  "confidence": 0.0..1.0,\n'
    '  "citation_claim_ids": ["..."],\n'
    '  "rationale": "<=300 chars"\n'
    "}\n\n"
    "Rules:\n"
    "  - A failed/empty/error payload is irrelevant (irrelevant=true, "
    "delta=0, confidence<=0.20). Do not invent support.\n"
    "  - When the payload IS directly relevant and the tool result "
    "blatantly disagrees with the hypothesis (numbers, direction, "
    "regime), use contradict with a calibrated negative delta.\n"
    "  - Never go outside [-0.30, +0.30]. One evidence cannot decide "
    "the case alone.\n"
    "  - DO NOT default to small-negative deltas for ambiguous / generic "
    "/ tangentially-related evidence. That bias contaminates posteriors. "
    "Mark such evidence irrelevant and let the BFS surface that fact."
)


def _build_user_prompt(
    hypothesis_text: str,
    question_text: str,
    tool_uri: str,
    tool_result: dict[str, Any],
    source_health: dict[str, Any],
    prior_posterior: float,
) -> str:
    """Render the per-call user prompt with hypothesis, question, payload,
    and source health summary."""
    # Truncate payload to keep prompts small (Haiku context, cost).
    try:
        payload_json = json.dumps(tool_result, default=str)
    except Exception:
        payload_json = str(tool_result)
    if len(payload_json) > 4000:
        payload_json = payload_json[:4000] + "...[truncated]"

    sh_lines: list[str] = []
    for sref, sh in (source_health or {}).items():
        if isinstance(sh, dict):
            status = sh.get("status", "unknown")
            last = sh.get("last_successful_at") or sh.get("last_success_at") or "?"
            sh_lines.append(f"  - {sref}: status={status}, last_successful_at={last}")
        else:
            sh_lines.append(f"  - {sref}: {sh}")
    sh_block = "\n".join(sh_lines) if sh_lines else "  (no source health reported)"

    return (
        f"## Hypothesis under investigation\n{hypothesis_text}\n\n"
        f"## Specialist's question for this evidence\n{question_text}\n\n"
        f"## Prior posterior (before this evidence)\n{prior_posterior:.3f}\n\n"
        f"## Tool just dispatched\n{tool_uri}\n\n"
        f"## Tool result payload (JSON)\n```\n{payload_json}\n```\n\n"
        f"## Source health for tools cited\n{sh_block}\n\n"
        f"Score this evidence. Output the JSON object only."
    )


# ============================================================================
# JSON repair (same tolerant parser as judge.py / runner.py)
# ============================================================================

def _parse_json(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        return {}
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        pass
    # Fenced blocks
    fences = re.findall(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL)
    for chunk in fences:
        try:
            obj = json.loads(chunk.strip())
            if isinstance(obj, dict):
                return obj
        except Exception:
            continue
    # Greedy braces
    start = text.find("{")
    end = text.rfind("}")
    if 0 <= start < end:
        try:
            obj = json.loads(text[start:end + 1])
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    return {}


# ============================================================================
# Chat bridge (sync wrapper around tic.desk.models.chat — same shape as
# brief/composer.py::_run_chat_sync).
# ============================================================================

def _run_chat_sync(chat_fn: Any, model: str, system: str, user: str) -> dict[str, Any]:
    """Call chat() with `fallback=None` (we walk the chain ourselves)."""
    async def _do() -> dict[str, Any]:
        return await chat_fn(model, system, user, fallback=None)

    try:
        asyncio.get_running_loop()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(asyncio.run, _do())
            return fut.result(timeout=120)
    except RuntimeError:
        return asyncio.run(_do())


# ============================================================================
# Public API
# ============================================================================

def _coerce_delta(raw: Any, direction: str) -> float:
    """Pull a float in [-0.30, +0.30] out of the LLM's claimed delta.
    Falls back to a direction-sign default if the model returned a bad
    number or omitted it."""
    try:
        d = float(raw)
    except (TypeError, ValueError):
        d = 0.0
    # Hard clamp per design contract.
    d = max(-MAX_DELTA_PER_EVIDENCE, min(MAX_DELTA_PER_EVIDENCE, d))
    # If the model says contradict but returned a positive delta, flip the
    # sign — and vice versa. Neutral with non-zero delta gets zeroed.
    if direction == "support" and d < 0:
        d = abs(d)
    elif direction == "contradict" and d > 0:
        d = -abs(d)
    elif direction == "neutral":
        d = 0.0
    return d


def _extract_citation_ids_from_payload(payload: Any) -> list[str]:
    """Best-effort scan for claim/record/id-looking strings in the payload.
    Used when the LLM omits citation_claim_ids — we pull common keys so
    downstream link_evidence still has something to cite."""
    if not isinstance(payload, dict):
        return []
    found: list[str] = []
    for key in ("claim_id", "claim_ids", "id", "ids", "record_id", "record_ids"):
        v = payload.get(key)
        if isinstance(v, str) and v:
            found.append(v)
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, str) and item:
                    found.append(item)
    # Dedupe preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for s in found:
        if s not in seen:
            out.append(s)
            seen.add(s)
    return out[:10]


def _stale_source_flags(source_health: dict[str, Any]) -> list[str]:
    """Return ['stale_source'] if any cited source is stale/never_succeeded."""
    if not isinstance(source_health, dict):
        return []
    for sref, sh in source_health.items():
        if not isinstance(sh, dict):
            continue
        status = (sh.get("status") or "").lower()
        if status in ("stale", "never_succeeded", "degraded_stale"):
            return ["stale_source"]
    return []


def score_evidence(
    hypothesis_text: str,
    question_text: str,
    tool_uri: str,
    tool_result: dict[str, Any],
    source_health: dict[str, Any],
    prior_posterior: float,
    *,
    model: str = "anthropic:claude-haiku-4-5",
) -> EvidenceScore:
    """Score a single tool result against the hypothesis it was meant to
    bear on. Returns a signed posterior delta plus citations and a
    contradiction flag.

    Args:
      hypothesis_text: the full hypothesis claim text under investigation.
      question_text: the specific BFS question this tool was answering.
      tool_uri: the dispatched URI (logged in the prompt for context).
      tool_result: the actual `ToolResult.result` payload (a dict).
      source_health: map sref -> {status, last_successful_at} for the
        tool's underlying sources.
      prior_posterior: hypothesis posterior before this evidence (0..1).
      model: starting model. The function walks the fallback chain on
        any provider failure.

    Returns:
      EvidenceScore with posterior_delta clamped to [-0.30, +0.30].

    Raises:
      EvidenceUnavailableError if every provider in the fallback chain
      failed or returned unparseable JSON. The BFS loop should skip this
      evidence (do NOT call link_evidence / update_posterior) when this
      raises.
    """
    _ensure_tic_on_path()
    try:
        from tic.desk.models import chat as _chat  # type: ignore
    except Exception as e:
        raise EvidenceUnavailableError(
            f"tic.desk.models.chat unavailable: {e!s}"
        ) from e

    system = EVIDENCE_SYSTEM_PROMPT
    user = _build_user_prompt(
        hypothesis_text=hypothesis_text,
        question_text=question_text,
        tool_uri=tool_uri,
        tool_result=tool_result if isinstance(tool_result, dict) else {"raw": tool_result},
        source_health=source_health or {},
        prior_posterior=float(prior_posterior),
    )

    chain = _build_provider_chain(model)
    last_error: Optional[str] = None
    for m in chain:
        try:
            res = _run_chat_sync(_chat, m, system, user)
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
        if not parsed:
            last_error = f"{m}: unparseable_json"
            continue

        # Pull fields.
        direction = str(parsed.get("direction") or "neutral").lower()
        if direction not in ("support", "contradict", "neutral"):
            direction = "neutral"
        delta = _coerce_delta(parsed.get("posterior_delta"), direction)
        try:
            conf = float(parsed.get("confidence", 0.5))
        except (TypeError, ValueError):
            conf = 0.5
        conf = max(0.0, min(1.0, conf))

        # Neutral-baseline calibration (Tweak 2, 2026-05-20). The LLM now
        # reports an `irrelevant` flag for off-topic / generic payloads.
        # Enforce the contract on our side: irrelevant => delta=0,
        # confidence<=0.20, direction='neutral'. This prevents the
        # "small-negative drift on ambiguous evidence" pathology from
        # contaminating the posterior.
        irr_raw = parsed.get("irrelevant", False)
        if isinstance(irr_raw, str):
            irrelevant = irr_raw.strip().lower() in ("true", "1", "yes", "y")
        else:
            irrelevant = bool(irr_raw)
        if irrelevant:
            delta = 0.0
            direction = "neutral"
            if conf > 0.20:
                conf = 0.20

        citations_raw = parsed.get("citation_claim_ids") or []
        if isinstance(citations_raw, str):
            citations_raw = [citations_raw]
        citations = [str(c) for c in citations_raw if c]
        if not citations:
            citations = _extract_citation_ids_from_payload(tool_result)
        rationale = str(parsed.get("rationale") or "").strip()
        if len(rationale) > RATIONALE_MAX_CHARS:
            rationale = rationale[: RATIONALE_MAX_CHARS - 3] + "..."

        flags = _stale_source_flags(source_health)
        # If the payload itself signals an error, tag a quality flag and
        # zero the delta — failed dispatches shouldn't move posterior.
        # Also force-irrelevant so BFS aggregates it correctly.
        if isinstance(tool_result, dict) and tool_result.get("error"):
            flags = sorted(set(flags + ["tool_payload_error"]))
            delta = 0.0
            direction = "neutral"
            irrelevant = True
            if conf > 0.20:
                conf = 0.20
        if irrelevant and "irrelevant_evidence" not in flags:
            flags = sorted(set(flags + ["irrelevant_evidence"]))

        model_used = res.get("model_used") or m
        cost = _estimate_cost_usd(model_used, system, user, text)
        return EvidenceScore(
            posterior_delta=delta,
            citation_claim_ids=citations,
            contradicts=(direction == "contradict"),
            confidence=conf,
            rationale=rationale,
            quality_flags=flags,
            cost_usd=cost,
            model_used=str(model_used),
            irrelevant_evidence_share=(1.0 if irrelevant else 0.0),
        )

    raise EvidenceUnavailableError(
        f"All {len(chain)} providers in the evidence-scorer fallback chain "
        f"failed. Last error: {last_error}. Chain: {chain}"
    )


# ============================================================================
# Inline smoke tests (Tweak 2 calibration)
# ============================================================================

if __name__ == "__main__":
    # These tests exercise the deterministic parts of the scorer: the JSON
    # contract, the irrelevant-baseline enforcement, the delta clamps, and
    # the aggregation helper. They DO NOT hit the LLM fallback chain — that
    # requires the brief_experiments tic.desk.models sibling and live keys,
    # which we don't want to depend on in a smoke test.
    import sys as _sys

    failures: list[str] = []

    def _check(name: str, cond: bool, detail: str = "") -> None:
        if cond:
            print(f"  OK   {name}")
        else:
            failures.append(f"{name} :: {detail}")
            print(f"  FAIL {name} :: {detail}")

    print("[1] _coerce_delta clamps to [-0.30, +0.30]")
    _check("clamp upper", _coerce_delta(0.99, "support") == 0.30, "got "
           f"{_coerce_delta(0.99, 'support')}")
    _check("clamp lower", _coerce_delta(-0.99, "contradict") == -0.30,
           f"got {_coerce_delta(-0.99, 'contradict')}")
    _check("neutral zeros", _coerce_delta(0.12, "neutral") == 0.0,
           f"got {_coerce_delta(0.12, 'neutral')}")
    _check("contradict sign flip",
           _coerce_delta(0.10, "contradict") == -0.10,
           f"got {_coerce_delta(0.10, 'contradict')}")
    _check("support sign flip",
           _coerce_delta(-0.10, "support") == 0.10,
           f"got {_coerce_delta(-0.10, 'support')}")

    print("[2] _parse_json tolerates fences / prose")
    obj1 = _parse_json('```json\n{"a": 1}\n```')
    _check("fenced", obj1 == {"a": 1}, f"got {obj1}")
    obj2 = _parse_json('prose before {"b": 2} prose after')
    _check("greedy braces", obj2 == {"b": 2}, f"got {obj2}")
    obj3 = _parse_json("")
    _check("empty", obj3 == {}, f"got {obj3}")

    print("[3] EvidenceScore.irrelevant_evidence_share default + aggregator")
    s_rel = EvidenceScore(
        posterior_delta=0.10, citation_claim_ids=[], contradicts=False,
        confidence=0.7, rationale="relevant", irrelevant_evidence_share=0.0,
    )
    s_irr = EvidenceScore(
        posterior_delta=0.0, citation_claim_ids=[], contradicts=False,
        confidence=0.15, rationale="off-topic",
        irrelevant_evidence_share=1.0,
    )
    _check("aggregate empty", irrelevant_evidence_share([]) == 0.0,
           f"got {irrelevant_evidence_share([])}")
    share = irrelevant_evidence_share([s_rel, s_rel, s_irr, s_irr])
    _check("aggregate half", share == 0.5, f"got {share}")
    share_all = irrelevant_evidence_share([s_irr, s_irr])
    _check("aggregate all-irr", share_all == 1.0, f"got {share_all}")

    print("[4] Prompt explicitly requires irrelevant-baseline behavior")
    _check("prompt mentions irrelevant",
           '"irrelevant"' in EVIDENCE_SYSTEM_PROMPT,
           "prompt missing irrelevant field")
    _check("prompt forbids small-negative default",
           "DO NOT default to small-negative" in EVIDENCE_SYSTEM_PROMPT,
           "prompt missing skeptical-drift guard")
    _check("prompt requires confidence<=0.20 when irrelevant",
           "confidence<=0.20" in EVIDENCE_SYSTEM_PROMPT
           or "confidence <= 0.20" in EVIDENCE_SYSTEM_PROMPT,
           "prompt missing confidence cap")

    if failures:
        print(f"\n{len(failures)} failure(s):")
        for f in failures:
            print(f"  - {f}")
        _sys.exit(1)
    print("\nAll smoke checks passed.")
    _sys.exit(0)

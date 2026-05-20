"""Judge-side primitives for the debate runner.

Per v2 §6 line 681:
  "Opus judges DeepSeek vs Grok; DeepSeek judges Claude vs GPT;
   GPT judges Grok vs Moonshot."

So the judge MUST come from a different provider family than both
participants. We read each participant's persona model from the
specialist_states.payload.persona_model field if present, else fall back
to a hardcoded specialist-to-model map for the synthetic test specialists
(noob_fade_v2, microstructure_v3, etc.).

Provider family is derived from the model id's prefix:
  anthropic:* -> anthropic
  openai:*    -> openai
  xai:*       -> xai
  deepseek:*  -> deepseek
  moonshot:*  -> moonshot
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional


# Fallback specialist -> model map used when specialist_states has no
# persona_model field set. This list is intentionally tiny — production
# specialists will write their own persona_version + model into the
# specialist_states row at hydrate time.
DEFAULT_SPECIALIST_MODEL_MAP: dict[str, str] = {
    "macro_regime_v2":    "anthropic:claude-sonnet-4-6",
    "macro_regime_v3":    "anthropic:claude-sonnet-4-6",
    "microstructure_v3":  "openai:gpt-5.5",
    "microstructure_v4":  "openai:gpt-5.5",
    "noob_fade_v2":       "xai:grok-4",
    "funding_regime_v1":  "deepseek:v4-pro",
    "options_flow_v1":    "moonshot:v1-32k",
}


def _provider_family(model_id: str) -> str:
    """Derive provider family from `provider:model` id. `anthropic:...` ->
    `anthropic`."""
    if ":" in model_id:
        return model_id.split(":", 1)[0]
    return model_id


def _persona_model_for_specialist(
    conn: sqlite3.Connection,
    specialist_id: str,
) -> str:
    """Look up the most recent open persona row for this specialist; pull
    payload.persona_model. Falls back to DEFAULT_SPECIALIST_MODEL_MAP."""
    row = conn.execute(
        "SELECT state_json FROM specialist_states "
        "WHERE specialist_id = ? AND state_kind = 'persona' "
        "AND transaction_to IS NULL "
        "ORDER BY transaction_from DESC LIMIT 1",
        (specialist_id,),
    ).fetchone()
    if row is not None:
        try:
            state = json.loads(row["state_json"])
            if isinstance(state, dict):
                pm = state.get("persona_model")
                if isinstance(pm, str) and pm:
                    return pm
        except Exception:
            pass
    return DEFAULT_SPECIALIST_MODEL_MAP.get(
        specialist_id, "anthropic:claude-sonnet-4-6"
    )


def _pick_judge_provider(
    participants: list[str],
    conn: Optional[sqlite3.Connection] = None,
    preferred_provider: Optional[str] = None,
) -> tuple[str, str]:
    """Return (judge_model_id, judge_provider_family).

    Picks a judge from a family different from BOTH participants' families.
    Default tier: claude-sonnet-4-6 (cheapest reliable Anthropic). If both
    participants are anthropic, picks gpt-5.5; if both are openai, picks
    claude-sonnet-4-6; etc.

    If `preferred_provider` is supplied (e.g. "anthropic"), we use it iff
    it's distinct from both participants. Otherwise we fall through to the
    default-pick table.
    """
    # Default candidate ladder per family (smallest -> reasonable).
    family_pick: dict[str, str] = {
        "anthropic": "anthropic:claude-sonnet-4-6",
        "openai":    "openai:gpt-5.5",
        "xai":       "xai:grok-4",
        "deepseek":  "deepseek:v4-pro",
        "moonshot":  "moonshot:v1-32k",
    }

    participant_families: list[str] = []
    if conn is not None:
        for p in participants:
            participant_families.append(_provider_family(_persona_model_for_specialist(conn, p)))
    else:
        for p in participants:
            participant_families.append(_provider_family(DEFAULT_SPECIALIST_MODEL_MAP.get(p, "anthropic:claude-sonnet-4-6")))

    p_set = set(participant_families)

    # If a preferred provider is set and distinct from participants, honor it.
    if preferred_provider and preferred_provider not in p_set:
        model = family_pick.get(preferred_provider)
        if model:
            return model, preferred_provider

    # Try each family in v2's spirit: Opus first (Anthropic), then GPT, then
    # DeepSeek, then Grok, then Moonshot. Pick the first one not used by
    # either participant.
    ladder = ["anthropic", "openai", "deepseek", "xai", "moonshot"]
    for fam in ladder:
        if fam not in p_set:
            return family_pick[fam], fam

    # Both participants somehow span 5 families — fall back to anthropic.
    return family_pick["anthropic"], "anthropic"


# ============================================================================
# Prompt builder
# ============================================================================

JUDGE_SYSTEM_PROMPT = (
    "You are an impartial debate judge for a quantitative trading research desk. "
    "Two specialists have presented conflicting arguments about a market claim or trade idea. "
    "Your job is to:\n"
    "  1. Pick a winner (or declare a tie) based on evidence quality, falsifiability, "
    "and resilience to the other side's strongest counter.\n"
    "  2. State your confidence (0..1) — be calibrated, not extreme.\n"
    "  3. Cite the deciding evidence by claim_id / tool_call_id.\n"
    "  4. Recommend ONE follow_up_action: downgrade_claim, supersede_hypothesis, "
    "cut_size_pct, or null if no action needed.\n"
    "  5. List any tool URIs you'd want run before next cycle to settle "
    "remaining uncertainty (max 3).\n\n"
    "Output STRICTLY in this JSON schema (no prose outside the JSON):\n"
    "{\n"
    '  "winner": "<agent_id>" | null,\n'
    '  "confidence": 0.0..1.0,\n'
    '  "rationale": "<=150 words",\n'
    '  "follow_up_action": null | {"type": "downgrade_claim"|"supersede_hypothesis"|"cut_size_pct", '
    '"target_id": "...", "new_prob": 0.0..1.0?, "factor": 0.0..1.0?},\n'
    '  "required_new_tool_calls": ["tic://tool/..."],\n'
    '  "judge_uncertainty": "<=40 words"\n'
    "}\n"
    "Do NOT inject opinions outside the evidence. If both sides are weak, "
    "say so and recommend more data."
)


def _build_judge_prompt(
    debate: Any,                              # talis_desk.debate.model.Debate
    triggering_claim: dict[str, Any],
    arguments: list[Any],                     # list[DebateArgument]
    claims_resolved: dict[str, dict[str, Any]],
    source_health: dict[str, dict[str, Any]],
) -> tuple[str, str]:
    """Build (system, user) prompts for the judge.

    Args:
      debate: the Debate row.
      triggering_claim: {id, text, value?, source_ref?, quality_flags?}.
      arguments: ordered DebateArgument list (typically 2).
      claims_resolved: map from citation_id -> {text, source_ref, quality_flags}.
      source_health: map from source_ref -> {last_successful_at, status}.
    """
    lines: list[str] = []
    lines.append(f"## Debate {debate.id} — trigger: {debate.trigger_kind}")
    lines.append("")
    lines.append("### Triggering claim / idea")
    lines.append(f"- id: {triggering_claim.get('id') or debate.trigger_id}")
    if triggering_claim.get("text"):
        lines.append(f"- text: {triggering_claim['text']}")
    if triggering_claim.get("value") is not None:
        lines.append(f"- value: {triggering_claim['value']}")
    if triggering_claim.get("source_ref"):
        lines.append(f"- source_ref: {triggering_claim['source_ref']}")
    if triggering_claim.get("quality_flags"):
        lines.append(f"- quality_flags: {triggering_claim['quality_flags']}")
    lines.append("")
    lines.append("### Arguments")
    for arg in arguments:
        lines.append(f"#### Agent: {arg.agent_id} (persona {arg.persona_version})")
        lines.append("```")
        lines.append(arg.argument_md)
        lines.append("```")
        lines.append(f"falsifiable_crux: {arg.falsifiable_crux}")
        lines.append(f"citations: {', '.join(arg.citation_ids) or '(none)'}")
        lines.append("")
    if claims_resolved:
        lines.append("### Resolved citations")
        for cid, info in claims_resolved.items():
            lines.append(f"- {cid}: {info.get('text','(no text)')[:180]}")
            if info.get("source_ref"):
                lines.append(f"    source_ref: {info['source_ref']}")
            if info.get("quality_flags"):
                lines.append(f"    quality_flags: {info['quality_flags']}")
        lines.append("")
    if source_health:
        lines.append("### Source health")
        for sref, sh in source_health.items():
            ok = "OK" if sh.get("status") == "ok" else f"DEGRADED ({sh.get('status')})"
            lines.append(f"- {sref}: {ok}, last_successful_at={sh.get('last_successful_at')}")
        lines.append("")
    lines.append("### Instructions")
    lines.append(
        f"Render your verdict as JSON exactly per the schema. The participants "
        f"you may pick as winner are: {', '.join(debate.participants)}. "
        f"Set winner=null only if you genuinely cannot pick a side."
    )
    user = "\n".join(lines)
    return JUDGE_SYSTEM_PROMPT, user


# ============================================================================
# LLM call wrapper
# ============================================================================

class JudgeUnavailableError(RuntimeError):
    """Raised when every provider in the judge fallback chain has failed.
    NO stub verdicts — the desk must explicitly handle the unavailable case
    (e.g. retry next cycle, mark debate as 'expired') rather than silently
    using a deterministic fake."""


# Full multi-provider fallback chain — we try each in order until one returns
# a parseable JSON verdict. NEVER stub.
_JUDGE_FALLBACK_CHAIN = [
    "anthropic:claude-sonnet-4-6",
    "anthropic:claude-opus-4-7",
    "openai:gpt-5.5",
    "deepseek:v4-pro",
    "xai:grok-4",
    "moonshot:v1-32k",
    "perplexity:sonar-pro",
]


def _build_provider_chain(judge_model: str) -> list[str]:
    """Start with the requested judge_model, then walk the rest of the chain
    skipping that one (and any duplicates)."""
    seen = {judge_model}
    chain = [judge_model]
    for m in _JUDGE_FALLBACK_CHAIN:
        if m not in seen:
            chain.append(m); seen.add(m)
    return chain


async def _call_judge_llm_async(
    system: str,
    user: str,
    judge_model: str,
    fallback_model: str = "anthropic:claude-sonnet-4-6",  # legacy kwarg, ignored
) -> dict[str, Any]:
    """Call the judge LLM via talis_tic.desk.models.chat with a REAL
    multi-provider fallback chain. NO STUBS — if every provider in the chain
    returns empty/unparseable, raise JudgeUnavailableError so the caller
    knows the verdict is missing rather than silently faking one."""
    import sys
    sib = "/Users/udaikhattar/jarvis-ios/docs/research/brief_experiments"
    if sib not in sys.path:
        sys.path.insert(0, sib)
    from tic.desk.models import chat as _chat  # type: ignore

    chain = _build_provider_chain(judge_model)
    last_error: Optional[str] = None
    for i, model in enumerate(chain):
        try:
            # Disable chat()'s built-in fallback — WE walk the chain ourselves
            res = await _chat(model, system, user, fallback=None)
            text = (res.get("text") or "").strip()
            err = res.get("error")
            if err:
                last_error = f"{model}: {err}"
                continue
            if not text:
                last_error = f"{model}: empty_completion"
                continue
            parsed = _parse_judge_json(text)
            if not parsed:
                last_error = f"{model}: unparseable_json"
                continue
            parsed["judge_model"] = res.get("model_used", model)
            parsed["judge_provider"] = res.get("provider", model.split(":", 1)[0])
            parsed["_fallback_position"] = i   # 0 = primary; >0 = fell back
            return parsed
        except Exception as e:  # noqa: BLE001
            last_error = f"{model}: {e!s}"
            continue

    raise JudgeUnavailableError(
        f"All {len(chain)} providers in the judge fallback chain failed. "
        f"Last error: {last_error}. Chain: {chain}"
    )


def _call_judge_llm(
    system: str,
    user: str,
    judge_model: str,
    fallback_model: str = "anthropic:claude-sonnet-4-6",
) -> dict[str, Any]:
    """Sync wrapper around the async judge call. Handles the case where the
    caller is already inside an event loop (e.g. Jupyter)."""
    import asyncio
    try:
        # Will raise RuntimeError if no running loop
        asyncio.get_running_loop()
        # Inside a loop: drop to a task in a worker thread
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(
                asyncio.run,
                _call_judge_llm_async(system, user, judge_model, fallback_model),
            )
            return fut.result(timeout=120)
    except RuntimeError:
        # No running loop — safe to use asyncio.run.
        return asyncio.run(_call_judge_llm_async(system, user, judge_model, fallback_model))


def _parse_judge_json(text: str) -> dict[str, Any]:
    """Pull a JSON object out of the model's response.

    Tolerates: bare JSON, JSON inside ```json``` fences, prose-before-JSON.
    Returns {} on failure.
    """
    text = (text or "").strip()
    # Try direct parse
    try:
        return json.loads(text)
    except Exception:
        pass
    # Try fenced
    if "```" in text:
        chunks = text.split("```")
        for c in chunks:
            c = c.strip()
            if c.startswith("json"):
                c = c[4:].strip()
            try:
                return json.loads(c)
            except Exception:
                continue
    # Try first-brace
    start = text.find("{")
    end = text.rfind("}")
    if 0 <= start < end:
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            pass
    return {}

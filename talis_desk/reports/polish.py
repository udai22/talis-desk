"""Stage 5 — Copy-edit polish.

ONE cheap Haiku call. Prose-only polish:
  * remove filler ("it should be noted that...", "in conclusion")
  * tighten hedge-fund PM tone
  * standardize formatting (headers, bullets, em-dashes)

DOES NOT add or remove substantive claims. Does NOT add or remove
paragraphs. DOES NOT change citation tokens.

# Contract

  * Input: the polished body markdown (post-revision).
  * Output: (polished_body_md, cost_usd). If every provider in the
    Haiku-first fallback chain fails, returns (input_body_md, 0.0) and
    records nothing — we never fabricate prose.
  * The function NEVER raises — polish is purely cosmetic, so on total
    chain failure the original body is returned untouched.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import re
from typing import Any, Optional

from .._tic_config import ensure_tic_on_path as _ensure_tic_on_path


logger = logging.getLogger(__name__)


# Haiku first for cost; fall back to slightly heavier Sonnet/Flash, then
# bigger models as a last resort. NEVER stub.
_POLISH_FALLBACK_CHAIN: list[str] = [
    "anthropic:claude-haiku-4-5",
    "deepseek:v4-flash",
    "anthropic:claude-sonnet-4-6",
    "deepseek:v4-pro",
    "openai:gpt-4o",
    "openai:gpt-5.5",
    "xai:grok-3",
]


_COST_PER_MTOK: dict[str, float] = {
    "anthropic:claude-haiku-4-5": 1.2,
    "anthropic:claude-sonnet-4-6": 6.0,
    "anthropic:claude-opus-4-7": 18.0,
    "openai:gpt-5.5": 12.0,
    "openai:gpt-4o": 8.0,
    "xai:grok-4": 7.0,
    "xai:grok-3": 3.0,
    "deepseek:v4-pro": 1.2,
    "deepseek:v4-flash": 0.3,
    "moonshot:v1-32k": 2.0,
    "perplexity:sonar-pro": 5.0,
}


def _build_chain(primary: str) -> list[str]:
    seen: set[str] = {primary}
    chain: list[str] = [primary]
    for m in _POLISH_FALLBACK_CHAIN:
        if m not in seen:
            chain.append(m)
            seen.add(m)
    return chain


def _estimate_cost(model: str, system: str, user: str, completion: str) -> float:
    rate = _COST_PER_MTOK.get(model, 1.5)
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


_POLISH_SYSTEM = (
    "You are a senior editor at an institutional research desk (think "
    "Goldman, JPM, Citadel desk research). Polish this report for PROSE "
    "QUALITY ONLY:\n"
    "  1. Remove filler phrases ('it should be noted that', 'in "
    "conclusion', 'as we have seen', 'going forward').\n"
    "  2. Tighten hedge-fund PM tone — declarative, no hedging caveats "
    "unless materially uncertain.\n"
    "  3. Standardize formatting: consistent header capitalization, "
    "consistent bullet structure, em-dashes for asides.\n"
    "  4. Fix passive voice where it weakens an assertion.\n\n"
    "STRICT RULES — DO NOT:\n"
    "  - Add, remove, or modify ANY numerical claim.\n"
    "  - Add, remove, or modify ANY citation token like [claim:abc] or [tc:xyz].\n"
    "  - Add or remove any section / heading.\n"
    "  - Add or remove any paragraph.\n"
    "  - Change the structural meaning of any sentence.\n\n"
    "Output the polished markdown ONLY. No commentary, no preamble. "
    "Begin with the same first heading as the input."
)


# Citation tokens we must preserve verbatim.
_CITATION_RE = re.compile(r"\[(?:claim|tc):[a-zA-Z0-9_\-]+\]")


def _citations_preserved(before: str, after: str) -> bool:
    """All [claim:...] / [tc:...] tokens in `before` must appear in
    `after`. We're conservative: if any citation is dropped, the polish
    is considered destructive and the original is kept."""
    before_set = set(_CITATION_RE.findall(before or ""))
    after_set = set(_CITATION_RE.findall(after or ""))
    if not before_set:
        return True
    # Allow polish to drop nothing — every citation must survive.
    return before_set.issubset(after_set)


def polish_prose(
    body_md: str,
    *,
    model: str = "anthropic:claude-haiku-4-5",
    max_tokens: int = 4000,
) -> tuple[str, float]:
    """Polish the report body via a cheap LLM call.

    Returns (polished_body_md, cost_usd). On any failure (LLM chain
    exhausted, output much shorter than input, citations dropped), the
    function returns (body_md, 0.0) untouched — polish is purely
    cosmetic so we never fail the pipeline over it.
    """
    body_md = body_md or ""
    if not body_md.strip():
        return body_md, 0.0

    try:
        _ensure_tic_on_path()
        from tic.desk.models import chat as _chat  # type: ignore
    except Exception as e:  # noqa: BLE001
        logger.info("polish_prose: tic.desk.models.chat unavailable: %s", e)
        return body_md, 0.0

    chain = _build_chain(model)
    user = (
        "## Report body to polish\n\n"
        f"{body_md.strip()}\n\n"
        "## Instruction\n"
        "Polish for prose quality only per the system prompt. Output the "
        "polished markdown ONLY — no preamble."
    )

    for m in chain:
        try:
            res = _run_chat_sync(_chat, m, _POLISH_SYSTEM, user,
                                  max_tokens=max_tokens)
        except Exception as e:  # noqa: BLE001
            logger.info("polish_prose: %s chat_call_failed: %s", m, e)
            continue
        text = (res.get("text") or "").strip()
        err = res.get("error")
        if err or not text:
            continue
        # Guard against destructive polish: output must be at least 70%
        # the length of the input AND must preserve all citations.
        if len(text) < int(0.7 * len(body_md)):
            logger.info(
                "polish_prose: %s output too short (%d < 0.7 * %d) — "
                "keeping original", m, len(text), len(body_md),
            )
            continue
        if not _citations_preserved(body_md, text):
            logger.info(
                "polish_prose: %s dropped citations — keeping original", m,
            )
            continue
        model_used = res.get("model_used") or m
        cost = _estimate_cost(model_used, _POLISH_SYSTEM, user, text)
        return text, cost

    # Total chain failure — return the original. Polish is cosmetic; we
    # don't penalize the pipeline.
    return body_md, 0.0


__all__ = ["polish_prose"]

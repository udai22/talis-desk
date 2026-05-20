"""Tier 1 — DeepSeek Flash scout swarm.

Runs `n` scouts in parallel via `asyncio.gather(...)` with a semaphore
concurrency cap (default 50 — avoids provider rate limits). Each scout:
  - Receives one `SeedCell` (entity x horizon x lens x bias)
  - Makes a single Flash-tier LLM call (~$0.0002 each)
  - Writes one hypothesis row to `hypotheses`
  - Posts one message to `bb_topic:scout_output:<lens>:<entity>`

Total cost target for 1000 scouts: <$0.25
Total wall time target: <5 min.

NO STUBS. If the Flash provider chain is exhausted, the scout returns a
ScoutOutput with `error` set and `quality_flag=['scout_provider_unavailable']`.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from ..agents_native import post_message
from ..coordination import (
    append_blackboard_event,
    attribute_failure,
    claim_task,
    complete_task,
    fail_task,
    start_task,
)
from .._tic_config import ensure_tic_on_path as _ensure_tic_on_path
from ..store import get_desk_store
from .seed_generator import SeedCell, record_coverage


logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------

# Provider fallback chain — Flash first (cheap), then increasingly capable
# (still cheap) tiers. Same pattern as runner._chat_sync.
DEFAULT_SCOUT_MODEL = "deepseek:v4-flash"
DEFAULT_SCOUT_FALLBACK = "anthropic:claude-haiku-4-5"

# Per-scout token budget.
SCOUT_MAX_TOKENS = 600

# Default concurrency cap.
DEFAULT_CONCURRENCY = 50

# Default per-cycle scout cost cap (kill switch).
DEFAULT_COST_CAP_USD = 1.0


# ----------------------------------------------------------------------
# Output dataclass
# ----------------------------------------------------------------------

@dataclass
class ScoutOutput:
    seed_id: str
    scout_id: str
    cycle_id: str
    entity: str
    lens: str
    horizon: str
    bias_mode: str
    hypothesis_text: str
    confidence: float
    rationale_brief: str
    suggested_tools: list[str]
    task_id: Optional[str] = None
    hypothesis_id: Optional[str] = None
    model_used: str = ""
    provider: str = ""
    cost_usd: float = 0.0
    elapsed_s: float = 0.0
    quality_flags: list[str] = field(default_factory=list)
    error: Optional[str] = None


# ----------------------------------------------------------------------
# Prompt construction
# ----------------------------------------------------------------------

SCOUT_SYSTEM_PROMPT = (
    "You are a Tier 1 scout on the Talis research desk. Your job is to "
    "propose ONE testable trading hypothesis grounded in the entity + "
    "horizon + lens + bias the orchestrator hands you. Return strict "
    "JSON only (no prose):\n\n"
    '{\n'
    '  "hypothesis": "<one sentence, falsifiable, max 280 chars>",\n'
    '  "confidence": 0.0,  // 0..1 prior\n'
    '  "rationale_brief": "<max 200 chars, must reference at least one '
    'concrete data point or tool you would invoke>",\n'
    '  "suggested_tools": ["<tic.tool.uri>", ...]  // 2-4 URIs copied from allowed_tool_candidates\n'
    '}\n\n'
    "Constraints:\n"
    "- The hypothesis MUST be specific to the entity + horizon.\n"
    "- The hypothesis MUST be falsifiable in the next horizon window.\n"
    "- Confidence MUST be calibrated (avoid uniform 0.5).\n"
    "- suggested_tools MUST be selected from allowed_tool_candidates exactly; "
    "never invent tool names.\n"
    "- bias_mode determines orientation: contrarian = bet against consensus, "
    "consensus_confirm = test the trade everyone is in, frontier = sample "
    "a thinly-traded edge, tail_risk = test a low-probability high-impact "
    "scenario, mean_reversion / momentum self-explanatory.\n"
    "- NO STUBS. If you cannot construct a falsifiable hypothesis from the "
    'cell, return {"hypothesis": "", "confidence": 0.0, '
    '"rationale_brief": "underspecified_cell", "suggested_tools": []}.'
)


def _build_user_prompt(seed: SeedCell) -> str:
    pieces = [
        f"entity={seed.entity}",
        f"horizon={seed.horizon}",
        f"lens={seed.lens}",
        f"bias_mode={seed.bias_mode}",
    ]
    if seed.theme:
        pieces.append(f"theme={seed.theme}")
    tool_candidates = seed.payload.get("tool_candidates") or []
    return (
        "Cell:\n  " + "\n  ".join(pieces) +
        "\n\nallowed_tool_candidates:\n  " +
        "\n  ".join(str(t) for t in tool_candidates[:8]) +
        "\n\nReturn one JSON object only."
    )


# ----------------------------------------------------------------------
# Single scout execution
# ----------------------------------------------------------------------

async def _run_one_scout(
    seed: SeedCell,
    cycle_id: str,
    model: str,
    fallback: str,
    cost_counter: dict[str, float],
    cost_cap: float,
) -> ScoutOutput:
    scout_id = f"scout_{uuid4().hex[:10]}"
    t0 = time.perf_counter()
    out = ScoutOutput(
        seed_id=seed.seed_id,
        scout_id=scout_id,
        cycle_id=cycle_id,
        entity=seed.entity,
        lens=seed.lens,
        horizon=seed.horizon,
        bias_mode=seed.bias_mode,
        hypothesis_text="",
        confidence=0.0,
        rationale_brief="",
        suggested_tools=[],
    )
    task_id = str(seed.payload.get("task_id") or "") if seed.payload else ""
    out.task_id = task_id or None
    if task_id:
        claimed = claim_task(
            task_id,
            agent_id=scout_id,
            specialist_id="tier1_scout",
        )
        if not claimed:
            out.error = "task_claim_failed"
            out.quality_flags.append("task_claim_failed")
            out.elapsed_s = time.perf_counter() - t0
            return out
        start_task(
            task_id,
            agent_id=scout_id,
            specialist_id="tier1_scout",
        )

    # Kill switch — collective scout cost cap.
    if cost_counter.get("total", 0.0) >= cost_cap:
        out.error = "scout_swarm_cost_cap_reached"
        out.quality_flags.append("scout_cost_cap")
        if task_id:
            fail_task(
                task_id,
                agent_id=scout_id,
                specialist_id="tier1_scout",
                reason=out.error,
            )
            attribute_failure(
                artifact_kind="task",
                artifact_id=task_id,
                failure_kind="scout_cost_cap",
                cycle_id=cycle_id,
                task_id=task_id,
                specialist_id="tier1_scout",
                severity="yellow",
                rationale="Tier-1 scout cost cap reached before this task could run.",
            )
        out.elapsed_s = time.perf_counter() - t0
        return out

    user_prompt = _build_user_prompt(seed)
    _ensure_tic_on_path()
    try:
        from tic.desk.models import chat as _chat  # type: ignore
    except Exception as e:
        out.error = f"tic_models_import_failed: {e}"
        out.quality_flags.append("scout_provider_unavailable")
        if task_id:
            fail_task(task_id, agent_id=scout_id, specialist_id="tier1_scout", reason=out.error)
            attribute_failure(
                artifact_kind="task",
                artifact_id=task_id,
                failure_kind="provider_unavailable",
                cycle_id=cycle_id,
                task_id=task_id,
                specialist_id="tier1_scout",
                severity="red",
                rationale=out.error,
            )
        out.elapsed_s = time.perf_counter() - t0
        return out

    try:
        res = await _chat(
            model, SCOUT_SYSTEM_PROMPT, user_prompt,
            max_tokens=SCOUT_MAX_TOKENS, fallback=fallback,
        )
    except Exception as e:
        out.error = f"chat_failed: {type(e).__name__}: {e}"
        out.quality_flags.append("scout_provider_unavailable")
        if task_id:
            fail_task(task_id, agent_id=scout_id, specialist_id="tier1_scout", reason=out.error)
            attribute_failure(
                artifact_kind="task",
                artifact_id=task_id,
                failure_kind="provider_unavailable",
                cycle_id=cycle_id,
                task_id=task_id,
                specialist_id="tier1_scout",
                severity="red",
                rationale=out.error,
            )
        out.elapsed_s = time.perf_counter() - t0
        return out

    text = (res.get("text") or "").strip()
    out.model_used = res.get("model_used", model)
    out.provider = res.get("provider", "?")
    if res.get("error"):
        out.error = res["error"]
        out.quality_flags.append("scout_provider_error")
    # Estimate cost (rough): Flash-tier ~$0.0002/scout. Use 600 tok cap.
    out.cost_usd = _flash_cost_estimate(out.model_used)
    cost_counter["total"] = cost_counter.get("total", 0.0) + out.cost_usd

    # Parse strict JSON. If parse fails, drop with quality flag.
    parsed = _extract_first_json(text)
    if not isinstance(parsed, dict):
        out.error = "scout_json_unparseable"
        out.quality_flags.append("scout_json_unparseable")
        if task_id:
            fail_task(task_id, agent_id=scout_id, specialist_id="tier1_scout", reason=out.error)
            attribute_failure(
                artifact_kind="task",
                artifact_id=task_id,
                failure_kind="json_unparseable",
                cycle_id=cycle_id,
                task_id=task_id,
                specialist_id="tier1_scout",
                severity="yellow",
                rationale="Scout returned unparseable JSON.",
            )
        out.elapsed_s = time.perf_counter() - t0
        return out
    out.hypothesis_text = (parsed.get("hypothesis") or "").strip()[:280]
    try:
        out.confidence = float(parsed.get("confidence") or 0.0)
    except Exception:
        out.confidence = 0.0
    out.confidence = max(0.0, min(1.0, out.confidence))
    out.rationale_brief = (parsed.get("rationale_brief") or "").strip()[:200]
    sugg = parsed.get("suggested_tools") or []
    if isinstance(sugg, list):
        out.suggested_tools = [str(x) for x in sugg[:6]]
    if not out.hypothesis_text:
        out.quality_flags.append("scout_empty_hypothesis")
        if task_id:
            fail_task(task_id, agent_id=scout_id, specialist_id="tier1_scout", reason="empty_hypothesis")
            attribute_failure(
                artifact_kind="task",
                artifact_id=task_id,
                failure_kind="empty_hypothesis",
                cycle_id=cycle_id,
                task_id=task_id,
                specialist_id="tier1_scout",
                severity="yellow",
                rationale="Scout produced no falsifiable hypothesis.",
            )

    # Persist hypothesis row + topic message — these are best-effort.
    try:
        out.hypothesis_id = _persist_hypothesis(out, seed)
    except Exception as e:
        logger.warning("scout %s: hypothesis persist failed: %s", scout_id, e)
    try:
        _post_scout_topic_message(out)
    except Exception as e:
        logger.warning("scout %s: topic post failed: %s", scout_id, e)
    try:
        record_coverage(cycle_id, seed, scout_id, published=False)
    except Exception as e:
        logger.debug("scout %s: coverage record failed: %s", scout_id, e)

    if out.hypothesis_text and task_id:
        append_blackboard_event(
            event_type="claim.proposed",
            cycle_id=cycle_id,
            topic=f"bb_topic:scout_output:{out.lens}:{out.entity}",
            task_id=task_id,
            claim_id=out.hypothesis_id or out.scout_id,
            agent_id=scout_id,
            specialist_id="tier1_scout",
            payload={
                "hypothesis_id": out.hypothesis_id,
                "hypothesis": out.hypothesis_text,
                "confidence": out.confidence,
                "suggested_tools": out.suggested_tools,
            },
        )
        complete_task(
            task_id,
            agent_id=scout_id,
            specialist_id="tier1_scout",
            payload={"hypothesis_id": out.hypothesis_id, "scout_id": scout_id},
        )

    out.elapsed_s = time.perf_counter() - t0
    return out


def _extract_first_json(text: str) -> Any:
    if not text:
        return None
    # Find the first balanced {...} block.
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


def _flash_cost_estimate(model: str) -> float:
    """Per-call cost in USD using the model's avg $/Mtok. Coarse but
    enough for kill-switch accounting."""
    rates = {
        "deepseek:v4-flash": 0.6,
        "deepseek:v4-pro": 1.2,
        "anthropic:claude-haiku-4-5": 1.2,
        "anthropic:claude-sonnet-4-6": 6.0,
        "openai:gpt-4o": 8.0,
    }
    rate = rates.get(model, 1.0)  # $/Mtok
    # ~ (in+out tokens) average. Flash scout = ~800 tokens roundtrip.
    return rate * 800 / 1_000_000


def _persist_hypothesis(out: ScoutOutput, seed: SeedCell) -> Optional[str]:
    """Insert one hypothesis row + return hypothesis_id.

    Matches the SOTA hypotheses schema: (id, cycle_id, specialist_id,
    title, hypothesis_text, status, posterior_prob, heat_score,
    novelty_score, entity_ids, source_ids, claim_ids, tool_call_ids,
    valid_from, transaction_from, payload).
    """
    if not out.hypothesis_text:
        return None
    conn = get_desk_store().conn
    cols = {r[1] for r in conn.execute("PRAGMA table_info(hypotheses)")}
    if not cols:
        return None
    hyp_id = f"hyp_{uuid4().hex[:10]}"
    now = datetime.now(timezone.utc).isoformat()
    # Build a tolerant insert — match SOTA schema first, fall back to
    # legacy column names if missing.
    title = out.hypothesis_text[:140]
    fields: dict[str, Any] = {
        "id": hyp_id,
        "specialist_id": "tier1_scout",
        "cycle_id": out.cycle_id,
        "title": title,
        "hypothesis_text": out.hypothesis_text,
        "text": out.hypothesis_text,  # legacy
        "instrument": out.entity,  # legacy
        "horizon": out.horizon,  # legacy
        "status": "active",
        "posterior_prob": out.confidence,
        "prior": out.confidence,  # legacy
        "posterior": out.confidence,  # legacy
        "heat_score": out.confidence,
        "novelty_score": 0.5,
        "entity_ids": json.dumps([out.entity]),
        "source_ids": json.dumps([]),
        "claim_ids": json.dumps([]),
        "tool_call_ids": json.dumps([]),
        "valid_from": now,
        "transaction_from": now,
        "payload": json.dumps({
            "scout_id": out.scout_id,
            "task_id": out.task_id,
            "seed_id": out.seed_id,
            "lens": out.lens,
            "bias_mode": out.bias_mode,
            "horizon": out.horizon,
            "rationale_brief": out.rationale_brief,
            "suggested_tools": out.suggested_tools,
            "model_used": out.model_used,
            "tier": "scout",
        }),
    }
    insertable = {k: v for k, v in fields.items() if k in cols}
    if "id" not in insertable:
        return None
    placeholders = ",".join("?" * len(insertable))
    col_names = ",".join(insertable.keys())
    try:
        conn.execute(
            f"INSERT INTO hypotheses ({col_names}) VALUES ({placeholders})",
            tuple(insertable.values()),
        )
        conn.commit()
    except sqlite3.OperationalError as e:
        logger.debug("hypothesis insert error: %s", e)
        return None
    return hyp_id


def _post_scout_topic_message(out: ScoutOutput) -> None:
    topic = f"bb_topic:scout_output:{out.lens}:{out.entity}"
    post_message(
        from_agent=f"scout:{out.scout_id}",
        to_agent_or_topic=topic,
        kind="observation",
        payload={
            "scout_id": out.scout_id,
            "seed_id": out.seed_id,
            "task_id": out.task_id,
            "cycle_id": out.cycle_id,
            "hypothesis_id": out.hypothesis_id,
            "hypothesis": out.hypothesis_text,
            "confidence": out.confidence,
            "entity": out.entity,
            "horizon": out.horizon,
            "lens": out.lens,
            "bias_mode": out.bias_mode,
            "rationale_brief": out.rationale_brief,
            "suggested_tools": out.suggested_tools,
            "quality_flags": out.quality_flags,
        },
        related_hypothesis_id=out.hypothesis_id,
        expires_in_hours=72,
        topic=topic,
    )


# ----------------------------------------------------------------------
# Public entry point
# ----------------------------------------------------------------------

async def _async_run_scouts(
    seeds: list[SeedCell],
    cycle_id: str,
    model: str,
    fallback: str,
    concurrency: int,
    cost_cap: float,
) -> list[ScoutOutput]:
    sem = asyncio.Semaphore(max(1, concurrency))
    cost_counter: dict[str, float] = {"total": 0.0}

    async def _bounded(seed: SeedCell) -> ScoutOutput:
        async with sem:
            return await _run_one_scout(
                seed, cycle_id, model, fallback,
                cost_counter, cost_cap,
            )

    tasks = [asyncio.create_task(_bounded(s)) for s in seeds]
    return await asyncio.gather(*tasks, return_exceptions=False)


def run_scouts(
    seeds: list[SeedCell],
    cycle_id: str,
    model: str = DEFAULT_SCOUT_MODEL,
    fallback: str = DEFAULT_SCOUT_FALLBACK,
    concurrency: int = DEFAULT_CONCURRENCY,
    cost_cap_usd: float = DEFAULT_COST_CAP_USD,
) -> list[ScoutOutput]:
    """Synchronous entry point — runs the scout swarm to completion.

    Handles being called from inside an event loop by trampolining to a
    worker-thread `asyncio.run`. Returns one ScoutOutput per seed.
    """
    if not seeds:
        return []

    async def _go() -> list[ScoutOutput]:
        return await _async_run_scouts(
            seeds, cycle_id, model, fallback, concurrency, cost_cap_usd,
        )

    try:
        asyncio.get_running_loop()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            return ex.submit(asyncio.run, _go()).result()
    except RuntimeError:
        return asyncio.run(_go())

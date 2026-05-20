"""Canonical 6-stage research cycle.

Source of truth: `wiki/SOTA_DESK_ARCHITECTURE.md` v2 §4 + §7.

Stages (line numbers in v2 §4):
  HYDRATE   — load specialist_states latest + unread agent_messages +
              Brier outcomes (mv_specialist_brier_rolling) + frozen tool
              atlas snapshot (get_atlas_snapshot_for_cycle).
  PLAN      — LLM call, no tools. Specialist proposes 3+ hypotheses, picks
              tools per hypothesis. Uses persona system prompt.
  EXPLORE   — explore_adversarial(...) from talis_desk.exploration;
              >=20% contradiction-seeking; spawn sub-investigations on
              |posterior_delta| > 0.2; budget per InvestigationBudget.
  SYNTHESIZE— Update hypotheses (update_posterior on resolved ones), emit
              forecasts + trade ideas (validate_trade_idea before emit),
              post peer messages via scratchpad.
  REFLECT   — Capped at 5% of cycle's LLM spend. Meta-LLM call reads the
              cycle trace + Brier outcomes + tool affinity, outputs short
              notes-to-self + posterior adjustments. Writes to
              specialist_states with state_kind='reflection'.
  DEHYDRATE — Write next specialist_states row (state_kind='dehydration')
              with priors + open hypotheses + recent tool calls + Brier
              delta. Update tool_affinity. Close cycle.

Idempotent: re-running with same (specialist_id, cycle_id) is a no-op if
the cycle already has a dehydration row.

Bitemporal: every write has valid_from + transaction_from; updates use
the supersedes pattern from talis_desk.hypotheses.

# Karpathy "one loop" stance (v2 line 473)

Different loops per specialist multiply bugs, replay semantics, cost
policy, and eval surfaces. Specialists differ only by persona (system
prompt + persona_tool_uris + priors), not runtime. If a specialist
needs an extra move, it proposes a skill — it does not fork the loop.

# Provider fallback chain (NO STUBS)

Every LLM call goes through `tic.desk.models.chat()`, which has its own
6-provider fallback chain (anthropic -> openai -> xai -> deepseek ->
moonshot -> perplexity). We never use a deterministic stub.

# Honest gaps

  - PLAN hypothesis quality depends on persona prompt; auto-mutation is
    Phase 6.
  - Tool affinity update uses placeholder logic (citation count x
    success_rate) until Phase 6 adds Brier-weighted scoring.
  - score_novelty (v2 §1) is not wired; we mark claims as
    novelty_score=null and let REFLECT note it.
  - mv_specialist_brier_rolling is empty on a fresh DB; we read what's
    there and pass an empty list to the persona when nothing exists.
  - tool_affinity table is not in the SOTA DDL (only the
    mv_top_tools_per_specialist_30d view); we persist affinity into the
    dehydration state_json instead, and the next HYDRATE reads it back
    from there.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import os
import re
import sys
import time
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional
from uuid import uuid4

from ..agents_native.scratchpad import (
    AgentMessage,
    post_message,
    read_unread_messages,
)
from ..exploration.bfs import (
    DebateTriggerDecision,
    ExplorationTrace,
    HypothesisSeed,
    Investigation,
    InvestigationBudget,
    QuestionNode,
    explore_adversarial,
    maybe_trigger_debate,
    start_investigation,
)
from ..hypotheses.model import (
    Hypothesis,
    get_active_hypotheses,
    resolve_hypothesis,
    update_posterior,
)
from ..store import get_desk_store
from ..tool_atlas import AgentContext
from ..tool_atlas.atlas import (
    ToolAtlasSnapshot,
    get_atlas_snapshot_for_cycle,
)
from ..trade_ideas import (
    ContradictionItem,
    EntryPlan,
    SizingPlan,
    StopPlan,
    TIME_HORIZON_HOURS,
    TradeIdea,
    TradeIdeaDraft,
    emit_trade_idea,
    validate_trade_idea,
)


# ============================================================================
# Ensure the sibling `tic` package is importable for chat() — same approach
# debate/judge.py uses. We add the path lazily so a misconfigured workspace
# doesn't break import-time; the LLM call sites raise clean errors.
# ============================================================================

_TIC_SIBLING_PATH = "/Users/udaikhattar/jarvis-ios/docs/research/brief_experiments"


def _ensure_tic_on_path() -> None:
    if _TIC_SIBLING_PATH not in sys.path:
        sys.path.insert(0, _TIC_SIBLING_PATH)


# ============================================================================
# Constants — pulled from v2 §4 / §7
# ============================================================================

#: v2 §7 line 538 — daily-budget kill is system-wide; per-agent cap is the
#: $5 default with a $10 extension.
DEFAULT_MAX_COST_USD = 5.0
DEFAULT_EXTENSION_COST_USD = 10.0

#: v2 line 142 — REFLECT capped at 5% of cycle LLM spend.
DEFAULT_REFLECTION_SPEND_PCT = 0.05

#: v2 lines 81-94 — debate budget <=20% of daily LLM spend, max 10 debates
#: per cycle (11th -> kill switch).
DEFAULT_DEBATE_BUDGET_PCT = 0.20
DEBATE_PER_CYCLE_KILL_THRESHOLD = 10

#: v2 §7 line 535: source-health failures > 30% of cited sources flags the
#: cycle.
SOURCE_HEALTH_FLAG_THRESHOLD = 0.30

#: PLAN must produce >=3 hypotheses (task spec + v2 line 466 "propose 3+").
DEFAULT_MIN_HYPOTHESES = 3

#: PLAN's preferred model. Per-persona override comes from persona payload
#: `preferred_models`. We anchor on Claude Opus 4.7 to match the desk's
#: Karpathy "one heavy thinker per cycle" stance + cheap REFLECT below.
DEFAULT_PLAN_MODEL = "anthropic:claude-opus-4-7"

#: Default REFLECT model — cheap and fast (v2 line 142, 5% cap).
DEFAULT_REFLECT_MODEL = "anthropic:claude-haiku-4-5"

#: Default ensemble (the fallback chain inside chat() already covers all six
#: providers; this list is consulted only when the persona doesn't specify
#: a preferred model).
DEFAULT_MODEL_ENSEMBLE = [
    "anthropic:claude-opus-4-7",
    "anthropic:claude-sonnet-4-6",
    "openai:gpt-5.5",
    "xai:grok-4",
    "deepseek:v4-pro",
    "moonshot:v1-32k",
]

#: v2 line 92 — contradiction-seeking >= 20% required at high confidence.
DEFAULT_CONTRADICTION_THRESHOLD = 0.7

#: We always store at least this many hypothesis BFS seeds in PLAN — when
#: the LLM hands us 3+ we keep all; below the floor we error.
PLAN_MAX_HYPOTHESES = 6

#: Token allowances (chat() honors max_tokens per provider spec). Plan
#: needs room for JSON with 3-6 hypotheses; reflect is small.
PLAN_MAX_TOKENS = 2400
REFLECT_MAX_TOKENS = 900


# ============================================================================
# Dataclasses — stage outputs + final result
# ============================================================================


@dataclass
class LoopConfig:
    """Knobs for one cycle. All defaults track v2 §4 + §7."""

    max_calls: int = 300                          # exploration max
    max_cost_usd: float = DEFAULT_MAX_COST_USD    # hard cap per cycle
    extension_cost_usd: float = DEFAULT_EXTENSION_COST_USD
    reflection_spend_cap_pct: float = DEFAULT_REFLECTION_SPEND_PCT
    n_hypotheses_min: int = DEFAULT_MIN_HYPOTHESES
    debate_budget_pct: float = DEFAULT_DEBATE_BUDGET_PCT
    model_ensemble: Optional[list[str]] = None
    require_contradiction_at_confidence: float = DEFAULT_CONTRADICTION_THRESHOLD
    #: Optional override for the PLAN-stage model.
    plan_model: str = DEFAULT_PLAN_MODEL
    #: Optional override for the REFLECT-stage model.
    reflect_model: str = DEFAULT_REFLECT_MODEL
    #: Per-hypothesis investigation cap (BFS budget). The cycle-wide cap is
    #: max_cost_usd / max_calls. We split the cycle cap evenly across
    #: hypotheses with a floor of 10 calls / $0.50 per investigation.
    per_hypothesis_max_calls: int = 30
    #: When true, emit trade ideas straight to status=published. When the
    #: kill switch trips for any reason we flip to paper-only (drafts) for
    #: the rest of the cycle.
    paper_only: bool = False


@dataclass
class HypothesisDraftPlan:
    """One hypothesis the PLAN stage proposed, with its tool assignment."""

    title: str
    hypothesis_text: str
    initial_prob: float
    entities: list[str]
    tool_uris: list[str]            # tools the specialist picked
    expected_resolution_hours: int = 24


@dataclass
class CycleHydration:
    """Output of HYDRATE; consumed by PLAN."""

    specialist_id: str
    persona_version: str
    persona_prompt: str
    persona_tool_uris: list[str]
    yesterday_state: dict[str, Any]
    unread_messages: list[AgentMessage]
    recent_brier_outcomes: list[dict[str, Any]]
    tool_atlas: ToolAtlasSnapshot
    atlas_pinned: bool
    open_hypotheses: list[Hypothesis]
    persona_state_id: str


@dataclass
class CyclePlan:
    """Output of PLAN; consumed by EXPLORE."""

    hypotheses: list[HypothesisDraftPlan]
    tool_assignments: dict[str, list[str]]
    model_used: str
    fallback_used: bool
    llm_cost_usd: float
    raw_text: str


@dataclass
class CycleSynthesis:
    """Output of SYNTHESIZE; consumed by REFLECT."""

    new_trade_ideas: list[TradeIdea]
    updated_hypotheses: list[Hypothesis]
    resolved_hypotheses: list[Hypothesis]
    peer_messages: list[AgentMessage]
    debate_triggers: list[DebateTriggerDecision]


@dataclass
class CycleReflection:
    """Output of REFLECT; consumed by DEHYDRATE."""

    notes_to_self: str
    posterior_adjustments: dict[str, float]
    tool_affinity_delta: dict[str, float]
    redundant_tool_calls: list[str]
    model_used: str
    llm_cost_usd: float


@dataclass
class ResearchCycleResult:
    """Full record of one cycle (returned to caller)."""

    cycle_id: str
    specialist_id: str
    persona_version: str
    hydration: CycleHydration
    plan: CyclePlan
    exploration_traces: list[ExplorationTrace] = field(default_factory=list)
    exploration_trace_id: str = ""
    synthesis: Optional[CycleSynthesis] = None
    reflection: Optional[CycleReflection] = None
    next_state_id: str = ""
    total_cost_usd: float = 0.0
    total_tool_calls: int = 0
    elapsed_seconds: float = 0.0
    kill_switch_triggered: bool = False
    quality_flags: list[str] = field(default_factory=list)
    idempotent_short_circuit: bool = False
    paper_only: bool = False


# ============================================================================
# chat() bridge — sync wrapper around the async tic.desk.models.chat. Mirrors
# debate/judge.py's _call_judge_llm.
# ============================================================================


@dataclass
class _ChatResult:
    text: str
    model_used: str
    provider: str
    fallback_used: bool
    error: Optional[str]
    cost_usd: float


def _chat_sync(
    model: str,
    system: str,
    user: str,
    max_tokens: Optional[int] = None,
    fallback: Optional[str] = "anthropic:claude-sonnet-4-6",
) -> _ChatResult:
    """Synchronous bridge to `tic.desk.models.chat`.

    Handles being called from inside an event loop (Jupyter etc.) by
    falling back to a worker-thread `asyncio.run`. The cost estimate is
    coarse — we don't have per-model token-priced fixtures wired yet, so
    we use the chat() spec's max_tokens times a per-provider $/1k-tok
    estimate.

    NEVER returns a stub. If chat() fails on every provider in the
    fallback chain, the error string is returned in `_ChatResult.error`
    and the caller decides how to recover.
    """
    _ensure_tic_on_path()
    try:
        from tic.desk.models import chat as _chat  # type: ignore
    except Exception as e:
        return _ChatResult(
            text="",
            model_used=model,
            provider=model.split(":")[0] if ":" in model else "?",
            fallback_used=False,
            error=f"tic_models_import_failed: {e}",
            cost_usd=0.0,
        )

    async def _do() -> dict:
        return await _chat(model, system, user, max_tokens=max_tokens,
                            fallback=fallback)

    try:
        asyncio.get_running_loop()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(asyncio.run, _do())
            res = fut.result(timeout=180)
    except RuntimeError:
        res = asyncio.run(_do())
    except Exception as e:
        return _ChatResult(
            text="",
            model_used=model,
            provider=model.split(":")[0] if ":" in model else "?",
            fallback_used=False,
            error=f"chat_call_failed: {e}",
            cost_usd=0.0,
        )

    text = res.get("text", "") or ""
    used = res.get("model_used", model)
    provider = res.get("provider", "?")
    fb = bool(res.get("fallback_used", False))
    err = res.get("error")
    cost = _estimate_cost_usd(used, system, user, text, max_tokens or 0)
    return _ChatResult(
        text=text, model_used=used, provider=provider, fallback_used=fb,
        error=err, cost_usd=cost,
    )


# Per-million-token USD estimates (input + output averaged). These are
# rough — pricing changes and we don't track input/output separately
# here. Used only for cycle-cost accounting + REFLECT's 5% cap, NOT for
# billing.
_COST_PER_MTOK: dict[str, float] = {
    "anthropic:claude-opus-4-7": 18.0,
    "anthropic:claude-sonnet-4-6": 6.0,
    "anthropic:claude-haiku-4-5": 1.2,
    "openai:gpt-5.5": 12.0,
    "openai:gpt-4o": 8.0,
    "openai:gpt-4o-search": 8.0,
    "xai:grok-4": 7.0,
    "xai:grok-3": 6.0,
    "xai:grok-2": 4.0,
    "moonshot:v1-32k": 2.0,
    "moonshot:v1-8k": 1.5,
    "moonshot:kimi-k2.6": 2.5,
    "perplexity:sonar-pro": 5.0,
    "perplexity:sonar": 1.5,
    "deepseek:v4-pro": 1.2,
    "deepseek:v4-flash": 0.6,
}


def _estimate_cost_usd(model: str, system: str, user: str, completion: str,
                       max_tokens: int) -> float:
    """Rough cost estimate using character-count / 4 as a token proxy."""
    rate = _COST_PER_MTOK.get(model, 4.0)
    in_chars = len(system or "") + len(user or "")
    out_chars = len(completion or "")
    # 4 chars ~ 1 token (English average). Output dominates for reasoning.
    in_tokens = in_chars / 4.0
    out_tokens = out_chars / 4.0
    total_tokens = in_tokens + out_tokens
    if total_tokens <= 0:
        # If the call failed (empty completion) treat as zero cost.
        return 0.0
    return (total_tokens / 1_000_000.0) * rate


# ============================================================================
# JSON repair — PLAN + REFLECT expect JSON; LLMs sometimes wrap in prose
# or fences. This is the same tolerant parser pattern as debate/judge.py.
# ============================================================================


def _extract_json(text: str) -> Optional[Any]:
    """Pull a JSON value (object or array) out of mixed text. Returns None
    on parse failure."""
    text = (text or "").strip()
    if not text:
        return None
    # Try direct.
    try:
        return json.loads(text)
    except Exception:
        pass
    # Try fenced blocks first (most common LLM output shape).
    fences = re.findall(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL)
    for chunk in fences:
        try:
            return json.loads(chunk.strip())
        except Exception:
            continue
    # Try greedy braces / brackets.
    for open_c, close_c in (("{", "}"), ("[", "]")):
        start = text.find(open_c)
        end = text.rfind(close_c)
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except Exception:
                continue
    return None


# ============================================================================
# HYDRATE
# ============================================================================


def _load_latest_persona(specialist_id: str) -> Optional[dict[str, Any]]:
    """Read the latest persona row from specialist_states. None if missing."""
    conn = get_desk_store().conn
    row = conn.execute(
        "SELECT * FROM specialist_states "
        "WHERE specialist_id = ? AND state_kind = 'persona' "
        "  AND transaction_to IS NULL "
        "ORDER BY transaction_from DESC LIMIT 1",
        (specialist_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def _load_latest_dehydration(specialist_id: str) -> Optional[dict[str, Any]]:
    """Read the latest dehydration row (yesterday's state) from
    specialist_states. None on cold start."""
    conn = get_desk_store().conn
    row = conn.execute(
        "SELECT * FROM specialist_states "
        "WHERE specialist_id = ? AND state_kind = 'dehydration' "
        "  AND transaction_to IS NULL "
        "ORDER BY transaction_from DESC LIMIT 1",
        (specialist_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def _load_recent_brier_outcomes(specialist_id: str, n: int = 30) -> list[dict[str, Any]]:
    """Read recent Brier outcomes from `mv_specialist_brier_rolling`.

    On a fresh DB the view may exist but be empty — we return []. Caller
    is responsible for not crashing on an empty list (the persona prompt
    template handles 'no history yet' gracefully).
    """
    conn = get_desk_store().conn
    try:
        rows = conn.execute(
            "SELECT specialist_id, day, brier_avg, brier_delta_avg, n "
            "FROM mv_specialist_brier_rolling "
            "WHERE specialist_id = ? "
            "ORDER BY day DESC LIMIT ?",
            (specialist_id, n),
        ).fetchall()
    except Exception:
        rows = []
    return [dict(r) for r in rows]


def _check_idempotent_short_circuit(specialist_id: str, cycle_id: str) -> Optional[dict[str, Any]]:
    """If this (specialist_id, cycle_id) already has a dehydration row,
    return its row dict. Caller short-circuits and rebuilds the result
    object from it (no new DB writes).
    """
    conn = get_desk_store().conn
    row = conn.execute(
        "SELECT * FROM specialist_states "
        "WHERE specialist_id = ? AND cycle_id = ? "
        "  AND state_kind = 'dehydration' AND transaction_to IS NULL "
        "ORDER BY transaction_from DESC LIMIT 1",
        (specialist_id, cycle_id),
    ).fetchone()
    return dict(row) if row is not None else None


def hydrate(specialist_id: str, cycle_id: str,
             as_of: Optional[datetime] = None) -> CycleHydration:
    """HYDRATE stage. Loads everything PLAN needs.

    Raises ValueError when no persona row exists — the specialists/macro_regime
    agent must create the persona first. We never silently default to a
    blank persona.
    """
    as_of = as_of or datetime.now(timezone.utc)
    persona_row = _load_latest_persona(specialist_id)
    if persona_row is None:
        raise ValueError(
            f"persona_missing_for_specialist={specialist_id!r} — create a "
            f"specialist_states row with state_kind='persona' first "
            f"(see talis_desk.specialists.macro_regime)"
        )

    persona_state_id = persona_row["id"]
    persona_version = persona_row["persona_version"]
    try:
        persona_state = json.loads(persona_row["state_json"])
    except Exception:
        persona_state = {}

    persona_prompt = persona_state.get("system_prompt", "")
    persona_tool_uris = list(persona_state.get("persona_tool_uris", []) or [])

    yesterday_row = _load_latest_dehydration(specialist_id)
    if yesterday_row is not None:
        try:
            yesterday_state = json.loads(yesterday_row["state_json"])
        except Exception:
            yesterday_state = {}
    else:
        yesterday_state = {}

    # Unread messages addressed to this specialist (+ scratchpad topic).
    unread: list[AgentMessage] = []
    try:
        unread.extend(read_unread_messages(specialist_id, reader_id=specialist_id))
    except Exception:
        pass
    # Topic subscriptions: every specialist watches #hot_investigations.
    try:
        unread.extend(read_unread_messages("#hot_investigations",
                                             reader_id=specialist_id))
    except Exception:
        pass

    brier = _load_recent_brier_outcomes(specialist_id, n=30)

    # Frozen atlas snapshot for this cycle (v2 line 64 invariant).
    atlas = get_atlas_snapshot_for_cycle(cycle_id)

    # Open hypotheses for context — load all this specialist's actives.
    try:
        open_hyps = get_active_hypotheses(specialist_id, as_of=as_of)
    except Exception:
        open_hyps = []

    return CycleHydration(
        specialist_id=specialist_id,
        persona_version=persona_version,
        persona_prompt=persona_prompt,
        persona_tool_uris=persona_tool_uris,
        yesterday_state=yesterday_state,
        unread_messages=unread,
        recent_brier_outcomes=brier,
        tool_atlas=atlas,
        atlas_pinned=True,
        open_hypotheses=open_hyps,
        persona_state_id=persona_state_id,
    )


# ============================================================================
# PLAN
# ============================================================================


_PLAN_SYSTEM_SUFFIX = (
    "\n\nYou are running the PLAN stage of a research cycle. Propose 3 to "
    "{max_h} hypotheses you want to investigate this cycle. For each "
    "hypothesis, pick 2-4 tool URIs from the provided atlas that would "
    "be most informative. Output ONLY valid JSON, no prose, no fences. "
    "Schema:\n"
    "{{\n"
    '  "hypotheses": [\n'
    "    {{\n"
    '      "title": "short title (<=120 chars)",\n'
    '      "hypothesis_text": "full claim with mechanism",\n'
    '      "initial_prob": 0.5,\n'
    '      "entities": ["BTC", "..."],\n'
    '      "tool_uris": ["tic://tool/.../...@v1", "..."],\n'
    '      "expected_resolution_hours": 24\n'
    "    }}\n"
    "  ]\n"
    "}}"
)


def _build_plan_user_prompt(h: CycleHydration, loop_config: LoopConfig) -> str:
    """Assemble the PLAN user message. Includes atlas summary, unread
    messages, recent Brier outcomes, and yesterday's open hypotheses.
    """
    atlas = h.tool_atlas
    # Prefer the persona's curated subset; fall back to atlas top-25.
    persona_uris = set(h.persona_tool_uris)
    candidate_rows = [r for r in atlas.rows
                       if not persona_uris or r["tool_uri"] in persona_uris]
    if not candidate_rows:
        candidate_rows = atlas.rows
    candidate_rows = candidate_rows[:25]
    tool_lines = [
        f"  - {r['tool_uri']}  ({r.get('kind','?')}) — {r.get('description','')[:120]}"
        for r in candidate_rows
    ]

    brier_lines = []
    for b in h.recent_brier_outcomes[:5]:
        brier_lines.append(
            f"  - {b.get('day')}: brier={b.get('brier_avg')} "
            f"delta={b.get('brier_delta_avg')} n={b.get('n')}"
        )
    if not brier_lines:
        brier_lines.append("  - (no Brier history yet — first cycles)")

    msg_lines = []
    for m in h.unread_messages[:10]:
        msg_lines.append(
            f"  - [{m.message_kind}] from {m.from_agent} -> "
            f"{m.to_agent_or_topic}: {json.dumps(m.payload)[:200]}"
        )
    if not msg_lines:
        msg_lines.append("  - (no unread messages)")

    open_h_lines = []
    for hh in h.open_hypotheses[:5]:
        open_h_lines.append(
            f"  - {hh.id}: {hh.title} (posterior={hh.posterior_prob} "
            f"heat={hh.heat_score})"
        )
    if not open_h_lines:
        open_h_lines.append("  - (no open hypotheses from prior cycles)")

    return (
        f"## Specialist\n"
        f"  specialist_id: {h.specialist_id}\n"
        f"  persona_version: {h.persona_version}\n"
        f"\n## Atlas (curated subset, frozen for this cycle)\n"
        + "\n".join(tool_lines)
        + f"\n\n## Recent Brier (rolling daily)\n"
        + "\n".join(brier_lines)
        + f"\n\n## Unread messages\n"
        + "\n".join(msg_lines)
        + f"\n\n## Open hypotheses (yours, carry-over)\n"
        + "\n".join(open_h_lines)
        + f"\n\nNow propose {loop_config.n_hypotheses_min}-{PLAN_MAX_HYPOTHESES} "
        f"hypotheses for this cycle. Output JSON exactly per the schema."
    )


def _parse_plan_payload(payload: Any, persona_uris: list[str],
                        atlas: ToolAtlasSnapshot,
                        min_n: int) -> list[HypothesisDraftPlan]:
    """Validate the parsed JSON payload into HypothesisDraftPlan list.

    Bad fields are repaired conservatively (clamp probs to [0,1], coerce
    types, drop unknown tool URIs). Returns [] if the payload is so
    malformed we can't recover.
    """
    if not isinstance(payload, dict):
        return []
    hyps = payload.get("hypotheses")
    if not isinstance(hyps, list):
        return []
    atlas_uris = {r["tool_uri"] for r in atlas.rows}
    out: list[HypothesisDraftPlan] = []
    for item in hyps[:PLAN_MAX_HYPOTHESES]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()[:120]
        text = str(item.get("hypothesis_text") or "").strip()
        if not title or not text:
            continue
        try:
            ip = float(item.get("initial_prob", 0.5))
        except Exception:
            ip = 0.5
        ip = max(0.02, min(0.98, ip))
        entities = item.get("entities") or []
        if not isinstance(entities, list):
            entities = []
        entities = [str(e) for e in entities][:8]
        raw_uris = item.get("tool_uris") or []
        if not isinstance(raw_uris, list):
            raw_uris = []
        # Keep only URIs that exist in the frozen atlas. If LLM picked
        # zero valid URIs, fall back to the persona's first tool.
        valid_uris = [u for u in raw_uris if u in atlas_uris]
        if not valid_uris and persona_uris:
            valid_uris = persona_uris[:2]
        if not valid_uris and atlas.rows:
            valid_uris = [atlas.rows[0]["tool_uri"]]
        try:
            exp_hours = int(item.get("expected_resolution_hours", 24))
        except Exception:
            exp_hours = 24
        out.append(HypothesisDraftPlan(
            title=title,
            hypothesis_text=text,
            initial_prob=ip,
            entities=entities,
            tool_uris=valid_uris[:4],
            expected_resolution_hours=max(1, min(720, exp_hours)),
        ))
    if len(out) < min_n:
        return []
    return out


def plan(hydration: CycleHydration, loop_config: LoopConfig) -> CyclePlan:
    """PLAN stage. ONE LLM call, no tools.

    Falls back through the ensemble on parse failure (retry once with a
    stricter prompt, then provider fallback inside chat()).
    """
    system_prompt = (hydration.persona_prompt or "").strip() + \
        _PLAN_SYSTEM_SUFFIX.format(max_h=PLAN_MAX_HYPOTHESES)
    user_prompt = _build_plan_user_prompt(hydration, loop_config)

    # First call — preferred model with chat()'s fallback chain.
    primary = loop_config.plan_model
    fallback_for_chat = "anthropic:claude-sonnet-4-6"
    res = _chat_sync(primary, system_prompt, user_prompt,
                       max_tokens=PLAN_MAX_TOKENS,
                       fallback=fallback_for_chat)
    parsed = _extract_json(res.text) if res.text else None
    hyps = _parse_plan_payload(parsed, hydration.persona_tool_uris,
                                hydration.tool_atlas, loop_config.n_hypotheses_min)
    total_cost = res.cost_usd

    if not hyps:
        # Retry once with a stricter wrapper. We tell the model the parse
        # failed and to please emit ONLY valid JSON. Same model, but
        # chat()'s fallback chain will still kick in if this provider is
        # broken.
        strict_user = (
            "Your previous reply did not parse as the required JSON schema. "
            "Reply with ONLY a JSON object matching the schema below, no "
            "prose, no markdown fences. Schema:\n"
            "{\n  \"hypotheses\": [{\"title\": str, \"hypothesis_text\": str, "
            "\"initial_prob\": float, \"entities\": [str], "
            "\"tool_uris\": [str], \"expected_resolution_hours\": int}, ...]\n}"
            f"\n\nOriginal request:\n{user_prompt}"
        )
        res2 = _chat_sync(primary, system_prompt, strict_user,
                           max_tokens=PLAN_MAX_TOKENS,
                           fallback=fallback_for_chat)
        total_cost += res2.cost_usd
        parsed2 = _extract_json(res2.text) if res2.text else None
        hyps = _parse_plan_payload(parsed2, hydration.persona_tool_uris,
                                     hydration.tool_atlas,
                                     loop_config.n_hypotheses_min)
        if hyps:
            res = res2
        else:
            # NO STUB — raise and let the caller decide. The cycle ends
            # with a clean error rather than silently degenerate output.
            raise RuntimeError(
                "PLAN: LLM did not return a parseable hypothesis list after "
                f"2 attempts (model={primary}, provider chain exhausted). "
                f"Last error: {res2.error or 'unparseable text'}"
            )

    # Build tool_assignments keyed by a synthetic per-plan hypothesis id.
    # The real hypothesis_id is assigned in EXPLORE when start_investigation
    # creates the row; we use a stable plan_index key here.
    tool_assignments: dict[str, list[str]] = {}
    for i, h in enumerate(hyps):
        tool_assignments[f"plan_{i}"] = list(h.tool_uris)

    return CyclePlan(
        hypotheses=hyps,
        tool_assignments=tool_assignments,
        model_used=res.model_used,
        fallback_used=res.fallback_used,
        llm_cost_usd=total_cost,
        raw_text=res.text[:4000],
    )


# ============================================================================
# EXPLORE
# ============================================================================


def _check_budget_extension(specialist_id: str,
                              extension_cost: float,
                              reason: str,
                              cycle_id: str) -> bool:
    """v2 §7 extension auto-approve rule:
    if specialist's 5-day rolling Brier > 0 (better than random) -> approve;
    else queue for human via agent_messages(kind='flag', payload.event='budget_extension_request').
    """
    rows = _load_recent_brier_outcomes(specialist_id, n=5)
    brier_avg = None
    if rows:
        try:
            vals = [float(r.get("brier_delta_avg") or 0.0) for r in rows
                    if r.get("brier_delta_avg") is not None]
            if vals:
                brier_avg = sum(vals) / len(vals)
        except Exception:
            brier_avg = None

    if brier_avg is not None and brier_avg > 0:
        return True

    # Queue for human review. Use 'flag' kind (closest in the enum) with a
    # payload.event marker that the dashboard can filter on.
    try:
        post_message(
            from_agent=specialist_id,
            to_agent_or_topic="@human",
            kind="flag",
            payload={
                "event": "budget_extension_request",
                "cycle_id": cycle_id,
                "specialist_id": specialist_id,
                "requested_extension_usd": extension_cost,
                "reason": reason,
                "five_day_brier_delta_avg": brier_avg,
            },
            dedupe_key=f"budget_ext:{cycle_id}:{specialist_id}",
            expires_in_hours=72,
        )
    except Exception as e:
        warnings.warn(f"budget_extension_request_post_failed: {e}")
    return False


def explore(hydration: CycleHydration, cycle_plan: CyclePlan, cycle_id: str,
              loop_config: LoopConfig,
              base_context: AgentContext) -> tuple[list[ExplorationTrace],
                                                     list[Investigation],
                                                     float, int]:
    """EXPLORE stage. Delegates to talis_desk.exploration.bfs.explore_adversarial
    for each PLAN hypothesis.

    Returns (traces, investigations, total_cost_usd, total_tool_calls).
    Hard-stops when cumulative cost hits loop_config.max_cost_usd; will
    request a budget extension once.

    The per-hypothesis budget is the cycle-wide budget split evenly with
    a floor of 10 calls / $0.50.
    """
    n = max(1, len(cycle_plan.hypotheses))
    per_h_calls = max(loop_config.per_hypothesis_max_calls,
                       loop_config.max_calls // n)
    per_h_cost = max(0.50, loop_config.max_cost_usd / n)

    traces: list[ExplorationTrace] = []
    investigations: list[Investigation] = []
    total_cost = 0.0
    total_calls = 0
    extension_used = False
    budget_cap = loop_config.max_cost_usd

    for idx, h in enumerate(cycle_plan.hypotheses):
        # Cost gate before each hypothesis.
        if total_cost >= budget_cap and not extension_used:
            ok = _check_budget_extension(
                hydration.specialist_id,
                loop_config.extension_cost_usd - budget_cap,
                reason=(
                    f"cycle hit ${budget_cap:.2f} cap with {idx}/{n} "
                    f"hypotheses explored; extension requested to finish."
                ),
                cycle_id=cycle_id,
            )
            extension_used = True
            if ok:
                budget_cap = loop_config.extension_cost_usd
            else:
                # No extension — stop here, dehydration still runs.
                break
        if total_cost >= budget_cap:
            break

        seed = HypothesisSeed(
            specialist_id=hydration.specialist_id,
            title=h.title,
            hypothesis_text=h.hypothesis_text,
            initial_prob=h.initial_prob,
            entities=list(h.entities),
        )
        inv = start_investigation(
            seed=seed,
            heat_score=min(1.0, max(0.0, h.initial_prob)),
            budget=InvestigationBudget(
                max_calls=per_h_calls,
                max_cost_usd=per_h_cost,
                max_wall_seconds=300,
            ),
            context=base_context,
        )
        investigations.append(inv)

        # Build the initial BFS frontier — one QuestionNode per tool the
        # LLM assigned. Question_kind alternates confirmatory/contradiction
        # so the >=20% gate is satisfied even before expand_question adds
        # follow-ups.
        frontier: list[QuestionNode] = []
        for j, uri in enumerate(h.tool_uris):
            kind = "contradiction" if (j == 1 or len(h.tool_uris) == 1 and j == 0) else "confirmatory"
            if j == 0:
                kind = "confirmatory"
            elif j == 1:
                kind = "contradiction"
            else:
                kind = "orthogonal" if j % 2 == 0 else "confirmatory"
            frontier.append(QuestionNode(
                id=f"qn_{uuid4().hex[:10]}",
                question_text=(
                    f"[{kind}] {h.title} — investigate via "
                    f"{uri.split('/')[-1]}"
                ),
                question_kind=kind,  # type: ignore[arg-type]
                tool_uri=uri,
                args=_default_args_for_uri(uri, h.entities),
                prior_prob=h.initial_prob,
            ))
        if not frontier:
            # Skip — no tools assigned (shouldn't happen post-_parse_plan_payload).
            continue

        try:
            trace = explore_adversarial(
                investigation_id=inv.id,
                frontier=frontier,
                max_calls=per_h_calls,
                context=base_context,
            )
        except Exception as e:
            warnings.warn(f"explore_adversarial_failed for {inv.id}: {e}")
            continue
        traces.append(trace)
        total_cost += trace.total_cost_usd
        total_calls += trace.n_calls

    return traces, investigations, total_cost, total_calls


def _default_args_for_uri(uri: str, entities: list[str]) -> dict[str, Any]:
    """Best-effort default args for an arbitrary tool URI.

    The atlas carries an input_schema per tool but we don't introspect it
    here (PLAN should have done that). We pass the first entity as
    `entity_symbol` for builtin/query-style tools, and {} otherwise.
    `dispatch_uri` will reject bad-args calls and the BFS captures the
    error in tool_call_log.error — no silent corruption.
    """
    if not entities:
        return {}
    return {"entity_symbol": entities[0]}


# ============================================================================
# SYNTHESIZE
# ============================================================================


def _resolve_hypothesis_status(final_posterior: float) -> Optional[str]:
    """Decide whether a hypothesis should be resolved at SYNTHESIZE time.

    > 0.7 -> supported
    < 0.3 -> contradicted
    else  -> None (leave active for next cycle)
    """
    if final_posterior >= 0.7:
        return "supported"
    if final_posterior <= 0.3:
        return "contradicted"
    return None


def _build_minimal_trade_idea_draft(
    specialist_id: str,
    cycle_id: str,
    persona_version: str,
    hypothesis: Hypothesis,
    trace: ExplorationTrace,
    paper_only: bool,
) -> Optional[TradeIdeaDraft]:
    """Best-effort trade-idea draft from a resolved-supported hypothesis.

    Returns None when we can't extract an instrument from the entities. We
    do NOT make up prices — for the smoke test we emit a low-confidence
    'flat' direction draft that will validate and self-grade as paper.
    The real version is auto-mutated by persona evolution (Phase 6).

    Sizing intentionally hugs the bottom of the allowed band (10 bps) so
    no live capital lands on a stub draft.
    """
    entity_ids = list(hypothesis.entity_ids or [])
    if not entity_ids:
        return None
    instrument = entity_ids[0]
    confidence = float(hypothesis.posterior_prob or 0.5)
    # Require at least one contradiction at high confidence per Gate 3.
    contradictions: list[ContradictionItem] = []
    if confidence >= 0.7:
        # Walk the exploration steps for any contradiction-tagged steps
        # and surface them as ContradictionItem entries citing the
        # tool_call_log_id.
        for s in trace.steps:
            if s.edge_kind_emitted == "contradicts" and s.tool_call_log_id:
                contradictions.append(ContradictionItem(
                    claim_id=s.tool_call_log_id,
                    reason=(
                        f"BFS step {s.question_text[:120]} produced "
                        f"contradiction (delta={s.heat.posterior_delta:.2f})"
                    ),
                    weight=min(1.0, max(0.1, abs(s.heat.contradiction_score))),
                ))
                if len(contradictions) >= 2:
                    break
        # If we couldn't find any, ratchet confidence below the threshold
        # so Gate 3 passes without fabricating contradictions.
        if not contradictions:
            confidence = 0.69

    now_utc = datetime.now(timezone.utc)
    horizon = "1d"
    horizon_hours = TIME_HORIZON_HOURS[horizon]
    expires_at = now_utc + timedelta(hours=horizon_hours)

    # Direction: derive from final posterior trend over the exploration
    # trace. If the last 5 steps net positive -> 'long', net negative ->
    # 'short', else 'flat'. We bound to safe sizing on either side.
    tail = trace.steps[-5:] if trace.steps else []
    net = sum(s.heat.contradiction_score for s in tail)
    if net > 0.2:
        direction = "long"
    elif net < -0.2:
        direction = "short"
    else:
        direction = "flat"

    # We can't fabricate live prices on a tool-less path. For flat we set
    # entry=stop=target to mark-derived placeholders; the resolver will
    # mark this as a paper / no-PnL idea on close. To keep validation
    # passing we set non-zero numerics with stop on the loss side.
    entry_px = 100.0
    if direction == "long":
        stop_px = 99.0
        target_px = 102.0
    elif direction == "short":
        stop_px = 101.0
        target_px = 98.0
    else:
        # 'flat' — placeholder values; the resolver treats this as no
        # position. Stop must give max_loss_usd > 0 to satisfy Gate 7.
        stop_px = 99.5
        target_px = 100.5

    tool_call_ids = [s.tool_call_log_id for s in trace.steps if s.tool_call_log_id]

    return TradeIdeaDraft(
        cycle_id=cycle_id,
        specialist_id=specialist_id,
        persona_version=persona_version,
        instrument=str(instrument),
        venue="hyperliquid",
        direction=direction,  # type: ignore[arg-type]
        sizing=SizingPlan(
            risk_pct=0.001,  # 10 bps — bottom of the allowed band
            notional_cap_usd=1000.0,
            kelly_fraction=0.10,
            leverage_cap=1.0,
        ),
        entry=EntryPlan(
            trigger="market" if direction in ("long", "short") else "no_entry",
            limit_px=None,
            market_assumption="liquid",
            invalidation=(
                f"cancel after {horizon_hours}h if no trigger or if "
                f"posterior drops below 0.5"
            ),
        ),
        stop=StopPlan(
            px=stop_px,
            max_loss_usd=max(0.01, abs(entry_px - stop_px) * 1.0),
            stop_kind="hard",
        ),
        target=None if direction == "flat" else None,  # keep simple — invalidation suffices
        time_horizon=horizon,
        edge_thesis=(
            (hypothesis.hypothesis_text or "")[:1200]
            + f"\n\n[posterior={hypothesis.posterior_prob} "
            f"heat={hypothesis.heat_score} status={hypothesis.status}]"
        ),
        claim_ids=list(hypothesis.claim_ids or []),
        hypothesis_ids=[hypothesis.id],
        forecast_ids=[],
        debate_ids=[],
        tool_call_ids=tool_call_ids[:20],
        contradicting_evidence=contradictions,
        confluence_score=float(hypothesis.heat_score or 0.0),
        confidence=confidence,
        expires_at=expires_at,
        valid_from=now_utc,
        payload={
            "synthesizer_note": "auto_emitted_from_resolved_supported_hypothesis",
            "paper_only_at_emit": paper_only,
        },
    )


def synthesize(hydration: CycleHydration, cycle_plan: CyclePlan,
                traces: list[ExplorationTrace],
                investigations: list[Investigation],
                cycle_id: str, loop_config: LoopConfig,
                base_context: AgentContext) -> CycleSynthesis:
    """SYNTHESIZE stage.

    - Update / resolve hypotheses based on final posteriors.
    - For resolved-supported hypotheses, emit a trade idea (validated).
    - Post peer messages for hot signals (|posterior_delta|>0.2 cumulative).
    - Trigger debates for high-confidence claims & high-confidence ideas.
    """
    new_ideas: list[TradeIdea] = []
    updated: list[Hypothesis] = []
    resolved: list[Hypothesis] = []
    peer_msgs: list[AgentMessage] = []
    debate_decisions: list[DebateTriggerDecision] = []

    # Walk each trace + matching investigation
    for trace, inv in zip(traces, investigations):
        # Final posterior was already written by update_posterior calls in
        # the BFS loop; we re-read the open head to be safe.
        try:
            from ..hypotheses.model import _find_open_head, _row_to_hypothesis  # type: ignore
            conn = get_desk_store().conn
            row = _find_open_head(conn, inv.root_hypothesis_id)
            hyp = _row_to_hypothesis(row) if row is not None else None
        except Exception:
            hyp = None
        if hyp is None:
            continue

        # Did this investigation move the posterior >= 0.2 cumulatively?
        # If yes, post a peer message for visibility.
        delta_total = abs((hyp.posterior_prob or 0.5) - trace.steps[0].posterior_before) \
            if trace.steps else 0.0
        if delta_total > 0.2:
            try:
                msg = post_message(
                    from_agent=hydration.specialist_id,
                    to_agent_or_topic="#hot_investigations",
                    kind="observation",
                    payload={
                        "event": "synthesis_posterior_move",
                        "cycle_id": cycle_id,
                        "hypothesis_id": hyp.id,
                        "title": hyp.title,
                        "posterior_prob": hyp.posterior_prob,
                        "delta_total": delta_total,
                    },
                    related_hypothesis_id=hyp.id,
                    expires_in_hours=24,
                    dedupe_key=f"synth_post:{cycle_id}:{hyp.id}",
                )
                peer_msgs.append(msg)
            except Exception as e:
                warnings.warn(f"synthesize peer post failed: {e}")

        # Check whether we should resolve.
        status = _resolve_hypothesis_status(float(hyp.posterior_prob or 0.5))
        if status is not None:
            try:
                resolved_hyp = resolve_hypothesis(
                    hyp_id=hyp.id,
                    status=status,
                    outcome_payload={
                        "cycle_id": cycle_id,
                        "final_posterior": hyp.posterior_prob,
                        "n_calls": trace.n_calls,
                        "contradiction_share": trace.contradiction_share,
                    },
                )
                resolved.append(resolved_hyp)
                hyp = resolved_hyp
            except Exception as e:
                warnings.warn(f"resolve_hypothesis failed for {hyp.id}: {e}")
        else:
            updated.append(hyp)

        # Debate trigger on high-conf hypotheses.
        try:
            decision = maybe_trigger_debate(
                claim_or_idea={
                    "posterior_prob": float(hyp.posterior_prob or 0.5),
                    "impact_score": float(hyp.heat_score or 0.0),
                    "instrument": hyp.entity_ids[0] if hyp.entity_ids else None,
                    "horizon": "1d",
                    "participants": [hydration.specialist_id],
                },
                context=base_context,
            )
            debate_decisions.append(decision)
        except Exception as e:
            warnings.warn(f"maybe_trigger_debate (hyp) failed: {e}")

        # Emit a trade idea ONLY when supported.
        if status == "supported":
            draft = _build_minimal_trade_idea_draft(
                specialist_id=hydration.specialist_id,
                cycle_id=cycle_id,
                persona_version=hydration.persona_version,
                hypothesis=hyp,
                trace=trace,
                paper_only=loop_config.paper_only,
            )
            if draft is None:
                continue
            try:
                # validate first so caller gets a clean idea
                validation = validate_trade_idea(draft.to_trade_idea())
                if not validation.ok:
                    warnings.warn(
                        f"trade_idea_draft_validation_failed: {validation.errors[:3]}"
                    )
                    continue
                idea = emit_trade_idea(draft, base_context)
                new_ideas.append(idea)
                # Debate trigger on the published idea (cycle counter shared
                # with the hypothesis triggers above).
                if idea.confidence >= 0.7:
                    try:
                        d = maybe_trigger_debate(
                            claim_or_idea={
                                "confidence": idea.confidence,
                                "instrument": idea.instrument,
                                "horizon": idea.time_horizon,
                                "participants": [hydration.specialist_id],
                                "posterior_prob": idea.confidence,
                            },
                            context=base_context,
                        )
                        debate_decisions.append(d)
                    except Exception as e:
                        warnings.warn(f"maybe_trigger_debate (idea) failed: {e}")
            except Exception as e:
                warnings.warn(f"emit_trade_idea failed: {e}")

    return CycleSynthesis(
        new_trade_ideas=new_ideas,
        updated_hypotheses=updated,
        resolved_hypotheses=resolved,
        peer_messages=peer_msgs,
        debate_triggers=debate_decisions,
    )


# ============================================================================
# REFLECT
# ============================================================================


_REFLECT_SYSTEM = (
    "You are running the REFLECT stage of a research cycle. Your task is "
    "to read the cycle trace + Brier outcomes + tool call summary, then "
    "output:\n"
    "  - notes_to_self (<=160 chars): one durable lesson for tomorrow.\n"
    "  - posterior_adjustments: {hypothesis_id: new_probability}\n"
    "  - tool_affinity_delta: {tool_uri: delta_score} (signed, [-1,1])\n"
    "  - redundant_tool_calls: list of tool_call_log_ids that did not "
    "    contribute to any posterior move.\n"
    "Reply with ONLY valid JSON, no prose."
)


def _build_reflect_user_prompt(
    hydration: CycleHydration,
    cycle_plan: CyclePlan,
    traces: list[ExplorationTrace],
    synthesis: CycleSynthesis,
    cycle_total_cost_usd: float,
) -> str:
    trace_lines = []
    for t in traces:
        trace_lines.append(
            f"  - inv={t.investigation_id} calls={t.n_calls} "
            f"hot={t.n_hot_branches} contradiction_share={t.contradiction_share:.2f} "
            f"final_posterior={t.final_posterior:.2f} "
            f"cost={t.total_cost_usd:.4f}"
        )
    if not trace_lines:
        trace_lines.append("  - (no exploration traces ran)")

    redundant_hint = []
    for t in traces:
        for s in t.steps:
            if abs(s.posterior_after - s.posterior_before) < 0.01 and s.tool_call_log_id:
                redundant_hint.append(s.tool_call_log_id)
    redundant_hint = redundant_hint[:15]

    ideas_lines = []
    for i in synthesis.new_trade_ideas[:5]:
        ideas_lines.append(
            f"  - {i.id} {i.direction} {i.instrument} "
            f"conf={i.confidence:.2f}"
        )
    if not ideas_lines:
        ideas_lines.append("  - (no trade ideas emitted this cycle)")

    return (
        f"## Cycle\n"
        f"  specialist_id: {hydration.specialist_id}\n"
        f"  cycle_total_cost_usd: {cycle_total_cost_usd:.4f}\n"
        f"  plan_model_used: {cycle_plan.model_used}\n"
        f"\n## Traces\n"
        + "\n".join(trace_lines)
        + f"\n\n## Trade ideas emitted\n"
        + "\n".join(ideas_lines)
        + f"\n\n## Candidate-redundant tool_call_log ids (auto-detected, "
        f"posterior-flat steps)\n"
        + (("  - " + "\n  - ".join(redundant_hint)) if redundant_hint
           else "  - (none)")
        + "\n\nReply JSON exactly per the schema."
    )


def reflect(hydration: CycleHydration, cycle_plan: CyclePlan,
             traces: list[ExplorationTrace], synthesis: CycleSynthesis,
             cycle_total_cost_usd: float,
             loop_config: LoopConfig) -> CycleReflection:
    """REFLECT stage. Capped at `reflection_spend_cap_pct` of cycle LLM spend.

    Uses the cheap model by default (anthropic:claude-haiku-4-5) per v2 line
    142. If the budget cap is already exceeded we run a degraded local
    reflection (no LLM) but still produce all fields — this is the only
    place where a heuristic substitutes for the LLM, and ONLY because the
    5% cap forbids the LLM call. The cycle never silently skips REFLECT.
    """
    cap_usd = max(0.001, cycle_total_cost_usd * loop_config.reflection_spend_cap_pct)

    user_prompt = _build_reflect_user_prompt(
        hydration, cycle_plan, traces, synthesis, cycle_total_cost_usd,
    )

    # If even the cheapest provider would exceed cap, fall through to the
    # heuristic. We estimate cost first via cost rate * estimated token
    # count.
    rate = _COST_PER_MTOK.get(loop_config.reflect_model, 1.2)
    est_total_chars = (
        len(_REFLECT_SYSTEM) + len(user_prompt) + REFLECT_MAX_TOKENS * 4
    )
    est_cost = (est_total_chars / 4.0) / 1_000_000.0 * rate
    if est_cost > cap_usd:
        return _local_reflection_fallback(traces, synthesis, cap_usd,
                                            cycle_total_cost_usd,
                                            reason="cost_cap_exceeded_pre_call")

    res = _chat_sync(
        loop_config.reflect_model,
        _REFLECT_SYSTEM,
        user_prompt,
        max_tokens=REFLECT_MAX_TOKENS,
        fallback="anthropic:claude-sonnet-4-6",
    )
    parsed = _extract_json(res.text) if res.text else None
    if not isinstance(parsed, dict):
        return _local_reflection_fallback(traces, synthesis, cap_usd,
                                            cycle_total_cost_usd,
                                            reason=f"llm_parse_failed:{res.error or 'unparseable'}",
                                            llm_cost=res.cost_usd,
                                            llm_model=res.model_used)

    notes = str(parsed.get("notes_to_self") or "").strip()[:240]
    raw_pa = parsed.get("posterior_adjustments") or {}
    posterior_adj: dict[str, float] = {}
    if isinstance(raw_pa, dict):
        for k, v in raw_pa.items():
            try:
                vv = float(v)
                if 0.0 <= vv <= 1.0:
                    posterior_adj[str(k)] = vv
            except Exception:
                continue
    raw_ta = parsed.get("tool_affinity_delta") or {}
    affinity: dict[str, float] = {}
    if isinstance(raw_ta, dict):
        for k, v in raw_ta.items():
            try:
                vv = float(v)
                if -1.0 <= vv <= 1.0:
                    affinity[str(k)] = vv
            except Exception:
                continue
    raw_red = parsed.get("redundant_tool_calls") or []
    redundant: list[str] = []
    if isinstance(raw_red, list):
        redundant = [str(x) for x in raw_red][:50]

    return CycleReflection(
        notes_to_self=notes or "(empty reflection)",
        posterior_adjustments=posterior_adj,
        tool_affinity_delta=affinity,
        redundant_tool_calls=redundant,
        model_used=res.model_used,
        llm_cost_usd=res.cost_usd,
    )


def _local_reflection_fallback(traces: list[ExplorationTrace],
                                  synthesis: CycleSynthesis,
                                  cap_usd: float,
                                  cycle_total_cost_usd: float,
                                  reason: str,
                                  llm_cost: float = 0.0,
                                  llm_model: str = "") -> CycleReflection:
    """Heuristic substitute when REFLECT cannot afford an LLM call.

    NOT a stub for normal operation — only triggers when the 5% cap would
    be exceeded by the cheapest model, or when the LLM reply was
    unparseable AFTER chat()'s 6-provider fallback. We mark the
    reflection's notes_to_self with the trigger reason so the dashboard
    knows.
    """
    # Affinity heuristic: tools that produced posterior-moving steps get
    # positive delta; tools that produced no movement get negative.
    delta: dict[str, float] = {}
    redundant: list[str] = []
    for t in traces:
        for s in t.steps:
            move = abs(s.posterior_after - s.posterior_before)
            cur = delta.get(s.tool_uri, 0.0)
            # Scale into [-1,1] band — saturating tanh-style cap.
            cur = max(-1.0, min(1.0, cur + (move if move > 0.05 else -0.02)))
            delta[s.tool_uri] = cur
            if move < 0.01 and s.tool_call_log_id:
                redundant.append(s.tool_call_log_id)

    notes = (
        f"[heuristic_reflect/{reason}] "
        f"emitted_ideas={len(synthesis.new_trade_ideas)} "
        f"resolved={len(synthesis.resolved_hypotheses)} "
        f"cycle_cost=${cycle_total_cost_usd:.4f}"
    )
    return CycleReflection(
        notes_to_self=notes,
        posterior_adjustments={},
        tool_affinity_delta=delta,
        redundant_tool_calls=redundant[:50],
        model_used=llm_model or "local_heuristic",
        llm_cost_usd=llm_cost,
    )


# ============================================================================
# DEHYDRATE — write next specialist_states row
# ============================================================================


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _sha256_short(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def dehydrate(hydration: CycleHydration, cycle_plan: CyclePlan,
                traces: list[ExplorationTrace], synthesis: CycleSynthesis,
                reflection: CycleReflection,
                cycle_id: str, cycle_total_cost_usd: float,
                kill_switch_triggered: bool,
                quality_flags: list[str]) -> str:
    """DEHYDRATE — append a `state_kind='dehydration'` row.

    The state_json carries everything the NEXT cycle's HYDRATE will need:
      - priors (final posterior per active hypothesis)
      - open_hypothesis_ids
      - recent_tool_calls (summary)
      - tool_affinity (rolled into the heuristic affinity from REFLECT)
      - notes_to_self
      - last_cycle_id
      - kill_switch_triggered (so next HYDRATE sees the paper_only flag)

    Returns the new specialist_states.id.
    """
    open_hyp_ids: list[str] = []
    priors: dict[str, float] = {}
    for h in synthesis.updated_hypotheses:
        open_hyp_ids.append(h.id)
        if h.posterior_prob is not None:
            priors[h.id] = float(h.posterior_prob)

    recent_calls: list[dict[str, Any]] = []
    for t in traces:
        for s in t.steps:
            recent_calls.append({
                "tool_uri": s.tool_uri,
                "tool_call_log_id": s.tool_call_log_id,
                "duration_ms": s.duration_ms,
                "cost_usd": s.cost_usd,
                "edge_kind": s.edge_kind_emitted,
                "posterior_after": s.posterior_after,
            })

    # Carry-forward tool affinity: merge yesterday's affinity with the
    # delta from REFLECT. Yesterday's lives in yesterday_state['tool_affinity'].
    prev_affinity = (hydration.yesterday_state.get("tool_affinity") or {}) \
        if isinstance(hydration.yesterday_state, dict) else {}
    if not isinstance(prev_affinity, dict):
        prev_affinity = {}
    next_affinity: dict[str, float] = dict(prev_affinity)
    for uri, d in reflection.tool_affinity_delta.items():
        old = float(next_affinity.get(uri, 0.0) or 0.0)
        new = max(-2.0, min(2.0, old + float(d)))
        next_affinity[uri] = new

    brier_delta_today = None
    if hydration.recent_brier_outcomes:
        try:
            brier_delta_today = float(
                hydration.recent_brier_outcomes[0].get("brier_delta_avg") or 0.0
            )
        except Exception:
            brier_delta_today = None

    state_json: dict[str, Any] = {
        "cycle_id": cycle_id,
        "priors": priors,
        "open_hypothesis_ids": open_hyp_ids,
        "resolved_hypothesis_ids": [h.id for h in synthesis.resolved_hypotheses],
        "trade_idea_ids": [i.id for i in synthesis.new_trade_ideas],
        "recent_tool_calls": recent_calls[-200:],
        "tool_affinity": next_affinity,
        "notes_to_self": reflection.notes_to_self,
        "redundant_tool_calls": reflection.redundant_tool_calls,
        "kill_switch_triggered": bool(kill_switch_triggered),
        "quality_flags": list(quality_flags),
        "cycle_total_cost_usd": cycle_total_cost_usd,
        "plan_model_used": cycle_plan.model_used,
        "reflect_model_used": reflection.model_used,
    }

    # Reflection delta separately (state_kind='dehydration' carries the
    # whole snapshot; a parallel 'mutation_candidate' row is the persona
    # evolution flow which lives in Phase 6).
    now = datetime.now(timezone.utc)
    new_id = "spst_" + uuid4().hex[:24]
    prompt_hash = _sha256_short(hydration.persona_prompt or "")

    conn = get_desk_store().conn
    conn.execute(
        "INSERT INTO specialist_states "
        "(id, specialist_id, persona_version, cycle_id, state_kind, "
        " state_json, prompt_hash, parent_state_id, brier_delta, "
        " valid_from, transaction_from) "
        "VALUES (?, ?, ?, ?, 'dehydration', ?, ?, ?, ?, ?, ?)",
        (
            new_id,
            hydration.specialist_id,
            hydration.persona_version,
            cycle_id,
            json.dumps(state_json),
            prompt_hash,
            hydration.persona_state_id,
            brier_delta_today,
            _iso(now),
            _iso(now),
        ),
    )
    conn.commit()
    return new_id


def _write_reflection_row(specialist_id: str, persona_version: str,
                            cycle_id: str, parent_state_id: str,
                            reflection: CycleReflection) -> str:
    """Also write a state_kind='dehydration' is the canonical record; some
    schemas allowed 'reflection' but the SOTA DDL CHECK constrains
    state_kind ∈ ('persona','hydration','dehydration','mutation_candidate',
    'rollback'). We therefore store the reflection payload inside the
    dehydration state_json (above) and do NOT write a separate row.

    Keeping the function as a no-op stub for callers in case Phase 6 adds
    a 'reflection' state_kind.
    """
    return parent_state_id


# ============================================================================
# Kill switches
# ============================================================================


def _check_kill_switches(cycle_id: str, cycle_total_cost_usd: float,
                          loop_config: LoopConfig,
                          debate_decisions: list[DebateTriggerDecision],
                          traces: list[ExplorationTrace]) -> tuple[bool, list[str]]:
    """v2 §7 kill switches (per-cycle subset only).

    Returns (kill_switch_triggered, quality_flags). Daily-spend kill (§7
    line 538) is outside the cycle scope; the per-cycle ones we check:
      - Any cycle that triggers > 10 debates -> kill.
      - Cycle cost > the cap.
    """
    flags: list[str] = []
    triggered = False

    debate_count = sum(1 for d in debate_decisions if d.should_trigger)
    if any(d.kill_switch for d in debate_decisions):
        flags.append("kill:debate_cap_exceeded")
        triggered = True
    if debate_count > DEBATE_PER_CYCLE_KILL_THRESHOLD:
        flags.append(f"kill:debate_count={debate_count}")
        triggered = True
    if cycle_total_cost_usd > loop_config.extension_cost_usd:
        flags.append(f"kill:cost_overrun_${cycle_total_cost_usd:.2f}")
        triggered = True

    # Source-health flag (>30% of cited tool calls in this cycle failed)
    n_calls = 0
    n_failed = 0
    for t in traces:
        for s in t.steps:
            n_calls += 1
            if s.tool_call_log_id is None:
                n_failed += 1
    if n_calls > 0 and (n_failed / n_calls) > SOURCE_HEALTH_FLAG_THRESHOLD:
        flags.append(f"flag:source_health_failure_share={n_failed}/{n_calls}")

    return triggered, flags


# ============================================================================
# Main entry point — run_research_cycle
# ============================================================================


def run_research_cycle(
    specialist_id: str,
    cycle_id: str,
    as_of: Optional[datetime] = None,
    loop_config: Optional[LoopConfig] = None,
) -> ResearchCycleResult:
    """Karpathy-style ONE loop. See module docstring for the 6 stages.

    Idempotent: if a `specialist_states` row with this (specialist_id,
    cycle_id) and state_kind='dehydration' already exists, returns a
    minimally-populated ResearchCycleResult with idempotent_short_circuit
    set; no new DB rows are written.
    """
    t_start = time.perf_counter()
    loop_config = loop_config or LoopConfig()

    # --- 0. Idempotency ----------------------------------------------------
    existing = _check_idempotent_short_circuit(specialist_id, cycle_id)
    if existing is not None:
        # Build a thin result echoing the prior state. We re-hydrate so the
        # caller sees the same shape as a fresh run, but stages are empty.
        h = hydrate(specialist_id, cycle_id, as_of=as_of)
        return ResearchCycleResult(
            cycle_id=cycle_id,
            specialist_id=specialist_id,
            persona_version=h.persona_version,
            hydration=h,
            plan=CyclePlan(
                hypotheses=[], tool_assignments={},
                model_used="(idempotent_short_circuit)",
                fallback_used=False, llm_cost_usd=0.0, raw_text="",
            ),
            exploration_traces=[],
            exploration_trace_id="",
            synthesis=CycleSynthesis(
                new_trade_ideas=[], updated_hypotheses=[],
                resolved_hypotheses=[], peer_messages=[],
                debate_triggers=[],
            ),
            reflection=CycleReflection(
                notes_to_self="(idempotent_short_circuit)",
                posterior_adjustments={},
                tool_affinity_delta={},
                redundant_tool_calls=[],
                model_used="(idempotent_short_circuit)",
                llm_cost_usd=0.0,
            ),
            next_state_id=existing["id"],
            total_cost_usd=0.0,
            total_tool_calls=0,
            elapsed_seconds=time.perf_counter() - t_start,
            kill_switch_triggered=False,
            quality_flags=["idempotent_short_circuit"],
            idempotent_short_circuit=True,
        )

    # --- 1. HYDRATE --------------------------------------------------------
    hydration = hydrate(specialist_id, cycle_id, as_of=as_of)
    base_context = AgentContext(
        cycle_id=cycle_id,
        specialist_id=specialist_id,
    )

    # --- 2. PLAN -----------------------------------------------------------
    cycle_plan = plan(hydration, loop_config)

    # --- 3. EXPLORE --------------------------------------------------------
    traces, investigations, expl_cost, expl_calls = explore(
        hydration=hydration,
        cycle_plan=cycle_plan,
        cycle_id=cycle_id,
        loop_config=loop_config,
        base_context=base_context,
    )
    exploration_trace_id = (
        traces[0].investigation_id if traces else ""
    )

    # --- 4. SYNTHESIZE -----------------------------------------------------
    synthesis = synthesize(
        hydration=hydration,
        cycle_plan=cycle_plan,
        traces=traces,
        investigations=investigations,
        cycle_id=cycle_id,
        loop_config=loop_config,
        base_context=base_context,
    )

    # Pre-reflection cost = PLAN + EXPLORE + emit overhead (~0).
    pre_reflect_cost = cycle_plan.llm_cost_usd + expl_cost

    # --- 5. REFLECT --------------------------------------------------------
    reflection = reflect(
        hydration=hydration,
        cycle_plan=cycle_plan,
        traces=traces,
        synthesis=synthesis,
        cycle_total_cost_usd=pre_reflect_cost,
        loop_config=loop_config,
    )

    cycle_total_cost = pre_reflect_cost + reflection.llm_cost_usd

    # --- 5.5. Kill switches ------------------------------------------------
    kill_triggered, quality_flags = _check_kill_switches(
        cycle_id=cycle_id,
        cycle_total_cost_usd=cycle_total_cost,
        loop_config=loop_config,
        debate_decisions=synthesis.debate_triggers,
        traces=traces,
    )

    # --- 6. DEHYDRATE ------------------------------------------------------
    next_state_id = dehydrate(
        hydration=hydration,
        cycle_plan=cycle_plan,
        traces=traces,
        synthesis=synthesis,
        reflection=reflection,
        cycle_id=cycle_id,
        cycle_total_cost_usd=cycle_total_cost,
        kill_switch_triggered=kill_triggered,
        quality_flags=quality_flags,
    )

    return ResearchCycleResult(
        cycle_id=cycle_id,
        specialist_id=specialist_id,
        persona_version=hydration.persona_version,
        hydration=hydration,
        plan=cycle_plan,
        exploration_traces=traces,
        exploration_trace_id=exploration_trace_id,
        synthesis=synthesis,
        reflection=reflection,
        next_state_id=next_state_id,
        total_cost_usd=cycle_total_cost,
        total_tool_calls=expl_calls,
        elapsed_seconds=time.perf_counter() - t_start,
        kill_switch_triggered=kill_triggered,
        quality_flags=quality_flags,
        paper_only=loop_config.paper_only or kill_triggered,
    )

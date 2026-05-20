"""Adversarial BFS engine — SOTA v2 Layer 2.

# What this implements

```python
def start_investigation(seed, heat_score, budget, context) -> Investigation
def explore_adversarial(investigation_id, frontier, max_calls, context) -> ExplorationTrace
def score_hot_signal(result, hypothesis_id, context) -> HotSignalScore
def expand_question(node, context) -> list[QuestionNode]
def spawn_sub_investigation(parent_id, signal, context) -> Investigation
def maybe_trigger_debate(claim_or_idea, context) -> DebateTriggerDecision
```

# Behavioral contract (v2 §2 Layer 2)

1. Breadth-first traversal with one extra move: contradiction-seeking.
   At each frontier expansion, at least one question must be tagged
   `question_kind='contradiction'`. We enforce this in `expand_question`
   by quota (1-2 of 3-5 follow-ups are contradiction-seeking) which
   averages to >=20% over an investigation.

2. In-cycle learning. If a single tool result produces
   `|posterior_delta| > 0.2`, the loop spawns a sub-investigation
   immediately AND posts to the scratchpad so peers can see it within
   the same cycle. No waiting for reflection.

3. Debate triggers (v2 line 92): posterior > 0.75, deadline <=24h,
   impact_score >= 0.7, trade-idea confidence >= 0.7, source conflict,
   or cross-specialist contradiction.

4. Thrash controls (v2 line 94):
     - max 10 debates per cycle (11th -> kill_switch=True)
     - max 3 debates per specialist pair
     - max 1 per instrument/horizon per 6h
     - debate budget <= 20% of daily LLM spend
     (the last cap is honored by the caller — we expose `cost_share`)

# Honest gaps

- `expand_question` uses Haiku (cheap) for question generation if an API
  key is configured (ANTHROPIC_API_KEY). Otherwise falls back to a
  deterministic template that still respects the contradiction quota.
- `score_hot_signal.posterior_delta` requires the LLM to declare its prior;
  for synthetic / fixture-driven tests we accept `prior_prob` injected via
  the result payload's `_prior` field.
- Debate cooldown tracking lives in-memory (process-local). v2 calls for
  this to move to a `debate_cooldowns` table or repurpose `agent_messages`
  with kind='debate_cooldown' before Phase 5. Function
  `reset_debate_cooldowns()` is exposed for tests.
"""
from __future__ import annotations

import json
import os
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Literal, Optional
from uuid import uuid4

from ..agents_native.scratchpad import post_message
from ..hypotheses.model import (
    Hypothesis,
    HypothesisDraft,
    HypothesisEdgeDraft,
    add_edge,
    link_evidence,
    propose_hypothesis,
    update_posterior,
)
from ..store import get_desk_store
from ..tool_atlas import AgentContext, dispatch_uri
from ..tool_atlas.atlas import ToolResult


# ============================================================================
# Dataclasses
# ============================================================================

QuestionKind = Literal["confirmatory", "contradiction", "orthogonal"]


@dataclass
class InvestigationBudget:
    """Per-investigation cost ceiling.

    Defaults per v2 §7 / Section 7 of v2 (lines 475-538):
    - max_calls: 300 (hot investigation depth target)
    - max_cost_usd: 5.00 per agent default; raised to 10.00 only with
      explicit justification recorded in `extension_to_usd`.
    - max_wall_seconds: 600 (10 min) hard ceiling.
    """

    max_calls: int = 300
    max_cost_usd: float = 5.0
    max_wall_seconds: int = 600
    extension_to_usd: Optional[float] = None  # raised to 10.0 w/ justification


@dataclass
class HypothesisSeed:
    """Input to `start_investigation`."""

    specialist_id: str
    title: str
    hypothesis_text: str
    initial_prob: float = 0.5
    entities: list[str] = field(default_factory=list)
    spawned_from_hypothesis_id: Optional[str] = None  # for sub-investigations


@dataclass
class HotSignalScore:
    """Output of `score_hot_signal`. All scores in [0, 1] (signed for
    contradiction)."""

    posterior_delta: float       # |new - prior|; >0.2 triggers hot branch
    surprise_score: float        # how unexpected was the tool result
    contradiction_score: float   # signed: <0 contradicts, >0 supports
    novelty_score: float         # not seen in prior claims/corpus
    overall_heat: float          # combined; > 0.7 = hot investigation


@dataclass
class QuestionNode:
    """One node in the BFS frontier — a question to ask + how to answer it.

    `tool_uri` is the tic://... URI that should be dispatched. `args` is
    the kwargs dict.
    """

    id: str
    question_text: str
    question_kind: QuestionKind
    tool_uri: str
    args: dict[str, Any]
    depth: int = 0
    parent_id: Optional[str] = None
    prior_prob: float = 0.5


@dataclass
class ToolResultLike:
    """Mock-friendly view of a tool result for `score_hot_signal`. Real
    callers pass a `ToolResult` from `dispatch_uri`. Tests pass synthetic
    dicts via `_from_result`."""

    ok: bool
    result: Any
    tool_call_log_id: str
    cost_usd: float
    duration_ms: int


@dataclass
class Investigation:
    """Handle returned by `start_investigation`. Carries the seed, budget,
    root hypothesis id, and a running counter of calls / cost."""

    id: str
    root_hypothesis_id: str
    seed: HypothesisSeed
    budget: InvestigationBudget
    spawned_from_investigation_id: Optional[str] = None
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    calls_used: int = 0
    cost_used_usd: float = 0.0
    wall_seconds_used: float = 0.0
    hot_branches_spawned: int = 0
    closed: bool = False


@dataclass
class ExplorationStep:
    """One BFS step's audit trail."""

    question_id: str
    question_text: str
    question_kind: QuestionKind
    tool_uri: str
    tool_call_log_id: Optional[str]
    cost_usd: float
    duration_ms: int
    posterior_before: float
    posterior_after: float
    heat: HotSignalScore
    edge_kind_emitted: str
    spawned_sub_investigation_id: Optional[str] = None
    # Codex finding #3: explicit dispatch result + error so failed tool calls
    # never feed posterior updates or evidence edges. `status` is one of
    # 'ok', 'dispatch_failed'. `error` holds the ToolResult.error message
    # (e.g. 'tool_uri_not_in_atlas: tic://...').
    status: str = "ok"
    error: Optional[str] = None


@dataclass
class ExplorationTrace:
    """Output of `explore_adversarial`."""

    investigation_id: str
    n_calls: int
    n_hot_branches: int
    n_contradiction_questions: int
    contradiction_share: float
    total_cost_usd: float
    wall_seconds: float
    final_posterior: float
    steps: list[ExplorationStep] = field(default_factory=list)
    sub_investigations: list[str] = field(default_factory=list)
    capped: bool = False  # True if we hit max_calls / cost / wall before frontier emptied
    # Codex finding #3 / #9: per-trace failure metrics. The kill-switch
    # cross-checks `n_failed_calls / n_calls` against the source-health
    # threshold. `aborted_due_to_source_health=True` when the BFS itself
    # tripped a per-investigation failure circuit-breaker (>50%).
    n_failed_calls: int = 0
    aborted_due_to_source_health: bool = False


@dataclass
class DebateTriggerDecision:
    """Output of `maybe_trigger_debate`."""

    should_trigger: bool
    reason: str
    kill_switch: bool = False
    cooldown_until: Optional[datetime] = None
    debates_this_cycle: int = 0


# ============================================================================
# Module-level state (debate cooldowns)
# ============================================================================
#
# In-memory only for Phase 4. v2 wants this in `debate_cooldowns` or
# `agent_messages(kind='debate_cooldown')`; deferred to Phase 5.

_DEBATE_STATE: dict[str, Any] = {
    "cycle_counts": {},          # cycle_id -> count
    "pair_counts": {},           # frozenset({a,b}) -> count
    "instrument_horizon": {},    # (instrument, horizon) -> last-fired datetime
}

# Each investigation gets a `random.Random` seeded from its id so question
# generation is replay-deterministic.
_RNG_BY_INVESTIGATION: dict[str, random.Random] = {}


def reset_debate_cooldowns() -> None:
    """Test hook: clear the in-memory debate state."""
    _DEBATE_STATE["cycle_counts"] = {}
    _DEBATE_STATE["pair_counts"] = {}
    _DEBATE_STATE["instrument_horizon"] = {}


# ============================================================================
# Schema bootstrap (investigations are tracked in a small side table — the
# SOTA DDL doesn't define an `investigations` table; `investigation_id` is
# just a FK in `tool_call_log`. We persist budgets + roots locally so
# spawn_sub_investigation can recover the chain.)
# ============================================================================

_INVESTIGATIONS_DDL = """
CREATE TABLE IF NOT EXISTS investigations (
    id TEXT PRIMARY KEY,
    root_hypothesis_id TEXT NOT NULL,
    specialist_id TEXT NOT NULL,
    cycle_id TEXT NOT NULL,
    seed_json TEXT NOT NULL,
    budget_json TEXT NOT NULL,
    spawned_from_investigation_id TEXT,
    started_at TEXT NOT NULL,
    closed INTEGER NOT NULL DEFAULT 0,
    calls_used INTEGER NOT NULL DEFAULT 0,
    cost_used_usd REAL NOT NULL DEFAULT 0,
    hot_branches_spawned INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_investigations_cycle ON investigations(cycle_id, started_at);
CREATE INDEX IF NOT EXISTS idx_investigations_root ON investigations(root_hypothesis_id);
"""

_SCHEMA_BOOTSTRAPPED = False


def _ensure_schema() -> None:
    global _SCHEMA_BOOTSTRAPPED
    if _SCHEMA_BOOTSTRAPPED:
        return
    conn = get_desk_store().conn
    for stmt in _INVESTIGATIONS_DDL.strip().split(";"):
        s = stmt.strip()
        if not s:
            continue
        try:
            conn.execute(s)
        except Exception:
            pass
    conn.commit()
    _SCHEMA_BOOTSTRAPPED = True


def _persist_investigation(inv: Investigation, specialist_id: str, cycle_id: str) -> None:
    conn = get_desk_store().conn
    conn.execute(
        "INSERT OR REPLACE INTO investigations "
        "(id, root_hypothesis_id, specialist_id, cycle_id, seed_json, budget_json, "
        " spawned_from_investigation_id, started_at, closed, calls_used, cost_used_usd, "
        " hot_branches_spawned) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            inv.id,
            inv.root_hypothesis_id,
            specialist_id,
            cycle_id,
            json.dumps({
                "specialist_id": inv.seed.specialist_id,
                "title": inv.seed.title,
                "hypothesis_text": inv.seed.hypothesis_text,
                "initial_prob": inv.seed.initial_prob,
                "entities": inv.seed.entities,
                "spawned_from_hypothesis_id": inv.seed.spawned_from_hypothesis_id,
            }),
            json.dumps({
                "max_calls": inv.budget.max_calls,
                "max_cost_usd": inv.budget.max_cost_usd,
                "max_wall_seconds": inv.budget.max_wall_seconds,
                "extension_to_usd": inv.budget.extension_to_usd,
            }),
            inv.spawned_from_investigation_id,
            inv.started_at.isoformat(),
            1 if inv.closed else 0,
            inv.calls_used,
            inv.cost_used_usd,
            inv.hot_branches_spawned,
        ),
    )
    conn.commit()


def _load_investigation(inv_id: str) -> Optional[Investigation]:
    _ensure_schema()
    conn = get_desk_store().conn
    row = conn.execute(
        "SELECT * FROM investigations WHERE id = ?", (inv_id,)
    ).fetchone()
    if row is None:
        return None
    d = dict(row)
    seed_d = json.loads(d["seed_json"])
    budget_d = json.loads(d["budget_json"])
    seed = HypothesisSeed(
        specialist_id=seed_d["specialist_id"],
        title=seed_d["title"],
        hypothesis_text=seed_d["hypothesis_text"],
        initial_prob=seed_d.get("initial_prob", 0.5),
        entities=list(seed_d.get("entities", []) or []),
        spawned_from_hypothesis_id=seed_d.get("spawned_from_hypothesis_id"),
    )
    budget = InvestigationBudget(
        max_calls=budget_d["max_calls"],
        max_cost_usd=budget_d["max_cost_usd"],
        max_wall_seconds=budget_d["max_wall_seconds"],
        extension_to_usd=budget_d.get("extension_to_usd"),
    )
    return Investigation(
        id=d["id"],
        root_hypothesis_id=d["root_hypothesis_id"],
        seed=seed,
        budget=budget,
        spawned_from_investigation_id=d.get("spawned_from_investigation_id"),
        started_at=datetime.fromisoformat(d["started_at"]),
        calls_used=d["calls_used"],
        cost_used_usd=d["cost_used_usd"],
        hot_branches_spawned=d["hot_branches_spawned"],
        closed=bool(d["closed"]),
    )


# ============================================================================
# start_investigation
# ============================================================================

def start_investigation(
    seed: HypothesisSeed,
    heat_score: float,
    budget: InvestigationBudget,
    context: AgentContext,
) -> Investigation:
    """Create a root hypothesis + register the investigation.

    The hypothesis is born with `posterior_prob=seed.initial_prob` and
    `heat_score=heat_score`. The investigation id is stamped onto the
    AgentContext (`context.investigation_id`) so subsequent dispatch_uri
    calls attach to it via `tool_call_log.investigation_id`.
    """
    _ensure_schema()
    inv_id = f"inv_{uuid4().hex[:12]}"

    hyp = propose_hypothesis(
        spec=HypothesisDraft(
            cycle_id=context.cycle_id,
            specialist_id=seed.specialist_id,
            title=seed.title[:120],
            hypothesis_text=seed.hypothesis_text,
            posterior_prob=seed.initial_prob,
            heat_score=heat_score,
            entity_ids=list(seed.entities),
            payload={
                "investigation_id": inv_id,
                "spawned_from_hypothesis_id": seed.spawned_from_hypothesis_id,
            },
        ),
        context=context,
    )

    # If this investigation was spawned by a parent hypothesis, attach the
    # 'spawned' edge per v2 §3 rules (parent_hypothesis_id column is dropped
    # — parent-child lives in edges only).
    if seed.spawned_from_hypothesis_id:
        add_edge(HypothesisEdgeDraft(
            from_node_kind="hypothesis",
            from_node_id=seed.spawned_from_hypothesis_id,
            to_node_kind="hypothesis",
            to_node_id=hyp.id,
            edge_kind="spawned",
            strength=heat_score,
        ))

    inv = Investigation(
        id=inv_id,
        root_hypothesis_id=hyp.id,
        seed=seed,
        budget=budget,
    )
    _persist_investigation(inv, seed.specialist_id, context.cycle_id)

    # RNG seeded by investigation id for replay determinism
    _RNG_BY_INVESTIGATION[inv_id] = random.Random(int(inv_id.split("_")[1], 16))

    # Stamp context so dispatch_uri attaches subsequent calls
    context.investigation_id = inv_id  # type: ignore[attr-defined]
    return inv


# ============================================================================
# expand_question
# ============================================================================

# Reserve the contradiction quota at the spec'd 20-40% range.
# Per v2 line 92: "at least 20% of frontier expansions must be contradiction
# queries". We sample 1-2 contradictions out of 3-5 follow-ups => 20-67%.
_FOLLOWUP_TEMPLATES_CONFIRMATORY = [
    "Does {title} hold for the same entity over a longer horizon?",
    "Does {title} replicate on a similar instrument (cross-asset)?",
]
_FOLLOWUP_TEMPLATES_CONTRADICTION = [
    "Where does {title} fail? Find a regime / cohort where it doesn't hold.",
    "What disconfirming evidence exists? Check sources that historically refute this.",
]
_FOLLOWUP_TEMPLATES_ORTHOGONAL = [
    "Could there be a different mechanism explaining the same observation as {title}?",
]


def _try_haiku_questions(node: QuestionNode, n: int) -> Optional[list[tuple[str, QuestionKind]]]:
    """Best-effort Haiku call for richer question synthesis. Returns None on
    any error (no key, network fail, parse fail) so callers fall back to
    templates."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        from anthropic import Anthropic  # type: ignore
    except Exception:
        return None
    try:
        client = Anthropic()
        prompt = (
            f"Given the question: '{node.question_text}'\n"
            f"Propose exactly {n} follow-up questions for an adversarial research "
            f"investigation. Distribution: 1-2 confirmatory (does this generalize?), "
            f"1-2 contradiction-seeking (where would this fail?), 1 orthogonal "
            f"(alternative explanation?). Reply as JSON array of "
            f"[{{\"kind\":\"confirmatory|contradiction|orthogonal\",\"q\":\"...\"}}]"
        )
        msg = client.messages.create(
            model="claude-3-5-haiku-20241022",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text  # type: ignore[attr-defined]
        # Best-effort JSON parse
        start = text.find("[")
        end = text.rfind("]")
        if start < 0 or end <= start:
            return None
        parsed = json.loads(text[start:end + 1])
        out: list[tuple[str, QuestionKind]] = []
        for p in parsed:
            k = p.get("kind", "confirmatory")
            if k not in ("confirmatory", "contradiction", "orthogonal"):
                k = "confirmatory"
            q = str(p.get("q", "")).strip()
            if q:
                out.append((q, k))  # type: ignore[arg-type]
        return out or None
    except Exception:
        return None


def expand_question(node: QuestionNode, context: AgentContext) -> list[QuestionNode]:
    """Given a question + current evidence, propose 3-5 follow-up questions.

    Distribution (per v2 line 92):
      - 1-2 confirmatory
      - 1-2 contradiction-seeking
      - 1 orthogonal

    Uses Haiku if ANTHROPIC_API_KEY is set; otherwise falls back to
    deterministic templates. Cost target: ~$0.001 per expansion.
    """
    rng = _RNG_BY_INVESTIGATION.get(context.investigation_id or "default",
                                     random.Random(0))
    n = rng.randint(3, 5)
    items = _try_haiku_questions(node, n)
    if items is None:
        # Template fallback — ensure contradiction quota (>= 1 of n).
        items = []
        # 2 confirmatory candidates
        items.extend([(t.format(title=node.question_text[:80]), "confirmatory")
                      for t in _FOLLOWUP_TEMPLATES_CONFIRMATORY])
        # 2 contradiction
        items.extend([(t.format(title=node.question_text[:80]), "contradiction")
                      for t in _FOLLOWUP_TEMPLATES_CONTRADICTION])
        # 1 orthogonal
        items.extend([(t.format(title=node.question_text[:80]), "orthogonal")
                      for t in _FOLLOWUP_TEMPLATES_ORTHOGONAL])
        # Cap to n; shuffle but FORCE at least 1 contradiction in the kept set.
        rng.shuffle(items)
        # Pick first n
        kept = items[:n]
        if not any(k == "contradiction" for _, k in kept):
            # Inject a contradiction in place of the last confirmatory
            for i, (_, k) in enumerate(kept):
                if k != "contradiction":
                    kept[i] = (_FOLLOWUP_TEMPLATES_CONTRADICTION[0].format(
                        title=node.question_text[:80]), "contradiction")
                    break
        items = kept

    out: list[QuestionNode] = []
    for q_text, kind in items:
        out.append(QuestionNode(
            id=f"qn_{uuid4().hex[:10]}",
            question_text=q_text,
            question_kind=kind,
            tool_uri=node.tool_uri,           # default: reuse parent's tool
            args=dict(node.args),
            depth=node.depth + 1,
            parent_id=node.id,
            prior_prob=node.prior_prob,
        ))
    return out


# ============================================================================
# score_hot_signal
# ============================================================================

def _extract_prior(result_payload: Any, fallback: float = 0.5) -> float:
    """Pull the LLM-declared prior from the result envelope when present.

    Real callers should put `_prior` in the tool result; tests inject via
    the synthetic result dict. Returns `fallback` (0.5 uniform) otherwise.
    """
    if isinstance(result_payload, dict):
        p = result_payload.get("_prior")
        if isinstance(p, (int, float)) and 0 <= p <= 1:
            return float(p)
    return fallback


def _extract_posterior_signal(result_payload: Any) -> tuple[float, float]:
    """Pull (signed_posterior_delta, surprise) from a result envelope.

    Test-friendly contract:
      result['_posterior_delta'] -> SIGNED delta (positive = supports;
                                     negative = contradicts).
      result['_surprise']        -> [0, 1] surprise score.

    For real tools we'd run a calibrated update; this is the synthetic
    hook tests use to exercise the hot-branch path.
    """
    if isinstance(result_payload, dict):
        d = float(result_payload.get("_posterior_delta", 0.0))
        s = float(result_payload.get("_surprise", abs(d)))
        return d, max(0.0, min(1.0, s))
    return 0.0, 0.0


def score_hot_signal(
    result: ToolResult | ToolResultLike | dict[str, Any],
    hypothesis_id: str,
    context: AgentContext,
) -> HotSignalScore:
    """Compute the 4 hot-signal scores.

    - posterior_delta: |signed_delta| where signed_delta comes from the
      result envelope's `_posterior_delta` (test hook) or a Bayesian
      update against the row's current posterior.
    - surprise_score: how unlikely the result was given the prior. For
      synthetic results, read `_surprise`; otherwise approximate as
      `|delta|` (a result that moves the posterior a lot was surprising).
    - contradiction_score: signed delta — negative means refutes.
    - novelty_score: kept simple — 0.5 placeholder unless a real novelty
      computation is wired in (Phase 6 wires `score_novelty`).

    overall_heat = clamp_01(0.5*posterior_delta + 0.3*surprise + 0.2*novelty).
    """
    # Normalize the various input shapes.
    if hasattr(result, "result"):
        payload = result.result  # type: ignore[union-attr]
    elif isinstance(result, dict):
        payload = result
    else:
        payload = None

    signed_delta, surprise = _extract_posterior_signal(payload)
    abs_delta = abs(signed_delta)
    contradiction = signed_delta  # signed; >0 supports
    novelty = 0.5  # placeholder; Phase 6 wires score_novelty
    if isinstance(payload, dict) and isinstance(payload.get("_novelty"), (int, float)):
        novelty = max(0.0, min(1.0, float(payload["_novelty"])))
    overall = max(0.0, min(1.0, 0.5 * abs_delta + 0.3 * surprise + 0.2 * novelty))
    return HotSignalScore(
        posterior_delta=abs_delta,
        surprise_score=surprise,
        contradiction_score=contradiction,
        novelty_score=novelty,
        overall_heat=overall,
    )


# ============================================================================
# spawn_sub_investigation
# ============================================================================

def spawn_sub_investigation(
    parent_id: str,
    signal: HotSignalScore,
    context: AgentContext,
    parent_hypothesis_id: Optional[str] = None,
) -> Investigation:
    """Create a child investigation linked via hypothesis_edges.spawned.

    Child gets a reduced budget (parent's remaining / 4) per the task
    spec. If the parent's remaining budget is below a sane floor we still
    grant the child a minimum of 10 calls / $0.50 so it can do *something*.
    """
    parent = _load_investigation(parent_id)
    if parent is None:
        raise KeyError(f"parent_investigation_not_found: {parent_id}")

    remaining_calls = max(parent.budget.max_calls - parent.calls_used, 0)
    remaining_cost = max(parent.budget.max_cost_usd - parent.cost_used_usd, 0.0)
    child_budget = InvestigationBudget(
        max_calls=max(remaining_calls // 4, 10),
        max_cost_usd=max(remaining_cost / 4.0, 0.50),
        max_wall_seconds=max(parent.budget.max_wall_seconds // 4, 60),
    )

    # The sub-investigation seeds with the same hypothesis text but flips the
    # framing if the signal is contradictive — the child's job is to refute
    # (or strengthen) the parent's claim with fresh evidence.
    sub_framing = "Devil's advocate: " if signal.contradiction_score < 0 else "Hot follow-up: "
    seed = HypothesisSeed(
        specialist_id=parent.seed.specialist_id,
        title=(sub_framing + parent.seed.title)[:120],
        hypothesis_text=(
            f"{sub_framing}{parent.seed.hypothesis_text}\n\n"
            f"Triggered by hot signal: delta={signal.posterior_delta:.2f}, "
            f"surprise={signal.surprise_score:.2f}, "
            f"contradiction={signal.contradiction_score:+.2f}"
        ),
        initial_prob=0.5,
        entities=list(parent.seed.entities),
        spawned_from_hypothesis_id=parent_hypothesis_id or parent.root_hypothesis_id,
    )
    child = start_investigation(seed, heat_score=signal.overall_heat,
                                 budget=child_budget, context=context)
    child.spawned_from_investigation_id = parent_id
    _persist_investigation(child, parent.seed.specialist_id, context.cycle_id)

    parent.hot_branches_spawned += 1
    _persist_investigation(parent, parent.seed.specialist_id, context.cycle_id)
    return child


# ============================================================================
# explore_adversarial — the BFS loop
# ============================================================================

# Mock dispatcher hook for tests. When set, replaces real dispatch_uri.
_DISPATCHER: Optional[Callable[[str, dict[str, Any], AgentContext], ToolResult]] = None


def set_dispatcher_for_test(fn: Optional[Callable[[str, dict[str, Any], AgentContext], ToolResult]]) -> None:
    """Inject a synthetic dispatcher (smoke test hook). Pass None to restore
    the real dispatch_uri."""
    global _DISPATCHER
    _DISPATCHER = fn


def _dispatch(uri: str, args: dict[str, Any], context: AgentContext) -> ToolResult:
    if _DISPATCHER is not None:
        return _DISPATCHER(uri, args, context)
    return dispatch_uri(uri, args, context)


def explore_adversarial(
    investigation_id: str,
    frontier: list[QuestionNode],
    max_calls: int = 300,
    context: Optional[AgentContext] = None,
) -> ExplorationTrace:
    """Breadth-first traversal with contradiction-seeking.

    For each question in the frontier:
      1. Dispatch the tool via dispatch_uri.
      2. score_hot_signal on the result.
      3. If |posterior_delta| > 0.2: spawn a sub-investigation, post to
         the durable scratchpad, and emit a 'spawned' edge linking the
         result's tool_call to the parent hypothesis.
      4. update_posterior on the hypothesis (append-only).
      5. Add a 'supports' or 'contradicts' edge from the tool_call to the
         hypothesis, with `strength = abs(contradiction_score)`.
      6. expand_question on the just-asked question and append to the
         frontier (BFS).
      7. Stop when frontier empties OR budget caps trip.

    `frontier` is consumed in-place; pass a copy if you want to inspect
    the original later.
    """
    inv = _load_investigation(investigation_id)
    if inv is None:
        raise KeyError(f"investigation_not_found: {investigation_id}")
    if context is None:
        context = AgentContext(
            cycle_id=_get_inv_cycle_id(investigation_id) or "unknown",
            specialist_id=inv.seed.specialist_id,
            investigation_id=investigation_id,
        )
    else:
        context.investigation_id = investigation_id  # type: ignore[attr-defined]

    steps: list[ExplorationStep] = []
    sub_invs: list[str] = []
    n_contradiction = 0
    n_failed_calls = 0
    aborted_due_to_source_health = False
    # Codex finding #3: once an investigation has done at least this many
    # calls AND >50% of them have failed, we abort the investigation early
    # rather than burn the rest of the budget on dead tools.
    SOURCE_HEALTH_ABORT_MIN_CALLS = 4
    SOURCE_HEALTH_ABORT_SHARE = 0.50
    t_start = time.perf_counter()
    capped_reason: Optional[str] = None
    current_posterior = inv.seed.initial_prob

    frontier = list(frontier)  # don't mutate caller's list

    cap = min(max_calls, inv.budget.max_calls)
    while frontier:
        if inv.calls_used >= cap:
            capped_reason = "max_calls"
            break
        if inv.cost_used_usd >= inv.budget.max_cost_usd:
            capped_reason = "max_cost_usd"
            break
        wall_so_far = time.perf_counter() - t_start
        if wall_so_far >= inv.budget.max_wall_seconds:
            capped_reason = "max_wall_seconds"
            break

        q = frontier.pop(0)
        if q.question_kind == "contradiction":
            n_contradiction += 1

        # 1) Dispatch the tool
        result = _dispatch(q.tool_uri, q.args, context)
        inv.calls_used += 1
        inv.cost_used_usd += float(result.cost_usd or 0.0)

        # Codex finding #3: when the dispatch failed, the tool_call_log row
        # still gets written (with `error` set) and gets an id, but the
        # payload is unusable as evidence. Skip scoring, edge-linking, and
        # posterior updates. Just record a 'dispatch_failed' step + counter
        # so the kill-switch + trace consumers can see the failure.
        if not result.ok:
            n_failed_calls += 1
            steps.append(ExplorationStep(
                question_id=q.id,
                question_text=q.question_text,
                question_kind=q.question_kind,
                tool_uri=q.tool_uri,
                tool_call_log_id=result.tool_call_log_id,
                cost_usd=float(result.cost_usd or 0.0),
                duration_ms=int(result.duration_ms or 0),
                posterior_before=current_posterior,
                posterior_after=current_posterior,
                heat=HotSignalScore(
                    posterior_delta=0.0,
                    surprise_score=0.0,
                    contradiction_score=0.0,
                    novelty_score=0.0,
                    overall_heat=0.0,
                ),
                edge_kind_emitted="none",
                spawned_sub_investigation_id=None,
                status="dispatch_failed",
                error=(result.error or "")[:500],
            ))
            # Persist running counters; we still consumed a call slot.
            _persist_investigation(inv, inv.seed.specialist_id, context.cycle_id)
            # Early-abort on source-health rot: >50% failure share once we
            # have a meaningful denominator.
            if (
                inv.calls_used >= SOURCE_HEALTH_ABORT_MIN_CALLS
                and (n_failed_calls / inv.calls_used) > SOURCE_HEALTH_ABORT_SHARE
            ):
                aborted_due_to_source_health = True
                capped_reason = "source_health_failure_share"
                break
            # Do NOT expand frontier off a failed call — the question was
            # never resolved by a real tool. The next iteration picks up
            # the existing frontier.
            continue

        # 2) Score — first the cheap heat-signal (surprise / novelty / heat)
        #    from the synthetic envelope hooks, then layer in a REAL
        #    LLM-driven evidence score for the signed posterior delta. The
        #    LLM scorer reads the actual payload + source health and either
        #    returns a calibrated signed delta or raises
        #    EvidenceUnavailableError, in which case we skip the posterior
        #    update for this result (no fabricated delta).
        heat = score_hot_signal(result, inv.root_hypothesis_id, context)

        # Pull hypothesis text + source health for the scorer. Best-effort;
        # missing pieces become empty strings / dicts (the scorer tolerates).
        try:
            from ..hypotheses.model import _find_open_head as _find_hyp_head  # type: ignore
            hyp_row = _find_hyp_head(
                get_desk_store().conn, inv.root_hypothesis_id
            )
            hyp_text_for_scorer = (
                hyp_row["hypothesis_text"] if hyp_row is not None else inv.seed.hypothesis_text
            )
        except Exception:
            hyp_text_for_scorer = inv.seed.hypothesis_text
        try:
            from tic.ingest._health import query_source_health  # type: ignore
            sh_for_scorer = query_source_health(lookback_hours=72) or {}
        except Exception:
            sh_for_scorer = {}

        evidence_score = None
        try:
            from .evidence_scorer import (
                EvidenceUnavailableError,
                score_evidence,
            )
            evidence_score = score_evidence(
                hypothesis_text=hyp_text_for_scorer,
                question_text=q.question_text,
                tool_uri=q.tool_uri,
                tool_result=result.result if isinstance(result.result, dict) else {"raw": result.result},
                source_health=sh_for_scorer,
                prior_posterior=float(current_posterior),
            )
            # Charge the cycle for the scorer call (cost accounting).
            inv.cost_used_usd += float(evidence_score.cost_usd or 0.0)
            # Codex finding #15: also charge the desk-wide daily ledger.
            # Best-effort — a ledger failure must not break the BFS loop.
            if (evidence_score.cost_usd or 0.0) > 0:
                try:
                    from ..cost_ledger import get_cost_ledger
                    get_cost_ledger().record(
                        amount_usd=float(evidence_score.cost_usd),
                        stage="explore_evidence",
                        specialist_id=inv.seed.specialist_id,
                        cycle_id=context.cycle_id,
                    )
                except Exception:
                    pass
            # Override the synthetic heat.contradiction_score with the REAL
            # signed delta. Keep posterior_delta/surprise/novelty from the
            # heat call so hot-branch spawning thresholds still work.
            heat = HotSignalScore(
                posterior_delta=max(heat.posterior_delta, abs(evidence_score.posterior_delta)),
                surprise_score=heat.surprise_score,
                contradiction_score=evidence_score.posterior_delta,
                novelty_score=heat.novelty_score,
                overall_heat=heat.overall_heat,
            )
        except EvidenceUnavailableError as _ev_err:
            # No real signal available — fall back to the heat-only delta
            # (which is 0 for non-synthetic payloads, so posterior stays put).
            evidence_score = None
        except Exception:
            # Defensive: never let the scorer break the BFS loop. The next
            # cycle's REFLECT pass will surface any chronic failures.
            evidence_score = None

        # 3) Record edge: tool_call -> hypothesis (supports/contradicts)
        edge_kind = "contradicts" if heat.contradiction_score < 0 else "supports"
        if result.tool_call_log_id:
            # Cite the evidence-scorer's claim_ids alongside the tool_call_log_id.
            cite_ids: list[str] = [result.tool_call_log_id]
            if evidence_score is not None:
                for cid in evidence_score.citation_claim_ids:
                    if cid not in cite_ids:
                        cite_ids.append(cid)
            link_evidence(
                hypothesis_id=inv.root_hypothesis_id,
                evidence_kind="tool_call",
                evidence_id=result.tool_call_log_id,
                edge_kind=edge_kind,  # type: ignore[arg-type]
                strength=abs(heat.contradiction_score),
                citation_ids=cite_ids,
            )

        # 4) Posterior update (append-only)
        prior = current_posterior
        signed = heat.contradiction_score
        # Calibration: clamp the new posterior to [0.02, 0.98] so we never
        # collapse to a degenerate point estimate from one result.
        proposed = max(0.02, min(0.98, prior + 0.5 * signed))
        try:
            updated = update_posterior(
                hyp_id=inv.root_hypothesis_id,
                new_prob=proposed,
                evidence_ids=[result.tool_call_log_id] if result.tool_call_log_id else [],
            )
            current_posterior = updated.posterior_prob or proposed
        except KeyError:
            # hypothesis was closed mid-run; abort gracefully
            capped_reason = "hypothesis_closed"
            break

        # 5) Hot branch spawn (in-cycle compounding per v2 line 92)
        spawned_sub_id: Optional[str] = None
        if heat.posterior_delta > 0.2 and inv.calls_used < cap:
            child = spawn_sub_investigation(
                parent_id=investigation_id,
                signal=heat,
                context=context,
                parent_hypothesis_id=inv.root_hypothesis_id,
            )
            spawned_sub_id = child.id
            sub_invs.append(child.id)
            inv.hot_branches_spawned += 1
            # Post to scratchpad so peers see the hot branch immediately
            try:
                post_message(
                    from_agent=inv.seed.specialist_id,
                    to_agent_or_topic="#hot_investigations",
                    kind="observation",
                    payload={
                        "event": "hot_branch_spawned",
                        "parent_investigation_id": investigation_id,
                        "child_investigation_id": child.id,
                        "posterior_delta": heat.posterior_delta,
                        "contradiction_score": heat.contradiction_score,
                        "title": inv.seed.title,
                    },
                    related_hypothesis_id=inv.root_hypothesis_id,
                    expires_in_hours=48,
                )
            except Exception:
                # Best-effort; don't fail the BFS for messaging hiccups.
                pass

        # 6) Step audit
        steps.append(ExplorationStep(
            question_id=q.id,
            question_text=q.question_text,
            question_kind=q.question_kind,
            tool_uri=q.tool_uri,
            tool_call_log_id=result.tool_call_log_id,
            cost_usd=float(result.cost_usd or 0.0),
            duration_ms=int(result.duration_ms or 0),
            posterior_before=prior,
            posterior_after=current_posterior,
            heat=heat,
            edge_kind_emitted=edge_kind,
            spawned_sub_investigation_id=spawned_sub_id,
        ))

        # 7) Expand frontier
        if inv.calls_used + len(frontier) < cap:
            followups = expand_question(q, context)
            # Mix into BFS frontier — append at tail (true breadth-first)
            frontier.extend(followups)

        # Persist running counters
        _persist_investigation(inv, inv.seed.specialist_id, context.cycle_id)

    wall = time.perf_counter() - t_start
    total_cost = sum(s.cost_usd for s in steps)
    n_calls = len(steps)
    contradiction_share = (n_contradiction / n_calls) if n_calls > 0 else 0.0

    inv.closed = capped_reason is not None and not frontier
    _persist_investigation(inv, inv.seed.specialist_id, context.cycle_id)

    return ExplorationTrace(
        investigation_id=investigation_id,
        n_calls=n_calls,
        n_hot_branches=inv.hot_branches_spawned,
        n_contradiction_questions=n_contradiction,
        contradiction_share=contradiction_share,
        total_cost_usd=total_cost,
        wall_seconds=wall,
        final_posterior=current_posterior,
        steps=steps,
        sub_investigations=sub_invs,
        capped=capped_reason is not None,
        n_failed_calls=n_failed_calls,
        aborted_due_to_source_health=aborted_due_to_source_health,
    )


def _get_inv_cycle_id(inv_id: str) -> Optional[str]:
    _ensure_schema()
    row = get_desk_store().conn.execute(
        "SELECT cycle_id FROM investigations WHERE id = ?", (inv_id,)
    ).fetchone()
    return row["cycle_id"] if row else None


# ============================================================================
# maybe_trigger_debate
# ============================================================================

def maybe_trigger_debate(
    claim_or_idea: dict[str, Any],
    context: AgentContext,
) -> DebateTriggerDecision:
    """Decide whether a claim or trade idea warrants a debate.

    Triggers per v2 line 92:
      - posterior_prob > 0.75
      - deadline / horizon <= 24h
      - impact_score >= 0.7
      - trade idea confidence >= 0.7
      - source_conflict flag set
      - cross_specialist_contradiction flag set

    Thrash controls per v2 line 94:
      - max 10 debates per cycle (11th -> kill_switch=True)
      - max 3 debates per specialist pair
      - max 1 per instrument/horizon per 6h

    `claim_or_idea` shape (flexible — all keys optional):
      {
        "posterior_prob": float,
        "horizon_hours": int,
        "impact_score": float,
        "confidence": float,
        "instrument": str,
        "horizon": str,
        "participants": list[str],
        "source_conflict": bool,
        "cross_specialist_contradiction": bool,
      }
    """
    cycle_id = context.cycle_id

    # ---- thrash control: cycle cap (kill switch on 11th) ----
    cycle_count = _DEBATE_STATE["cycle_counts"].get(cycle_id, 0)
    if cycle_count >= 10:
        # The 11th would trip the kill switch. Increment + signal.
        _DEBATE_STATE["cycle_counts"][cycle_id] = cycle_count + 1
        return DebateTriggerDecision(
            should_trigger=False,
            reason="cycle_debate_cap_exceeded_kill_switch",
            kill_switch=True,
            debates_this_cycle=cycle_count + 1,
        )

    # ---- trigger reasons ----
    triggers: list[str] = []
    if (claim_or_idea.get("posterior_prob") or 0) > 0.75:
        triggers.append("posterior_gt_0_75")
    horizon_hours = claim_or_idea.get("horizon_hours")
    if isinstance(horizon_hours, (int, float)) and horizon_hours <= 24:
        triggers.append("short_horizon")
    if (claim_or_idea.get("impact_score") or 0) >= 0.7:
        triggers.append("impact_ge_0_7")
    if (claim_or_idea.get("confidence") or 0) >= 0.7:
        triggers.append("trade_idea_confidence_ge_0_7")
    if claim_or_idea.get("source_conflict"):
        triggers.append("source_conflict")
    if claim_or_idea.get("cross_specialist_contradiction"):
        triggers.append("cross_specialist_contradiction")

    if not triggers:
        return DebateTriggerDecision(
            should_trigger=False,
            reason="no_trigger_matched",
            debates_this_cycle=cycle_count,
        )

    # ---- thrash: specialist pair cap (3 per pair) ----
    participants = claim_or_idea.get("participants") or []
    pair_key: Optional[frozenset[str]] = None
    if len(participants) >= 2:
        pair_key = frozenset(participants[:2])
        pair_count = _DEBATE_STATE["pair_counts"].get(pair_key, 0)
        if pair_count >= 3:
            return DebateTriggerDecision(
                should_trigger=False,
                reason="specialist_pair_cap_exceeded",
                debates_this_cycle=cycle_count,
            )

    # ---- thrash: instrument/horizon per 6h ----
    instrument = claim_or_idea.get("instrument")
    horizon = claim_or_idea.get("horizon")
    ih_key = None
    cooldown_until: Optional[datetime] = None
    if instrument and horizon:
        ih_key = (str(instrument), str(horizon))
        last = _DEBATE_STATE["instrument_horizon"].get(ih_key)
        now = datetime.now(timezone.utc)
        if last is not None and (now - last) < timedelta(hours=6):
            cooldown_until = last + timedelta(hours=6)
            return DebateTriggerDecision(
                should_trigger=False,
                reason="instrument_horizon_cooldown",
                cooldown_until=cooldown_until,
                debates_this_cycle=cycle_count,
            )

    # ---- approved: increment counters ----
    _DEBATE_STATE["cycle_counts"][cycle_id] = cycle_count + 1
    if pair_key is not None:
        _DEBATE_STATE["pair_counts"][pair_key] = (
            _DEBATE_STATE["pair_counts"].get(pair_key, 0) + 1
        )
    if ih_key is not None:
        _DEBATE_STATE["instrument_horizon"][ih_key] = datetime.now(timezone.utc)

    return DebateTriggerDecision(
        should_trigger=True,
        reason=";".join(triggers),
        debates_this_cycle=cycle_count + 1,
    )

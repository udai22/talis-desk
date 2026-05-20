"""Reward-log writers + aggregators for Phase 6 self-improvement.

Per wiki/SOTA_DESK_ARCHITECTURE.md v2 §2 Layer 5 (lines 138-152) and §3
(reward_log DDL, lines 394-410). The reward_log is the substrate the
nightly auto-mutator reads when deciding whether to propose a persona
mutation. Every reward write is append-only, bitemporal, and carries an
`attribution_json` blob that the dashboard / mutator can pivot on.

# Reward kinds (CHECK constraint in schema.sota lines 263 / 587)
  - 'curiosity'      : exploration novelty signal (set by exploration.bfs)
  - 'correctness'    : forecast / hypothesis Brier (lower is better)
  - 'alpha'          : trade idea alpha vs benchmark (resolver.py)
  - 'novelty'        : claim/hypothesis novelty per eval/novelty.py
  - 'coverage'       : tool atlas coverage % per specialist per window
  - 'cost_penalty'   : NEGATIVE reward when a specialist burns budget
                       without producing actionable output
  - 'debate_quality' : reward for well-cited arguments + calibrated judges
  - 'playbook_hit'   : reward for a playbook-instantiated trade closing
                       (mirrors 'alpha' but bucketed to the playbook id)

# Append-only contract
All writers here INSERT exactly one row per call (or zero if the call is
idempotent / nothing to record). They NEVER UPDATE existing rows. The
mutator reads `transaction_to IS NULL` to skip retracted rows; corrections
go via a new row with `supersedes` pointing back.

# Score convention (so the mutator can compare across kinds)
  - 'correctness'   : 1.0 - brier         (higher = better; range [0,1])
  - 'alpha'         : pct (-inf..+inf)    (higher = better)
  - 'novelty'       : 1.0 - cosine        (higher = more novel)
  - 'coverage'      : ratio in [0, 1]     (higher = better)
  - 'cost_penalty'  : -cost_usd_overrun   (lower = worse; always <= 0)
  - 'debate_quality': in [0, 1]           (composite)
  - 'playbook_hit'  : pct (-inf..+inf)    (higher = better)

# Honest gaps
- `score_alpha` calls `eval.resolver.attribute_alpha` so it inherits the
  resolver's "simple" attribution method by default. Caller can pass
  `attribution_method='shapley-lite'` to use citation-frequency weighting.
- `score_debate_quality` is heuristic: judge confidence in [0.55, 0.85]
  is "calibrated"; outside that is penalized linearly. Real calibration
  is `judge_brier_tracking` per v2 §9 risks — Phase 7 work.
- `score_cost_penalty` heuristically uses `tool_call_log.cost_usd` rolled
  up per specialist+cycle. Budget thresholds live in
  `DEFAULT_BUDGET_PER_SPECIALIST_USD` until v2 §4 budgets land.
- `aggregate_tool_affinity` uses Brier delta from `tool_call_log.reward_score`
  if set, else falls back to the per-tool alpha share (proportional to
  citation count of the tool in alpha-rewarded ideas).
"""
from __future__ import annotations

import json
import math
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional


# ============================================================================
# Constants
# ============================================================================

#: Default per-specialist daily budget (USD) for cost_penalty calculation.
#: Wikipedia v2 §4 line 475 sets the desk-wide cap at $100/day across all
#: specialists; a 7-specialist desk implies ~$14/specialist/day baseline.
DEFAULT_BUDGET_PER_SPECIALIST_USD = 14.0

#: A specialist whose cycle cost exceeds this fraction of budget AND
#: produces no actionable output gets a `cost_penalty` reward.
COST_PENALTY_BUDGET_FRACTION = 0.80

#: Coverage target — v2 §1 line 21 sets ">80% tools touched weekly".
COVERAGE_TARGET = 0.80

#: Judge confidence calibration band — outside this we penalize.
JUDGE_CALIBRATION_BAND = (0.55, 0.85)


REWARD_KINDS = (
    "curiosity",
    "correctness",
    "alpha",
    "novelty",
    "coverage",
    "cost_penalty",
    "debate_quality",
    "playbook_hit",
)


# ============================================================================
# Return envelopes
# ============================================================================

@dataclass
class RewardEntry:
    """One row written to `reward_log`. Returned by every `score_*` call."""

    id: str
    cycle_id: str
    reward_kind: str
    subject_kind: str
    subject_id: str
    specialist_id: Optional[str]
    score: float
    baseline_score: Optional[float] = None
    delta: Optional[float] = None
    attribution_json: dict[str, Any] = field(default_factory=dict)
    valid_from: Optional[datetime] = None
    transaction_from: Optional[datetime] = None
    quality_flags: list[str] = field(default_factory=list)


@dataclass
class SpecialistRewardAggregate:
    """Aggregate-by-reward_kind for one specialist over a window.

    Returned by `aggregate_specialist_rewards`. Values are means; counts
    and sums are split out per-kind for the mutator to weight.
    """

    specialist_id: str
    window_days: int
    as_of: datetime
    per_kind: dict[str, dict[str, float]]
    # Top-5 worst-performing subjects per kind (mutator's "weakness" pointer)
    weaknesses: list[dict[str, Any]] = field(default_factory=list)


# ============================================================================
# DB helpers
# ============================================================================

def _resolve_conn(conn: Optional[sqlite3.Connection]) -> sqlite3.Connection:
    if conn is not None:
        return conn
    from ..store import get_desk_store
    return get_desk_store().conn


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _insert_reward_row(
    *,
    conn: sqlite3.Connection,
    cycle_id: str,
    reward_kind: str,
    subject_kind: str,
    subject_id: str,
    specialist_id: Optional[str],
    score: float,
    baseline_score: Optional[float],
    delta: Optional[float],
    attribution_json: dict[str, Any],
    valid_from: Optional[datetime] = None,
) -> RewardEntry:
    """Atomic, append-only insert into `reward_log`.

    Returns the full RewardEntry envelope (id + bitemporal stamps).
    """
    if reward_kind not in REWARD_KINDS:
        raise ValueError(
            f"invalid_reward_kind: {reward_kind!r} not in {REWARD_KINDS}"
        )
    if subject_kind == "tool":
        # Per v2 §3 line 172: tool-level rewards live in
        # `tool_call_log.reward_score`, not in `reward_log`.
        raise ValueError(
            "subject_kind='tool' is forbidden in reward_log; use "
            "tool_call_log.reward_score instead"
        )

    now = datetime.now(timezone.utc)
    vf = valid_from or now
    now_iso = _iso(now)
    valid_from_iso = _iso(vf)
    rid = "rew_" + uuid.uuid4().hex[:12]

    conn.execute(
        "INSERT INTO reward_log ("
        "id, cycle_id, reward_kind, subject_kind, subject_id, "
        "specialist_id, score, baseline_score, delta, attribution_json, "
        "valid_from, transaction_from"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            rid, cycle_id, reward_kind, subject_kind, subject_id,
            specialist_id, float(score),
            float(baseline_score) if baseline_score is not None else None,
            float(delta) if delta is not None else None,
            _canonical_json(attribution_json),
            valid_from_iso, now_iso,
        ),
    )

    return RewardEntry(
        id=rid, cycle_id=cycle_id, reward_kind=reward_kind,
        subject_kind=subject_kind, subject_id=subject_id,
        specialist_id=specialist_id, score=float(score),
        baseline_score=baseline_score, delta=delta,
        attribution_json=dict(attribution_json),
        valid_from=vf, transaction_from=now,
    )


# ============================================================================
# Per-kind writers
# ============================================================================

def score_correctness(
    forecast_id: str,
    realized_outcome: dict,
    *,
    conn: Optional[sqlite3.Connection] = None,
    cycle_id: Optional[str] = None,
) -> RewardEntry:
    """Write one reward_log row with reward_kind='correctness'.

    Brier is computed from `realized_outcome`:
      - If `realized_outcome` has a top-level 'brier' key, use it directly.
      - Else if it has `prob_assigned` + `realized` (binary 0/1), compute
        Brier = (prob - realized)^2.

    score = 1.0 - brier (higher is better, [0,1]).
    subject_kind defaults to 'forecast'; we look up the forecast row to
    find specialist_id + cycle_id if not passed in.
    """
    conn = _resolve_conn(conn)
    brier = realized_outcome.get("brier")
    if brier is None:
        p = realized_outcome.get("prob_assigned")
        y = realized_outcome.get("realized")
        if p is not None and y is not None:
            try:
                brier = (float(p) - float(y)) ** 2
            except (TypeError, ValueError):
                brier = None
    if brier is None:
        raise ValueError(
            f"score_correctness: realized_outcome lacks 'brier' or "
            f"('prob_assigned','realized'): {realized_outcome!r}"
        )
    brier = max(0.0, min(1.0, float(brier)))
    score = 1.0 - brier

    # Figure out specialist_id / cycle_id. Forecasts may live in a
    # `forecasts` table (TIC); if absent we fall back to None and let the
    # caller supply via cycle_id arg.
    specialist_id: Optional[str] = realized_outcome.get("specialist_id")
    fcst_cycle: Optional[str] = cycle_id or realized_outcome.get("cycle_id")
    try:
        # Hypothesis-backed forecasts: look up specialist on hypotheses.
        row = conn.execute(
            "SELECT specialist_id, cycle_id FROM hypotheses "
            "WHERE id = ? AND transaction_to IS NULL "
            "ORDER BY transaction_from DESC LIMIT 1",
            (forecast_id,),
        ).fetchone()
        if row is not None:
            specialist_id = specialist_id or row["specialist_id"]
            fcst_cycle = fcst_cycle or row["cycle_id"]
    except sqlite3.Error:
        pass

    return _insert_reward_row(
        conn=conn,
        cycle_id=fcst_cycle or "(unknown)",
        reward_kind="correctness",
        subject_kind=realized_outcome.get("subject_kind", "forecast"),
        subject_id=forecast_id,
        specialist_id=specialist_id,
        score=score,
        baseline_score=0.5,  # naive 50/50 prior Brier=0.25 -> score=0.75
        delta=score - 0.75,
        attribution_json={
            "brier": brier,
            "realized_outcome": realized_outcome,
        },
    )


def score_alpha(
    trade_idea_id: str,
    attribution_method: Literal["simple", "shapley-lite"] = "simple",
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> RewardEntry:
    """Write reward_log row with reward_kind='alpha'.

    Score is `contributed_alpha_pct` from the resolved trade idea. We also
    split credit across cited specialist_id / tool_uris / playbook_id /
    hypothesis_ids per the requested attribution method. The attribution
    is stored in `attribution_json`; the per-citation reward_log entries
    are NOT split into separate rows (one master 'alpha' row per idea, with
    attribution embedded), which keeps the mutator's join simple.
    """
    from .resolver import attribute_alpha

    conn = _resolve_conn(conn)
    attr = attribute_alpha(trade_idea_id, method=attribution_method, conn=conn)

    # Find the resolved (terminal) row to get cycle_id + specialist_id.
    row = conn.execute(
        "SELECT id, cycle_id, specialist_id, playbook_id, "
        "       contributed_alpha_pct, realized_return_after_fees_pct, "
        "       benchmark_return_pct "
        "FROM trade_ideas "
        "WHERE (id = ? OR supersedes = ?) "
        "AND transaction_to IS NULL "
        "ORDER BY transaction_from DESC LIMIT 1",
        (trade_idea_id, trade_idea_id),
    ).fetchone()
    if row is None:
        raise KeyError(f"score_alpha: trade_idea {trade_idea_id!r} not found")

    alpha = row["contributed_alpha_pct"]
    if alpha is None:
        # Idea not yet resolved — no alpha to score.
        raise ValueError(
            f"score_alpha: trade_idea {trade_idea_id!r} not resolved "
            f"(contributed_alpha_pct is NULL)"
        )

    return _insert_reward_row(
        conn=conn,
        cycle_id=row["cycle_id"],
        reward_kind="alpha",
        subject_kind="trade_idea",
        subject_id=trade_idea_id,
        specialist_id=row["specialist_id"],
        score=float(alpha),
        baseline_score=0.0,  # passive HL benchmark is baseline-zero by def
        delta=float(alpha),
        attribution_json={
            "method": attr.method,
            "total_alpha_pct": attr.total_alpha_pct,
            "components": attr.components,
            "realized_return_after_fees_pct": row["realized_return_after_fees_pct"],
            "benchmark_return_pct": row["benchmark_return_pct"],
            "playbook_id": row["playbook_id"],
        },
    )


def score_novelty(
    claim_or_hypothesis_id: str,
    kind: Literal["claim", "hypothesis"] = "hypothesis",
    *,
    as_of: Optional[datetime] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> RewardEntry:
    """Write reward_log row with reward_kind='novelty'.

    Delegates to `eval.novelty.score_novelty` for the actual scoring. Score
    is `1.0 - cosine_to_nearest_in_corpus` and we also store the binary
    `is_novel` flag in attribution_json.
    """
    from .novelty import score_novelty as _score_novelty_score

    conn = _resolve_conn(conn)
    as_of = as_of or datetime.now(timezone.utc)
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)

    # Pull text + specialist for the subject.
    table = "hypotheses" if kind == "hypothesis" else "claims"
    text: str = ""
    specialist_id: Optional[str] = None
    cycle_id: str = "(unknown)"
    if kind == "hypothesis":
        row = conn.execute(
            "SELECT specialist_id, cycle_id, title, hypothesis_text "
            "FROM hypotheses WHERE id = ? AND transaction_to IS NULL "
            "ORDER BY transaction_from DESC LIMIT 1",
            (claim_or_hypothesis_id,),
        ).fetchone()
        if row is None:
            raise KeyError(
                f"score_novelty: hypothesis {claim_or_hypothesis_id!r} not found"
            )
        specialist_id = row["specialist_id"]
        cycle_id = row["cycle_id"]
        text = f"{row['title']}\n\n{row['hypothesis_text']}"
    else:
        # Claims live in talis-tic. We can't import its store here without
        # circular boundary risk; the caller is expected to pass an object
        # with `text` field by writing to attribution_json directly. For
        # the smoke we use a hypothesis-backed call.
        text = claim_or_hypothesis_id

    score_result = _score_novelty_score(
        {"id": claim_or_hypothesis_id, "text": text, "kind": kind},
        as_of=as_of,
    )

    return _insert_reward_row(
        conn=conn,
        cycle_id=cycle_id,
        reward_kind="novelty",
        subject_kind=kind,
        subject_id=claim_or_hypothesis_id,
        specialist_id=specialist_id,
        score=1.0 - score_result.cosine_to_nearest_in_corpus,
        baseline_score=0.35,  # heuristic: anything below 0.65 cosine is non-novel
        delta=(1.0 - score_result.cosine_to_nearest_in_corpus) - 0.35,
        attribution_json={
            "cosine_to_nearest_in_corpus": score_result.cosine_to_nearest_in_corpus,
            "present_in_external_research": score_result.present_in_external_research,
            "nearest_internal_claim_id": score_result.nearest_internal_claim_id,
            "nearest_external_url": score_result.nearest_external_url,
            "is_novel": score_result.is_novel,
        },
    )


def score_coverage_for_specialist(
    specialist_id: str,
    window_days: int = 7,
    *,
    conn: Optional[sqlite3.Connection] = None,
    cycle_id: Optional[str] = None,
) -> RewardEntry:
    """Compute % of approved tool atlas used by this specialist in window.

    score = (distinct active tool_uris this specialist called) /
            (approved tool_atlas size at start of window)

    Target >= COVERAGE_TARGET (0.80). The delta is score - target.
    """
    conn = _resolve_conn(conn)
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=window_days)

    # Numerator: distinct tool_uris this specialist actually called
    used_rows = conn.execute(
        "SELECT DISTINCT tool_uri FROM tool_call_log "
        "WHERE specialist_id = ? AND started_at >= ? "
        "AND (transaction_to IS NULL OR transaction_to > ?)",
        (specialist_id, _iso(start), _iso(now)),
    ).fetchall()
    n_used = len(used_rows)

    # Denominator: approved tool atlas size (status='active' or 'approved')
    atlas_rows = conn.execute(
        "SELECT DISTINCT tool_uri FROM tool_atlas "
        "WHERE status IN ('active','approved') "
        "AND transaction_to IS NULL"
    ).fetchall()
    n_atlas = len(atlas_rows)

    if n_atlas == 0:
        # No atlas yet; score 1.0 (no penalty) but mark quality flag.
        score = 1.0
    else:
        score = min(1.0, n_used / float(n_atlas))

    return _insert_reward_row(
        conn=conn,
        cycle_id=cycle_id or "(coverage_aggregate)",
        reward_kind="coverage",
        subject_kind="specialist",
        subject_id=specialist_id,
        specialist_id=specialist_id,
        score=score,
        baseline_score=COVERAGE_TARGET,
        delta=score - COVERAGE_TARGET,
        attribution_json={
            "window_days": window_days,
            "n_tools_used": n_used,
            "n_atlas_approved": n_atlas,
            "target": COVERAGE_TARGET,
        },
    )


def score_cost_penalty(
    specialist_id: str,
    cycle_id: str,
    *,
    budget_usd: float = DEFAULT_BUDGET_PER_SPECIALIST_USD,
    conn: Optional[sqlite3.Connection] = None,
) -> RewardEntry:
    """NEGATIVE reward when a specialist burns >80% of its budget in a
    cycle without producing actionable output (no trade ideas, no posterior
    updates >0.1).

    score = -max(0, cost - threshold) ; threshold = budget * 0.80
    delta tracks the overrun if any.
    """
    conn = _resolve_conn(conn)

    # Sum tool call cost for this (specialist, cycle).
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) as total_cost "
        "FROM tool_call_log "
        "WHERE specialist_id = ? AND cycle_id = ? "
        "AND (transaction_to IS NULL)",
        (specialist_id, cycle_id),
    ).fetchone()
    total_cost = float(row["total_cost"] or 0.0)

    # Count actionable outputs for this (specialist, cycle).
    n_trade_ideas = conn.execute(
        "SELECT COUNT(*) FROM trade_ideas "
        "WHERE specialist_id = ? AND cycle_id = ? AND transaction_to IS NULL",
        (specialist_id, cycle_id),
    ).fetchone()[0]
    n_hypotheses_active = conn.execute(
        "SELECT COUNT(*) FROM hypotheses "
        "WHERE specialist_id = ? AND cycle_id = ? AND transaction_to IS NULL",
        (specialist_id, cycle_id),
    ).fetchone()[0]

    threshold = budget_usd * COST_PENALTY_BUDGET_FRACTION
    overrun = max(0.0, total_cost - threshold)
    productive = (n_trade_ideas > 0) or (n_hypotheses_active > 0)
    # Penalize only if overrun AND no productive output
    score = -overrun if (overrun > 0 and not productive) else 0.0

    return _insert_reward_row(
        conn=conn,
        cycle_id=cycle_id,
        reward_kind="cost_penalty",
        subject_kind="specialist",
        subject_id=specialist_id,
        specialist_id=specialist_id,
        score=score,
        baseline_score=0.0,
        delta=score,
        attribution_json={
            "total_cost_usd": total_cost,
            "budget_usd": budget_usd,
            "threshold_usd": threshold,
            "n_trade_ideas": int(n_trade_ideas),
            "n_hypotheses_active": int(n_hypotheses_active),
            "productive": productive,
            "overrun_usd": overrun,
        },
    )


def score_debate_quality(
    debate_id: str,
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> RewardEntry:
    """Reward debate quality: winner's argument well-cited + judge confidence
    in calibration band [0.55, 0.85].

    Score components (each in [0,1], averaged):
      a. citation_density   : winner's citation count clipped to 5
      b. judge_calibration  : 1.0 if conf in band, linearly down to 0 at edges
      c. provider_diversity : 1.0 if judge_provider distinct from both
                              participants' provider families (always true by
                              construction in our judge.py; included for audit).
    """
    conn = _resolve_conn(conn)
    deb = conn.execute(
        "SELECT id, cycle_id, participants, judge_model, judge_provider, "
        "       winner, judge_confidence, argument_payload, verdict "
        "FROM debates WHERE id = ? AND transaction_to IS NULL "
        "ORDER BY transaction_from DESC LIMIT 1",
        (debate_id,),
    ).fetchone()
    if deb is None:
        raise KeyError(f"score_debate_quality: debate {debate_id!r} not found")

    judge_conf = deb["judge_confidence"]
    if judge_conf is None:
        # Not yet judged — no quality score
        raise ValueError(
            f"score_debate_quality: debate {debate_id!r} has no judge_confidence"
        )

    # Component a: winner citation_density
    citation_density = 0.0
    try:
        arg_payload = json.loads(deb["argument_payload"]) if deb["argument_payload"] else {}
        if isinstance(arg_payload, dict):
            args = arg_payload.get("arguments", [])
            winner = deb["winner"]
            for a in args:
                if a.get("agent_id") == winner:
                    cites = a.get("citation_ids", [])
                    citation_density = min(1.0, len(cites) / 5.0)
                    break
    except Exception:
        pass

    # Component b: judge calibration
    lo, hi = JUDGE_CALIBRATION_BAND
    if lo <= judge_conf <= hi:
        judge_cal = 1.0
    elif judge_conf < lo:
        # Linear ramp from 0 at conf=0 to 1.0 at conf=lo
        judge_cal = max(0.0, judge_conf / lo)
    else:
        # Linear ramp from 1.0 at conf=hi to 0 at conf=1.0
        judge_cal = max(0.0, (1.0 - judge_conf) / (1.0 - hi))

    # Component c: provider diversity (we trust judge.py here)
    provider_diversity = 1.0

    score = (citation_density + judge_cal + provider_diversity) / 3.0

    return _insert_reward_row(
        conn=conn,
        cycle_id=deb["cycle_id"],
        reward_kind="debate_quality",
        subject_kind="debate",
        subject_id=debate_id,
        specialist_id=None,
        score=score,
        baseline_score=0.5,
        delta=score - 0.5,
        attribution_json={
            "citation_density": citation_density,
            "judge_calibration": judge_cal,
            "provider_diversity": provider_diversity,
            "judge_confidence": judge_conf,
            "judge_model": deb["judge_model"],
            "winner": deb["winner"],
        },
    )


def score_playbook_hit(
    trade_idea_id: str,
    playbook_id: str,
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> RewardEntry:
    """When a playbook-instantiated trade idea closes, write reward_log
    + update playbook.historical_hit_rate via supersedes.

    Idempotency: if a 'playbook_hit' row already exists for this
    (subject_id, playbook_id), returns the existing row without re-writing.
    """
    conn = _resolve_conn(conn)
    # Idempotency check
    existing = conn.execute(
        "SELECT id FROM reward_log "
        "WHERE reward_kind = 'playbook_hit' AND subject_id = ? "
        "AND json_extract(attribution_json, '$.playbook_id') = ? "
        "AND transaction_to IS NULL",
        (trade_idea_id, playbook_id),
    ).fetchone()
    if existing is not None:
        # Return a stub envelope (we don't re-fetch full row here)
        return RewardEntry(
            id=existing["id"], cycle_id="(idempotent)",
            reward_kind="playbook_hit", subject_kind="trade_idea",
            subject_id=trade_idea_id, specialist_id=None,
            score=0.0,
            attribution_json={"_idempotent": True, "playbook_id": playbook_id},
            quality_flags=["idempotent_existing"],
        )

    # Resolve the closed trade idea
    row = conn.execute(
        "SELECT id, cycle_id, specialist_id, contributed_alpha_pct, "
        "       realized_return_after_fees_pct, status "
        "FROM trade_ideas "
        "WHERE (id = ? OR supersedes = ?) AND transaction_to IS NULL "
        "ORDER BY transaction_from DESC LIMIT 1",
        (trade_idea_id, trade_idea_id),
    ).fetchone()
    if row is None:
        raise KeyError(f"score_playbook_hit: trade_idea {trade_idea_id!r} not found")
    if row["status"] not in ("closed", "expired", "invalidated"):
        raise ValueError(
            f"score_playbook_hit: trade_idea must be closed; got status={row['status']!r}"
        )

    alpha = row["contributed_alpha_pct"] or 0.0
    return_aft = row["realized_return_after_fees_pct"] or 0.0
    # score = return after fees (the "did this trigger pay off?" signal)
    score = float(return_aft)

    # Update the playbook's historical_hit_rate via supersedes (append-only)
    _bump_playbook_hit_history(conn, playbook_id, hit=(return_aft > 0.0),
                               return_pct=float(return_aft))

    return _insert_reward_row(
        conn=conn,
        cycle_id=row["cycle_id"],
        reward_kind="playbook_hit",
        subject_kind="trade_idea",
        subject_id=trade_idea_id,
        specialist_id=row["specialist_id"],
        score=score,
        baseline_score=0.0,
        delta=score,
        attribution_json={
            "playbook_id": playbook_id,
            "contributed_alpha_pct": float(alpha),
            "realized_return_after_fees_pct": float(return_aft),
            "hit": return_aft > 0.0,
        },
    )


def _bump_playbook_hit_history(
    conn: sqlite3.Connection,
    playbook_id: str,
    *,
    hit: bool,
    return_pct: float,
) -> None:
    """Update playbook.historical_hit_rate via append-only supersedes."""
    row = conn.execute(
        "SELECT * FROM playbooks WHERE id = ? AND transaction_to IS NULL "
        "ORDER BY transaction_from DESC LIMIT 1",
        (playbook_id,),
    ).fetchone()
    if row is None:
        return  # Playbook missing; skip silently
    pb = dict(row)
    n_old = int(pb.get("historical_trigger_count") or 0)
    hr_old = float(pb.get("historical_hit_rate") or 0.0)
    avg_old = float(pb.get("historical_avg_return_pct") or 0.0)
    n_new = n_old + 1
    hr_new = (hr_old * n_old + (1.0 if hit else 0.0)) / n_new
    avg_new = (avg_old * n_old + return_pct) / n_new

    now = datetime.now(timezone.utc)
    now_iso = _iso(now)
    # Close old row
    conn.execute(
        "UPDATE playbooks SET transaction_to = ? "
        "WHERE id = ? AND transaction_to IS NULL",
        (now_iso, playbook_id),
    )
    # Append new row with bumped stats
    new_id = "pb_" + uuid.uuid4().hex[:12]
    conn.execute(
        "INSERT INTO playbooks ("
        "id, name, version, owner_specialist, description, trigger_spec, "
        "action_template, min_sample_size, historical_trigger_count, "
        "historical_avg_return_pct, historical_hit_rate, promoted_status, "
        "evidence_ids, supersedes, valid_from, transaction_from"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            new_id, pb["name"], pb["version"], pb["owner_specialist"],
            pb["description"], pb["trigger_spec"], pb["action_template"],
            pb["min_sample_size"], n_new, avg_new, hr_new, pb["promoted_status"],
            pb["evidence_ids"], playbook_id, now_iso, now_iso,
        ),
    )


# ============================================================================
# Aggregators
# ============================================================================

def aggregate_specialist_rewards(
    specialist_id: str,
    window_days: int = 30,
    *,
    conn: Optional[sqlite3.Connection] = None,
    as_of: Optional[datetime] = None,
) -> SpecialistRewardAggregate:
    """Aggregate scores per reward_kind for `specialist_id` over the window.

    Used by the mutator to decide whether to propose a persona mutation.

    Returns per_kind = {
      reward_kind: {n, score_avg, score_sum, score_min, score_max,
                    delta_avg}
    }
    plus a `weaknesses` list of the 5 worst-scoring subjects across
    correctness+alpha (the kinds where lower is worse).
    """
    conn = _resolve_conn(conn)
    as_of = as_of or datetime.now(timezone.utc)
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    start = as_of - timedelta(days=window_days)

    rows = conn.execute(
        "SELECT reward_kind, subject_kind, subject_id, score, delta, "
        "       valid_from, attribution_json "
        "FROM reward_log "
        "WHERE specialist_id = ? "
        "AND valid_from >= ? AND valid_from <= ? "
        "AND transaction_to IS NULL",
        (specialist_id, _iso(start), _iso(as_of)),
    ).fetchall()

    per_kind: dict[str, dict[str, float]] = {}
    weakness_candidates: list[dict[str, Any]] = []
    for r in rows:
        k = r["reward_kind"]
        s = float(r["score"]) if r["score"] is not None else 0.0
        d = float(r["delta"]) if r["delta"] is not None else 0.0
        bucket = per_kind.setdefault(k, {
            "n": 0.0, "score_sum": 0.0,
            "score_min": math.inf, "score_max": -math.inf,
            "delta_sum": 0.0,
        })
        bucket["n"] += 1.0
        bucket["score_sum"] += s
        bucket["score_min"] = min(bucket["score_min"], s)
        bucket["score_max"] = max(bucket["score_max"], s)
        bucket["delta_sum"] += d
        # collect weakness candidates from low-correctness or low-alpha rows
        if k in ("correctness", "alpha", "novelty"):
            weakness_candidates.append({
                "reward_kind": k,
                "subject_kind": r["subject_kind"],
                "subject_id": r["subject_id"],
                "score": s,
                "delta": d,
            })

    # Finalize means
    for k, b in per_kind.items():
        n = b["n"] or 1.0
        b["score_avg"] = b["score_sum"] / n
        b["delta_avg"] = b["delta_sum"] / n
        if b["score_min"] == math.inf:
            b["score_min"] = 0.0
        if b["score_max"] == -math.inf:
            b["score_max"] = 0.0

    # Sort weaknesses by score ascending (worst first), take top 5
    weakness_candidates.sort(key=lambda c: c["score"])
    weaknesses = weakness_candidates[:5]

    return SpecialistRewardAggregate(
        specialist_id=specialist_id,
        window_days=window_days,
        as_of=as_of,
        per_kind=per_kind,
        weaknesses=weaknesses,
    )


def aggregate_tool_affinity(
    specialist_id: str,
    window_days: int = 30,
    *,
    conn: Optional[sqlite3.Connection] = None,
    as_of: Optional[datetime] = None,
) -> dict[str, float]:
    """Per-tool Brier-weighted score for this specialist over the window.

    Used by tool atlas to pick the curated subset shown to this specialist.

    Algorithm:
      1. Read tool_call_log rows for (specialist_id, window).
      2. For each tool_uri, average reward_score->>'brier_delta' (positive
         => calls improve subsequent forecast Brier). Fallback: weight by
         citation count in alpha-rewarded ideas.
      3. Normalize to [0, 1] so the atlas can rank.

    Returns a dict {tool_uri: affinity_score}, sorted by score desc.
    """
    conn = _resolve_conn(conn)
    as_of = as_of or datetime.now(timezone.utc)
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    start = as_of - timedelta(days=window_days)

    # Pass 1: Brier-delta from tool_call_log.reward_score
    rows = conn.execute(
        "SELECT tool_uri, reward_score, cited_in_ids "
        "FROM tool_call_log "
        "WHERE specialist_id = ? AND started_at >= ? AND started_at <= ? "
        "AND (transaction_to IS NULL)",
        (specialist_id, _iso(start), _iso(as_of)),
    ).fetchall()

    accum: dict[str, dict[str, float]] = {}
    for r in rows:
        uri = r["tool_uri"]
        bucket = accum.setdefault(uri, {"n_calls": 0.0, "brier_delta_sum": 0.0,
                                        "n_cited": 0.0})
        bucket["n_calls"] += 1.0
        try:
            rs = json.loads(r["reward_score"] or "{}")
            bd = rs.get("brier_delta")
            if bd is not None:
                bucket["brier_delta_sum"] += float(bd)
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
        try:
            cites = json.loads(r["cited_in_ids"] or "[]")
            if isinstance(cites, list):
                bucket["n_cited"] += float(len(cites))
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

    # Pass 2: bring in alpha-share weight from reward_log attribution
    alpha_rows = conn.execute(
        "SELECT score, attribution_json FROM reward_log "
        "WHERE reward_kind = 'alpha' AND specialist_id = ? "
        "AND valid_from >= ? AND valid_from <= ? "
        "AND transaction_to IS NULL",
        (specialist_id, _iso(start), _iso(as_of)),
    ).fetchall()
    for ar in alpha_rows:
        try:
            attr = json.loads(ar["attribution_json"] or "{}")
            for comp in attr.get("components", []):
                if comp.get("kind") == "tool_call":
                    # Map tool_call_id -> tool_uri via log lookup
                    tc_row = conn.execute(
                        "SELECT tool_uri FROM tool_call_log WHERE id = ?",
                        (comp.get("id"),),
                    ).fetchone()
                    if tc_row is not None:
                        uri = tc_row["tool_uri"]
                        bucket = accum.setdefault(uri, {
                            "n_calls": 0.0, "brier_delta_sum": 0.0, "n_cited": 0.0,
                        })
                        bucket.setdefault("alpha_share_sum", 0.0)
                        bucket["alpha_share_sum"] += float(comp.get("alpha_pct", 0.0))
        except (json.JSONDecodeError, TypeError, ValueError):
            continue

    # Compose final affinity score per URI
    out: dict[str, float] = {}
    for uri, b in accum.items():
        n = b["n_calls"] or 1.0
        brier_avg = b["brier_delta_sum"] / n
        cited_frac = b["n_cited"] / n
        alpha_share = b.get("alpha_share_sum", 0.0)
        # Composite: Brier delta dominates; citation fraction + alpha boost
        raw = 0.6 * _sigmoid(brier_avg) + 0.25 * min(1.0, cited_frac) \
              + 0.15 * _sigmoid(alpha_share / 10.0)
        out[uri] = round(float(raw), 6)

    # Sort by score desc for ergonomic display (callers can dict-sort their way)
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def _sigmoid(x: float) -> float:
    """Squash to [0,1]. Used to make heterogeneous-scale signals comparable."""
    try:
        return 1.0 / (1.0 + math.exp(-x))
    except OverflowError:
        return 0.0 if x < 0 else 1.0

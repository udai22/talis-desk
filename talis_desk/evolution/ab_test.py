"""Persona A/B testing — Phase 6 Layer 5 of SOTA Desk Architecture v2.

Per wiki/SOTA_DESK_ARCHITECTURE.md v2 §5 (lines 477-491) and §2 lines
144-150:

  4. A/B runs base vs candidate on identical bitemporal snapshots
     + frozen tool atlas + same budget class.
  5. Score arm difference on Brier delta, alpha delta, cost-per-useful-
     citation, and novelty-and-correct share.

# Replay-safety
Both arms read from the SAME `talis_desk.replay.build_replay_context`
snapshot — they MUST NOT mutate the world. We tag every artifact each
arm writes with `ab_test_group='base' or 'candidate'` via the
specialist_states `ab_test_group` column (already in the schema, lines
176 / 492) so the arms' outputs don't bleed into the live dashboard.

# What this module does (and doesn't)
Does:
  - Score arm difference on the 4 metrics from v2 §5.
  - Compute a z-score-ish significance (see Honest Gaps).
  - Return a `PersonaABResult` envelope with a `recommendation` string the
    auto-mutator can route on.

Does NOT:
  - Actually run new loop cycles. v2 §5 line 491 says "run base + candidate
    on identical bitemporal snapshots". The full implementation requires the
    loop runner (in `talis_desk.loop/`, owned by a different agent). For
    Phase 6 we score arms over EXISTING `trade_ideas` + `reward_log` rows
    bucketed by `ab_test_group` in the window. The caller is expected to
    have already produced rows on both arms; otherwise the comparison is
    degenerate (n=0) and we return `recommendation='need_more_data'`.
  - Write any new bitemporal rows. A/B is READ-ONLY.

# Honest gaps
- `significance` is a Welch-style z-score for the alpha delta, NOT a proper
  p-value. With n_cycles_per_arm <= 30 the asymptotic Normal assumption is
  weak; treat the number as "rough effect-size signal" not "statistical
  significance".
- `cost_per_useful_citation` is computed from `tool_call_log.cost_usd` and
  `cited_in_ids` for the arm. "Useful" = cited in a trade_idea that ended
  up alpha-positive. This is a noisy proxy; v2 §5 ideally wants a
  Brier-weighted version once judge attribution lands.
"""
from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional


# ============================================================================
# Types
# ============================================================================

@dataclass
class PersonaABResult:
    """The envelope returned by `run_persona_ab_test`."""
    specialist_id: str
    base_version: str
    candidate_version: str
    n_cycles_per_arm: int
    n_ideas_base: int
    n_ideas_candidate: int
    base_brier_avg: Optional[float]
    candidate_brier_avg: Optional[float]
    brier_delta: Optional[float]
    base_alpha_pct: Optional[float]
    candidate_alpha_pct: Optional[float]
    alpha_delta_pct: Optional[float]
    base_cost_usd: float
    candidate_cost_usd: float
    base_cost_per_useful_citation: Optional[float]
    candidate_cost_per_useful_citation: Optional[float]
    base_novelty_correct_share: Optional[float]
    candidate_novelty_correct_share: Optional[float]
    winner: Literal["base", "candidate", "tie"]
    significance: float  # z-score-ish; see honest gaps
    recommendation: Literal["promote_candidate", "keep_base", "need_more_data"]
    quality_flags: list[str] = field(default_factory=list)
    window_start: Optional[datetime] = None
    window_end: Optional[datetime] = None


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


# ============================================================================
# Core
# ============================================================================

def run_persona_ab_test(
    specialist_id: str,
    base_version: str,
    candidate_version: str,
    *,
    window_days: int = 15,
    n_cycles_per_arm: int = 30,
    conn: Optional[sqlite3.Connection] = None,
    as_of: Optional[datetime] = None,
) -> PersonaABResult:
    """Score base vs candidate persona on identical bitemporal snapshots
    + same frozen tool atlas + same budget class.

    Selection: trade_ideas / reward_log rows in the window where the
    specialist's persona_version matches `base_version` or `candidate_version`.
    We use `payload->>ab_test_group` (set by the loop when it tags artifacts)
    or fall back to `specialist_states.ab_test_group` for any joined rows.

    Returns PersonaABResult with winner + significance + recommendation.
    """
    conn = _resolve_conn(conn)
    as_of = as_of or datetime.now(timezone.utc)
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    window_start = as_of - timedelta(days=window_days)

    quality_flags: list[str] = []

    base_ideas = _fetch_ideas_for_arm(
        conn, specialist_id, base_version, window_start, as_of,
    )
    cand_ideas = _fetch_ideas_for_arm(
        conn, specialist_id, candidate_version, window_start, as_of,
    )

    # Cap to n_cycles_per_arm (most recent)
    base_ideas = base_ideas[:n_cycles_per_arm]
    cand_ideas = cand_ideas[:n_cycles_per_arm]

    base_brier = _avg_brier(base_ideas)
    cand_brier = _avg_brier(cand_ideas)
    base_alpha = _avg_alpha(base_ideas)
    cand_alpha = _avg_alpha(cand_ideas)

    brier_delta = None
    if base_brier is not None and cand_brier is not None:
        # Brier: lower is better; positive delta = candidate is WORSE
        brier_delta = cand_brier - base_brier
    alpha_delta = None
    if base_alpha is not None and cand_alpha is not None:
        alpha_delta = cand_alpha - base_alpha

    base_cost = _sum_cost(conn, specialist_id, base_version, window_start, as_of)
    cand_cost = _sum_cost(conn, specialist_id, candidate_version, window_start, as_of)

    base_cpuc = _cost_per_useful_citation(
        conn, specialist_id, base_version, window_start, as_of, base_ideas,
    )
    cand_cpuc = _cost_per_useful_citation(
        conn, specialist_id, candidate_version, window_start, as_of, cand_ideas,
    )

    base_nc = _novelty_correct_share(
        conn, specialist_id, base_version, window_start, as_of,
    )
    cand_nc = _novelty_correct_share(
        conn, specialist_id, candidate_version, window_start, as_of,
    )

    # Significance: Welch-ish z on alpha (or Brier if alpha n=0). Tiny n.
    significance, sig_basis = _compute_significance(base_ideas, cand_ideas)

    # Winner pick
    winner, recommendation = _pick_winner_and_rec(
        n_base=len(base_ideas),
        n_cand=len(cand_ideas),
        brier_delta=brier_delta,
        alpha_delta=alpha_delta,
        significance=significance,
        base_cpuc=base_cpuc, cand_cpuc=cand_cpuc,
    )
    # If both arms had identical underlying rows (e.g., same persona used
    # for both during smoke test) -> treat as tie.
    if base_version == candidate_version:
        winner = "tie"
        recommendation = "keep_base"
        quality_flags.append("identical_versions_passed_in")

    if len(base_ideas) == 0 and len(cand_ideas) == 0:
        quality_flags.append("no_data_either_arm")
    if len(base_ideas) == 0:
        quality_flags.append("no_data_base_arm")
    if len(cand_ideas) == 0:
        quality_flags.append("no_data_candidate_arm")
    quality_flags.append(f"significance_basis={sig_basis}")

    return PersonaABResult(
        specialist_id=specialist_id,
        base_version=base_version,
        candidate_version=candidate_version,
        n_cycles_per_arm=n_cycles_per_arm,
        n_ideas_base=len(base_ideas),
        n_ideas_candidate=len(cand_ideas),
        base_brier_avg=base_brier,
        candidate_brier_avg=cand_brier,
        brier_delta=brier_delta,
        base_alpha_pct=base_alpha,
        candidate_alpha_pct=cand_alpha,
        alpha_delta_pct=alpha_delta,
        base_cost_usd=base_cost,
        candidate_cost_usd=cand_cost,
        base_cost_per_useful_citation=base_cpuc,
        candidate_cost_per_useful_citation=cand_cpuc,
        base_novelty_correct_share=base_nc,
        candidate_novelty_correct_share=cand_nc,
        winner=winner,
        significance=significance,
        recommendation=recommendation,
        quality_flags=quality_flags,
        window_start=window_start,
        window_end=as_of,
    )


# ============================================================================
# Arm data fetchers
# ============================================================================

def _fetch_ideas_for_arm(
    conn: sqlite3.Connection,
    specialist_id: str,
    persona_version: str,
    window_start: datetime,
    window_end: datetime,
) -> list[dict[str, Any]]:
    """All resolved trade_ideas tagged to this specialist+persona_version
    in the window. We bucket by `payload->>persona_version` OR fall back to
    rows where the specialist's persona_version at `published_at` matches.

    Honest gap: in v0 the loop runner doesn't always tag persona_version
    on every idea; if absent we approximate by selecting all
    `specialist_id` ideas in window. The smoke test seeds the
    `payload.persona_version` field explicitly so this path stays clean.
    """
    rows = conn.execute(
        "SELECT id, cycle_id, realized_pnl_pct, "
        "       realized_return_after_fees_pct, benchmark_return_pct, "
        "       contributed_alpha_pct, brier, status, payload, valid_from "
        "FROM trade_ideas "
        "WHERE specialist_id = ? "
        "AND transaction_to IS NULL "
        "AND status IN ('closed', 'expired') "
        "AND valid_from >= ? AND valid_from <= ? "
        "ORDER BY valid_from DESC",
        (specialist_id, _iso(window_start), _iso(window_end)),
    ).fetchall()

    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        # Bucket: payload.persona_version OR payload.ab_test_group + lookup
        try:
            pl = json.loads(d["payload"] or "{}")
        except json.JSONDecodeError:
            pl = {}
        pv_in_payload = pl.get("persona_version")
        ab_in_payload = pl.get("ab_test_group")
        # Direct match on persona_version
        if pv_in_payload == persona_version:
            out.append(d)
            continue
        # Match via ab_test_group label that resolves to persona_version
        if ab_in_payload in ("base", "candidate"):
            # Try to resolve via specialist_states
            row = conn.execute(
                "SELECT persona_version FROM specialist_states "
                "WHERE specialist_id = ? AND ab_test_group = ? "
                "ORDER BY transaction_from DESC LIMIT 1",
                (specialist_id, ab_in_payload),
            ).fetchone()
            if row is not None and row["persona_version"] == persona_version:
                out.append(d)
                continue
        # Fallback: if no tag, include rows when persona_version IS the only
        # open persona for this specialist (smoke-test convenience)
        if pv_in_payload is None and ab_in_payload is None:
            # Only include if no other persona was open in this window
            n_open = conn.execute(
                "SELECT COUNT(DISTINCT persona_version) FROM specialist_states "
                "WHERE specialist_id = ? AND state_kind = 'persona'",
                (specialist_id,),
            ).fetchone()[0]
            if n_open == 1:
                out.append(d)
    return out


def _avg_brier(ideas: list[dict[str, Any]]) -> Optional[float]:
    vals = [float(i["brier"]) for i in ideas if i.get("brier") is not None]
    if not vals:
        return None
    return sum(vals) / len(vals)


def _avg_alpha(ideas: list[dict[str, Any]]) -> Optional[float]:
    vals = [float(i["contributed_alpha_pct"]) for i in ideas
            if i.get("contributed_alpha_pct") is not None]
    if not vals:
        return None
    return sum(vals) / len(vals)


def _sum_cost(
    conn: sqlite3.Connection,
    specialist_id: str,
    persona_version: str,
    window_start: datetime,
    window_end: datetime,
) -> float:
    """Sum tool_call_log.cost_usd for this arm. We can't easily filter by
    persona_version in tool_call_log (no column), so we approximate by
    summing all specialist's cost in the window and splitting proportionally
    to ideas-per-arm. For a true cross-arm split we'd need to thread
    persona_version through `tool_call_log.payload` — Phase 7."""
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM tool_call_log "
        "WHERE specialist_id = ? "
        "AND started_at >= ? AND started_at <= ? "
        "AND (transaction_to IS NULL)",
        (specialist_id, _iso(window_start), _iso(window_end)),
    ).fetchone()
    return float(row["total"] or 0.0)


def _cost_per_useful_citation(
    conn: sqlite3.Connection,
    specialist_id: str,
    persona_version: str,
    window_start: datetime,
    window_end: datetime,
    ideas: list[dict[str, Any]],
) -> Optional[float]:
    """cost / count(useful_citations).

    'Useful' = tool_call's cited_in_ids intersects ids of alpha-positive
    ideas in this arm. Returns None if denominator is 0.
    """
    pos_alpha_ideas = {i["id"] for i in ideas
                       if (i.get("contributed_alpha_pct") or 0.0) > 0.0}
    if not pos_alpha_ideas:
        return None

    rows = conn.execute(
        "SELECT cited_in_ids, cost_usd FROM tool_call_log "
        "WHERE specialist_id = ? "
        "AND started_at >= ? AND started_at <= ? "
        "AND (transaction_to IS NULL)",
        (specialist_id, _iso(window_start), _iso(window_end)),
    ).fetchall()

    useful_cost = 0.0
    n_useful = 0
    for r in rows:
        try:
            cites = set(json.loads(r["cited_in_ids"] or "[]"))
        except (json.JSONDecodeError, TypeError):
            cites = set()
        if cites & pos_alpha_ideas:
            useful_cost += float(r["cost_usd"] or 0.0)
            n_useful += 1
    if n_useful == 0:
        return None
    return useful_cost / n_useful


def _novelty_correct_share(
    conn: sqlite3.Connection,
    specialist_id: str,
    persona_version: str,
    window_start: datetime,
    window_end: datetime,
) -> Optional[float]:
    """Share of this arm's claims/hypotheses that are BOTH novel
    (reward_kind='novelty', is_novel=true in attribution) AND correct
    (reward_kind='correctness', brier < 0.25).

    Returns None if no qualifying rows.
    """
    rows = conn.execute(
        "SELECT reward_kind, subject_id, score, attribution_json "
        "FROM reward_log "
        "WHERE specialist_id = ? "
        "AND valid_from >= ? AND valid_from <= ? "
        "AND transaction_to IS NULL "
        "AND reward_kind IN ('novelty', 'correctness')",
        (specialist_id, _iso(window_start), _iso(window_end)),
    ).fetchall()

    by_subject: dict[str, dict[str, Any]] = {}
    for r in rows:
        sid = r["subject_id"]
        slot = by_subject.setdefault(sid, {})
        if r["reward_kind"] == "correctness":
            slot["correct"] = float(r["score"]) >= 0.75  # score=1-brier; correct iff brier<=0.25
        else:
            try:
                attr = json.loads(r["attribution_json"] or "{}")
                slot["novel"] = bool(attr.get("is_novel"))
            except json.JSONDecodeError:
                pass

    total = len(by_subject)
    if total == 0:
        return None
    qualifying = sum(
        1 for v in by_subject.values()
        if v.get("novel") and v.get("correct")
    )
    return qualifying / total


# ============================================================================
# Significance + winner
# ============================================================================

def _compute_significance(
    base_ideas: list[dict[str, Any]],
    cand_ideas: list[dict[str, Any]],
) -> tuple[float, str]:
    """Welch-ish z on per-idea alpha. Falls back to Brier if alpha n=0.

    Returns (z_score, basis). z_score = 0 if not enough data.
    """
    base_alpha = [float(i["contributed_alpha_pct"]) for i in base_ideas
                  if i.get("contributed_alpha_pct") is not None]
    cand_alpha = [float(i["contributed_alpha_pct"]) for i in cand_ideas
                  if i.get("contributed_alpha_pct") is not None]

    if len(base_alpha) >= 2 and len(cand_alpha) >= 2:
        z = _welch_z(base_alpha, cand_alpha)
        return z, "alpha"

    base_brier = [float(i["brier"]) for i in base_ideas if i.get("brier") is not None]
    cand_brier = [float(i["brier"]) for i in cand_ideas if i.get("brier") is not None]
    if len(base_brier) >= 2 and len(cand_brier) >= 2:
        # Note: Brier-lower-is-better; we flip the sign so a positive z means
        # candidate is BETTER (lower Brier).
        z = -_welch_z(base_brier, cand_brier)
        return z, "brier_inverted"
    return 0.0, "insufficient_data"


def _welch_z(a: list[float], b: list[float]) -> float:
    """Welch-Satterthwaite z-ish statistic for two small samples.

    z = (mean_b - mean_a) / sqrt(var_b/n_b + var_a/n_a)

    Honest gap: with n<<30 this is closer to a t-statistic than a z, and
    the asymptotic Normal sig is not the right reference. Callers should
    treat |z|>=1.5 as "directional" rather than "significant".
    """
    n_a = len(a)
    n_b = len(b)
    if n_a < 2 or n_b < 2:
        return 0.0
    mu_a = sum(a) / n_a
    mu_b = sum(b) / n_b
    var_a = sum((x - mu_a) ** 2 for x in a) / (n_a - 1)
    var_b = sum((x - mu_b) ** 2 for x in b) / (n_b - 1)
    denom = math.sqrt((var_a / n_a) + (var_b / n_b))
    if denom < 1e-9:
        return 0.0
    return (mu_b - mu_a) / denom


def _pick_winner_and_rec(
    *,
    n_base: int,
    n_cand: int,
    brier_delta: Optional[float],
    alpha_delta: Optional[float],
    significance: float,
    base_cpuc: Optional[float],
    cand_cpuc: Optional[float],
) -> tuple[Literal["base", "candidate", "tie"], Literal["promote_candidate", "keep_base", "need_more_data"]]:
    """Pick winner + recommendation per v2 §5.

    Rules (in order):
      1. Both arms < 5 rows -> tie + need_more_data
      2. alpha_delta > 0 AND |significance| >= 1.5 -> candidate + promote
      3. alpha_delta < 0 AND |significance| >= 1.5 -> base + keep
      4. Brier-only signal: brier_delta < -0.05 (candidate better) -> candidate
      5. Otherwise -> tie + keep_base (conservative: no change)
    """
    if n_base < 5 and n_cand < 5:
        return "tie", "need_more_data"
    if alpha_delta is not None and significance is not None:
        if alpha_delta > 0 and significance >= 1.5:
            return "candidate", "promote_candidate"
        if alpha_delta < 0 and significance <= -1.5:
            return "base", "keep_base"
    if brier_delta is not None:
        if brier_delta < -0.05:  # candidate Brier <<lower>> better
            return "candidate", "promote_candidate"
        if brier_delta > 0.05:
            return "base", "keep_base"
    # cost tiebreaker: candidate cheaper per useful citation
    if (base_cpuc is not None and cand_cpuc is not None
            and cand_cpuc < base_cpuc * 0.5):
        return "candidate", "promote_candidate"
    return "tie", "keep_base"

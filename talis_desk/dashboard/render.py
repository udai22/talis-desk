"""HTML renderer + data gatherers for the manual eval dashboard.

Per `wiki/SOTA_DESK_ARCHITECTURE.md` v2 §5 (lines 477-491). The renderer is
intentionally a single-file static SPA-ish HTML — every panel is rendered
server-side as one `<section>` and JS just toggles which tab is visible.

Pattern matches `tic_dashboard.py` + `build_data_layer_site.py` in the
talis-tic sibling repo: no template engine, no React, just f-strings and
Pico CSS dark theme so it looks like
`https://udai22.github.io/talis-data-layer/`.

Public entry points:
  - `render_dashboard_html(as_of) -> str`   (full page)
  - `gather_scorecard(as_of) -> dict`       (9 S-tier metrics)
  - `gather_trade_book(as_of) -> dict`      (open + closed PnL)
  - `gather_debates_panel(as_of) -> dict`   (unresolved / judged)
  - `gather_hypothesis_graph_sample() -> dict`
  - `gather_persona_diffs(as_of) -> dict`
  - `gather_weakness_map(as_of) -> dict`
  - `gather_tool_atlas(as_of) -> dict`

# Honest gaps
- Server-rendered. v2 §5 line 491 suggests <30min reviewer flow; this MVP
  hits that with tab-based panels but a React SPA could be tighter.
- Some metrics are derived from on-disk state in real time (no warmup);
  on large prod databases the renderer will want SQL views + cache.
- The hypothesis graph sampler returns a flat node+edge list as JSON; the
  static HTML renders a basic D3-less table. A future panel can add a
  force-directed graph.
"""
from __future__ import annotations

import json
import math
import random
import sqlite3
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any, Optional

from ..store import get_desk_store


# ============================================================================
# Constants — v2 §1 scorecard thresholds (lines 15-25)
# ============================================================================

# Each entry: (metric_id, label, threshold, direction). `direction="up"`
# means higher is better; `"down"` means lower is better.
SCORECARD_THRESHOLDS = [
    ("weekly_alpha_bps", "Weekly alpha vs HL", 50.0, "up", "bps after fees"),
    ("hit_rate", "Idea hit rate", 0.55, "up", "fraction"),
    ("sharpe", "Trade book Sharpe", 1.5, "up", "ratio"),
    ("novel_correct_share", "Novel-and-correct claim share", 0.30, "up", "fraction"),
    ("coverage_breadth", "Coverage breadth", 0.80, "up", "tools touched / approved"),
    ("avg_investigation_depth", "Investigation depth", 30.0, "up", "avg calls"),
    ("reflection_lift_share", "Reflection lift > 0 share", 0.70, "up", "fraction"),
    ("playbook_hit_rate", "Playbook hit rate", 0.50, "up", "fraction"),
    ("debate_resolution_rate", "Debate resolution rate", 0.90, "up", "fraction"),
]


# ============================================================================
# Helpers
# ============================================================================

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _from_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _conn() -> sqlite3.Connection:
    return get_desk_store().conn


def _safe_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except Exception:
        return None


def _classify_metric(value: Optional[float], threshold: float, direction: str) -> str:
    """Return 'green' | 'yellow' | 'red' | 'unknown'."""
    if value is None or not math.isfinite(value):
        return "unknown"
    if direction == "up":
        if value >= threshold:
            return "green"
        if value >= threshold * 0.7:
            return "yellow"
        return "red"
    else:
        if value <= threshold:
            return "green"
        if value <= threshold * 1.3:
            return "yellow"
        return "red"


# ============================================================================
# 1. Scorecard
# ============================================================================

def gather_scorecard(as_of: Optional[datetime] = None) -> dict[str, Any]:
    """Return the 9 S-tier metrics per v2 §1.

    NOTE: we do best-effort estimation from on-disk artifacts. Where a
    metric needs a source that hasn't been wired yet (e.g.
    `tool_atlas.coverage` requires the frozen daily atlas), we mark the
    metric `unknown` and record the gap in `why_red`. This is honest:
    a green scorecard with stub data would be the worst-case failure.
    """
    as_of = as_of or _utc_now()
    conn = _conn()
    window_start_iso = _iso(as_of - timedelta(days=30))
    metrics: list[dict[str, Any]] = []

    # 1) weekly_alpha_bps — sum of contributed_alpha_pct on closed trades in
    #    last 7 days, scaled to bps and divided by 1 week.
    row = conn.execute(
        "SELECT avg(contributed_alpha_pct) AS aa, sum(contributed_alpha_pct) AS sa, "
        "count(*) AS n FROM trade_ideas "
        "WHERE status = 'closed' AND transaction_to IS NULL "
        "AND published_at >= ?",
        (_iso(as_of - timedelta(days=7)),),
    ).fetchone()
    weekly_alpha_bps = None
    why_red_alpha = []
    if row is not None and dict(row).get("aa") is not None:
        # contributed_alpha_pct stored as percent; * 100 -> bps
        weekly_alpha_bps = float(row["aa"]) * 100.0
        if (row["n"] or 0) < 5:
            why_red_alpha.append(f"n_closed_7d={row['n']} (low sample)")
    else:
        why_red_alpha.append("no closed ideas in last 7d")
    metrics.append(_metric_row(
        "weekly_alpha_bps", "Weekly alpha vs HL",
        weekly_alpha_bps, 50.0, "up", "bps after fees", why_red_alpha,
    ))

    # 2) hit_rate
    row = conn.execute(
        "SELECT count(*) AS n_total, "
        "sum(CASE WHEN realized_return_after_fees_pct > 0 THEN 1 ELSE 0 END) AS n_hit "
        "FROM trade_ideas "
        "WHERE status = 'closed' AND transaction_to IS NULL "
        "AND published_at >= ?",
        (window_start_iso,),
    ).fetchone()
    n_total = int((dict(row).get("n_total") or 0) if row else 0)
    n_hit = int((dict(row).get("n_hit") or 0) if row else 0)
    hit_rate = (n_hit / n_total) if n_total else None
    metrics.append(_metric_row(
        "hit_rate", "Idea hit rate", hit_rate, 0.55, "up", "fraction",
        ([f"n_closed_30d={n_total} (target >= 20)"] if n_total < 20 else []),
    ))

    # 3) sharpe — daily-return sharpe of closed ideas over last 30d
    rows = conn.execute(
        "SELECT realized_return_after_fees_pct FROM trade_ideas "
        "WHERE status = 'closed' AND transaction_to IS NULL "
        "AND published_at >= ? AND realized_return_after_fees_pct IS NOT NULL",
        (window_start_iso,),
    ).fetchall()
    rets = [float(r[0]) for r in rows if r[0] is not None]
    sharpe = _sharpe_estimate(rets) if rets else None
    metrics.append(_metric_row(
        "sharpe", "Trade book Sharpe", sharpe, 1.5, "up", "ratio",
        ([f"n_returns={len(rets)} (target >= 20)"] if len(rets) < 20 else []),
    ))

    # 4) novel_correct_share — fraction of novel claims that resolved
    #    'supported'. We approximate from `reward_log` rows with
    #    `reward_kind='novelty'` joined to hypothesis status='supported'.
    rows = conn.execute(
        "SELECT subject_id, score FROM reward_log "
        "WHERE reward_kind = 'novelty' AND transaction_to IS NULL "
        "AND valid_from >= ?",
        (window_start_iso,),
    ).fetchall()
    novel_ids = [r["subject_id"] for r in rows if r["subject_id"]]
    novel_correct = 0
    novel_total = len(novel_ids)
    if novel_ids:
        placeholders = ",".join(["?"] * len(novel_ids))
        cnt = conn.execute(
            f"SELECT count(*) FROM hypotheses WHERE id IN ({placeholders}) "
            f"AND status = 'supported' AND transaction_to IS NULL",
            novel_ids,
        ).fetchone()[0]
        novel_correct = int(cnt or 0)
    novel_share = (novel_correct / novel_total) if novel_total else None
    metrics.append(_metric_row(
        "novel_correct_share", "Novel-and-correct claim share",
        novel_share, 0.30, "up", "fraction",
        ([f"novel_n={novel_total} (low sample)"] if novel_total < 5 else []),
    ))

    # 5) coverage_breadth
    approved = conn.execute(
        "SELECT count(DISTINCT tool_uri) FROM tool_atlas "
        "WHERE status IN ('active','approved') AND transaction_to IS NULL"
    ).fetchone()[0]
    active_calls = conn.execute(
        "SELECT count(DISTINCT tool_uri) FROM tool_call_log "
        "WHERE started_at >= ?",
        (_iso(as_of - timedelta(days=7)),),
    ).fetchone()[0]
    if approved and approved > 0:
        coverage = float(active_calls) / float(approved)
    else:
        coverage = None
    metrics.append(_metric_row(
        "coverage_breadth", "Coverage breadth", coverage, 0.80, "up",
        "tools touched / approved",
        ([f"approved={approved} active_7d={active_calls}"] if coverage is not None and coverage < 0.80 else []),
    ))

    # 6) avg_investigation_depth — avg calls per investigation_id over 30d
    row = conn.execute(
        "SELECT investigation_id, count(*) AS c FROM tool_call_log "
        "WHERE investigation_id IS NOT NULL AND started_at >= ? "
        "GROUP BY investigation_id",
        (window_start_iso,),
    ).fetchall()
    depths = [int(r["c"]) for r in row]
    avg_depth = (sum(depths) / len(depths)) if depths else None
    metrics.append(_metric_row(
        "avg_investigation_depth", "Investigation depth",
        avg_depth, 30.0, "up", "avg calls",
        ([f"n_investigations={len(depths)}"] if len(depths) < 3 else []),
    ))

    # 7) reflection_lift_share — fraction of reward_log rows with delta > 0
    row = conn.execute(
        "SELECT count(*) AS n_total, "
        "sum(CASE WHEN delta > 0 THEN 1 ELSE 0 END) AS n_up "
        "FROM reward_log WHERE reward_kind = 'correctness' "
        "AND transaction_to IS NULL AND valid_from >= ?",
        (window_start_iso,),
    ).fetchone()
    n_total_refl = int((dict(row).get("n_total") or 0) if row else 0)
    n_up_refl = int((dict(row).get("n_up") or 0) if row else 0)
    refl_share = (n_up_refl / n_total_refl) if n_total_refl else None
    metrics.append(_metric_row(
        "reflection_lift_share", "Reflection lift > 0 share",
        refl_share, 0.70, "up", "fraction",
        ([f"n_correctness={n_total_refl}"] if n_total_refl < 10 else []),
    ))

    # 8) playbook_hit_rate
    row = conn.execute(
        "SELECT avg(historical_hit_rate) AS avg_hr, count(*) AS n FROM playbooks "
        "WHERE promoted_status IN ('approved','experimental') AND transaction_to IS NULL"
    ).fetchone()
    pb_hr = _safe_float(dict(row).get("avg_hr")) if row else None
    pb_n = int((dict(row).get("n") or 0) if row else 0)
    metrics.append(_metric_row(
        "playbook_hit_rate", "Playbook hit rate",
        pb_hr, 0.50, "up", "fraction",
        ([f"n_active_playbooks={pb_n}"] if pb_n < 2 else []),
    ))

    # 9) debate_resolution_rate
    row = conn.execute(
        "SELECT count(*) AS n_total, "
        "sum(CASE WHEN status IN ('judged','applied') THEN 1 ELSE 0 END) AS n_done "
        "FROM debates WHERE transaction_to IS NULL AND valid_from >= ?",
        (window_start_iso,),
    ).fetchone()
    n_deb = int((dict(row).get("n_total") or 0) if row else 0)
    n_deb_done = int((dict(row).get("n_done") or 0) if row else 0)
    deb_res = (n_deb_done / n_deb) if n_deb else None
    metrics.append(_metric_row(
        "debate_resolution_rate", "Debate resolution rate",
        deb_res, 0.90, "up", "fraction",
        ([f"n_debates_30d={n_deb}"] if n_deb < 5 else []),
    ))

    n_green = sum(1 for m in metrics if m["status"] == "green")
    n_red = sum(1 for m in metrics if m["status"] == "red")
    s_tier_candidate = (n_green >= 7) and (n_red == 0)

    return {
        "as_of": _iso(as_of),
        "metrics": metrics,
        "n_green": n_green,
        "n_red": n_red,
        "s_tier_candidate": s_tier_candidate,
    }


def _metric_row(
    metric_id: str,
    label: str,
    value: Optional[float],
    threshold: float,
    direction: str,
    unit: str,
    why_red: list[str],
) -> dict[str, Any]:
    status = _classify_metric(value, threshold, direction)
    return {
        "id": metric_id,
        "label": label,
        "value": value,
        "threshold": threshold,
        "direction": direction,
        "unit": unit,
        "status": status,
        "why_red": list(why_red) if status != "green" else [],
    }


def _sharpe_estimate(returns: list[float]) -> Optional[float]:
    if len(returns) < 2:
        return None
    mu = sum(returns) / len(returns)
    var = sum((r - mu) ** 2 for r in returns) / max(1, len(returns) - 1)
    sd = math.sqrt(var)
    if sd <= 0:
        return None
    # Annualize assuming ~252 trading days; returns above are per-trade.
    # Use sqrt(n_trades) scaling as a rough proxy (honest gap: needs per-day
    # bucketing).
    return (mu / sd) * math.sqrt(252)


# ============================================================================
# 2. Trade book
# ============================================================================

def gather_trade_book(as_of: Optional[datetime] = None, limit: int = 50) -> dict[str, Any]:
    """Open + closed ideas + PnL waterfall + benchmark delta."""
    as_of = as_of or _utc_now()
    conn = _conn()

    open_rows = conn.execute(
        "SELECT id, specialist_id, instrument, direction, confidence, "
        "confluence_score, published_at, expires_at FROM trade_ideas "
        "WHERE status IN ('published','open') AND transaction_to IS NULL "
        "ORDER BY published_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    closed_rows = conn.execute(
        "SELECT id, specialist_id, instrument, direction, confidence, "
        "realized_return_after_fees_pct, benchmark_return_pct, "
        "contributed_alpha_pct, brier, published_at, expires_at "
        "FROM trade_ideas WHERE status = 'closed' AND transaction_to IS NULL "
        "ORDER BY published_at DESC LIMIT ?",
        (limit,),
    ).fetchall()

    closed = [dict(r) for r in closed_rows]
    # PnL waterfall ordered by published_at ascending, cumulative
    cum = 0.0
    waterfall: list[dict[str, Any]] = []
    for r in sorted(closed, key=lambda x: x.get("published_at") or ""):
        pnl = _safe_float(r.get("realized_return_after_fees_pct")) or 0.0
        cum += pnl
        waterfall.append({
            "idea_id": r["id"],
            "instrument": r["instrument"],
            "pnl": pnl,
            "cum_pnl": cum,
        })

    # Discipline (stop / target hit rates)
    n_target = sum(1 for r in closed if (_safe_float(r.get("realized_return_after_fees_pct")) or 0.0) > 0)
    n_stop = sum(1 for r in closed if (_safe_float(r.get("realized_return_after_fees_pct")) or 0.0) < 0)
    n_total = max(1, len(closed))

    best = sorted(
        closed,
        key=lambda x: _safe_float(x.get("contributed_alpha_pct")) or -1e9,
        reverse=True,
    )[:3]
    worst = sorted(
        closed,
        key=lambda x: _safe_float(x.get("contributed_alpha_pct")) or 1e9,
    )[:3]

    return {
        "as_of": _iso(as_of),
        "n_open": len(open_rows),
        "n_closed": len(closed),
        "open": [dict(r) for r in open_rows],
        "closed": closed,
        "waterfall": waterfall,
        "discipline": {
            "n_target_hit": n_target,
            "n_stop_hit": n_stop,
            "target_hit_rate": n_target / n_total,
            "stop_hit_rate": n_stop / n_total,
        },
        "best": best,
        "worst": worst,
    }


# ============================================================================
# 3. Debates
# ============================================================================

def gather_debates_panel(as_of: Optional[datetime] = None) -> dict[str, Any]:
    """Unresolved + judged + judge reliability."""
    as_of = as_of or _utc_now()
    conn = _conn()
    rows = conn.execute(
        "SELECT id, cycle_id, trigger_kind, trigger_id, participants, "
        "judge_model, judge_provider, status, due_at, winner, judge_confidence, "
        "later_brier, valid_from FROM debates "
        "WHERE transaction_to IS NULL ORDER BY valid_from DESC LIMIT 200"
    ).fetchall()
    all_debates = [dict(r) for r in rows]
    unresolved = [d for d in all_debates if d["status"] in ("open", "awaiting_arguments")]
    judged = [d for d in all_debates if d["status"] in ("judged", "applied")]
    overturned = [d for d in judged if _safe_float(d.get("later_brier")) is not None
                  and _safe_float(d.get("later_brier")) >= 0.4]

    # Judge reliability: per judge_model, count + avg later_brier (lower is
    # better; high later_brier means judge was wrong).
    reliability: dict[str, dict[str, Any]] = {}
    for d in judged:
        jm = d.get("judge_model") or "unknown"
        slot = reliability.setdefault(jm, {"n": 0, "later_brier_sum": 0.0,
                                          "later_brier_n": 0,
                                          "judge_confidence_sum": 0.0,
                                          "judge_confidence_n": 0})
        slot["n"] += 1
        lb = _safe_float(d.get("later_brier"))
        if lb is not None:
            slot["later_brier_sum"] += lb
            slot["later_brier_n"] += 1
        jc = _safe_float(d.get("judge_confidence"))
        if jc is not None:
            slot["judge_confidence_sum"] += jc
            slot["judge_confidence_n"] += 1
    reliability_list = []
    for jm, s in reliability.items():
        avg_lb = (s["later_brier_sum"] / s["later_brier_n"]) if s["later_brier_n"] else None
        avg_jc = (s["judge_confidence_sum"] / s["judge_confidence_n"]) if s["judge_confidence_n"] else None
        reliability_list.append({
            "judge_model": jm,
            "n_debates": s["n"],
            "avg_later_brier": avg_lb,
            "avg_judge_confidence": avg_jc,
        })
    reliability_list.sort(key=lambda r: (r["avg_later_brier"] if r["avg_later_brier"] is not None else 1.0))

    return {
        "as_of": _iso(as_of),
        "n_total": len(all_debates),
        "n_unresolved": len(unresolved),
        "n_judged": len(judged),
        "n_overturned": len(overturned),
        "unresolved": unresolved,
        "judged_recent": judged[:20],
        "judge_reliability": reliability_list,
    }


# ============================================================================
# 4. Hypothesis graph sample
# ============================================================================

def gather_hypothesis_graph_sample(seed: Optional[int] = None) -> dict[str, Any]:
    """Return one random hypothesis + its edges + linked claims."""
    conn = _conn()
    rng = random.Random(seed)
    rows = conn.execute(
        "SELECT id, specialist_id, title, hypothesis_text, status, "
        "posterior_prob, heat_score, valid_from FROM hypotheses "
        "WHERE transaction_to IS NULL ORDER BY heat_score DESC LIMIT 50"
    ).fetchall()
    if not rows:
        return {"root": None, "nodes": [], "edges": []}
    root = dict(rng.choice(rows))
    root_id = root["id"]
    edges_rows = conn.execute(
        "SELECT id, from_node_kind, from_node_id, to_node_kind, to_node_id, "
        "edge_kind, strength FROM hypothesis_edges "
        "WHERE (from_node_id = ? OR to_node_id = ?) "
        "AND transaction_to IS NULL LIMIT 50",
        (root_id, root_id),
    ).fetchall()
    edges = [dict(e) for e in edges_rows]

    related_hyp_ids: set[str] = {root_id}
    for e in edges:
        for kind, nid in [(e["from_node_kind"], e["from_node_id"]),
                          (e["to_node_kind"], e["to_node_id"])]:
            if kind == "hypothesis":
                related_hyp_ids.add(nid)

    nodes: list[dict[str, Any]] = []
    for hid in related_hyp_ids:
        row = conn.execute(
            "SELECT id, specialist_id, title, status, posterior_prob, heat_score "
            "FROM hypotheses WHERE id = ? AND transaction_to IS NULL LIMIT 1",
            (hid,),
        ).fetchone()
        if row is not None:
            nodes.append(dict(row))

    n_supports = sum(1 for e in edges if e["edge_kind"] == "supports")
    n_contradicts = sum(1 for e in edges if e["edge_kind"] == "contradicts")
    return {
        "root": root,
        "nodes": nodes,
        "edges": edges,
        "summary": {
            "n_nodes": len(nodes),
            "n_edges": len(edges),
            "n_supports": n_supports,
            "n_contradicts": n_contradicts,
        },
    }


# ============================================================================
# 5. Persona diffs
# ============================================================================

def gather_persona_diffs(as_of: Optional[datetime] = None) -> dict[str, Any]:
    """Mutation candidates pending the 24h veto window vs their base."""
    as_of = as_of or _utc_now()
    conn = _conn()
    rows = conn.execute(
        "SELECT id, specialist_id, persona_version, cycle_id, state_json, "
        "brier_delta, alpha_delta_pct, redundancy_delta, parent_state_id, "
        "ab_test_group, valid_from "
        "FROM specialist_states WHERE state_kind = 'mutation_candidate' "
        "AND transaction_to IS NULL ORDER BY valid_from DESC LIMIT 30"
    ).fetchall()
    candidates: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        # Resolve base state
        base = None
        if d.get("parent_state_id"):
            base_row = conn.execute(
                "SELECT id, persona_version, state_json FROM specialist_states "
                "WHERE id = ?",
                (d["parent_state_id"],),
            ).fetchone()
            if base_row is not None:
                base = dict(base_row)
        try:
            cand_state = json.loads(d.get("state_json") or "{}")
        except Exception:
            cand_state = {}
        try:
            base_state = json.loads(base["state_json"]) if base else {}
        except Exception:
            base_state = {}
        # Detect whether this candidate has a veto already.
        veto_msg = conn.execute(
            "SELECT id, payload FROM agent_messages "
            "WHERE message_kind = 'veto' AND transaction_to IS NULL "
            "AND related_artifact_id = ? LIMIT 1",
            (d["id"],),
        ).fetchone()
        candidates.append({
            "candidate_id": d["id"],
            "specialist_id": d["specialist_id"],
            "persona_version": d["persona_version"],
            "cycle_id": d["cycle_id"],
            "brier_delta": _safe_float(d.get("brier_delta")),
            "alpha_delta_pct": _safe_float(d.get("alpha_delta_pct")),
            "redundancy_delta": _safe_float(d.get("redundancy_delta")),
            "ab_test_group": d.get("ab_test_group"),
            "valid_from": d.get("valid_from"),
            "candidate_state": cand_state,
            "base_state": base_state,
            "veto_present": veto_msg is not None,
            "veto_payload": (json.loads(veto_msg["payload"]) if veto_msg else None),
        })
    return {"as_of": _iso(as_of), "candidates": candidates}


# ============================================================================
# 6. Weakness map
# ============================================================================

def gather_weakness_map(as_of: Optional[datetime] = None) -> dict[str, Any]:
    """Top 5 weaknesses with owner, budget spent, expected lift, actual lift."""
    as_of = as_of or _utc_now()
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT specialist_id, weakness_kind, evidence_json FROM mv_weakness_map"
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    weaknesses: list[dict[str, Any]] = []
    today = as_of.date().isoformat()
    for r in rows:
        d = dict(r)
        try:
            ev = json.loads(d.get("evidence_json") or "{}")
        except Exception:
            ev = {}
        wid = f"weak_{d['specialist_id']}_{today}_{d.get('weakness_kind') or 'agg'}"
        weaknesses.append({
            "weakness_id": wid,
            "specialist_id": d["specialist_id"],
            "weakness_kind": d["weakness_kind"],
            "brier_avg": _safe_float(ev.get("brier")),
            "delta_avg": _safe_float(ev.get("delta")),
        })
    weaknesses.sort(key=lambda w: -(w["brier_avg"] or 0.0))
    weaknesses = weaknesses[:5]

    # Match each weakness to the most recent curriculum allocation that
    # called it out (best-effort linkage via specialist_id).
    plan_rows = conn.execute(
        "SELECT state_json, valid_from FROM specialist_states "
        "WHERE specialist_id = 'research_director' AND state_kind = 'persona' "
        "AND transaction_to IS NULL ORDER BY valid_from DESC LIMIT 5"
    ).fetchall()
    plans: list[dict[str, Any]] = []
    for r in plan_rows:
        try:
            sj = json.loads(r["state_json"])
        except Exception:
            continue
        cp = sj.get("curriculum_plan") if isinstance(sj, dict) else None
        if cp:
            plans.append(cp)

    # Hydrate any persisted curriculum outcomes for actual_lift.
    outcome_rows = conn.execute(
        "SELECT state_json FROM specialist_states "
        "WHERE specialist_id = 'research_director' AND state_kind = 'dehydration' "
        "AND transaction_to IS NULL ORDER BY valid_from DESC LIMIT 20"
    ).fetchall()
    outcomes_by_plan: dict[str, dict[str, Any]] = {}
    for r in outcome_rows:
        try:
            sj = json.loads(r["state_json"])
        except Exception:
            continue
        if not isinstance(sj, dict) or sj.get("kind") != "curriculum_outcome":
            continue
        pid = sj.get("plan_id")
        if pid:
            outcomes_by_plan[pid] = sj.get("outcome") or {}

    for w in weaknesses:
        sid = w["specialist_id"]
        # Find first plan that lists this weakness owner.
        matched = None
        for p in plans:
            alloc = (p.get("per_specialist") or {}).get(sid)
            if alloc:
                matched = (p, alloc)
                break
        if matched is None:
            w["budget_usd"] = None
            w["expected_lift"] = None
            w["actual_lift"] = None
            w["plan_id"] = None
            continue
        plan, alloc = matched
        w["budget_usd"] = _safe_float(alloc.get("budget_usd"))
        w["expected_lift"] = _safe_float(alloc.get("expected_brier_lift"))
        w["plan_id"] = plan.get("plan_id")
        outcome = outcomes_by_plan.get(plan.get("plan_id") or "")
        actual_map = (outcome or {}).get("per_specialist_brier_delta") or {}
        w["actual_lift"] = _safe_float(actual_map.get(sid))

    return {"as_of": _iso(as_of), "weaknesses": weaknesses}


# ============================================================================
# 7. Tool atlas
# ============================================================================

def gather_tool_atlas(as_of: Optional[datetime] = None) -> dict[str, Any]:
    """Coverage, cost, latency, errors, affinity by specialist."""
    as_of = as_of or _utc_now()
    conn = _conn()

    atlas_rows = conn.execute(
        "SELECT tool_uri, tool_name, kind, status, provider, valid_from "
        "FROM tool_atlas WHERE transaction_to IS NULL"
    ).fetchall()
    atlas_summary = {
        "n_tools": len(atlas_rows),
        "by_status": {},
        "by_kind": {},
    }
    for r in atlas_rows:
        d = dict(r)
        atlas_summary["by_status"][d["status"]] = atlas_summary["by_status"].get(d["status"], 0) + 1
        atlas_summary["by_kind"][d["kind"]] = atlas_summary["by_kind"].get(d["kind"], 0) + 1

    # Per-specialist tool usage
    rows = conn.execute(
        "SELECT specialist_id, tool_uri, n_calls, n_success, total_cost_usd, "
        "brier_delta_avg FROM mv_top_tools_per_specialist_30d"
    ).fetchall()
    by_specialist: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        d = dict(r)
        by_specialist.setdefault(d["specialist_id"], []).append({
            "tool_uri": d["tool_uri"],
            "n_calls": int(d["n_calls"] or 0),
            "n_success": int(d["n_success"] or 0),
            "total_cost_usd": _safe_float(d["total_cost_usd"]) or 0.0,
            "brier_delta_avg": _safe_float(d["brier_delta_avg"]),
        })

    # Per-tool latency + error counts from raw tool_call_log
    call_rows = conn.execute(
        "SELECT tool_uri, avg(duration_ms) AS lat, count(*) AS n_calls, "
        "sum(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) AS n_err "
        "FROM tool_call_log WHERE started_at >= ? GROUP BY tool_uri",
        (_iso(as_of - timedelta(days=30)),),
    ).fetchall()
    per_tool_health: list[dict[str, Any]] = []
    for r in call_rows:
        d = dict(r)
        per_tool_health.append({
            "tool_uri": d["tool_uri"],
            "avg_latency_ms": _safe_float(d["lat"]),
            "n_calls": int(d["n_calls"] or 0),
            "n_errors": int(d["n_err"] or 0),
            "error_rate": (int(d["n_err"] or 0) / int(d["n_calls"] or 1))
                          if int(d["n_calls"] or 0) > 0 else None,
        })
    per_tool_health.sort(key=lambda x: -(x["error_rate"] or 0.0))

    return {
        "as_of": _iso(as_of),
        "atlas_summary": atlas_summary,
        "per_specialist": by_specialist,
        "per_tool_health": per_tool_health[:30],
    }


# ============================================================================
# HTML renderer (dark theme, mobile-friendly, no JS framework)
# ============================================================================

PICO_CSS = "https://cdn.jsdelivr.net/npm/@picocss/pico@2/css/pico.min.css"


def render_dashboard_html(as_of: Optional[datetime] = None) -> str:
    """Render the full single-page dashboard HTML. Pure server-render: every
    panel is in the same document and a tiny JS toggles the active tab.

    Returns a complete `<!doctype html>` document as a string.
    """
    as_of = as_of or _utc_now()
    scorecard = gather_scorecard(as_of)
    trade_book = gather_trade_book(as_of)
    debates = gather_debates_panel(as_of)
    graph_sample = gather_hypothesis_graph_sample(seed=int(as_of.timestamp()))
    persona = gather_persona_diffs(as_of)
    weakness = gather_weakness_map(as_of)
    tools = gather_tool_atlas(as_of)

    panels = [
        ("scorecard", "Scorecard", _render_scorecard(scorecard)),
        ("tradebook", "Trade book", _render_trade_book(trade_book)),
        ("debates", "Debates", _render_debates(debates)),
        ("graph", "Hypothesis graph", _render_graph_sample(graph_sample)),
        ("persona", "Persona diffs", _render_persona_diffs(persona)),
        ("weakness", "Weakness map", _render_weakness_map(weakness)),
        ("toolatlas", "Tool atlas", _render_tool_atlas(tools)),
    ]
    tabs = "".join(
        f'<a class="tab-link" href="#panel-{pid}" data-tab="{pid}">{escape(label)}</a>'
        for pid, label, _ in panels
    )
    sections = "".join(
        f'<section class="panel" id="panel-{pid}">'
        f'<h2 class="panel-title">{escape(label)}</h2>{body}</section>'
        for pid, label, body in panels
    )

    s_tier = scorecard["s_tier_candidate"]
    s_tier_pill = (
        '<span class="pill green">S-TIER CANDIDATE</span>'
        if s_tier else '<span class="pill red">NOT S-TIER</span>'
    )

    return f"""<!doctype html>
<html data-theme="dark" lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Talis Desk — Eval Dashboard</title>
  <link rel="stylesheet" href="{PICO_CSS}">
  <style>{_DASHBOARD_CSS}</style>
</head>
<body>
  <header class="desk-header">
    <h1>Talis Desk — Eval Dashboard</h1>
    <div class="desk-meta">
      <span class="muted">as_of {escape(_iso(as_of))}</span>
      {s_tier_pill}
      <span class="muted">{scorecard['n_green']} green · {scorecard['n_red']} red</span>
    </div>
  </header>
  <nav class="desk-tabs">{tabs}</nav>
  <main>
    {sections}
  </main>
  <footer class="desk-footer">
    <span class="muted">talis-desk · v2 §5 manual eval · reviewer flow: red metrics &rarr; losing ideas &rarr; graph &rarr; debates &rarr; persona diffs &rarr; sign weekly eval</span>
  </footer>
  <script>{_DASHBOARD_JS}</script>
</body>
</html>"""


_DASHBOARD_CSS = """
  body { max-width: 1400px; margin: 0 auto; padding: 1rem 1.5rem;
         background: #0c0e12; color: #e3e6ec; }
  h1, h2, h3 { color: #e7ecf3; }
  .desk-header { display: flex; align-items: baseline; justify-content: space-between;
                 border-bottom: 1px solid #222a35; padding-bottom: 0.5rem; }
  .desk-meta { display: flex; align-items: center; gap: 0.8rem; }
  .desk-tabs { display: flex; gap: 0.5rem; margin: 1rem 0;
               border-bottom: 1px solid #222a35; padding-bottom: 0.5rem;
               flex-wrap: wrap; }
  .tab-link { color: #aab3c2; text-decoration: none; padding: 0.3rem 0.7rem;
              border-radius: 6px; font-size: 0.9rem; }
  .tab-link.active { background: #1b3a78; color: #e7ecf3; }
  .panel { display: none; padding: 0.5rem 0; }
  .panel.active { display: block; }
  .panel-title { font-size: 1.3rem; margin-bottom: 0.6rem; }
  table { width: 100%; font-size: 0.85rem; }
  th, td { padding: 0.35rem 0.5rem !important; vertical-align: top;
           border-bottom: 1px solid #1a212d; }
  th { color: #aab3c2; font-weight: 600; }
  .muted { color: #6b7585; font-size: 0.8rem; }
  .pill { display: inline-block; padding: 1px 8px; border-radius: 10px;
          font-size: 0.7rem; font-weight: 600; }
  .pill.green  { background: #1c5; color: white; }
  .pill.yellow { background: #ea3; color: black; }
  .pill.red    { background: #d33; color: white; }
  .pill.unknown { background: #555; color: white; }
  .pill.open   { background: #888; color: white; }
  .pill.judged { background: #36a; color: white; }
  .pill.applied { background: #1c5; color: white; }
  .metric-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
                 gap: 0.6rem; margin-bottom: 1rem; }
  .metric-card { background: #131822; border: 1px solid #1f2733;
                 border-radius: 8px; padding: 0.7rem 0.9rem; }
  .metric-card .metric-value { font-size: 1.6rem; font-weight: 700;
                               font-variant-numeric: tabular-nums; }
  .metric-card .metric-label { color: #aab3c2; font-size: 0.85rem; }
  .metric-card .metric-why { color: #ea3; font-size: 0.75rem; margin-top: 0.3rem; }
  pre.json { background: #131822; padding: 0.7rem; border-radius: 6px;
             max-height: 380px; overflow: auto; font-size: 0.78rem;
             color: #c8cdda; border: 1px solid #1f2733; }
  .num { font-variant-numeric: tabular-nums; }
  .num.pos { color: #6ce28b; }
  .num.neg { color: #ff7777; }
  .small { font-size: 0.75rem; }
  .truncate { max-width: 340px; overflow: hidden; text-overflow: ellipsis;
              white-space: nowrap; }
  .desk-footer { margin-top: 2rem; padding-top: 0.8rem;
                 border-top: 1px solid #222a35; }
  .veto-row { display: flex; gap: 0.5rem; align-items: center; }
  .veto-row input { background: #131822; border: 1px solid #1f2733;
                    color: #e3e6ec; padding: 0.3rem 0.5rem; border-radius: 4px;
                    flex: 1; }
  .veto-row button { background: #d33; color: white; border: none;
                     padding: 0.3rem 0.7rem; border-radius: 4px;
                     cursor: pointer; font-weight: 600; }
  .veto-row button:hover { background: #b22; }
"""

_DASHBOARD_JS = """
(function() {
  const links = document.querySelectorAll('.tab-link');
  const panels = document.querySelectorAll('.panel');
  function activate(name) {
    links.forEach(l => l.classList.toggle('active', l.dataset.tab === name));
    panels.forEach(p => p.classList.toggle('active', p.id === 'panel-' + name));
    try { history.replaceState(null, '', '#panel-' + name); } catch (_) {}
  }
  links.forEach(l => l.addEventListener('click', (e) => {
    e.preventDefault();
    activate(l.dataset.tab);
  }));
  let initial = location.hash ? location.hash.replace('#panel-', '') : 'scorecard';
  if (!document.getElementById('panel-' + initial)) initial = 'scorecard';
  activate(initial);
  window.deskVeto = function(candidateId) {
    const reasonEl = document.getElementById('veto-reason-' + candidateId);
    const reason = reasonEl ? reasonEl.value : '';
    if (!reason || reason.length < 3) {
      alert('Provide a veto reason (>=3 chars).');
      return;
    }
    fetch('/api/veto/' + encodeURIComponent(candidateId) +
          '?reason=' + encodeURIComponent(reason),
          { method: 'POST' })
      .then(r => r.json())
      .then(j => alert('Veto posted: msg=' + (j.message_id || '?')))
      .catch(err => alert('Veto failed: ' + err));
  };
})();
"""


# ---- per-panel HTML helpers -------------------------------------------------

def _fmt_num(v: Optional[float], decimals: int = 4) -> str:
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return '<span class="muted">n/a</span>'
    cls = "pos" if v > 0 else ("neg" if v < 0 else "")
    return f'<span class="num {cls}">{v:.{decimals}f}</span>'


def _render_scorecard(payload: dict[str, Any]) -> str:
    cards = []
    for m in payload["metrics"]:
        val = m["value"]
        val_str = f"{val:.4f}" if val is not None else "—"
        why_html = ""
        if m["status"] != "green" and m.get("why_red"):
            why = " · ".join(escape(str(x)) for x in m["why_red"])
            why_html = f'<div class="metric-why">{why}</div>'
        cards.append(
            f'<div class="metric-card">'
            f'  <div class="metric-label">{escape(m["label"])}</div>'
            f'  <div class="metric-value">{escape(val_str)}'
            f'    <span class="pill {m["status"]}" style="margin-left:0.4rem;">'
            f'      {m["status"].upper()}'
            f'    </span>'
            f'  </div>'
            f'  <div class="small muted">target {m["direction"]} {m["threshold"]} · {escape(m["unit"])}</div>'
            f'  {why_html}'
            f'</div>'
        )
    return f'<div class="metric-grid">{"".join(cards)}</div>'


def _render_trade_book(payload: dict[str, Any]) -> str:
    open_html = _table_html(
        ["id", "specialist_id", "instrument", "direction", "confidence",
         "published_at", "expires_at"],
        payload["open"],
    )
    closed_html = _table_html(
        ["id", "specialist_id", "instrument", "direction",
         "realized_return_after_fees_pct", "benchmark_return_pct",
         "contributed_alpha_pct", "brier"],
        payload["closed"],
        numeric_cols={"realized_return_after_fees_pct", "benchmark_return_pct",
                      "contributed_alpha_pct", "brier"},
    )
    waterfall_rows = "".join(
        f"<tr><td>{escape(w['idea_id'])}</td><td>{escape(w['instrument'])}</td>"
        f"<td>{_fmt_num(w['pnl'])}</td><td>{_fmt_num(w['cum_pnl'])}</td></tr>"
        for w in payload["waterfall"]
    )
    disc = payload["discipline"]
    best_html = _table_html(
        ["id", "instrument", "contributed_alpha_pct"], payload["best"],
        numeric_cols={"contributed_alpha_pct"},
    )
    worst_html = _table_html(
        ["id", "instrument", "contributed_alpha_pct"], payload["worst"],
        numeric_cols={"contributed_alpha_pct"},
    )
    return f"""
    <h3>Open ({payload['n_open']})</h3>
    {open_html}
    <h3>Closed ({payload['n_closed']})</h3>
    {closed_html}
    <h3>PnL waterfall</h3>
    <table><thead><tr><th>idea_id</th><th>instrument</th><th>pnl_pct</th><th>cum</th></tr></thead>
    <tbody>{waterfall_rows or '<tr><td colspan=4 class="muted">no closed ideas yet</td></tr>'}</tbody></table>
    <h3>Discipline</h3>
    <p>target_hits={disc['n_target_hit']} ({disc['target_hit_rate']:.2%}) ·
       stop_hits={disc['n_stop_hit']} ({disc['stop_hit_rate']:.2%})</p>
    <h3>Best ideas (alpha)</h3>
    {best_html}
    <h3>Worst ideas (alpha)</h3>
    {worst_html}
    """


def _render_debates(payload: dict[str, Any]) -> str:
    unresolved_html = _table_html(
        ["id", "trigger_kind", "trigger_id", "participants", "due_at"],
        payload["unresolved"],
    )
    judged_html = _table_html(
        ["id", "trigger_kind", "winner", "judge_model", "judge_confidence", "later_brier"],
        payload["judged_recent"],
        numeric_cols={"judge_confidence", "later_brier"},
    )
    reliability_html = _table_html(
        ["judge_model", "n_debates", "avg_later_brier", "avg_judge_confidence"],
        payload["judge_reliability"],
        numeric_cols={"avg_later_brier", "avg_judge_confidence"},
    )
    return f"""
    <p>total={payload['n_total']} · unresolved={payload['n_unresolved']} ·
       judged={payload['n_judged']} · overturned={payload['n_overturned']}</p>
    <h3>Unresolved</h3>{unresolved_html}
    <h3>Recently judged</h3>{judged_html}
    <h3>Judge reliability</h3>{reliability_html}
    """


def _render_graph_sample(payload: dict[str, Any]) -> str:
    root = payload.get("root")
    if not root:
        return '<p class="muted">No hypotheses yet.</p>'
    summary = payload.get("summary", {})
    nodes_html = _table_html(
        ["id", "specialist_id", "title", "status", "posterior_prob", "heat_score"],
        payload["nodes"],
        numeric_cols={"posterior_prob", "heat_score"},
    )
    edges_html = _table_html(
        ["edge_kind", "from_node_kind", "from_node_id", "to_node_kind", "to_node_id", "strength"],
        payload["edges"],
        numeric_cols={"strength"},
    )
    return f"""
    <h3>Root: {escape(root.get('title') or root.get('id', ''))}</h3>
    <p class="muted">id={escape(root.get('id') or '')} · specialist={escape(root.get('specialist_id') or '')}
       · status={escape(root.get('status') or '')}
       · posterior={root.get('posterior_prob')} · heat={root.get('heat_score')}</p>
    <p>summary: {summary.get('n_nodes', 0)} nodes · {summary.get('n_edges', 0)} edges ·
       supports={summary.get('n_supports', 0)} · contradicts={summary.get('n_contradicts', 0)}</p>
    <h3>Nodes</h3>{nodes_html}
    <h3>Edges</h3>{edges_html}
    """


def _render_persona_diffs(payload: dict[str, Any]) -> str:
    if not payload["candidates"]:
        return '<p class="muted">No pending mutation candidates.</p>'
    rows = []
    for c in payload["candidates"]:
        veto_pill = (
            '<span class="pill red">VETOED</span>'
            if c["veto_present"] else '<span class="pill yellow">PENDING</span>'
        )
        veto_input = (
            f'<div class="veto-row">'
            f'  <input id="veto-reason-{escape(c["candidate_id"])}" '
            f'         placeholder="veto reason (>=3 chars)" />'
            f'  <button onclick="window.deskVeto(\'{escape(c["candidate_id"])}\')">veto</button>'
            f'</div>'
            if not c["veto_present"] else ""
        )
        base_json = escape(json.dumps(c["base_state"], indent=2)[:1500])
        cand_json = escape(json.dumps(c["candidate_state"], indent=2)[:1500])
        rows.append(f"""
        <article class="metric-card">
          <h3>{escape(c["specialist_id"])} · {escape(c["persona_version"])}
              {veto_pill}</h3>
          <p class="small muted">candidate={escape(c["candidate_id"])} ·
             cycle={escape(c["cycle_id"])} ·
             ab_group={escape(c.get("ab_test_group") or "—")}</p>
          <p>brier_delta={_fmt_num(c['brier_delta'])} ·
             alpha_delta_pct={_fmt_num(c['alpha_delta_pct'])} ·
             redundancy_delta={_fmt_num(c['redundancy_delta'])}</p>
          <details><summary>Candidate state</summary>
            <pre class="json">{cand_json}</pre></details>
          <details><summary>Base state</summary>
            <pre class="json">{base_json}</pre></details>
          {veto_input}
        </article>""")
    return "".join(rows)


def _render_weakness_map(payload: dict[str, Any]) -> str:
    if not payload["weaknesses"]:
        return '<p class="muted">No weaknesses in mv_weakness_map yet.</p>'
    cols = ["weakness_id", "specialist_id", "weakness_kind", "brier_avg",
            "budget_usd", "expected_lift", "actual_lift", "plan_id"]
    return _table_html(
        cols, payload["weaknesses"],
        numeric_cols={"brier_avg", "budget_usd", "expected_lift", "actual_lift"},
    )


def _render_tool_atlas(payload: dict[str, Any]) -> str:
    s = payload["atlas_summary"]
    by_status = " · ".join(f"{k}={v}" for k, v in s.get("by_status", {}).items())
    by_kind = " · ".join(f"{k}={v}" for k, v in s.get("by_kind", {}).items())
    per_spec_blocks = []
    for sid, tools in payload["per_specialist"].items():
        table = _table_html(
            ["tool_uri", "n_calls", "n_success", "total_cost_usd", "brier_delta_avg"],
            tools,
            numeric_cols={"total_cost_usd", "brier_delta_avg"},
        )
        per_spec_blocks.append(f"<h3>{escape(sid)}</h3>{table}")
    health = _table_html(
        ["tool_uri", "avg_latency_ms", "n_calls", "n_errors", "error_rate"],
        payload["per_tool_health"],
        numeric_cols={"avg_latency_ms", "error_rate"},
    )
    return f"""
    <p>atlas size: {s.get('n_tools', 0)} · status: {escape(by_status)} · kind: {escape(by_kind)}</p>
    <h3>Per-tool health (30d)</h3>{health}
    {''.join(per_spec_blocks) or '<p class="muted">No specialist tool calls yet.</p>'}
    """


def _table_html(
    cols: list[str],
    rows: list[dict[str, Any]],
    numeric_cols: Optional[set[str]] = None,
) -> str:
    numeric_cols = numeric_cols or set()
    if not rows:
        return '<p class="muted">empty.</p>'
    header = "".join(f"<th>{escape(c)}</th>" for c in cols)
    body_rows = []
    for r in rows:
        cells = []
        for c in cols:
            v = r.get(c)
            if c in numeric_cols:
                cells.append(f"<td>{_fmt_num(_safe_float(v))}</td>")
            elif isinstance(v, (list, dict)):
                cells.append(f'<td class="truncate small">{escape(json.dumps(v)[:140])}</td>')
            else:
                cells.append(f'<td class="truncate">{escape(str(v) if v is not None else "")}</td>')
        body_rows.append(f"<tr>{''.join(cells)}</tr>")
    return f"<table><thead><tr>{header}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>"

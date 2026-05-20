"""Daily brief composer — renders the desk's cycle outputs into markdown.

Source of truth: `wiki/SOTA_DESK_ARCHITECTURE.md` v2. Per Section 1 (North
Star), trade ideas are the **primary** artifact and the brief is the
explanation layer. Per Section 5 (Manual Eval Dashboard), the human
reviewer should be able to read the brief in under 30 minutes and start
at "why is a metric red". Per Section 7 (Kill Switch), the brief
surfaces source health + paper-only mode + the methodology context.

# What this module owns

`compose_brief(...)` is the single entry point. It:
  1. Resolves a bitemporal `ReplayContext` (defaults to now, but accepts
     `as_of` for replay).
  2. Queries desk.db for the open trade book, closed 7d trade book, hot
     hypotheses (`heat_score > 0.7`), recently-judged debates (24h),
     currently-triggered approved playbooks, and reward_log novelty/alpha.
  3. Queries talis-tic via `TICStore` (read-only) for source_health.
  4. Calls `tic.desk.models.chat()` (the 6-provider fallback chain) **once**
     to synthesize a headline that connects the top trade idea + top
     hypothesis + macro regime. Cost is hard-capped at $0.10 with a soft
     budget of $0.05.
  5. Bubbles `quality_flags` from cited claims/tool_call_log up to the
     Brief object so the reader sees `stale_source` / `cap_artifact` etc.
  6. Renders markdown via `talis_desk.brief.templates`.
  7. Persists the brief metadata into TICStore's `artifacts` table with
     `source_ref="talis_desk:brief:cycle_<cycle_id>"` — the one allowed
     write path back into talis-tic (per `wiki/REPO_BOUNDARY.md`).

# Honest gaps

  - **Novelty score** (% of claims that were novel AND correct) is set
    to `None` until Phase 6's `score_novelty` scoring lands. We don't
    fake the metric. (v2 §1 lines 29-36 — `score_novelty()` is specced
    but not yet implemented in the desk pipeline.)
  - **Cycle stats** for cost/tool calls come from `tool_call_log` in
    desk.db filtered by `cycle_id` if provided; otherwise the rolling
    last-24h slice.
  - **HTML / JSON output formats are stubs.** `markdown` is the canonical
    output. See `templates.render_html` / `render_json` for the contract.
  - **No LLM call per trade-idea narrative.** The spec mentions an
    optional per-idea narrative; the current composer omits it to stay
    under the $0.05 soft budget while we wait for usable trade-idea
    volume. The headline already cites the top idea.
  - **No stub headlines.** When every provider in the headline fallback
    chain returns empty/errors, `compose_brief` catches
    `BriefHeadlineUnavailableError`, sets `headline_text=None`, and adds
    `quality_flag='headline_unavailable'` to the methodology block. The
    structured sections below the headline (open trade book, hypotheses,
    debates, etc.) still render — only the LLM-synthesized prose is gated
    on a real LLM response. We never fabricate templated text in place of
    an unavailable headline.

# Cost guardrails

  - Soft budget per brief: **$0.05** (one Sonnet/Haiku headline call).
  - Hard kill: **$0.10**. If observed cost exceeds this, the composer
    raises `BriefCostExceededError` rather than silently overrunning.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import logging
import sqlite3
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Literal, Optional

from ..replay import build_replay_context, ReplayContext
from ..store import get_desk_store
from .templates import (
    NUMERIC_AFFECTING_FLAGS,
    render_brief_markdown,
    render_closed_trade_book,
    render_cycle_stats,
    render_debates,
    render_header,
    render_headline,
    render_hot_hypotheses,
    render_html,
    render_json,
    render_methodology_notes,
    render_open_trade_book,
    render_playbooks,
    render_quiet_cycle,
    render_source_health,
)


logger = logging.getLogger(__name__)


# ============================================================================
# Constants — cost budgets + tuning knobs
# ============================================================================

#: Soft per-brief cost ceiling (USD). One headline call lands here.
BRIEF_COST_SOFT_USD = 0.05

#: Hard per-brief cost ceiling. Composer raises BriefCostExceededError above
#: this — defense in depth against runaway prompts.
BRIEF_COST_HARD_USD = 0.10

#: Approximate per-1k-token prices we use for accounting. The chat() wrapper
#: doesn't currently return usage tokens (see honest gap in `models.py`), so
#: we estimate from the response length. Conservative defaults.
_COST_PER_1K_TOKENS_BY_MODEL: dict[str, float] = {
    # Anthropic
    "anthropic:claude-haiku-4-5":  0.0008,
    "anthropic:claude-sonnet-4-6": 0.0030,
    "anthropic:claude-opus-4-7":   0.0150,
    # OpenAI
    "openai:gpt-5.5":              0.0050,
    "openai:gpt-4o":               0.0050,
    # xAI
    "xai:grok-4":                  0.0050,
    "xai:grok-3":                  0.0030,
    # Moonshot
    "moonshot:kimi-k2.6":          0.0030,
    # Perplexity
    "perplexity:sonar-pro":        0.0050,
    # DeepSeek
    "deepseek:v4-pro":             0.0010,
    "deepseek:v4-flash":           0.0003,
}
_DEFAULT_COST_PER_1K = 0.005  # used when model isn't in the table

#: Hot-hypothesis threshold per v2 §1 + §3 (`heat_score >= 0.7`).
HEAT_SCORE_HOT_THRESHOLD = 0.7

#: Closed trade book window (days) shown in the brief.
CLOSED_BOOK_LOOKBACK_DAYS = 7

#: Debates window — what counts as "recent".
DEBATES_LOOKBACK_HOURS = 24

#: Reward log window for novelty/alpha aggregation.
REWARD_LOOKBACK_HOURS = 24

#: Default headline model. The 6-provider fallback chain in `tic.desk.models`
#: will demote to sonnet/haiku/openai on failure.
DEFAULT_HEADLINE_MODEL = "anthropic:claude-sonnet-4-6"
DEFAULT_HEADLINE_FALLBACK = "anthropic:claude-haiku-4-5"


class BriefHeadlineUnavailableError(RuntimeError):
    """Raised when every provider in the headline LLM fallback chain has
    failed. NO stub headlines — the composer marks the brief with
    quality_flag='headline_unavailable' and persists the rest of the
    structured data so the user still gets actionable content."""


# Full multi-provider fallback chain for the brief headline. Mirrors the
# judge.py pattern. NEVER stub.
_BRIEF_FALLBACK_CHAIN = [
    "anthropic:claude-sonnet-4-6",
    "anthropic:claude-opus-4-7",
    "anthropic:claude-haiku-4-5",
    "openai:gpt-5.5",
    "deepseek:v4-pro",
    "xai:grok-4",
    "moonshot:v1-32k",
    "perplexity:sonar-pro",
]


def _build_brief_provider_chain(primary_model: str,
                                  secondary_model: Optional[str]) -> list[str]:
    """Caller's primary model first, then optional secondary, then the rest
    of the canonical chain, deduplicated."""
    seen: set[str] = set()
    chain: list[str] = []
    for m in (primary_model, secondary_model):
        if m and m not in seen:
            chain.append(m); seen.add(m)
    for m in _BRIEF_FALLBACK_CHAIN:
        if m not in seen:
            chain.append(m); seen.add(m)
    return chain


class BriefCostExceededError(RuntimeError):
    """Raised when a single brief's LLM cost exceeds BRIEF_COST_HARD_USD."""


# ============================================================================
# Output dataclass — what compose_brief returns
# ============================================================================

@dataclass
class Brief:
    """Output of `compose_brief`. Bitemporal — every cite has lineage.

    Fields:
      id: synthetic `brf_<hex>` id.
      cycle_id: the desk cycle this brief reflects.
      scope: usually 'market'; could be 'btc' / 'eth' / a specialist slug.
      as_of: bitemporal anchor (world-time).
      title: short title used in eval dashboards.
      markdown: the rendered brief (primary output).
      structured_payload: dict of the underlying data (debug + JSON output).
      cited_claim_ids / cited_hypothesis_ids / cited_trade_idea_ids /
        cited_debate_ids: lineage. Resolvable via the desk / TIC store.
      quality_flags: bubbled-up flags from cited claims (e.g. stale_source).
      cost_usd: total LLM spend for this brief.
      elapsed_seconds: wall-clock to compose.
      valid_from / transaction_from: bitemporal stamps.
    """
    id: str
    cycle_id: str
    scope: str
    as_of: datetime
    title: str
    markdown: str
    structured_payload: dict[str, Any]
    cited_claim_ids: list[str]
    cited_hypothesis_ids: list[str]
    cited_trade_idea_ids: list[str]
    cited_debate_ids: list[str]
    quality_flags: list[str]
    cost_usd: float
    elapsed_seconds: float
    valid_from: datetime
    transaction_from: datetime
    # Optional convenience fields (not in the spec but useful in tests)
    headline_model_used: Optional[str] = None
    headline_provider: Optional[str] = None
    headline_fallback_used: bool = False
    tic_artifact_id: Optional[str] = None
    output_format: str = "markdown"
    rendered_html: Optional[str] = None
    rendered_json: Optional[str] = None


# ============================================================================
# Top-level entry point
# ============================================================================

def compose_brief(
    cycle_id: Optional[str] = None,
    as_of: Optional[datetime] = None,
    scope: str = "market",
    output_format: Literal["markdown", "html", "json"] = "markdown",
    *,
    conn: Optional[sqlite3.Connection] = None,
    headline_model: str = DEFAULT_HEADLINE_MODEL,
    headline_fallback: str = DEFAULT_HEADLINE_FALLBACK,
    write_tic_artifact: bool = True,
) -> Brief:
    """Compose a daily brief from the desk's current state.

    See module docstring for the full contract. Briefly:
      - `cycle_id`: if None, we don't filter cycle stats by cycle, we use
        the rolling last-24h slice instead.
      - `as_of`: world-time anchor. None -> now (UTC). Replay queries use
        `build_replay_context(as_of_valid=as_of)`.
      - `scope`: free-form label for the brief title.
      - `output_format`: 'markdown' is canonical; 'html' / 'json' are stubs.
      - `conn`: override the desk.db connection (used by tests).
      - `headline_model`: starting model for the headline LLM call. The
        6-provider fallback chain in `tic.desk.models.chat()` kicks in on
        any provider failure.
      - `write_tic_artifact`: True to persist into TICStore's `artifacts`
        table. False for dry runs.

    Returns a populated `Brief` object. Raises `BriefCostExceededError` if
    the LLM call exceeds the hard cost ceiling.
    """
    start = time.perf_counter()
    now = _utc_now()
    if as_of is None:
        # "Show me the world right now" — bias forward by 1 second so rows
        # whose `transaction_from` SQLite default landed milliseconds after
        # the caller's `datetime.now()` clock-read are still visible. The
        # `valid_from <= ? AND transaction_from <= ?` predicate is otherwise
        # racy on same-tick writes.
        anchor = now + timedelta(seconds=1)
    else:
        anchor = as_of
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)
    ctx = build_replay_context(as_of_valid=anchor)

    conn = conn or get_desk_store().conn

    # --- Pull data ---------------------------------------------------------
    open_book = _fetch_open_trade_book(conn, ctx)
    closed_book = _fetch_closed_trade_book(conn, anchor, CLOSED_BOOK_LOOKBACK_DAYS)
    book_metrics = _compute_book_metrics(conn, anchor, CLOSED_BOOK_LOOKBACK_DAYS)
    hot_hyps = _fetch_hot_hypotheses(conn, ctx)
    debates = _fetch_recent_debates(conn, anchor, DEBATES_LOOKBACK_HOURS)
    triggered_pbs = _fetch_triggered_playbooks(conn, ctx)
    reward_summary = _fetch_reward_summary(conn, anchor, REWARD_LOOKBACK_HOURS)
    cycle_stats = _fetch_cycle_stats(conn, cycle_id, anchor)
    source_health = _fetch_source_health()  # talis-tic; degraded if missing

    # --- Quality flag propagation -----------------------------------------
    cited_claim_ids = _gather_cited_claim_ids(open_book, hot_hyps, debates)
    quality_flags = _bubble_quality_flags(conn, cited_claim_ids, open_book,
                                           hot_hyps, ctx)

    # --- Headline synthesis (one LLM call) --------------------------------
    headline_payload = {
        "top_idea": open_book[0] if open_book else None,
        "top_hypothesis": hot_hyps[0] if hot_hyps else None,
        "recent_debate": debates[0] if debates else None,
        "n_open": len(open_book),
        "n_hot": len(hot_hyps),
        "as_of": _iso(anchor),
        "scope": scope,
    }
    headline_unavailable_reason: Optional[str] = None
    try:
        headline_text, headline_meta, cost_usd = _synthesize_headline(
            payload=headline_payload,
            model=headline_model,
            fallback=headline_fallback,
        )
    except BriefHeadlineUnavailableError as e:
        # NO STUBS — every provider in the headline fallback chain failed.
        # Surface the gap explicitly; the brief still emits with the full
        # structured trade book, hypotheses, debates, etc.
        headline_text = None
        headline_meta = {
            "stub": False,
            "quiet_cycle": False,
            "model_used": None,
            "provider": None,
            "fallback_used": False,
            "chain_position": None,
            "error": str(e)[:500],
        }
        cost_usd = 0.0
        headline_unavailable_reason = str(e)[:500]
        quality_flags = sorted(set(quality_flags + ["headline_unavailable"]))
        logger.warning(
            "compose_brief: headline unavailable, emitting brief without "
            "a synthesized headline. reason=%s", headline_unavailable_reason,
        )

    if cost_usd > BRIEF_COST_HARD_USD:
        raise BriefCostExceededError(
            f"brief LLM spend ${cost_usd:.4f} exceeded hard cap "
            f"${BRIEF_COST_HARD_USD:.4f}"
        )
    if cost_usd > BRIEF_COST_SOFT_USD:
        logger.warning(
            "compose_brief: cost $%.4f exceeded soft budget $%.4f",
            cost_usd, BRIEF_COST_SOFT_USD,
        )

    # Replaces the old `_deterministic_summary` fallback — we no longer
    # fabricate a templated headline when the LLM is unavailable. The
    # `fallback_summary` field is kept on the structured payload as None
    # for back-compat with downstream consumers.
    fallback_summary: Optional[str] = None

    # --- Build sections ---------------------------------------------------
    is_quiet = not open_book and not hot_hyps
    sections: dict[str, str] = {
        "header": render_header(scope, anchor),
        "headline": render_headline(headline_text, fallback_summary),
    }
    if is_quiet:
        sections["quiet_cycle"] = render_quiet_cycle()
    else:
        sections["open_trade_book"] = render_open_trade_book(open_book)
        sections["closed_trade_book"] = render_closed_trade_book(
            closed_book, book_metrics
        )
        sections["hot_hypotheses"] = render_hot_hypotheses(hot_hyps)
        sections["debates"] = render_debates(debates)
        sections["playbooks"] = render_playbooks(triggered_pbs)
    sections["source_health"] = render_source_health(source_health)
    sections["cycle_stats"] = render_cycle_stats({
        **cycle_stats,
        "novel_and_correct_pct": reward_summary.get("novel_and_correct_pct"),
    })
    sections["methodology"] = render_methodology_notes(quality_flags, anchor)

    markdown = render_brief_markdown(sections)

    # --- Structured payload (drives JSON output + downstream consumers) ---
    structured_payload: dict[str, Any] = {
        "headline_text": headline_text,
        "headline_meta": headline_meta,
        "fallback_summary": fallback_summary,
        "open_trade_book": open_book,
        "closed_trade_book": closed_book,
        "book_metrics": book_metrics,
        "hot_hypotheses": hot_hyps,
        "debates": debates,
        "triggered_playbooks": triggered_pbs,
        "source_health": source_health,
        "cycle_stats": cycle_stats,
        "reward_summary": reward_summary,
        "quality_flags": quality_flags,
        "scope": scope,
        "as_of": _iso(anchor),
        "is_quiet_cycle": is_quiet,
    }

    # --- Assemble Brief ---------------------------------------------------
    brief_id = "brf_" + uuid.uuid4().hex[:12]
    title = f"Talis Daily Brief — {scope} — {_short_date(anchor)}"
    elapsed = time.perf_counter() - start
    brief = Brief(
        id=brief_id,
        cycle_id=cycle_id or cycle_stats.get("cycle_id") or "ad_hoc",
        scope=scope,
        as_of=anchor,
        title=title,
        markdown=markdown,
        structured_payload=structured_payload,
        cited_claim_ids=cited_claim_ids,
        cited_hypothesis_ids=[h.get("id") for h in hot_hyps if h.get("id")],
        cited_trade_idea_ids=[ti.get("id") for ti in open_book if ti.get("id")]
                              + [ti.get("id") for ti in closed_book if ti.get("id")],
        cited_debate_ids=[d.get("id") for d in debates if d.get("id")],
        quality_flags=quality_flags,
        cost_usd=cost_usd,
        elapsed_seconds=elapsed,
        valid_from=anchor,
        transaction_from=now,
        headline_model_used=headline_meta.get("model_used"),
        headline_provider=headline_meta.get("provider"),
        headline_fallback_used=bool(headline_meta.get("fallback_used")),
        output_format=output_format,
    )

    if output_format == "html":
        brief.rendered_html = render_html(markdown)
    elif output_format == "json":
        brief.rendered_json = render_json(structured_payload)

    # --- Persist to TIC artifacts table (the one allowed mutation) --------
    if write_tic_artifact:
        try:
            brief.tic_artifact_id = _write_to_tic_artifacts(brief)
        except Exception as e:  # noqa: BLE001
            logger.warning("compose_brief: TIC artifact write failed: %s", e)

    return brief


# ============================================================================
# Data fetchers — desk.db
# ============================================================================

def _fetch_open_trade_book(
    conn: sqlite3.Connection, ctx: ReplayContext,
) -> list[dict[str, Any]]:
    """Pull rows from mv_trade_book_open + join edge_thesis + quality_flags.

    Quality flags here come from the trade idea's payload (`validation_report`
    warnings + any post-emit warnings the resolver might add). Source-level
    flags come from `tool_call_log.quality_flags` on cited tool calls.
    """
    # We can't bitemporally filter the view itself (it has hard-coded
    # status filter). Instead, we re-query the underlying table to get all
    # the fields we need.
    where, params = ctx.where_clause("trade_ideas")
    sql = (
        "SELECT id, cycle_id, specialist_id, instrument, direction, sizing, "
        "       entry, stop, target, time_horizon, edge_thesis, "
        "       claim_ids, hypothesis_ids, debate_ids, tool_call_ids, "
        "       confidence, confluence_score, published_at, expires_at, "
        "       status, payload "
        "FROM trade_ideas "
        f"WHERE status IN ('published','open') AND {where} "
        "ORDER BY confidence DESC, published_at DESC LIMIT 50"
    )
    rows = conn.execute(sql, params).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        d["sizing"] = _json_load(d.get("sizing")) or {}
        d["entry"] = _json_load(d.get("entry")) or {}
        d["stop"] = _json_load(d.get("stop")) or {}
        d["target"] = _json_load(d.get("target")) or None
        d["claim_ids"] = _json_load(d.get("claim_ids")) or []
        d["hypothesis_ids"] = _json_load(d.get("hypothesis_ids")) or []
        d["debate_ids"] = _json_load(d.get("debate_ids")) or []
        d["tool_call_ids"] = _json_load(d.get("tool_call_ids")) or []
        d["payload"] = _json_load(d.get("payload")) or {}
        # Quality flags from validation_report warnings + tool_call_log
        warns = (
            d.get("payload", {}).get("validation_report", {}).get("warnings")
            or []
        )
        d["quality_flags"] = list(set(warns))
        out.append(d)
    return out


def _fetch_closed_trade_book(
    conn: sqlite3.Connection, anchor: datetime, window_days: int,
) -> list[dict[str, Any]]:
    """Closed ideas in the last `window_days`."""
    cutoff = _iso(anchor - timedelta(days=window_days))
    rows = conn.execute(
        "SELECT id, instrument, direction, status, "
        "       realized_pnl_pct, realized_return_after_fees_pct, "
        "       benchmark_return_pct, contributed_alpha_pct, brier "
        "FROM trade_ideas "
        "WHERE status IN ('closed','expired','invalidated') "
        "AND transaction_to IS NULL "
        "AND valid_from >= ? "
        "ORDER BY valid_from DESC LIMIT 100",
        (cutoff,),
    ).fetchall()
    return [dict(r) for r in rows]


def _compute_book_metrics(
    conn: sqlite3.Connection, anchor: datetime, window_days: int,
) -> dict[str, Any]:
    """Reuse the resolver's `compute_trade_book_metrics` so the brief's
    aggregate row matches the dashboard's metric."""
    try:
        from ..eval import compute_trade_book_metrics
        m = compute_trade_book_metrics(window_days=window_days, conn=conn,
                                       as_of=anchor)
        return {
            "window_days": m.window_days,
            "as_of": _iso(m.as_of),
            "n_closed": m.n_closed,
            "hit_rate": m.hit_rate,
            "avg_return_after_fees_pct": m.avg_return_after_fees_pct,
            "avg_alpha_pct": m.avg_alpha_pct,
            "sharpe": m.sharpe,
            "brier_avg": m.brier_avg,
            "biggest_loss_pct": m.biggest_loss_pct,
            "biggest_loss_idea_id": m.biggest_loss_idea_id,
            "best_alpha_pct": m.best_alpha_pct,
            "best_alpha_idea_id": m.best_alpha_idea_id,
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("compose_brief: book metrics computation failed: %s", e)
        return {"window_days": window_days, "n_closed": 0}


def _fetch_hot_hypotheses(
    conn: sqlite3.Connection, ctx: ReplayContext,
) -> list[dict[str, Any]]:
    """Active hypotheses with heat_score above threshold, ordered by heat."""
    where, params = ctx.where_clause("hypotheses", alias="h")
    sql = (
        "SELECT h.id, h.specialist_id, h.title, h.hypothesis_text, "
        "       h.posterior_prob, h.heat_score, h.novelty_score, "
        "       h.expected_resolution_at, h.claim_ids, h.source_ids, "
        "       h.tool_call_ids, h.payload "
        "FROM hypotheses h "
        f"WHERE h.status = 'active' AND h.heat_score >= ? AND {where} "
        "ORDER BY h.heat_score DESC, h.transaction_from DESC LIMIT 25"
    )
    rows = conn.execute(sql, [HEAT_SCORE_HOT_THRESHOLD] + params).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        d["claim_ids"] = _json_load(d.get("claim_ids")) or []
        d["source_ids"] = _json_load(d.get("source_ids")) or []
        d["tool_call_ids"] = _json_load(d.get("tool_call_ids")) or []
        d["payload"] = _json_load(d.get("payload")) or {}
        # Pull supporting / contradicting edges
        supports, contradicts = _fetch_edges_for_hypothesis(conn, d["id"], ctx)
        d["supporting_ids"] = supports
        d["contradicting_ids"] = contradicts
        out.append(d)
    return out


def _fetch_edges_for_hypothesis(
    conn: sqlite3.Connection, hyp_id: str, ctx: ReplayContext,
) -> tuple[list[str], list[str]]:
    """Return (supporters, contradicters) — node IDs from edges pointing TO
    the hypothesis. Honors the bitemporal slice."""
    where, params = ctx.where_clause("hypothesis_edges")
    rows = conn.execute(
        f"SELECT from_node_id, edge_kind FROM hypothesis_edges "
        f"WHERE to_node_id = ? AND {where} LIMIT 50",
        [hyp_id] + params,
    ).fetchall()
    supports = [r["from_node_id"] for r in rows if r["edge_kind"] == "supports"]
    contradicts = [r["from_node_id"] for r in rows
                   if r["edge_kind"] == "contradicts"]
    return supports, contradicts


def _fetch_recent_debates(
    conn: sqlite3.Connection, anchor: datetime, lookback_hours: int,
) -> list[dict[str, Any]]:
    """Judged debates in the last `lookback_hours`."""
    cutoff = _iso(anchor - timedelta(hours=lookback_hours))
    rows = conn.execute(
        "SELECT id, cycle_id, trigger_kind, trigger_id, participants, "
        "       judge_model, judge_provider, status, winner, verdict, "
        "       judge_confidence, transaction_from "
        "FROM debates "
        "WHERE status IN ('judged','applied') "
        "AND transaction_to IS NULL "
        "AND transaction_from >= ? "
        "ORDER BY transaction_from DESC LIMIT 25",
        (cutoff,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        d["participants"] = _json_load(d.get("participants")) or []
        verdict = _json_load(d.get("verdict")) or {}
        d["rationale"] = verdict.get("rationale", "")
        d["follow_up_action"] = verdict.get("follow_up_action")
        out.append(d)
    return out


def _fetch_triggered_playbooks(
    conn: sqlite3.Connection, ctx: ReplayContext,
) -> list[dict[str, Any]]:
    """Approved playbooks that have a recent trigger event in
    `agent_messages` (kind='playbook_trigger') OR an instantiated trade idea
    with `playbook_id` set in the last 24h."""
    where, params = ctx.where_clause("playbooks", alias="p")
    rows = conn.execute(
        "SELECT p.id, p.name, p.version, p.owner_specialist, p.description, "
        "       p.historical_trigger_count, p.historical_hit_rate, "
        "       p.historical_avg_return_pct, p.evidence_ids, p.promoted_status "
        "FROM playbooks p "
        f"WHERE p.promoted_status = 'approved' AND {where} "
        "ORDER BY p.historical_hit_rate DESC NULLS LAST LIMIT 25",
        params,
    ).fetchall() if _supports_nulls_last(conn) else conn.execute(
        "SELECT p.id, p.name, p.version, p.owner_specialist, p.description, "
        "       p.historical_trigger_count, p.historical_hit_rate, "
        "       p.historical_avg_return_pct, p.evidence_ids, p.promoted_status "
        "FROM playbooks p "
        f"WHERE p.promoted_status = 'approved' AND {where} "
        "ORDER BY COALESCE(p.historical_hit_rate, -1) DESC LIMIT 25",
        params,
    ).fetchall()
    if not rows:
        return []
    out: list[dict[str, Any]] = []
    # Find trade ideas with playbook_id set in the lookback window so we can
    # show the auto-generated trade idea id alongside the playbook row.
    twenty_four_h_ago = _iso(_utc_now() - timedelta(hours=24))
    pb_ids = [r["id"] for r in rows]
    placeholders = ",".join("?" * len(pb_ids))
    ti_rows = conn.execute(
        f"SELECT id, playbook_id FROM trade_ideas "
        f"WHERE playbook_id IN ({placeholders}) "
        f"AND transaction_to IS NULL "
        f"AND valid_from >= ?",
        (*pb_ids, twenty_four_h_ago),
    ).fetchall()
    ti_by_pb = {r["playbook_id"]: r["id"] for r in ti_rows}
    for r in rows:
        d = dict(r)
        d["evidence_ids"] = _json_load(d.get("evidence_ids")) or []
        d["instantiated_trade_idea_id"] = ti_by_pb.get(d["id"])
        out.append(d)
    return out


def _supports_nulls_last(conn: sqlite3.Connection) -> bool:
    """SQLite needs `COALESCE(... , -1)` since NULLS LAST is non-standard."""
    try:
        conn.execute("SELECT 1 ORDER BY 1 NULLS LAST LIMIT 1").fetchone()
        return True
    except sqlite3.OperationalError:
        return False


def _fetch_reward_summary(
    conn: sqlite3.Connection, anchor: datetime, lookback_hours: int,
) -> dict[str, Any]:
    """Aggregate novelty + alpha attribution from `reward_log` over the
    last `lookback_hours`. Novel-and-correct % is a function of two
    separate reward kinds; we return None for that metric until Phase 6
    novelty scoring is wired (per the honest gap in module docstring)."""
    cutoff = _iso(anchor - timedelta(hours=lookback_hours))
    novelty_rows = conn.execute(
        "SELECT score, delta FROM reward_log "
        "WHERE reward_kind='novelty' AND transaction_to IS NULL "
        "AND valid_from >= ?",
        (cutoff,),
    ).fetchall()
    alpha_rows = conn.execute(
        "SELECT score, delta FROM reward_log "
        "WHERE reward_kind='alpha' AND transaction_to IS NULL "
        "AND valid_from >= ?",
        (cutoff,),
    ).fetchall()
    novelty_avg = (
        sum(r["score"] for r in novelty_rows) / len(novelty_rows)
        if novelty_rows else None
    )
    alpha_avg = (
        sum(r["score"] for r in alpha_rows) / len(alpha_rows)
        if alpha_rows else None
    )
    return {
        "novelty_avg": novelty_avg,
        "alpha_avg": alpha_avg,
        "novel_and_correct_pct": None,  # Phase 6 gap
        "n_novelty_rows": len(novelty_rows),
        "n_alpha_rows": len(alpha_rows),
    }


def _fetch_cycle_stats(
    conn: sqlite3.Connection,
    cycle_id: Optional[str],
    anchor: datetime,
) -> dict[str, Any]:
    """Tool-call count + cost + specialist details for the brief footer."""
    base_sql = (
        "SELECT count(*) AS n_tool_calls, "
        "       COALESCE(sum(cost_usd), 0.0) AS cost_usd "
        "FROM tool_call_log "
        "WHERE transaction_to IS NULL "
    )
    if cycle_id:
        row = conn.execute(base_sql + "AND cycle_id = ?", (cycle_id,)).fetchone()
    else:
        cutoff = _iso(anchor - timedelta(hours=24))
        row = conn.execute(base_sql + "AND started_at >= ?",
                           (cutoff,)).fetchone()
    n_tc = (row["n_tool_calls"] if row else 0) or 0
    cost = (row["cost_usd"] if row else 0.0) or 0.0

    # Count hypotheses, trade ideas, debates for this cycle
    if cycle_id:
        n_hyp = conn.execute(
            "SELECT count(*) FROM hypotheses WHERE cycle_id = ? "
            "AND transaction_to IS NULL", (cycle_id,)
        ).fetchone()[0] or 0
        n_ti = conn.execute(
            "SELECT count(*) FROM trade_ideas WHERE cycle_id = ? "
            "AND transaction_to IS NULL", (cycle_id,)
        ).fetchone()[0] or 0
        n_deb = conn.execute(
            "SELECT count(*) FROM debates WHERE cycle_id = ? "
            "AND transaction_to IS NULL", (cycle_id,)
        ).fetchone()[0] or 0
        specialist = None
        persona_version = None
        sp_row = conn.execute(
            "SELECT specialist_id, persona_version FROM specialist_states "
            "WHERE cycle_id = ? ORDER BY transaction_from DESC LIMIT 1",
            (cycle_id,),
        ).fetchone()
        if sp_row:
            specialist = sp_row["specialist_id"]
            persona_version = sp_row["persona_version"]
    else:
        cutoff = _iso(anchor - timedelta(hours=24))
        n_hyp = conn.execute(
            "SELECT count(*) FROM hypotheses WHERE transaction_to IS NULL "
            "AND valid_from >= ?", (cutoff,)
        ).fetchone()[0] or 0
        n_ti = conn.execute(
            "SELECT count(*) FROM trade_ideas WHERE transaction_to IS NULL "
            "AND valid_from >= ?", (cutoff,)
        ).fetchone()[0] or 0
        n_deb = conn.execute(
            "SELECT count(*) FROM debates WHERE transaction_to IS NULL "
            "AND valid_from >= ?", (cutoff,)
        ).fetchone()[0] or 0
        specialist = "desk"
        persona_version = "rolling_24h"
    return {
        "cycle_id": cycle_id,
        "specialist_id": specialist or "desk",
        "persona_version": persona_version or "rolling_24h",
        "n_tool_calls": int(n_tc),
        "cost_usd": float(cost),
        "n_hypotheses_proposed": int(n_hyp),
        "n_trade_ideas_emitted": int(n_ti),
        "n_debates": int(n_deb),
    }


# ============================================================================
# Data fetchers — talis-tic (read-only)
# ============================================================================

def _fetch_source_health() -> list[dict[str, Any]]:
    """Pull source_health from talis-tic. Returns a *list* of degraded
    sources only (status != 'ok'). Returns an empty list when talis-tic
    isn't importable (CI / standalone tests / dev without sibling checkout).

    We import lazily so the desk doesn't hard-fail when talis-tic is
    missing — the boundary contract permits read access, not a hard
    dependency at import time.
    """
    try:
        _ensure_tic_on_path()
        from tic.ingest._health import query_source_health  # type: ignore
        snapshot = query_source_health(lookback_hours=72)
    except Exception as e:  # noqa: BLE001
        logger.info("compose_brief: source_health unavailable (%s)", e)
        return []

    degraded: list[dict[str, Any]] = []
    for slug, info in (snapshot or {}).items():
        status = info.get("current_status")
        if status and status != "ok":
            degraded.append({
                "source_slug": slug,
                "current_status": status,
                "last_successful_at": info.get("last_successful_at"),
                "last_error": info.get("last_error"),
                "consecutive_failures": info.get("consecutive_failures"),
            })
    return degraded


# ============================================================================
# Quality-flag propagation
# ============================================================================

def _gather_cited_claim_ids(
    open_book: list[dict],
    hot_hyps: list[dict],
    debates: list[dict],
) -> list[str]:
    """Union all claim_ids cited across the surfaces shown in the brief."""
    s: set[str] = set()
    for ti in open_book:
        s.update(ti.get("claim_ids") or [])
    for h in hot_hyps:
        s.update(h.get("claim_ids") or [])
    for d in debates:
        # Debates cite via the verdict's "citation_ids" if present; not
        # currently surfaced in our debate row, so noop is fine.
        pass
    return sorted(s)


def _bubble_quality_flags(
    conn: sqlite3.Connection,
    claim_ids: list[str],
    open_book: list[dict],
    hot_hyps: list[dict],
    ctx: ReplayContext,
) -> list[str]:
    """Walk the lineage of every cited claim + tool_call and bubble the
    union of quality_flags up to the Brief level. This is what lets the
    reader see `stale_source` even when the table cell doesn't show it.

    Sources of flags:
      1. `tool_call_log.quality_flags` on cited tool calls.
      2. `payload.validation_report.warnings` on cited trade ideas.
      3. The TICStore `claims` table (when reachable) — flags on the
         actual cited claim rows.
    """
    flags: set[str] = set()

    # 1. Tool-call flags from desk.db
    tool_call_ids: set[str] = set()
    for ti in open_book:
        tool_call_ids.update(ti.get("tool_call_ids") or [])
    for h in hot_hyps:
        tool_call_ids.update(h.get("tool_call_ids") or [])
    if tool_call_ids:
        placeholders = ",".join("?" * len(tool_call_ids))
        rows = conn.execute(
            f"SELECT quality_flags FROM tool_call_log "
            f"WHERE id IN ({placeholders}) AND transaction_to IS NULL",
            tuple(tool_call_ids),
        ).fetchall()
        for r in rows:
            for f in (_json_load(r["quality_flags"]) or []):
                flags.add(f)

    # 2. Trade idea validation_report warnings
    for ti in open_book:
        for f in ti.get("quality_flags") or []:
            flags.add(f)

    # 3. TIC claim quality_flags (best-effort)
    if claim_ids:
        try:
            _ensure_tic_on_path()
            from tic.store import TICStore  # type: ignore
            tic = TICStore()
            placeholders = ",".join("?" * len(claim_ids))
            rows = tic.conn.execute(
                f"SELECT quality_flags FROM claims WHERE id IN ({placeholders})",
                tuple(claim_ids),
            ).fetchall()
            for r in rows:
                try:
                    qf = json.loads(r["quality_flags"] or "[]")
                except Exception:
                    qf = []
                for f in qf or []:
                    flags.add(f)
        except Exception as e:  # noqa: BLE001
            logger.info("compose_brief: TIC claim flags unavailable (%s)", e)

    return sorted(flags)


# ============================================================================
# Headline synthesis — the one LLM call
# ============================================================================

def _synthesize_headline(
    payload: dict[str, Any],
    model: str,
    fallback: str,
) -> tuple[str, dict[str, Any], float]:
    """Walk the full multi-provider headline fallback chain to produce a
    2-sentence headline. Returns (text, meta, cost_usd).

    NO STUBS: if every provider in the chain returns empty/errors, raise
    BriefHeadlineUnavailableError so the caller can mark the brief with
    quality_flag='headline_unavailable' and still emit the structured
    sections beneath. Never fabricate templated text in place of a
    real LLM headline.

    The only deterministic shortcut is the quiet-cycle path (no open
    ideas AND no hot hypotheses), which is not a fallback for an LLM
    failure — it's an explicit "we don't need the LLM here" decision.
    """
    system = (
        "You are the desk briefer for the Talis autonomous Hyperliquid "
        "research desk. Your sole task: write a TWO-sentence headline that "
        "links the most actionable open trade idea to the macro/microstructure "
        "frame and any debate verdict in play. Be concrete — cite the "
        "instrument, direction, confidence, and the single highest-conviction "
        "supporting or contradicting hypothesis. No hype, no caveats, no "
        "emojis. If there is no open trade idea AND no hot hypothesis, "
        "produce one sentence noting the quiet cycle and the watch item "
        "for the next cycle."
    )
    user = _format_headline_prompt(payload)

    if not payload.get("top_idea") and not payload.get("top_hypothesis"):
        # Quiet cycle: skip the LLM call entirely. This is NOT a stub for
        # an LLM failure; it's a deterministic shortcut when there is
        # nothing for the LLM to summarize.
        return (
            "Quiet cycle: no open trade ideas and no hot hypotheses. "
            "Watch source-health and incoming claims for the next trigger.",
            {"stub": False, "quiet_cycle": True, "model_used": None,
             "provider": None, "fallback_used": False, "chain_position": None},
            0.0,
        )

    _ensure_tic_on_path()
    from tic.desk.models import chat as _chat  # type: ignore

    chain = _build_brief_provider_chain(model, fallback)
    last_error: Optional[str] = None
    for i, m in enumerate(chain):
        try:
            # Disable chat()'s built-in fallback — WE walk the chain.
            result = _run_chat_sync(_chat, m, system, user, fallback=None)
        except Exception as e:  # noqa: BLE001
            last_error = f"{m}: chat_call_failed: {e}"
            continue
        text = (result.get("text") or "").strip()
        err = result.get("error")
        if err:
            last_error = f"{m}: {err}"
            continue
        if not text:
            last_error = f"{m}: empty_completion"
            continue
        # Truncate to ~3 sentences just in case the model overshoots.
        text = _coerce_two_sentences(text)
        meta: dict[str, Any] = {
            "stub": False,
            "quiet_cycle": False,
            "model_used": result.get("model_used") or m,
            "provider": result.get("provider") or m.split(":", 1)[0],
            "fallback_used": (i > 0),
            "chain_position": i,
            "error": None,
        }
        cost = _estimate_cost_usd(meta["model_used"] or m, text)
        return text, meta, cost

    raise BriefHeadlineUnavailableError(
        f"All {len(chain)} providers in the headline fallback chain failed. "
        f"Last error: {last_error}. Chain: {chain}"
    )


def _format_headline_prompt(payload: dict[str, Any]) -> str:
    lines = [
        f"as_of: {payload['as_of']}",
        f"scope: {payload['scope']}",
        f"n_open_ideas: {payload['n_open']}",
        f"n_hot_hypotheses: {payload['n_hot']}",
        "",
    ]
    ti = payload.get("top_idea")
    if ti:
        sizing = ti.get("sizing") or {}
        entry = ti.get("entry") or {}
        lines.append("Top trade idea:")
        lines.append(f"  id: {ti.get('id')}")
        lines.append(f"  instrument: {ti.get('instrument')} {ti.get('direction')}")
        lines.append(f"  confidence: {ti.get('confidence')}")
        lines.append(f"  time_horizon: {ti.get('time_horizon')}")
        lines.append(f"  risk_pct: {sizing.get('risk_pct')}")
        lines.append(f"  entry_trigger: {entry.get('trigger')}")
        lines.append(f"  edge_thesis: {(ti.get('edge_thesis') or '')[:400]}")
        lines.append("")
    h = payload.get("top_hypothesis")
    if h:
        lines.append("Top hot hypothesis:")
        lines.append(f"  id: {h.get('id')}")
        lines.append(f"  title: {h.get('title')}")
        lines.append(f"  posterior: {h.get('posterior_prob')}, heat: {h.get('heat_score')}")
        lines.append(f"  text: {(h.get('hypothesis_text') or '')[:400]}")
        lines.append("")
    d = payload.get("recent_debate")
    if d:
        lines.append("Recent debate verdict:")
        lines.append(f"  trigger_id: {d.get('trigger_id')}")
        lines.append(f"  winner: {d.get('winner')}")
        lines.append(f"  rationale: {(d.get('rationale') or '')[:300]}")
        lines.append("")
    lines.append("Write the headline now. Exactly 2 sentences. No preamble.")
    return "\n".join(lines)


def _deterministic_summary(payload: dict[str, Any]) -> str:
    """Templated fallback when the LLM is unreachable / returns empty."""
    ti = payload.get("top_idea")
    h = payload.get("top_hypothesis")
    if ti:
        sizing = ti.get("sizing") or {}
        risk_bps = (sizing.get("risk_pct") or 0) * 10_000
        thesis = (ti.get("edge_thesis") or "").strip()
        thesis_clip = thesis if len(thesis) <= 180 else thesis[:177] + "..."
        first = (
            f"Top idea: {ti.get('instrument','?')} {ti.get('direction','?')} "
            f"at {ti.get('confidence',0):.2f} confidence, "
            f"{risk_bps:.0f} bps risk over {ti.get('time_horizon','?')}."
        )
        if h:
            second = (
                f"Underlying frame: {h.get('title','(no title)')} "
                f"(posterior {h.get('posterior_prob','?')}). "
                f"Edge thesis: {thesis_clip}"
            )
        else:
            second = f"Edge thesis: {thesis_clip}"
        return f"{first} {second}"
    if h:
        return (
            f"No open trade idea; the desk's hottest hypothesis is "
            f"\"{h.get('title','?')}\" (posterior "
            f"{h.get('posterior_prob','?')}, heat {h.get('heat_score','?')}). "
            f"Watch for evidence that flips it into a trade idea."
        )
    return (
        "Quiet cycle: no open ideas, no hot hypotheses. Watch source health "
        "and incoming claims for the next trigger."
    )


def _coerce_two_sentences(text: str) -> str:
    """Best-effort clamp to at most 3 sentences (the prompt asks for 2,
    but models sometimes add a trailing clause). Avoids splitting on
    periods inside numbers ('2.4σ') or abbreviations — we only treat a
    sentence boundary as period+space+capital, or end-of-line.

    Returns input unchanged when no clear boundary is found."""
    if not text:
        return text
    import re as _re
    # Split on sentence-end punctuation followed by whitespace + capital
    # letter / digit-start, but tolerate a missing space at EOS.
    parts = _re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", text.strip())
    if len(parts) <= 3:
        return " ".join(p.strip() for p in parts if p.strip())
    return " ".join(p.strip() for p in parts[:3] if p.strip())


def _estimate_cost_usd(model: str, response_text: str) -> float:
    """Conservative cost estimate. We assume the response is the dominant
    cost (input is small for headlines) and use the per-1k-token cost
    from `_COST_PER_1K_TOKENS_BY_MODEL`."""
    per_1k = _COST_PER_1K_TOKENS_BY_MODEL.get(model, _DEFAULT_COST_PER_1K)
    # Rough estimate: 4 chars / token. Conservative round up.
    tokens = max(1, len(response_text) // 4)
    # Account for input prompt too — ~2k tokens for the headline prompt.
    tokens += 2000
    return round((tokens / 1000.0) * per_1k, 6)


def _run_chat_sync(
    chat_fn: Any, model: str, system: str, user: str, fallback: str,
) -> dict[str, Any]:
    """Sync wrapper around the async `chat()` from tic.desk.models.

    Same pattern used in `talis_desk/debate/judge.py:_call_judge_llm` — if
    we're already inside an event loop (Jupyter / async caller), drop to a
    worker thread; otherwise just `asyncio.run`."""
    try:
        asyncio.get_running_loop()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(
                asyncio.run,
                chat_fn(model, system, user, fallback=fallback),
            )
            return fut.result(timeout=60)
    except RuntimeError:
        return asyncio.run(chat_fn(model, system, user, fallback=fallback))


# ============================================================================
# TIC artifact persistence
# ============================================================================

def _write_to_tic_artifacts(brief: Brief) -> Optional[str]:
    """Persist brief metadata into TICStore's `artifacts` table.

    Per `wiki/REPO_BOUNDARY.md`, talis-desk has ONE allowed mutation into
    talis-tic: writing artifact/claim rows tagged with
    `source_ref="talis_desk:..."`. We use raw `conn.execute` (bypassing the
    Pydantic `Artifact` model) so we can use an artifact_kind value
    ("daily_brief") that isn't part of the TIC enum yet. The DB column is
    plain TEXT NOT NULL — no constraint violation. A future talis-tic PR
    can promote `daily_brief` into `ArtifactKind`.
    """
    try:
        _ensure_tic_on_path()
        from tic.store import TICStore  # type: ignore
    except Exception as e:  # noqa: BLE001
        logger.info("compose_brief: TICStore unavailable, skipping persist (%s)", e)
        return None

    tic = TICStore()
    art_id = "art_" + uuid.uuid4().hex[:12]
    cycle_id = brief.cycle_id or "ad_hoc"
    source_ref = f"talis_desk:brief:cycle_{cycle_id}"
    window_start = brief.as_of - timedelta(days=CLOSED_BOOK_LOOKBACK_DAYS)
    try:
        with tic.txn() as conn:
            conn.execute(
                "INSERT INTO artifacts ("
                "id, artifact_kind, scope_id, desk_run_id, version, model_id, "
                "window_start, window_end, as_of, prose, claim_ids, "
                "forecast_ids, watch_levels, what_we_dont_have, cross_refs, "
                "validator_report, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    art_id,
                    "daily_brief",
                    brief.scope,
                    brief.cycle_id,
                    "v1",
                    f"{brief.headline_model_used or 'stub'}@composer",
                    window_start.isoformat(),
                    brief.as_of.isoformat(),
                    brief.as_of.isoformat(),
                    brief.markdown,
                    json.dumps(brief.cited_claim_ids),
                    json.dumps([]),  # no forecast_ids surfaced yet
                    json.dumps([]),
                    json.dumps(brief.quality_flags),
                    json.dumps({
                        "brief_id": brief.id,
                        "source_ref": source_ref,
                        "trade_idea_ids": brief.cited_trade_idea_ids,
                        "hypothesis_ids": brief.cited_hypothesis_ids,
                        "debate_ids": brief.cited_debate_ids,
                    }),
                    json.dumps({
                        "cost_usd": brief.cost_usd,
                        "elapsed_seconds": brief.elapsed_seconds,
                        "headline_model": brief.headline_model_used,
                        "headline_provider": brief.headline_provider,
                        "headline_fallback_used": brief.headline_fallback_used,
                    }),
                    brief.transaction_from.isoformat(),
                ),
            )
        return art_id
    except Exception as e:  # noqa: BLE001
        logger.warning("compose_brief: TIC artifact insert failed: %s", e)
        return None


# ============================================================================
# Internal helpers
# ============================================================================

_TIC_SIBLING_PATH = "/Users/udaikhattar/jarvis-ios/docs/research/brief_experiments"


def _ensure_tic_on_path() -> None:
    """Make the sibling talis-tic checkout importable. Same pattern used by
    `talis_desk/debate/judge.py`. The boundary contract still applies: we
    only ever read from `tic.*`, never write to anything but `claims` and
    `artifacts` with `source_ref='talis_desk:...'`."""
    if _TIC_SIBLING_PATH not in sys.path:
        sys.path.insert(0, _TIC_SIBLING_PATH)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _short_date(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")


def _json_load(v: Any) -> Any:
    """SQLite returns JSON blobs as strings. None / non-string passes
    through unchanged."""
    if v is None or not isinstance(v, str):
        return v
    try:
        return json.loads(v)
    except Exception:
        return v

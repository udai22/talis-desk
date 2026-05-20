"""Trade Idea Resolver — daily cron + at-expiry grading.

Per wiki/SOTA_DESK_ARCHITECTURE.md v2 §2 Layer 4 (lines 109-137) and §7
(lines 134-136). This is the PnL gate that turns the flywheel:

  emit -> trade book open -> resolver fills realized_pnl_pct + benchmark +
  alpha + brier -> reward_log -> persona/tool affinity updates -> next cycle.

# What the resolver does
  1. Find ideas due for resolution: status in ('published','open') AND
     expires_at <= as_of.
  2. For each idea, pull HL candles for [published_at, min(expires_at, as_of)],
     find MFE / MAE / final price, detect stop/target hits.
  3. Compute realized_pnl_pct (signed by direction), subtract fees + slippage
     for realized_return_after_fees_pct.
  4. Call benchmark.compute_benchmark_return for the same window.
  5. Compute Brier vs the idea's implied directional probability
     (max(confidence, 0.5) on the chosen side; 1-confidence on the other).
  6. Append-only write: insert a new trade_ideas row with realized_* fields
     filled, status='closed' (or 'expired'/'invalidated'), supersedes=old.id,
     and set the old row's transaction_to.
  7. Write a reward_log row with reward_kind='alpha', subject_kind='trade_idea',
     score=realized_return_after_fees_pct.

# Idempotency
Re-resolving an already-closed idea is a no-op: the loop only considers rows
where status in ('published','open'). The resolver_run_id is recorded on the
new row so we can audit the pass that closed it.

# Bitemporal replay
Replay at as_of_valid=published_at returns the original 'published' row
because its valid_from <= that timestamp AND its transaction_to is None (or
> as_of_valid for the historical knowledge axis). The closed row's valid_from
is the close timestamp, so it's invisible at the published-time slice.

# Honest gaps
  - Fees model is constant 4 bps taker + 1 bps slippage per side (8/10 bps
    round trip); real HL fees are tiered and the dashboard should flag this.
  - 'flat' / 'spread' directions don't have a meaningful Brier yet; we set
    Brier to None and record realized_pnl_pct=0 for 'flat'. Spread requires
    multi-leg pricing.
  - Brier is computed on the directional outcome bucket (win vs loss net of
    costs), not against a continuous return — a more honest formulation is to
    use the calibration of the *probability* against the realized binary.
    Acceptable for v0.
  - Alpha attribution is "simple"-method by default: split realized alpha by
    citation count weight. `shapley-lite` and `ablation` need full cycle
    replay (Phase 6 Research Director).
"""
from __future__ import annotations

import json
import sqlite3
import uuid
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional

from .benchmark import compute_benchmark_return, BenchmarkResult


# ============================================================================
# Constants — keep here so dashboards can read them without re-deriving.
# ============================================================================

#: Round-trip costs in basis points (10 bps = 0.10%). 8 bps taker + 2 bps
#: average slippage = 10 bps round trip. Conservative enough to bias us
#: toward NOT declaring alpha when there isn't any.
HL_FEES_BPS_ROUND_TRIP = 10.0

#: Map from idea.entry.market_assumption to extra slippage in bps each side.
#: Layered on top of the base HL_FEES_BPS_ROUND_TRIP.
SLIPPAGE_EXTRA_BPS = {
    "tight_ladder": 0.0,
    "liquid": 2.0,
    "thin_book": 8.0,
    "illiquid": 25.0,
}

#: Hard ceiling on resolve window — if expires_at is more than this far past
#: published_at, resolver chunks it. v0 just caps the candle fetch.
MAX_RESOLVE_WINDOW_HOURS = 24 * 35


# ============================================================================
# Return envelopes
# ============================================================================

@dataclass
class TradeIdeaOutcome:
    """Outcome for one resolved idea."""
    idea_id: str
    new_idea_id: str               # the supersedes row created by resolver
    status: str                    # closed | expired | invalidated
    realized_outcome: dict
    realized_pnl_pct: Optional[float]
    realized_return_after_fees_pct: Optional[float]
    benchmark_return_pct: Optional[float]
    contributed_alpha_pct: Optional[float]
    brier: Optional[float]
    resolver_run_id: str
    notes: list[str] = field(default_factory=list)


@dataclass
class ResolverRunReport:
    """Aggregate result of `resolve_all_due`."""
    run_id: str
    started_at: datetime
    finished_at: datetime
    n_due: int
    n_resolved: int
    n_skipped: int
    n_errors: int
    outcomes: list[TradeIdeaOutcome] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)


@dataclass
class TradeBookMetrics:
    """Aggregate trade-book metrics for the rolling window (v2 §5)."""
    window_days: int
    as_of: datetime
    n_closed: int
    hit_rate: Optional[float]
    avg_return_after_fees_pct: Optional[float]
    avg_alpha_pct: Optional[float]
    sharpe: Optional[float]
    brier_avg: Optional[float]
    biggest_loss_pct: Optional[float]
    biggest_loss_idea_id: Optional[str]
    best_alpha_pct: Optional[float]
    best_alpha_idea_id: Optional[str]
    worst_alpha_pct: Optional[float]
    worst_alpha_idea_id: Optional[str]


@dataclass
class AlphaAttribution:
    """Output of `attribute_alpha`."""
    idea_id: str
    method: str
    total_alpha_pct: float
    components: list[dict[str, Any]]
    notes: list[str] = field(default_factory=list)


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


def _row_to_idea_dict(row: sqlite3.Row) -> dict[str, Any]:
    """Inflate a trade_ideas row, decoding the JSON-as-TEXT columns."""
    d = dict(row)
    for col in ("sizing", "entry", "stop", "target", "claim_ids",
                "contradicting_evidence", "hypothesis_ids", "forecast_ids",
                "debate_ids", "tool_call_ids", "realized_outcome", "payload"):
        v = d.get(col)
        if isinstance(v, str) and v:
            try:
                d[col] = json.loads(v)
            except Exception:
                pass
    return d


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


# ============================================================================
# Price-trajectory helpers
# ============================================================================

def _fetch_price_path(coin: str, start: datetime, end: datetime) -> dict[str, Any]:
    """Return {entry_px, exit_px, max_px, min_px, n_candles, candles, error?}.

    Uses HL candleSnapshot with an interval chosen to keep the result small.
    """
    import sys as _sys
    sibling = "/Users/udaikhattar/jarvis-ios/docs/research/brief_experiments"
    if sibling not in _sys.path:
        _sys.path.insert(0, sibling)
    try:
        from tic.desk.tools.hl_history_tools import get_hl_candles
    except Exception as e:
        return {"error": f"import_failed: {e}"}

    start = start if start.tzinfo else start.replace(tzinfo=timezone.utc)
    end = end if end.tzinfo else end.replace(tzinfo=timezone.utc)
    if end <= start:
        return {"error": "end_before_start"}
    delta_hours = (end - start).total_seconds() / 3600.0
    delta_hours = min(delta_hours, MAX_RESOLVE_WINDOW_HOURS)
    if delta_hours <= 24:
        interval = "5m"
    elif delta_hours <= 72:
        interval = "15m"
    elif delta_hours <= 7 * 24:
        interval = "1h"
    else:
        interval = "4h"
    lookback_hours = max(1, int(delta_hours))

    res = get_hl_candles(
        coin=coin,
        interval=interval,
        lookback_hours=lookback_hours,
        end_time_iso=end.isoformat(),
    )
    if "error" in res:
        return {"error": res["error"]}

    candles = res.get("candles") or []
    # Filter to the actual [start, end] window (HL returns whole bars touching range)
    start_ms = start.timestamp() * 1000.0
    end_ms = end.timestamp() * 1000.0
    kept = []
    for c in candles:
        ts = c.get("ts")
        if not ts:
            continue
        try:
            cdt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            continue
        cms = cdt.timestamp() * 1000.0
        if start_ms <= cms <= end_ms:
            kept.append(c)
    if not kept:
        # Fallback: use the original candles list if our window filter dropped everything
        kept = candles
    if not kept:
        return {"error": "no_candles_in_window"}

    entry_px = float(kept[0]["close"])
    exit_px = float(kept[-1]["close"])
    max_px = max(float(c["high"]) for c in kept)
    min_px = min(float(c["low"]) for c in kept)
    return {
        "entry_px": entry_px,
        "exit_px": exit_px,
        "max_px": max_px,
        "min_px": min_px,
        "n_candles": len(kept),
        "interval": interval,
        "start_ts": kept[0]["ts"],
        "end_ts": kept[-1]["ts"],
    }


def _detect_stop_target_hits(
    direction: str,
    entry_px: float,
    stop_px: Optional[float],
    target_px: Optional[float],
    max_px: float,
    min_px: float,
) -> tuple[bool, bool]:
    """Return (stop_hit, target_hit) for a long/short directional idea."""
    stop_hit = False
    target_hit = False
    if direction == "long":
        if stop_px is not None and min_px <= stop_px:
            stop_hit = True
        if target_px is not None and max_px >= target_px:
            target_hit = True
    elif direction == "short":
        if stop_px is not None and max_px >= stop_px:
            stop_hit = True
        if target_px is not None and min_px <= target_px:
            target_hit = True
    return stop_hit, target_hit


# ============================================================================
# Core resolver
# ============================================================================

def resolve_trade_idea(
    idea_id: str,
    as_of: Optional[datetime] = None,
    *,
    conn: Optional[sqlite3.Connection] = None,
    resolver_run_id: Optional[str] = None,
) -> TradeIdeaOutcome:
    """Resolve one trade idea. Idempotent: if the idea is already closed/
    expired/invalidated, returns its existing outcome without writing.

    Args:
      idea_id: id of the trade_ideas row to resolve.
      as_of: when "now" is for this resolver pass; defaults to current UTC.
      conn: optional override; uses singleton desk.db otherwise.
      resolver_run_id: tag for this resolver pass; auto-generated if None.
    """
    conn = _resolve_conn(conn)
    as_of = as_of or datetime.now(timezone.utc)
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    run_id = resolver_run_id or f"rrun_{uuid.uuid4().hex[:12]}"
    notes: list[str] = []

    # Follow the supersedes chain. If a later row supersedes `idea_id`, that
    # row is the terminal view of this idea — and may already be closed.
    chain_head = conn.execute(
        "SELECT * FROM trade_ideas "
        "WHERE supersedes = ? AND transaction_to IS NULL "
        "ORDER BY transaction_from DESC LIMIT 1",
        (idea_id,),
    ).fetchone()
    if chain_head is None:
        # No supersedes yet — look for the original row (live then historical)
        chain_head = conn.execute(
            "SELECT * FROM trade_ideas "
            "WHERE id = ? AND transaction_to IS NULL "
            "ORDER BY transaction_from DESC LIMIT 1",
            (idea_id,),
        ).fetchone()
        if chain_head is None:
            chain_head = conn.execute(
                "SELECT * FROM trade_ideas WHERE id = ? "
                "ORDER BY transaction_from DESC LIMIT 1",
                (idea_id,),
            ).fetchone()
        if chain_head is None:
            raise KeyError(f"trade_idea_not_found: {idea_id}")

    idea = _row_to_idea_dict(chain_head)

    # Idempotency: if already terminal, return existing outcome
    if idea["status"] in ("closed", "expired", "invalidated"):
        notes.append(f"already_terminal_status={idea['status']}; idempotent_no_op")
        return TradeIdeaOutcome(
            idea_id=idea_id,
            new_idea_id=idea["id"],
            status=idea["status"],
            realized_outcome=idea.get("realized_outcome") or {},
            realized_pnl_pct=idea.get("realized_pnl_pct"),
            realized_return_after_fees_pct=idea.get("realized_return_after_fees_pct"),
            benchmark_return_pct=idea.get("benchmark_return_pct"),
            contributed_alpha_pct=idea.get("contributed_alpha_pct"),
            brier=idea.get("brier"),
            resolver_run_id=idea.get("resolver_run_id") or "(prior)",
            notes=notes,
        )

    # Determine window: [published_at, min(expires_at, as_of)]
    published_at = _parse_iso(idea.get("published_at")) or _parse_iso(idea.get("valid_from"))
    if published_at is None:
        raise ValueError(f"resolve: idea {idea_id} missing published_at/valid_from")
    expires_at = _parse_iso(idea.get("expires_at"))
    if expires_at is None:
        raise ValueError(f"resolve: idea {idea_id} missing expires_at")
    window_end = min(expires_at, as_of)
    if window_end <= published_at:
        notes.append(f"window_end_before_published_at; window_end={window_end} published_at={published_at}")
        window_end = published_at + timedelta(minutes=5)  # tiny dummy window

    instrument = idea["instrument"]
    coin = _coin_from_instrument(instrument)
    direction = idea["direction"]
    entry_obj = idea.get("entry") or {}
    stop_obj = idea.get("stop") or {}
    target_obj = idea.get("target") or {}

    limit_px = entry_obj.get("limit_px")
    stop_px = stop_obj.get("px")
    target_px = (target_obj.get("px") if isinstance(target_obj, dict) else None)
    market_assumption = entry_obj.get("market_assumption", "liquid")
    extra_bps = SLIPPAGE_EXTRA_BPS.get(market_assumption, 5.0)
    total_fees_bps = HL_FEES_BPS_ROUND_TRIP + (extra_bps * 2.0)  # both sides

    # Pull the price path
    path = _fetch_price_path(coin, published_at, window_end)
    if "error" in path:
        notes.append(f"price_path_error:{path['error']}")
        # Treat as invalidated — couldn't fetch outcome
        return _write_terminal_row(
            conn=conn, idea=idea, new_status="invalidated",
            realized_outcome={"error": path["error"]},
            realized_pnl_pct=None, realized_return_after_fees_pct=None,
            benchmark_return_pct=None, contributed_alpha_pct=None,
            brier=None, resolver_run_id=run_id,
            window_start=published_at, window_end=window_end, notes=notes,
        )

    entry_px = float(limit_px) if limit_px is not None else float(path["entry_px"])
    exit_px = float(path["exit_px"])
    max_px = float(path["max_px"])
    min_px = float(path["min_px"])

    # Stop / target detection
    stop_hit, target_hit = _detect_stop_target_hits(
        direction=direction, entry_px=entry_px,
        stop_px=stop_px, target_px=target_px,
        max_px=max_px, min_px=min_px,
    )
    # Realized exit: stop/target overrides final close price
    realized_exit_px = exit_px
    exit_reason = "time_expired"
    if stop_hit and target_hit:
        # Both touched intra-window — conservative: assume stop fired first.
        realized_exit_px = float(stop_px)
        exit_reason = "stop"
    elif stop_hit:
        realized_exit_px = float(stop_px)
        exit_reason = "stop"
    elif target_hit:
        realized_exit_px = float(target_px)
        exit_reason = "target"

    # Compute pnl
    if direction == "long":
        pnl_pct_raw = (realized_exit_px / entry_px - 1.0) * 100.0
        mfe_pct = (max_px / entry_px - 1.0) * 100.0
        mae_pct = (min_px / entry_px - 1.0) * 100.0
    elif direction == "short":
        pnl_pct_raw = (entry_px / realized_exit_px - 1.0) * 100.0
        mfe_pct = (entry_px / min_px - 1.0) * 100.0
        mae_pct = (entry_px / max_px - 1.0) * 100.0
    elif direction == "flat":
        pnl_pct_raw = 0.0
        mfe_pct = 0.0
        mae_pct = 0.0
        notes.append("direction=flat: realized_pnl_pct fixed to 0")
    elif direction == "spread":
        # Phase-3 stub: spread = absolute deviation from entry (cheap proxy).
        pnl_pct_raw = -abs(exit_px / entry_px - 1.0) * 100.0
        mfe_pct = -abs(max_px / entry_px - 1.0) * 100.0
        mae_pct = -abs(min_px / entry_px - 1.0) * 100.0
        notes.append("direction=spread: using abs-deviation proxy; multi-leg pricing pending")
    else:
        pnl_pct_raw = 0.0
        mfe_pct = 0.0
        mae_pct = 0.0
        notes.append(f"unknown_direction:{direction}; pnl=0")

    fees_pct = total_fees_bps / 100.0  # bps -> pct
    realized_return_after_fees_pct = pnl_pct_raw - fees_pct

    # Benchmark
    bench: BenchmarkResult = compute_benchmark_return(
        window_start=published_at,
        window_end=window_end,
        kind="hl_top10",
    )
    benchmark_return_pct = bench.return_pct
    alpha_pct: Optional[float] = None
    if benchmark_return_pct is not None:
        alpha_pct = realized_return_after_fees_pct - benchmark_return_pct

    # Brier: binary win-vs-loss against the implied probability.
    brier = _compute_brier(idea, realized_return_after_fees_pct, direction)

    # Decide final status
    if exit_reason in ("stop", "target"):
        new_status = "closed"
    elif window_end >= expires_at:
        new_status = "expired"
    elif as_of >= expires_at:
        new_status = "expired"
    else:
        new_status = "closed"

    realized_outcome = {
        "entry_px": entry_px,
        "exit_px": realized_exit_px,
        "max_px": max_px,
        "min_px": min_px,
        "mfe_pct": mfe_pct,
        "mae_pct": mae_pct,
        "stop_hit": stop_hit,
        "target_hit": target_hit,
        "exit_reason": exit_reason,
        "fees_bps_round_trip": total_fees_bps,
        "n_candles": path["n_candles"],
        "interval": path["interval"],
        "window_start": _iso(published_at),
        "window_end": _iso(window_end),
        "benchmark": {
            "kind": bench.kind,
            "return_pct": benchmark_return_pct,
            "constituents_used": [c["coin"] for c in bench.constituents
                                  if c.get("return_pct") is not None],
        },
    }

    outcome = _write_terminal_row(
        conn=conn, idea=idea, new_status=new_status,
        realized_outcome=realized_outcome,
        realized_pnl_pct=pnl_pct_raw,
        realized_return_after_fees_pct=realized_return_after_fees_pct,
        benchmark_return_pct=benchmark_return_pct,
        contributed_alpha_pct=alpha_pct,
        brier=brier, resolver_run_id=run_id,
        window_start=published_at, window_end=window_end, notes=notes,
    )

    # reward_log row (alpha attribution at the per-idea level)
    if alpha_pct is not None:
        _write_reward_log(
            conn=conn, idea=idea, alpha_pct=alpha_pct,
            realized_return_after_fees_pct=realized_return_after_fees_pct,
            brier=brier, resolver_run_id=run_id,
        )

    return outcome


def _coin_from_instrument(instrument: str) -> str:
    """Map an instrument symbol (e.g. 'BTC-USD' or 'HYPE') to the HL coin slug."""
    if not instrument:
        return ""
    s = instrument.strip().upper()
    # Drop common suffixes
    for suffix in ("-USD", "-PERP", "/USD", "_USD", "USD", "-USDC"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
            break
    return s.strip("-_/").strip()


def _compute_brier(
    idea: dict[str, Any],
    realized_return_after_fees_pct: float,
    direction: str,
) -> Optional[float]:
    """Brier score against the directional win/loss bucket.

    The idea declares a `confidence` in [0,1]. For a directional idea, we
    interpret it as "probability the position is profitable after fees". The
    outcome y is 1 if realized_return_after_fees_pct > 0, else 0. Brier =
    (confidence - y)^2.

    For 'flat' or 'spread' directions, Brier is not meaningful — return None.
    """
    if direction not in ("long", "short"):
        return None
    try:
        p = float(idea.get("confidence", 0.5))
    except Exception:
        return None
    p = max(0.0, min(1.0, p))
    y = 1.0 if realized_return_after_fees_pct > 0 else 0.0
    return (p - y) ** 2


def _write_terminal_row(
    conn: sqlite3.Connection,
    idea: dict[str, Any],
    new_status: str,
    realized_outcome: dict[str, Any],
    realized_pnl_pct: Optional[float],
    realized_return_after_fees_pct: Optional[float],
    benchmark_return_pct: Optional[float],
    contributed_alpha_pct: Optional[float],
    brier: Optional[float],
    resolver_run_id: str,
    window_start: datetime,
    window_end: datetime,
    notes: list[str],
) -> TradeIdeaOutcome:
    """Insert a supersedes row with realized_* filled, close the old row's
    transaction_to. Append-only — original row is preserved for replay.
    """
    now_dt = datetime.now(timezone.utc)
    now_iso = _iso(now_dt)
    new_id = "ti_" + uuid.uuid4().hex[:12]

    # Close old row's transaction_to
    conn.execute(
        "UPDATE trade_ideas SET transaction_to = ? "
        "WHERE id = ? AND transaction_to IS NULL",
        (now_iso, idea["id"]),
    )

    # Re-encode embedded JSON columns to TEXT
    def _to_json_text(v: Any) -> Optional[str]:
        if v is None:
            return None
        if isinstance(v, str):
            return v
        return _canonical_json(v)

    conn.execute(
        "INSERT INTO trade_ideas ("
        "id, cycle_id, specialist_id, instrument, venue, direction, "
        "sizing, entry, stop, target, time_horizon, edge_thesis, "
        "claim_ids, contradicting_evidence, confluence_score, confidence, "
        "hypothesis_ids, forecast_ids, debate_ids, playbook_id, "
        "tool_call_ids, status, published_at, expires_at, "
        "realized_outcome, realized_pnl_pct, realized_return_after_fees_pct, "
        "benchmark_return_pct, contributed_alpha_pct, brier, resolver_run_id, "
        "supersedes, valid_from, transaction_from, payload"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            new_id, idea["cycle_id"], idea["specialist_id"], idea["instrument"],
            idea["venue"], idea["direction"],
            _to_json_text(idea.get("sizing")),
            _to_json_text(idea.get("entry")),
            _to_json_text(idea.get("stop")),
            _to_json_text(idea.get("target")),
            idea["time_horizon"], idea["edge_thesis"],
            _to_json_text(idea.get("claim_ids") or []),
            _to_json_text(idea.get("contradicting_evidence") or []),
            idea.get("confluence_score"), idea["confidence"],
            _to_json_text(idea.get("hypothesis_ids") or []),
            _to_json_text(idea.get("forecast_ids") or []),
            _to_json_text(idea.get("debate_ids") or []),
            idea.get("playbook_id"),
            _to_json_text(idea.get("tool_call_ids") or []),
            new_status,
            idea.get("published_at"),
            idea.get("expires_at"),
            _to_json_text(realized_outcome),
            realized_pnl_pct,
            realized_return_after_fees_pct,
            benchmark_return_pct,
            contributed_alpha_pct,
            brier,
            resolver_run_id,
            idea["id"],  # supersedes
            now_iso,    # valid_from = resolution time
            now_iso,    # transaction_from
            _to_json_text({**(idea.get("payload") or {}),
                            "resolver_notes": notes}),
        ),
    )

    return TradeIdeaOutcome(
        idea_id=idea["id"],
        new_idea_id=new_id,
        status=new_status,
        realized_outcome=realized_outcome,
        realized_pnl_pct=realized_pnl_pct,
        realized_return_after_fees_pct=realized_return_after_fees_pct,
        benchmark_return_pct=benchmark_return_pct,
        contributed_alpha_pct=contributed_alpha_pct,
        brier=brier,
        resolver_run_id=resolver_run_id,
        notes=notes,
    )


def _write_reward_log(
    conn: sqlite3.Connection,
    idea: dict[str, Any],
    alpha_pct: float,
    realized_return_after_fees_pct: float,
    brier: Optional[float],
    resolver_run_id: str,
) -> None:
    """Append one reward_log row: reward_kind='alpha', score=alpha_pct,
    subject_kind='trade_idea', subject_id=idea.id.

    Also drops a Brier 'correctness' row so persona evolution sees the signal.
    """
    now_iso = _iso(datetime.now(timezone.utc))
    rew_id = "rew_" + uuid.uuid4().hex[:12]
    try:
        conn.execute(
            "INSERT INTO reward_log ("
            "id, cycle_id, reward_kind, subject_kind, subject_id, "
            "specialist_id, score, baseline_score, delta, attribution_json, "
            "valid_from, transaction_from"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rew_id, idea.get("cycle_id") or "(unknown)",
                "alpha", "trade_idea", idea["id"],
                idea.get("specialist_id"),
                alpha_pct,
                0.0,  # baseline = passive (alpha is already baseline-relative)
                alpha_pct,
                _canonical_json({
                    "realized_return_after_fees_pct": realized_return_after_fees_pct,
                    "resolver_run_id": resolver_run_id,
                }),
                now_iso, now_iso,
            ),
        )
        if brier is not None:
            rew_id2 = "rew_" + uuid.uuid4().hex[:12]
            conn.execute(
                "INSERT INTO reward_log ("
                "id, cycle_id, reward_kind, subject_kind, subject_id, "
                "specialist_id, score, attribution_json, "
                "valid_from, transaction_from"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    rew_id2, idea.get("cycle_id") or "(unknown)",
                    "correctness", "trade_idea", idea["id"],
                    idea.get("specialist_id"),
                    brier,
                    _canonical_json({"resolver_run_id": resolver_run_id}),
                    now_iso, now_iso,
                ),
            )
    except Exception as e:  # pragma: no cover - best-effort
        warnings.warn(f"reward_log insert failed: {e}")


# ============================================================================
# Cron pass
# ============================================================================

def resolve_all_due(
    as_of: Optional[datetime] = None,
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> ResolverRunReport:
    """Find every idea that should be resolved at `as_of` and resolve it.

    Selection criterion:
      status IN ('published','open') AND expires_at <= as_of
      AND transaction_to IS NULL
    """
    conn = _resolve_conn(conn)
    as_of = as_of or datetime.now(timezone.utc)
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    started_at = datetime.now(timezone.utc)
    run_id = f"rrun_{uuid.uuid4().hex[:12]}"

    rows = conn.execute(
        "SELECT id FROM trade_ideas "
        "WHERE status IN ('published','open') "
        "AND transaction_to IS NULL "
        "AND expires_at <= ? "
        "ORDER BY expires_at ASC",
        (_iso(as_of),),
    ).fetchall()
    ids = [r["id"] for r in rows]
    n_due = len(ids)

    outcomes: list[TradeIdeaOutcome] = []
    errors: list[dict[str, str]] = []
    for idea_id in ids:
        try:
            o = resolve_trade_idea(
                idea_id, as_of=as_of, conn=conn, resolver_run_id=run_id,
            )
            outcomes.append(o)
        except Exception as e:
            errors.append({"idea_id": idea_id, "error": f"{type(e).__name__}: {e}"})

    return ResolverRunReport(
        run_id=run_id,
        started_at=started_at,
        finished_at=datetime.now(timezone.utc),
        n_due=n_due,
        n_resolved=len(outcomes),
        n_skipped=0,
        n_errors=len(errors),
        outcomes=outcomes,
        errors=errors,
    )


# ============================================================================
# Trade-book metrics (v2 §5 "Trade book" panel)
# ============================================================================

def compute_trade_book_metrics(
    window_days: int = 30,
    *,
    conn: Optional[sqlite3.Connection] = None,
    as_of: Optional[datetime] = None,
) -> TradeBookMetrics:
    """Aggregate Brier/hit rate/Sharpe/alpha for the rolling window."""
    conn = _resolve_conn(conn)
    as_of = as_of or datetime.now(timezone.utc)
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    start = as_of - timedelta(days=window_days)

    rows = conn.execute(
        "SELECT id, realized_pnl_pct, realized_return_after_fees_pct, "
        "       benchmark_return_pct, contributed_alpha_pct, brier, "
        "       valid_from, status "
        "FROM trade_ideas "
        "WHERE status IN ('closed','expired','invalidated') "
        "AND transaction_to IS NULL "
        "AND valid_from >= ?",
        (_iso(start),),
    ).fetchall()

    if not rows:
        return TradeBookMetrics(
            window_days=window_days, as_of=as_of, n_closed=0,
            hit_rate=None, avg_return_after_fees_pct=None,
            avg_alpha_pct=None, sharpe=None, brier_avg=None,
            biggest_loss_pct=None, biggest_loss_idea_id=None,
            best_alpha_pct=None, best_alpha_idea_id=None,
            worst_alpha_pct=None, worst_alpha_idea_id=None,
        )

    after_fees = [r["realized_return_after_fees_pct"] for r in rows
                  if r["realized_return_after_fees_pct"] is not None]
    alphas = [(r["id"], r["contributed_alpha_pct"]) for r in rows
              if r["contributed_alpha_pct"] is not None]
    briers = [r["brier"] for r in rows if r["brier"] is not None]
    pnls = [(r["id"], r["realized_return_after_fees_pct"]) for r in rows
            if r["realized_return_after_fees_pct"] is not None]

    hit_rate = (sum(1 for x in after_fees if x > 0) / len(after_fees)
                if after_fees else None)
    avg_aft = (sum(after_fees) / len(after_fees)) if after_fees else None
    avg_alpha = (sum(a for _, a in alphas) / len(alphas)) if alphas else None
    brier_avg = (sum(briers) / len(briers)) if briers else None

    # Sharpe-ish: mean / std of after-fee returns (window-level, not annualized)
    sharpe: Optional[float] = None
    if after_fees and len(after_fees) >= 2:
        mu = sum(after_fees) / len(after_fees)
        var = sum((x - mu) ** 2 for x in after_fees) / (len(after_fees) - 1)
        sd = var ** 0.5
        sharpe = (mu / sd) if sd > 1e-9 else None

    biggest_loss_idea_id: Optional[str] = None
    biggest_loss_pct: Optional[float] = None
    if pnls:
        worst = min(pnls, key=lambda x: x[1])
        biggest_loss_idea_id, biggest_loss_pct = worst[0], worst[1]

    best_alpha_idea_id: Optional[str] = None
    best_alpha_pct: Optional[float] = None
    worst_alpha_idea_id: Optional[str] = None
    worst_alpha_pct: Optional[float] = None
    if alphas:
        best_idx = max(alphas, key=lambda x: x[1])
        worst_idx = min(alphas, key=lambda x: x[1])
        best_alpha_idea_id, best_alpha_pct = best_idx[0], best_idx[1]
        worst_alpha_idea_id, worst_alpha_pct = worst_idx[0], worst_idx[1]

    return TradeBookMetrics(
        window_days=window_days, as_of=as_of, n_closed=len(rows),
        hit_rate=hit_rate, avg_return_after_fees_pct=avg_aft,
        avg_alpha_pct=avg_alpha, sharpe=sharpe, brier_avg=brier_avg,
        biggest_loss_pct=biggest_loss_pct,
        biggest_loss_idea_id=biggest_loss_idea_id,
        best_alpha_pct=best_alpha_pct,
        best_alpha_idea_id=best_alpha_idea_id,
        worst_alpha_pct=worst_alpha_pct,
        worst_alpha_idea_id=worst_alpha_idea_id,
    )


# ============================================================================
# Alpha attribution
# ============================================================================

def attribute_alpha(
    idea_id: str,
    method: Literal["simple", "shapley-lite", "ablation"] = "simple",
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> AlphaAttribution:
    """Split the realized alpha across the cited specialist/tool/playbook/etc.

    Methods:
      - simple        : equal share among cited claim/hypothesis/tool/playbook
                        ids (the LLM's stated credits).
      - shapley-lite  : weight by citation count + edge_thesis mention freq.
      - ablation      : (not yet) requires Phase 6 Research Director cycle
                        replay; raises NotImplementedError for now.
    """
    conn = _resolve_conn(conn)
    # We resolve against the most recent (terminal) row for idea_id; the
    # supersedes new_id row carries the realized fields. We need the trail.
    row = conn.execute(
        "SELECT id, supersedes, specialist_id, claim_ids, hypothesis_ids, "
        "       tool_call_ids, playbook_id, contributed_alpha_pct "
        "FROM trade_ideas "
        "WHERE (id = ? OR supersedes = ?) "
        "AND transaction_to IS NULL "
        "ORDER BY transaction_from DESC LIMIT 1",
        (idea_id, idea_id),
    ).fetchone()
    if row is None:
        raise KeyError(f"attribute_alpha: idea {idea_id!r} not found or not yet resolved")
    d = dict(row)
    alpha = d.get("contributed_alpha_pct")
    if alpha is None:
        return AlphaAttribution(
            idea_id=idea_id, method=method, total_alpha_pct=0.0,
            components=[],
            notes=["no contributed_alpha_pct on this idea (not resolved or no benchmark)"],
        )

    if method == "ablation":
        raise NotImplementedError(
            "ablation attribution requires Phase 6 Research Director cycle replay"
        )

    def _json_list(v: Any) -> list[str]:
        if v is None:
            return []
        if isinstance(v, list):
            return [str(x) for x in v]
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
                return [str(x) for x in (parsed or [])]
            except Exception:
                return []
        return []

    citations: list[tuple[str, str]] = []  # (kind, id)
    for cid in _json_list(d.get("claim_ids")):
        citations.append(("claim", cid))
    for hid in _json_list(d.get("hypothesis_ids")):
        citations.append(("hypothesis", hid))
    for tid in _json_list(d.get("tool_call_ids")):
        citations.append(("tool_call", tid))
    if d.get("playbook_id"):
        citations.append(("playbook", str(d["playbook_id"])))
    # Specialist always gets a share too
    if d.get("specialist_id"):
        citations.append(("specialist", str(d["specialist_id"])))

    components: list[dict[str, Any]] = []
    notes: list[str] = []
    if not citations:
        components.append({
            "kind": "specialist", "id": d.get("specialist_id") or "(unknown)",
            "weight": 1.0, "alpha_pct": float(alpha),
        })
        notes.append("no citations on idea; full alpha attributed to specialist")
        return AlphaAttribution(
            idea_id=idea_id, method=method, total_alpha_pct=float(alpha),
            components=components, notes=notes,
        )

    if method == "simple":
        share = 1.0 / len(citations)
        for kind, cid in citations:
            components.append({
                "kind": kind, "id": cid,
                "weight": share, "alpha_pct": float(alpha) * share,
            })
        return AlphaAttribution(
            idea_id=idea_id, method=method, total_alpha_pct=float(alpha),
            components=components, notes=notes,
        )

    # shapley-lite: count occurrences within citation list; specialist double-credit.
    weights: dict[tuple[str, str], float] = {}
    for kind, cid in citations:
        w = weights.get((kind, cid), 0.0) + 1.0
        if kind == "specialist":
            w += 1.0  # the "owner" bonus
        weights[(kind, cid)] = w
    tot = sum(weights.values()) or 1.0
    for (kind, cid), w in weights.items():
        share = w / tot
        components.append({
            "kind": kind, "id": cid,
            "weight": share, "alpha_pct": float(alpha) * share,
        })
    notes.append("shapley-lite: declared-citation weights only; "
                 "ablation replay pending Phase 6")
    return AlphaAttribution(
        idea_id=idea_id, method=method, total_alpha_pct=float(alpha),
        components=components, notes=notes,
    )

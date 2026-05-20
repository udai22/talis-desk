"""HL passive benchmark for alpha attribution.

Per v2 Section 1 (line 17) the desk's primary metric is "Weekly alpha vs HL
benchmark > 50 bps after fees". The benchmark is two options:

  - `hl_top10`   : equal-weight hold of the top-10 HL perp coins by interest
                   over the window. Approximates "passive HL beta".
  - `btc`        : passive BTC hold (cheapest, conservative default).

The HL Info API exposes per-coin candleSnapshot; we average the window-returns.

# Honest gaps
  - "Top-10 by interest" is hard to backfill without a snapshot; for now we
    take a static list of `HL_TOP10_DEFAULT` perps (BTC, ETH, SOL, HYPE,
    DOGE, ARB, AVAX, OP, MATIC, INJ). Fix in Phase 6 when we have an MV that
    tracks monthly volume rankings.
  - When a constituent's window data is unavailable (e.g. delisted in mid-
    window), we drop it from the average — this biases the benchmark toward
    survivors. Acceptable for v0; document so the dashboard can flag it.
  - The "right" benchmark long-term may be Coinglass perp index; we'll add it
    when that source lands in talis-tic.
"""
from __future__ import annotations

import math
import sys
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Optional


# Top-10 HL perps by recent interest. Static for v0; see honest gaps above.
HL_TOP10_DEFAULT: tuple[str, ...] = (
    "BTC", "ETH", "SOL", "HYPE", "DOGE", "ARB", "AVAX", "OP", "MATIC", "INJ",
)


@dataclass
class BenchmarkResult:
    """Outcome of `compute_benchmark_return`."""
    kind: str
    window_start: datetime
    window_end: datetime
    return_pct: Optional[float]      # net pct (not basis points; multiply *100 already done)
    constituents: list[dict]         # per-coin returns
    notes: list[str]


def _ensure_tic_on_path() -> None:
    """Make `tic.desk.tools.hl_history_tools` importable.

    Codex finding #16: path resolution is centralized in
    `talis_desk._tic_config`. Guarded so we don't shadow a real
    installation if/when one shows up.
    """
    try:
        import tic.desk.tools.hl_history_tools  # noqa: F401
        return
    except ImportError:
        pass
    from .._tic_config import ensure_tic_on_path as _impl
    _impl()


def _coin_return_pct(coin: str, start: datetime, end: datetime) -> Optional[float]:
    """Compute coin's percent return in `[start, end]` via HL candleSnapshot.

    Returns None if data is unavailable, so the caller can drop the
    constituent rather than poison the average.
    """
    _ensure_tic_on_path()
    try:
        from tic.desk.tools.hl_history_tools import get_hl_candles
    except Exception as e:  # pragma: no cover
        warnings.warn(f"benchmark: cannot import get_hl_candles: {e}")
        return None

    end_utc = end if end.tzinfo else end.replace(tzinfo=timezone.utc)
    start_utc = start if start.tzinfo else start.replace(tzinfo=timezone.utc)
    delta = end_utc - start_utc
    lookback_hours = max(1, int(math.ceil(delta.total_seconds() / 3600.0)))
    # Use 1h interval for windows < 7d; 4h for longer
    interval = "1h" if lookback_hours <= 7 * 24 else "4h"

    res = get_hl_candles(
        coin=coin,
        interval=interval,
        lookback_hours=lookback_hours,
        end_time_iso=end_utc.isoformat(),
    )
    if "error" in res:
        return None
    summary = res.get("summary") or {}
    rp = summary.get("return_pct")
    if rp is None:
        return None
    try:
        return float(rp)
    except (TypeError, ValueError):
        return None


def compute_benchmark_return(
    window_start: datetime,
    window_end: datetime,
    kind: Literal["hl_top10", "btc"] = "hl_top10",
) -> BenchmarkResult:
    """Return the passive HL benchmark return over `[window_start, window_end]`.

    Args:
        window_start: ISO-able UTC datetime (usually trade idea published_at).
        window_end: ISO-able UTC datetime (usually trade idea closed_at or expires_at).
        kind: 'hl_top10' (default, equal-weight top-10) or 'btc' (BTC-only).

    Returns:
        BenchmarkResult with `return_pct` in percentage points (e.g. 1.23 means
        +1.23%), or `None` if the entire benchmark could not be computed.
    """
    notes: list[str] = []
    if window_end <= window_start:
        notes.append("window_end <= window_start; returning 0.0")
        return BenchmarkResult(
            kind=kind, window_start=window_start, window_end=window_end,
            return_pct=0.0, constituents=[], notes=notes,
        )

    if kind == "btc":
        coins = ("BTC",)
    elif kind == "hl_top10":
        coins = HL_TOP10_DEFAULT
    else:
        raise ValueError(f"unknown benchmark kind: {kind!r}")

    rets: list[tuple[str, float]] = []
    constituents: list[dict] = []
    for c in coins:
        r = _coin_return_pct(c, window_start, window_end)
        if r is None:
            constituents.append({"coin": c, "return_pct": None, "note": "no_data"})
            continue
        rets.append((c, r))
        constituents.append({"coin": c, "return_pct": r})

    if not rets:
        notes.append("no constituents had usable data; benchmark is None")
        return BenchmarkResult(
            kind=kind, window_start=window_start, window_end=window_end,
            return_pct=None, constituents=constituents, notes=notes,
        )

    avg = sum(r for _, r in rets) / len(rets)
    if len(rets) < len(coins):
        notes.append(
            f"only {len(rets)}/{len(coins)} constituents available; "
            f"survivor bias possible"
        )
    return BenchmarkResult(
        kind=kind, window_start=window_start, window_end=window_end,
        return_pct=avg, constituents=constituents, notes=notes,
    )

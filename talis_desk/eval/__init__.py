"""Trade idea evaluator — Layer 4 PnL gate of the SOTA Desk Architecture v2.

The resolver fills realized PnL, benchmark return, alpha, and Brier on every
published trade idea. The trade-book metrics aggregate those into the
rolling-30d scorecard the manual dashboard cares about.

Public API:
  - resolve_trade_idea(idea_id, as_of=None)        — score one idea, idempotent
  - resolve_all_due(as_of=None)                    — daily cron pass
  - compute_trade_book_metrics(window_days=30)     — rolling aggregate
  - attribute_alpha(idea_id, method='simple')      — split alpha across cites
  - compute_benchmark_return(start, end, kind)    — HL passive benchmark
"""
from .resolver import (
    resolve_trade_idea,
    resolve_all_due,
    compute_trade_book_metrics,
    attribute_alpha,
    TradeIdeaOutcome,
    ResolverRunReport,
    TradeBookMetrics,
    AlphaAttribution,
)
from .benchmark import compute_benchmark_return, BenchmarkResult, HL_TOP10_DEFAULT

__all__ = [
    "resolve_trade_idea",
    "resolve_all_due",
    "compute_trade_book_metrics",
    "attribute_alpha",
    "compute_benchmark_return",
    "TradeIdeaOutcome",
    "ResolverRunReport",
    "TradeBookMetrics",
    "AlphaAttribution",
    "BenchmarkResult",
    "HL_TOP10_DEFAULT",
]

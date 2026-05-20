"""Manual Eval Dashboard — Phase 7 / v2 §5.

Serves a single-page HTML view of the SOTA desk's state, designed so a human
reviewer can sign off the weekly eval in under 30 minutes (v2 line 479).

Seven panels (v2 lines 481-489):
  1. Top strip: 9 S-tier metrics with status + 30d trend + "why red"
  2. Trade book: open/closed PnL waterfall + benchmark delta + discipline
  3. Debates: unresolved/judged/overturned + judge reliability
  4. Hypothesis graph sampler
  5. Persona diffs: candidate vs base with Brier/alpha before-after
  6. Weakness map: top 5 with owner, budget, expected lift, actual lift
  7. Tool atlas: coverage/cost/latency/errors/affinity per specialist

Public API:
  - `app`                       FastAPI app, mountable on uvicorn
  - `render_dashboard_html`     Pure HTML render (no FastAPI dependency)
  - `gather_*` functions        Used by `/api/*` JSON endpoints

Layout matches `udai22.github.io/talis-data-layer/` dark theme.
"""
from .render import (
    gather_debates_panel,
    gather_hypothesis_graph_sample,
    gather_persona_diffs,
    gather_scorecard,
    gather_tool_atlas,
    gather_trade_book,
    gather_weakness_map,
    render_dashboard_html,
)
from .server import app

__all__ = [
    "app",
    "gather_debates_panel",
    "gather_hypothesis_graph_sample",
    "gather_persona_diffs",
    "gather_scorecard",
    "gather_tool_atlas",
    "gather_trade_book",
    "gather_weakness_map",
    "render_dashboard_html",
]

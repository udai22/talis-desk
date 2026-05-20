"""Brief composer — renders the desk's cycle outputs into a daily markdown
artifact.

The brief is the *explanation* of the trade-idea book, not the product
itself (per `wiki/SOTA_DESK_ARCHITECTURE.md` v2 §2 Layer 4: "Briefs explain
the book; they are not the product."). It's downstream of trade ideas and
intentionally cheap to produce so the human eval loop in Section 5 stays
under 30 minutes per week.

Public API:
  - compose_brief(...)         -> the single entry point. Returns Brief.
  - Brief                      -> output dataclass (markdown + structured
                                   payload + audit refs).
  - BriefCostExceededError     -> raised when LLM spend exceeds the hard
                                   cap of $0.10 per brief.

See `composer.py` for the contract + honest gaps; see `templates.py` for
the rendering primitives.
"""
from .composer import (
    Brief,
    BriefCostExceededError,
    compose_brief,
    BRIEF_COST_HARD_USD,
    BRIEF_COST_SOFT_USD,
    HEAT_SCORE_HOT_THRESHOLD,
)
from .templates import (
    render_brief_markdown,
    render_open_trade_book,
    render_closed_trade_book,
    render_hot_hypotheses,
    render_debates,
    render_playbooks,
    render_source_health,
    render_cycle_stats,
    render_methodology_notes,
    render_quiet_cycle,
    render_headline,
    render_header,
    render_html,
    render_json,
    NUMERIC_AFFECTING_FLAGS,
)

__all__ = [
    "Brief",
    "BriefCostExceededError",
    "compose_brief",
    "BRIEF_COST_HARD_USD",
    "BRIEF_COST_SOFT_USD",
    "HEAT_SCORE_HOT_THRESHOLD",
    "render_brief_markdown",
    "render_open_trade_book",
    "render_closed_trade_book",
    "render_hot_hypotheses",
    "render_debates",
    "render_playbooks",
    "render_source_health",
    "render_cycle_stats",
    "render_methodology_notes",
    "render_quiet_cycle",
    "render_headline",
    "render_header",
    "render_html",
    "render_json",
    "NUMERIC_AFFECTING_FLAGS",
]

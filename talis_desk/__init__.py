"""talis-desk — autonomous Hyperliquid research desk.

Layer 2 of the Talis stack. Depends on talis-tic (Layer 1 data foundation).
See wiki/SOTA_DESK_ARCHITECTURE.md for the locked v2 specification.
See wiki/REPO_BOUNDARY.md for the boundary contract with talis-tic.
"""

__version__ = "0.1.0"

from .store import DeskStore, get_desk_store
from .schema import apply_sota_schema
from .replay import build_replay_context
from .tool_atlas import (
    regenerate_tool_atlas,
    dispatch_uri,
    resolve_tool_uri,
    parse_tool_uri,
)
from .trade_ideas import (
    TradeIdea,
    TradeIdeaDraft,
    validate_trade_idea,
    emit_trade_idea,
)
from .eval import (
    resolve_trade_idea,
    resolve_all_due,
    compute_trade_book_metrics,
    attribute_alpha,
    compute_benchmark_return,
)
from .hypotheses import (
    Hypothesis,
    HypothesisDraft,
    HypothesisEdge,
    HypothesisEdgeDraft,
    HypothesisGraph,
    add_edge,
    get_active_hypotheses,
    get_hypothesis_graph,
    propose_hypothesis,
    resolve_hypothesis,
    update_posterior,
)
from .exploration import (
    DebateTriggerDecision,
    ExplorationTrace,
    HotSignalScore,
    HypothesisSeed,
    Investigation,
    InvestigationBudget,
    QuestionNode,
    explore_adversarial,
    maybe_trigger_debate,
    start_investigation,
)
from .agents_native.scratchpad import (
    AgentMessage,
    mark_read,
    post_message,
    post_to_scratchpad_for_cycle,
    read_unread_messages,
)

__all__ = [
    "DeskStore",
    "get_desk_store",
    "apply_sota_schema",
    "build_replay_context",
    "regenerate_tool_atlas",
    "dispatch_uri",
    "resolve_tool_uri",
    "parse_tool_uri",
    "TradeIdea",
    "TradeIdeaDraft",
    "validate_trade_idea",
    "emit_trade_idea",
    "resolve_trade_idea",
    "resolve_all_due",
    "compute_trade_book_metrics",
    "attribute_alpha",
    "compute_benchmark_return",
    # Phase 4: hypotheses
    "Hypothesis",
    "HypothesisDraft",
    "HypothesisEdge",
    "HypothesisEdgeDraft",
    "HypothesisGraph",
    "propose_hypothesis",
    "add_edge",
    "update_posterior",
    "resolve_hypothesis",
    "get_active_hypotheses",
    "get_hypothesis_graph",
    # Phase 4: exploration
    "InvestigationBudget",
    "HypothesisSeed",
    "HotSignalScore",
    "QuestionNode",
    "Investigation",
    "ExplorationTrace",
    "DebateTriggerDecision",
    "start_investigation",
    "explore_adversarial",
    "maybe_trigger_debate",
    # Phase 4: durable scratchpad
    "AgentMessage",
    "post_message",
    "read_unread_messages",
    "mark_read",
    "post_to_scratchpad_for_cycle",
]

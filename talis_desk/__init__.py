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
from .playbooks import (
    ActionTemplate,
    Playbook,
    PlaybookBacktest,
    PlaybookDraft,
    PlaybookTrigger,
    TriggerSpec,
    detect_playbook_triggers,
    evaluate_playbook,
    instantiate_playbook_trade,
    promote_playbook,
    propose_playbook,
    retire_playbook,
)
from .debate import (
    Debate,
    DebateArgument,
    DebateVerdict,
    apply_debate_verdict,
    judge_debate,
    open_debate,
    run_full_debate_cycle,
    submit_debate_argument,
)
from .specialists import (
    SpecialistPersona,
    SpecialistState,
    register_persona,
    get_current_persona,
    list_personas,
    build_macro_regime_v1,
    register_macro_regime_v1,
    MACRO_REGIME_INITIAL_PRIORS,
    MACRO_REGIME_CURATED_TOOL_URIS,
)
from .loop import (
    CycleHydration,
    CyclePlan,
    CycleReflection,
    CycleSynthesis,
    HypothesisDraftPlan,
    LoopConfig,
    ResearchCycleResult,
    run_research_cycle,
)
from .brief import (
    Brief,
    BriefCostExceededError,
    compose_brief,
    BRIEF_COST_HARD_USD,
    BRIEF_COST_SOFT_USD,
    HEAT_SCORE_HOT_THRESHOLD,
)
from .evolution import (
    PersonaDiff,
    PersonaMutationCandidate,
    PersonaABResult,
    VetoStatus,
    propose_persona_mutation,
    run_nightly_auto_mutation,
    promote_persona,
    rollback_persona,
    check_veto_window,
    run_persona_ab_test,
)
from .eval.rewards import (
    score_correctness,
    score_alpha,
    score_novelty as score_novelty_reward,
    score_coverage_for_specialist,
    score_cost_penalty,
    score_debate_quality,
    score_playbook_hit,
    aggregate_specialist_rewards,
    aggregate_tool_affinity,
)
from .eval.novelty import score_novelty, NoveltyScore

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
    # Phase 5: playbooks
    "Playbook",
    "PlaybookDraft",
    "PlaybookBacktest",
    "PlaybookTrigger",
    "TriggerSpec",
    "ActionTemplate",
    "propose_playbook",
    "evaluate_playbook",
    "detect_playbook_triggers",
    "instantiate_playbook_trade",
    "promote_playbook",
    "retire_playbook",
    # Phase 5: debate
    "Debate",
    "DebateArgument",
    "DebateVerdict",
    "open_debate",
    "submit_debate_argument",
    "judge_debate",
    "apply_debate_verdict",
    "run_full_debate_cycle",
    # Phase 6: specialists (personas)
    "SpecialistPersona",
    "SpecialistState",
    "register_persona",
    "get_current_persona",
    "list_personas",
    "build_macro_regime_v1",
    "register_macro_regime_v1",
    "MACRO_REGIME_INITIAL_PRIORS",
    "MACRO_REGIME_CURATED_TOOL_URIS",
    # Phase 4: research-cycle orchestrator
    "run_research_cycle",
    "LoopConfig",
    "ResearchCycleResult",
    "CycleHydration",
    "CyclePlan",
    "CycleSynthesis",
    "CycleReflection",
    "HypothesisDraftPlan",
    # Phase 6: brief composer
    "Brief",
    "BriefCostExceededError",
    "compose_brief",
    "BRIEF_COST_HARD_USD",
    "BRIEF_COST_SOFT_USD",
    "HEAT_SCORE_HOT_THRESHOLD",
    # Phase 6: evolution + rewards + novelty
    "PersonaDiff",
    "PersonaMutationCandidate",
    "PersonaABResult",
    "VetoStatus",
    "propose_persona_mutation",
    "run_nightly_auto_mutation",
    "promote_persona",
    "rollback_persona",
    "check_veto_window",
    "run_persona_ab_test",
    "score_correctness",
    "score_alpha",
    "score_novelty_reward",
    "score_coverage_for_specialist",
    "score_cost_penalty",
    "score_debate_quality",
    "score_playbook_hit",
    "aggregate_specialist_rewards",
    "aggregate_tool_affinity",
    "score_novelty",
    "NoveltyScore",
]

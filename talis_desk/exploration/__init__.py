"""Adversarial exploration — Layer 2 of the SOTA Desk Architecture v2.

Hot investigations do breadth-first exploration AND contradiction search in
one loop. A hot signal spawns confirmation branches plus a parallel
devil's-advocate sub-investigation.

Source of truth: `wiki/SOTA_DESK_ARCHITECTURE.md` v2 §2 Layer 2 (lines 81-95)
and §7 (kill switch / thrash controls).
"""
from .bfs import (
    DebateTriggerDecision,
    ExplorationTrace,
    HotSignalScore,
    HypothesisSeed,
    Investigation,
    InvestigationBudget,
    QuestionNode,
    expand_question,
    explore_adversarial,
    maybe_trigger_debate,
    reset_debate_cooldowns,
    score_hot_signal,
    spawn_sub_investigation,
    start_investigation,
)

__all__ = [
    "InvestigationBudget",
    "HypothesisSeed",
    "HotSignalScore",
    "QuestionNode",
    "Investigation",
    "ExplorationTrace",
    "DebateTriggerDecision",
    "start_investigation",
    "explore_adversarial",
    "score_hot_signal",
    "expand_question",
    "spawn_sub_investigation",
    "maybe_trigger_debate",
    "reset_debate_cooldowns",
]

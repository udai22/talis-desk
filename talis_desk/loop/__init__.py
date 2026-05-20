"""Canonical research-cycle orchestrator — Layer agnostic 6-stage loop.

Source of truth: `wiki/SOTA_DESK_ARCHITECTURE.md` v2 §4 (the agent loop) +
§7 (kill switch). One loop, parameterized by specialist state.

Public API:
  - run_research_cycle(specialist_id, cycle_id, as_of=None, loop_config=None)
      -> ResearchCycleResult
  - LoopConfig                     — knobs (caps, ensemble, % budgets)
  - ResearchCycleResult            — output of one cycle (all 6 stages)
  - CycleHydration / CyclePlan /
    CycleSynthesis / CycleReflection — stage outputs (carried in result)
"""
from .runner import (
    CycleHydration,
    CyclePlan,
    CycleReflection,
    CycleSynthesis,
    HypothesisDraftPlan,
    LoopConfig,
    ResearchCycleResult,
    run_research_cycle,
)

__all__ = [
    "run_research_cycle",
    "LoopConfig",
    "ResearchCycleResult",
    "CycleHydration",
    "CyclePlan",
    "CycleSynthesis",
    "CycleReflection",
    "HypothesisDraftPlan",
]

"""Research Director — Phase 7 / Layer 5 curriculum allocator.

Per `wiki/SOTA_DESK_ARCHITECTURE.md` v2 §6 Week 6 (lines 521-523) and Layer 5
(lines 138-168): the Research Director itself uses the canonical loop, but
its outputs are ASSIGNMENTS (via `agent_messages` with
`message_kind='curriculum_assignment'`) — not trade ideas.

Inputs:
  - `mv_weakness_map`               -> top weaknesses per specialist
  - `mv_specialist_brier_rolling`   -> per-specialist Brier track record
  - `mv_top_tools_per_specialist_30d` -> coverage gaps
  - `mv_hot_hypotheses`             -> which themes need more investigation

Output:
  - `CurriculumPlan` with per-specialist allocations (budget, focus areas,
    expected Brier lift)
  - `agent_messages` rows posted to each specialist's inbox
  - `specialist_states` row tagged `state_kind='persona'` with
    `specialist_id='research_director'`
"""
from .research_director import (
    CurriculumOutcome,
    CurriculumPlan,
    InvestigationAssignment,
    SpecialistAllocation,
    assign_hot_investigations,
    evaluate_curriculum_lift,
    run_research_director_cycle,
)

__all__ = [
    "CurriculumOutcome",
    "CurriculumPlan",
    "InvestigationAssignment",
    "SpecialistAllocation",
    "assign_hot_investigations",
    "evaluate_curriculum_lift",
    "run_research_director_cycle",
]

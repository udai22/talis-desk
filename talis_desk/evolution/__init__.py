"""Persona evolution + auto-mutation — Phase 6 Layer 5 of SOTA Desk v2.

Per wiki/SOTA_DESK_ARCHITECTURE.md §2 Layer 5 (lines 138-152) + §5 (lines
477-491). The pieces:

  - propose_persona_mutation(specialist_id, reason, diff, author)
      Append a `specialist_states.state_kind='mutation_candidate'` row.

  - run_nightly_auto_mutation(as_of=None)
      Read each specialist's `reward_log` aggregates + brier rolling, ask
      Haiku (via tic.desk.models.chat fallback chain) for a small diff,
      and write it as a mutation_candidate.

  - check_veto_window(candidate_id)
      Has the 24h human-veto window elapsed without an agent_messages
      'veto' message? Returns enough info for the auto-promote loop.

  - promote_persona(candidate_id, judge_verdict_id=None)
      Apply the candidate's diff to the base persona, write a new
      `state_kind='persona'` row, close the prior persona + candidate.

  - rollback_persona(specialist_id, target_version, reason)
      Append-only rollback to an older persona version.

  - run_persona_ab_test(specialist_id, base_version, candidate_version,
                        window_days=15, n_cycles_per_arm=30)
      Score base vs candidate on identical bitemporal snapshots: Brier
      delta, alpha delta, cost-per-useful-citation, novelty-correct share.
"""
from .mutator import (
    PersonaDiff,
    PersonaMutationCandidate,
    VetoStatus,
    propose_persona_mutation,
    run_nightly_auto_mutation,
    promote_persona,
    rollback_persona,
    check_veto_window,
    VETO_WINDOW_HOURS,
    DEFAULT_MUTATION_MODEL,
)
from .ab_test import (
    PersonaABResult,
    run_persona_ab_test,
)

__all__ = [
    # mutator
    "PersonaDiff",
    "PersonaMutationCandidate",
    "VetoStatus",
    "propose_persona_mutation",
    "run_nightly_auto_mutation",
    "promote_persona",
    "rollback_persona",
    "check_veto_window",
    "VETO_WINDOW_HOURS",
    "DEFAULT_MUTATION_MODEL",
    # ab_test
    "PersonaABResult",
    "run_persona_ab_test",
]

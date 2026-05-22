"""Coordination-kernel primitives for the v5 desk.

These helpers sit above the bitemporal blackboard tables. They give future
swarm runners a typed way to post work, claim work, record verifier votes,
update coverage, and explain failures.
"""

from .kernel import (
    append_blackboard_event,
    attribute_failure,
    claim_task,
    complete_task,
    coverage_cell_key,
    expire_overdue_tasks,
    expire_task,
    fail_task,
    kill_task,
    post_task,
    promote_task,
    record_claim_vote,
    start_task,
    tally_votes,
    touch_coverage_cell,
)

__all__ = [
    "append_blackboard_event",
    "attribute_failure",
    "claim_task",
    "complete_task",
    "coverage_cell_key",
    "expire_overdue_tasks",
    "expire_task",
    "fail_task",
    "kill_task",
    "post_task",
    "promote_task",
    "record_claim_vote",
    "start_task",
    "tally_votes",
    "touch_coverage_cell",
]

"""Playbook Consolidation — Layer 3 of the SOTA Desk Architecture v2.

Repeated profitable patterns become named, versioned, triggerable playbooks
with out-of-sample scoring (wiki/SOTA_DESK_ARCHITECTURE.md v2 §2 Layer 3,
lines 96-107 + DDL lines 279-299).

Public API:
  - Playbook / PlaybookDraft / TriggerSpec / ActionTemplate — Pydantic shapes
  - PlaybookBacktest / PlaybookTrigger — return shells for evaluate / detect
  - propose_playbook(spec, owner_specialist) -> Playbook
  - evaluate_playbook(playbook_id, as_of, lookback_days) -> PlaybookBacktest
  - detect_playbook_triggers(as_of) -> list[PlaybookTrigger]
  - instantiate_playbook_trade(playbook_id, trigger, context) -> TradeIdea
  - promote_playbook(playbook_id) -> Playbook
  - retire_playbook(playbook_id, reason) -> Playbook
"""
from .model import (
    ActionTemplate,
    Playbook,
    PlaybookBacktest,
    PlaybookDraft,
    PlaybookTrigger,
    TriggerSpec,
    detect_playbook_triggers,
    evaluate_playbook,
    get_playbook,
    instantiate_playbook_trade,
    promote_playbook,
    propose_playbook,
    retire_playbook,
)

__all__ = [
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
    "get_playbook",
]

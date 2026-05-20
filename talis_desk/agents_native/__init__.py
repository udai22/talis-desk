"""Agent-native primitives (durable scratchpad, etc.).

Ported from `tic/agent_native/` with the SOTA v2 §4 durable-message model:
messages live in the `agent_messages` table (cross-cycle) instead of a
per-cycle `scratchpads` table.
"""
from .scratchpad import (
    AgentMessage,
    cycle_summary,
    mark_read,
    post_message,
    post_to_scratchpad_for_cycle,
    read_unread_messages,
)

__all__ = [
    "AgentMessage",
    "post_message",
    "read_unread_messages",
    "mark_read",
    "post_to_scratchpad_for_cycle",
    "cycle_summary",
]

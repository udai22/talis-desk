"""Multi-Agent Debate — SOTA Desk Architecture v2 §2 Layer 2 + §6 protocol.

When a claim/trade-idea hits a debate trigger (posterior > 0.75, confidence
>= 0.7, short horizon, source conflict, etc.), `maybe_trigger_debate` in
the exploration layer raises the flag and this module runs the protocol:

  1. open_debate(trigger_kind, trigger_id, participants, judge_provider?)
       - Pause publication of the claim/idea
       - Pick judge from a different provider family than both participants
       - Insert `debates` row + post `request_debate_argument` to each
         participant via durable agent_messages.

  2. submit_debate_argument(debate_id, agent_id, argument_md, ...)
       - ≤200 words, citations must resolve, falsifiable_crux required.
       - When both participants have submitted, transition to status='judged'
         and call judge_debate.

  3. judge_debate(debate_id, judge_model?)
       - Build judge prompt with claim + both arguments + resolved citations
         + source health, call the judge LLM (Sonnet default).
       - Parse structured JSON response, persist verdict, set status='judged'.

  4. apply_debate_verdict(debate_id)
       - Write specialist_states row with state_kind='mutation_candidate'
         citing debate_id on the LOSER's record. If follow_up_action specifies
         a downgrade, call update_posterior or supersede the claim.
       - Set debate.status='applied'.

  5. run_full_debate_cycle (helper for testing) — open + submit + judge +
     apply in one call.
"""
from .model import (
    Debate,
    DebateArgument,
    DebateVerdict,
)
from .runner import (
    apply_debate_verdict,
    judge_debate,
    open_debate,
    run_full_debate_cycle,
    submit_debate_argument,
)

__all__ = [
    "Debate",
    "DebateArgument",
    "DebateVerdict",
    "open_debate",
    "submit_debate_argument",
    "judge_debate",
    "apply_debate_verdict",
    "run_full_debate_cycle",
]

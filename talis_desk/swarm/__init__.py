"""Talis Desk v5 swarm orchestrator.

Replaces the per-specialist sequential pipeline with a 4-tier parallel
swarm:

  Tier 0  -> stratified seed generation (Latin Hypercube + coverage penalty
              + theme injection + manifold-density routing)
  Tier 1  -> 1000 DeepSeek Flash scouts (~$0.0002 each, ~$0.25 total)
  Tier 1.5 -> 3-family verifier council (2/3 majority required to graduate)
  Tier 2  -> ~50 Sonnet 4.6 / DeepSeek Pro analysts (constitutional voices =
              existing 21 specialist personas, unchanged)
  Tier 3  -> existing 6-stage adversarial pipeline, scoped to top-20 only
  Tier 4  -> 1-2 Opus calls produce the daily brief

Total cycle cost target: ~$4.81. Daily cap: $100 -> 20 cycles/day.

NO STUBS anywhere: every LLM call walks the multi-provider fallback chain.
If a tier can't get a real verifier, the scout output is dropped with
`quality_flag=['verifier_unavailable']` — never fabricate a verification.
"""
from .seed_generator import (
    SeedCell,
    generate_seeds,
)
from .scout_runner import (
    ScoutOutput,
    run_scouts,
)
from .verifier_council import (
    VerifierVerdict,
    run_verifier_council,
)
from .analyst_pool import (
    AnalystOutput,
    run_analyst_pool,
)
from .brief_synthesis import synthesize_brief
from .market import (
    MarketBid,
    agent_brier_reputation,
    award_task,
    post_priced_task,
    submit_bid,
    update_market_posteriors,
)

__all__ = [
    "SeedCell",
    "generate_seeds",
    "ScoutOutput",
    "run_scouts",
    "VerifierVerdict",
    "run_verifier_council",
    "AnalystOutput",
    "run_analyst_pool",
    "synthesize_brief",
    "MarketBid",
    "agent_brier_reputation",
    "award_task",
    "post_priced_task",
    "submit_bid",
    "update_market_posteriors",
]

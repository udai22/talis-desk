"""Microstructure Specialist — v1.0 persona constructor.

Builds and registers the canonical microstructure persona row in
`specialist_states`. Idempotent — re-importing this module or re-calling
`register_microstructure_v1` will NOT duplicate rows.

The persona's system prompt lives in `prompt.md` (adjacent file) so it can
be read, diffed, and reviewed without diving into Python source. The
prompt and the curated tool subset together fully define the v1 persona.

Source of truth for the persona contract: see `..base.py`.
Source of truth for the system-prompt structure: this file's prompt.md +
`wiki/SOTA_DESK_ARCHITECTURE.md` §4 (Agent Loop) + §5 (Self-Improvement).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..base import SpecialistPersona, SpecialistState, register_persona


# ============================================================================
# Constants — exported for reuse by the loop runner / tests
# ============================================================================

SPECIALIST_ID = "microstructure"
PERSONA_VERSION = "v1.0"
PERSONA_NAME = "Microstructure Specialist"
PERSONA_SCOPE = (
    "The tactical/execution voice on the Talis desk. Read HL microstructure "
    "in real time — orderflow, L2 imbalance, perp funding, cross-venue "
    "basis, oracle drift, realized vol vs implied, candle pattern integrity, "
    "and anomaly bursts — and cross-check macro/smart-money calls against "
    "the actual market plumbing before sizing."
)

# Preferred model — the loop's `chat()` call falls back through the
# provider chain if Anthropic is down. Opus chosen for the multi-tool
# orchestration + contradiction synthesis required when funding, basis,
# and L2 disagree.
PREFERRED_MODEL = "anthropic:claude-opus-4-7"

# Inter-agent topics this specialist subscribes to. The scratchpad
# router uses these to route peer messages from other specialists.
SUBSCRIBED_TOPICS = [
    "microstructure",        # direct address
    "all",                   # broadcast
    "execution",             # sizing / slippage questions from director
    "funding",               # perp funding cross-cutting
    "basis",                 # cross-venue spread cross-cutting
    "oracle",                # HL oracle drift cross-cutting
    "research_director",     # assignments from the director
]


# Initial priors — the "world model" the specialist starts with on its
# first cycle. Updated via REFLECT each cycle; supersedes chain visible
# in `specialist_states`.
#
# Snapshot date: 2026-05-20.
INITIAL_PRIORS: dict[str, Any] = {
    "regime": "compressed_realized_vol",            # 30d RV well below 1y avg
    "dominant_imbalance": "balanced",               # neither buyer nor seller dominant
    "funding_baseline_bps": 5,                      # 8h funding ~5bps annualized neutral
    "basis_baseline_bps": 8,                        # HL vs Binance perp basis ~8bps neutral
    "oracle_drift_baseline_bps": 2,                 # HL oracle vs index ~2bps neutral
    "realized_vol_baseline_pct": 35,                # 30d RV ~35% annualized for BTC
    "confidence": 0.65,
    "uncertain_about": [
        "whether compressed RV resolves up (vol expansion) or down (range continuation)",
        "HL oracle node restart cadence and its impact on intra-second drift",
        "stablecoin-driven funding regime vs flow-driven funding regime",
    ],
    "watch_list": [
        "BTC perp funding crossing +/- 15bps (8h) as a positioning extreme",
        "HL vs Binance basis > 25bps (rich) or < -15bps (cheap) for >2h",
        "oracle drift > 10bps sustained (chart integrity risk)",
        "L2 top-of-book imbalance > 3:1 persisting > 5 minutes",
        "realized vol 1d / realized vol 30d ratio > 1.8 (vol regime break)",
    ],
}


# Curated tool subset — the 15 URIs this specialist sees. Per v2 §5: NOT
# the full atlas. The loop runner builds the function-calling tool list
# from this subset.
#
# Selection rationale: microstructure needs L2/orderflow snapshots,
# funding + basis + oracle reads, candle-feature verification, anomaly
# queries, cross-source confluence, and ad-hoc stats. NO macro time-
# series tools (those belong to macro_regime), NO wallet flow tools
# (smart_money), NO pattern-recognition chart tools (chartist).
CURATED_TOOL_URIS: list[str] = [
    # --- HL L2 / orderbook ---
    "tic://tool/builtin/get_hl_l2_snapshot@v1",
    # --- Funding + basis + perps ---
    "tic://tool/builtin/get_hl_funding_history@v1",
    "tic://tool/builtin/cross_venue_basis@v1",
    # --- Candles + price action ---
    "tic://tool/builtin/get_hl_candles@v1",
    "tic://tool/builtin/candle_features@v1",
    # --- Microstructure synthesis primitives ---
    "tic://tool/builtin/get_microstructure_state@v1",
    "tic://tool/builtin/analyze_microstructure@v1",
    # --- Realized vol ---
    "tic://tool/builtin/get_realized_vol@v1",
    # --- Oracle integrity (HL oracle vs external refs) ---
    "tic://tool/builtin/get_oracle_drift@v1",
    "tic://tool/builtin/get_oracle_price_history@v1",
    "tic://tool/builtin/get_pyth_price@v1",
    # --- Anomalies + cross-source confluence ---
    "tic://tool/builtin/query_anomalies_active@v1",
    "tic://tool/builtin/find_confluence@v1",
    # --- Stats + source health ---
    "tic://tool/builtin/compute_stat@v1",
    "tic://tool/builtin/query_source_health@v1",
]


# ============================================================================
# Prompt loader
# ============================================================================

_PROMPT_PATH = Path(__file__).parent / "prompt.md"


def _load_system_prompt() -> str:
    """Read prompt.md from the adjacent file. Raises FileNotFoundError if
    the file is missing — this is a packaging bug, not a runtime fallback."""
    if not _PROMPT_PATH.exists():
        raise FileNotFoundError(
            f"microstructure prompt.md not found at {_PROMPT_PATH}. "
            f"This is a packaging bug — the file must ship with the module."
        )
    text = _PROMPT_PATH.read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError(f"microstructure prompt.md is empty at {_PROMPT_PATH}")
    # Quick structural check: the 5 required sections must be present.
    required_sections = [
        "## ROLE",
        "## BEHAVIORAL DEFAULTS",
        "## TOOL SELECTION DECISION TREE",
        "## 3 WORKED EXAMPLES",
        "## OUTPUT CONTRACT",
    ]
    missing = [s for s in required_sections if s not in text]
    if missing:
        raise ValueError(
            f"microstructure prompt.md is missing required sections: {missing}. "
            f"See talis_desk/specialists/microstructure_v1/prompt.md."
        )
    return text


# ============================================================================
# Builders
# ============================================================================

def build_microstructure_v1() -> SpecialistPersona:
    """Construct the v1 persona. Reads system_prompt from prompt.md.

    Returns a fully-populated `SpecialistPersona` ready to pass into
    `register_persona`. Pure — does not touch the DB.
    """
    system_prompt = _load_system_prompt()
    return SpecialistPersona(
        specialist_id=SPECIALIST_ID,
        persona_version=PERSONA_VERSION,
        name=PERSONA_NAME,
        scope=PERSONA_SCOPE,
        system_prompt=system_prompt,
        tool_uris=list(CURATED_TOOL_URIS),
        preferred_model=PREFERRED_MODEL,
        subscribed_topics=list(SUBSCRIBED_TOPICS),
        initial_priors=dict(INITIAL_PRIORS),
        created_at=datetime.now(timezone.utc),
        author="talis-desk",
    )


def register_microstructure_v1() -> SpecialistState:
    """Idempotent registration. Returns the SpecialistState for the open
    persona row — existing if already registered, new if first call.

    See `base.register_persona` for the idempotency contract:
      - If (specialist_id, persona_version, prompt_hash) already has an
        open row → returns that row unchanged.
      - If specialist_id has an open row at a different version/hash →
        closes the old row (transaction_to=now), inserts new with
        supersedes set, returns new.
    """
    persona = build_microstructure_v1()
    return register_persona(persona)

"""Anomaly Scanner Specialist — v1.0 persona constructor.

Builds and registers the canonical anomaly_scanner persona row in
`specialist_states`. Idempotent — re-importing this module or re-calling
`register_anomaly_scanner_v1` will NOT duplicate rows.

Source of truth for the persona contract: see `..base.py`.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..base import SpecialistPersona, SpecialistState, register_persona


# ============================================================================
# Constants — exported for reuse by the loop runner / tests
# ============================================================================

SPECIALIST_ID = "anomaly_scanner"
PERSONA_VERSION = "v1.0"
PERSONA_NAME = "Anomaly Scanner Specialist"
PERSONA_SCOPE = (
    "Pure anomaly hunter. Read `query_anomalies_active`, run cross-source "
    "convergence checks via `find_confluence`, use `run_code` to compute "
    "custom z-scores, regime-break tests, and matrix-profile scans. "
    "Surface setups NO other specialist sees. Trust nothing without a "
    "numerical receipt — every anomaly cited must carry an explicit "
    "threshold and a confluence check."
)

# Preferred model — chosen for the data-mining discipline required to
# avoid false-positive anomalies and combine cross-source evidence.
PREFERRED_MODEL = "anthropic:claude-opus-4-7"

# Inter-agent topics this specialist subscribes to.
SUBSCRIBED_TOPICS = [
    "anomaly_scanner",       # direct address
    "all",                   # broadcast
    "anomalies",             # cross-cutting
    "regime_break",          # cross-cutting (correlation / vol regime)
    "data_quality",          # cross-cutting (helps source-health debugging)
    "research_director",     # assignments from the director
]


# Initial priors — Snapshot date: 2026-05-20.
INITIAL_PRIORS: dict[str, Any] = {
    "n_active_anomalies_24h": 0,                            # filled at hydrate
    "top_anomaly_kind": "funding_extreme",                  # placeholder; updated each cycle
    "cross_source_confluence_threshold": 0.55,              # min confluence to surface
    "regime_break_pct_threshold": 2.5,                      # |z| threshold for regime break
    "confidence": 0.5,
    "uncertain_about": [
        "matrix-profile false-positive rate on illiquid HL perp candles",
        "whether cross_dex_collision anomalies are arbitrage or wash trading",
        "reject-burst signal half-life (when does a 4h-old reject pattern decay?)",
    ],
    "watch_list": [
        "funding z-score > 2 on >= 3 perps simultaneously (consensus extreme)",
        "BTC-NDX 30d correlation breaking by > 0.3 in 5 trading days",
        "oracle drift > 10 bps sustained > 30 min (chart integrity)",
        "reject corpus showing same wallet across >= 3 coins liquidating",
        "news spike z > 3 with no confluence signal (manipulated or genuine)",
        "matrix-profile motif distance > 4.0 (rare configuration)",
    ],
    "frontier_research_quota": 2,                           # ALWAYS hold 2 hypothesis slots
                                                            # for niche scans (see prompt)
}


# Curated tool subset — the 15 URIs this specialist sees.
#
# Selection rationale: anomaly_scanner is data-mining-first. It needs
# the anomalies endpoint, confluence (cross-source vote), run_code for
# custom z-scores / matrix-profile / regime-break tests, compute_stat
# for basic statistical primitives, find_similar_setups for analog
# scarcity check (base rate), time_machine_snapshot for "did this
# anomaly ever happen before?", semantic_search for narrative cross-
# check, query_recent_news for catalyst detection, cross_dex_collision
# for HL-vs-other-DEX flow anomaly, get_reject_pattern + whale_check
# for the reject-corpus signal, query_timeseries for the raw data,
# query_events_recent for event-driven anomaly attribution, source_
# health for data validity, parallel_search for fast multi-angle scans.
CURATED_TOOL_URIS: list[str] = [
    # --- Primary anomaly endpoint ---
    "tic://tool/builtin/query_anomalies_active@v1",
    # --- Cross-source confluence (anomaly validation) ---
    "tic://tool/builtin/find_confluence@v1",
    # --- Ad-hoc code (matrix profile, z-scores, regime tests) ---
    "tic://tool/builtin/run_code@v1",
    # --- Stat primitives ---
    "tic://tool/builtin/compute_stat@v1",
    # --- Analog scarcity / base-rate check ---
    "tic://tool/builtin/find_similar_setups@v1",
    # --- Bitemporal replay (historical analog comparison) ---
    "tic://tool/builtin/time_machine_snapshot@v1",
    # --- Knowledge retrieval (narrative cross-check) ---
    "tic://tool/builtin/semantic_search@v1",
    # --- News catalyst detection ---
    "tic://tool/builtin/query_recent_news@v1",
    # --- DEX flow anomaly ---
    "tic://tool/builtin/cross_dex_collision@v1",
    # --- Reject corpus + whale signals ---
    "tic://tool/builtin/get_reject_pattern@v1",
    "tic://tool/builtin/whale_check@v1",
    # --- Raw timeseries ---
    "tic://tool/builtin/query_timeseries@v1",
    # --- Event calendar (anomaly attribution) ---
    "tic://tool/builtin/query_events_recent@v1",
    # --- Source health (anomaly validity gate) ---
    "tic://tool/builtin/query_source_health@v1",
    # --- Parallel multi-angle scans ---
    "tic://tool/builtin/parallel_search@v1",
]


# ============================================================================
# Prompt loader
# ============================================================================

_PROMPT_PATH = Path(__file__).parent / "prompt.md"


def _load_system_prompt() -> str:
    if not _PROMPT_PATH.exists():
        raise FileNotFoundError(
            f"anomaly_scanner prompt.md not found at {_PROMPT_PATH}. "
            f"This is a packaging bug — the file must ship with the module."
        )
    text = _PROMPT_PATH.read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError(f"anomaly_scanner prompt.md is empty at {_PROMPT_PATH}")
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
            f"anomaly_scanner prompt.md is missing required sections: {missing}. "
            f"See talis_desk/specialists/anomaly_scanner_v1/prompt.md."
        )
    return text


# ============================================================================
# Builders
# ============================================================================

def build_anomaly_scanner_v1() -> SpecialistPersona:
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


def register_anomaly_scanner_v1() -> SpecialistState:
    persona = build_anomaly_scanner_v1()
    return register_persona(persona)

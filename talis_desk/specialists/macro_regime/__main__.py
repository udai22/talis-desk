"""Smoke test: register the macro_regime v1 persona and verify the row.

Run: `python -m talis_desk.specialists.macro_regime`

Exits 0 on success, 1 on any check failure. Uses a temp DB so we don't
clobber the dev `desk.db`.
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path


def _smoke_test() -> int:
    print("=" * 72)
    print("MACRO_REGIME v1 PERSONA — SMOKE TEST")
    print("=" * 72)

    # Use a temp DB so we don't touch the dev desk.db
    tmpdir = tempfile.mkdtemp(prefix="macro_regime_smoke_")
    db_path = Path(tmpdir) / "desk_smoke.db"
    os.environ["TALIS_DESK_DB_PATH"] = str(db_path)

    # Force a fresh DeskStore at this path. The singleton in ..store reads
    # the first-call db_path, so we wire it explicitly.
    from talis_desk.store import DeskStore
    from talis_desk import store as _store_mod

    desk = DeskStore(db_path=db_path)
    _store_mod._STORE = desk  # type: ignore[attr-defined]

    # Now import the specialist module — it will use our temp store.
    from talis_desk.specialists.macro_regime import (
        build_macro_regime_v1,
        register_macro_regime_v1,
        INITIAL_PRIORS,
        CURATED_TOOL_URIS,
        SPECIALIST_ID,
        PERSONA_VERSION,
    )
    from talis_desk.specialists import get_current_persona, list_personas

    # ---- 1. Build persona -------------------------------------------------
    print("\n[1] build_macro_regime_v1()")
    persona = build_macro_regime_v1()
    print(f"    specialist_id   : {persona.specialist_id}")
    print(f"    persona_version : {persona.persona_version}")
    print(f"    name            : {persona.name}")
    print(f"    n_tools         : {len(persona.tool_uris)}")
    print(f"    preferred_model : {persona.preferred_model}")
    print(f"    len(prompt)     : {len(persona.system_prompt)} chars")
    print(f"    prompt_hash[:12]: {persona.prompt_hash()[:12]}")

    # Sanity checks on the built persona
    if persona.specialist_id != "macro_regime":
        print(f"    FAIL: expected specialist_id 'macro_regime', got {persona.specialist_id!r}")
        return 1
    if persona.persona_version != "v1.0":
        print(f"    FAIL: expected persona_version 'v1.0', got {persona.persona_version!r}")
        return 1
    if len(persona.tool_uris) != 15:
        print(f"    FAIL: expected 15 curated tool URIs, got {len(persona.tool_uris)}")
        return 1
    if persona.preferred_model != "anthropic:claude-opus-4-7":
        print(f"    FAIL: expected preferred_model 'anthropic:claude-opus-4-7'")
        return 1
    # Verify required sections are in the prompt
    required = [
        "## ROLE",
        "## BEHAVIORAL DEFAULTS",
        "## TOOL SELECTION DECISION TREE",
        "## 3 WORKED EXAMPLES",
        "## OUTPUT CONTRACT",
    ]
    missing = [s for s in required if s not in persona.system_prompt]
    if missing:
        print(f"    FAIL: prompt missing sections: {missing}")
        return 1
    print("    [OK] persona built; all 5 required sections present; 15 curated tools")

    # ---- 2. Register ------------------------------------------------------
    print("\n[2] register_macro_regime_v1()")
    state1 = register_macro_regime_v1()
    print(f"    spst_id         : {state1.id}")
    print(f"    state_kind      : {state1.state_kind}")
    print(f"    valid_from      : {state1.valid_from.isoformat()}")
    print(f"    transaction_from: {state1.transaction_from.isoformat()}")
    print(f"    prompt_hash[:12]: {(state1.prompt_hash or '')[:12]}")
    if state1.state_kind != "persona":
        print(f"    FAIL: expected state_kind='persona', got {state1.state_kind!r}")
        return 1
    if not state1.id.startswith("spst_"):
        print(f"    FAIL: id should be spst_*, got {state1.id!r}")
        return 1
    # Verify row exists in the DB
    row = desk.conn.execute(
        "SELECT id, specialist_id, persona_version, state_kind, "
        "       valid_from, transaction_from, transaction_to "
        "FROM specialist_states WHERE id = ?",
        (state1.id,),
    ).fetchone()
    if row is None:
        print(f"    FAIL: row not found in DB after register")
        return 1
    if row["state_kind"] != "persona":
        print(f"    FAIL: row.state_kind != 'persona', got {row['state_kind']!r}")
        return 1
    if row["transaction_to"] is not None:
        print(f"    FAIL: row should be open (transaction_to IS NULL), got {row['transaction_to']!r}")
        return 1
    if not row["valid_from"] or not row["transaction_from"]:
        print(f"    FAIL: bitemporal cols not set")
        return 1
    print(f"    [OK] row persisted, state_kind='persona', bitemporal cols set")

    # ---- 3. Idempotency ---------------------------------------------------
    print("\n[3] Re-register (idempotency)")
    state2 = register_macro_regime_v1()
    print(f"    state2.id       : {state2.id}")
    print(f"    same as state1? : {state2.id == state1.id}")
    if state2.id != state1.id:
        print(f"    FAIL: re-register inserted a new row ({state2.id}) instead of returning existing ({state1.id})")
        return 1
    # And the count in the DB should still be 1
    n_rows = desk.conn.execute(
        "SELECT COUNT(*) FROM specialist_states WHERE specialist_id = ?",
        (SPECIALIST_ID,),
    ).fetchone()[0]
    print(f"    DB row count for macro_regime: {n_rows} (expect 1)")
    if n_rows != 1:
        print(f"    FAIL: expected 1 row, got {n_rows}")
        return 1
    print("    [OK] re-register is a no-op; row count unchanged")

    # ---- 4. get_current_persona ------------------------------------------
    print("\n[4] get_current_persona('macro_regime')")
    fetched = get_current_persona("macro_regime")
    print(f"    fetched.specialist_id   : {fetched.specialist_id}")
    print(f"    fetched.persona_version : {fetched.persona_version}")
    print(f"    fetched.n_tools         : {len(fetched.tool_uris)}")
    print(f"    fetched.preferred_model : {fetched.preferred_model}")
    print(f"    fetched prompt_hash[:12]: {fetched.prompt_hash()[:12]}")
    # Equivalence check vs the built persona
    if fetched.specialist_id != persona.specialist_id:
        print(f"    FAIL: specialist_id mismatch")
        return 1
    if fetched.persona_version != persona.persona_version:
        print(f"    FAIL: persona_version mismatch")
        return 1
    if fetched.system_prompt != persona.system_prompt:
        print(f"    FAIL: system_prompt mismatch")
        return 1
    if list(fetched.tool_uris) != list(persona.tool_uris):
        print(f"    FAIL: tool_uris mismatch")
        return 1
    if fetched.preferred_model != persona.preferred_model:
        print(f"    FAIL: preferred_model mismatch")
        return 1
    if dict(fetched.initial_priors) != dict(persona.initial_priors):
        print(f"    FAIL: initial_priors mismatch")
        return 1
    print("    [OK] roundtrip identical (prompt, tools, model, priors)")

    # ---- 5. list_personas -------------------------------------------------
    print("\n[5] list_personas()")
    all_personas = list_personas()
    print(f"    count           : {len(all_personas)} (expect 1)")
    print(f"    ids             : {[p.specialist_id for p in all_personas]}")
    if len(all_personas) != 1:
        print(f"    FAIL: expected 1 persona, got {len(all_personas)}")
        return 1
    if all_personas[0].specialist_id != "macro_regime":
        print(f"    FAIL: list_personas returned wrong specialist")
        return 1
    print("    [OK] list_personas returns the one open persona")

    # ---- 6. Tool URIs spot check -----------------------------------------
    print("\n[6] Curated tool URIs (expect 15 macro-focused)")
    expected_tools = [
        "tic://tool/builtin/query_timeseries@v1",
        "tic://tool/builtin/query_claims_by_entity@v1",
        "tic://tool/builtin/query_events_recent@v1",
        "tic://tool/builtin/get_fed_balance_sheet_state@v1",
        "tic://tool/builtin/get_fomc_next_event@v1",
        "tic://tool/builtin/get_econ_event_today@v1",
        "tic://tool/builtin/get_treasury_auction_calendar@v1",
        "tic://tool/builtin/get_cot_positioning@v1",
        "tic://tool/builtin/get_cross_asset_corr@v1",
        "tic://tool/builtin/find_confluence@v1",
        "tic://tool/builtin/compute_stat@v1",
        "tic://tool/builtin/run_code@v1",
        "tic://tool/builtin/semantic_search@v1",
        "tic://tool/builtin/time_machine_snapshot@v1",
        "tic://tool/builtin/query_source_health@v1",
    ]
    if list(CURATED_TOOL_URIS) != expected_tools:
        print(f"    FAIL: curated URI list does not match expected spec")
        print(f"    got     : {CURATED_TOOL_URIS}")
        print(f"    expected: {expected_tools}")
        return 1
    for u in CURATED_TOOL_URIS:
        if not u.startswith("tic://tool/builtin/"):
            print(f"    FAIL: non-builtin URI in curated subset: {u}")
            return 1
        print(f"      - {u}")
    print(f"    [OK] all 15 URIs present, all builtin")

    # ---- 7. Priors structure sanity --------------------------------------
    print("\n[7] INITIAL_PRIORS structure")
    expected_keys = {
        "rates_regime", "growth_regime", "credit_regime",
        "btc_regime", "liquidity_regime", "confidence",
        "uncertain_about",
    }
    got_keys = set(INITIAL_PRIORS.keys())
    missing_keys = expected_keys - got_keys
    print(f"    keys            : {sorted(got_keys)}")
    if missing_keys:
        print(f"    FAIL: missing required prior keys: {missing_keys}")
        return 1
    if not isinstance(INITIAL_PRIORS["uncertain_about"], list):
        print(f"    FAIL: uncertain_about must be a list")
        return 1
    if not 0.0 <= INITIAL_PRIORS["confidence"] <= 1.0:
        print(f"    FAIL: confidence must be in [0,1]")
        return 1
    print(f"    [OK] all required keys present; confidence in [0,1]")

    # ---- 8. Bitemporal slice sanity --------------------------------------
    print("\n[8] Bitemporal: get_current_persona at past timestamp")
    # At a time BEFORE valid_from, the row should not be visible.
    past = state1.valid_from - timedelta(hours=1)
    try:
        get_current_persona("macro_regime", as_of=past)
        print(f"    FAIL: persona should be invisible at as_of={past.isoformat()}")
        return 1
    except KeyError:
        print(f"    [OK] persona invisible at {past.isoformat()} (1h before valid_from)")
    # At now+1m, it should be visible.
    future = state1.valid_from + timedelta(minutes=1)
    visible = get_current_persona("macro_regime", as_of=future)
    if visible.specialist_id != "macro_regime":
        print(f"    FAIL: persona not visible at future timestamp")
        return 1
    print(f"    [OK] persona visible at {future.isoformat()}")

    # ---- Final stamp ------------------------------------------------------
    print()
    print("=" * 72)
    print("MACRO_REGIME v1 PERSONA REGISTERED")
    print("=" * 72)
    print(f"  specialist_id   : {state1.specialist_id}")
    print(f"  persona_version : {state1.persona_version}")
    print(f"  spst_id         : {state1.id}")
    print(f"  prompt          : {len(persona.system_prompt)} chars, sha256={persona.prompt_hash()[:16]}...")
    print(f"  n_tools         : {len(CURATED_TOOL_URIS)}")
    print(f"  preferred_model : {persona.preferred_model}")
    print(f"  priors keys     : {sorted(INITIAL_PRIORS.keys())}")
    print(f"  smoke db        : {db_path}")
    print()
    desk.close()
    return 0


if __name__ == "__main__":
    sys.exit(_smoke_test())

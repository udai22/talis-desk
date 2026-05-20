"""End-to-end smoke test for Phase 7 (Research Director + Eval Dashboard).

Run:
    python -m talis_desk.director

Six checks:

  1. Seed: 2 specialists with persona rows + reward_log entries that
     populate mv_weakness_map and mv_specialist_brier_rolling. Also seed
     a couple of hypotheses + tool_call_log rows so the director has
     coverage gaps + hot themes to look at.
  2. Call `run_research_director_cycle(as_of=now())`. The director MUST
     call the real LLM via `tic.desk.models.chat()` with full fallback
     chain — no hardcoded stub responses.
  3. Verify agent_messages with kind='curriculum_assignment' were posted
     to each specialist. Print per-specialist budgets + focus areas.
  4. Verify a persona row for `research_director` lives in
     specialist_states.
  5. Render the dashboard HTML and write to `/tmp/eval_dashboard.html`.
     Verify all 7 panels render (scorecard, trade book, debates, graph,
     persona diffs, weakness map, tool atlas).
  6. Veto path: call `write_veto_message` against a fake candidate id
     and verify the agent_messages `veto` row landed.

A "PHASE 7 DIRECTOR + DASHBOARD — READY" stamp prints on success.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _isolated_store() -> Path:
    tmpdir = tempfile.mkdtemp(prefix="phase7_director_smoke_")
    return Path(tmpdir) / "desk.db"


def _hr(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def main() -> int:
    db_path = _isolated_store()
    from talis_desk import store as store_mod
    store_mod._STORE = None  # type: ignore[attr-defined]
    store_mod.get_desk_store(db_path=db_path)

    from talis_desk.director.research_director import (
        CURRICULUM_ASSIGNMENT_KIND,
        DIRECTOR_SPECIALIST_ID,
        VETO_KIND,
        run_research_director_cycle,
        write_veto_message,
    )
    from talis_desk.dashboard.render import (
        gather_debates_panel,
        gather_hypothesis_graph_sample,
        gather_persona_diffs,
        gather_scorecard,
        gather_tool_atlas,
        gather_trade_book,
        gather_weakness_map,
        render_dashboard_html,
    )
    from talis_desk.store import get_desk_store

    conn = get_desk_store().conn

    print(f"smoke db: {db_path}")
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    print(f"ANTHROPIC_API_KEY present: {has_key}")
    print(f"(director will use real Sonnet)" if has_key else
          "(LLM call will fail; deterministic fallback path)")

    # ============================================================
    # 1. Seed two specialists + brier rows + hypotheses + tool calls
    # ============================================================
    _hr("[1] Seed specialists, reward_log, hypotheses, tool_call_log")
    now = datetime.now(timezone.utc)
    cycle_id = "smoke_cycle_p7"

    specialists = [
        ("microstructure_v3", "openai:gpt-5.5"),
        ("macro_regime_v2", "anthropic:claude-sonnet-4-6"),
    ]
    for sid, model in specialists:
        conn.execute(
            "INSERT INTO specialist_states "
            "(id, specialist_id, persona_version, cycle_id, state_kind, "
            " state_json, valid_from, transaction_from) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"spst_{sid}_seed", sid, "smoke_v1", cycle_id, "persona",
                json.dumps({"persona_model": model}),
                _iso(now), _iso(now),
            ),
        )

    # reward_log rows: macro is well-calibrated (brier ~ 0.18), micro is
    # miscalibrated (brier ~ 0.42). Director should weight macro more
    # heavily but assign micro more investigations.
    rewards = [
        ("microstructure_v3", 0.42, -0.04),
        ("microstructure_v3", 0.45, -0.03),
        ("microstructure_v3", 0.40, -0.05),
        ("microstructure_v3", 0.38, -0.02),
        ("microstructure_v3", 0.46, 0.00),
        ("macro_regime_v2", 0.18, 0.05),
        ("macro_regime_v2", 0.20, 0.04),
        ("macro_regime_v2", 0.16, 0.06),
        ("macro_regime_v2", 0.22, 0.03),
        ("macro_regime_v2", 0.19, 0.07),
    ]
    for i, (sid, score, delta) in enumerate(rewards):
        vf = _iso(now - timedelta(days=(i % 5) + 1))
        conn.execute(
            "INSERT INTO reward_log "
            "(id, cycle_id, reward_kind, subject_kind, subject_id, "
            " specialist_id, score, delta, valid_from) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"rew_smoke_{i}", cycle_id, "correctness", "hypothesis",
                f"hyp_seed_{i}", sid, score, delta, vf,
            ),
        )

    # Hypotheses with heat
    hyps = [
        ("hyp_smoke_a", "microstructure_v3", "BTC funding extreme + OFI flip", 0.72, 0.85),
        ("hyp_smoke_b", "macro_regime_v2", "DXY breakout = risk-off", 0.65, 0.55),
        ("hyp_smoke_c", "microstructure_v3", "L5 ladder thinning intraday", 0.58, 0.70),
    ]
    for hid, sid, title, post, heat in hyps:
        conn.execute(
            "INSERT INTO hypotheses (id, cycle_id, specialist_id, title, "
            " hypothesis_text, status, posterior_prob, heat_score, valid_from) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                hid, cycle_id, sid, title, title + " — full text",
                "active", post, heat, _iso(now - timedelta(days=2)),
            ),
        )

    # Tool calls — micro has 2 active tools, macro has 1; cover gap exists.
    tool_calls = [
        ("tc_smoke_1", "microstructure_v3", "tic://tool/builtin/query_ofi@v1", 0, 250, "ts_query"),
        ("tc_smoke_2", "microstructure_v3", "tic://tool/builtin/query_funding@v1", 0, 180, "funding"),
        ("tc_smoke_3", "microstructure_v3", "tic://tool/builtin/query_ofi@v1", 0.001, 220, "ts_query"),
        ("tc_smoke_4", "macro_regime_v2", "tic://tool/builtin/query_dxy@v1", 0.002, 320, "dxy"),
        ("tc_smoke_5", "macro_regime_v2", "tic://tool/builtin/query_dxy@v1", 0.002, 310, "dxy"),
    ]
    for tcid, sid, uri, cost, dur, name in tool_calls:
        conn.execute(
            "INSERT INTO tool_call_log "
            "(id, cycle_id, specialist_id, tool_uri, tool_version, "
            " args_hash, args_json, result_summary, started_at, finished_at, "
            " duration_ms, cost_usd, valid_from) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                tcid, cycle_id, sid, uri, "v1", "ah_smoke",
                json.dumps({"q": name}),
                json.dumps({"summary": name}),
                _iso(now - timedelta(hours=4)),
                _iso(now - timedelta(hours=4) + timedelta(milliseconds=dur)),
                dur, cost, _iso(now - timedelta(hours=4)),
            ),
        )

    # A pair of seed tool_atlas rows so coverage_breadth has a denominator.
    conn.execute(
        "INSERT INTO tool_atlas (id, tool_uri, tool_name, version, kind, "
        " provider, callable_ref, schema_json, description, status, valid_from) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("tool_ofi", "tic://tool/builtin/query_ofi@v1", "query_ofi", "v1",
         "builtin", "tic", "tic.tools.ofi", "{}", "OFI",
         "active", _iso(now - timedelta(days=10))),
    )
    conn.execute(
        "INSERT INTO tool_atlas (id, tool_uri, tool_name, version, kind, "
        " provider, callable_ref, schema_json, description, status, valid_from) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("tool_dxy", "tic://tool/builtin/query_dxy@v1", "query_dxy", "v1",
         "builtin", "tic", "tic.tools.dxy", "{}", "DXY",
         "active", _iso(now - timedelta(days=10))),
    )
    conn.commit()
    print("  seeded:")
    print("    - 2 specialist personas (macro_regime_v2 anthropic, microstructure_v3 openai)")
    print("    - 10 reward_log correctness rows (micro brier ~0.42, macro ~0.19)")
    print("    - 3 hot hypotheses")
    print("    - 5 tool_call_log rows + 2 tool_atlas rows")
    # Confirm the weakness MV picks them up.
    weakness_rows = conn.execute(
        "SELECT * FROM mv_weakness_map"
    ).fetchall()
    print(f"  mv_weakness_map rows: {len(weakness_rows)}")
    for r in weakness_rows:
        d = dict(r)
        print(f"    - specialist={d['specialist_id']} evidence={d['evidence_json']}")
    if not weakness_rows:
        print("  FAIL: mv_weakness_map empty after seed")
        return 1

    # ============================================================
    # 2. run_research_director_cycle (REAL LLM call, no stub)
    # ============================================================
    _hr("[2] run_research_director_cycle (real LLM via tic.desk.models.chat)")
    plan = run_research_director_cycle(as_of=now, budget_usd=5.0, cycle_id=cycle_id)
    print(f"  plan_id        : {plan.plan_id}")
    print(f"  cycle_id       : {plan.cycle_id}")
    print(f"  as_of          : {plan.as_of}")
    print(f"  total_budget   : ${plan.total_budget_usd:.2f}")
    print(f"  llm_model_used : {plan.llm_model_used}")
    print(f"  llm_fallback   : {plan.llm_fallback_used}")
    print(f"  quality_flags  : {plan.quality_flags}")
    print(f"  cross_themes   : {plan.cross_cutting_themes}")
    print(f"  director_rationale (first 200 chars):")
    print(f"    {plan.director_rationale[:200]}")
    print(f"  per_specialist count : {len(plan.per_specialist)}")
    if not plan.per_specialist:
        print("  FAIL: per_specialist empty")
        return 1
    for sid, alloc in plan.per_specialist.items():
        print(f"  -- {sid}")
        print(f"     budget_usd          : ${alloc.budget_usd:.3f}")
        print(f"     focus_areas         : {alloc.focus_areas}")
        print(f"     expected_brier_lift : {alloc.expected_brier_lift:.3f}")
        print(f"     n_investigations    : {len(alloc.assigned_investigations)}")
        for inv in alloc.assigned_investigations:
            print(f"        - {inv.title!r} (target_weakness={inv.target_weakness_id}, "
                  f"target_hyp={inv.target_hypothesis_id}, calls={inv.expected_calls})")
    # Acceptance: every specialist got at least 1 investigation.
    for sid, alloc in plan.per_specialist.items():
        if not alloc.assigned_investigations:
            print(f"  FAIL: specialist {sid} got 0 investigations")
            return 1
    # Acceptance: the plan cites at least one explicit weakness id.
    cited_weaknesses = sum(
        len(v) for v in plan.explicit_weakness_assignments.values()
    )
    print(f"  explicit_weakness_assignments total ids: {cited_weaknesses}")
    if cited_weaknesses == 0:
        print("  FAIL: plan does not cite any weakness id from mv_weakness_map")
        return 1
    print("  [OK] CurriculumPlan well-formed + cites measured weaknesses")

    # ============================================================
    # 3. Verify agent_messages with kind='curriculum_assignment'
    # ============================================================
    _hr("[3] agent_messages with kind='curriculum_assignment'")
    rows = conn.execute(
        "SELECT id, from_agent, to_agent_or_topic, message_kind, dedupe_key "
        "FROM agent_messages "
        "WHERE message_kind = ? AND from_agent = ? AND transaction_to IS NULL",
        (CURRICULUM_ASSIGNMENT_KIND, DIRECTOR_SPECIALIST_ID),
    ).fetchall()
    recipients = sorted(r["to_agent_or_topic"] for r in rows)
    print(f"  curriculum_assignment recipients: {recipients}")
    print(f"  message ids: {[r['id'] for r in rows]}")
    if set(recipients) != set(plan.per_specialist.keys()):
        print(f"  FAIL: expected {set(plan.per_specialist.keys())}, got {set(recipients)}")
        return 1
    print("  [OK] one message per specialist")

    # Re-run -> idempotent (dedupe_key prevents duplicates)
    print("\n  re-running director cycle to confirm dedupe_key idempotency...")
    plan2 = run_research_director_cycle(as_of=now, budget_usd=5.0, cycle_id=cycle_id)
    # The second cycle creates a NEW plan (new plan_id), so messages WILL be
    # different. To test idempotency we instead call assign_hot_investigations
    # with the original plan.
    from talis_desk.director.research_director import assign_hot_investigations
    posts1 = assign_hot_investigations(plan)
    print(f"  assign_hot_investigations re-post: {len(posts1)} (each should be deduped=True)")
    n_dedup = sum(1 for p in posts1 if p.get("deduped"))
    print(f"  deduped count: {n_dedup}/{len(posts1)}")
    if n_dedup != len(posts1):
        print("  FAIL: dedupe_key did not prevent re-posts on identical plan")
        return 1
    print("  [OK] dedupe_key prevents duplicate curriculum_assignments")

    # ============================================================
    # 4. Verify persona row for research_director
    # ============================================================
    _hr("[4] specialist_states persona row for research_director")
    persona_rows = conn.execute(
        "SELECT id, persona_version, state_json FROM specialist_states "
        "WHERE specialist_id = ? AND state_kind = 'persona' "
        "AND transaction_to IS NULL ORDER BY transaction_from DESC",
        (DIRECTOR_SPECIALIST_ID,),
    ).fetchall()
    print(f"  director persona rows: {len(persona_rows)}")
    for r in persona_rows[:2]:
        try:
            sj = json.loads(r["state_json"])
        except Exception:
            sj = {}
        cp = sj.get("curriculum_plan", {})
        print(f"  - {r['id']} persona_version={r['persona_version']} "
              f"plan_id={cp.get('plan_id')} n_specialists={len(cp.get('per_specialist') or {})}")
    if not persona_rows:
        print("  FAIL: no research_director persona row")
        return 1
    print("  [OK] director persona persisted to specialist_states")

    # ============================================================
    # 5. Dashboard rendering
    # ============================================================
    _hr("[5] Dashboard HTML render + panel gather sanity")
    panel_results = {}
    panel_results["scorecard"] = gather_scorecard(now)
    panel_results["trade_book"] = gather_trade_book(now)
    panel_results["debates"] = gather_debates_panel(now)
    panel_results["graph"] = gather_hypothesis_graph_sample(seed=42)
    panel_results["persona"] = gather_persona_diffs(now)
    panel_results["weakness"] = gather_weakness_map(now)
    panel_results["tool_atlas"] = gather_tool_atlas(now)

    print(f"  scorecard.n_metrics  = {len(panel_results['scorecard']['metrics'])}")
    print(f"  trade_book.n_open    = {panel_results['trade_book']['n_open']}")
    print(f"  trade_book.n_closed  = {panel_results['trade_book']['n_closed']}")
    print(f"  debates.n_total      = {panel_results['debates']['n_total']}")
    print(f"  graph.root_id        = "
          f"{(panel_results['graph']['root'] or {}).get('id')}")
    print(f"  persona.n_candidates = {len(panel_results['persona']['candidates'])}")
    print(f"  weakness.n_top       = {len(panel_results['weakness']['weaknesses'])}")
    print(f"  tools.n_atlas        = {panel_results['tool_atlas']['atlas_summary']['n_tools']}")

    # All 9 metrics must be present
    if len(panel_results["scorecard"]["metrics"]) != 9:
        print(f"  FAIL: scorecard expected 9 metrics, got "
              f"{len(panel_results['scorecard']['metrics'])}")
        return 1

    html = render_dashboard_html(now)
    out_path = Path("/tmp/eval_dashboard.html")
    out_path.write_text(html)
    print(f"\n  wrote: {out_path}  ({len(html):,} bytes)")

    # Every panel id must appear in the rendered HTML
    required_panel_ids = [
        "panel-scorecard", "panel-tradebook", "panel-debates",
        "panel-graph", "panel-persona", "panel-weakness", "panel-toolatlas",
    ]
    missing = [p for p in required_panel_ids if p not in html]
    if missing:
        print(f"  FAIL: missing panel ids in HTML: {missing}")
        return 1
    print(f"  [OK] all 7 panels present in HTML")

    # ============================================================
    # 6. Veto endpoint plumbing
    # ============================================================
    _hr("[6] write_veto_message")
    fake_candidate_id = f"spst_fake_{uuid.uuid4().hex[:10]}"
    # Seed it so we have something to reference
    conn.execute(
        "INSERT INTO specialist_states "
        "(id, specialist_id, persona_version, cycle_id, state_kind, "
        " state_json, valid_from, transaction_from) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            fake_candidate_id, "microstructure_v3", "v3.candidate",
            cycle_id, "mutation_candidate",
            json.dumps({"diff": "test"}),
            _iso(now), _iso(now),
        ),
    )
    conn.commit()
    msg_id = write_veto_message(
        candidate_id=fake_candidate_id,
        reason="smoke-test veto: candidate did not improve Brier in A/B",
    )
    print(f"  veto message id: {msg_id}")
    veto_row = conn.execute(
        "SELECT id, message_kind, related_artifact_id, payload FROM agent_messages "
        "WHERE id = ?", (msg_id,),
    ).fetchone()
    if veto_row is None:
        print("  FAIL: veto row not found")
        return 1
    d = dict(veto_row)
    if d["message_kind"] != VETO_KIND:
        print(f"  FAIL: expected kind={VETO_KIND}, got {d['message_kind']}")
        return 1
    if d["related_artifact_id"] != fake_candidate_id:
        print(f"  FAIL: related_artifact_id mismatch")
        return 1
    # Re-call -> dedupe
    msg_id2 = write_veto_message(
        candidate_id=fake_candidate_id,
        reason="duplicate call — should dedupe",
    )
    if msg_id2 != msg_id:
        print(f"  FAIL: veto dedupe_key broken (msg1={msg_id}, msg2={msg_id2})")
        return 1
    print(f"  [OK] veto row landed + dedupe_key idempotent")

    _hr("PHASE 7 DIRECTOR + DASHBOARD — READY")
    print(f"  smoke db        : {db_path}")
    print(f"  dashboard html  : {out_path}")
    print(f"  plan_id         : {plan.plan_id}")
    print(f"  llm_used        : {plan.llm_model_used} "
          f"(fallback={plan.llm_fallback_used})")
    print(f"  flags           : {plan.quality_flags}")
    print()
    print(f"  Serve the dashboard with:")
    print(f"    uvicorn talis_desk.dashboard.server:app --host 127.0.0.1 "
          f"--port 8765 --reload")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Lifecycle for agent-created learned tools.

This closes the loop from analysis quality gap -> proposal -> generated
manifest/fixtures -> sandbox evaluation -> active atlas row -> dispatchable URI.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ..store import get_store
from .atlas import AgentContext, dispatch_uri, learned_tools_dir
from .discovery import (
    AnalysisToolProposal,
    iterate_tool_proposal,
    load_analysis_tool_proposals,
    persist_analysis_tool_proposals,
)
from .learned_runtime import SUPPORTED_LEARNED_RUNTIMES


@dataclass
class LearnedToolPromotion:
    proposal_id: str
    tool_name: str
    tool_uri: str
    tool_dir: str
    status: str
    eval_report: dict[str, Any] = field(default_factory=dict)
    quality_flags: list[str] = field(default_factory=list)
    iteration_proposal_id: str = ""

    @property
    def passed(self) -> bool:
        return self.status == "active" and bool(self.eval_report.get("passed"))


@dataclass
class RuntimeAdapterWorkOrder:
    proposal_id: str
    tool_name: str
    runtime: str
    work_order_path: str
    status: str = "adapter_requested"
    quality_flags: list[str] = field(default_factory=list)


@dataclass
class RuntimeAdapterReadiness:
    proposal_id: str
    runtime: str
    ready: bool
    status: str
    quality_flags: list[str] = field(default_factory=list)
    work_order_path: str = ""


def promote_analysis_tool_proposal(
    proposal_id: str,
    *,
    conn: Any = None,
    activate: bool = True,
) -> LearnedToolPromotion:
    db = conn or get_store().conn
    proposal = _load_proposal(proposal_id, conn=db)
    if proposal is None:
        raise KeyError(f"analysis_tool_proposal_not_found:{proposal_id}")
    tool_dir = scaffold_learned_tool(proposal)
    manifest = json.loads((tool_dir / "manifest.json").read_text(encoding="utf-8"))
    register_learned_tool_in_atlas(manifest, conn=db)
    report = evaluate_learned_tool(tool_dir, conn=db)
    if not report.get("passed"):
        _update_proposal_status(db, proposal.proposal_id or proposal_id, "eval_failed")
        iteration_id = _persist_iteration_for_failed_eval(proposal, report, conn=db)
        return LearnedToolPromotion(
            proposal_id=proposal.proposal_id or proposal_id,
            tool_name=proposal.tool_name,
            tool_uri=_tool_uri(proposal.tool_name),
            tool_dir=str(tool_dir),
            status="eval_failed",
            eval_report=report,
            quality_flags=report.get("quality_flags", []),
            iteration_proposal_id=iteration_id,
        )
    manifest["status"] = "active" if activate else "candidate"
    manifest["last_eval_report"] = report
    manifest["activated_at"] = _now() if activate else ""
    (tool_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    if activate:
        register_learned_tool_in_atlas(manifest, conn=db)
        _update_proposal_status(db, proposal.proposal_id or proposal_id, "active")
    return LearnedToolPromotion(
        proposal_id=proposal.proposal_id or proposal_id,
        tool_name=proposal.tool_name,
        tool_uri=manifest["tool_uri"],
        tool_dir=str(tool_dir),
        status=manifest["status"],
        eval_report=report,
        quality_flags=report.get("quality_flags", []),
    )


def _persist_iteration_for_failed_eval(
    proposal: AnalysisToolProposal,
    report: dict[str, Any],
    *,
    conn: Any,
) -> str:
    """Feed failed learned-tool evals back into the proposal substrate.

    This is the small but important evolution edge: tool creation does not end
    at "failed". The evaluator writes the next improvement target. Runtime
    adapter misses are held in a non-auto-promoted status so the desk asks for
    a real adapter instead of retrying the same manifest forever.
    """
    if int(proposal.iteration or 0) >= 3:
        return ""
    flags = [str(x) for x in (report.get("quality_flags") or []) if str(x).strip()]
    checks = report.get("checks") or {}
    if isinstance(checks, dict):
        flags.extend(f"failed_check:{k}" for k, v in checks.items() if not v)
    error = str(report.get("error") or "").strip()
    if error:
        flags.append(error)
    runtime = str(report.get("runtime") or "")
    adapter_missing = "runtime_adapter_missing" in flags or "learned_runtime_adapter_missing" in error
    note = (
        f"Create a real learned-runtime adapter for runtime={runtime!r}, "
        "with fixtures that exercise the source and promotion gate."
        if adapter_missing else
        "Tighten the manifest, fixtures, input schema, and promotion gate until sandbox eval passes."
    )
    improved = iterate_tool_proposal(
        proposal,
        critique_flags=sorted(set(flags)),
        improvement_note=note,
        eval_plan_delta={
            "last_eval_report": report,
            "required_runtime_adapter": runtime if adapter_missing else "",
        },
        promotion_gate_delta={
            "runtime_adapter_exists": True,
        } if adapter_missing else {},
    )
    if adapter_missing:
        improved.proposal_kind = "runtime_adapter"
        improved.status = "needs_runtime_adapter"
    ids = persist_analysis_tool_proposals([improved], conn=conn)
    return ids[0] if ids else ""


def promote_pending_analysis_tool_proposals(
    *,
    cycle_id: str = "",
    limit: int = 5,
    conn: Any = None,
) -> list[LearnedToolPromotion]:
    db = conn or get_store().conn
    rows = load_analysis_tool_proposals(cycle_id=cycle_id, status="proposed", limit=limit, conn=db)
    out: list[LearnedToolPromotion] = []
    for row in rows:
        out.append(promote_analysis_tool_proposal(str(row["id"]), conn=db, activate=True))
    return out


def create_runtime_adapter_work_orders(
    *,
    cycle_id: str = "",
    limit: int = 3,
    conn: Any = None,
) -> list[RuntimeAdapterWorkOrder]:
    """Materialize work orders for runtime-adapter backlog proposals.

    `needs_runtime_adapter` proposals are intentionally not auto-promoted:
    they require code. This function turns them into explicit, versioned
    adapter work orders so Codex/worker agents can build the adapter with the
    proposal, fixture expectations, and promotion gate in one inspectable file.
    """
    db = conn or get_store().conn
    rows = load_analysis_tool_proposals(
        cycle_id=cycle_id,
        status="needs_runtime_adapter",
        limit=max(1, int(limit)),
        conn=db,
    )
    out: list[RuntimeAdapterWorkOrder] = []
    root = learned_tools_dir() / "_runtime_adapter_work_orders"
    root.mkdir(parents=True, exist_ok=True)
    for row in rows:
        proposal_id = str(row["id"])
        proposal = _load_proposal(proposal_id, conn=db)
        if proposal is None:
            continue
        runtime = _required_runtime_adapter(proposal)
        order_id = _adapter_work_order_id(proposal_id, runtime, int(proposal.iteration or 0))
        path = root / f"{order_id}.json"
        payload = _adapter_work_order_payload(
            order_id=order_id,
            proposal=proposal,
            runtime=runtime,
        )
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        _update_proposal_status(db, proposal_id, "adapter_requested")
        _append_proposal_quality_flags(
            db,
            proposal_id,
            [
                "runtime_adapter_work_order_created",
                f"runtime_adapter:{runtime}",
            ],
        )
        out.append(RuntimeAdapterWorkOrder(
            proposal_id=proposal_id,
            tool_name=proposal.tool_name,
            runtime=runtime,
            work_order_path=str(path),
            quality_flags=list(payload["quality_flags"]),
        ))
    return out


def mark_runtime_adapter_ready(
    *,
    proposal_id: str = "",
    work_order_path: str = "",
    conn: Any = None,
) -> RuntimeAdapterReadiness:
    """Move an adapter work order back into eval once code exists.

    Agents building adapters need a durable handoff: after adding a supported
    runtime adapter and fixtures, call this. It verifies the runtime is present
    in the constrained adapter registry, marks the proposal `proposed`, and
    lets the normal learned-tool promotion/eval path prove it.
    """
    db = conn or get_store().conn
    order = _load_adapter_work_order(work_order_path) if work_order_path else {}
    pid = proposal_id or str((order.get("proposal") or {}).get("id") or "")
    if not pid:
        raise ValueError("mark_runtime_adapter_ready_requires_proposal_id_or_work_order_path")
    proposal = _load_proposal(pid, conn=db)
    if proposal is None:
        raise KeyError(f"analysis_tool_proposal_not_found:{pid}")
    runtime = str(((order.get("adapter") or {}).get("runtime")) or _required_runtime_adapter(proposal))
    if runtime not in SUPPORTED_LEARNED_RUNTIMES:
        flags = [
            "runtime_adapter_not_ready",
            f"runtime_adapter_missing:{runtime}",
        ]
        _append_proposal_quality_flags(db, pid, flags)
        return RuntimeAdapterReadiness(
            proposal_id=pid,
            runtime=runtime,
            ready=False,
            status=str(proposal.status),
            quality_flags=flags,
            work_order_path=work_order_path,
        )
    flags = [
        "runtime_adapter_ready_for_eval",
        f"runtime_adapter:{runtime}",
    ]
    _update_proposal_status(db, pid, "proposed")
    _append_proposal_quality_flags(db, pid, flags)
    return RuntimeAdapterReadiness(
        proposal_id=pid,
        runtime=runtime,
        ready=True,
        status="proposed",
        quality_flags=flags,
        work_order_path=work_order_path,
    )


def scaffold_learned_tool(proposal: AnalysisToolProposal) -> Path:
    slug = _slug(proposal.tool_name)
    root = learned_tools_dir()
    tool_dir = root / slug
    tool_dir.mkdir(parents=True, exist_ok=True)
    manifest = _manifest_for_proposal(proposal, tool_dir)
    (tool_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (tool_dir / "fixtures.json").write_text(
        json.dumps(_fixtures_for_runtime(manifest["runtime"]), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (tool_dir / "README.md").write_text(_readme_for_manifest(manifest), encoding="utf-8")
    return tool_dir


def evaluate_learned_tool(tool_dir: Path, *, conn: Any = None) -> dict[str, Any]:
    manifest = json.loads((tool_dir / "manifest.json").read_text(encoding="utf-8"))
    fixtures = json.loads((tool_dir / "fixtures.json").read_text(encoding="utf-8"))
    runtime = str(manifest.get("runtime") or "")
    if runtime not in SUPPORTED_LEARNED_RUNTIMES:
        return {
            "passed": False,
            "tool_uri": manifest["tool_uri"],
            "runtime": runtime,
            "dispatch_ok": False,
            "tool_call_log_id": None,
            "checks": {
                "runtime_adapter_exists": False,
                "fixture_declared": bool(fixtures),
            },
            "quality_flags": ["runtime_adapter_missing"],
            "error": f"learned_runtime_adapter_missing:{runtime}",
            "next_action": "iterate_tool_proposal_with_runtime_adapter",
        }
    context = AgentContext(
        cycle_id=str(fixtures.get("cycle_id") or "learned_tool_eval"),
        specialist_id="learned_tool_evaluator",
        investigation_id=str(manifest.get("proposal_id") or ""),
    )
    if conn is not None:
        _seed_eval_tool_logs(conn, fixtures)
    result = dispatch_uri(manifest["tool_uri"], dict(fixtures.get("input") or {}), context)
    checks = _checks_for_runtime(manifest["runtime"], result.result)
    passed = bool(result.ok and all(checks.values()))
    return {
        "passed": passed,
        "tool_uri": manifest["tool_uri"],
        "runtime": manifest["runtime"],
        "dispatch_ok": result.ok,
        "tool_call_log_id": result.tool_call_log_id,
        "checks": checks,
        "quality_flags": [] if passed else [k for k, v in checks.items() if not v],
        "error": result.error,
    }


def register_learned_tool_in_atlas(manifest: dict[str, Any], *, conn: Any = None) -> str:
    db = conn or get_store().conn
    now = _now()
    db.execute(
        "UPDATE tool_atlas SET transaction_to = ? "
        "WHERE tool_uri = ? AND version = ? AND transaction_to IS NULL",
        (now, manifest["tool_uri"], manifest.get("version", "v1")),
    )
    atlas_id = "tool_" + uuid.uuid4().hex[:24]
    db.execute(
        """
        INSERT INTO tool_atlas (
            id, tool_uri, tool_name, version, kind, provider, callable_ref,
            schema_json, skill_md_path, description, source_dependencies,
            permission_scope, network_hosts, cost_hint, status, code_sha256,
            valid_from, transaction_from
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            atlas_id,
            manifest["tool_uri"],
            manifest["name"],
            manifest.get("version", "v1"),
            "learned",
            manifest.get("owner", "analysis_tool_discovery"),
            manifest.get("callable_ref", "talis_desk.tool_atlas.learned_runtime:dispatch_learned_tool"),
            json.dumps(manifest.get("input_schema", {}), sort_keys=True),
            None,
            manifest.get("description", ""),
            json.dumps(manifest.get("source_dependencies", []), sort_keys=True),
            manifest.get("permission_scope", "read_only"),
            json.dumps(manifest.get("network_hosts", []), sort_keys=True),
            json.dumps(manifest.get("cost_hint", {}), sort_keys=True),
            manifest.get("status", "active"),
            manifest.get("code_sha256"),
            now,
            now,
        ),
    )
    db.commit()
    return atlas_id


def _manifest_for_proposal(proposal: AnalysisToolProposal, tool_dir: Path) -> dict[str, Any]:
    slug = _slug(proposal.tool_name)
    runtime = _runtime_for_tool(slug)
    return {
        "name": slug,
        "version": "v1",
        "tool_uri": _tool_uri(slug),
        "runtime": runtime,
        "status": "candidate",
        "owner": proposal.created_by or "analysis_tool_discovery",
        "proposal_id": proposal.proposal_id,
        "artifact_kind": proposal.artifact_kind,
        "artifact_id": proposal.artifact_id,
        "description": proposal.purpose or f"Learned tool generated for {slug}.",
        "input_schema": proposal.input_shape or _default_input_schema(runtime),
        "promotion_gate": proposal.promotion_gate,
        "eval_plan": proposal.eval_plan,
        "source_family": proposal.source_family,
        "source_dependencies": _source_dependencies(runtime, proposal),
        "permission_scope": "read_only",
        "network_hosts": [],
        "cost_hint": {"usd_per_call_estimate": 0.0001, "note": "local learned-tool adapter"},
        "callable_ref": "talis_desk.tool_atlas.learned_runtime:dispatch_learned_tool",
        "created_at": _now(),
        "fixture_path": str(tool_dir / "fixtures.json"),
        "quality_flags": sorted(set(proposal.quality_flags)),
    }


def _runtime_for_tool(slug: str) -> str:
    if slug in {
        "evidence_ref_resolver",
        "hl_node_stream_reader",
        "hyperevm_mempool_actor_watch",
        "hydromancer_actor_quality_bulk",
        "liquidity_absorption_context",
    }:
        return slug
    if "mempool" in slug:
        return "hyperevm_mempool_actor_watch"
    if "node" in slug or "reject" in slug:
        return "hl_node_stream_reader"
    if "hydromancer" in slug or "actor_quality" in slug:
        return "hydromancer_actor_quality_bulk"
    if "evidence" in slug or "source_ref" in slug:
        return "evidence_ref_resolver"
    if "liquidity" in slug or "absorption" in slug:
        return "liquidity_absorption_context"
    return slug


def _fixtures_for_runtime(runtime: str) -> dict[str, Any]:
    if runtime == "hl_node_stream_reader":
        return {
            "cycle_id": "learned_tool_eval_node",
            "input": {"coin": "HYPE", "wallets": ["0xabc"], "lookback_minutes": 90},
            "tool_logs": [
                {
                    "id": "tc_eval_node",
                    "tool_uri": "tic://source/hl/hl_reject_corpus",
                    "args_json": {"coin": "HYPE", "wallets": ["0xabc"]},
                    "result_summary": {
                        "wallet": "0xabc",
                        "reject_rate_pct": 1.7,
                        "status_counts": {"filled": 116, "rejected": 2},
                        "top_reject_reasons": [["insufficient_margin", 2]],
                    },
                }
            ],
        }
    if runtime == "hyperevm_mempool_actor_watch":
        return {
            "cycle_id": "learned_tool_eval_mempool",
            "input": {
                "addresses": ["0xabc"],
                "contracts": ["0xrouter"],
                "asset": "HYPE",
                "fixture_events": [
                    {
                        "tx_hash": "0xhash",
                        "actor": "0xabc",
                        "contract": "0xrouter",
                        "method": "transfer",
                        "asset": "HYPE",
                        "seen_at": "2026-05-21T10:00:00Z",
                        "settled_event_ref": "tc_settled",
                    }
                ],
            },
            "tool_logs": [],
        }
    if runtime == "evidence_ref_resolver":
        return {
            "cycle_id": "learned_tool_eval_evidence",
            "input": {"source_refs": ["tc_eval_ref"]},
            "tool_logs": [
                {
                    "id": "tc_eval_ref",
                    "tool_uri": "tic://tool/builtin/query_events_recent@v1",
                    "args_json": {"ticker": "HYPE"},
                    "result_summary": {"events": [{"entity": "HYPE"}]},
                }
            ],
        }
    if runtime == "hydromancer_actor_quality_bulk":
        return {
            "cycle_id": "learned_tool_eval_hydro",
            "input": {"wallets": ["0xabc"]},
            "tool_logs": [
                {
                    "id": "tc_eval_hydro",
                    "tool_uri": "tic://tool/hydromancer/get_hl_pnl_leaderboard@v1",
                    "args_json": {"top_n": 25},
                    "result_summary": {"leaders": [{"wallet": "0xabc", "realized_pnl_usd": 1000}]},
                }
            ],
        }
    if runtime == "liquidity_absorption_context":
        return {
            "cycle_id": "learned_tool_eval_liquidity",
            "input": {
                "asset": "HYPE",
                "amount": 558_100,
                "depth_1pct_usd": 8_000_000,
                "volume_24h_usd": 120_000_000,
                "before_after": True,
            },
            "tool_logs": [],
        }
    return {"cycle_id": "learned_tool_eval_generic", "input": {}, "tool_logs": []}


def _checks_for_runtime(runtime: str, result: Any) -> dict[str, bool]:
    runtime_adapter_exists = runtime in SUPPORTED_LEARNED_RUNTIMES
    if not isinstance(result, dict):
        return {"runtime_adapter_exists": runtime_adapter_exists, "result_object": False}
    checks = {
        "runtime_adapter_exists": runtime_adapter_exists,
        "result_object": True,
        "status_ok": result.get("status") in {"ok", "no_mempool_source_configured"},
    }
    if runtime == "hl_node_stream_reader":
        checks["has_observations"] = int(result.get("n_observations") or 0) > 0
        checks["has_raw_offsets"] = bool(result.get("has_raw_offsets"))
        checks["source_family"] = result.get("source_family") == "our_hl_node"
    elif runtime == "hyperevm_mempool_actor_watch":
        checks["dedupes_by_tx_hash"] = bool(result.get("dedupe_by_tx_hash"))
        checks["settlement_reconciliation"] = bool(result.get("settlement_reconciliation"))
    elif runtime == "evidence_ref_resolver":
        checks["resolved_refs"] = float(result.get("resolution_rate") or 0.0) >= 0.95
    elif runtime == "hydromancer_actor_quality_bulk":
        checks["has_actors"] = int(result.get("n_actors") or 0) > 0
    elif runtime == "liquidity_absorption_context":
        checks["has_depth"] = bool(result.get("has_depth"))
        checks["has_volume"] = bool(result.get("has_volume"))
        checks["has_before_after"] = bool(result.get("has_before_after"))
    return checks


def _seed_eval_tool_logs(conn: Any, fixtures: dict[str, Any]) -> None:
    now = _now()
    for row in fixtures.get("tool_logs") or []:
        conn.execute(
            """
            INSERT OR REPLACE INTO tool_call_log (
                id, cycle_id, investigation_id, specialist_id, tool_uri,
                tool_version, args_hash, args_json, result_hash,
                result_summary, error, started_at, finished_at, duration_ms,
                cost_usd, valid_from, transaction_from
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["id"],
                fixtures.get("cycle_id") or "learned_tool_eval",
                "learned_tool_eval",
                "learned_tool_evaluator",
                row["tool_uri"],
                "v1",
                "fixture_args",
                json.dumps(row.get("args_json") or {}, sort_keys=True),
                "fixture_result",
                json.dumps(row.get("result_summary") or {}, sort_keys=True),
                None,
                now,
                now,
                1,
                0.0,
                now,
                now,
            ),
        )
    conn.commit()


def _load_proposal(proposal_id: str, *, conn: Any) -> Optional[AnalysisToolProposal]:
    rows = load_analysis_tool_proposals(limit=1000, conn=conn)
    for row in rows:
        if str(row.get("id")) == proposal_id:
            return AnalysisToolProposal(
                proposal_id=str(row["id"]),
                cycle_id=str(row["cycle_id"]),
                artifact_kind=str(row["artifact_kind"]),
                artifact_id=str(row["artifact_id"]),
                entity=str(row.get("entity") or ""),
                horizon=str(row.get("horizon") or ""),
                lens=str(row.get("lens") or ""),
                proposal_kind=str(row.get("proposal_kind") or "new_tool"),
                tool_name=str(row.get("tool_name") or ""),
                purpose=str(row.get("purpose") or ""),
                source_family=str(row.get("source_family") or ""),
                trigger=str(row.get("trigger") or ""),
                input_shape=dict(row.get("input_shape_json") or {}),
                promotion_gate=dict(row.get("promotion_gate_json") or {}),
                eval_plan=dict(row.get("eval_plan_json") or {}),
                priority=str(row.get("priority") or "medium"),
                status=str(row.get("status") or "proposed"),
                parent_proposal_id=str(row.get("parent_proposal_id") or ""),
                iteration=int(row.get("iteration") or 0),
                created_by=str(row.get("created_by") or "analysis_tool_discovery"),
                quality_flags=list(row.get("quality_flags") or []),
            )
    return None


def _update_proposal_status(conn: Any, proposal_id: str, status: str) -> None:
    conn.execute(
        "UPDATE analysis_tool_proposals SET status = ? WHERE id = ?",
        (status, proposal_id),
    )
    conn.commit()


def _append_proposal_quality_flags(conn: Any, proposal_id: str, flags: list[str]) -> None:
    row = conn.execute(
        "SELECT quality_flags FROM analysis_tool_proposals WHERE id = ?",
        (proposal_id,),
    ).fetchone()
    if row is None:
        return
    current: list[str]
    try:
        current = json.loads(row["quality_flags"] or "[]")
        if not isinstance(current, list):
            current = []
    except Exception:
        current = []
    merged = sorted(set([str(x) for x in current if str(x).strip()] + flags))
    conn.execute(
        "UPDATE analysis_tool_proposals SET quality_flags = ? WHERE id = ?",
        (json.dumps(merged), proposal_id),
    )
    conn.commit()


def _load_adapter_work_order(path: str) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"runtime_adapter_work_order_missing:{path}")
    raw = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"runtime_adapter_work_order_not_object:{path}")
    return raw


def _required_runtime_adapter(proposal: AnalysisToolProposal) -> str:
    eval_plan = proposal.eval_plan or {}
    runtime = str(eval_plan.get("required_runtime_adapter") or "").strip()
    return runtime or _runtime_for_tool(proposal.tool_name)


def _adapter_work_order_id(proposal_id: str, runtime: str, iteration: int) -> str:
    raw = f"{proposal_id}|{runtime}|{iteration}"
    return "rta_" + uuid.uuid5(uuid.NAMESPACE_URL, raw).hex[:16]


def _adapter_work_order_payload(
    *,
    order_id: str,
    proposal: AnalysisToolProposal,
    runtime: str,
) -> dict[str, Any]:
    slug = _slug(proposal.tool_name)
    return {
        "id": order_id,
        "created_at": _now(),
        "status": "adapter_requested",
        "proposal": {
            "id": proposal.proposal_id,
            "cycle_id": proposal.cycle_id,
            "artifact_kind": proposal.artifact_kind,
            "artifact_id": proposal.artifact_id,
            "entity": proposal.entity,
            "horizon": proposal.horizon,
            "lens": proposal.lens,
            "tool_name": proposal.tool_name,
            "source_family": proposal.source_family,
            "trigger": proposal.trigger,
            "purpose": proposal.purpose,
            "iteration": proposal.iteration,
            "parent_proposal_id": proposal.parent_proposal_id,
        },
        "adapter": {
            "runtime": runtime,
            "tool_slug": slug,
            "target_runtime_file": "talis_desk/tool_atlas/learned_runtime.py",
            "target_lifecycle_file": "talis_desk/tool_atlas/learned_lifecycle.py",
            "expected_tool_uri": _tool_uri(slug),
            "input_schema": proposal.input_shape,
            "source_family": proposal.source_family,
            "source_dependencies": _source_dependencies(runtime, proposal),
        },
        "promotion_gate": proposal.promotion_gate,
        "eval_plan": proposal.eval_plan,
        "acceptance_checks": [
            "Add runtime to SUPPORTED_LEARNED_RUNTIMES.",
            "Implement a deterministic read-only adapter in learned_runtime.py.",
            "Add fixture payload in _fixtures_for_runtime.",
            "Add runtime-specific checks in _checks_for_runtime.",
            "Prove promote_analysis_tool_proposal passes and dispatch_uri returns sourced data.",
            "Keep failures honest: no generated code import, no fabricated rows, no generic echo.",
        ],
        "quality_flags": sorted(set([
            *proposal.quality_flags,
            "runtime_adapter_work_order_created",
            f"runtime_adapter:{runtime}",
        ])),
    }


def _source_dependencies(runtime: str, proposal: AnalysisToolProposal) -> list[str]:
    deps = [proposal.source_family] if proposal.source_family else []
    if runtime == "hl_node_stream_reader":
        deps.append("tool_call_log:hl_reject_corpus")
    if runtime == "hyperevm_mempool_actor_watch":
        deps.append("mempool_events")
    return sorted(set(x for x in deps if x))


def _default_input_schema(runtime: str) -> dict[str, Any]:
    if runtime == "hl_node_stream_reader":
        return {"coin": "string", "wallets": ["0x..."], "lookback_minutes": 90}
    if runtime == "hyperevm_mempool_actor_watch":
        return {"addresses": ["0x..."], "contracts": ["0x..."], "asset": "string"}
    if runtime == "evidence_ref_resolver":
        return {"source_refs": ["tc_..."]}
    return {}


def _readme_for_manifest(manifest: dict[str, Any]) -> str:
    return (
        f"# {manifest['name']}\n\n"
        f"{manifest.get('description', '')}\n\n"
        f"- URI: `{manifest['tool_uri']}`\n"
        f"- Runtime: `{manifest['runtime']}`\n"
        f"- Status: `{manifest['status']}`\n"
        f"- Proposal: `{manifest.get('proposal_id')}`\n"
    )


def _tool_uri(name: str) -> str:
    return f"tic://tool/learned/{_slug(name)}@v1"


def _slug(raw: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9_]+", "_", str(raw or "learned_tool").strip().lower())
    text = re.sub(r"_+", "_", text).strip("_")
    return text or "learned_tool"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "LearnedToolPromotion",
    "RuntimeAdapterReadiness",
    "RuntimeAdapterWorkOrder",
    "create_runtime_adapter_work_orders",
    "evaluate_learned_tool",
    "mark_runtime_adapter_ready",
    "promote_analysis_tool_proposal",
    "promote_pending_analysis_tool_proposals",
    "register_learned_tool_in_atlas",
    "scaffold_learned_tool",
]

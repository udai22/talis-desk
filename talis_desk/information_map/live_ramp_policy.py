"""Executable policy patches derived from live scout learning.

The learning report is only useful if the next ramp can consume it. This module
keeps that handoff deliberately small: a report can emit a compact policy patch,
and the live scout harness applies it to seed payloads without deleting any
locally routed tool candidates or MarketEvolve metadata.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any


def load_live_scout_ramp_policy(path: str | Path) -> dict[str, Any]:
    """Load a JSON ramp policy from disk."""
    p = Path(path).expanduser().resolve()
    with p.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, dict):
        raise ValueError(f"ramp policy must be a JSON object: {p}")
    return raw


def apply_live_scout_ramp_policy_to_seeds(
    seeds: list[Any],
    policy: dict[str, Any],
) -> dict[str, Any]:
    """Apply a live-learning policy to scout seed payloads.

    The patch is monotonic by design: it can raise minimum standards, add watch
    metrics, add source-family targets, and annotate geometry targets. It does
    not remove existing tools, replace MarketEvolve arms, or force a single
    brittle route through the market.
    """
    if not isinstance(policy, dict) or not seeds:
        return {
            "schema_version": "live_scout_ramp_policy_application_v1",
            "status": "empty",
            "seed_count": len(seeds),
            "policy_id": "",
            "applied_seed_count": 0,
            "quality_flags": ["no_policy_or_no_seeds"],
        }
    seed_patch = policy.get("seed_payload_patch") if isinstance(policy.get("seed_payload_patch"), dict) else {}
    watch_metrics = _string_list(policy.get("watch_metrics"))
    repair_ids = _string_list(policy.get("repair_work_order_ids"))
    prompt_modes = _string_list(policy.get("prompt_repair_modes"))
    source_targets = _string_list(seed_patch.get("source_family_targets_append"))
    geometry_targets = [
        target for target in (policy.get("geometry_replication_targets") or [])
        if isinstance(target, dict)
    ]
    applied = 0
    geometry_annotated = 0
    refreshed_tool_seed_count = 0
    added_tool_count = 0
    refresh_errors: list[str] = []
    for seed in seeds:
        payload = dict(getattr(seed, "payload", None) or {})
        payload["learning_policy_id"] = str(policy.get("policy_id") or "")
        payload["learning_policy_source"] = str(policy.get("source") or "live_scout_learning_report")
        if watch_metrics:
            payload["learning_watch_metrics"] = _merge_lists(payload.get("learning_watch_metrics"), watch_metrics)
        if repair_ids:
            payload["learning_repair_work_order_ids"] = _merge_lists(payload.get("learning_repair_work_order_ids"), repair_ids)
        if prompt_modes:
            payload["prompt_repair_modes"] = _merge_lists(payload.get("prompt_repair_modes"), prompt_modes)
        _raise_string(payload, "prompt_contract_pressure", seed_patch.get("prompt_contract_pressure"))
        _raise_int(payload, "prompt_min_information_strings", seed_patch.get("prompt_min_information_strings"), cap=3)
        _raise_int(payload, "max_tool_iterations", seed_patch.get("max_tool_iterations"), cap=2)
        _raise_int(payload, "max_evidence_tools", seed_patch.get("max_evidence_tools_min"), cap=8)
        _raise_int(payload, "tool_candidate_limit", seed_patch.get("tool_candidate_limit_min"), cap=16)
        for key in (
            "prompt_require_mechanism",
            "prompt_require_kill_signal",
            "prompt_require_evidence_refs",
            "suggested_tool_allowlist_only",
            "preserve_missing_tool_as_proposal",
            "stale_evidence_becomes_gap_string",
            "quarantine_before_verifier_spend",
        ):
            if key in seed_patch:
                payload[key] = bool(seed_patch.get(key))
        if source_targets:
            payload["source_family_targets"] = _merge_lists(payload.get("source_family_targets"), source_targets)
        matching_targets = _matching_geometry_targets(seed, geometry_targets)
        if matching_targets:
            geometry_annotated += 1
            payload["learning_geometry_replication_targets"] = matching_targets[:3]
            payload["alpha_geometry_route_directive"] = payload.get("alpha_geometry_route_directive") or "replicate_or_falsify"
            payload["alpha_geometry_action"] = payload.get("alpha_geometry_action") or "independent_replication"
            payload["alpha_geometry_success_gate"] = payload.get("alpha_geometry_success_gate") or (
                "Independent scout confirms, contradicts, or kills the prior geometry edge."
            )
        setattr(seed, "payload", payload)
        refresh = _refresh_seed_tool_candidates(seed)
        if refresh.get("status") == "refreshed":
            refreshed_tool_seed_count += 1
            added_tool_count += int(refresh.get("added_count") or 0)
        elif refresh.get("status") == "error":
            refresh_errors.append(str(refresh.get("error") or "tool_candidate_refresh_failed"))
        applied += 1
    return {
        "schema_version": "live_scout_ramp_policy_application_v1",
        "status": "applied" if applied else "empty",
        "seed_count": len(seeds),
        "applied_seed_count": applied,
        "geometry_annotated_seed_count": geometry_annotated,
        "tool_candidate_refreshed_seed_count": refreshed_tool_seed_count,
        "tool_candidate_added_count": added_tool_count,
        "policy_id": str(policy.get("policy_id") or ""),
        "watch_metrics": watch_metrics,
        "repair_work_order_ids": repair_ids,
        "source_family_targets_added": source_targets,
        "quality_flags": [f"tool_candidate_refresh_error:{err}" for err in refresh_errors[:3]],
    }


def build_live_scout_ramp_policy_rehearsal(
    *,
    baseline_seeds: list[Any],
    candidate_seeds: list[Any],
    policy: dict[str, Any],
    application: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compare the same no-spend slice before/after a ramp policy patch."""
    application = application if isinstance(application, dict) else {}
    baseline_rows = [_seed_affordance_row(seed) for seed in baseline_seeds]
    candidate_rows = [_seed_affordance_row(seed) for seed in candidate_seeds]
    seed_count = max(len(candidate_rows), len(baseline_rows), 1)
    seed_patch = policy.get("seed_payload_patch") if isinstance(policy.get("seed_payload_patch"), dict) else {}
    watch_metrics = _string_list(policy.get("watch_metrics"))
    repair_ids = _string_list(policy.get("repair_work_order_ids"))
    target_families = _normalize_source_families(seed_patch.get("source_family_targets_append"))
    candidate_family_counts = Counter(
        family
        for row in candidate_rows
        for family in row.get("source_families", [])
    )
    baseline_family_counts = Counter(
        family
        for row in baseline_rows
        for family in row.get("source_families", [])
    )
    tool_deltas = []
    for idx, row in enumerate(candidate_rows):
        baseline = baseline_rows[idx] if idx < len(baseline_rows) else {}
        before = set(baseline.get("tool_candidates") or [])
        after = set(row.get("tool_candidates") or [])
        tool_deltas.append({
            "seed_id": row.get("seed_id"),
            "entity": row.get("entity"),
            "horizon": row.get("horizon"),
            "lens": row.get("lens"),
            "baseline_tool_count": len(before),
            "candidate_tool_count": len(after),
            "added_tools": sorted(after - before)[:12],
            "removed_tools": sorted(before - after)[:12],
            "source_families": row.get("source_families") or [],
        })
    policy_id = str(policy.get("policy_id") or "")
    policy_attached_rate = _rate(
        sum(1 for row in candidate_rows if row.get("learning_policy_id") == policy_id),
        seed_count,
    )
    watch_attached_rate = _rate(
        sum(1 for row in candidate_rows if set(watch_metrics).issubset(set(row.get("learning_watch_metrics") or []))),
        seed_count,
    ) if watch_metrics else 1.0
    repair_attached_rate = _rate(
        sum(1 for row in candidate_rows if set(repair_ids).issubset(set(row.get("learning_repair_work_order_ids") or []))),
        seed_count,
    ) if repair_ids else 1.0
    strict_contract_rate = _rate(
        sum(1 for row in candidate_rows if row.get("prompt_contract_pressure") in {"strict", "high", "raise"}),
        seed_count,
    )
    refreshed_rate = _rate(
        int(application.get("tool_candidate_refreshed_seed_count") or 0),
        seed_count,
    )
    target_hits = {
        family: int(candidate_family_counts.get(family, 0))
        for family in target_families
    }
    target_coverage_rate = _rate(sum(1 for hits in target_hits.values() if hits > 0), max(1, len(target_hits))) if target_hits else 1.0
    baseline_avg_tools = _avg(row.get("tool_candidate_count") for row in baseline_rows)
    candidate_avg_tools = _avg(row.get("tool_candidate_count") for row in candidate_rows)
    max_limit = int(seed_patch.get("tool_candidate_limit_min") or max([row.get("tool_candidate_count") or 0 for row in candidate_rows] or [0]) or 0)
    over_limit_count = sum(
        1 for row in candidate_rows
        if max_limit > 0 and int(row.get("tool_candidate_count") or 0) > max_limit
    )
    gates = {
        "policy_attached_all": policy_attached_rate >= 0.999,
        "watch_metrics_attached_all": watch_attached_rate >= 0.999,
        "repair_ids_attached_all": repair_attached_rate >= 0.999,
        "strict_contract_all": strict_contract_rate >= 0.999,
        "tool_refresh_all": refreshed_rate >= 0.999 if max_limit else True,
        "candidate_menu_not_smaller": candidate_avg_tools >= baseline_avg_tools,
        "source_target_coverage": target_coverage_rate >= (0.60 if target_hits else 1.0),
        "tool_overbreadth_ok": over_limit_count == 0,
    }
    score = round(sum(1.0 for ok in gates.values() if ok) / max(1, len(gates)), 4)
    status = "pass" if all(gates.values()) else "fail"
    return {
        "schema_version": "live_scout_ramp_policy_rehearsal_v1",
        "policy_id": policy_id,
        "status": status,
        "decision": "policy_can_gate_live_spend" if status == "pass" else "repair_policy_before_live_spend",
        "score": score,
        "seed_count": len(candidate_rows),
        "baseline": {
            "avg_tool_candidates": round(baseline_avg_tools, 3),
            "source_family_counts": dict(sorted(baseline_family_counts.items())),
        },
        "candidate": {
            "avg_tool_candidates": round(candidate_avg_tools, 3),
            "source_family_counts": dict(sorted(candidate_family_counts.items())),
        },
        "metrics": {
            "policy_attached_rate": policy_attached_rate,
            "watch_metrics_attached_rate": watch_attached_rate,
            "repair_ids_attached_rate": repair_attached_rate,
            "strict_contract_rate": strict_contract_rate,
            "tool_candidate_refresh_rate": refreshed_rate,
            "tool_candidate_added_count": int(application.get("tool_candidate_added_count") or 0),
            "source_target_coverage_rate": target_coverage_rate,
            "candidate_tool_delta_avg": round(candidate_avg_tools - baseline_avg_tools, 3),
            "over_limit_count": over_limit_count,
        },
        "target_source_family_hits": target_hits,
        "gates": gates,
        "sample_deltas": tool_deltas[:20],
        "application": application,
        "quality_flags": [] if status == "pass" else [
            f"failed_gate:{name}" for name, ok in gates.items() if not ok
        ],
    }


def _matching_geometry_targets(seed: Any, targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not targets:
        return []
    entity = str(getattr(seed, "entity", "") or "")
    horizon = str(getattr(seed, "horizon", "") or "")
    lens = str(getattr(seed, "lens", "") or "")
    out: list[dict[str, Any]] = []
    for target in targets:
        cell = str(target.get("cell_key") or "")
        parts = cell.split("|")
        if len(parts) >= 3:
            if parts[0] and parts[0] != entity:
                continue
            if parts[1] and parts[1] != horizon:
                continue
            if parts[2] and parts[2] != lens:
                continue
        out.append({
            "cell_key": target.get("cell_key"),
            "route_directive": target.get("route_directive"),
            "trade_scream_score": target.get("trade_scream_score"),
            "source_work_order_id": target.get("source_work_order_id"),
        })
    return out


def _seed_affordance_row(seed: Any) -> dict[str, Any]:
    payload = dict(getattr(seed, "payload", None) or {})
    tools = _string_list(payload.get("tool_candidates"))
    families = sorted({_source_family_for_uri(uri) for uri in tools})
    return {
        "seed_id": str(getattr(seed, "seed_id", "") or ""),
        "entity": str(getattr(seed, "entity", "") or ""),
        "horizon": str(getattr(seed, "horizon", "") or ""),
        "lens": str(getattr(seed, "lens", "") or ""),
        "bias_mode": str(getattr(seed, "bias_mode", "") or ""),
        "theme": str(getattr(seed, "theme", "") or ""),
        "tool_candidate_count": len(tools),
        "tool_candidates": tools,
        "source_families": families,
        "learning_policy_id": payload.get("learning_policy_id"),
        "learning_watch_metrics": _string_list(payload.get("learning_watch_metrics")),
        "learning_repair_work_order_ids": _string_list(payload.get("learning_repair_work_order_ids")),
        "prompt_contract_pressure": payload.get("prompt_contract_pressure"),
        "source_family_targets": _string_list(payload.get("source_family_targets")),
        "learning_tool_candidate_refresh": payload.get("learning_tool_candidate_refresh") or {},
    }


def _refresh_seed_tool_candidates(seed: Any) -> dict[str, Any]:
    """Refresh the actual candidate menu after policy changes.

    This is deliberately best-effort: a missing atlas should not prevent a
    no-spend preflight or deterministic test from proving the policy metadata,
    but a healthy atlas should let the policy widen the real affordance surface.
    """
    payload = dict(getattr(seed, "payload", None) or {})
    try:
        limit = int(payload.get("tool_candidate_limit") or 0)
    except Exception:
        limit = 0
    if limit <= 0:
        return {"status": "skipped", "reason": "no_tool_candidate_limit"}
    existing = _string_list(payload.get("tool_candidates"))
    try:
        from ..swarm.seed_generator import narrow_tools_for_seed

        refreshed = narrow_tools_for_seed(seed, k=limit)
    except Exception as exc:
        return {"status": "error", "error": f"{type(exc).__name__}:{exc}"}
    merged = _merge_lists(existing, _string_list(refreshed))[:limit]
    payload["tool_candidates"] = merged
    payload["learning_tool_candidate_refresh"] = {
        "status": "refreshed",
        "before_count": len(existing),
        "after_count": len(merged),
        "added_count": max(0, len(set(merged) - set(existing))),
        "limit": limit,
    }
    setattr(seed, "payload", payload)
    return payload["learning_tool_candidate_refresh"]


def _normalize_source_families(raw: Any) -> list[str]:
    aliases = {
        "our_hl_node": "our_node",
        "hl_node": "our_node",
        "web_attention": "parallel_web",
        "market_microstructure": "market_timeseries",
        "grok_x": "grok_x_alpha",
        "x_search": "grok_x_alpha",
        "twitter": "grok_x_alpha",
        "social_alpha": "grok_x_alpha",
    }
    out = []
    for item in _string_list(raw):
        key = item.lower()
        out.append(aliases.get(key, key))
    return sorted(set(out))


def _source_family_for_uri(uri: str) -> str:
    text = str(uri or "").lower()
    if any(tok in text for tok in ("farm_grok_x_alpha", "grok", "x_search", "xai", "twitter", "x.com")):
        return "grok_x_alpha"
    if "hydromancer" in text:
        return "hydromancer"
    if "parallel" in text or "perplexity" in text:
        return "parallel_web"
    if "source_health" in text:
        return "source_health"
    if "event" in text or "calendar" in text or "news" in text:
        return "event_feed"
    if any(tok in text for tok in ("timeseries", "candles", "l2", "orderbook", "microstructure", "funding")):
        return "market_timeseries"
    if any(tok in text for tok in ("hl_", "clearinghouse", "builder", "reject", "wallet")):
        return "our_node"
    if "celestial" in text or "astro" in text:
        return "astrology"
    if "polymarket" in text:
        return "prediction_market"
    if "source/" in text:
        return text.split("source/", 1)[1].split("/", 1)[0].split("@", 1)[0]
    return "tool_atlas"


def _rate(numerator: int | float, denominator: int | float) -> float:
    return round(float(numerator) / max(1.0, float(denominator)), 4)


def _avg(values: Any) -> float:
    nums = []
    for raw in values:
        try:
            nums.append(float(raw or 0.0))
        except Exception:
            nums.append(0.0)
    return sum(nums) / max(1, len(nums))


def _raise_string(payload: dict[str, Any], key: str, raw: Any) -> None:
    value = str(raw or "").strip()
    if value:
        payload[key] = value


def _raise_int(payload: dict[str, Any], key: str, raw: Any, *, cap: int) -> None:
    try:
        value = int(raw)
    except Exception:
        return
    if value <= 0:
        return
    current = 0
    try:
        current = int(payload.get(key) or 0)
    except Exception:
        current = 0
    payload[key] = min(cap, max(current, value))


def _merge_lists(existing: Any, additions: list[str]) -> list[str]:
    return list(dict.fromkeys([*_string_list(existing), *additions]))


def _string_list(raw: Any) -> list[str]:
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [str(x) for x in raw if str(x).strip()]

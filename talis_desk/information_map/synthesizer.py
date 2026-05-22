"""Cross-string synthesis for the information map.

This is the attention layer: thousands of cheap scout strings are reduced
to a small set of confluences/tensions worth spending verifier and
specialist budget on.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from .._tic_config import ensure_tic_on_path
from ..store import get_desk_store
from .store import recent_information_strings


@dataclass
class InformationSynthesis:
    synthesis_id: str
    cycle_id: str
    summary: str
    confluences: list[dict[str, Any]] = field(default_factory=list)
    tensions: list[dict[str, Any]] = field(default_factory=list)
    promoted_string_ids: list[str] = field(default_factory=list)
    promoted_hypotheses: list[dict[str, Any]] = field(default_factory=list)
    synthesis_item_ids: list[str] = field(default_factory=list)
    candidate_ids: list[str] = field(default_factory=list)
    model_used: str = "deterministic"
    provider: str = "local"
    cost_usd: float = 0.0
    quality_flags: list[str] = field(default_factory=list)


SYNTHESIS_SYSTEM_PROMPT = (
    "You are the attention layer of the Talis market information map. "
    "You read many cheap scout strings and return only the confluences and "
    "tensions worth spending verifier/specialist budget on. Return strict "
    "JSON only:\n"
    "{\n"
    '  "summary": "<3 sentence map summary>",\n'
    '  "confluences": [{"label": "...", "string_ids": ["..."], "why_it_matters": "..."}],\n'
    '  "tensions": [{"label": "...", "string_ids": ["..."], "why_it_matters": "..."}],\n'
    '  "promoted_hypotheses": [\n'
    '    {"hypothesis": "<falsifiable promoted claim>", "entity": "<ticker/entity>", '
    '"horizon": "<horizon>", "lens": "<lens>", "confidence": 0.0, '
    '"rationale_brief": "<why this deserves Tier 1.5>", '
    '"source_string_ids": ["..."]}\n'
    "  ]\n"
    "}\n\n"
    "Rules: promote at most 8 hypotheses; prefer cross-string convergence, "
    "non-obvious second-order mechanisms, and direct contradictions. Do not "
    "promote generic market commentary. Every promoted hypothesis should cite "
    "at least two source_string_ids unless the only valuable output is an "
    "explicit contradiction that must be resolved."
)


def run_information_synthesis(
    *,
    cycle_id: str,
    max_strings: int = 200,
    model: str = "deepseek:v4-flash",
    use_llm: bool = True,
    geometry_weights: Optional[dict[str, Any]] = None,
    routing_thresholds: Optional[dict[str, Any]] = None,
) -> InformationSynthesis:
    strings = recent_information_strings(cycle_id=cycle_id, limit=max_strings)
    if not strings:
        result = InformationSynthesis(
            synthesis_id=f"isyn_{uuid4().hex[:12]}",
            cycle_id=cycle_id,
            summary="No information strings emitted this cycle.",
            quality_flags=["no_information_strings"],
        )
        _attach_alpha_geometry(
            result,
            cycle_id,
            geometry_weights=geometry_weights,
            routing_thresholds=routing_thresholds,
        )
        _persist(result)
        return result
    deterministic = _deterministic_synthesis(cycle_id, strings)
    if not use_llm:
        _attach_alpha_geometry(
            deterministic,
            cycle_id,
            geometry_weights=geometry_weights,
            routing_thresholds=routing_thresholds,
        )
        _persist(deterministic)
        return deterministic

    user = _render_strings_for_llm(strings)
    try:
        ensure_tic_on_path()
        from tic.desk.models import chat as _chat  # type: ignore
        res = _run_chat_sync(_chat, model, SYNTHESIS_SYSTEM_PROMPT, user, max_tokens=6000)
        text = (res.get("text") or "").strip()
        parsed = _extract_first_json(text)
        if not isinstance(parsed, dict):
            raise RuntimeError("information_synthesis_json_unparseable")
        result = _result_from_parsed(
            cycle_id=cycle_id,
            parsed=parsed,
            strings=strings,
            fallback=deterministic,
            model_used=res.get("model_used") or model,
            provider=res.get("provider") or "?",
        )
    except Exception as e:
        deterministic.quality_flags.append(f"llm_synthesis_failed:{type(e).__name__}")
        result = deterministic
    _attach_alpha_geometry(
        result,
        cycle_id,
        geometry_weights=geometry_weights,
        routing_thresholds=routing_thresholds,
    )
    _persist(result)
    return result


def _deterministic_synthesis(cycle_id: str, strings: list[dict[str, Any]]) -> InformationSynthesis:
    by_cluster: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    by_entity: dict[str, list[dict[str, Any]]] = {}
    by_entity_lens: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for s in strings:
        theme = str(s.get("theme") or s.get("lens") or "unclassified")
        horizon = str(s.get("horizon") or s.get("time_horizon") or "unknown")
        lens = str(s.get("lens") or "unknown")
        entity = str(s.get("entity") or "UNKNOWN")
        by_cluster.setdefault((theme, horizon, lens), []).append(s)
        by_entity.setdefault(entity, []).append(s)
        by_entity_lens.setdefault((entity, horizon, lens), []).append(s)

    confluences: list[dict[str, Any]] = []
    seen_item_sources: set[tuple[str, ...]] = set()
    for (theme, horizon, lens), rows in sorted(
        by_cluster.items(),
        key=lambda kv: (_cluster_strength(kv[1]), len(kv[1])),
        reverse=True,
    ):
        item = _build_confluence_item(
            label=f"{theme} / {horizon} / {lens}",
            rows=rows,
            theme=theme,
            horizon=horizon,
            lens=lens,
            seen_sources=seen_item_sources,
        )
        if item:
            confluences.append(item)
    for (entity, horizon, lens), rows in sorted(
        by_entity_lens.items(),
        key=lambda kv: (_cluster_strength(kv[1]), len(kv[1])),
        reverse=True,
    ):
        item = _build_confluence_item(
            label=f"{entity} / {horizon} / {lens}",
            rows=rows,
            entity=entity,
            horizon=horizon,
            lens=lens,
            seen_sources=seen_item_sources,
        )
        if item:
            confluences.append(item)

    tensions: list[dict[str, Any]] = []
    for entity, rows in by_entity.items():
        if len(rows) < 2:
            continue
        high_crowded = sorted(
            [r for r in rows if float(r.get("crowdedness") or 0.5) >= 0.7],
            key=_row_attention,
            reverse=True,
        )
        high_novel = sorted(
            [r for r in rows if float(r.get("novelty_score") or 0.5) >= 0.65],
            key=_row_attention,
            reverse=True,
        )
        explicit_contradictions = sorted(
            [r for r in rows if str(r.get("extends_or_contradicts") or "").lower() == "contradicts"],
            key=_row_attention,
            reverse=True,
        )
        if explicit_contradictions:
            base = sorted(
                [r for r in rows if r["id"] != explicit_contradictions[0]["id"]],
                key=_row_attention,
                reverse=True,
            )[:1]
            source_rows = explicit_contradictions[:1] + base
            if len(source_rows) >= 2:
                tensions.append(_build_tension_item(
                    label=f"{entity}: explicit contradiction in scout strings",
                    rows=source_rows,
                    why="A new string explicitly contradicts a prior trail; resolve before spending specialist budget.",
                ))
        if high_crowded and high_novel:
            tensions.append(_build_tension_item(
                label=f"{entity}: crowded consensus vs novel string",
                rows=[high_crowded[0], high_novel[0]],
                why="The same entity has both crowded and novel causal interpretations.",
            ))

    confluences = confluences[:12]
    tensions = tensions[:12]
    promoted = _promote_from_items(confluences + tensions, strings, max_items=8)
    if not promoted:
        promoted = _promote_top_strings(strings, max_items=8)
    promoted_ids = sorted({
        sid
        for p in promoted
        for sid in p.get("source_string_ids", [])
    })
    return InformationSynthesis(
        synthesis_id=f"isyn_{uuid4().hex[:12]}",
        cycle_id=cycle_id,
        summary=(
            f"Information map ingested {len(strings)} strings. "
            f"Detected {len(confluences)} confluence clusters and "
            f"{len(tensions)} tension clusters."
        ),
        confluences=confluences,
        tensions=tensions,
        promoted_string_ids=promoted_ids,
        promoted_hypotheses=promoted,
        quality_flags=["deterministic_synthesis"],
    )


def _attach_alpha_geometry(
    result: InformationSynthesis,
    cycle_id: str,
    *,
    geometry_weights: Optional[dict[str, Any]] = None,
    routing_thresholds: Optional[dict[str, Any]] = None,
) -> None:
    try:
        from .alpha_geometry import alpha_geometry_seed_directives, compute_alpha_geometry

        geometry = compute_alpha_geometry(
            cycle_id=cycle_id,
            geometry_weights=geometry_weights,
            routing_thresholds=routing_thresholds,
        )
        result.quality_flags.append(f"alpha_geometry_cells:{len(geometry.cells)}")
        if geometry_weights is not None:
            result.quality_flags.append("policy_weighted_geometry")
        if routing_thresholds is not None:
            result.quality_flags.append("policy_routed_geometry")
        directives = alpha_geometry_seed_directives(geometry, max_items=3)
        if directives:
            result.quality_flags.append("alpha_geometry_routes_available")
    except Exception as e:
        result.quality_flags.append(f"alpha_geometry_failed:{type(e).__name__}")


def _build_confluence_item(
    *,
    label: str,
    rows: list[dict[str, Any]],
    seen_sources: set[tuple[str, ...]],
    entity: str = "",
    theme: str = "",
    horizon: str = "",
    lens: str = "",
) -> Optional[dict[str, Any]]:
    if len(rows) < 2:
        return None
    top = sorted(rows, key=_row_attention, reverse=True)[:6]
    source_ids = tuple(sorted(str(r["id"]) for r in top if r.get("id")))
    if len(source_ids) < 2 or source_ids in seen_sources:
        return None
    seen_sources.add(source_ids)
    entities = sorted({str(r.get("entity") or "") for r in top if r.get("entity")})
    themes = sorted({str(r.get("theme") or "") for r in top if r.get("theme")})
    horizons = sorted({str(r.get("horizon") or r.get("time_horizon") or "") for r in top if r.get("horizon") or r.get("time_horizon")})
    lenses = sorted({str(r.get("lens") or "") for r in top if r.get("lens")})
    strength = _cluster_strength(top)
    return {
        "label": label,
        "item_type": "confluence",
        "string_ids": list(source_ids),
        "entity": entity or (entities[0] if len(entities) == 1 else "basket"),
        "theme": theme or (themes[0] if len(themes) == 1 else ""),
        "horizon": horizon or (horizons[0] if len(horizons) == 1 else "mixed"),
        "lens": lens or (lenses[0] if len(lenses) == 1 else "mixed"),
        "strength": strength,
        "why_it_matters": (
            f"{len(top)} high-attention strings converge around {label}; "
            "this is a better verifier spend than another isolated scout claim."
        ),
    }


def _build_tension_item(
    *,
    label: str,
    rows: list[dict[str, Any]],
    why: str,
) -> dict[str, Any]:
    top = sorted(rows, key=_row_attention, reverse=True)[:4]
    entities = sorted({str(r.get("entity") or "") for r in top if r.get("entity")})
    horizons = sorted({str(r.get("horizon") or r.get("time_horizon") or "") for r in top if r.get("horizon") or r.get("time_horizon")})
    lenses = sorted({str(r.get("lens") or "") for r in top if r.get("lens")})
    return {
        "label": label,
        "item_type": "tension",
        "string_ids": [str(r["id"]) for r in top if r.get("id")],
        "entity": entities[0] if len(entities) == 1 else "basket",
        "horizon": horizons[0] if len(horizons) == 1 else "mixed",
        "lens": lenses[0] if len(lenses) == 1 else "mixed",
        "strength": _cluster_strength(top) + 0.05,
        "why_it_matters": why,
    }


def _cluster_strength(rows: list[dict[str, Any]]) -> float:
    if not rows:
        return 0.0
    attentions = [_row_attention(r) for r in rows]
    diversity_bonus = min(0.16, 0.04 * len({str(r.get("entity") or "") for r in rows}))
    evidence_bonus = min(0.10, 0.025 * sum(1 for r in rows if r.get("evidence_refs") or r.get("source_tool_call_ids")))
    return round(max(0.0, min(1.0, sum(attentions) / len(attentions) + diversity_bonus + evidence_bonus)), 4)


def _row_attention(row: dict[str, Any]) -> float:
    try:
        attention = float(row.get("attention_score"))
    except Exception:
        attention = 0.0
    if attention > 0:
        return attention
    return (
        float(row.get("conviction") or 0.0) * 0.45
        + float(row.get("novelty_score") or 0.0) * 0.30
        + (1.0 - float(row.get("crowdedness") or 0.5)) * 0.15
    )


def _promote_from_items(
    items: list[dict[str, Any]],
    strings: list[dict[str, Any]],
    *,
    max_items: int,
) -> list[dict[str, Any]]:
    by_id = {str(s.get("id")): s for s in strings if s.get("id")}
    out: list[dict[str, Any]] = []
    seen_sources: set[tuple[str, ...]] = set()
    for item in sorted(items, key=lambda i: float(i.get("strength") or 0.0), reverse=True):
        source_ids = [str(sid) for sid in item.get("string_ids") or [] if str(sid).strip()]
        source_key = tuple(sorted(source_ids))
        if len(source_key) < 2 or source_key in seen_sources:
            continue
        seen_sources.add(source_key)
        rows = [by_id[sid] for sid in source_ids if sid in by_id]
        entity = str(item.get("entity") or _dominant(rows, "entity") or "basket")
        horizon = str(item.get("horizon") or _dominant(rows, "horizon") or "mixed")
        lens = str(item.get("lens") or _dominant(rows, "lens") or "synthesis")
        item_type = str(item.get("item_type") or "confluence")
        label = str(item.get("label") or item_type)
        confidence = max(0.0, min(1.0, float(item.get("strength") or _cluster_strength(rows))))
        out.append({
            "hypothesis": _promoted_hypothesis_text(item_type, label, rows, horizon),
            "entity": entity,
            "horizon": horizon,
            "lens": lens,
            "bias_mode": "frontier" if item_type == "confluence" else "resolve_tension",
            "confidence": confidence,
            "promotion_score": confidence,
            "rationale_brief": str(item.get("why_it_matters") or "")[:240],
            "source_string_ids": source_ids,
            "synthesis_item_label": label,
            "suggested_tools": [],
        })
        _apply_promotion_gate(out[-1], rows)
        if len(out) >= max_items:
            break
    return out


def _promoted_hypothesis_text(
    item_type: str,
    label: str,
    rows: list[dict[str, Any]],
    horizon: str,
) -> str:
    entities = [str(r.get("entity") or "") for r in rows if r.get("entity")]
    entity_text = ", ".join(dict.fromkeys(entities[:4])) or "the mapped basket"
    if item_type == "tension":
        return (
            f"Resolve {label}: contradictory scout strings around {entity_text} "
            f"should produce a falsifiable direction or rejection inside {horizon}."
        )[:280]
    return (
        f"{label} deserves Tier 1.5 verification because independent scout strings "
        f"converge around {entity_text} inside {horizon}."
    )[:280]


def _dominant(rows: list[dict[str, Any]], key: str) -> str:
    counts: dict[str, int] = {}
    for row in rows:
        val = str(row.get(key) or "")
        if val:
            counts[val] = counts.get(val, 0) + 1
    if not counts:
        return ""
    return sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[0][0]


def _promote_top_strings(strings: list[dict[str, Any]], max_items: int) -> list[dict[str, Any]]:
    ranked = sorted(
        strings,
        key=lambda s: (
            float(s.get("conviction") or 0.0) * 0.55
            + float(s.get("novelty_score") or 0.0) * 0.35
            + (1.0 - float(s.get("crowdedness") or 0.5)) * 0.10
        ),
        reverse=True,
    )
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for s in ranked:
        entity = str(s.get("entity") or "UNKNOWN")
        horizon = str(s.get("horizon") or s.get("time_horizon") or "1d")
        lens = str(s.get("lens") or "anomaly")
        key = (entity, horizon, lens)
        if key in seen:
            continue
        seen.add(key)
        candidate = {
            "hypothesis": str(s.get("thesis") or s.get("title") or "")[:280],
            "entity": entity,
            "horizon": horizon,
            "lens": lens,
            "bias_mode": str(s.get("bias_mode") or "frontier"),
            "confidence": max(0.0, min(1.0, float(s.get("conviction") or 0.5))),
            "rationale_brief": str(s.get("mechanism") or s.get("expected_outcome") or "")[:240],
            "source_string_ids": [s["id"]],
            "suggested_tools": [],
            "status": "needs_cross_string_support",
            "quality_flags": ["single_string_candidate", "needs_confluence_before_verifier"],
        }
        candidate["promotion_score"] = min(0.45, float(candidate["confidence"]))
        candidate["confidence"] = candidate["promotion_score"]
        out.append(candidate)
        if len(out) >= max_items:
            break
    return out


def _calibrate_promoted_candidates(
    candidates: list[dict[str, Any]],
    strings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_id = {str(s.get("id")): s for s in strings if s.get("id")}
    out: list[dict[str, Any]] = []
    for candidate in candidates:
        source_ids = [str(sid) for sid in candidate.get("source_string_ids") or [] if str(sid).strip()]
        rows = [by_id[sid] for sid in source_ids if sid in by_id]
        _apply_promotion_gate(candidate, rows)
        out.append(candidate)
    return out


def _apply_promotion_gate(candidate: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    """Adversarial budget gate for synthesis promotions.

    The synthesis can still record weak/single-string ideas for audit, but only
    cross-string and cross-scout candidates should consume verifier budget.
    """
    source_ids = [str(sid) for sid in candidate.get("source_string_ids") or [] if str(sid).strip()]
    flags = set(_string_list(candidate.get("quality_flags")))
    status = str(candidate.get("status") or "queued_verifier")
    confidence = max(0.0, min(1.0, float(candidate.get("confidence") or 0.5)))

    scout_ids = {str(r.get("scout_id") or "") for r in rows if r.get("scout_id")}
    coverage_cells = {
        str(r.get("coverage_cell_key") or r.get("seed_id") or "")
        for r in rows
        if r.get("coverage_cell_key") or r.get("seed_id")
    }
    row_flags = {
        str(flag)
        for row in rows
        for flag in _string_list(row.get("quality_flags"))
    }
    evidence_supported = sum(1 for r in rows if r.get("evidence_refs") or r.get("source_tool_call_ids"))

    if len(source_ids) < 2:
        flags.update({"single_string_candidate", "needs_confluence_before_verifier"})
        status = "needs_cross_string_support"
        confidence = min(confidence, 0.45)
    if source_ids and len(rows) < len(source_ids):
        flags.add("missing_source_string_rows")
        status = "needs_source_repair"
        confidence = min(confidence, 0.35)
    if rows and len(scout_ids) < 2:
        flags.add("single_scout_confluence")
        status = "needs_independent_scout"
        confidence = min(confidence, 0.46)
    if rows and len(coverage_cells) < 2:
        flags.add("single_cell_confluence")
        confidence = min(confidence, 0.55)
    if any("adversarial_decision:quarantine" in flag for flag in row_flags):
        flags.add("contains_quarantined_source")
        status = "needs_source_repair"
        confidence = min(confidence, 0.35)
    if any("failed_call_as_evidence" in flag for flag in row_flags):
        flags.add("contains_failed_evidence_flag")
        confidence = min(confidence, 0.50)
    if rows and evidence_supported < len(rows):
        flags.add("partial_evidence_support")
        confidence = min(confidence, 0.62)
    if rows and evidence_supported == 0:
        flags.add("thin_evidence_support")
        confidence = min(confidence, 0.52)

    candidate["confidence"] = round(confidence, 4)
    candidate["promotion_score"] = round(min(confidence, float(candidate.get("promotion_score") or confidence)), 4)
    candidate["status"] = status
    candidate["quality_flags"] = sorted(flags | {"adversarial_promotion_gate"})


def _result_from_parsed(
    *,
    cycle_id: str,
    parsed: dict[str, Any],
    strings: list[dict[str, Any]],
    fallback: InformationSynthesis,
    model_used: str,
    provider: str,
) -> InformationSynthesis:
    promoted = parsed.get("promoted_hypotheses")
    if not isinstance(promoted, list):
        promoted = fallback.promoted_hypotheses
    promoted = _calibrate_promoted_candidates(_dict_list(promoted), strings)
    promoted_ids = sorted({
        str(sid)
        for p in promoted
        if isinstance(p, dict)
        for sid in (p.get("source_string_ids") or [])
    })
    return InformationSynthesis(
        synthesis_id=f"isyn_{uuid4().hex[:12]}",
        cycle_id=cycle_id,
        summary=str(parsed.get("summary") or fallback.summary)[:3000],
        confluences=_dict_list(parsed.get("confluences")) or fallback.confluences,
        tensions=_dict_list(parsed.get("tensions")) or fallback.tensions,
        promoted_string_ids=promoted_ids or fallback.promoted_string_ids,
        promoted_hypotheses=promoted[:8] or fallback.promoted_hypotheses,
        model_used=model_used,
        provider=provider,
        cost_usd=0.004,
    )


def _persist(result: InformationSynthesis) -> None:
    conn = get_desk_store().conn
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT OR REPLACE INTO information_syntheses (
            id, cycle_id, summary, confluences_json, tensions_json,
            promoted_string_ids_json, promoted_hypotheses_json, model_used,
            provider, cost_usd, quality_flags, created_at, valid_from,
            transaction_from, transaction_to
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
        """,
        (
            result.synthesis_id,
            result.cycle_id,
            result.summary,
            json.dumps(result.confluences),
            json.dumps(result.tensions),
            json.dumps(result.promoted_string_ids),
            json.dumps(result.promoted_hypotheses),
            result.model_used,
            result.provider,
            result.cost_usd,
            json.dumps(result.quality_flags),
            now,
            now,
            now,
        ),
    )
    _persist_items_and_candidates(conn, result, now)
    conn.commit()


def load_promoted_candidates(
    *,
    cycle_id: Optional[str] = None,
    synthesis_id: Optional[str] = None,
    limit: int = 50,
    conn: Any = None,
) -> list[dict[str, Any]]:
    db = conn or get_desk_store().conn
    clauses: list[str] = []
    params: list[Any] = []
    if cycle_id:
        clauses.append("cycle_id = ?")
        params.append(cycle_id)
    if synthesis_id:
        clauses.append("synthesis_id = ?")
        params.append(synthesis_id)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(int(limit))
    rows = db.execute(
        f"""
        SELECT * FROM promoted_candidates
        {where}
        ORDER BY promotion_score DESC, created_at DESC
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        for key in ("source_string_ids_json", "suggested_tools_json", "quality_flags"):
            try:
                d[key] = json.loads(d.get(key) or "[]")
            except Exception:
                d[key] = []
        d["source_string_ids"] = d.pop("source_string_ids_json")
        d["suggested_tools"] = d.pop("suggested_tools_json")
        out.append(d)
    return out


def _persist_items_and_candidates(conn: Any, result: InformationSynthesis, now: str) -> None:
    result.synthesis_item_ids = []
    result.candidate_ids = []
    items = [
        ("confluence", item)
        for item in result.confluences
        if isinstance(item, dict)
    ] + [
        ("tension", item)
        for item in result.tensions
        if isinstance(item, dict)
    ]
    source_to_item: dict[tuple[str, ...], str] = {}
    for item_type, item in items:
        source_ids = _string_list(item.get("string_ids"))
        item_id = str(item.get("id") or _synthesis_item_id(result.synthesis_id, item_type, item, source_ids))
        item["id"] = item_id
        item["item_type"] = item_type
        result.synthesis_item_ids.append(item_id)
        source_key = tuple(sorted(source_ids))
        if source_key:
            source_to_item[source_key] = item_id
        conn.execute(
            """
            INSERT OR REPLACE INTO information_synthesis_items (
                id, synthesis_id, cycle_id, item_type, label,
                coverage_cell_key, entity, theme, horizon, lens,
                why_it_matters, string_ids_json, strength, quality_flags,
                created_at, valid_from, transaction_from, transaction_to
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                item_id,
                result.synthesis_id,
                result.cycle_id,
                item_type,
                str(item.get("label") or item_type)[:240],
                item.get("coverage_cell_key"),
                item.get("entity"),
                item.get("theme"),
                item.get("horizon"),
                item.get("lens"),
                str(item.get("why_it_matters") or "")[:2000],
                json.dumps(source_ids),
                float(item.get("strength") or 0.0),
                json.dumps(_string_list(item.get("quality_flags"))),
                now,
                now,
                now,
            ),
        )
        _insert_artifact_edge(
            conn,
            cycle_id=result.cycle_id,
            from_kind="information_synthesis",
            from_id=result.synthesis_id,
            to_kind="information_synthesis_item",
            to_id=item_id,
            edge_kind=item_type,
            strength=float(item.get("strength") or 0.0),
            evidence_role="attention_cluster",
            now=now,
        )
        for sid in source_ids:
            _insert_artifact_edge(
                conn,
                cycle_id=result.cycle_id,
                from_kind="information_synthesis_item",
                from_id=item_id,
                to_kind="information_string",
                to_id=sid,
                edge_kind=f"{item_type}_source",
                strength=float(item.get("strength") or 0.0),
                evidence_role="source_string",
                now=now,
            )

    for candidate in result.promoted_hypotheses:
        if not isinstance(candidate, dict):
            continue
        source_ids = _string_list(candidate.get("source_string_ids"))
        item_id = str(candidate.get("synthesis_item_id") or "") or _best_item_for_sources(source_ids, source_to_item)
        if item_id:
            candidate["synthesis_item_id"] = item_id
        candidate_id = str(candidate.get("candidate_id") or _promoted_candidate_id(result.synthesis_id, candidate, source_ids))
        candidate["candidate_id"] = candidate_id
        result.candidate_ids.append(candidate_id)
        conn.execute(
            """
            INSERT OR REPLACE INTO promoted_candidates (
                id, synthesis_id, synthesis_item_id, cycle_id,
                coverage_cell_key, entity, theme, horizon, lens, bias_mode,
                hypothesis, rationale_brief, confidence, promotion_score,
                source_string_ids_json, suggested_tools_json, status,
                quality_flags, created_at, valid_from, transaction_from,
                transaction_to
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                candidate_id,
                result.synthesis_id,
                item_id or None,
                result.cycle_id,
                candidate.get("coverage_cell_key"),
                candidate.get("entity"),
                candidate.get("theme"),
                candidate.get("horizon"),
                candidate.get("lens"),
                candidate.get("bias_mode"),
                str(candidate.get("hypothesis") or "")[:2000],
                str(candidate.get("rationale_brief") or "")[:2000],
                float(candidate.get("confidence") or 0.5),
                float(candidate.get("promotion_score") or candidate.get("confidence") or 0.0),
                json.dumps(source_ids),
                json.dumps(_string_list(candidate.get("suggested_tools"))),
                str(candidate.get("status") or "queued_verifier"),
                json.dumps(_string_list(candidate.get("quality_flags"))),
                now,
                now,
                now,
            ),
        )
        _insert_artifact_edge(
            conn,
            cycle_id=result.cycle_id,
            from_kind="information_synthesis",
            from_id=result.synthesis_id,
            to_kind="promoted_candidate",
            to_id=candidate_id,
            edge_kind="promotes",
            strength=float(candidate.get("promotion_score") or candidate.get("confidence") or 0.0),
            evidence_role="promotion",
            now=now,
        )
        if item_id:
            _insert_artifact_edge(
                conn,
                cycle_id=result.cycle_id,
                from_kind="information_synthesis_item",
                from_id=item_id,
                to_kind="promoted_candidate",
                to_id=candidate_id,
                edge_kind="promotes",
                strength=float(candidate.get("promotion_score") or candidate.get("confidence") or 0.0),
                evidence_role="candidate_parent",
                now=now,
            )
        for sid in source_ids:
            _insert_artifact_edge(
                conn,
                cycle_id=result.cycle_id,
                from_kind="promoted_candidate",
                from_id=candidate_id,
                to_kind="information_string",
                to_id=sid,
                edge_kind="uses_source_string",
                strength=float(candidate.get("promotion_score") or candidate.get("confidence") or 0.0),
                evidence_role="candidate_source",
                now=now,
            )


def _insert_artifact_edge(
    conn: Any,
    *,
    cycle_id: str,
    from_kind: str,
    from_id: str,
    to_kind: str,
    to_id: str,
    edge_kind: str,
    strength: float,
    evidence_role: str,
    now: str,
) -> None:
    edge_id = "iaedge_" + hashlib.sha256(
        f"{cycle_id}|{from_kind}|{from_id}|{to_kind}|{to_id}|{edge_kind}".encode()
    ).hexdigest()[:16]
    conn.execute(
        """
        INSERT OR REPLACE INTO information_artifact_edges (
            id, cycle_id, from_kind, from_id, to_kind, to_id, edge_kind,
            strength, evidence_role, payload, valid_from, transaction_from,
            transaction_to
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
        """,
        (
            edge_id,
            cycle_id,
            from_kind,
            from_id,
            to_kind,
            to_id,
            edge_kind,
            float(strength or 0.0),
            evidence_role,
            json.dumps({}),
            now,
            now,
        ),
    )


def _synthesis_item_id(
    synthesis_id: str,
    item_type: str,
    item: dict[str, Any],
    source_ids: list[str],
) -> str:
    raw = f"{synthesis_id}|{item_type}|{item.get('label')}|{','.join(sorted(source_ids))}"
    return "isitem_" + hashlib.sha256(raw.encode()).hexdigest()[:14]


def _promoted_candidate_id(
    synthesis_id: str,
    candidate: dict[str, Any],
    source_ids: list[str],
) -> str:
    raw = f"{synthesis_id}|{candidate.get('hypothesis')}|{','.join(sorted(source_ids))}"
    return "pcand_" + hashlib.sha256(raw.encode()).hexdigest()[:14]


def _best_item_for_sources(source_ids: list[str], source_to_item: dict[tuple[str, ...], str]) -> str:
    source_set = set(source_ids)
    best_item = ""
    best_overlap = 0
    for key, item_id in source_to_item.items():
        overlap = len(source_set.intersection(key))
        if overlap > best_overlap:
            best_overlap = overlap
            best_item = item_id
    return best_item if best_overlap else ""


def _string_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw] if raw.strip() else []
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    return []


def _render_strings_for_llm(strings: list[dict[str, Any]]) -> str:
    rows = []
    for s in strings[:200]:
        rows.append({
            "id": s.get("id"),
            "entity": s.get("entity"),
            "theme": s.get("theme"),
            "horizon": s.get("horizon"),
            "lens": s.get("lens"),
            "conviction": s.get("conviction"),
            "novelty_score": s.get("novelty_score"),
            "crowdedness": s.get("crowdedness"),
            "attention_score": s.get("attention_score"),
            "extends_or_contradicts": s.get("extends_or_contradicts"),
            "would_change_decision": s.get("would_change_decision"),
            "thesis": s.get("thesis"),
            "mechanism": s.get("mechanism"),
            "expected_outcome": s.get("expected_outcome"),
            "kill_signal": s.get("kill_signal"),
            "evidence_refs": s.get("evidence_refs"),
        })
    return "information_strings_json:\n" + json.dumps(rows, ensure_ascii=True)[:60000]


def _run_chat_sync(chat_fn: Any, model: str, system: str, user: str, *, max_tokens: int) -> dict[str, Any]:
    async def _go() -> dict[str, Any]:
        return await chat_fn(model, system, user, max_tokens=max_tokens, fallback="anthropic:claude-haiku-4-5")
    try:
        asyncio.get_running_loop()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            return ex.submit(asyncio.run, _go()).result(timeout=45)
    except RuntimeError:
        return asyncio.run(_go())


def _extract_first_json(text: str) -> Any:
    s = (text or "").strip()
    if s.startswith("```"):
        lines = s.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        s = "\n".join(lines).strip()
    start = s.find("{")
    if start < 0:
        return None
    decoder = json.JSONDecoder()
    try:
        obj, _ = decoder.raw_decode(s[start:])
        return obj
    except Exception:
        pass
    depth = 0
    in_string = False
    escape = False
    for i, ch in enumerate(s[start:], start):
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(s[start:i + 1])
                except Exception:
                    return None
    return None


def _dict_list(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    return [x for x in raw if isinstance(x, dict)]

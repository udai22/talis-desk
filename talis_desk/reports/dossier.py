"""Stage 0 — Evidence dossier pull.

Before stage-1 researcher draft runs, this module assembles a
comprehensive evidence dossier per hypothesis using ONLY `tic.desk.tools`
read calls (no LLM). It pulls:

  * semantic_neighbors  via `semantic_search`
  * similar_setups      via `find_similar_setups`
  * confluence_votes    via `find_confluence`
  * correlation_matrix  via `get_correlation_matrix`
  * recent_news         via `query_recent_news`
  * source_health       via `query_source_health`
  * timeseries          via `query_timeseries`

The dossier ALWAYS returns — when individual tools fail, the dossier
records a quality flag and the rest of the dossier continues. The
researcher draft stage downstream consumes the dossier in its system
prompt so every cited number can reference a real `claim_id` /
`tool_call_log_id` in the dossier.

# Tool-arg discipline (Codex finding #10 — bad-args lesson)

We DO NOT hardcode tool kwarg names. Each call inspects
`TOOLS[name]['input_schema']` and only passes kwargs the schema declares
as `properties`. Missing required args → quality_flag, no call.

# Cost

Pure tic.db reads — `pull_cost_usd` is recorded as 0.0 in the cost
ledger (we still report it for symmetry with downstream stages).
"""
from __future__ import annotations

import logging
import warnings
from dataclasses import dataclass, field
from typing import Any, Optional

from .._tic_config import ensure_tic_on_path as _ensure_tic_on_path


logger = logging.getLogger(__name__)


# ============================================================================
# Result type
# ============================================================================


@dataclass
class EvidenceDossier:
    """Output of `pull_dossier`. All fields are JSON-serializable so the
    full dossier can be embedded in `ResearchReport.payload`."""

    hypothesis_id: str
    instrument: str
    semantic_neighbors: list[dict[str, Any]] = field(default_factory=list)
    similar_setups: list[dict[str, Any]] = field(default_factory=list)
    confluence_votes: dict[str, Any] = field(default_factory=dict)
    correlation_matrix: dict[str, Any] = field(default_factory=dict)
    recent_news: list[dict[str, Any]] = field(default_factory=list)
    source_health: dict[str, Any] = field(default_factory=dict)
    timeseries_snapshots: dict[str, Any] = field(default_factory=dict)
    n_claims_pulled: int = 0
    n_unique_sources: int = 0
    pull_cost_usd: float = 0.0
    quality_flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "hypothesis_id": self.hypothesis_id,
            "instrument": self.instrument,
            "semantic_neighbors": self.semantic_neighbors,
            "similar_setups": self.similar_setups,
            "confluence_votes": self.confluence_votes,
            "correlation_matrix": self.correlation_matrix,
            "recent_news": self.recent_news,
            "source_health": self.source_health,
            "timeseries_snapshots": self.timeseries_snapshots,
            "n_claims_pulled": self.n_claims_pulled,
            "n_unique_sources": self.n_unique_sources,
            "pull_cost_usd": self.pull_cost_usd,
            "quality_flags": list(self.quality_flags),
        }


# ============================================================================
# Helpers
# ============================================================================


def _filter_kwargs_by_schema(spec: dict[str, Any], kwargs: dict[str, Any]) -> dict[str, Any]:
    """Keep only kwargs that the tool's input_schema declares as a
    property. Codex finding #10 enforcement — don't pass `entity_symbol`
    to a tool that wants `ticker`."""
    schema = (spec or {}).get("input_schema") or {}
    props = (schema.get("properties") or {})
    return {k: v for k, v in kwargs.items() if k in props}


def _required_args(spec: dict[str, Any]) -> list[str]:
    schema = (spec or {}).get("input_schema") or {}
    return list(schema.get("required") or [])


def _safe_call(
    tools: dict[str, Any],
    name: str,
    *,
    flags: list[str],
    **candidate_kwargs: Any,
) -> Any:
    """Invoke `tools[name]` with kwargs filtered by its schema. On any
    failure (missing required arg, callable raises, etc.), append a
    quality flag and return None. NEVER raises."""
    spec = tools.get(name)
    if not spec:
        flags.append(f"dossier_tool_missing:{name}")
        return None
    fn = spec.get("callable")
    if fn is None:
        flags.append(f"dossier_tool_no_callable:{name}")
        return None
    # Filter to schema-declared properties so we never send a bogus kwarg.
    kw = _filter_kwargs_by_schema(spec, candidate_kwargs)
    # Verify required args are all present after filtering.
    missing = [r for r in _required_args(spec) if r not in kw]
    if missing:
        flags.append(f"dossier_tool_missing_args:{name}:{','.join(missing)}")
        return None
    try:
        return fn(**kw)
    except Exception as e:  # noqa: BLE001
        flags.append(f"dossier_tool_call_failed:{name}:{type(e).__name__}")
        logger.info("dossier: tool %s raised %s: %s", name, type(e).__name__, e)
        return None


def _semantic_text_from_hypothesis(hypothesis: dict[str, Any]) -> str:
    title = (hypothesis.get("title") or "").strip()
    body = (hypothesis.get("hypothesis_text") or "").strip()
    if title and body:
        return f"{title}. {body}"[:600]
    return (title or body)[:600]


def _instrument_from_hypothesis(hypothesis: dict[str, Any]) -> str:
    ents = hypothesis.get("entity_ids") or []
    if ents:
        return str(ents[0])
    return ""


def _coerce_semantic_neighbors(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for row in raw[:30]:
        if not isinstance(row, dict):
            continue
        out.append({
            "id": row.get("id"),
            "object_kind": row.get("object_kind"),
            "object_id": row.get("object_id"),
            "fused_score": row.get("fused_score"),
            "similarity": row.get("similarity"),
            "text": (row.get("text") or "")[:600],
        })
    return out


def _coerce_similar_setups(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, dict):
        matches = raw.get("matches") or []
    elif isinstance(raw, list):
        matches = raw
    else:
        matches = []
    out: list[dict[str, Any]] = []
    for row in (matches or [])[:10]:
        if not isinstance(row, dict):
            continue
        out.append({
            "match_anchor": row.get("anchor") or row.get("date_range") or row.get("ts"),
            "similarity_score": row.get("similarity") or row.get("score"),
            "fwd_return": row.get("fwd_return"),
            "outcome_summary": row.get("outcome_summary") or row.get("outcome"),
            "claim_ids": list(row.get("claim_ids") or row.get("claim_id_refs") or [])[:5],
        })
    return out


def _coerce_confluence(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    return {
        "entity_symbol": raw.get("entity_symbol"),
        "lookback_hours": raw.get("lookback_hours"),
        "confluence_score": raw.get("confluence_score"),
        "n_bull": raw.get("n_bull"),
        "n_bear": raw.get("n_bear"),
        "n_neutral": raw.get("n_neutral"),
        "n_unavailable": raw.get("n_unavailable"),
        "by_family": raw.get("by_family"),
    }


def _coerce_recent_news(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for row in raw[:15]:
        if not isinstance(row, dict):
            continue
        out.append({
            "headline": (row.get("headline") or "")[:240],
            "source": row.get("source"),
            "ts": row.get("ts") or row.get("published_at"),
            "url": row.get("url"),
            "claim_id": row.get("claim_id"),
            "sentiment": row.get("sentiment"),
        })
    return out


def _coerce_source_health(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    for slug, info in raw.items():
        if not isinstance(info, dict):
            continue
        out[slug] = {
            "status": info.get("current_status") or info.get("status"),
            "last_successful_at": info.get("last_successful_at"),
            "consecutive_failures": info.get("consecutive_failures"),
            "last_error": (info.get("last_error") or "")[:200] if info.get("last_error") else None,
        }
    return out


def _coerce_correlation_matrix(raw: Any, instrument: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    # We deliberately keep this small — only summary stats + per-pair if
    # tic exposes them in this shape.
    out: dict[str, Any] = {
        "n_assets": raw.get("n_assets"),
        "n_pairs": raw.get("n_pairs"),
        "claims_written": raw.get("claims_written"),
        "regime_shifts_flagged": raw.get("regime_shifts_flagged"),
        "instrument": instrument,
    }
    # Some implementations also expose `pairs` / `top_correlations`.
    pairs = raw.get("pairs") or raw.get("top_correlations")
    if isinstance(pairs, list):
        out["top_pairs"] = pairs[:10]
    return out


def _coerce_timeseries(raw: Any, metric_prefix: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    return {
        "metric_prefix": metric_prefix,
        "anchor": raw.get("anchor"),
        "latest_ts": raw.get("latest_ts"),
        "latest_age_s": raw.get("latest_age_s"),
        "points": (raw.get("points") or raw.get("series") or [])[:20],
    }


# ============================================================================
# Public entry point
# ============================================================================


# Default metric prefixes pulled per instrument. We pick families that
# `query_timeseries` indexes on tic.db: hl (Hyperliquid orderbook/spot),
# fred (macro). Keep small so dossier pull stays cheap.
_DEFAULT_TS_PREFIXES: tuple[str, ...] = ("hl", "fred")


def pull_dossier(
    hypothesis: dict[str, Any],
    *,
    conn: Any = None,  # accepted for API symmetry, not currently used
    semantic_k: int = 20,
    similar_top_k: int = 5,
    similar_fwd_days: int = 14,
    confluence_lookback_hours: int = 24,
    news_lookback_hours: int = 168,
    timeseries_lookback_hours: int = 168,
    timeseries_limit: int = 20,
    metric_prefixes: tuple[str, ...] = _DEFAULT_TS_PREFIXES,
) -> EvidenceDossier:
    """Pull a comprehensive evidence dossier for one hypothesis.

    NO LLM call. Calls 5-7 cheap tic.db read tools and aggregates. Always
    returns an `EvidenceDossier` — even when every tool fails the dossier
    is still returned with `quality_flags` enumerating what failed.

    Parameters
    ----------
    hypothesis: dict with keys `id`, `title`, `hypothesis_text`,
        `entity_ids`, etc. (the shape emitted by the loop runner).
    semantic_k: how many semantic neighbors to retrieve.
    similar_top_k / similar_fwd_days: passed to find_similar_setups.
    confluence_lookback_hours: passed to find_confluence.
    news_lookback_hours: passed to query_recent_news.
    timeseries_lookback_hours / timeseries_limit: query_timeseries args.
    metric_prefixes: metric families to pull per instrument.
    """
    hyp_id = str(hypothesis.get("id") or "")
    instrument = _instrument_from_hypothesis(hypothesis)
    semantic_text = _semantic_text_from_hypothesis(hypothesis)
    flags: list[str] = []

    if not instrument:
        flags.append("dossier_missing_instrument")

    # Pull TOOLS lazily — fail gracefully if tic isn't importable.
    tools: dict[str, Any] = {}
    try:
        _ensure_tic_on_path()
        from tic.desk.tools import TOOLS  # type: ignore
        tools = TOOLS
    except Exception as e:  # noqa: BLE001
        warnings.warn(f"pull_dossier: tic.desk.tools unavailable: {e}")
        flags.append("dossier_tic_unavailable")
        return EvidenceDossier(
            hypothesis_id=hyp_id,
            instrument=instrument,
            quality_flags=flags,
        )

    # ------------------------------------------------------------------
    # semantic_search — semantic_neighbors
    # ------------------------------------------------------------------
    semantic_raw = _safe_call(
        tools, "semantic_search", flags=flags,
        query=semantic_text or hyp_id or instrument or "market regime",
        kinds=["claim", "event", "artifact"],
        k=int(semantic_k),
    )
    semantic_neighbors = _coerce_semantic_neighbors(semantic_raw)

    # ------------------------------------------------------------------
    # find_similar_setups
    # ------------------------------------------------------------------
    similar_raw = None
    if instrument:
        similar_raw = _safe_call(
            tools, "find_similar_setups", flags=flags,
            entity_symbol=instrument,
            top_k=int(similar_top_k),
            fwd_days=int(similar_fwd_days),
        )
    similar_setups = _coerce_similar_setups(similar_raw)

    # ------------------------------------------------------------------
    # find_confluence
    # ------------------------------------------------------------------
    confluence_raw = None
    if instrument:
        confluence_raw = _safe_call(
            tools, "find_confluence", flags=flags,
            entity_symbol=instrument,
            lookback_hours=int(confluence_lookback_hours),
        )
    confluence_votes = _coerce_confluence(confluence_raw)

    # ------------------------------------------------------------------
    # get_correlation_matrix (zero required args)
    # ------------------------------------------------------------------
    corr_raw = _safe_call(tools, "get_correlation_matrix", flags=flags)
    correlation_matrix = _coerce_correlation_matrix(corr_raw, instrument)

    # ------------------------------------------------------------------
    # query_recent_news (requires `ticker`)
    # ------------------------------------------------------------------
    news_raw = None
    if instrument:
        news_raw = _safe_call(
            tools, "query_recent_news", flags=flags,
            ticker=instrument,
            lookback_hours=int(news_lookback_hours),
        )
    recent_news = _coerce_recent_news(news_raw)

    # ------------------------------------------------------------------
    # query_source_health — one global pull (no args).
    # ------------------------------------------------------------------
    sh_raw = _safe_call(
        tools, "query_source_health", flags=flags,
        lookback_hours=int(news_lookback_hours),
    )
    source_health = _coerce_source_health(sh_raw)

    # ------------------------------------------------------------------
    # query_timeseries — one snapshot per metric prefix family.
    # ------------------------------------------------------------------
    timeseries_snapshots: dict[str, Any] = {}
    if instrument:
        for prefix in metric_prefixes:
            ts_raw = _safe_call(
                tools, "query_timeseries", flags=flags,
                entity_symbol=instrument,
                metric_prefix=prefix,
                lookback_hours=int(timeseries_lookback_hours),
                limit=int(timeseries_limit),
            )
            coerced = _coerce_timeseries(ts_raw, prefix)
            if coerced:
                timeseries_snapshots[prefix] = coerced

    # ------------------------------------------------------------------
    # Aggregate stats
    # ------------------------------------------------------------------
    n_claims_pulled = 0
    sources_seen: set[str] = set()
    for n in semantic_neighbors:
        n_claims_pulled += 1
    for s in similar_setups:
        n_claims_pulled += len(s.get("claim_ids") or [])
    for n in recent_news:
        n_claims_pulled += 1
        src = n.get("source")
        if src:
            sources_seen.add(str(src))
    for slug in source_health.keys():
        sources_seen.add(slug)
    n_unique_sources = len(sources_seen)

    if n_claims_pulled == 0:
        flags.append("dossier_empty")
    if not semantic_neighbors:
        flags.append("dossier_semantic_empty")
    if not similar_setups:
        flags.append("dossier_similar_setups_empty")
    if not confluence_votes:
        flags.append("dossier_confluence_empty")

    # De-dup quality flags while preserving order.
    seen_flags: set[str] = set()
    deduped: list[str] = []
    for f in flags:
        if f not in seen_flags:
            seen_flags.add(f)
            deduped.append(f)

    return EvidenceDossier(
        hypothesis_id=hyp_id,
        instrument=instrument,
        semantic_neighbors=semantic_neighbors,
        similar_setups=similar_setups,
        confluence_votes=confluence_votes,
        correlation_matrix=correlation_matrix,
        recent_news=recent_news,
        source_health=source_health,
        timeseries_snapshots=timeseries_snapshots,
        n_claims_pulled=n_claims_pulled,
        n_unique_sources=n_unique_sources,
        pull_cost_usd=0.0,
        quality_flags=deduped,
    )


# ============================================================================
# Markdown rendering — for the researcher draft prompt context
# ============================================================================


def render_dossier_markdown(dossier: EvidenceDossier) -> str:
    """Render the dossier as compact markdown for the researcher prompt.
    Stays under ~6KB so it fits inside the stage-1 user prompt without
    blowing the input token budget."""
    out: list[str] = []
    out.append("### Evidence dossier (stage-0 pull, no LLM)")
    out.append(
        f"- instrument: {dossier.instrument or '?'} | "
        f"n_claims_pulled: {dossier.n_claims_pulled} | "
        f"n_unique_sources: {dossier.n_unique_sources}"
    )
    if dossier.quality_flags:
        out.append(f"- quality_flags: {dossier.quality_flags}")
    out.append("")

    if dossier.semantic_neighbors:
        out.append("#### Semantic neighbors (top {})".format(
            min(len(dossier.semantic_neighbors), 10)
        ))
        for n in dossier.semantic_neighbors[:10]:
            cid = n.get("id") or n.get("object_id") or "?"
            kind = n.get("object_kind") or "?"
            sim = n.get("similarity")
            sim_str = f"{float(sim):.3f}" if isinstance(sim, (int, float)) else "n/a"
            text = (n.get("text") or "")[:200]
            out.append(f"- [claim:{cid}] kind={kind} sim={sim_str} :: {text}")
        out.append("")

    if dossier.similar_setups:
        out.append("#### Similar historical setups")
        for s in dossier.similar_setups[:5]:
            anchor = s.get("match_anchor") or "?"
            score = s.get("similarity_score")
            score_str = (
                f"{float(score):.3f}"
                if isinstance(score, (int, float)) else "n/a"
            )
            fwd = s.get("fwd_return")
            outcome = (s.get("outcome_summary") or "")[:160]
            claim_refs = s.get("claim_ids") or []
            refs = " ".join(f"[claim:{c}]" for c in claim_refs[:3])
            out.append(
                f"- anchor={anchor} sim={score_str} fwd={fwd} "
                f"{refs} :: {outcome}"
            )
        out.append("")

    if dossier.confluence_votes:
        cv = dossier.confluence_votes
        out.append("#### Confluence (8-source vote)")
        out.append(
            f"- score={cv.get('confluence_score')} bull={cv.get('n_bull')} "
            f"bear={cv.get('n_bear')} neutral={cv.get('n_neutral')} "
            f"unavailable={cv.get('n_unavailable')}"
        )
        bf = cv.get("by_family")
        if isinstance(bf, dict):
            for fam, info in list(bf.items())[:8]:
                out.append(f"  - {fam}: {info}")
        out.append("")

    if dossier.correlation_matrix:
        cm = dossier.correlation_matrix
        out.append("#### Correlation matrix")
        out.append(
            f"- n_assets={cm.get('n_assets')} n_pairs={cm.get('n_pairs')} "
            f"regime_shifts={cm.get('regime_shifts_flagged')}"
        )
        for pair in (cm.get("top_pairs") or [])[:5]:
            out.append(f"  - {pair}")
        out.append("")

    if dossier.recent_news:
        out.append("#### Recent news (7d window)")
        for n in dossier.recent_news[:8]:
            ts = n.get("ts") or "?"
            src = n.get("source") or "?"
            cid = n.get("claim_id")
            ref = f" [claim:{cid}]" if cid else ""
            out.append(f"- {ts} | {src}{ref} :: {n.get('headline','')[:200]}")
        out.append("")

    if dossier.source_health:
        degraded = [
            (slug, info) for slug, info in dossier.source_health.items()
            if (info.get("status") or "").lower() not in ("ok", "")
        ]
        if degraded:
            out.append("#### Source health (degraded sources)")
            for slug, info in degraded[:12]:
                out.append(
                    f"- {slug}: status={info.get('status')} "
                    f"consec_fail={info.get('consecutive_failures')}"
                )
            out.append("")

    if dossier.timeseries_snapshots:
        out.append("#### Timeseries snapshots")
        for prefix, snap in dossier.timeseries_snapshots.items():
            out.append(
                f"- {prefix}: latest_ts={snap.get('latest_ts')} "
                f"age_s={snap.get('latest_age_s')} "
                f"n_points={len(snap.get('points') or [])}"
            )
        out.append("")

    return "\n".join(out)


__all__ = [
    "EvidenceDossier",
    "pull_dossier",
    "render_dossier_markdown",
]

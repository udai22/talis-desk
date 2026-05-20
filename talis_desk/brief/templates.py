"""Brief markdown templates — pure-data rendering helpers.

The brief is rendered as markdown (primary path) per the SOTA Desk
Architecture v2 (see `wiki/SOTA_DESK_ARCHITECTURE.md` Section 5 — Manual Eval
Dashboard; the brief is downstream of trade ideas per Section 7 - kill
switch + risk surfaces). HTML/JSON formats are honest stubs for now;
markdown is the contract.

Every section accepts plain dicts/lists so the composer can swap a real
talis-tic/desk record for a synthetic preview row without changing
templates. Numbers that come from a "stale_source" or "cap_artifact"
quality_flag get a trailing tag so the reader does NOT mistake them for
clean values.

Honest gaps:
  - `render_html` is a stub: it wraps the markdown in <pre>. A future
    phase can swap in a proper renderer.
  - `render_json` returns the structured_payload as-is (no compaction).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Iterable, Optional


# ============================================================================
# Quality-flag helpers
# ============================================================================

#: Flags that affect the trustworthiness of a number. The brief surfaces
#: these inline next to the value they affect so the reader can tell which
#: numbers came from a degraded source.
NUMERIC_AFFECTING_FLAGS = frozenset({
    "stale_source", "cap_artifact", "fallback_source", "synthetic",
    "estimated", "low_confidence",
})


def _flag_tag(flags: Iterable[str]) -> str:
    """Inline tag for a number whose lineage has a quality flag. Empty
    string if no flags affect the value. The tag is bracketed so it
    visually separates from the number itself."""
    affecting = [f for f in (flags or []) if f in NUMERIC_AFFECTING_FLAGS]
    if not affecting:
        return ""
    return f" [_{'/'.join(sorted(set(affecting)))}_]"


def _fmt_pct(v: Optional[float], digits: int = 2) -> str:
    """Render a fraction as `+0.50%`. None -> `n/a`."""
    if v is None:
        return "n/a"
    return f"{v:+.{digits}f}%"


def _fmt_num(v: Optional[float], digits: int = 4) -> str:
    if v is None:
        return "n/a"
    return f"{v:.{digits}f}"


def _fmt_dt(v: Any) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, datetime):
        return v.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    s = str(v)
    # Best-effort ISO -> human render
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(
            timezone.utc
        ).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return s


def _trunc(s: Optional[str], n: int) -> str:
    if not s:
        return ""
    s = str(s)
    if len(s) <= n:
        return s
    return s[: max(0, n - 1)].rstrip() + "..."


# ============================================================================
# Section renderers
# ============================================================================

def render_header(scope: str, as_of: datetime) -> str:
    """`# Talis Daily Brief — {scope} — {as_of} UTC`"""
    return f"# Talis Daily Brief — {scope} — {_fmt_dt(as_of)}"


def render_headline(headline_md: Optional[str],
                    fallback_summary: Optional[str]) -> str:
    """The '## Headline' section. If `headline_md` is empty (headline LLM
    was unavailable and every provider in the chain failed — see
    composer's BriefHeadlineUnavailableError path), we surface that gap
    explicitly instead of fabricating templated text. The `fallback_summary`
    parameter is kept for back-compat but is always None in the no-stubs
    code path."""
    body = (headline_md or "").strip()
    if not body and fallback_summary:
        # Legacy callers (pre-no-stubs patch) may still pass a non-None
        # fallback_summary. We accept it but the canonical desk code path
        # never does this.
        body = fallback_summary.strip()
    if not body:
        body = (
            "_Headline unavailable — every LLM provider in the headline "
            "fallback chain returned empty/errors this cycle. The "
            "structured sections below are unaffected. See methodology._"
        )
    return "## Headline\n\n" + body


def render_open_trade_book(rows: list[dict[str, Any]]) -> str:
    """Table of open trade ideas. `rows` are dicts pulled from
    `mv_trade_book_open` joined with edge_thesis + quality_flags."""
    lines = [f"## Open Trade Book ({len(rows)})"]
    if not rows:
        lines.append("")
        lines.append("_No open trade ideas at this cycle. See Quiet Cycle below._")
        return "\n".join(lines)
    lines.append("")
    lines.append(
        "| ID | Instrument | Direction | Sizing | Entry | Stop | Target | "
        "Conf | Edge thesis | Flags |"
    )
    lines.append(
        "|----|------------|-----------|--------|-------|------|--------|"
        "------|-------------|-------|"
    )
    for r in rows:
        flags = list(r.get("quality_flags") or [])
        tag = _flag_tag(flags)
        entry = r.get("entry") or {}
        stop = r.get("stop") or {}
        target = r.get("target") or {}
        sizing = r.get("sizing") or {}
        entry_px = entry.get("limit_px")
        stop_px = stop.get("px")
        target_px = target.get("px") if isinstance(target, dict) else None
        risk_pct = sizing.get("risk_pct")
        sizing_str = (
            f"{risk_pct * 100:.2f}bps" if risk_pct is not None
            else "n/a"
        )
        thesis = _trunc(r.get("edge_thesis"), 80)
        lines.append(
            f"| `{r.get('id','?')}` "
            f"| {r.get('instrument','?')} "
            f"| {r.get('direction','?')} "
            f"| {sizing_str}{tag} "
            f"| {_fmt_num(entry_px, 2)} "
            f"| {_fmt_num(stop_px, 2)} "
            f"| {_fmt_num(target_px, 2)} "
            f"| {_fmt_num(r.get('confidence'), 2)} "
            f"| {thesis} "
            f"| {','.join(flags) if flags else '-'} |"
        )
    return "\n".join(lines)


def render_closed_trade_book(
    rows: list[dict[str, Any]],
    metrics: Optional[dict[str, Any]],
) -> str:
    """Closed trade ideas in last 7d + aggregate metrics."""
    lines = [f"## Closed Trade Book (last 7d, n={len(rows)})"]
    if not rows:
        lines.append("")
        lines.append("_No closed ideas in the last 7d window._")
    else:
        lines.append("")
        lines.append(
            "| ID | Instrument | Realized PnL | Alpha vs bench | Brier | Outcome |"
        )
        lines.append(
            "|----|------------|--------------|----------------|-------|---------|"
        )
        for r in rows:
            lines.append(
                f"| `{r.get('id','?')}` "
                f"| {r.get('instrument','?')} "
                f"| {_fmt_pct(r.get('realized_return_after_fees_pct'))} "
                f"| {_fmt_pct(r.get('contributed_alpha_pct'))} "
                f"| {_fmt_num(r.get('brier'), 4)} "
                f"| {r.get('status','?')} |"
            )

    # Aggregate stats line (always render, even on no rows)
    lines.append("")
    if metrics:
        hr = metrics.get("hit_rate")
        sh = metrics.get("sharpe")
        ap = metrics.get("avg_alpha_pct")
        ba = metrics.get("brier_avg")
        lines.append(
            "**Aggregate:** "
            f"hit_rate={'n/a' if hr is None else f'{hr*100:.1f}%'}, "
            f"sharpe={_fmt_num(sh, 2)}, "
            f"avg_alpha={_fmt_pct(ap)}, "
            f"brier_avg={_fmt_num(ba, 4)} "
            f"(window={metrics.get('window_days','n/a')}d, "
            f"n_closed={metrics.get('n_closed',0)})"
        )
    else:
        lines.append("**Aggregate:** _no metrics computed_")
    return "\n".join(lines)


def render_hot_hypotheses(rows: list[dict[str, Any]]) -> str:
    """Active hypotheses with heat_score > 0.7 + supporting / contradicting
    citations."""
    lines = [f"## Hot Hypotheses ({len(rows)})"]
    if not rows:
        lines.append("")
        lines.append("_No hypotheses with heat_score > 0.7 currently active._")
        return "\n".join(lines)
    lines.append("")
    for h in rows:
        pp = _fmt_num(h.get("posterior_prob"), 2)
        hs = _fmt_num(h.get("heat_score"), 2)
        supporters = h.get("supporting_ids") or []
        contradicters = h.get("contradicting_ids") or []
        exp_at = h.get("expected_resolution_at")
        lines.append(
            f"- **{h.get('title','(no title)')}** "
            f"(posterior={pp}, heat={hs}) by `{h.get('specialist_id','?')}`"
        )
        body = _trunc(h.get("hypothesis_text"), 280)
        if body:
            lines.append(f"  - {body}")
        lines.append(
            "  - Supporting: "
            + (", ".join(f"`{cid}`" for cid in supporters[:8])
               if supporters else "_none_")
        )
        lines.append(
            "  - Contradicting: "
            + (", ".join(f"`{cid}`" for cid in contradicters[:8])
               if contradicters else "_none_")
        )
        if exp_at:
            lines.append(f"  - Expected resolution: {_fmt_dt(exp_at)}")
    return "\n".join(lines)


def render_debates(rows: list[dict[str, Any]]) -> str:
    """Debates judged in the last 24h with verdict, rationale, follow-up."""
    lines = [f"## Recent Debates ({len(rows)} judged in last 24h)"]
    if not rows:
        lines.append("")
        lines.append("_No judged debates in the last 24h._")
        return "\n".join(lines)
    lines.append("")
    for d in rows:
        winner = d.get("winner") or "_no clear winner_"
        rationale = _trunc(d.get("rationale"), 280)
        fua = d.get("follow_up_action")
        if isinstance(fua, dict):
            fua_str = fua.get("type") or "_no follow-up_"
        else:
            fua_str = fua or "_no follow-up_"
        lines.append(
            f"- **{d.get('trigger_kind','?')}** on `{d.get('trigger_id','?')}` "
            f"-> Winner: **{winner}** (conf={_fmt_num(d.get('judge_confidence'),2)}) "
            f"| Judge: `{d.get('judge_provider','?')}/{d.get('judge_model','?')}`"
        )
        if rationale:
            lines.append(f"  - Rationale: {rationale}")
        lines.append(f"  - Follow-up: {fua_str}")
    return "\n".join(lines)


def render_playbooks(rows: list[dict[str, Any]]) -> str:
    """Currently-triggered approved playbooks."""
    lines = [f"## Triggered Playbooks ({len(rows)})"]
    if not rows:
        lines.append("")
        lines.append("_No approved playbooks currently triggered._")
        return "\n".join(lines)
    lines.append("")
    for p in rows:
        hr = p.get("historical_hit_rate")
        hr_str = "n/a" if hr is None else f"{hr*100:.1f}%"
        n_hist = p.get("historical_trigger_count") or 0
        ev = p.get("evidence_ids") or []
        ti_id = p.get("instantiated_trade_idea_id")
        lines.append(
            f"- **{p.get('name','?')}** v{p.get('version','?')} "
            f"by `{p.get('owner_specialist','?')}`"
        )
        lines.append(
            f"  - Historical: hit_rate={hr_str} over {n_hist} triggers"
        )
        if ev:
            lines.append(
                "  - Live trigger evidence: "
                + ", ".join(f"`{e}`" for e in ev[:8])
            )
        if ti_id:
            lines.append(f"  - Auto-generated trade idea: `{ti_id}`")
    return "\n".join(lines)


def render_source_health(rows: list[dict[str, Any]]) -> str:
    """Sources currently stale or failing. Sourced from talis-tic's
    `source_health` table via TICStore (read-only)."""
    lines = ["## Source Health Watch"]
    if not rows:
        lines.append("")
        lines.append(
            "_All sources OK in the lookback window (or TICStore not available; "
            "see Methodology Notes)._"
        )
        return "\n".join(lines)
    lines.append("")
    lines.append("Sources currently stale or failing — agents should hedge any "
                 "claim citing these:")
    lines.append("")
    for s in rows:
        slug = s.get("source_slug") or s.get("slug") or "?"
        status = s.get("current_status") or s.get("status") or "?"
        last_ok = _fmt_dt(s.get("last_successful_at"))
        err = _trunc(s.get("last_error"), 100)
        lines.append(
            f"- `{slug}`: status=**{status}**, last_successful_at={last_ok}"
            + (f", error={err}" if err else "")
        )
    return "\n".join(lines)


def render_cycle_stats(stats: dict[str, Any]) -> str:
    """Top-level cycle stats. Reads aggregates pre-computed by the composer."""
    lines = ["## Cycle Stats"]
    lines.append("")
    nac = stats.get("novel_and_correct_pct")
    nac_str = "n/a (requires Phase 6 novelty scoring)" if nac is None else f"{nac*100:.1f}%"
    lines.append(f"- Specialist: `{stats.get('specialist_id','desk')}` "
                 f"({stats.get('persona_version','n/a')})")
    lines.append(f"- Tool calls: {stats.get('n_tool_calls', 0)}")
    lines.append(f"- Cost: ${_fmt_num(stats.get('cost_usd', 0.0), 4)}")
    lines.append(f"- Hypotheses proposed: {stats.get('n_hypotheses_proposed', 0)}")
    lines.append(f"- Trade ideas emitted: {stats.get('n_trade_ideas_emitted', 0)}")
    lines.append(f"- Debates triggered: {stats.get('n_debates', 0)}")
    lines.append(f"- Novel-and-correct claim share: {nac_str}")
    return "\n".join(lines)


def render_quiet_cycle() -> str:
    """Rendered in place of empty tables when the desk had no signal."""
    return (
        "## Quiet Cycle\n"
        "\n"
        "No open trade ideas and no hot hypotheses this cycle. The desk "
        "spent its time scanning sources, refreshing priors, and looking "
        "for setups that didn't qualify against the trade-idea validation "
        "gates (see `talis_desk/trade_ideas/model.py:validate_trade_idea`). "
        "A quiet cycle is not a failure — forced trades degrade Brier and "
        "PnL. The next cycle starts at the top of the loop."
    )


def render_methodology_notes(
    quality_flags: list[str],
    bitemporal_anchor: datetime,
) -> str:
    """Static methodology block. Surfaces bubbled-up quality flags."""
    lines = ["## Methodology Notes"]
    lines.append("")
    lines.append(
        "- Trade ideas are **paper-only** until the desk demonstrates 30 days "
        "of S-tier metrics (per `wiki/SOTA_DESK_ARCHITECTURE.md` v2 §1 + §7)."
    )
    lines.append(
        f"- Bitemporal replay available — every claim, hypothesis, and trade "
        f"idea cited above has a `valid_from`/`transaction_from`. This brief "
        f"is anchored at {_fmt_dt(bitemporal_anchor)}."
    )
    lines.append(
        "- Quality flags from upstream sources are surfaced inline; an "
        "unflagged number does NOT mean perfect provenance — cross-check "
        "the Source Health Watch table for the sources behind the numbers."
    )
    if quality_flags:
        lines.append(
            "- Cited claims carry these quality flags this cycle: "
            + ", ".join(f"`{f}`" for f in sorted(set(quality_flags)))
        )
    return "\n".join(lines)


# ============================================================================
# Composite renderer
# ============================================================================

def render_brief_markdown(sections: dict[str, str]) -> str:
    """Glue all section blocks into one markdown document. Sections that are
    None or empty are skipped (lets the composer build the doc piece-wise)."""
    order = [
        "header",
        "headline",
        "open_trade_book",
        "closed_trade_book",
        "hot_hypotheses",
        "debates",
        "playbooks",
        "source_health",
        "quiet_cycle",
        "cycle_stats",
        "methodology",
    ]
    blocks = [sections[k] for k in order if sections.get(k)]
    return "\n\n".join(blocks).rstrip() + "\n"


def render_html(markdown: str) -> str:
    """Honest stub: wrap markdown in a <pre> block. Future phase: real
    markdown -> HTML rendering."""
    safe = markdown.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return (
        "<!doctype html>\n<html><head><meta charset='utf-8'>"
        "<title>Talis Daily Brief</title></head>"
        f"<body><pre style='font-family:monospace;white-space:pre-wrap'>{safe}"
        "</pre></body></html>"
    )


def render_json(payload: dict[str, Any]) -> str:
    """Stub JSON output — returns the structured_payload verbatim."""
    return json.dumps(payload, indent=2, sort_keys=True, default=str)

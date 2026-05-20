"""Six-stage adversarial research-report pipeline.

This is the polished pipeline that wraps the original 3-stage baseline
(researcher → critic → revision) with three new stages before and three
after, producing **industry-grade institutional research reports** that
displace what a JPM/Goldman/Citadel desk publishes:

  Stage 0 — DOSSIER PULL (`dossier.pull_dossier`, no LLM)
      Pull comprehensive evidence via tic.desk.tools: semantic neighbors,
      similar setups, confluence votes, correlation matrix, recent news,
      source health, timeseries snapshots.

  Stage 1 — COMPARABLES (`comparables.find_comparables`, 1 Sonnet call)
      Structured analogs + consensus position + our divergence + edge
      durability.

  Stage 2 — RESEARCHER DRAFT (Opus, ~5K-token output)
      Hedge-fund PM voice. 600-1500 words with section structure that
      includes Historical Analogs, Consensus Divergence, Sensitivity
      Analysis, Position Sizing Rationale, Falsifiable Forward Statement.

  Stage 3 — DUAL ADVERSARIAL CRITIC (Opus × 2, parallel via asyncio)
      Risk reviewer + methodology reviewer in parallel. Severity merged
      as MAX(risk, methodology) over the {green < yellow < red} order.

  Stage 4 — ITERATIVE IMPROVE LOOP (≤3 turns)
      Re-runs revision + dual-critic until severity=green OR no
      improvement (must_address shrinks <20%) OR turn limit hit. On
      diminishing returns, stop wasting compute.

  Stage 5 — COPY-EDIT POLISH (`polish.polish_prose`, 1 Haiku call)
      Prose polish only — never modifies claims/citations.

  Stage 6 — GRADE & SCORE (`grade.grade_report`, 1 Sonnet call)
      5-dimension score (clarity, evidence_depth, novelty_vs_consensus,
      falsifiability, sizing_rigor) + overall. Reports < 7.0 get
      `below_grade_threshold` quality flag.

# Cost target

~$0.40-0.60 per report; 72 reports/day → $31/day. Comfortable under
$100/day desk cap.

# No stubs

Every LLM call walks the desk's multi-provider fallback chain. On total
chain failure for any single stage the pipeline degrades gracefully:
the report still emits with the available stages and the missing-stage
flag recorded in `quality_flags`. The only hard failure is the
researcher draft (stage 2) — without a body there is no report.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import re
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Literal, Optional

from .._tic_config import ensure_tic_on_path as _ensure_tic_on_path
from .comparables import (
    ComparablesPack,
    ComparablesUnavailableError,
    empty_pack as _empty_comparables_pack,
    find_comparables,
    render_comparables_markdown,
)
from .dossier import (
    EvidenceDossier,
    pull_dossier,
    render_dossier_markdown,
)
from .grade import DEFAULT_GRADE_THRESHOLD, ReportGrade, grade_report
from .model import (
    AdversarialSeverity,
    ReportKind,
    ResearchReport,
    _utc_now,
    new_report_id,
)
from .polish import polish_prose


logger = logging.getLogger(__name__)


# ============================================================================
# Fallback chain (heavy reasoning first, then cheaper providers)
# ============================================================================

_REPORT_FALLBACK_CHAIN: list[str] = [
    "anthropic:claude-opus-4-7",
    "anthropic:claude-sonnet-4-6",
    "openai:gpt-5.5",
    "xai:grok-4",
    "deepseek:v4-pro",
    "moonshot:v1-32k",
    "anthropic:claude-haiku-4-5",
    "perplexity:sonar-pro",
]

#: Per-million-token USD estimates. Same shape as runner._COST_PER_MTOK.
_COST_PER_MTOK: dict[str, float] = {
    "anthropic:claude-opus-4-7": 18.0,
    "anthropic:claude-sonnet-4-6": 6.0,
    "anthropic:claude-haiku-4-5": 1.2,
    "openai:gpt-5.5": 12.0,
    "openai:gpt-4o": 8.0,
    "xai:grok-4": 7.0,
    "deepseek:v4-pro": 1.2,
    "moonshot:v1-32k": 2.0,
    "perplexity:sonar-pro": 5.0,
}


def _build_provider_chain(primary: str) -> list[str]:
    seen: set[str] = {primary}
    chain: list[str] = [primary]
    for m in _REPORT_FALLBACK_CHAIN:
        if m not in seen:
            chain.append(m)
            seen.add(m)
    return chain


def _estimate_cost_usd(model: str, system: str, user: str, completion: str) -> float:
    rate = _COST_PER_MTOK.get(model, 6.0)
    in_tokens = (len(system or "") + len(user or "")) / 4.0
    out_tokens = len(completion or "") / 4.0
    total = in_tokens + out_tokens
    if total <= 0:
        return 0.0
    return (total / 1_000_000.0) * rate


# ============================================================================
# Result types + errors
# ============================================================================


class ReportPipelineUnavailableError(RuntimeError):
    """Raised when the researcher draft stage (stage 2) has every
    provider in its fallback chain fail. Without a body there is no
    report; the caller logs + skips this hypothesis. NEVER fabricate.

    Comparables / critics / polish / grade can each individually fail
    without raising — the pipeline records the missing-stage flag and
    continues.
    """


@dataclass
class ResearchPipelineResult:
    """Output of `run_report_pipeline`."""
    report: ResearchReport
    initial_draft_text: str
    critic_text: str
    revision_text: str
    transcript: list[dict[str, Any]]
    total_cost_usd: float
    adversarial_severity: AdversarialSeverity
    abandoned: bool = False
    dossier: Optional[EvidenceDossier] = None
    comparables: Optional[ComparablesPack] = None
    grade: Optional[ReportGrade] = None
    n_revision_turns: int = 1
    stage_costs_usd: dict[str, float] = field(default_factory=dict)


# ============================================================================
# Chat bridge — sync + async wrappers around `tic.desk.models.chat`
# ============================================================================


def _run_chat_sync(
    chat_fn: Any, model: str, system: str, user: str,
    *, max_tokens: Optional[int] = None,
) -> dict[str, Any]:
    async def _do() -> dict[str, Any]:
        return await chat_fn(model, system, user, max_tokens=max_tokens,
                              fallback=None)

    try:
        asyncio.get_running_loop()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(asyncio.run, _do())
            return fut.result(timeout=240)
    except RuntimeError:
        return asyncio.run(_do())


async def _run_chat_async(
    chat_fn: Any, model: str, system: str, user: str,
    *, max_tokens: Optional[int] = None,
) -> dict[str, Any]:
    """Async variant — used to run the dual critics in parallel."""
    return await chat_fn(model, system, user, max_tokens=max_tokens,
                          fallback=None)


# ============================================================================
# JSON repair (same tolerant parser pattern as judge.py / synthesizer)
# ============================================================================


def _parse_json(text: str) -> Optional[dict[str, Any]]:
    text = (text or "").strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    fences = re.findall(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL)
    for chunk in fences:
        try:
            obj = json.loads(chunk.strip())
            if isinstance(obj, dict):
                return obj
        except Exception:
            continue
    start = text.find("{")
    end = text.rfind("}")
    if 0 <= start < end:
        try:
            obj = json.loads(text[start:end + 1])
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    return None


# ============================================================================
# Per-stage prompt builders
# ============================================================================


_STAGE_RESEARCHER_INSTRUCTIONS = (
    "\n\n## Adversarial research-report pipeline — Stage 2: Researcher draft\n"
    "Write a SERIOUS institutional research report. 600-1500 words of "
    "dense, well-cited markdown in HEDGE-FUND PM VOICE. No filler. No "
    "hedging caveats unless materially uncertain. Cite EVERYTHING you "
    "assert numerically — every number references a real [claim:id] "
    "from the dossier or [tc:id] from the BFS evidence chain.\n\n"
    "Structure (use these exact headings):\n\n"
    "  ## TL;DR\n"
    "  (3-4 sentences — the headline claim + setup)\n\n"
    "  ## Thesis & Mechanism\n"
    "  (why does this work? explicit mechanism)\n\n"
    "  ## Evidence\n"
    "  (every numerical claim cites [claim:id] from the dossier)\n\n"
    "  ## Historical Analogs\n"
    "  (the 3 cases from the comparables pack — what happened, why this "
    "is similar/different. Cite the analog's claim_ids.)\n\n"
    "  ## Consensus Divergence\n"
    "  (the street is at X, we are at Y because Z. Pull directly from "
    "the comparables pack's consensus_position + our_divergence.)\n\n"
    "  ## Risks\n"
    "  (>= 2 contradicting items, each with cited evidence. Name the "
    "regime where the thesis breaks.)\n\n"
    "  ## Setup\n"
    "  (one of:\n"
    "    - If trade-actionable: entry / stop / target / invalidation / "
    "expected horizon, anchored on the live snapshot.\n"
    "    - If watchlist: watch_condition + promotion trigger.\n"
    "    - If regime-change/anomaly: regime_break_signal that would "
    "confirm OR kill the thesis.)\n\n"
    "  ## Sensitivity Analysis\n"
    "  (3 scenarios: bull / base / bear. For each: probability (sum to "
    "1.0), price target, key driver. Be honest about distribution width.)\n\n"
    "  ## Falsifiable Forward Statement\n"
    "  (ONE specific number that resolves <=30 days. e.g. 'BTC perp "
    "funding annualized <0 by 2026-06-15' or 'ETH/BTC ratio breaks 0.05 "
    "before next FOMC'. NOT 'price will move'.)\n\n"
    "  ## Position Sizing Rationale\n"
    "  (Kelly fraction OR risk %, max drawdown tolerance, why this size "
    "given the stated conviction. High-conviction = bigger Kelly, with "
    "caveats. Note: if this is a watchlist / blocked / regime piece, "
    "explain why no size is being recommended.)\n\n"
    "Tone: a hedge-fund PM reading this on Monday morning. Output the "
    "markdown body ONLY. No JSON wrapper, no preamble. Start with "
    "'## TL;DR'."
)


_STAGE_RISK_CRITIC_INSTRUCTIONS = (
    "\n\n## Adversarial research-report pipeline — Stage 3a: Risk reviewer\n"
    "You are an institutional risk reviewer at a top-quartile hedge fund.\n"
    "The researcher's draft below makes a directional claim. Your job "
    "is to find the WEAKEST 3 assertions and attack them. For each "
    "attack:\n"
    "  - Cite a SPECIFIC risk, alternative explanation, or missing "
    "piece of evidence the researcher should have addressed.\n"
    "  - Do NOT hedge. If the thesis is fundamentally flawed, say so.\n\n"
    "Output STRICT JSON only. Schema:\n"
    "{\n"
    '  "severity": "green" | "yellow" | "red",\n'
    '  "rationale": "<1-2 sentence overall verdict>",\n'
    '  "attacks": [\n'
    '    {"quote": "<exact phrase from the draft>", '
    '"weakness": "<what is wrong>", '
    '"recommended_fix": "<how to address it>"}\n'
    "  ],\n"
    '  "must_address": ["<short bullet>", ...],\n'
    '  "should_address": ["<short bullet>", ...]\n'
    "}\n\n"
    "Severity rules:\n"
    "  - red    = thesis is fundamentally flawed; recommend KILL.\n"
    "  - yellow = thesis survives but needs material revisions.\n"
    "  - green  = minor polish only — ship after light edits.\n\n"
    "Output JSON ONLY. No prose outside the JSON."
)


_STAGE_METHOD_CRITIC_INSTRUCTIONS = (
    "\n\n## Adversarial research-report pipeline — Stage 3b: Methodology reviewer\n"
    "You are reviewing the RIGOR of this report's analytical method. "
    "Attack on these axes:\n"
    "  (1) citation_integrity — every numerical claim cites a real "
    "[claim:id] or [tc:id]. Uncited numbers are a defect.\n"
    "  (2) analog_aptness — historical analogs are genuinely similar, "
    "not cherry-picked. The 'key differences' must be honest.\n"
    "  (3) sensitivity_rigor — the 3-scenario analysis covers the "
    "realistic distribution. Probabilities sum to 1. Bear case is "
    "credible (not strawman).\n"
    "  (4) sizing_self_consistency — position sizing matches stated "
    "conviction. High-conviction = bigger Kelly (with caveats). Watch "
    "for sizing that contradicts the TL;DR.\n\n"
    "Output STRICT JSON only. Schema:\n"
    "{\n"
    '  "severity": "green" | "yellow" | "red",\n'
    '  "rationale": "<1-2 sentence overall verdict>",\n'
    '  "methodology_attacks": [\n'
    '    {"axis": "citation_integrity"|"analog_aptness"|"sensitivity_rigor"|"sizing_self_consistency",\n'
    '     "weakness": "<what is wrong>",\n'
    '     "fix": "<how to address it>"}\n'
    "  ],\n"
    '  "must_address": ["<short bullet>", ...],\n'
    '  "should_address": ["<short bullet>", ...]\n'
    "}\n\n"
    "Severity rules (same as risk reviewer): red = fundamentally "
    "flawed methodology; yellow = needs revisions; green = ship.\n\n"
    "Output JSON ONLY. No prose outside the JSON."
)


_STAGE_REVISION_INSTRUCTIONS = (
    "\n\n## Adversarial research-report pipeline — Stage 4: Revision\n"
    "Revise your original draft to address EVERY `must_address` item "
    "from BOTH critics (risk + methodology) and AT LEAST HALF the "
    "`should_address` items. Keep the same section structure. Stay in "
    "the researcher's voice — do NOT include meta-commentary about the "
    "revision itself.\n\n"
    "If the merged critic verdict is RED AND you cannot honestly "
    "defend the thesis after addressing the attacks, abandon the "
    "report. In that case output STRICT JSON ONLY:\n"
    "  {\"abandon\": true, \"reason\": \"<one sentence why>\"}\n\n"
    "Otherwise output the revised markdown body (same shape as "
    "stage 2, starting with '## TL;DR'). NO JSON wrapper for the "
    "markdown case. NO preamble."
)


def _format_hypothesis_block(hypothesis: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"### Hypothesis {hypothesis.get('id') or '?'}")
    lines.append(f"- title: {hypothesis.get('title') or '(untitled)'}")
    posterior = hypothesis.get("posterior_prob")
    lines.append(f"- posterior: {posterior}")
    heat = hypothesis.get("heat_score")
    lines.append(f"- heat_score: {heat}")
    status = hypothesis.get("status")
    if status:
        lines.append(f"- status: {status}")
    entities = hypothesis.get("entity_ids") or []
    lines.append(f"- entities: {list(entities)}")
    claim_ids = hypothesis.get("claim_ids") or []
    if claim_ids:
        lines.append(f"- claim_ids: {list(claim_ids)[:10]}")
    text = hypothesis.get("hypothesis_text") or ""
    if text:
        clipped = text if len(text) <= 1200 else text[:1200] + "..."
        lines.append(f"- text: {clipped}")
    return "\n".join(lines)


def _format_evidence_block(tool_evidence: list[dict[str, Any]]) -> str:
    if not tool_evidence:
        return "  (no BFS evidence rows surfaced)"
    out: list[str] = []
    for ev in tool_evidence[:20]:
        tcid = ev.get("tool_call_log_id") or "?"
        uri = ev.get("tool_uri") or "?"
        delta = ev.get("posterior_delta")
        contradicts = ev.get("contradicts")
        rationale = (ev.get("rationale") or "")[:200]
        out.append(
            f"- tool_call_log_id={tcid} uri={uri} "
            f"delta={delta} contradicts={contradicts} :: {rationale}"
        )
    return "\n".join(out)


def _format_market_snapshot_block(snapshot: Optional[dict[str, Any]]) -> str:
    if not snapshot:
        return "  (no live market snapshot — synthesizer not run for this report)"
    out: list[str] = []
    for inst, snap in (snapshot or {}).items():
        if not isinstance(snap, dict):
            continue
        out.append(f"- {inst}:")
        for k, v in snap.items():
            if k == "instrument":
                continue
            out.append(f"    {k}: {v}")
    return "\n".join(out)


def _format_artifact_block(artifact: Optional[dict[str, Any]]) -> str:
    if not artifact:
        return "  (no primary artifact — this report is the standalone output)"
    out: list[str] = []
    for k, v in artifact.items():
        if isinstance(v, (dict, list)):
            v_str = json.dumps(v, default=str)
            if len(v_str) > 400:
                v_str = v_str[:400] + "..."
        else:
            v_str = str(v)
            if len(v_str) > 400:
                v_str = v_str[:400] + "..."
        out.append(f"- {k}: {v_str}")
    return "\n".join(out)


def _build_researcher_user_prompt(
    *,
    specialist_id: str,
    hypothesis: dict[str, Any],
    primary_artifact: Optional[dict[str, Any]],
    tool_evidence: list[dict[str, Any]],
    market_snapshot: Optional[dict[str, Any]],
    source_health: dict[str, Any],
    dossier: EvidenceDossier,
    comparables: ComparablesPack,
) -> str:
    lines: list[str] = []
    lines.append(f"## Specialist: {specialist_id}")
    lines.append("")
    lines.append("## Surviving hypothesis")
    lines.append(_format_hypothesis_block(hypothesis))
    lines.append("")
    lines.append("## Primary artifact (trade idea / watchlist / blocked — if any)")
    lines.append(_format_artifact_block(primary_artifact))
    lines.append("")
    lines.append("## BFS evidence chain (resolved tool calls that survived)")
    lines.append(_format_evidence_block(tool_evidence))
    lines.append("")
    lines.append("## Live market snapshot (Hyperliquid)")
    lines.append(_format_market_snapshot_block(market_snapshot))
    lines.append("")
    # Dossier + comparables — the new context that lifts our reports above JPM.
    lines.append("## Evidence dossier (stage-0 pull)")
    lines.append(render_dossier_markdown(dossier))
    lines.append("")
    lines.append("## Comparables (stage-1)")
    lines.append(render_comparables_markdown(comparables))
    lines.append("")
    if source_health:
        lines.append("## Additional source health")
        lines.append(json.dumps(source_health, default=str, indent=2)[:1200])
        lines.append("")
    lines.append(
        "## Instruction\nWrite the institutional research report per "
        "the system prompt. 600-1500 words. Markdown body only. Use "
        "ONLY claim_ids that actually appear in the dossier or BFS "
        "evidence — never invent citations."
    )
    return "\n".join(lines)


def _build_risk_critic_user_prompt(initial_draft: str) -> str:
    return (
        "## Researcher's draft (attack this)\n\n"
        f"{initial_draft.strip()}\n\n"
        "## Instruction\n"
        "Find the 3 weakest assertions. Output JSON per the schema."
    )


def _build_method_critic_user_prompt(initial_draft: str) -> str:
    return (
        "## Researcher's draft (attack the methodology)\n\n"
        f"{initial_draft.strip()}\n\n"
        "## Instruction\n"
        "Attack the methodology along the 4 axes. Output JSON per the "
        "schema."
    )


def _build_revision_user_prompt(
    initial_draft: str, merged_verdict: dict[str, Any],
) -> str:
    return (
        "## Original draft\n\n"
        f"{initial_draft.strip()}\n\n"
        "## Merged critic verdict (risk + methodology)\n\n"
        f"{json.dumps(merged_verdict, default=str, indent=2)}\n\n"
        "## Instruction\n"
        "Revise to address EVERY must_address item and at least half "
        "of should_address. Output the revised markdown body OR (if "
        "abandoning) the abandon JSON."
    )


# ============================================================================
# Title / abstract / instrument inference from the body markdown
# ============================================================================


def _extract_first_heading_or_sentence(body_md: str) -> str:
    if not body_md:
        return ""
    text = body_md.strip()
    m = re.match(r"^#\s+(.+)$", text, flags=re.MULTILINE)
    if m and "tl;dr" not in m.group(1).lower():
        return m.group(1).strip()[:120]
    m2 = re.search(r"##\s*TL;DR[^\n]*\n+([^\n]+)", text, flags=re.IGNORECASE)
    if m2:
        return m2.group(1).strip()[:120]
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line[:120]
    return ""


def _extract_abstract(body_md: str) -> str:
    if not body_md:
        return ""
    m = re.search(
        r"##\s*TL;DR[^\n]*\n+(.+?)(?=\n##|\Z)",
        body_md, flags=re.IGNORECASE | re.DOTALL,
    )
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip()[:400]
    return body_md.strip()[:400]


def _extract_citation_ids(body_md: str) -> tuple[list[str], list[str]]:
    claims = list(dict.fromkeys(re.findall(
        r"\[claim:([a-zA-Z0-9_\-]+)\]", body_md or "",
    )))
    tcs = list(dict.fromkeys(re.findall(
        r"\[tc:([a-zA-Z0-9_\-]+)\]", body_md or "",
    )))
    return claims, tcs


# ============================================================================
# Stage runners
# ============================================================================


def _run_stage(
    *,
    chat_fn: Any,
    chain: list[str],
    system: str,
    user: str,
    stage_label: str,
    expect_json: bool,
    max_tokens: int,
) -> tuple[str, str, float, Optional[dict[str, Any]]]:
    """Run one LLM stage synchronously. Returns (text, model_used,
    cost_usd, parsed_json_or_None). Raises ReportPipelineUnavailableError
    on total chain failure."""
    last_error: Optional[str] = None
    for m in chain:
        try:
            res = _run_chat_sync(chat_fn, m, system, user, max_tokens=max_tokens)
        except Exception as e:  # noqa: BLE001
            last_error = f"{m}: chat_call_failed: {e!s}"
            continue
        text = (res.get("text") or "").strip()
        err = res.get("error")
        if err:
            last_error = f"{m}: {err}"
            continue
        if not text:
            last_error = f"{m}: empty_completion"
            continue
        parsed: Optional[dict[str, Any]] = None
        if expect_json:
            parsed = _parse_json(text)
            if parsed is None:
                last_error = f"{m}: unparseable_json"
                continue
        model_used = res.get("model_used") or m
        cost = _estimate_cost_usd(model_used, system, user, text)
        return text, model_used, cost, parsed

    raise ReportPipelineUnavailableError(
        f"All {len(chain)} providers failed at stage={stage_label!r}. "
        f"Last error: {last_error}"
    )


async def _run_stage_async(
    *,
    chat_fn: Any,
    chain: list[str],
    system: str,
    user: str,
    stage_label: str,
    expect_json: bool,
    max_tokens: int,
) -> tuple[str, str, float, Optional[dict[str, Any]]]:
    """Async variant for parallel dual-critic. Raises on total failure."""
    last_error: Optional[str] = None
    for m in chain:
        try:
            res = await _run_chat_async(chat_fn, m, system, user,
                                          max_tokens=max_tokens)
        except Exception as e:  # noqa: BLE001
            last_error = f"{m}: chat_call_failed: {e!s}"
            continue
        text = (res.get("text") or "").strip()
        err = res.get("error")
        if err:
            last_error = f"{m}: {err}"
            continue
        if not text:
            last_error = f"{m}: empty_completion"
            continue
        parsed: Optional[dict[str, Any]] = None
        if expect_json:
            parsed = _parse_json(text)
            if parsed is None:
                last_error = f"{m}: unparseable_json"
                continue
        model_used = res.get("model_used") or m
        cost = _estimate_cost_usd(model_used, system, user, text)
        return text, model_used, cost, parsed

    raise ReportPipelineUnavailableError(
        f"All {len(chain)} providers failed at stage={stage_label!r}. "
        f"Last error: {last_error}"
    )


# ============================================================================
# Dual-critic merge
# ============================================================================


_SEVERITY_ORDER = {"green": 0, "yellow": 1, "red": 2}


def _normalize_severity(raw: Any) -> AdversarialSeverity:
    s = str(raw or "yellow").lower().strip()
    return s if s in ("green", "yellow", "red") else "yellow"  # type: ignore[return-value]


def _merge_critic_verdicts(
    risk: dict[str, Any], method: dict[str, Any],
) -> dict[str, Any]:
    """Merge two critic JSON verdicts into a single combined verdict.

    Severity is MAX over {green=0, yellow=1, red=2}.
    must_address / should_address are concatenated + de-duped (case-insensitive).
    attacks list contains both risk attacks and methodology attacks.
    """
    r_sev = _normalize_severity(risk.get("severity"))
    m_sev = _normalize_severity(method.get("severity"))
    merged_sev = max(
        (r_sev, m_sev), key=lambda s: _SEVERITY_ORDER.get(s, 1),
    )

    def _dedup(items: list[Any]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for it in items or []:
            s = str(it).strip()
            key = s.lower()
            if not s or key in seen:
                continue
            seen.add(key)
            out.append(s)
        return out

    must = _dedup(
        list(risk.get("must_address") or [])
        + list(method.get("must_address") or [])
    )
    should = _dedup(
        list(risk.get("should_address") or [])
        + list(method.get("should_address") or [])
    )
    attacks: list[dict[str, Any]] = []
    for a in (risk.get("attacks") or []) or []:
        if isinstance(a, dict):
            attacks.append({
                "source": "risk",
                "quote": str(a.get("quote") or "")[:300],
                "weakness": str(a.get("weakness") or "")[:400],
                "recommended_fix": str(a.get("recommended_fix") or "")[:400],
            })
    for a in (method.get("methodology_attacks") or []) or []:
        if isinstance(a, dict):
            attacks.append({
                "source": "methodology",
                "axis": str(a.get("axis") or ""),
                "weakness": str(a.get("weakness") or "")[:400],
                "recommended_fix": str(a.get("fix") or "")[:400],
            })
    return {
        "severity": merged_sev,
        "rationale": (
            (risk.get("rationale") or "")
            + " | "
            + (method.get("rationale") or "")
        )[:600],
        "attacks": attacks,
        "must_address": must,
        "should_address": should,
        "_risk_severity": r_sev,
        "_method_severity": m_sev,
    }


def _improvement_detected(prev: dict[str, Any], curr: dict[str, Any]) -> bool:
    """True when curr has materially fewer must_address items than prev.

    The threshold is "shrunk by at least 20%". If the new verdict still
    has >=80% of the prev must_address count, we conclude the critic is
    finding the same defects — diminishing returns, stop the loop.
    """
    prev_n = len(prev.get("must_address") or [])
    curr_n = len(curr.get("must_address") or [])
    if prev_n <= 0:
        return False  # nothing was wrong before; no improvement to make
    return curr_n < int(0.8 * prev_n)


# ============================================================================
# Dual-critic runner
# ============================================================================


def _run_dual_critics(
    *,
    chat_fn: Any,
    risk_chain: list[str],
    method_chain: list[str],
    initial_draft: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], float]:
    """Run the risk + methodology critics IN PARALLEL via asyncio.gather.

    Returns (merged_verdict, transcript_entries, combined_cost_usd).

    If one critic fails its full chain we log + continue with just the
    other. If BOTH fail we raise ReportPipelineUnavailableError — the
    caller treats this as a stage-3 failure (but still emits whatever
    the researcher draft produced with a quality_flag).
    """
    risk_system = (
        "You are an institutional risk reviewer at a top-quartile hedge "
        "fund. Output JSON only."
        + _STAGE_RISK_CRITIC_INSTRUCTIONS
    )
    method_system = (
        "You are a quant methodology reviewer at a top-quartile hedge "
        "fund. Output JSON only."
        + _STAGE_METHOD_CRITIC_INSTRUCTIONS
    )
    risk_user = _build_risk_critic_user_prompt(initial_draft)
    method_user = _build_method_critic_user_prompt(initial_draft)

    async def _both():
        return await asyncio.gather(
            _run_stage_async(
                chat_fn=chat_fn, chain=risk_chain,
                system=risk_system, user=risk_user,
                stage_label="risk_critic", expect_json=True, max_tokens=1800,
            ),
            _run_stage_async(
                chat_fn=chat_fn, chain=method_chain,
                system=method_system, user=method_user,
                stage_label="method_critic", expect_json=True, max_tokens=1800,
            ),
            return_exceptions=True,
        )

    try:
        asyncio.get_running_loop()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(asyncio.run, _both())
            results = fut.result(timeout=300)
    except RuntimeError:
        results = asyncio.run(_both())

    risk_result, method_result = results
    risk_ok = not isinstance(risk_result, Exception)
    method_ok = not isinstance(method_result, Exception)

    if not risk_ok and not method_ok:
        raise ReportPipelineUnavailableError(
            f"Both critics failed. risk_err={risk_result!s}; "
            f"method_err={method_result!s}"
        )

    transcript: list[dict[str, Any]] = []
    combined_cost = 0.0
    risk_json: dict[str, Any] = {}
    method_json: dict[str, Any] = {}

    if risk_ok:
        rt, rm, rc, rj = risk_result  # type: ignore[misc]
        risk_json = rj or {}
        combined_cost += rc
        transcript.append({
            "role": "risk_critic", "model": rm, "cost_usd": rc,
            "text": rt, "parsed": rj,
        })
    else:
        transcript.append({
            "role": "risk_critic", "model": None, "cost_usd": 0.0,
            "text": "", "parsed": None,
            "error": str(risk_result)[:400],
        })

    if method_ok:
        mt, mm, mc, mj = method_result  # type: ignore[misc]
        method_json = mj or {}
        combined_cost += mc
        transcript.append({
            "role": "methodology_critic", "model": mm, "cost_usd": mc,
            "text": mt, "parsed": mj,
        })
    else:
        transcript.append({
            "role": "methodology_critic", "model": None, "cost_usd": 0.0,
            "text": "", "parsed": None,
            "error": str(method_result)[:400],
        })

    merged = _merge_critic_verdicts(risk_json, method_json)
    return merged, transcript, combined_cost


# ============================================================================
# Public entry point — the 6-stage pipeline
# ============================================================================


def run_report_pipeline(
    *,
    specialist_id: str,
    persona_prompt: str,
    hypothesis: dict[str, Any],
    primary_artifact: Optional[dict[str, Any]],
    tool_evidence: list[dict[str, Any]],
    market_snapshot: Optional[dict[str, Any]],
    source_health: dict[str, Any],
    cycle_id: str,
    report_kind: ReportKind = "trade_idea",
    researcher_model: str = "anthropic:claude-opus-4-7",
    critic_model: str = "anthropic:claude-opus-4-7",
    methodology_critic_model: str = "anthropic:claude-opus-4-7",
    revision_model: str = "anthropic:claude-opus-4-7",
    comparables_model: str = "anthropic:claude-sonnet-4-6",
    polish_model: str = "anthropic:claude-haiku-4-5",
    grade_model: str = "anthropic:claude-sonnet-4-6",
    max_revision_turns: int = 3,
    confidence_hint: Optional[float] = None,
    novelty_score_hint: Optional[float] = None,
    conn: Any = None,
) -> ResearchPipelineResult:
    """Run the 6-stage adversarial research-report pipeline.

    Stages:
      0. Dossier pull (tic.db reads, no LLM)
      1. Comparables (1 Sonnet call) — soft-fails to empty pack
      2. Researcher draft (Opus) — HARD-fails if all providers exhausted
      3. Dual critic (Opus × 2 in parallel) — soft-fails if both exhaust
      4. Revision loop (≤max_revision_turns)
      5. Polish (Haiku) — soft-fails to identity
      6. Grade (Sonnet) — soft-fails to zero grade

    Raises `ReportPipelineUnavailableError` only when stage 2
    (researcher draft) exhausts every provider — without a body there
    is no report.
    """
    # ------------------------------------------------------------------
    # 1. Resolve chat() — hard dependency.
    # ------------------------------------------------------------------
    _ensure_tic_on_path()
    try:
        from tic.desk.models import chat as _chat  # type: ignore
    except Exception as e:
        raise ReportPipelineUnavailableError(
            f"tic.desk.models.chat unavailable: {e!s}"
        ) from e

    stage_costs: dict[str, float] = {}
    transcript: list[dict[str, Any]] = []
    pipeline_quality_flags: list[str] = []

    # ------------------------------------------------------------------
    # Stage 0 — Dossier pull (tic.db reads)
    # ------------------------------------------------------------------
    try:
        dossier = pull_dossier(hypothesis, conn=conn)
    except Exception as e:  # noqa: BLE001
        warnings.warn(f"pipeline: pull_dossier crashed: {e!s}")
        dossier = EvidenceDossier(
            hypothesis_id=str(hypothesis.get("id") or ""),
            instrument=str((hypothesis.get("entity_ids") or [""])[0] or ""),
            quality_flags=["dossier_crashed"],
        )
    stage_costs["dossier"] = float(dossier.pull_cost_usd or 0.0)
    if "dossier_empty" in dossier.quality_flags:
        pipeline_quality_flags.append("dossier_unavailable")
    transcript.append({
        "turn": 0, "role": "dossier", "stage": "dossier_pull",
        "model": None, "cost_usd": stage_costs["dossier"],
        "n_claims_pulled": dossier.n_claims_pulled,
        "n_unique_sources": dossier.n_unique_sources,
        "quality_flags": list(dossier.quality_flags),
    })

    # ------------------------------------------------------------------
    # Stage 1 — Comparables (soft-fails)
    # ------------------------------------------------------------------
    try:
        comparables = find_comparables(
            hypothesis, dossier, model=comparables_model,
        )
    except ComparablesUnavailableError as e:
        warnings.warn(f"pipeline: comparables unavailable: {e!s}")
        comparables = _empty_comparables_pack()
        pipeline_quality_flags.append("comparables_unavailable")
    except Exception as e:  # noqa: BLE001
        warnings.warn(f"pipeline: comparables crashed: {e!s}")
        comparables = _empty_comparables_pack()
        pipeline_quality_flags.append("comparables_unavailable")
    stage_costs["comparables"] = float(comparables.cost_usd or 0.0)
    transcript.append({
        "turn": 1, "role": "comparables", "stage": "comparables_pull",
        "model": comparables.model_used,
        "cost_usd": stage_costs["comparables"],
        "n_analogs": len(comparables.historical_analogs),
        "edge_durability_days": comparables.edge_durability_days,
        "quality_flags": list(comparables.quality_flags),
    })

    # ------------------------------------------------------------------
    # Stage 2 — Researcher draft (HARD-fails)
    # ------------------------------------------------------------------
    persona = (persona_prompt or "").rstrip()
    researcher_system = persona + _STAGE_RESEARCHER_INSTRUCTIONS
    researcher_user = _build_researcher_user_prompt(
        specialist_id=specialist_id,
        hypothesis=hypothesis,
        primary_artifact=primary_artifact,
        tool_evidence=tool_evidence,
        market_snapshot=market_snapshot,
        source_health=source_health or {},
        dossier=dossier,
        comparables=comparables,
    )
    researcher_chain = _build_provider_chain(researcher_model)
    initial_draft_text, researcher_model_used, researcher_cost, _ = _run_stage(
        chat_fn=_chat,
        chain=researcher_chain,
        system=researcher_system,
        user=researcher_user,
        stage_label="researcher_draft",
        expect_json=False,
        max_tokens=5000,
    )
    stage_costs["researcher"] = float(researcher_cost or 0.0)
    transcript.append({
        "turn": 2, "role": "researcher", "stage": "researcher_draft",
        "model": researcher_model_used,
        "cost_usd": stage_costs["researcher"],
        "text": initial_draft_text,
    })

    # ------------------------------------------------------------------
    # Stage 3 — Dual adversarial critic (in parallel)
    # ------------------------------------------------------------------
    risk_chain = _build_provider_chain(critic_model)
    method_chain = _build_provider_chain(methodology_critic_model)
    try:
        merged_verdict, critic_turns, critic_cost = _run_dual_critics(
            chat_fn=_chat,
            risk_chain=risk_chain,
            method_chain=method_chain,
            initial_draft=initial_draft_text,
        )
    except ReportPipelineUnavailableError as e:
        warnings.warn(f"pipeline: dual critic unavailable: {e!s}")
        merged_verdict = {
            "severity": "yellow",
            "rationale": "critic_unavailable",
            "attacks": [], "must_address": [], "should_address": [],
        }
        critic_turns = []
        critic_cost = 0.0
        pipeline_quality_flags.append("critic_unavailable")
    stage_costs["critic"] = float(critic_cost or 0.0)
    severity: AdversarialSeverity = _normalize_severity(
        merged_verdict.get("severity")
    )

    # Critic transcript turns (turn 3a + 3b)
    for i, t in enumerate(critic_turns):
        t_entry = dict(t)
        t_entry["turn"] = f"3{'ab'[min(i, 1)]}"
        t_entry["stage"] = (
            "risk_critic" if t.get("role") == "risk_critic"
            else "methodology_critic"
        )
        transcript.append(t_entry)

    transcript.append({
        "turn": "3-merge", "role": "critic_merge",
        "stage": "critic_merge",
        "severity": severity,
        "n_must_address": len(merged_verdict.get("must_address") or []),
        "n_should_address": len(merged_verdict.get("should_address") or []),
    })

    # ------------------------------------------------------------------
    # Stage 4 — Iterative improve loop (≤max_revision_turns)
    # ------------------------------------------------------------------
    current_draft = initial_draft_text
    current_verdict = merged_verdict
    revision_text = initial_draft_text
    abandoned = False
    abandon_reason: Optional[str] = None
    n_revision_turns = 0
    revision_cost_total = 0.0
    revision_model_last = ""

    if severity == "green":
        # No revision needed — current draft is final.
        revision_text = initial_draft_text
        transcript.append({
            "turn": "4-skip", "role": "revision",
            "stage": "revision_skipped_green",
            "model": None, "cost_usd": 0.0,
        })
    else:
        revision_chain = _build_provider_chain(revision_model)
        revision_system = persona + _STAGE_REVISION_INSTRUCTIONS

        while n_revision_turns < max(1, int(max_revision_turns)):
            n_revision_turns += 1

            try:
                revision_user = _build_revision_user_prompt(
                    current_draft, current_verdict,
                )
                rev_text, rev_model_used, rev_cost, _ = _run_stage(
                    chat_fn=_chat,
                    chain=revision_chain,
                    system=revision_system,
                    user=revision_user,
                    stage_label=f"revision_turn_{n_revision_turns}",
                    expect_json=False,
                    max_tokens=5000,
                )
            except ReportPipelineUnavailableError as e:
                warnings.warn(
                    f"pipeline: revision turn {n_revision_turns} "
                    f"unavailable: {e!s}"
                )
                pipeline_quality_flags.append(
                    f"revision_turn_{n_revision_turns}_unavailable"
                )
                break

            revision_cost_total += float(rev_cost or 0.0)
            revision_model_last = rev_model_used
            transcript.append({
                "turn": f"4.{n_revision_turns}", "role": "revision",
                "stage": f"revision_turn_{n_revision_turns}",
                "model": rev_model_used, "cost_usd": rev_cost,
                "text": rev_text,
            })

            # Detect abandon JSON
            maybe_abandon = _parse_json(rev_text)
            if (
                isinstance(maybe_abandon, dict)
                and bool(maybe_abandon.get("abandon"))
            ):
                abandoned = True
                abandon_reason = str(
                    maybe_abandon.get("reason") or "abandoned_by_reviewer"
                )
                revision_text = rev_text
                break

            revision_text = rev_text
            current_draft = rev_text

            # Re-grade with the dual critics. If both fail, we still
            # exit the loop with the current draft + current verdict.
            try:
                new_verdict, new_critic_turns, new_critic_cost = (
                    _run_dual_critics(
                        chat_fn=_chat,
                        risk_chain=risk_chain,
                        method_chain=method_chain,
                        initial_draft=rev_text,
                    )
                )
            except ReportPipelineUnavailableError as e:
                warnings.warn(
                    f"pipeline: critic re-evaluation turn "
                    f"{n_revision_turns} unavailable: {e!s}"
                )
                break

            stage_costs["critic"] = float(
                stage_costs.get("critic", 0.0) + float(new_critic_cost or 0.0)
            )
            for i, t in enumerate(new_critic_turns):
                t_entry = dict(t)
                t_entry["turn"] = f"4.{n_revision_turns}-critic-{'ab'[min(i, 1)]}"
                transcript.append(t_entry)

            new_severity: AdversarialSeverity = _normalize_severity(
                new_verdict.get("severity")
            )

            transcript.append({
                "turn": f"4.{n_revision_turns}-merge",
                "role": "critic_merge",
                "stage": "critic_merge",
                "severity": new_severity,
                "n_must_address": len(new_verdict.get("must_address") or []),
                "n_should_address": len(
                    new_verdict.get("should_address") or []
                ),
            })

            # GREEN → done.
            if new_severity == "green":
                severity = "green"
                current_verdict = new_verdict
                break

            # No improvement detected → stop wasting compute.
            if not _improvement_detected(current_verdict, new_verdict):
                pipeline_quality_flags.append(
                    f"revision_loop_diminishing_returns_at_turn_{n_revision_turns}"
                )
                current_verdict = new_verdict
                severity = new_severity
                break

            current_verdict = new_verdict
            severity = new_severity

        else:
            # Loop exhausted without breaking — max turns hit.
            pipeline_quality_flags.append("max_revisions_exhausted")

    stage_costs["revisions"] = revision_cost_total

    # If abandoned: fall back to the audit body
    final_body_md = revision_text
    if abandoned:
        final_body_md = (
            "## TL;DR\n"
            f"Pipeline abandoned: {abandon_reason}\n\n"
            "## Thesis & Mechanism\n"
            "After the dual adversarial critic graded this thesis RED, "
            "the researcher could not honestly defend it. No actionable "
            "setup is being recommended.\n\n"
            "## Original draft (for audit)\n\n"
            + initial_draft_text.strip()
        )

    # ------------------------------------------------------------------
    # Stage 5 — Copy-edit polish (soft-fails to identity)
    # ------------------------------------------------------------------
    polished_body, polish_cost = polish_prose(
        final_body_md, model=polish_model,
    )
    stage_costs["polish"] = float(polish_cost or 0.0)
    if polished_body != final_body_md:
        transcript.append({
            "turn": 5, "role": "polish", "stage": "copy_edit_polish",
            "model": polish_model,
            "cost_usd": stage_costs["polish"],
            "polished": True,
        })
        final_body_md = polished_body
    else:
        transcript.append({
            "turn": 5, "role": "polish", "stage": "copy_edit_polish",
            "model": polish_model, "cost_usd": 0.0,
            "polished": False,
            "note": "polish unavailable or non-destructive; keeping original",
        })
        if polish_cost == 0.0:
            pipeline_quality_flags.append("polish_unavailable")

    # ------------------------------------------------------------------
    # Stage 6 — Grade (soft-fails to zero grade)
    # ------------------------------------------------------------------
    grade = grade_report(
        final_body_md, dossier, comparables, model=grade_model,
    )
    stage_costs["grade"] = float(grade.cost_usd or 0.0)
    if "grade_unavailable" in grade.quality_flags:
        pipeline_quality_flags.append("grade_unavailable")
    transcript.append({
        "turn": 6, "role": "grader", "stage": "grade_report",
        "model": grade.model_used,
        "cost_usd": stage_costs["grade"],
        "overall_score": grade.overall_score,
        "scores": {
            "clarity": grade.clarity_score,
            "evidence_depth": grade.evidence_depth_score,
            "novelty_vs_consensus": grade.novelty_vs_consensus_score,
            "falsifiability": grade.falsifiability_score,
            "sizing_rigor": grade.sizing_rigor_score,
        },
        "rationale": grade.grader_rationale,
    })

    # ------------------------------------------------------------------
    # Build ResearchReport
    # ------------------------------------------------------------------
    title = _extract_first_heading_or_sentence(final_body_md) or (
        (hypothesis.get("title") or "").strip()[:120]
    ) or (specialist_id + " research note")
    abstract = _extract_abstract(final_body_md)
    cited_claims, cited_tcs = _extract_citation_ids(final_body_md)
    for cid in (hypothesis.get("claim_ids") or [])[:60]:
        if cid and cid not in cited_claims:
            cited_claims.append(cid)
    for ev in (tool_evidence or [])[:60]:
        tcid = ev.get("tool_call_log_id")
        if tcid and tcid not in cited_tcs:
            cited_tcs.append(tcid)
    # Pull dossier claim_ids into the citation set so the brief can link them.
    for n in dossier.semantic_neighbors[:30]:
        cid = n.get("id") or n.get("object_id")
        if cid and cid not in cited_claims and str(cid).startswith("clm"):
            cited_claims.append(str(cid))

    # Derive instrument
    instrument = ""
    ents = hypothesis.get("entity_ids") or []
    if ents:
        instrument = str(ents[0])
    if not instrument and primary_artifact:
        instrument = str(primary_artifact.get("instrument") or "")
    if not instrument:
        instrument = "DESK"

    # Contradicting evidence from the merged critic verdict
    contradicting: list[dict[str, Any]] = []
    for atk in (current_verdict.get("attacks") or []):
        if not isinstance(atk, dict):
            continue
        contradicting.append({
            "source": atk.get("source") or "risk",
            "axis": atk.get("axis"),
            "quote": str(atk.get("quote") or "")[:300],
            "weakness": str(atk.get("weakness") or "")[:400],
            "recommended_fix": str(atk.get("recommended_fix") or "")[:400],
        })

    # Confidence derivation
    if confidence_hint is not None:
        conf = float(confidence_hint)
    elif hypothesis.get("posterior_prob") is not None:
        conf = float(hypothesis["posterior_prob"])
    else:
        conf = 0.5
    conf = max(0.0, min(1.0, conf))
    if severity == "red":
        conf = min(conf, 0.45)
    elif severity == "yellow":
        conf = min(conf, 0.7)

    # Quality flags + report_kind on abandon
    quality_flags: list[str] = list(pipeline_quality_flags)
    final_report_kind: ReportKind = report_kind
    if abandoned:
        quality_flags.append("pipeline_abandoned")
        final_report_kind = "blocked_thesis"
    if severity == "red" and not abandoned:
        quality_flags.append("critic_severity_red")
    if severity == "yellow":
        quality_flags.append("critic_severity_yellow")
    if "below_grade_threshold" in grade.quality_flags:
        quality_flags.append("below_grade_threshold")
    # De-dup, preserve order.
    seen_q: set[str] = set()
    quality_flags_dedup: list[str] = []
    for f in quality_flags:
        if f not in seen_q:
            seen_q.add(f)
            quality_flags_dedup.append(f)

    primary_artifact_id: Optional[str] = None
    if primary_artifact and isinstance(primary_artifact, dict):
        primary_artifact_id = (
            primary_artifact.get("id")
            or primary_artifact.get("idea_id")
            or primary_artifact.get("setup_id")
        )

    now_dt = _utc_now()
    total_cost = float(sum(stage_costs.values()))

    # Reviewer turns get the full transcript (already accumulated).
    # Critic_text + revision_text kept for back-compat with downstream.
    critic_text_repr = json.dumps({
        "merged": current_verdict,
        "severity": severity,
    }, default=str)

    report = ResearchReport(
        id=new_report_id(),
        specialist_id=specialist_id,
        cycle_id=cycle_id,
        hypothesis_id=str(hypothesis.get("id") or ""),
        instrument=instrument,
        report_kind=final_report_kind,
        title=title,
        abstract=abstract,
        body_md=final_body_md,
        edge_thesis=(hypothesis.get("hypothesis_text") or "")[:1200],
        contradicting_evidence=contradicting,
        citation_claim_ids=cited_claims[:120],
        citation_tool_call_ids=cited_tcs[:120],
        primary_artifact_id=primary_artifact_id,
        confidence=conf,
        novelty_score=novelty_score_hint,
        quality_flags=quality_flags_dedup,
        reviewer_turns=transcript,
        adversarial_severity=severity,
        revised_at=now_dt,
        cost_usd=total_cost,
        valid_from=now_dt,
        transaction_from=now_dt,
        payload={
            "pipeline_version": "report_pipeline_v2_6stage",
            "stage_models": {
                "researcher": researcher_model_used,
                "comparables": comparables.model_used,
                "revision_last": revision_model_last,
                "polish": polish_model,
                "grade": grade.model_used,
            },
            "stage_costs_usd": stage_costs,
            "n_revision_turns": n_revision_turns,
            "abandon_reason": abandon_reason,
            "critic_must_address": current_verdict.get("must_address") or [],
            "critic_should_address": current_verdict.get("should_address") or [],
            "critic_rationale": current_verdict.get("rationale") or "",
            "critic_severities": {
                "risk": current_verdict.get("_risk_severity") or severity,
                "methodology": current_verdict.get("_method_severity") or severity,
                "merged": severity,
            },
            "dossier": dossier.to_dict(),
            "comparables": comparables.to_dict(),
            "grade": grade.to_dict(),
        },
    )

    return ResearchPipelineResult(
        report=report,
        initial_draft_text=initial_draft_text,
        critic_text=critic_text_repr,
        revision_text=revision_text,
        transcript=transcript,
        total_cost_usd=total_cost,
        adversarial_severity=severity,
        abandoned=abandoned,
        dossier=dossier,
        comparables=comparables,
        grade=grade,
        n_revision_turns=n_revision_turns,
        stage_costs_usd=stage_costs,
    )


__all__ = [
    "run_report_pipeline",
    "ResearchPipelineResult",
    "ReportPipelineUnavailableError",
]

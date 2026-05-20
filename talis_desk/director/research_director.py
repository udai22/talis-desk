"""Research Director — daily curriculum allocator.

Per `wiki/SOTA_DESK_ARCHITECTURE.md` v2 §6 Week 6 (lines 521-523) and
Layer 5 (lines 138-168).

The Research Director is itself a specialist that runs the canonical loop,
but instead of emitting trade ideas it emits CURRICULUM ASSIGNMENTS to the
other specialists. Concretely:

  1. Read `mv_weakness_map` -> top 5 weaknesses across the desk.
  2. Read `mv_specialist_brier_rolling` -> per-specialist track record.
  3. Read `mv_top_tools_per_specialist_30d` -> coverage gaps.
  4. Read `mv_hot_hypotheses` -> under-explored themes.
  5. Call Sonnet (real LLM via `tic.desk.models.chat()` with full fallback
     chain) to propose:
       - per-specialist focus areas for the next 7 days
       - per-specialist budget cost-allocated by Brier track record
       - specific investigation seeds to assign
  6. Write assignments as `agent_messages` rows with
     `kind='curriculum_assignment'` to each specialist's inbox.
  7. Write the `CurriculumPlan` to `specialist_states` with
     `state_kind='persona'` and `specialist_id='research_director'` — the
     Director is its own specialist.

# Honest gaps
- `expected_brier_lift` is a heuristic anchored at 0.03 per assigned
  investigation; needs more cycles of data to calibrate.
- The director's persona model is `anthropic:claude-sonnet-4-6` with full
  fallback chain. Future: A/B test against other heavy-reasoning models
  (gpt-5.5, deepseek-v4-pro).
- `evaluate_curriculum_lift` uses a 7-day forward Brier window; if the
  specialist's Brier sample is sparse, lift is reported as inconclusive
  rather than "no_lift" — see `CurriculumOutcome.inconclusive`.
- LLM responses are gated by a multi-provider fallback chain
  (`_DIRECTOR_FALLBACK_CHAIN`). If every provider in the chain returns
  empty / errors / unparseable JSON, `propose_curriculum` raises
  `DirectorPlanUnavailableError`. The cycle continues without a
  curriculum; the loop runner has its own per-specialist scheduling
  and does not depend on director guidance. We NEVER fabricate a
  curriculum from heuristics — that would corrupt the dashboard's
  audit trail with non-LLM signals.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from ..store import get_desk_store


# ============================================================================
# Director constants
# ============================================================================

DIRECTOR_SPECIALIST_ID = "research_director"
DIRECTOR_PERSONA_VERSION = "v1"
DIRECTOR_PERSONA_MODEL = "anthropic:claude-sonnet-4-6"
DIRECTOR_FALLBACK_MODEL = "openai:gpt-5.5"

#: Full multi-provider fallback chain for director curriculum proposals.
#: Mirrors the judge / brief / mutator pattern. NEVER stub — if every
#: provider fails, the caller raises DirectorPlanUnavailableError and
#: the cycle proceeds with NO curriculum (loop runner has its own
#: per-specialist scheduling and can run without director guidance).
_DIRECTOR_FALLBACK_CHAIN = [
    "anthropic:claude-sonnet-4-6",
    "anthropic:claude-opus-4-7",
    "openai:gpt-5.5",
    "deepseek:v4-pro",
    "xai:grok-4",
    "moonshot:v1-32k",
    "perplexity:sonar-pro",
]


class DirectorPlanUnavailableError(RuntimeError):
    """Raised when every provider in the director fallback chain returns
    empty / errors / unparseable JSON. NO stub curriculums — without a
    real LLM-generated plan, the loop continues without director
    guidance (specialists still run on their own schedule)."""

CURRICULUM_ASSIGNMENT_KIND = "curriculum_assignment"
VETO_KIND = "veto"

DEFAULT_CYCLE_BUDGET_USD = 5.0
DEFAULT_LOOKAHEAD_DAYS = 7

# Per-specialist floor budget so even bad-Brier specialists keep exploring.
PER_SPECIALIST_MIN_BUDGET_USD = 0.25


# ============================================================================
# Data classes (the public contract)
# ============================================================================


@dataclass
class InvestigationAssignment:
    """A single seed handed to a specialist."""

    seed_id: str
    title: str
    rationale: str
    target_weakness_id: Optional[str] = None
    target_hypothesis_id: Optional[str] = None
    suggested_tool_uris: list[str] = field(default_factory=list)
    expected_calls: int = 30  # v2 line 22: "avg >= 30 calls per hot investigation"


@dataclass
class SpecialistAllocation:
    """Per-specialist slice of the curriculum."""

    specialist_id: str
    budget_usd: float
    focus_areas: list[str]
    expected_brier_lift: float
    assigned_investigations: list[InvestigationAssignment] = field(default_factory=list)
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "specialist_id": self.specialist_id,
            "budget_usd": float(self.budget_usd),
            "focus_areas": list(self.focus_areas),
            "expected_brier_lift": float(self.expected_brier_lift),
            "assigned_investigations": [asdict(i) for i in self.assigned_investigations],
            "rationale": self.rationale,
        }


@dataclass
class CurriculumPlan:
    """One day's curriculum allocation. Written to `specialist_states` and
    posted as `agent_messages` to each specialist's inbox."""

    as_of: datetime
    cycle_id: str
    plan_id: str
    total_budget_usd: float
    per_specialist: dict[str, SpecialistAllocation]
    cross_cutting_themes: list[str]
    explicit_weakness_assignments: dict[str, list[str]]  # {specialist_id: [weakness_id,...]}
    quality_flags: list[str] = field(default_factory=list)
    llm_model_used: Optional[str] = None
    llm_fallback_used: bool = False
    llm_cost_usd: float = 0.0
    director_rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of": _iso(self.as_of),
            "cycle_id": self.cycle_id,
            "plan_id": self.plan_id,
            "total_budget_usd": float(self.total_budget_usd),
            "per_specialist": {
                k: v.to_dict() for k, v in self.per_specialist.items()
            },
            "cross_cutting_themes": list(self.cross_cutting_themes),
            "explicit_weakness_assignments": {
                k: list(v) for k, v in self.explicit_weakness_assignments.items()
            },
            "quality_flags": list(self.quality_flags),
            "llm_model_used": self.llm_model_used,
            "llm_fallback_used": self.llm_fallback_used,
            "llm_cost_usd": float(self.llm_cost_usd),
            "director_rationale": self.director_rationale,
        }


@dataclass
class CurriculumOutcome:
    """Result of `evaluate_curriculum_lift`."""

    plan_id: str
    evaluated_at: datetime
    per_specialist_brier_delta: dict[str, Optional[float]]
    classification: str  # 'effective' | 'no_lift' | 'inconclusive'
    inconclusive: bool
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "evaluated_at": _iso(self.evaluated_at),
            "per_specialist_brier_delta": {
                k: (None if v is None else float(v))
                for k, v in self.per_specialist_brier_delta.items()
            },
            "classification": self.classification,
            "inconclusive": self.inconclusive,
            "notes": list(self.notes),
        }


# ============================================================================
# Small helpers
# ============================================================================

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _from_iso(s: Optional[str]) -> Optional[datetime]:
    if s is None:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _conn() -> sqlite3.Connection:
    return get_desk_store().conn


# ============================================================================
# Data gathering — reads from MVs (which on SQLite are plain VIEWs)
# ============================================================================

def _read_weakness_map(conn: sqlite3.Connection, limit: int = 5) -> list[dict[str, Any]]:
    """Top-N weaknesses from `mv_weakness_map`. SQLite view exposes one row
    per specialist with the aggregate brier/delta in `evidence_json`. We
    rank by the WORST (highest) avg Brier — higher = more wrong.

    Returns rows shaped:
      {specialist_id, weakness_kind, brier, delta, weakness_id}
    where `weakness_id` is a deterministic id built from (specialist_id,
    weakness_kind, as_of_day) so the dashboard + lift evaluator can refer
    to a concrete row.
    """
    try:
        rows = conn.execute(
            "SELECT specialist_id, weakness_kind, evidence_json FROM mv_weakness_map"
        ).fetchall()
    except sqlite3.OperationalError:
        return []

    parsed: list[dict[str, Any]] = []
    today = _utc_now().date().isoformat()
    for r in rows:
        try:
            d = dict(r)
        except Exception:
            d = {k: r[k] for k in r.keys()}
        ev = d.get("evidence_json") or "{}"
        try:
            ev_dict = json.loads(ev) if isinstance(ev, str) else ev
        except Exception:
            ev_dict = {}
        sid = d.get("specialist_id") or "unknown"
        brier = ev_dict.get("brier")
        delta = ev_dict.get("delta")
        weakness_id = f"weak_{sid}_{today}_{(d.get('weakness_kind') or 'agg')}"
        parsed.append({
            "specialist_id": sid,
            "weakness_kind": d.get("weakness_kind") or "brier_or_alpha_or_cost",
            "brier": float(brier) if brier is not None else None,
            "delta": float(delta) if delta is not None else None,
            "weakness_id": weakness_id,
        })

    # Rank: highest brier first (worst calibration). Specialists with no
    # brier yet are placed last so they don't crowd out evidence-backed
    # weaknesses; their `brier` is treated as 0.5 (totally uninformed) for
    # ranking only.
    def _rank_key(row: dict[str, Any]) -> tuple[float, str]:
        b = row.get("brier")
        rank_brier = -float(b) if b is not None else -0.5
        return (rank_brier, row.get("specialist_id") or "")

    parsed.sort(key=_rank_key)
    return parsed[:limit]


def _read_specialist_brier_rolling(
    conn: sqlite3.Connection,
    lookback_days: int = 30,
) -> dict[str, dict[str, Any]]:
    """Average Brier per specialist over the last `lookback_days`.

    Returns: {specialist_id: {brier_avg, n_days, last_day}}.
    """
    cutoff = (_utc_now() - timedelta(days=lookback_days)).date().isoformat()
    out: dict[str, dict[str, Any]] = {}
    try:
        rows = conn.execute(
            "SELECT specialist_id, day, brier_avg, n "
            "FROM mv_specialist_brier_rolling "
            "WHERE day >= ? "
            "ORDER BY specialist_id, day",
            (cutoff,),
        ).fetchall()
    except sqlite3.OperationalError:
        return out

    for r in rows:
        try:
            d = dict(r)
        except Exception:
            d = {k: r[k] for k in r.keys()}
        sid = d.get("specialist_id") or "unknown"
        slot = out.setdefault(sid, {"briers": [], "ns": [], "last_day": None})
        b = d.get("brier_avg")
        if b is not None:
            slot["briers"].append(float(b))
        n = d.get("n")
        if n is not None:
            slot["ns"].append(int(n))
        day = d.get("day")
        if day and (slot["last_day"] is None or str(day) > str(slot["last_day"])):
            slot["last_day"] = day

    # Compress: mean Brier over collected days.
    compressed: dict[str, dict[str, Any]] = {}
    for sid, slot in out.items():
        briers = slot["briers"]
        avg = sum(briers) / len(briers) if briers else None
        compressed[sid] = {
            "brier_avg": avg,
            "n_days": len(briers),
            "n_resolved": sum(slot["ns"]),
            "last_day": slot["last_day"],
        }
    return compressed


def _read_top_tools_per_specialist(
    conn: sqlite3.Connection,
    top_k: int = 5,
) -> dict[str, list[dict[str, Any]]]:
    """For each specialist, the top tools by call count + Brier delta from
    `mv_top_tools_per_specialist_30d`."""
    out: dict[str, list[dict[str, Any]]] = {}
    try:
        rows = conn.execute(
            "SELECT specialist_id, tool_uri, n_calls, n_success, total_cost_usd, "
            "brier_delta_avg FROM mv_top_tools_per_specialist_30d"
        ).fetchall()
    except sqlite3.OperationalError:
        return out

    for r in rows:
        try:
            d = dict(r)
        except Exception:
            d = {k: r[k] for k in r.keys()}
        sid = d.get("specialist_id") or "unknown"
        out.setdefault(sid, []).append({
            "tool_uri": d.get("tool_uri"),
            "n_calls": int(d.get("n_calls") or 0),
            "n_success": int(d.get("n_success") or 0),
            "total_cost_usd": float(d.get("total_cost_usd") or 0.0),
            "brier_delta_avg": (
                float(d["brier_delta_avg"]) if d.get("brier_delta_avg") is not None else None
            ),
        })

    for sid, lst in out.items():
        lst.sort(key=lambda x: x["n_calls"], reverse=True)
        out[sid] = lst[:top_k]
    return out


def _read_hot_hypotheses(
    conn: sqlite3.Connection,
    top_k: int = 10,
) -> list[dict[str, Any]]:
    """Top hot hypotheses across the desk by heat_score."""
    try:
        rows = conn.execute(
            "SELECT id, specialist_id, title, posterior_prob, heat_score, "
            "evidence_edges, last_updated "
            "FROM mv_hot_hypotheses "
            "ORDER BY heat_score DESC, posterior_prob DESC LIMIT ?",
            (top_k,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    out: list[dict[str, Any]] = []
    for r in rows:
        try:
            d = dict(r)
        except Exception:
            d = {k: r[k] for k in r.keys()}
        out.append({
            "id": d.get("id"),
            "specialist_id": d.get("specialist_id"),
            "title": d.get("title"),
            "posterior_prob": (
                float(d["posterior_prob"]) if d.get("posterior_prob") is not None else None
            ),
            "heat_score": float(d.get("heat_score") or 0.0),
            "evidence_edges": int(d.get("evidence_edges") or 0),
            "last_updated": d.get("last_updated"),
        })
    return out


def _enumerate_active_specialists(
    conn: sqlite3.Connection,
    weakness_map: list[dict[str, Any]],
    brier_rolling: dict[str, dict[str, Any]],
) -> list[str]:
    """Union of specialists that show up anywhere in the data sources, minus
    the Research Director itself. Falls back to whatever is in
    `specialist_states` if nothing else is available."""
    candidates: set[str] = set()
    for w in weakness_map:
        sid = w.get("specialist_id")
        if sid and sid != DIRECTOR_SPECIALIST_ID:
            candidates.add(sid)
    for sid in brier_rolling:
        if sid and sid != DIRECTOR_SPECIALIST_ID:
            candidates.add(sid)
    if not candidates:
        try:
            rows = conn.execute(
                "SELECT DISTINCT specialist_id FROM specialist_states "
                "WHERE transaction_to IS NULL"
            ).fetchall()
            for r in rows:
                sid = r["specialist_id"]
                if sid and sid != DIRECTOR_SPECIALIST_ID:
                    candidates.add(sid)
        except sqlite3.OperationalError:
            pass
    return sorted(candidates)


# ============================================================================
# Budget allocator — cost-allocated by Brier track record
# ============================================================================

def _allocate_budgets(
    specialists: list[str],
    brier_rolling: dict[str, dict[str, Any]],
    total_budget_usd: float,
) -> dict[str, float]:
    """Give better-calibrated specialists a larger slice. Brier is "lower is
    better" so we invert: weight_i = 1 - clamp(brier_i, 0, 1). Specialists
    without a Brier sample get the median weight so they're not starved.

    The minimum per-specialist budget is `PER_SPECIALIST_MIN_BUDGET_USD` so
    even the worst-Brier specialist keeps exploring.
    """
    if not specialists:
        return {}

    weights: dict[str, float] = {}
    raw_briers: list[float] = []
    for sid in specialists:
        b = (brier_rolling.get(sid) or {}).get("brier_avg")
        if b is None:
            weights[sid] = None  # type: ignore[assignment]
        else:
            # Clamp to [0,1]
            bclip = max(0.0, min(1.0, float(b)))
            weights[sid] = 1.0 - bclip
            raw_briers.append(bclip)

    # Median weight for the unknowns
    if raw_briers:
        median_brier = sorted(raw_briers)[len(raw_briers) // 2]
        median_weight = 1.0 - median_brier
    else:
        median_weight = 0.5

    for sid, w in list(weights.items()):
        if w is None:
            weights[sid] = median_weight  # type: ignore[assignment]

    # Floor budgets first
    floors_total = PER_SPECIALIST_MIN_BUDGET_USD * len(specialists)
    if total_budget_usd <= floors_total:
        # Pathological: total budget can't cover floors. Distribute pro-rata.
        per = total_budget_usd / len(specialists)
        return {sid: per for sid in specialists}

    remaining = total_budget_usd - floors_total
    weight_sum = sum(weights.values()) or 1.0
    out: dict[str, float] = {}
    for sid in specialists:
        w = weights[sid]
        share = remaining * (w / weight_sum)
        out[sid] = round(PER_SPECIALIST_MIN_BUDGET_USD + share, 6)
    return out


# ============================================================================
# LLM planning prompt
# ============================================================================

DIRECTOR_SYSTEM_PROMPT = (
    "You are the Research Director for a quantitative Hyperliquid trading "
    "desk. Each day you allocate a curriculum across specialist agents: "
    "what each one should investigate over the next 7 days, given measured "
    "weakness in their forecasts (Brier), their tool coverage, and the desk's "
    "hot hypotheses. Your output is operational — short, specific, "
    "falsifiable. Do NOT propose trade ideas; only research assignments.\n\n"
    "Output STRICTLY one JSON object matching this shape (no prose outside):\n"
    "{\n"
    '  "cross_cutting_themes": ["<2-4 short phrases that span specialists>"],\n'
    '  "per_specialist": {\n'
    '    "<specialist_id>": {\n'
    '      "focus_areas": ["<2-4 short phrases>"],\n'
    '      "rationale": "<<=120 words tying focus to measured weakness>",\n'
    '      "expected_brier_lift": 0.0..0.1,\n'
    '      "assigned_investigations": [\n'
    '         {"title": "<short>", "rationale": "<<=60 words>",\n'
    '          "target_weakness_id": "<weakness_id or null>",\n'
    '          "target_hypothesis_id": "<hyp_id or null>",\n'
    '          "suggested_tool_uris": ["tic://tool/..."],\n'
    '          "expected_calls": 20..120}\n'
    '      ]\n'
    "    }\n"
    "  },\n"
    '  "director_rationale": "<<=150 words on the overall allocation>"\n'
    "}\n\n"
    "Hard rules:\n"
    "  - Each specialist gets at least 1 investigation.\n"
    "  - Each investigation cites a target weakness_id OR a target hypothesis_id "
    "(both null only if neither is provided in the inputs).\n"
    "  - expected_brier_lift is a small honest number (0-0.1).\n"
    "  - Tool URIs come from the specialist's existing top-tools list "
    "where possible; you may suggest a NEW tool URI iff it directly addresses "
    "the cited weakness.\n"
)


def _build_director_user_prompt(
    as_of: datetime,
    weakness_map: list[dict[str, Any]],
    brier_rolling: dict[str, dict[str, Any]],
    top_tools: dict[str, list[dict[str, Any]]],
    hot_hypotheses: list[dict[str, Any]],
    specialists: list[str],
    budgets: dict[str, float],
) -> str:
    lines: list[str] = []
    lines.append(f"## Curriculum cycle as_of {_iso(as_of)}")
    lines.append("")
    lines.append("### Specialists in scope")
    for sid in specialists:
        b = (brier_rolling.get(sid) or {}).get("brier_avg")
        n = (brier_rolling.get(sid) or {}).get("n_days") or 0
        budget = budgets.get(sid, 0.0)
        b_str = f"{b:.3f}" if b is not None else "n/a"
        lines.append(f"  - {sid}: brier_30d={b_str} (n_days={n}), budget_usd={budget:.3f}")
    lines.append("")
    lines.append("### Top 5 weaknesses (from mv_weakness_map)")
    if weakness_map:
        for w in weakness_map:
            b = w.get("brier")
            b_str = f"{b:.3f}" if b is not None else "n/a"
            lines.append(
                f"  - id={w['weakness_id']} owner={w['specialist_id']} "
                f"kind={w['weakness_kind']} brier_avg={b_str}"
            )
    else:
        lines.append("  (none reported)")
    lines.append("")
    lines.append("### Top tools per specialist (last 30d)")
    if top_tools:
        for sid, tools in top_tools.items():
            lines.append(f"  {sid}:")
            for t in tools:
                bd = t.get("brier_delta_avg")
                bd_str = f"{bd:+.3f}" if bd is not None else "n/a"
                lines.append(
                    f"    - {t['tool_uri']} n_calls={t['n_calls']} "
                    f"brier_delta_avg={bd_str} cost_usd={t['total_cost_usd']:.3f}"
                )
    else:
        lines.append("  (no tool calls logged yet — coverage gap)")
    lines.append("")
    lines.append("### Hot hypotheses (under-explored)")
    if hot_hypotheses:
        for h in hot_hypotheses[:8]:
            pp = h.get("posterior_prob")
            pp_str = f"{pp:.2f}" if pp is not None else "n/a"
            lines.append(
                f"  - id={h['id']} owner={h.get('specialist_id')} "
                f"heat={h['heat_score']:.2f} posterior={pp_str} "
                f"evidence_edges={h['evidence_edges']} title={h.get('title')!r}"
            )
    else:
        lines.append("  (none active)")
    lines.append("")
    lines.append("### Instructions")
    lines.append(
        "Produce the curriculum JSON. For each specialist, draw focus areas "
        "from their measured weaknesses + coverage gaps. Cite weakness_id / "
        "hypothesis_id whenever possible. Keep all text short."
    )
    return "\n".join(lines)


# ============================================================================
# LLM call — talis_tic.desk.models.chat with real fallback
# ============================================================================

async def _call_director_llm_async(
    system: str,
    user: str,
    model: str = DIRECTOR_PERSONA_MODEL,
    fallback: str = DIRECTOR_FALLBACK_MODEL,
) -> dict[str, Any]:
    """Walk the full multi-provider fallback chain for the director call.

    NO STUBS: if every provider returns empty / errors / unparseable JSON,
    raise DirectorPlanUnavailableError. The caller (propose_curriculum)
    propagates upward; the cycle continues without a curriculum rather
    than fabricating one.

    Returns dict shape:
      {"text": str, "model_used": str, "fallback_used": bool,
       "error": Optional[str], "parsed": dict, "chain_position": int}
    """
    # Codex finding #16: centralized path resolution.
    from .._tic_config import ensure_tic_on_path
    ensure_tic_on_path()
    from tic.desk.models import chat as _chat  # type: ignore

    # Build chain: caller-supplied primary + secondary, then canonical chain.
    seen: set[str] = set()
    chain: list[str] = []
    for m in (model, fallback):
        if m and m not in seen:
            chain.append(m); seen.add(m)
    for m in _DIRECTOR_FALLBACK_CHAIN:
        if m not in seen:
            chain.append(m); seen.add(m)

    last_error: Optional[str] = None
    for i, m in enumerate(chain):
        try:
            # Disable chat()'s built-in fallback — WE walk the chain.
            res = await _chat(m, system, user, max_tokens=3000, fallback=None)
        except Exception as e:  # noqa: BLE001
            last_error = f"{m}: chat_call_failed: {e}"
            continue
        text = (res.get("text") or "").strip()
        err = res.get("error")
        if err:
            last_error = f"{m}: {err}"
            continue
        if not text:
            last_error = f"{m}: empty_completion"
            continue
        parsed = _parse_director_json(text)
        if not parsed:
            last_error = f"{m}: unparseable_director_json"
            continue
        return {
            "text": text,
            "model_used": res.get("model_used", m),
            "provider": res.get("provider", m.split(":", 1)[0]),
            "fallback_used": (i > 0),
            "error": None,
            "parsed": parsed,
            "chain_position": i,
        }

    raise DirectorPlanUnavailableError(
        f"All {len(chain)} providers in the director fallback chain failed "
        f"to return a parseable curriculum. Last error: {last_error}. "
        f"Chain: {chain}"
    )


def _call_director_llm(
    system: str,
    user: str,
    model: str = DIRECTOR_PERSONA_MODEL,
    fallback: str = DIRECTOR_FALLBACK_MODEL,
) -> dict[str, Any]:
    """Sync wrapper around the async call. Handles being inside an event loop."""
    try:
        asyncio.get_running_loop()
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(
                asyncio.run,
                _call_director_llm_async(system, user, model, fallback),
            )
            return fut.result(timeout=180)
    except RuntimeError:
        return asyncio.run(_call_director_llm_async(system, user, model, fallback))


def _parse_director_json(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        return {}
    # Direct
    try:
        return json.loads(text)
    except Exception:
        pass
    # Fenced
    if "```" in text:
        for chunk in text.split("```"):
            c = chunk.strip()
            if c.startswith("json"):
                c = c[4:].strip()
            try:
                return json.loads(c)
            except Exception:
                continue
    # First brace
    start = text.find("{")
    end = text.rfind("}")
    if 0 <= start < end:
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            pass
    return {}


# ============================================================================
# Removed `_deterministic_allocations`.
# The no-stubs rule disallows fabricated curriculums. When every provider
# in the director fallback chain fails, `_call_director_llm_async` raises
# `DirectorPlanUnavailableError`. The caller propagates the error; the
# loop continues without director guidance (the loop runner has its own
# per-specialist scheduling and works without a curriculum).
# ============================================================================


# ============================================================================
# Persistence — agent_messages + specialist_states
# ============================================================================

def _post_curriculum_messages(
    plan: CurriculumPlan,
) -> list[dict[str, Any]]:
    """Write one `agent_messages` row per specialist with
    `kind='curriculum_assignment'`. Returns list of {message_id,
    specialist_id, n_investigations}.

    NOTE: We bypass `agents_native.scratchpad.post_message` because that
    helper enforces a fixed `VALID_MESSAGE_KINDS` set. The
    `agent_messages.message_kind` column itself is unconstrained at the
    DB layer, so a direct INSERT with the new kind is legal.
    """
    conn = _conn()
    now = _utc_now()
    out: list[dict[str, Any]] = []
    for sid, alloc in plan.per_specialist.items():
        msg_id = f"msg_{uuid.uuid4().hex[:12]}"
        payload = {
            "plan_id": plan.plan_id,
            "cycle_id": plan.cycle_id,
            "as_of": _iso(plan.as_of),
            "budget_usd": alloc.budget_usd,
            "focus_areas": list(alloc.focus_areas),
            "expected_brier_lift": alloc.expected_brier_lift,
            "rationale": alloc.rationale,
            "assigned_investigations": [
                asdict(inv) for inv in alloc.assigned_investigations
            ],
            "explicit_weakness_ids": list(
                plan.explicit_weakness_assignments.get(sid, [])
            ),
            "cross_cutting_themes": list(plan.cross_cutting_themes),
            "quality_flags": list(plan.quality_flags),
        }
        dedupe_key = f"curriculum:{plan.plan_id}:{sid}"
        # Skip if a dedupe-matched row already exists (re-runs are idempotent).
        existing = conn.execute(
            "SELECT id FROM agent_messages WHERE from_agent = ? "
            "AND dedupe_key = ? AND transaction_to IS NULL LIMIT 1",
            (DIRECTOR_SPECIALIST_ID, dedupe_key),
        ).fetchone()
        if existing is not None:
            out.append({
                "message_id": existing["id"],
                "specialist_id": sid,
                "n_investigations": len(alloc.assigned_investigations),
                "deduped": True,
            })
            continue
        expires_at = now + timedelta(days=DEFAULT_LOOKAHEAD_DAYS)
        conn.execute(
            "INSERT INTO agent_messages "
            "(id, from_agent, to_agent_or_topic, message_kind, payload, "
            " posted_at, read_by, expires_at, dedupe_key, valid_from, "
            " transaction_from) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                msg_id,
                DIRECTOR_SPECIALIST_ID,
                sid,
                CURRICULUM_ASSIGNMENT_KIND,
                json.dumps(payload),
                _iso(now),
                "[]",
                _iso(expires_at),
                dedupe_key,
                _iso(now),
                _iso(now),
            ),
        )
        out.append({
            "message_id": msg_id,
            "specialist_id": sid,
            "n_investigations": len(alloc.assigned_investigations),
            "deduped": False,
        })
    conn.commit()
    return out


def assign_hot_investigations(plan: CurriculumPlan) -> list[dict[str, Any]]:
    """Public API: write `agent_messages` rows for every SpecialistAllocation
    in `plan`. Returns the list of {message_id, specialist_id,
    n_investigations} dicts that were posted.

    Idempotent via `dedupe_key=f"curriculum:{plan_id}:{specialist_id}"`.
    """
    return _post_curriculum_messages(plan)


def _persist_director_persona_state(plan: CurriculumPlan) -> str:
    """Write a `specialist_states` row tagged
    `specialist_id='research_director'`, `state_kind='persona'`. The
    full plan JSON lives in `state_json` so dashboards + the lift
    evaluator can hydrate it later.

    Returns the inserted row id.
    """
    conn = _conn()
    now = _utc_now()
    spst_id = f"spst_{uuid.uuid4().hex[:12]}"
    state_obj = {
        "persona_model": DIRECTOR_PERSONA_MODEL,
        "curriculum_plan": plan.to_dict(),
    }
    conn.execute(
        "INSERT INTO specialist_states "
        "(id, specialist_id, persona_version, cycle_id, state_kind, "
        " state_json, valid_from, transaction_from) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            spst_id,
            DIRECTOR_SPECIALIST_ID,
            DIRECTOR_PERSONA_VERSION,
            plan.cycle_id,
            "persona",
            json.dumps(state_obj),
            _iso(now),
            _iso(now),
        ),
    )
    conn.commit()
    return spst_id


# ============================================================================
# Main entry point
# ============================================================================

def run_research_director_cycle(
    as_of: Optional[datetime] = None,
    budget_usd: float = DEFAULT_CYCLE_BUDGET_USD,
    cycle_id: Optional[str] = None,
) -> CurriculumPlan:
    """Daily curriculum allocator.

    Steps:
      1. Read mv_weakness_map -> top 5 weaknesses across the desk.
      2. Read mv_specialist_brier_rolling for each specialist.
      3. Read mv_top_tools_per_specialist_30d for coverage gaps.
      4. Read mv_hot_hypotheses -> under-explored themes.
      5. Use Sonnet (real LLM, via chat() with full fallback chain) to
         propose per-specialist focus + budget + investigations.
      6. Write assignments as agent_messages with
         kind='curriculum_assignment' to each specialist's inbox.
      7. Write the CurriculumPlan to specialist_states with
         state_kind='persona', specialist_id='research_director'.

    Returns the plan.
    """
    as_of = as_of or _utc_now()
    cycle_id = cycle_id or f"director_{as_of.strftime('%Y%m%d_%H%M%S')}"
    plan_id = f"plan_{uuid.uuid4().hex[:12]}"

    conn = _conn()
    weakness_map = _read_weakness_map(conn, limit=5)
    brier_rolling = _read_specialist_brier_rolling(conn, lookback_days=30)
    top_tools = _read_top_tools_per_specialist(conn, top_k=5)
    hot_hypotheses = _read_hot_hypotheses(conn, top_k=10)
    specialists = _enumerate_active_specialists(conn, weakness_map, brier_rolling)

    if not specialists:
        # Nothing to plan for. Still produce an empty plan so callers
        # can rely on the shape. Tag with a quality flag.
        return CurriculumPlan(
            as_of=as_of,
            cycle_id=cycle_id,
            plan_id=plan_id,
            total_budget_usd=budget_usd,
            per_specialist={},
            cross_cutting_themes=[],
            explicit_weakness_assignments={},
            quality_flags=["no_specialists_in_scope"],
            llm_model_used=None,
            director_rationale=(
                "No specialists found in mv_weakness_map, "
                "mv_specialist_brier_rolling, or specialist_states."
            ),
        )

    budgets = _allocate_budgets(specialists, brier_rolling, budget_usd)

    # ---- Build LLM prompt ------------------------------------------------
    system = DIRECTOR_SYSTEM_PROMPT
    user = _build_director_user_prompt(
        as_of=as_of,
        weakness_map=weakness_map,
        brier_rolling=brier_rolling,
        top_tools=top_tools,
        hot_hypotheses=hot_hypotheses,
        specialists=specialists,
        budgets=budgets,
    )

    # ---- Call the LLM (real chat() with full fallback chain — NO STUB) --
    # Walks every provider in _DIRECTOR_FALLBACK_CHAIN ourselves. If every
    # provider returns empty/errors/unparseable JSON, this raises
    # DirectorPlanUnavailableError — caller decides whether to fail the
    # cycle or proceed without a curriculum. We do NOT fabricate one.
    quality_flags: list[str] = []
    llm_res = _call_director_llm(system, user)
    parsed: dict[str, Any] = llm_res["parsed"]
    llm_model_used = llm_res["model_used"]
    llm_fallback_used = llm_res["fallback_used"]

    # ---- Build allocations -----------------------------------------------
    explicit_weakness_assignments: dict[str, list[str]] = {}
    for w in weakness_map:
        sid = w["specialist_id"]
        explicit_weakness_assignments.setdefault(sid, []).append(w["weakness_id"])

    per_specialist: dict[str, SpecialistAllocation] = {}
    cross_cutting_themes = _safe_list_str(parsed.get("cross_cutting_themes"))
    director_rationale = str(parsed.get("director_rationale") or "")[:1500]
    ps = parsed.get("per_specialist") or {}
    for sid in specialists:
        row = ps.get(sid) or {}
        focus = _safe_list_str(row.get("focus_areas"))
        rationale = str(row.get("rationale") or "")[:1000]
        try:
            expected_lift = float(row.get("expected_brier_lift", 0.03))
        except Exception:
            expected_lift = 0.03
        expected_lift = max(0.0, min(0.1, expected_lift))

        raw_invs = row.get("assigned_investigations") or []
        invs: list[InvestigationAssignment] = []
        for ri in raw_invs:
            if not isinstance(ri, dict):
                continue
            seed_id = f"inv_{uuid.uuid4().hex[:10]}"
            title = str(ri.get("title") or "untitled")[:120]
            inv_rationale = str(ri.get("rationale") or "")[:600]
            target_w = ri.get("target_weakness_id")
            target_h = ri.get("target_hypothesis_id")
            tool_uris = _safe_list_str(ri.get("suggested_tool_uris"))
            try:
                expected_calls = int(ri.get("expected_calls", 30))
            except Exception:
                expected_calls = 30
            expected_calls = max(5, min(300, expected_calls))
            invs.append(InvestigationAssignment(
                seed_id=seed_id,
                title=title,
                rationale=inv_rationale,
                target_weakness_id=target_w if isinstance(target_w, str) else None,
                target_hypothesis_id=target_h if isinstance(target_h, str) else None,
                suggested_tool_uris=tool_uris,
                expected_calls=expected_calls,
            ))

        if not focus:
            # LLM omitted this specialist entirely — they get NO curriculum
            # this cycle. We do not synthesize a placeholder. The loop
            # runner will still schedule the specialist via its own logic;
            # the curriculum is additive guidance.
            quality_flags.append(f"llm_missing_specialist:{sid}")
            continue

        # If the LLM gave focus_areas but no investigations, accept the
        # partial allocation — the specialist still benefits from the
        # focus_areas guidance even without seeded investigations.
        if not invs:
            quality_flags.append(f"llm_empty_invs:{sid}")

        per_specialist[sid] = SpecialistAllocation(
            specialist_id=sid,
            budget_usd=budgets.get(sid, PER_SPECIALIST_MIN_BUDGET_USD),
            focus_areas=focus,
            expected_brier_lift=expected_lift,
            assigned_investigations=invs,
            rationale=rationale,
        )

    plan = CurriculumPlan(
        as_of=as_of,
        cycle_id=cycle_id,
        plan_id=plan_id,
        total_budget_usd=budget_usd,
        per_specialist=per_specialist,
        cross_cutting_themes=cross_cutting_themes,
        explicit_weakness_assignments=explicit_weakness_assignments,
        quality_flags=quality_flags,
        llm_model_used=llm_model_used,
        llm_fallback_used=llm_fallback_used,
        director_rationale=director_rationale,
    )

    # ---- Persist + notify ------------------------------------------------
    _persist_director_persona_state(plan)
    _post_curriculum_messages(plan)
    return plan


def _safe_list_str(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x) for x in v if x is not None][:8]
    if isinstance(v, str):
        return [v]
    return []


# ============================================================================
# Lift evaluator — one week later
# ============================================================================

def evaluate_curriculum_lift(
    plan_id: str,
    as_of: Optional[datetime] = None,
    lookback_days: int = DEFAULT_LOOKAHEAD_DAYS,
    threshold: float = 0.03,
    min_n_for_signal: int = 3,
) -> CurriculumOutcome:
    """One week (default) after a plan was issued, check whether the assigned
    investigations moved Brier on the targeted weakness.

    Classification rules:
      - 'effective'    if avg Brier lift across specialists >= threshold AND
                       at least one specialist met the per-specialist
                       threshold AND we have >= min_n_for_signal Brier
                       data points per assigned specialist.
      - 'no_lift'      if we have the data but lift < threshold.
      - 'inconclusive' if specialists' Brier samples are too sparse to call.

    Inputs:
      - `plan_id`: as returned by `run_research_director_cycle`.
    """
    as_of = as_of or _utc_now()
    conn = _conn()

    # Hydrate the plan from specialist_states (where we wrote it).
    plan = _load_plan(conn, plan_id)
    if plan is None:
        return CurriculumOutcome(
            plan_id=plan_id,
            evaluated_at=as_of,
            per_specialist_brier_delta={},
            classification="inconclusive",
            inconclusive=True,
            notes=[f"plan_not_found: {plan_id}"],
        )

    plan_as_of = plan.as_of
    window_start_iso = _iso(plan_as_of - timedelta(days=lookback_days))
    plan_as_of_iso = _iso(plan_as_of)
    window_end_iso = _iso(as_of)

    per_specialist_delta: dict[str, Optional[float]] = {}
    notes: list[str] = []
    counted = 0
    sum_lift = 0.0

    for sid in plan.per_specialist:
        before, n_before = _brier_window(conn, sid, window_start_iso, plan_as_of_iso)
        after, n_after = _brier_window(conn, sid, plan_as_of_iso, window_end_iso)
        if before is None or after is None or min(n_before, n_after) < min_n_for_signal:
            per_specialist_delta[sid] = None
            notes.append(
                f"{sid}: insufficient sample (before n={n_before}, after n={n_after})"
            )
            continue
        # Brier is "lower is better"; a positive lift means after < before.
        lift = before - after
        per_specialist_delta[sid] = lift
        counted += 1
        sum_lift += lift

    if counted == 0:
        return CurriculumOutcome(
            plan_id=plan_id,
            evaluated_at=as_of,
            per_specialist_brier_delta=per_specialist_delta,
            classification="inconclusive",
            inconclusive=True,
            notes=notes,
        )

    avg_lift = sum_lift / counted
    any_specialist_above = any(
        v is not None and v >= threshold for v in per_specialist_delta.values()
    )
    classification = "effective" if (avg_lift >= threshold and any_specialist_above) else "no_lift"
    notes.append(
        f"avg_lift={avg_lift:.4f} threshold={threshold:.3f} n={counted}"
    )

    outcome = CurriculumOutcome(
        plan_id=plan_id,
        evaluated_at=as_of,
        per_specialist_brier_delta=per_specialist_delta,
        classification=classification,
        inconclusive=False,
        notes=notes,
    )

    # Persist the outcome on the director's specialist_states so future
    # plans can read it and avoid repeating ineffective curricula.
    _persist_curriculum_outcome(plan_id, outcome)
    return outcome


def _brier_window(
    conn: sqlite3.Connection,
    specialist_id: str,
    start_iso: str,
    end_iso: str,
) -> tuple[Optional[float], int]:
    """Avg Brier score for `specialist_id` over `[start_iso, end_iso)`."""
    row = conn.execute(
        "SELECT avg(score) AS brier_avg, count(*) AS n FROM reward_log "
        "WHERE specialist_id = ? AND reward_kind = 'correctness' "
        "AND valid_from >= ? AND valid_from < ? "
        "AND transaction_to IS NULL",
        (specialist_id, start_iso, end_iso),
    ).fetchone()
    if row is None:
        return None, 0
    d = dict(row)
    if d.get("brier_avg") is None:
        return None, 0
    return float(d["brier_avg"]), int(d.get("n") or 0)


def _load_plan(conn: sqlite3.Connection, plan_id: str) -> Optional[CurriculumPlan]:
    """Walk `specialist_states` for the row that carries this plan."""
    rows = conn.execute(
        "SELECT state_json, valid_from FROM specialist_states "
        "WHERE specialist_id = ? AND state_kind = 'persona' "
        "AND transaction_to IS NULL "
        "ORDER BY transaction_from DESC",
        (DIRECTOR_SPECIALIST_ID,),
    ).fetchall()
    for r in rows:
        try:
            sj = json.loads(r["state_json"])
        except Exception:
            continue
        cp = sj.get("curriculum_plan") if isinstance(sj, dict) else None
        if not isinstance(cp, dict):
            continue
        if cp.get("plan_id") != plan_id:
            continue
        return _plan_from_dict(cp)
    return None


def _plan_from_dict(d: dict[str, Any]) -> CurriculumPlan:
    per_specialist: dict[str, SpecialistAllocation] = {}
    for sid, alloc_d in (d.get("per_specialist") or {}).items():
        invs = []
        for i in alloc_d.get("assigned_investigations") or []:
            invs.append(InvestigationAssignment(
                seed_id=i.get("seed_id") or f"inv_{uuid.uuid4().hex[:10]}",
                title=i.get("title") or "",
                rationale=i.get("rationale") or "",
                target_weakness_id=i.get("target_weakness_id"),
                target_hypothesis_id=i.get("target_hypothesis_id"),
                suggested_tool_uris=list(i.get("suggested_tool_uris") or []),
                expected_calls=int(i.get("expected_calls") or 30),
            ))
        per_specialist[sid] = SpecialistAllocation(
            specialist_id=sid,
            budget_usd=float(alloc_d.get("budget_usd") or 0.0),
            focus_areas=list(alloc_d.get("focus_areas") or []),
            expected_brier_lift=float(alloc_d.get("expected_brier_lift") or 0.0),
            assigned_investigations=invs,
            rationale=alloc_d.get("rationale") or "",
        )
    return CurriculumPlan(
        as_of=_from_iso(d.get("as_of")) or _utc_now(),
        cycle_id=d.get("cycle_id") or "unknown_cycle",
        plan_id=d.get("plan_id") or "unknown_plan",
        total_budget_usd=float(d.get("total_budget_usd") or 0.0),
        per_specialist=per_specialist,
        cross_cutting_themes=list(d.get("cross_cutting_themes") or []),
        explicit_weakness_assignments={
            k: list(v) for k, v in (d.get("explicit_weakness_assignments") or {}).items()
        },
        quality_flags=list(d.get("quality_flags") or []),
        llm_model_used=d.get("llm_model_used"),
        llm_fallback_used=bool(d.get("llm_fallback_used")),
        llm_cost_usd=float(d.get("llm_cost_usd") or 0.0),
        director_rationale=d.get("director_rationale") or "",
    )


def _persist_curriculum_outcome(plan_id: str, outcome: CurriculumOutcome) -> str:
    """Write a dehydration-style row tagging the plan with its measured lift."""
    conn = _conn()
    now = _utc_now()
    spst_id = f"spst_{uuid.uuid4().hex[:12]}"
    state_obj = {
        "kind": "curriculum_outcome",
        "plan_id": plan_id,
        "outcome": outcome.to_dict(),
    }
    conn.execute(
        "INSERT INTO specialist_states "
        "(id, specialist_id, persona_version, cycle_id, state_kind, "
        " state_json, valid_from, transaction_from) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            spst_id,
            DIRECTOR_SPECIALIST_ID,
            DIRECTOR_PERSONA_VERSION,
            plan_id,
            "dehydration",
            json.dumps(state_obj),
            _iso(now),
            _iso(now),
        ),
    )
    conn.commit()
    return spst_id


# ============================================================================
# Veto helper — used by the dashboard to block 24h auto-promote
# ============================================================================

def write_veto_message(
    candidate_id: str,
    reason: str,
    from_agent: str = "human_reviewer",
) -> str:
    """Write an `agent_messages` row with `kind='veto'` targeting a
    persona mutation candidate. The 24h auto-promote loop in
    `talis_desk/evolution` (Phase 5) is expected to read these and
    refuse to promote.

    Returns the inserted message id.

    NOTE: We intentionally do NOT mutate the `specialist_states` row
    here — vetoes are append-only signals; the evolution module is the
    one allowed to write rollback rows.
    """
    conn = _conn()
    now = _utc_now()
    msg_id = f"msg_{uuid.uuid4().hex[:12]}"
    payload = {
        "candidate_id": candidate_id,
        "reason": reason[:1000],
        "posted_at_iso": _iso(now),
    }
    dedupe_key = f"veto:{candidate_id}:{from_agent}"
    existing = conn.execute(
        "SELECT id FROM agent_messages WHERE from_agent = ? "
        "AND dedupe_key = ? AND transaction_to IS NULL LIMIT 1",
        (from_agent, dedupe_key),
    ).fetchone()
    if existing is not None:
        return existing["id"]
    conn.execute(
        "INSERT INTO agent_messages "
        "(id, from_agent, to_agent_or_topic, message_kind, payload, "
        " posted_at, read_by, expires_at, dedupe_key, valid_from, "
        " transaction_from, related_artifact_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            msg_id,
            from_agent,
            "evolution",
            VETO_KIND,
            json.dumps(payload),
            _iso(now),
            "[]",
            _iso(now + timedelta(days=30)),
            dedupe_key,
            _iso(now),
            _iso(now),
            candidate_id,
        ),
    )
    conn.commit()
    return msg_id

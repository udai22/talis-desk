"""FastAPI server for the Talis Desk Monitor.

Routes:
    GET  /              -> index.html (the SPA)
    GET  /api/state     -> live JSON describing all 8 panels
    GET  /api/sse       -> Server-Sent Events stream (push every 3s)
    GET  /healthz       -> liveness

Read-only. Discovers the newest desk.db under /var/folders/**/full_desk_*/
and (as fallback) ~/.talis/desk.db; reads the newest manifest+brief from
/tmp. Returns {"status":"no_active_run"} when nothing is found so the UI
can show a graceful waiting state.
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.responses import StreamingResponse


# ---------------------------------------------------------------------------
# Specialists the desk knows about (canonical list — used for the grid even
# when most haven't yet emitted a row in specialist_states).
# ---------------------------------------------------------------------------
KNOWN_SPECIALISTS: list[str] = [
    "macro_regime",
    "mega_cap",
    "equity_fundamentals",
    "catalyst_tracker",
    "flow_positioning",
    "smart_money",
    "options_vol",
    "microstructure",
    "anomaly_scanner",
    "sentiment_event",
    "rrg_rotation",
    "pair_trade",
    "polymarket_divergence",
    "structural_osho",
    "structural_thiel_lky",
    "osho_astrology_tracer",
    "factor_strategy",
    "stat_arb",
    "vol_quant",
    "time_series_signal",
    "retail_alpha",
    "money_rotation",
    "material_info_surveillance",
]

# Stage labels (left -> right pipeline)
STAGE_ORDER = ["HYDRATE", "PLAN", "EXPLORE", "SYNTHESIZE", "REFLECT", "DEHYDRATE"]

# Cost gauge stages (Panel 5)
COST_STAGES = ["plan", "explore_evidence", "synthesize_idea", "report_pipeline", "brief_headline"]

DAILY_COST_CAP = 100.0  # $100 daily cap
DESK_TZ = ZoneInfo("America/New_York")

INDEX_HTML = Path(__file__).parent / "index.html"
SCOUT_HTML = Path(__file__).parent / "scouts.html"

app = FastAPI(
    title="Talis Desk Monitor",
    docs_url=None,
    redoc_url=None,
)


# ---------------------------------------------------------------------------
# Filesystem discovery
# ---------------------------------------------------------------------------

def _newest(paths: list[str]) -> Optional[str]:
    if not paths:
        return None
    paths = [p for p in paths if os.path.exists(p)]
    if not paths:
        return None
    return max(paths, key=lambda p: os.path.getmtime(p))


def _db_activity_sort_key(path: str) -> tuple[str, int, float]:
    """Prefer the DB with the freshest desk activity, then row volume, then mtime."""
    latest = ""
    rows = 0
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro&immutable=0", uri=True, timeout=0.5)
        try:
            latest = conn.execute(
                "SELECT COALESCE(MAX(started_at), '') FROM tool_call_log"
            ).fetchone()[0] or ""
            rows = int(conn.execute("SELECT COUNT(*) FROM tool_call_log").fetchone()[0] or 0)
        finally:
            conn.close()
    except Exception:
        pass
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0.0
    return (latest, rows, mtime)


def _find_latest_desk_db() -> Optional[Path]:
    """Newest desk.db across the canonical locations.

    Order:
      1. env override TALIS_DESK_DB (explicit)
      2. newest of ~/.talis/desk.db and discovered run DBs
    """
    env = os.environ.get("TALIS_DESK_DB")
    if env and os.path.exists(env):
        return Path(env)

    home_db = Path.home() / ".talis" / "desk.db"
    candidates: list[str] = []
    if home_db.exists():
        candidates.append(str(home_db))

    candidates.extend(glob.glob("/var/folders/*/*/full_desk_*/desk.db"))
    candidates.extend(glob.glob("/var/folders/*/*/T/full_desk_*/desk.db"))
    candidates.extend(glob.glob("/tmp/full_desk_*/desk.db"))
    candidates.extend(glob.glob("/tmp/desk_*.db"))

    chosen = max(candidates, key=_db_activity_sort_key) if candidates else None
    return Path(chosen) if chosen else None


def _manifest_matches_db(path: str, db_path: Optional[Path]) -> bool:
    if db_path is None:
        return True
    try:
        payload = json.loads(Path(path).read_text())
        return str(payload.get("desk_db") or "") == str(db_path)
    except Exception:
        return False


def _find_latest_manifest(db_path: Optional[Path] = None) -> Optional[Path]:
    """Newest manifest whose ``desk_db`` matches ``db_path``.

    Strict: when a DB is provided we only return a manifest that points at
    the same DB. We never silently fall back to an unrelated manifest —
    callers rely on artifact coherence (manifest, brief, db all from the
    same cycle) for correctness, otherwise the UI shows rows from one
    cycle alongside a brief headline from another.
    """
    candidates = (
        glob.glob("/tmp/talis_swarm_manifest_*.json")
        + glob.glob("/tmp/talis_full_desk_manifest_*.json")
    )
    if db_path is not None:
        candidates = [p for p in candidates if _manifest_matches_db(p, db_path)]
    p = _newest(candidates)
    return Path(p) if p else None


def _find_latest_brief(manifest: Optional[dict] = None) -> Optional[Path]:
    """Return the brief paired with ``manifest``.

    Strict: the brief MUST come from ``manifest.brief_path``. We do not
    fall back to the newest brief in ``/tmp`` because that produces
    mixed-cycle state where ``desk_db`` and the brief headline come from
    different runs.
    """
    if not manifest:
        return None
    bp = manifest.get("brief_path")
    if isinstance(bp, str) and os.path.exists(bp):
        return Path(bp)
    return None


def _compute_artifact_coherence(
    db_path: Optional[Path],
    manifest_path: Optional[Path],
    manifest: Optional[dict],
    brief_path: Optional[Path],
) -> dict[str, Any]:
    """Diagnose whether the resolved artifacts are from the same cycle.

    Status values:
      * ``no_db`` — no desk DB discovered (monitor is idle)
      * ``missing_manifest`` — DB found but no manifest points at it
      * ``missing_brief`` — DB + manifest matched but brief is absent
      * ``ok`` — DB, manifest, and brief are all coherent
    """
    cycle_id = (
        manifest.get("cycle_id")
        if isinstance(manifest, dict) and manifest
        else None
    )
    if db_path is None:
        return {
            "status": "no_db",
            "cycle_id": None,
            "desk_db": None,
            "manifest_path": None,
            "brief_path": None,
            "reason": "no desk DB discovered",
        }
    if manifest_path is None or not manifest:
        return {
            "status": "missing_manifest",
            "cycle_id": None,
            "desk_db": str(db_path),
            "manifest_path": None,
            "brief_path": None,
            "reason": (
                f"no manifest matches desk_db={db_path}; refusing to pair "
                "with an unrelated cycle"
            ),
        }
    if brief_path is None:
        return {
            "status": "missing_brief",
            "cycle_id": cycle_id,
            "desk_db": str(db_path),
            "manifest_path": str(manifest_path),
            "brief_path": None,
            "reason": (
                "manifest brief_path missing on disk; not substituting an "
                "unrelated brief"
            ),
        }
    return {
        "status": "ok",
        "cycle_id": cycle_id,
        "desk_db": str(db_path),
        "manifest_path": str(manifest_path),
        "brief_path": str(brief_path),
        "reason": None,
    }


# ---------------------------------------------------------------------------
# SQLite helpers
# ---------------------------------------------------------------------------

def _open_ro(db_path: Path) -> sqlite3.Connection:
    """Open the desk db read-only (so we never lock the writer)."""
    uri = f"file:{db_path}?mode=ro&immutable=0"
    conn = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=2.0)
    conn.row_factory = sqlite3.Row
    return conn


def _safe(query: callable, default: Any) -> Any:
    try:
        return query()
    except Exception:
        return default


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(_safe(
        lambda: conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone(),
        None,
    ))


def _json_value(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw))
    except Exception:
        return default


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _today_desk() -> str:
    return datetime.now(timezone.utc).astimezone(DESK_TZ).strftime("%Y-%m-%d")


def _ago_seconds(iso: Optional[str]) -> Optional[float]:
    if not iso:
        return None
    try:
        s = iso.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Panel builders
# ---------------------------------------------------------------------------

def _panel_hero(conn: sqlite3.Connection, manifest: dict) -> dict:
    """Panel 1 — 4 huge numbers."""
    today = _today_utc()

    # Reports today. Grade filtering belongs in the report panel; the hero
    # should answer "did the desk produce research?" without hiding honest
    # watchlist reports whose confidence is intentionally low.
    reports_today = _safe(lambda: conn.execute(
        "SELECT COUNT(*) AS n FROM research_reports "
        "WHERE date(valid_from) = ?",
        (today,),
    ).fetchone()["n"], 0)

    # Active specialists today: distinct specialist_id with rows in
    # specialist_states or tool_call_log today.
    active = _safe(lambda: conn.execute(
        "SELECT COUNT(DISTINCT specialist_id) AS n FROM tool_call_log "
        "WHERE date(started_at) = ?", (today,),
    ).fetchone()["n"], 0)
    if active == 0:
        active = _safe(lambda: conn.execute(
            "SELECT COUNT(DISTINCT specialist_id) AS n FROM specialist_states "
            "WHERE date(valid_from) = ?", (today,),
        ).fetchone()["n"], 0)

    # Errors today (red badge)
    errors = _safe(lambda: conn.execute(
        "SELECT COUNT(*) AS n FROM tool_call_log "
        "WHERE date(started_at) = ? AND error IS NOT NULL", (today,),
    ).fetchone()["n"], 0)

    # Cycle cost ($ today). Older rows may under-record adversarial LLM spend
    # in cost_ledger, so prefer a matching swarm manifest total when present.
    cost_today = _safe(lambda: conn.execute(
        "SELECT COALESCE(SUM(amount_usd), 0) AS s FROM cost_ledger "
        "WHERE date_utc = ?", (today,),
    ).fetchone()["s"], 0.0)
    manifest_cost = (((manifest or {}).get("cost_breakdown") or {}).get("total"))
    if manifest_cost is not None:
        try:
            cost_today = max(float(cost_today), float(manifest_cost))
        except Exception:
            pass

    avg_grade = _safe(lambda: conn.execute(
        """
        SELECT AVG(CAST(json_extract(payload, '$.grade.overall_score') AS REAL)) AS a
        FROM research_reports
        WHERE date(valid_from) = ?
          AND json_extract(payload, '$.grade.overall_score') IS NOT NULL
        """,
        (today,),
    ).fetchone()["a"], None)
    avg_grade = round(float(avg_grade), 1) if avg_grade is not None else None

    return {
        "reports_today": int(reports_today),
        "active_specialists": int(active),
        "total_specialists": len(KNOWN_SPECIALISTS),
        "errors_today": int(errors),
        "cycle_cost_usd": round(float(cost_today), 4),
        "daily_cap_usd": DAILY_COST_CAP,
        "avg_grade": avg_grade,
    }


def _panel_specialists(conn: sqlite3.Connection) -> list[dict]:
    """Panel 2 — one row per specialist."""
    today = _today_utc()

    # Latest persona/state per specialist
    state_rows = _safe(lambda: conn.execute(
        """
        SELECT s.specialist_id, s.persona_version, s.state_kind, s.cycle_id,
               s.valid_from
        FROM specialist_states s
        INNER JOIN (
            SELECT specialist_id, MAX(valid_from) AS max_vf
            FROM specialist_states GROUP BY specialist_id
        ) m ON s.specialist_id = m.specialist_id AND s.valid_from = m.max_vf
        """
    ).fetchall(), [])
    state_map = {r["specialist_id"]: dict(r) for r in state_rows}

    # Hypotheses count today per specialist
    hyp_rows = _safe(lambda: conn.execute(
        "SELECT specialist_id, COUNT(*) AS n FROM hypotheses "
        "WHERE date(valid_from) = ? GROUP BY specialist_id", (today,),
    ).fetchall(), [])
    hyp_map = {r["specialist_id"]: r["n"] for r in hyp_rows}

    # Tool calls today: ok + err per specialist + last started_at
    tc_rows = _safe(lambda: conn.execute(
        """
        SELECT specialist_id,
               SUM(CASE WHEN error IS NULL THEN 1 ELSE 0 END) AS ok,
               SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) AS err,
               MAX(started_at) AS last_at
        FROM tool_call_log
        WHERE date(started_at) = ?
        GROUP BY specialist_id
        """, (today,),
    ).fetchall(), [])
    tc_map = {r["specialist_id"]: dict(r) for r in tc_rows}

    out: list[dict] = []
    for sid in KNOWN_SPECIALISTS:
        st = state_map.get(sid, {})
        tc = tc_map.get(sid, {})
        ok = tc.get("ok") or 0
        err = tc.get("err") or 0
        last_at = tc.get("last_at") or st.get("valid_from")
        ago = _ago_seconds(last_at)
        # Status heuristic
        if ago is None:
            status = "idle"
        elif ago < 30:
            status = "running"
        elif err and ago < 600:
            status = "error"
        elif ago < 3600:
            status = "complete"
        else:
            status = "idle"
        # Stage heuristic (best-effort — desk doesn't emit explicit stage events)
        stage = _infer_stage(st.get("state_kind"), ok, hyp_map.get(sid, 0))
        out.append({
            "specialist_id": sid,
            "persona_version": st.get("persona_version") or "—",
            "stage": stage,
            "hypotheses": int(hyp_map.get(sid, 0)),
            "hypotheses_target": 12,
            "tool_calls_ok": int(ok),
            "tool_calls_err": int(err),
            "tool_success_pct": round(100 * ok / (ok + err), 1) if (ok + err) else None,
            "last_at": last_at,
            "ago_seconds": ago,
            "status": status,
            "cycle_id": st.get("cycle_id"),
        })
    # Sort: running > error > complete > idle, then ago asc.
    rank = {"running": 0, "error": 1, "complete": 2, "idle": 3}
    out.sort(key=lambda r: (rank[r["status"]], r["ago_seconds"] or 9e9))
    return out


def _infer_stage(state_kind: Optional[str], n_tools: int, n_hyps: int) -> str:
    if state_kind == "mutation_candidate":
        return "REFLECT"
    if n_tools == 0 and n_hyps == 0:
        return "HYDRATE"
    if n_hyps and n_tools < 3:
        return "PLAN"
    if n_tools >= 3 and n_hyps:
        return "EXPLORE"
    if n_tools >= 8:
        return "SYNTHESIZE"
    return "HYDRATE"


def _panel_top_reports(conn: sqlite3.Connection) -> list[dict]:
    """Panel 3 — Top 5 reports today (or fallback to top trade ideas)."""
    rows = _safe(lambda: conn.execute(
        """
        SELECT id, specialist_id, title, abstract, body_md, confidence,
               novelty_score, adversarial_severity, valid_from,
               CAST(json_extract(payload, '$.grade.overall_score') AS REAL) AS grade
        FROM research_reports
        ORDER BY grade DESC NULLS LAST, confidence DESC NULLS LAST, valid_from DESC
        LIMIT 5
        """
    ).fetchall(), [])
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        sev = (d.get("adversarial_severity") or "low").lower()
        out.append({
            "id": d["id"],
            "specialist_id": d["specialist_id"],
            "title": d["title"],
            "abstract": d.get("abstract") or "",
            "body_md": (d.get("body_md") or "")[:4000],
            "grade": round(float(d.get("grade")), 1)
            if d.get("grade") is not None else round((d.get("confidence") or 0) * 10, 1),
            "confidence": d.get("confidence"),
            "severity": sev,
            "valid_from": d.get("valid_from"),
        })
    if out:
        return out

    # Fallback to trade ideas when reports are empty
    rows = _safe(lambda: conn.execute(
        """
        SELECT id, specialist_id, instrument, direction, edge_thesis,
               confidence, valid_from
        FROM trade_ideas
        ORDER BY confidence DESC, valid_from DESC LIMIT 5
        """
    ).fetchall(), [])
    for r in rows:
        d = dict(r)
        out.append({
            "id": d["id"],
            "specialist_id": d["specialist_id"],
            "title": f"{d['instrument']} {d['direction'].upper()}",
            "abstract": d.get("edge_thesis") or "",
            "body_md": d.get("edge_thesis") or "",
            "grade": round((d.get("confidence") or 0) * 10, 1),
            "confidence": d.get("confidence"),
            "severity": "low",
            "valid_from": d.get("valid_from"),
        })
    return out


def _panel_calendar(conn: sqlite3.Connection, brief_md: Optional[str]) -> dict:
    """Panel 4 — today's calendar gate from the brief markdown."""
    today = _today_desk()
    catalysts: list[dict] = []
    severity = "low"
    headline_note = None
    if brief_md:
        # Extract the first calendar-gate section if present
        m = re.search(r"(?im)^##+\s*calendar[^\n]*$", brief_md)
        if m:
            tail = brief_md[m.end():]
            chunk = tail.split("\n## ", 1)[0]
            # Pull bullet lines
            for line in chunk.splitlines():
                line = line.strip()
                if line.startswith(("- ", "* ", "• ")):
                    text = line[2:].strip()
                    sev = "low"
                    low = text.lower()
                    if low.startswith("must lead:") or low.startswith("critical:"):
                        sev, severity = "critical", "critical"
                    elif any(k in low for k in ("fomc", "cpi", "ppi", "nfp", "fed minutes")):
                        sev = "high"
                        if severity not in {"critical"}:
                            severity = "high"
                    elif any(k in low for k in ("earnings", "8-k", "10-q", "spx options")):
                        sev = "med"
                        if severity == "low":
                            severity = "med"
                    catalysts.append({"text": text[:240], "severity": sev})
        if severity in {"critical", "high"}:
            headline_note = "Brief headline must lead with this catalyst."
    return {
        "date": today,
        "catalysts": catalysts[:8],
        "severity": severity,
        "headline_note": headline_note,
    }


def _panel_cost(conn: sqlite3.Connection) -> dict:
    """Panel 5 — cost gauge + per-stage breakdown + sparkline."""
    today = _today_utc()
    total = _safe(lambda: conn.execute(
        "SELECT COALESCE(SUM(amount_usd),0) AS s FROM cost_ledger WHERE date_utc=?",
        (today,),
    ).fetchone()["s"], 0.0)

    rows = _safe(lambda: conn.execute(
        "SELECT stage, COALESCE(SUM(amount_usd),0) AS s FROM cost_ledger "
        "WHERE date_utc=? GROUP BY stage", (today,),
    ).fetchall(), [])
    by_stage = {r["stage"]: float(r["s"]) for r in rows}

    # Last 24h hourly spend for the sparkline
    hourly_rows = _safe(lambda: conn.execute(
        """
        SELECT strftime('%H', recorded_at) AS hr,
               COALESCE(SUM(amount_usd),0) AS s
        FROM cost_ledger
        WHERE recorded_at >= datetime('now', '-24 hours')
        GROUP BY hr ORDER BY hr
        """
    ).fetchall(), [])
    sparkline = [round(float(r["s"]), 4) for r in hourly_rows]
    if len(sparkline) < 24:
        sparkline = [0.0] * (24 - len(sparkline)) + sparkline

    return {
        "today_usd": round(float(total), 4),
        "cap_usd": DAILY_COST_CAP,
        "pct_used": round(100 * float(total) / DAILY_COST_CAP, 2),
        "by_stage": {s: round(by_stage.get(s, 0.0), 4) for s in COST_STAGES + list(by_stage.keys())},
        "sparkline_24h": sparkline,
    }


def _panel_vehicle_flow(conn: sqlite3.Connection) -> dict:
    """Panel 6 — HL vehicle flow (BHYP / THYP / PURR).

    Reads agent_messages of kind == 'observation' published by
    material_info_surveillance, falling back to absence-of-data state.
    """
    today = _today_utc()
    rows = _safe(lambda: conn.execute(
        """
        SELECT payload, posted_at FROM agent_messages
        WHERE from_agent = 'material_info_surveillance'
          AND date(posted_at) >= date('now', '-7 days')
        ORDER BY posted_at DESC LIMIT 20
        """
    ).fetchall(), [])

    vehicles = {"BHYP": None, "THYP": None, "PURR": None}
    multiple_vs_baseline = None
    status = "no_data"

    for r in rows:
        try:
            p = json.loads(r["payload"])
        except Exception:
            continue
        for ticker in vehicles:
            v = p.get(ticker) or p.get(f"${ticker}") or p.get(ticker.lower())
            if isinstance(v, (int, float)) and vehicles[ticker] is None:
                vehicles[ticker] = float(v)
        if multiple_vs_baseline is None:
            multiple_vs_baseline = p.get("multiple_vs_af_baseline") or p.get("multiple")

    # Decide status
    worst = max((v or 0) for v in vehicles.values()) if any(vehicles.values()) else 0
    if worst >= 0.10:
        status = "critical"
    elif worst >= 0.05:
        status = "elevated"
    elif any(v is not None for v in vehicles.values()):
        status = "below_threshold"

    return {
        "vehicles": vehicles,
        "multiple_vs_baseline": multiple_vs_baseline,
        "status": status,
        "sparkline_7d": [],
    }


def _panel_sources(conn: sqlite3.Connection) -> dict:
    """Panel 7 — last 5 source library queries + top author / unique books."""
    rows = _safe(lambda: conn.execute(
        """
        SELECT specialist_id, tool_uri, args_json, started_at
        FROM tool_call_log
        WHERE tool_uri LIKE '%source_library%' OR tool_uri LIKE '%book%'
           OR tool_uri LIKE '%cite%'
        ORDER BY started_at DESC LIMIT 10
        """
    ).fetchall(), [])

    queries: list[dict] = []
    books: set[str] = set()
    authors: dict[str, int] = {}
    for r in rows:
        try:
            args = json.loads(r["args_json"] or "{}")
        except Exception:
            args = {}
        book = args.get("book") or args.get("title")
        chapter = args.get("chapter")
        page = args.get("page")
        author = args.get("author")
        if book:
            books.add(book)
        if author:
            authors[author] = authors.get(author, 0) + 1
        queries.append({
            "specialist_id": r["specialist_id"],
            "tool_uri": r["tool_uri"],
            "book": book, "chapter": chapter, "page": page, "author": author,
            "started_at": r["started_at"],
        })

    top_author = max(authors.items(), key=lambda kv: kv[1])[0] if authors else None
    return {
        "recent": queries[:5],
        "unique_books_cited": len(books),
        "top_author": top_author,
    }


def _panel_information_rrg(conn: sqlite3.Connection) -> dict:
    """Information RRG preview.

    Each point is a rough 2D projection:
      x = relative information strength (share of evidence in the last 24h)
      y = momentum (share of evidence in the last 60m vs prior 24h)

    This is intentionally approximate until the scout seed registry lands.
    It gives the Pipeline Inspector a live surface to evolve from.
    """
    dimensions = [
        ("Catalyst", ("calendar", "event", "earning", "filing", "sec", "fomc", "cpi", "ppi")),
        ("Narrative", ("news", "gdelt", "social", "sentiment", "mindshare")),
        ("Vol", ("option", "vol", "skew", "funding", "open_interest")),
        ("Flow", ("flow", "wallet", "whale", "money", "positioning", "cot", "vehicle")),
        ("On-chain", ("onchain", "hyperliquid", "hl_", "nansen", "coingecko")),
        ("Source", ("source_library", "semantic", "book", "cite")),
    ]

    rows_24h = _safe(lambda: conn.execute(
        """
        SELECT tool_uri, specialist_id, started_at, error
        FROM tool_call_log
        WHERE started_at >= datetime('now', '-24 hours')
        """
    ).fetchall(), [])
    rows_60m = _safe(lambda: conn.execute(
        """
        SELECT tool_uri, specialist_id, started_at, error
        FROM tool_call_log
        WHERE started_at >= datetime('now', '-60 minutes')
        """
    ).fetchall(), [])

    total_24h = max(1, len(rows_24h))
    total_60m = max(1, len(rows_60m))
    points: list[dict[str, Any]] = []

    for name, needles in dimensions:
        def matches(row: sqlite3.Row) -> bool:
            blob = f"{row['tool_uri']} {row['specialist_id']}".lower()
            return any(n in blob for n in needles)

        n24 = sum(1 for r in rows_24h if matches(r))
        n60 = sum(1 for r in rows_60m if matches(r))
        err24 = sum(1 for r in rows_24h if matches(r) and r["error"] is not None)
        x = n24 / total_24h
        recent_share = n60 / total_60m
        prior_share = max(0.0001, (n24 - n60) / max(1, total_24h - total_60m))
        y = recent_share / prior_share
        points.append({
            "dimension": name,
            "x": round(min(1.0, x), 4),
            "y": round(min(3.0, y), 4),
            "calls_24h": n24,
            "calls_60m": n60,
            "errors_24h": err24,
            "quadrant": _rrg_quadrant(x, y),
        })

    return {
        "points": points,
        "x_label": "relative information strength",
        "y_label": "information momentum",
        "note": "preview until scout seed registry and real manifold coordinates land",
    }


def _rrg_quadrant(x: float, y: float) -> str:
    if x >= 0.18 and y >= 1.0:
        return "leading"
    if x >= 0.18 and y < 1.0:
        return "weakening"
    if x < 0.18 and y >= 1.0:
        return "improving"
    return "lagging"


def _panel_brier(conn: sqlite3.Connection) -> list[dict]:
    """Panel 8 — per-specialist 30d Brier."""
    rows = _safe(lambda: conn.execute(
        """
        SELECT specialist_id, brier_avg, n
        FROM mv_specialist_brier_rolling
        WHERE day >= date('now', '-30 days')
        ORDER BY brier_avg ASC
        """
    ).fetchall(), [])
    if rows:
        out = [
            {
                "specialist_id": r["specialist_id"],
                "brier": round(float(r["brier_avg"] or 0), 4),
                "cycles": int(r["n"] or 0),
                "calibrating": (r["n"] or 0) < 30,
            }
            for r in rows
        ]
        return out

    # Fallback: per-specialist average Brier from trade_ideas where resolved
    rows = _safe(lambda: conn.execute(
        """
        SELECT specialist_id, AVG(brier) AS b, COUNT(brier) AS n
        FROM trade_ideas
        WHERE brier IS NOT NULL
        GROUP BY specialist_id ORDER BY b ASC
        """
    ).fetchall(), [])
    out = []
    for r in rows:
        out.append({
            "specialist_id": r["specialist_id"],
            "brier": round(float(r["b"] or 0), 4) if r["b"] is not None else None,
            "cycles": int(r["n"] or 0),
            "calibrating": (r["n"] or 0) < 30,
        })
    # Pad with calibrating entries for known specialists missing data
    seen = {r["specialist_id"] for r in out}
    for sid in KNOWN_SPECIALISTS:
        if sid not in seen:
            out.append({"specialist_id": sid, "brier": None, "cycles": 0, "calibrating": True})
    return out


# ---------------------------------------------------------------------------
# Scout Inspector data
# ---------------------------------------------------------------------------

def _latest_scout_cycle(conn: sqlite3.Connection, manifest: Optional[dict] = None) -> Optional[str]:
    """Resolve the cycle the scout inspector should show.

    Prefer the manifest cycle so the visualizer stays artifact-coherent.
    Fall back to the newest scout/info-string cycle in the DB for older
    runs that predate scout-only manifests.
    """
    if manifest and manifest.get("cycle_id"):
        return str(manifest["cycle_id"])
    for table, column in (
        ("information_strings", "cycle_id"),
        ("hypotheses", "cycle_id"),
        ("blackboard_events", "cycle_id"),
    ):
        if not _table_exists(conn, table):
            continue
        row = _safe(
            lambda t=table, c=column: conn.execute(
                f"SELECT {c} AS cycle_id FROM {t} "
                f"WHERE {c} IS NOT NULL AND {c} != '' "
                "ORDER BY rowid DESC LIMIT 1"
            ).fetchone(),
            None,
        )
        if row and row["cycle_id"]:
            return str(row["cycle_id"])
    return None


def _panel_scouts_summary(conn: sqlite3.Connection, manifest: Optional[dict] = None) -> dict[str, Any]:
    payload = _build_scout_inspector_payload(conn, manifest=manifest, limit=1)
    summary = payload.get("summary") or {}
    return {
        "cycle_id": payload.get("cycle_id"),
        "run_mode": (manifest or {}).get("run_mode"),
        "scouts": summary.get("scouts", 0),
        "strings": summary.get("strings", 0),
        "avg_confidence": summary.get("avg_confidence"),
        "prompt_variants": summary.get("prompt_variants", {}),
    }


def _panel_research_evolution(conn: sqlite3.Connection, manifest: Optional[dict] = None) -> dict[str, Any]:
    payload = _build_research_evolution_payload(conn, manifest=manifest, limit=12)
    summary = payload.get("summary") or {}
    return {
        "status": payload.get("status"),
        "cycle_id": payload.get("cycle_id"),
        "geometry_cells": summary.get("geometry_cells", 0),
        "frontier_trade_candidates": summary.get("frontier_trade_candidates", 0),
        "top_route_directive": summary.get("top_route_directive"),
        "top_trade_scream_score": summary.get("top_trade_scream_score"),
        "top_verifier_readiness": summary.get("top_verifier_readiness"),
        "active_program": summary.get("active_program"),
        "active_generation": summary.get("active_generation"),
        "best_evaluator_score": summary.get("best_evaluator_score"),
        "best_evaluator_passed": summary.get("best_evaluator_passed"),
        "latest_mutation_kind": summary.get("latest_mutation_kind"),
        "hard_experiment_status": summary.get("hard_experiment_status"),
        "next_action": summary.get("next_action"),
    }


def _build_scout_inspector_payload(
    conn: sqlite3.Connection,
    *,
    manifest: Optional[dict] = None,
    cycle_id: Optional[str] = None,
    limit: int = 80,
) -> dict[str, Any]:
    cycle = cycle_id or _latest_scout_cycle(conn, manifest)
    if not cycle:
        return {
            "status": "no_scout_cycle",
            "cycle_id": None,
            "summary": _empty_scout_summary(),
            "scouts": [],
            "strings": [],
        }
    strings = _fetch_information_strings(conn, cycle_id=cycle, limit=max(limit * 3, 120))
    events = _fetch_event_intelligence(conn, cycle_id=cycle, limit=max(limit * 2, 80))
    node_intel = _fetch_node_intelligence(conn, cycle_id=cycle, limit=max(limit * 2, 80))
    tool_proposals = _fetch_analysis_tool_proposals(conn, cycle_id=cycle, limit=max(limit * 3, 120))
    scouts = _fetch_scout_rows(
        conn,
        cycle_id=cycle,
        strings=strings,
        events=events,
        node_intel=node_intel,
        tool_proposals=tool_proposals,
        limit=limit,
    )
    return {
        "status": "ok",
        "cycle_id": cycle,
        "run_mode": (manifest or {}).get("run_mode"),
        "summary": _scout_summary(scouts, strings, events, node_intel),
        "scouts": scouts,
        "strings": strings[:limit],
        "event_intelligence": events[:limit],
        "node_intelligence": node_intel[:limit],
        "tool_proposals": tool_proposals[:limit],
    }


def _build_research_evolution_payload(
    conn: sqlite3.Connection,
    *,
    manifest: Optional[dict] = None,
    cycle_id: Optional[str] = None,
    limit: int = 80,
) -> dict[str, Any]:
    """Read-only AlphaEvolve-style inspector for the current research loop."""
    cycle = cycle_id or _latest_scout_cycle(conn, manifest)
    if not cycle:
        return {
            "status": "no_scout_cycle",
            "cycle_id": None,
            "summary": _empty_research_evolution_summary(),
            "alpha_geometry": [],
            "market_evolve": _empty_market_evolve_payload(),
            "evolution_control": _empty_evolution_control_payload(cycle_id=None, reason="no_scout_cycle"),
        }
    geometry = _fetch_alpha_geometry(conn, cycle_id=cycle, limit=limit)
    programs = _fetch_market_evolve_programs(conn, limit=limit)
    evaluations = _fetch_market_evolve_evaluations(conn, cycle_id=cycle, limit=limit)
    mutations = _fetch_market_evolve_mutations(conn, cycle_id=cycle, limit=limit)
    experiments = _fetch_market_evolve_experiments(conn, cycle_id=cycle, limit=limit)
    results = _fetch_market_evolve_experiment_results(conn, cycle_id=cycle, limit=limit)
    active_program = next((p for p in programs if p.get("status") == "active"), programs[0] if programs else {})
    best_evaluation = evaluations[0] if evaluations else {}
    latest_mutation = mutations[0] if mutations else {}
    latest_experiment = experiments[0] if experiments else {}
    top_cell = geometry[0] if geometry else {}
    status = "ok" if (geometry or programs or evaluations or mutations or experiments) else "empty"
    evolution_control = (
        _manifest_evolution_control_payload(manifest, cycle)
        or _live_evolution_control_payload(
            conn,
            cycle_id=cycle,
            active_program=active_program,
            best_evaluation=best_evaluation,
        )
    )
    summary = _research_evolution_summary(
        cycle_id=cycle,
        top_cell=top_cell,
        active_program=active_program,
        best_evaluation=best_evaluation,
        latest_mutation=latest_mutation,
        latest_experiment=latest_experiment,
        geometry=geometry,
        experiments=experiments,
        results=results,
        evolution_control=evolution_control,
    )
    return {
        "status": status,
        "cycle_id": cycle,
        "summary": summary,
        "alpha_geometry": geometry,
        "evolution_control": evolution_control,
        "market_evolve": {
            "active_program": active_program or None,
            "programs": programs,
            "evaluations": evaluations,
            "mutations": mutations,
            "experiments": experiments,
            "experiment_results": results,
        },
        "control_loop": {
            "inputs": [
                "information_strings",
                "tool_call_log",
                "analysis_tool_proposals",
                "claim_votes",
                "research_reports",
                "information_geometry_snapshots",
            ],
            "evaluator": "accepted_unique_high_quality_coverage_per_dollar",
            "mutation_surface": [
                "prompt_policy",
                "routing_thresholds",
                "tool_request_policy",
                "geometry_weights",
                "objective_weights",
            ],
            "hard_experiment": "matched_policy_ab",
        },
    }


def _research_evolution_summary(
    *,
    cycle_id: str,
    top_cell: dict[str, Any],
    active_program: dict[str, Any],
    best_evaluation: dict[str, Any],
    latest_mutation: dict[str, Any],
    latest_experiment: dict[str, Any],
    geometry: list[dict[str, Any]],
    experiments: list[dict[str, Any]],
    results: list[dict[str, Any]],
    evolution_control: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    route = str(top_cell.get("route_directive") or "")
    mutation_kind = str(latest_mutation.get("mutation_kind") or "")
    experiment_status = str(latest_experiment.get("status") or "")
    next_action = _evolution_next_action(route, mutation_kind, experiment_status)
    proof = (evolution_control or {}).get("proof") if isinstance(evolution_control, dict) else {}
    cortex = (evolution_control or {}).get("geometry_cortex_review") if isinstance(evolution_control, dict) else {}
    proposed = cortex.get("proposed_geometry_policy") if isinstance(cortex, dict) else {}
    return {
        **_empty_research_evolution_summary(),
        "cycle_id": cycle_id,
        "geometry_cells": len(geometry),
        "frontier_trade_candidates": sum(
            1 for cell in geometry
            if str(cell.get("route_directive") or "") == "verify_now"
            or "frontier_trade_candidate" in (cell.get("quality_flags") or [])
        ),
        "top_cell_key": top_cell.get("cell_key"),
        "top_route_directive": route or None,
        "top_trade_scream_score": top_cell.get("trade_scream_score"),
        "top_verifier_readiness": top_cell.get("verifier_readiness"),
        "top_coordinates": top_cell.get("coordinates") or {},
        "top_metrics": top_cell.get("metrics") or {},
        "active_program": active_program.get("name"),
        "active_program_id": active_program.get("id"),
        "active_generation": active_program.get("generation"),
        "active_program_score": active_program.get("score"),
        "best_evaluator_score": best_evaluation.get("score"),
        "best_evaluator_passed": best_evaluation.get("passed"),
        "best_evaluator_rationale": best_evaluation.get("rationale"),
        "latest_mutation_kind": mutation_kind or None,
        "latest_mutation_status": latest_mutation.get("status"),
        "latest_mutation_rationale": latest_mutation.get("rationale"),
        "hard_experiment_count": len(experiments),
        "hard_experiment_status": experiment_status or None,
        "experiment_results": len(results),
        "evolution_control_source": (evolution_control or {}).get("source") if isinstance(evolution_control, dict) else None,
        "shape_can_direct_next": bool((proof or {}).get("shape_can_direct_next")),
        "cortex_diagnostic_codes": (proof or {}).get("diagnostic_codes") or [],
        "cortex_mutation_hint": (
            (proof or {}).get("mutation_kind_hint")
            or (proposed or {}).get("mutation_kind_hint")
        ),
        "lineage_frontier_count": int((proof or {}).get("lineage_frontier_count") or 0),
        "next_action": next_action,
    }


def _empty_research_evolution_summary() -> dict[str, Any]:
    return {
        "cycle_id": None,
        "geometry_cells": 0,
        "frontier_trade_candidates": 0,
        "top_cell_key": None,
        "top_route_directive": None,
        "top_trade_scream_score": None,
        "top_verifier_readiness": None,
        "top_coordinates": {},
        "top_metrics": {},
        "active_program": None,
        "active_program_id": None,
        "active_generation": None,
        "active_program_score": None,
        "best_evaluator_score": None,
        "best_evaluator_passed": None,
        "best_evaluator_rationale": None,
        "latest_mutation_kind": None,
        "latest_mutation_status": None,
        "latest_mutation_rationale": None,
        "hard_experiment_count": 0,
        "hard_experiment_status": None,
        "experiment_results": 0,
        "evolution_control_source": None,
        "shape_can_direct_next": False,
        "cortex_diagnostic_codes": [],
        "cortex_mutation_hint": None,
        "lineage_frontier_count": 0,
        "next_action": "waiting for geometry and evaluator output",
    }


def _empty_market_evolve_payload() -> dict[str, Any]:
    return {
        "active_program": None,
        "programs": [],
        "evaluations": [],
        "mutations": [],
        "experiments": [],
        "experiment_results": [],
    }


def _empty_evolution_control_payload(
    *,
    cycle_id: Optional[str],
    reason: str = "unavailable",
) -> dict[str, Any]:
    return {
        "schema_version": "swarm_evolution_control_v1",
        "cycle_id": cycle_id,
        "source": "monitor_empty",
        "status": "empty",
        "reason": reason,
        "active_program": None,
        "alpha_geometry_action_plan": {},
        "geometry_cortex_review": {},
        "market_evolve_lineage": {},
        "proof": {
            "action_plan_ready": False,
            "cortex_review_ready": False,
            "shape_can_direct_next": False,
            "diagnostic_codes": [],
            "policy_patch_present": False,
            "lineage_frontier_count": 0,
            "mutation_kind_hint": None,
        },
    }


def _manifest_evolution_control_payload(
    manifest: Optional[dict],
    cycle_id: str,
) -> Optional[dict[str, Any]]:
    control = (manifest or {}).get("evolution_control") if isinstance(manifest, dict) else None
    if not isinstance(control, dict):
        return None
    control_cycle = str(control.get("cycle_id") or "")
    if control_cycle and control_cycle != cycle_id:
        return None
    out = dict(control)
    out.setdefault("schema_version", "swarm_evolution_control_v1")
    out.setdefault("cycle_id", cycle_id)
    out.setdefault("source", "manifest")
    out.setdefault("proof", {})
    return out


def _live_evolution_control_payload(
    conn: sqlite3.Connection,
    *,
    cycle_id: str,
    active_program: dict[str, Any],
    best_evaluation: dict[str, Any],
) -> dict[str, Any]:
    if not _table_exists(conn, "information_geometry_snapshots"):
        return _empty_evolution_control_payload(cycle_id=cycle_id, reason="missing_information_geometry_snapshots")
    try:
        from talis_desk.information_map import build_evolution_control_payload

        return build_evolution_control_payload(
            cycle_id=cycle_id,
            active_program=active_program or None,
            best_evaluation=best_evaluation or None,
            conn=conn,
            source="monitor_live_rebuild",
        )
    except Exception as exc:
        payload = _empty_evolution_control_payload(cycle_id=cycle_id, reason=f"{type(exc).__name__}: {exc}")
        payload["source"] = "monitor_live_rebuild"
        payload["status"] = "error"
        return payload


def _evolution_next_action(route: str, mutation_kind: str, experiment_status: str) -> str:
    if experiment_status in {"planned", "running"} and mutation_kind:
        return f"run matched A/B for {mutation_kind.replace('_', ' ')}"
    if route == "verify_now":
        return "route top geometry cell to verifier spend"
    if route == "widen_scouts":
        return "assign more independent scouts to the top cell"
    if route == "widen_sources":
        return "add source-family breadth before verification"
    if route == "repair_sources":
        return "repair fragile evidence before increasing conviction"
    if route == "resolve_tension":
        return "send contradictions to adversarial verifier"
    if mutation_kind:
        return f"evaluate mutation {mutation_kind.replace('_', ' ')}"
    return "observe until geometry or evaluator pressure changes"


def _fetch_alpha_geometry(
    conn: sqlite3.Connection,
    *,
    cycle_id: str,
    limit: int,
) -> list[dict[str, Any]]:
    if not _table_exists(conn, "information_geometry_snapshots"):
        return []
    rows = _safe(
        lambda: conn.execute(
            """
            SELECT id, cycle_id, cell_key, entity, theme, horizon, lens,
                   string_count, scout_count, evidence_ref_count,
                   source_families_json, coordinates_json, metrics_json,
                   route_directive, trade_scream_score, verifier_readiness,
                   quality_flags, created_at, valid_from
            FROM information_geometry_snapshots
            WHERE cycle_id = ?
            ORDER BY trade_scream_score DESC, verifier_readiness DESC,
                     COALESCE(created_at, valid_from, '') DESC
            LIMIT ?
            """,
            (cycle_id, int(limit)),
        ).fetchall(),
        [],
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        d = dict(row)
        d["source_families"] = _json_value(d.pop("source_families_json", None), [])
        d["coordinates"] = _json_value(d.pop("coordinates_json", None), {})
        d["metrics"] = _json_value(d.pop("metrics_json", None), {})
        d["quality_flags"] = _json_value(d.get("quality_flags"), [])
        d["string_count"] = int(d.get("string_count") or 0)
        d["scout_count"] = int(d.get("scout_count") or 0)
        d["evidence_ref_count"] = int(d.get("evidence_ref_count") or 0)
        d["trade_scream_score"] = _float(d.get("trade_scream_score"), 0.0)
        d["verifier_readiness"] = _float(d.get("verifier_readiness"), 0.0)
        out.append(d)
    return out


def _fetch_market_evolve_programs(
    conn: sqlite3.Connection,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    if not _table_exists(conn, "market_evolve_programs"):
        return []
    rows = _safe(
        lambda: conn.execute(
            """
            SELECT id, program_kind, name, generation, parent_program_ids_json,
                   genome_json, objective_json, status, created_from_cycle_id,
                   score, metrics_json, quality_flags, created_at, valid_from
            FROM market_evolve_programs
            ORDER BY status = 'active' DESC, score DESC, generation DESC,
                     COALESCE(created_at, valid_from, '') DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall(),
        [],
    )
    return [_market_evolve_program_payload(row) for row in rows]


def _fetch_market_evolve_evaluations(
    conn: sqlite3.Connection,
    *,
    cycle_id: str,
    limit: int,
) -> list[dict[str, Any]]:
    if not _table_exists(conn, "market_evolve_evaluations"):
        return []
    rows = _safe(
        lambda: conn.execute(
            """
            SELECT id, program_id, cycle_id, evaluator_version, score,
                   metrics_json, baseline_metrics_json, passed, rationale,
                   quality_flags, evaluated_at, valid_from
            FROM market_evolve_evaluations
            WHERE cycle_id = ?
            ORDER BY score DESC, COALESCE(evaluated_at, valid_from, '') DESC
            LIMIT ?
            """,
            (cycle_id, int(limit)),
        ).fetchall(),
        [],
    )
    return [_market_evolve_evaluation_payload(row) for row in rows]


def _fetch_market_evolve_mutations(
    conn: sqlite3.Connection,
    *,
    cycle_id: str,
    limit: int,
) -> list[dict[str, Any]]:
    if not _table_exists(conn, "market_evolve_mutations"):
        return []
    rows = _safe(
        lambda: conn.execute(
            """
            SELECT id, parent_program_id, child_program_id, cycle_id,
                   mutation_kind, mutation_json, rationale, status,
                   created_at, valid_from
            FROM market_evolve_mutations
            WHERE cycle_id = ?
            ORDER BY COALESCE(created_at, valid_from, '') DESC
            LIMIT ?
            """,
            (cycle_id, int(limit)),
        ).fetchall(),
        [],
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        d = dict(row)
        d["mutation"] = _json_value(d.pop("mutation_json", None), {})
        out.append(d)
    return out


def _fetch_market_evolve_experiments(
    conn: sqlite3.Connection,
    *,
    cycle_id: str,
    limit: int,
) -> list[dict[str, Any]]:
    if not _table_exists(conn, "market_evolve_experiments"):
        return []
    rows = _safe(
        lambda: conn.execute(
            """
            SELECT id, cycle_id, parent_program_id, candidate_program_id,
                   experiment_kind, matched_slice_json, arms_json,
                   success_criteria_json, status, rationale, quality_flags,
                   created_at, valid_from
            FROM market_evolve_experiments
            WHERE cycle_id = ?
            ORDER BY COALESCE(created_at, valid_from, '') DESC
            LIMIT ?
            """,
            (cycle_id, int(limit)),
        ).fetchall(),
        [],
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        d = dict(row)
        d["matched_slice"] = _json_value(d.pop("matched_slice_json", None), {})
        d["arms"] = _json_value(d.pop("arms_json", None), [])
        d["success_criteria"] = _json_value(d.pop("success_criteria_json", None), {})
        d["quality_flags"] = _json_value(d.get("quality_flags"), [])
        out.append(d)
    return out


def _fetch_market_evolve_experiment_results(
    conn: sqlite3.Connection,
    *,
    cycle_id: str,
    limit: int,
) -> list[dict[str, Any]]:
    if not _table_exists(conn, "market_evolve_experiment_results"):
        return []
    rows = _safe(
        lambda: conn.execute(
            """
            SELECT id, experiment_id, cycle_id, parent_program_id,
                   candidate_program_id, control_metrics_json,
                   candidate_metrics_json, control_score, candidate_score,
                   score_delta, decision, rationale, quality_flags,
                   created_at, valid_from
            FROM market_evolve_experiment_results
            WHERE cycle_id = ?
            ORDER BY COALESCE(created_at, valid_from, '') DESC
            LIMIT ?
            """,
            (cycle_id, int(limit)),
        ).fetchall(),
        [],
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        d = dict(row)
        d["control_metrics"] = _json_value(d.pop("control_metrics_json", None), {})
        d["candidate_metrics"] = _json_value(d.pop("candidate_metrics_json", None), {})
        d["control_score"] = _float(d.get("control_score"), 0.0)
        d["candidate_score"] = _float(d.get("candidate_score"), 0.0)
        d["score_delta"] = _float(d.get("score_delta"), 0.0)
        d["quality_flags"] = _json_value(d.get("quality_flags"), [])
        out.append(d)
    return out


def _market_evolve_program_payload(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["generation"] = int(d.get("generation") or 0)
    d["score"] = _float(d.get("score"), 0.0)
    d["parent_program_ids"] = _json_value(d.pop("parent_program_ids_json", None), [])
    d["genome"] = _json_value(d.pop("genome_json", None), {})
    d["objective"] = _json_value(d.pop("objective_json", None), {})
    d["metrics"] = _json_value(d.pop("metrics_json", None), {})
    d["quality_flags"] = _json_value(d.get("quality_flags"), [])
    return d


def _market_evolve_evaluation_payload(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["score"] = _float(d.get("score"), 0.0)
    d["passed"] = bool(d.get("passed"))
    d["metrics"] = _json_value(d.pop("metrics_json", None), {})
    d["baseline_metrics"] = _json_value(d.pop("baseline_metrics_json", None), {})
    d["quality_flags"] = _json_value(d.get("quality_flags"), [])
    return d


def _fetch_scout_rows(
    conn: sqlite3.Connection,
    *,
    cycle_id: str,
    strings: list[dict[str, Any]],
    events: list[dict[str, Any]],
    node_intel: list[dict[str, Any]],
    tool_proposals: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    if not _table_exists(conn, "hypotheses"):
        return []
    string_by_id = {str(s.get("id")): s for s in strings if s.get("id")}
    event_by_id = {str(e.get("id")): e for e in events if e.get("id")}
    node_by_id = {str(n.get("id")): n for n in node_intel if n.get("id")}
    tool_proposal_by_id = {str(p.get("id")): p for p in tool_proposals if p.get("id")}
    rows = _safe(
        lambda: conn.execute(
            """
            SELECT id, cycle_id, title, hypothesis_text, posterior_prob,
                   heat_score, novelty_score, valid_from, transaction_from,
                   payload
            FROM hypotheses
            WHERE cycle_id = ?
              AND specialist_id = 'tier1_scout'
            ORDER BY COALESCE(posterior_prob, heat_score, 0) DESC,
                     COALESCE(valid_from, transaction_from, '') DESC
            LIMIT ?
            """,
            (cycle_id, int(limit)),
        ).fetchall(),
        [],
    )
    out: list[dict[str, Any]] = []
    parsed_rows: list[dict[str, Any]] = []
    missing_string_ids: set[str] = set()
    missing_event_ids: set[str] = set()
    missing_node_ids: set[str] = set()
    missing_proposal_ids: set[str] = set()
    for row in rows:
        payload = _json_value(row["payload"], {})
        info_ids = [str(x) for x in (payload.get("information_string_ids") or [])]
        event_ids = [str(x) for x in (payload.get("event_intelligence_ids") or [])]
        node_ids = [str(x) for x in (payload.get("node_intelligence_ids") or [])]
        proposal_ids = [str(x) for x in (payload.get("tool_proposal_ids") or [])]
        flags = [str(x) for x in (payload.get("quality_flags") or [])]
        prompt_variant = str(payload.get("prompt_variant") or _variant_from_flags(flags) or "")
        missing_string_ids.update(sid for sid in info_ids if sid not in string_by_id)
        missing_event_ids.update(eid for eid in event_ids if eid not in event_by_id)
        missing_node_ids.update(nid for nid in node_ids if nid not in node_by_id)
        missing_proposal_ids.update(pid for pid in proposal_ids if pid not in tool_proposal_by_id)
        parsed_rows.append({
            "row": row,
            "payload": payload,
            "info_ids": info_ids,
            "event_ids": event_ids,
            "node_ids": node_ids,
            "proposal_ids": proposal_ids,
            "flags": flags,
            "prompt_variant": prompt_variant,
        })
    if missing_string_ids:
        string_by_id.update({
            str(s.get("id")): s
            for s in _fetch_information_strings_by_ids(conn, sorted(missing_string_ids))
            if s.get("id")
        })
    if missing_event_ids:
        event_by_id.update({
            str(e.get("id")): e
            for e in _fetch_event_intelligence_by_ids(conn, sorted(missing_event_ids))
            if e.get("id")
        })
    if missing_node_ids:
        node_by_id.update({
            str(n.get("id")): n
            for n in _fetch_node_intelligence_by_ids(conn, sorted(missing_node_ids))
            if n.get("id")
        })
    if missing_proposal_ids:
        tool_proposal_by_id.update({
            str(p.get("id")): p
            for p in _fetch_analysis_tool_proposals_by_ids(conn, sorted(missing_proposal_ids))
            if p.get("id")
        })
    for parsed in parsed_rows:
        row = parsed["row"]
        payload = parsed["payload"]
        info_ids = parsed["info_ids"]
        event_ids = parsed["event_ids"]
        node_ids = parsed["node_ids"]
        proposal_ids = parsed["proposal_ids"]
        flags = parsed["flags"]
        prompt_variant = parsed["prompt_variant"]
        attached_strings = [string_by_id[sid] for sid in info_ids if sid in string_by_id]
        attached_events = [event_by_id[eid] for eid in event_ids if eid in event_by_id]
        attached_node_intel = [node_by_id[nid] for nid in node_ids if nid in node_by_id]
        attached_tool_proposals = [
            tool_proposal_by_id[pid] for pid in proposal_ids if pid in tool_proposal_by_id
        ]
        out.append({
            "hypothesis_id": row["id"],
            "scout_id": payload.get("scout_id"),
            "task_id": payload.get("task_id"),
            "seed_id": payload.get("seed_id"),
            "entity": payload.get("entity"),
            "horizon": payload.get("horizon"),
            "lens": payload.get("lens"),
            "bias_mode": payload.get("bias_mode"),
            "prompt_variant": prompt_variant,
            "model_used": payload.get("model_used"),
            "provider": payload.get("provider"),
            "confidence": _float(row["posterior_prob"], 0.0),
            "novelty_score": _float(row["novelty_score"], 0.0),
            "hypothesis": row["hypothesis_text"] or row["title"],
            "rationale_brief": payload.get("rationale_brief"),
            "suggested_tools": payload.get("suggested_tools") or [],
            "tool_evidence": payload.get("tool_evidence") or [],
            "information_string_ids": info_ids,
            "event_intelligence_ids": event_ids,
            "event_intelligence": attached_events,
            "node_intelligence_ids": node_ids,
            "node_intelligence": attached_node_intel,
            "tool_proposal_ids": proposal_ids,
            "tool_proposals": attached_tool_proposals,
            "information_strings": attached_strings,
            "quality_flags": flags,
            "valid_from": row["valid_from"] or row["transaction_from"],
        })
    return out


def _fetch_information_strings(
    conn: sqlite3.Connection,
    *,
    cycle_id: str,
    limit: int,
) -> list[dict[str, Any]]:
    if not _table_exists(conn, "information_strings"):
        return []
    rows = _safe(
        lambda: conn.execute(
            """
            SELECT id, cycle_id, coverage_cell_key, scout_id, seed_id, entity,
                   theme, horizon, lens, bias_mode, title, thesis, mechanism,
                   expected_outcome, time_horizon, time_scale,
                   event_time_start, event_time_end, observed_at, ingested_at,
                   source_time_basis, kill_signal,
                   extends_or_contradicts, would_change_decision, expires_at,
                   crowdedness, conviction, novelty_score, attention_score,
                   entities_chain, depth_layers, evidence_refs,
                   prior_thread_refs, source_tool_call_ids, model_used,
                   provider, cost_usd, quality_flags, rollup_parent_ids,
                   lower_timeframe_refs, higher_timeframe_context_refs,
                   temporal_confidence, valid_from
            FROM information_strings
            WHERE cycle_id = ?
            ORDER BY attention_score DESC, conviction DESC, novelty_score DESC
            LIMIT ?
            """,
            (cycle_id, int(limit)),
        ).fetchall(),
        [],
    )
    return [_information_string_payload(row) for row in rows]


def _information_string_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        **dict(row),
        "would_change_decision": bool(row["would_change_decision"]),
        "crowdedness": _float(row["crowdedness"], 0.5),
        "conviction": _float(row["conviction"], 0.0),
        "novelty_score": _float(row["novelty_score"], 0.0),
        "attention_score": _float(row["attention_score"], 0.0),
        "cost_usd": _float(row["cost_usd"], 0.0),
        "entities_chain": _json_value(row["entities_chain"], []),
        "depth_layers": _json_value(row["depth_layers"], []),
        "evidence_refs": _json_value(row["evidence_refs"], []),
        "prior_thread_refs": _json_value(row["prior_thread_refs"], []),
        "source_tool_call_ids": _json_value(row["source_tool_call_ids"], []),
        "rollup_parent_ids": _json_value(row["rollup_parent_ids"], []),
        "lower_timeframe_refs": _json_value(row["lower_timeframe_refs"], []),
        "higher_timeframe_context_refs": _json_value(row["higher_timeframe_context_refs"], []),
        "temporal_confidence": _float(row["temporal_confidence"], 0.5),
        "quality_flags": _json_value(row["quality_flags"], []),
    }


def _fetch_information_strings_by_ids(
    conn: sqlite3.Connection,
    ids: list[str],
) -> list[dict[str, Any]]:
    ids = [str(x) for x in ids if str(x).strip()]
    if not ids or not _table_exists(conn, "information_strings"):
        return []
    placeholders = ",".join("?" * len(ids))
    rows = _safe(
        lambda: conn.execute(
            f"""
            SELECT id, cycle_id, coverage_cell_key, scout_id, seed_id, entity,
                   theme, horizon, lens, bias_mode, title, thesis, mechanism,
                   expected_outcome, time_horizon, time_scale,
                   event_time_start, event_time_end, observed_at, ingested_at,
                   source_time_basis, kill_signal,
                   extends_or_contradicts, would_change_decision, expires_at,
                   crowdedness, conviction, novelty_score, attention_score,
                   entities_chain, depth_layers, evidence_refs,
                   prior_thread_refs, source_tool_call_ids, model_used,
                   provider, cost_usd, quality_flags, rollup_parent_ids,
                   lower_timeframe_refs, higher_timeframe_context_refs,
                   temporal_confidence, valid_from
            FROM information_strings
            WHERE id IN ({placeholders})
            """,
            tuple(ids),
        ).fetchall(),
        [],
    )
    return [_information_string_payload(row) for row in rows]


def _fetch_event_intelligence(
    conn: sqlite3.Connection,
    *,
    cycle_id: str,
    limit: int,
) -> list[dict[str, Any]]:
    if not _table_exists(conn, "market_event_intelligence"):
        return []
    rows = _safe(
        lambda: conn.execute(
            """
            SELECT id, cycle_id, event_type, entity, asset, protocol,
                   event_time, source_time_basis, actor_label, actor_address,
                   actor_cluster_id, actor_type, amount, amount_unit,
                   notional_usd, severity_score, intelligence_score,
                   directional_bias, summary, base_case, bull_case, bear_case,
                   kill_signal, source_refs_json, quality_flags, raw_event_json,
                   created_at, valid_from
            FROM market_event_intelligence
            WHERE cycle_id = ?
            ORDER BY intelligence_score DESC, severity_score DESC,
                     COALESCE(event_time, valid_from, '') DESC
            LIMIT ?
            """,
            (cycle_id, int(limit)),
        ).fetchall(),
        [],
    )
    ids = [str(row["id"]) for row in rows]
    data_points = _fetch_event_data_points(conn, ids)
    triggers = _fetch_event_watch_triggers(conn, ids)
    out: list[dict[str, Any]] = []
    for row in rows:
        bundle_id = str(row["id"])
        out.append(_event_intelligence_payload(row, data_points, triggers))
    return out


def _fetch_event_intelligence_by_ids(
    conn: sqlite3.Connection,
    ids: list[str],
) -> list[dict[str, Any]]:
    ids = [str(x) for x in ids if str(x).strip()]
    if not ids or not _table_exists(conn, "market_event_intelligence"):
        return []
    placeholders = ",".join("?" * len(ids))
    rows = _safe(
        lambda: conn.execute(
            f"""
            SELECT id, cycle_id, event_type, entity, asset, protocol,
                   event_time, source_time_basis, actor_label, actor_address,
                   actor_cluster_id, actor_type, amount, amount_unit,
                   notional_usd, severity_score, intelligence_score,
                   directional_bias, summary, base_case, bull_case, bear_case,
                   kill_signal, source_refs_json, quality_flags, raw_event_json,
                   created_at, valid_from
            FROM market_event_intelligence
            WHERE id IN ({placeholders})
            """,
            tuple(ids),
        ).fetchall(),
        [],
    )
    row_ids = [str(row["id"]) for row in rows]
    data_points = _fetch_event_data_points(conn, row_ids)
    triggers = _fetch_event_watch_triggers(conn, row_ids)
    return [_event_intelligence_payload(row, data_points, triggers) for row in rows]


def _event_intelligence_payload(
    row: sqlite3.Row,
    data_points: dict[str, list[dict[str, Any]]],
    triggers: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    bundle_id = str(row["id"])
    return {
        **dict(row),
        "amount": _float(row["amount"], None),
        "notional_usd": _float(row["notional_usd"], None),
        "severity_score": _float(row["severity_score"], 0.0),
        "intelligence_score": _float(row["intelligence_score"], 0.0),
        "source_refs": _json_value(row["source_refs_json"], []),
        "quality_flags": _json_value(row["quality_flags"], []),
        "raw_event": _json_value(row["raw_event_json"], {}),
        "data_points": data_points.get(bundle_id, []),
        "watch_triggers": triggers.get(bundle_id, []),
    }


def _fetch_event_data_points(
    conn: sqlite3.Connection,
    bundle_ids: list[str],
) -> dict[str, list[dict[str, Any]]]:
    if not bundle_ids or not _table_exists(conn, "market_event_data_points"):
        return {}
    placeholders = ",".join("?" * len(bundle_ids))
    rows = _safe(
        lambda: conn.execute(
            f"""
            SELECT id, bundle_id, category, label, value_text, numeric_value,
                   unit, source_ref, confidence, observed_at, payload_json
            FROM market_event_data_points
            WHERE bundle_id IN ({placeholders})
            ORDER BY category, label
            """,
            tuple(bundle_ids),
        ).fetchall(),
        [],
    )
    out: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        out.setdefault(str(row["bundle_id"]), []).append({
            **dict(row),
            "numeric_value": _float(row["numeric_value"], None),
            "confidence": _float(row["confidence"], 0.5),
            "payload": _json_value(row["payload_json"], {}),
        })
    return out


def _fetch_event_watch_triggers(
    conn: sqlite3.Connection,
    bundle_ids: list[str],
) -> dict[str, list[dict[str, Any]]]:
    if not bundle_ids or not _table_exists(conn, "market_event_watch_triggers"):
        return {}
    placeholders = ",".join("?" * len(bundle_ids))
    rows = _safe(
        lambda: conn.execute(
            f"""
            SELECT id, bundle_id, trigger_kind, description, horizon,
                   direction, severity, source_refs_json, status
            FROM market_event_watch_triggers
            WHERE bundle_id IN ({placeholders})
            ORDER BY severity DESC, trigger_kind
            """,
            tuple(bundle_ids),
        ).fetchall(),
        [],
    )
    out: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        out.setdefault(str(row["bundle_id"]), []).append({
            **dict(row),
            "source_refs": _json_value(row["source_refs_json"], []),
        })
    return out


def _fetch_node_intelligence(
    conn: sqlite3.Connection,
    *,
    cycle_id: str,
    limit: int,
) -> list[dict[str, Any]]:
    if not _table_exists(conn, "node_intelligence_snapshots"):
        return []
    rows = _safe(
        lambda: conn.execute(
            """
            SELECT id, cycle_id, entity, chain, protocol, as_of, summary,
                   edge_summary, node_score, source_refs_json,
                   source_families_json, coverage_json, actor_summaries_json,
                   quality_flags, raw_payload_json, created_at, valid_from
            FROM node_intelligence_snapshots
            WHERE cycle_id = ?
            ORDER BY node_score DESC, as_of DESC
            LIMIT ?
            """,
            (cycle_id, int(limit)),
        ).fetchall(),
        [],
    )
    ids = [str(row["id"]) for row in rows]
    observations = _fetch_node_observations(conn, ids)
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(_node_intelligence_payload(row, observations))
    return out


def _fetch_node_intelligence_by_ids(
    conn: sqlite3.Connection,
    ids: list[str],
) -> list[dict[str, Any]]:
    ids = [str(x) for x in ids if str(x).strip()]
    if not ids or not _table_exists(conn, "node_intelligence_snapshots"):
        return []
    placeholders = ",".join("?" * len(ids))
    rows = _safe(
        lambda: conn.execute(
            f"""
            SELECT id, cycle_id, entity, chain, protocol, as_of, summary,
                   edge_summary, node_score, source_refs_json,
                   source_families_json, coverage_json, actor_summaries_json,
                   quality_flags, raw_payload_json, created_at, valid_from
            FROM node_intelligence_snapshots
            WHERE id IN ({placeholders})
            """,
            tuple(ids),
        ).fetchall(),
        [],
    )
    row_ids = [str(row["id"]) for row in rows]
    observations = _fetch_node_observations(conn, row_ids)
    return [_node_intelligence_payload(row, observations) for row in rows]


def _node_intelligence_payload(
    row: sqlite3.Row,
    observations: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    snapshot_id = str(row["id"])
    return {
        **dict(row),
        "node_score": _float(row["node_score"], 0.0),
        "source_refs": _json_value(row["source_refs_json"], []),
        "source_families": _json_value(row["source_families_json"], []),
        "coverage": _json_value(row["coverage_json"], {}),
        "actors": _json_value(row["actor_summaries_json"], []),
        "quality_flags": _json_value(row["quality_flags"], []),
        "raw_payload": _json_value(row["raw_payload_json"], {}),
        "observations": observations.get(snapshot_id, []),
    }


def _fetch_node_observations(
    conn: sqlite3.Connection,
    snapshot_ids: list[str],
) -> dict[str, list[dict[str, Any]]]:
    if not snapshot_ids or not _table_exists(conn, "node_intelligence_observations"):
        return {}
    placeholders = ",".join("?" * len(snapshot_ids))
    rows = _safe(
        lambda: conn.execute(
            f"""
            SELECT id, snapshot_id, category, label, actor, value_text,
                   numeric_value, unit, source_ref, source_family, confidence,
                   observed_at, payload_json
            FROM node_intelligence_observations
            WHERE snapshot_id IN ({placeholders})
            ORDER BY category, confidence DESC
            """,
            tuple(snapshot_ids),
        ).fetchall(),
        [],
    )
    out: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        out.setdefault(str(row["snapshot_id"]), []).append({
            **dict(row),
            "numeric_value": _float(row["numeric_value"], None),
            "confidence": _float(row["confidence"], 0.5),
            "payload": _json_value(row["payload_json"], {}),
        })
    return out


def _fetch_analysis_tool_proposals(
    conn: sqlite3.Connection,
    *,
    cycle_id: str,
    limit: int,
) -> list[dict[str, Any]]:
    if not _table_exists(conn, "analysis_tool_proposals"):
        return []
    rows = _safe(
        lambda: conn.execute(
            """
            SELECT id, cycle_id, artifact_kind, artifact_id, entity, horizon,
                   lens, proposal_kind, tool_name, purpose, source_family,
                   trigger, input_shape_json, promotion_gate_json,
                   eval_plan_json, priority, status, parent_proposal_id,
                   iteration, created_by, quality_flags, created_at, valid_from
            FROM analysis_tool_proposals
            WHERE cycle_id = ?
            ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
                     iteration DESC, created_at DESC
            LIMIT ?
            """,
            (cycle_id, int(limit)),
        ).fetchall(),
        [],
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(_analysis_tool_proposal_payload(row))
    return out


def _fetch_analysis_tool_proposals_by_ids(
    conn: sqlite3.Connection,
    ids: list[str],
) -> list[dict[str, Any]]:
    ids = [str(x) for x in ids if str(x).strip()]
    if not ids or not _table_exists(conn, "analysis_tool_proposals"):
        return []
    placeholders = ",".join("?" * len(ids))
    rows = _safe(
        lambda: conn.execute(
            f"""
            SELECT id, cycle_id, artifact_kind, artifact_id, entity, horizon,
                   lens, proposal_kind, tool_name, purpose, source_family,
                   trigger, input_shape_json, promotion_gate_json,
                   eval_plan_json, priority, status, parent_proposal_id,
                   iteration, created_by, quality_flags, created_at, valid_from
            FROM analysis_tool_proposals
            WHERE id IN ({placeholders})
            """,
            tuple(ids),
        ).fetchall(),
        [],
    )
    return [_analysis_tool_proposal_payload(row) for row in rows]


def _analysis_tool_proposal_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        **dict(row),
        "input_shape": _json_value(row["input_shape_json"], {}),
        "promotion_gate": _json_value(row["promotion_gate_json"], {}),
        "eval_plan": _json_value(row["eval_plan_json"], {}),
        "quality_flags": _json_value(row["quality_flags"], []),
    }


def _scout_summary(
    scouts: list[dict[str, Any]],
    strings: list[dict[str, Any]],
    events: list[dict[str, Any]],
    node_intel: list[dict[str, Any]],
) -> dict[str, Any]:
    variants: dict[str, int] = {}
    by_lens: dict[str, int] = {}
    by_entity: dict[str, int] = {}
    confidences: list[float] = []
    for scout in scouts:
        variant = str(scout.get("prompt_variant") or "unknown")
        lens = str(scout.get("lens") or "unknown")
        entity = str(scout.get("entity") or "unknown")
        variants[variant] = variants.get(variant, 0) + 1
        by_lens[lens] = by_lens.get(lens, 0) + 1
        by_entity[entity] = by_entity.get(entity, 0) + 1
        confidences.append(float(scout.get("confidence") or 0.0))
    avg_conf = sum(confidences) / len(confidences) if confidences else None
    return {
        **_empty_scout_summary(),
        "scouts": len(scouts),
        "strings": len(strings),
        "event_intelligence": len(events),
        "node_intelligence": len(node_intel),
        "avg_confidence": round(avg_conf, 4) if avg_conf is not None else None,
        "avg_attention": _avg(strings, "attention_score"),
        "avg_conviction": _avg(strings, "conviction"),
        "prompt_variants": variants,
        "by_lens": _sorted_counts(by_lens),
        "by_entity": _sorted_counts(by_entity),
    }


def _empty_scout_summary() -> dict[str, Any]:
    return {
        "scouts": 0,
        "strings": 0,
        "event_intelligence": 0,
        "node_intelligence": 0,
        "avg_confidence": None,
        "avg_attention": None,
        "avg_conviction": None,
        "prompt_variants": {},
        "by_lens": {},
        "by_entity": {},
    }


def _sorted_counts(counts: dict[str, int]) -> dict[str, int]:
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:16])


def _variant_from_flags(flags: list[str]) -> Optional[str]:
    for flag in flags:
        if flag.startswith("prompt_variant:"):
            return flag.split(":", 1)[1]
    return None


def _avg(rows: list[dict[str, Any]], key: str) -> Optional[float]:
    vals = [float(r.get(key) or 0.0) for r in rows if r.get(key) is not None]
    if not vals:
        return None
    return round(sum(vals) / len(vals), 4)


def _float(raw: Any, default: float) -> float:
    try:
        return float(raw)
    except Exception:
        return default


# ---------------------------------------------------------------------------
# Top-level state
# ---------------------------------------------------------------------------

def get_state() -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    db_path = _find_latest_desk_db()
    manifest_path = _find_latest_manifest(db_path)

    manifest: dict = {}
    if manifest_path:
        try:
            manifest = json.loads(manifest_path.read_text())
        except Exception:
            manifest = {}

    brief_path = _find_latest_brief(manifest)
    artifact_coherence = _compute_artifact_coherence(
        db_path, manifest_path, manifest, brief_path,
    )
    cycle_id = artifact_coherence.get("cycle_id")

    brief_md = None
    if brief_path:
        try:
            brief_md = brief_path.read_text()
        except Exception:
            brief_md = None

    if not db_path:
        return {
            "status": "no_active_run",
            "as_of": now,
            "desk_db": None,
            "manifest_path": str(manifest_path) if manifest_path else None,
            "brief_path": str(brief_path) if brief_path else None,
            "manifest": manifest,
            "cycle_id": cycle_id,
            "artifact_coherence": artifact_coherence,
            "panels": {
                "hero": {
                    "reports_today": 0, "active_specialists": 0,
                    "total_specialists": len(KNOWN_SPECIALISTS),
                    "errors_today": 0, "cycle_cost_usd": 0.0,
                    "daily_cap_usd": DAILY_COST_CAP, "avg_grade": None,
                },
                "specialists": [
                    {"specialist_id": s, "persona_version": "—", "stage": "HYDRATE",
                     "hypotheses": 0, "hypotheses_target": 12,
                     "tool_calls_ok": 0, "tool_calls_err": 0,
                     "tool_success_pct": None, "last_at": None,
                     "ago_seconds": None, "status": "idle", "cycle_id": None}
                    for s in KNOWN_SPECIALISTS
                ],
                "top_reports": [],
                "calendar": {"date": _today_utc(), "catalysts": [],
                              "severity": "low", "headline_note": None},
                "cost": {"today_usd": 0.0, "cap_usd": DAILY_COST_CAP,
                          "pct_used": 0.0, "by_stage": {}, "sparkline_24h": [0.0] * 24},
                "vehicle_flow": {"vehicles": {"BHYP": None, "THYP": None, "PURR": None},
                                  "multiple_vs_baseline": None, "status": "no_data",
                                  "sparkline_7d": []},
                "sources": {"recent": [], "unique_books_cited": 0, "top_author": None},
                "information_rrg": {
                    "points": [],
                    "x_label": "relative information strength",
                    "y_label": "information momentum",
                    "note": "waiting for desk data",
                },
                "scouts": _empty_scout_summary(),
                "research_evolution": _empty_research_evolution_summary(),
                "brier": [
                    {"specialist_id": s, "brier": None, "cycles": 0, "calibrating": True}
                    for s in KNOWN_SPECIALISTS
                ],
            },
        }

    conn = _open_ro(db_path)
    try:
        panels = {
            "hero": _panel_hero(conn, manifest),
            "specialists": _panel_specialists(conn),
            "top_reports": _panel_top_reports(conn),
            "calendar": _panel_calendar(conn, brief_md),
            "cost": _panel_cost(conn),
            "vehicle_flow": _panel_vehicle_flow(conn),
            "sources": _panel_sources(conn),
            "information_rrg": _panel_information_rrg(conn),
            "scouts": _panel_scouts_summary(conn, manifest),
            "research_evolution": _panel_research_evolution(conn, manifest),
            "brier": _panel_brier(conn),
        }
    finally:
        conn.close()

    return {
        "status": "ok",
        "as_of": now,
        "desk_db": str(db_path),
        "manifest_path": str(manifest_path) if manifest_path else None,
        "brief_path": str(brief_path) if brief_path else None,
        "manifest": manifest,
        "cycle_id": cycle_id,
        "artifact_coherence": artifact_coherence,
        "panels": panels,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
async def home() -> FileResponse:
    return FileResponse(str(INDEX_HTML), media_type="text/html")


@app.get("/scouts")
async def scouts_page() -> FileResponse:
    return FileResponse(str(SCOUT_HTML), media_type="text/html")


@app.get("/api/state")
async def api_state() -> JSONResponse:
    return JSONResponse(get_state())


@app.get("/api/scouts")
async def api_scouts(cycle_id: Optional[str] = None, limit: int = 80) -> JSONResponse:
    db_path = _find_latest_desk_db()
    if db_path is None:
        return JSONResponse({
            "status": "no_active_run",
            "cycle_id": None,
            "desk_db": None,
            "summary": _empty_scout_summary(),
            "scouts": [],
            "strings": [],
        })
    manifest_path = _find_latest_manifest(db_path)
    manifest: dict = {}
    if manifest_path:
        manifest = _json_value(manifest_path.read_text(), {})
    conn = _open_ro(db_path)
    try:
        payload = _build_scout_inspector_payload(
            conn,
            manifest=manifest,
            cycle_id=cycle_id,
            limit=max(1, min(int(limit), 250)),
        )
    finally:
        conn.close()
    payload["desk_db"] = str(db_path)
    payload["manifest_path"] = str(manifest_path) if manifest_path else None
    return JSONResponse(payload)


@app.get("/api/evolution")
async def api_evolution(cycle_id: Optional[str] = None, limit: int = 80) -> JSONResponse:
    db_path = _find_latest_desk_db()
    if db_path is None:
        return JSONResponse({
            "status": "no_active_run",
            "cycle_id": None,
            "desk_db": None,
            "summary": _empty_research_evolution_summary(),
            "alpha_geometry": [],
            "market_evolve": _empty_market_evolve_payload(),
            "evolution_control": _empty_evolution_control_payload(cycle_id=None, reason="no_active_run"),
        })
    manifest_path = _find_latest_manifest(db_path)
    manifest: dict = {}
    if manifest_path:
        manifest = _json_value(manifest_path.read_text(), {})
    conn = _open_ro(db_path)
    try:
        payload = _build_research_evolution_payload(
            conn,
            manifest=manifest,
            cycle_id=cycle_id,
            limit=max(1, min(int(limit), 250)),
        )
    finally:
        conn.close()
    payload["desk_db"] = str(db_path)
    payload["manifest_path"] = str(manifest_path) if manifest_path else None
    return JSONResponse(payload)


@app.get("/api/sse")
async def api_sse() -> StreamingResponse:
    async def gen():
        while True:
            payload = json.dumps(get_state())
            yield f"data: {payload}\n\n"
            await asyncio.sleep(3.0)
    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/healthz")
async def healthz() -> PlainTextResponse:
    return PlainTextResponse("ok")


# ---------------------------------------------------------------------------
# Pipeline Inspector — Layer 2 of the visualizer (Wave 3.5)
# ---------------------------------------------------------------------------

@app.get("/api/inspector/{report_id}")
async def api_inspector_report(report_id: str) -> JSONResponse:
    """Per-report forensic drill-down.

    Returns the full per-stage breakdown for `report_id`:
      dossier -> comparables -> researcher draft -> risk critic ->
      methodology critic -> revisions[] -> polish -> grade
    plus the tool_call_log entries cited + the citation chain. Empty
    stages are returned with `available=False` so the UI can show them
    greyed out.
    """
    db_path = _find_latest_desk_db()
    if db_path is None:
        return JSONResponse({"error": "no_desk_db"}, status_code=404)
    conn = _open_ro(db_path)
    out: dict[str, Any] = {"report_id": report_id}
    # 1. The research_reports row itself.
    row = _safe(
        lambda: conn.execute(
            "SELECT * FROM research_reports WHERE id = ? "
            "AND transaction_to IS NULL LIMIT 1",
            (report_id,),
        ).fetchone(),
        None,
    )
    if row is None:
        return JSONResponse({"error": "report_not_found", "report_id": report_id}, status_code=404)
    d = dict(row)
    # Parse JSON fields
    try:
        d["citation_claim_ids"] = json.loads(d.get("citation_claim_ids") or "[]")
    except Exception:
        d["citation_claim_ids"] = []
    try:
        d["citation_tool_call_ids"] = json.loads(d.get("citation_tool_call_ids") or "[]")
    except Exception:
        d["citation_tool_call_ids"] = []
    try:
        d["reviewer_turns"] = json.loads(d.get("reviewer_turns") or "[]")
    except Exception:
        d["reviewer_turns"] = []
    try:
        d["quality_flags"] = json.loads(d.get("quality_flags") or "[]")
    except Exception:
        d["quality_flags"] = []
    out["report"] = d

    # 2. Reviewer turns -> stage view (dossier, comparables, researcher,
    # risk_critic, methodology_critic, revision_N, polish, grade).
    stages: dict[str, Any] = {}
    for t in d["reviewer_turns"]:
        if not isinstance(t, dict):
            continue
        stage_name = (t.get("stage") or t.get("name") or "").lower()
        if not stage_name:
            continue
        stages[stage_name] = {
            "available": True,
            "model": t.get("model_used") or t.get("model"),
            "cost_usd": t.get("cost_usd"),
            "duration_s": t.get("elapsed_s") or t.get("duration_s"),
            "input": (t.get("input") or t.get("user") or "")[:4000],
            "output": (t.get("output") or t.get("text") or "")[:4000],
            "quality_flags": t.get("quality_flags") or [],
        }
    # Fill in any missing canonical stages with available=False
    canonical_stages = [
        "dossier", "comparables", "researcher", "risk_critic",
        "methodology_critic", "revision_1", "revision_2",
        "polish", "grade",
    ]
    for s in canonical_stages:
        stages.setdefault(s, {"available": False})
    out["stages"] = stages

    # 3. Cited tool calls.
    tool_calls: list[dict] = []
    for tc_id in d["citation_tool_call_ids"]:
        try:
            row = conn.execute(
                "SELECT id, tool_uri, args_hash, started_at, finished_at, "
                "       duration_ms, cost_usd, source_ids, error "
                "FROM tool_call_log WHERE id = ? "
                "AND transaction_to IS NULL LIMIT 1",
                (tc_id,),
            ).fetchone()
        except sqlite3.OperationalError:
            row = None
        if row is None:
            continue
        td = dict(row)
        tool_calls.append(td)
    out["tool_calls"] = tool_calls

    # 4. Citations linked to this report (via tool_calls or claim_ids).
    citations: list[dict] = []
    for cid in d["citation_claim_ids"]:
        try:
            row = conn.execute(
                "SELECT id, canonical_url, content_hash, anchor, "
                "       quote_excerpt, fetched_at, still_valid, "
                "       hash_changed, quality_flags "
                "FROM citations WHERE id = ? "
                "AND transaction_to IS NULL LIMIT 1",
                (cid,),
            ).fetchone()
        except sqlite3.OperationalError:
            row = None
        if row is None:
            continue
        cd = dict(row)
        try:
            cd["quality_flags"] = json.loads(cd.get("quality_flags") or "[]")
        except Exception:
            pass
        citations.append(cd)
    out["citations"] = citations

    return JSONResponse(out)


@app.get("/api/inspector/citation/{citation_id}")
async def api_inspector_citation(citation_id: str) -> JSONResponse:
    """Per-citation drill-down — full provenance row + linked artifacts."""
    db_path = _find_latest_desk_db()
    if db_path is None:
        return JSONResponse({"error": "no_desk_db"}, status_code=404)
    conn = _open_ro(db_path)
    row = _safe(
        lambda: conn.execute(
            "SELECT * FROM citations WHERE id = ? "
            "AND transaction_to IS NULL LIMIT 1",
            (citation_id,),
        ).fetchone(),
        None,
    )
    if row is None:
        return JSONResponse({"error": "citation_not_found"}, status_code=404)
    d = dict(row)
    try:
        d["quality_flags"] = json.loads(d.get("quality_flags") or "[]")
    except Exception:
        d["quality_flags"] = []
    try:
        d["payload"] = json.loads(d.get("payload") or "{}")
    except Exception:
        d["payload"] = {}
    return JSONResponse({"citation": d})


@app.get("/api/inspector/reports")
async def api_inspector_reports_index() -> JSONResponse:
    """Index of recent research_reports for the inspector list view."""
    db_path = _find_latest_desk_db()
    if db_path is None:
        return JSONResponse({"reports": []})
    conn = _open_ro(db_path)
    rows = _safe(
        lambda: conn.execute(
            "SELECT id, specialist_id, cycle_id, instrument, title, "
            "       confidence, adversarial_severity, revised_at "
            "FROM research_reports "
            "WHERE transaction_to IS NULL "
            "ORDER BY revised_at DESC LIMIT 100"
        ).fetchall(),
        [],
    )
    return JSONResponse({"reports": [dict(r) for r in rows]})


# ---------------------------------------------------------------------------
# Topology endpoint — Gap 4 (Pipeline Inspector Layer 2 UI)
# ---------------------------------------------------------------------------

@app.get("/api/topology")
async def api_topology(cycle_id: Optional[str] = None) -> JSONResponse:
    """Projected manifold for the requested (or latest) cycle.

    Returns:
      {
        "cycle_id": "...",
        "points": [{"hypothesis_id","x","y","entity","lens","is_frontier"}, ...],
        "regions": [{"region_id","centroid_x","centroid_y","density","radius",
                     "n_members","is_frontier","label"}, ...],
        "n_points": int,
        "n_regions": int,
        "projection_view": "umap_2d" | "tsne_2d" | "pca_2d"
      }
    """
    db_path = _find_latest_desk_db()
    if db_path is None:
        return JSONResponse({
            "cycle_id": None, "points": [], "regions": [],
            "n_points": 0, "n_regions": 0,
        })
    conn = _open_ro(db_path)

    # Resolve cycle: latest cycle with topology rows if not specified.
    if not cycle_id:
        row = _safe(
            lambda: conn.execute(
                "SELECT cycle_id FROM topology_density_map "
                "WHERE transaction_to IS NULL OR transaction_to = '' "
                "ORDER BY transaction_from DESC LIMIT 1"
            ).fetchone(),
            None,
        )
        if row is None:
            return JSONResponse({
                "cycle_id": None, "points": [], "regions": [],
                "n_points": 0, "n_regions": 0,
            })
        cycle_id = row["cycle_id"]

    # Prefer umap_2d, then tsne_2d, then pca_2d.
    projection_view = None
    for view in ("umap_2d", "tsne_2d", "pca_2d"):
        n = _safe(
            lambda v=view: conn.execute(
                "SELECT COUNT(*) AS n FROM topology_density_map "
                "WHERE cycle_id = ? AND projection_view = ?",
                (cycle_id, v),
            ).fetchone(),
            None,
        )
        if n and n["n"] and n["n"] > 0:
            projection_view = view
            break
    if not projection_view:
        return JSONResponse({
            "cycle_id": cycle_id, "points": [], "regions": [],
            "n_points": 0, "n_regions": 0,
            "projection_view": None,
        })

    region_rows = _safe(
        lambda: conn.execute(
            "SELECT region_id, density, centroid_x, centroid_y, radius, "
            "       n_members, is_frontier, label, member_hypothesis_ids "
            "FROM topology_density_map "
            "WHERE cycle_id = ? AND projection_view = ? "
            "ORDER BY density DESC",
            (cycle_id, projection_view),
        ).fetchall(),
        [],
    )

    regions: list[dict[str, Any]] = []
    member_to_region: dict[str, str] = {}
    for r in region_rows:
        rd = dict(r)
        regions.append({
            "region_id": rd["region_id"],
            "density": rd["density"],
            "centroid_x": rd["centroid_x"],
            "centroid_y": rd["centroid_y"],
            "radius": rd["radius"],
            "n_members": rd["n_members"],
            "is_frontier": bool(rd["is_frontier"]),
            "label": rd["label"],
        })
        try:
            members = json.loads(rd.get("member_hypothesis_ids") or "[]")
        except Exception:
            members = []
        for hid in members:
            member_to_region[str(hid)] = rd["region_id"]

    # Pull the underlying hypotheses for the cycle so the UI can render
    # individual points around each cluster centroid.
    hypothesis_rows = _safe(
        lambda: conn.execute(
            "SELECT id, entity_ids, hypothesis_text, posterior_prob "
            "FROM hypotheses "
            "WHERE cycle_id = ? AND transaction_to IS NULL "
            "LIMIT 500",
            (cycle_id,),
        ).fetchall(),
        [],
    )

    points: list[dict[str, Any]] = []
    centroid_by_region = {r["region_id"]: r for r in regions}
    for h in hypothesis_rows:
        hid = h["id"]
        rid = member_to_region.get(str(hid))
        if not rid:
            continue
        ctr = centroid_by_region.get(rid)
        if not ctr:
            continue
        try:
            entities = json.loads(h["entity_ids"] or "[]")
        except Exception:
            entities = []
        points.append({
            "hypothesis_id": hid,
            "region_id": rid,
            "x": ctr["centroid_x"],
            "y": ctr["centroid_y"],
            "entity": entities[0] if entities else None,
            "is_frontier": ctr["is_frontier"],
            "text_snippet": (h["hypothesis_text"] or "")[:140],
            "posterior": h["posterior_prob"],
        })

    return JSONResponse({
        "cycle_id": cycle_id,
        "projection_view": projection_view,
        "points": points,
        "regions": regions,
        "n_points": len(points),
        "n_regions": len(regions),
    })

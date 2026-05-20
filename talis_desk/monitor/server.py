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

INDEX_HTML = Path(__file__).parent / "index.html"

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


def _find_latest_manifest() -> Optional[Path]:
    p = _newest(glob.glob("/tmp/talis_full_desk_manifest_*.json"))
    return Path(p) if p else None


def _find_latest_brief() -> Optional[Path]:
    p = _newest(glob.glob("/tmp/talis_full_desk_brief_*.md"))
    return Path(p) if p else None


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


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


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

    # Reports >= 7.0 (we use confidence*10 as a proxy when grade isn't stored,
    # else fall back to total report count for today).
    reports_today = _safe(lambda: conn.execute(
        "SELECT COUNT(*) AS n FROM research_reports "
        "WHERE date(valid_from) = ? AND (confidence >= 0.7 OR confidence IS NULL)",
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

    # Cycle cost ($ today)
    cost_today = _safe(lambda: conn.execute(
        "SELECT COALESCE(SUM(amount_usd), 0) AS s FROM cost_ledger "
        "WHERE date_utc = ?", (today,),
    ).fetchone()["s"], 0.0)

    # Avg confidence as grade proxy
    avg_conf = _safe(lambda: conn.execute(
        "SELECT AVG(confidence) AS a FROM trade_ideas "
        "WHERE date(valid_from) = ?", (today,),
    ).fetchone()["a"], None)
    avg_grade = round(avg_conf * 10, 1) if avg_conf else None

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
               novelty_score, adversarial_severity, valid_from
        FROM research_reports
        ORDER BY confidence DESC NULLS LAST, valid_from DESC
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
            "grade": round((d.get("confidence") or 0) * 10, 1),
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
    today = _today_utc()
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
                    if any(k in low for k in ("fomc", "cpi", "ppi", "nfp", "fed minutes")):
                        sev, severity = "high", "high"
                    elif any(k in low for k in ("earnings", "8-k", "10-q", "spx options")):
                        sev = "med"
                        if severity == "low":
                            severity = "med"
                    catalysts.append({"text": text[:240], "severity": sev})
        if severity == "high":
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
        "sparkline_7d": [],  # placeholder — desk hasn't backfilled history yet
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
# Top-level state
# ---------------------------------------------------------------------------

def get_state() -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    db_path = _find_latest_desk_db()
    manifest_path = _find_latest_manifest()
    brief_path = _find_latest_brief()

    manifest = {}
    if manifest_path:
        try:
            manifest = json.loads(manifest_path.read_text())
        except Exception:
            manifest = {}

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
        "panels": panels,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
async def home() -> FileResponse:
    return FileResponse(str(INDEX_HTML), media_type="text/html")


@app.get("/api/state")
async def api_state() -> JSONResponse:
    return JSONResponse(get_state())


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

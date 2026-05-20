"""FastAPI dashboard server.

Run:
    uvicorn talis_desk.dashboard.server:app --host 127.0.0.1 --port 8765 --reload

Exposes:
  - GET  /                              full dashboard HTML
  - GET  /api/scorecard                 9 S-tier metrics (v2 §1)
  - GET  /api/trade-book                open+closed + PnL waterfall
  - GET  /api/debates                   unresolved + judged + reliability
  - GET  /api/hypothesis-graph-sample   one random root + neighbors
  - GET  /api/persona-diffs             candidate vs base + veto state
  - GET  /api/weakness-map              top 5 weaknesses with lift
  - GET  /api/tool-atlas                coverage / cost / latency / errors
  - POST /api/veto/{candidate_id}       write a `veto` agent_messages row
  - GET  /healthz                       liveness check
  - GET  /static/{path}                 CSS/JS bundle (Pico CSS is on a CDN)

Pattern matches `tic_dashboard.py` in the talis-tic sibling repo.

# Honest gaps
- No auth. Local dev only. Production needs a reverse proxy + auth
  (the reviewer flow expects a single human reading once a week).
- No rate limit. Cheap reads + the veto endpoint is local-only.
- `/api/veto` writes a `veto` `agent_messages` row but the evolution
  module is in charge of actually halting the 24h auto-promote;
  this endpoint posts the message and trusts that loop to read it.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from ..director.research_director import write_veto_message
from .render import (
    gather_debates_panel,
    gather_hypothesis_graph_sample,
    gather_persona_diffs,
    gather_scorecard,
    gather_tool_atlas,
    gather_trade_book,
    gather_weakness_map,
    render_dashboard_html,
)


app = FastAPI(
    title="Talis Desk — Eval Dashboard",
    docs_url=None,
    redoc_url=None,
)


_STATIC_DIR = Path(__file__).parent / "static"
if _STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


def _parse_as_of(as_of: Optional[str]) -> Optional[datetime]:
    if not as_of:
        return None
    try:
        return datetime.fromisoformat(as_of)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=400, detail=f"as_of must be ISO-8601, got {as_of!r}: {e}"
        )


# ============================================================================
# Pages
# ============================================================================

@app.get("/", response_class=HTMLResponse)
async def home(as_of: Optional[str] = Query(None)) -> HTMLResponse:
    """Full dashboard. Optional `?as_of=ISO8601` for replay."""
    dt = _parse_as_of(as_of)
    html = render_dashboard_html(dt)
    return HTMLResponse(html)


# ============================================================================
# JSON APIs (one per panel)
# ============================================================================

@app.get("/api/scorecard")
async def api_scorecard(as_of: Optional[str] = Query(None)) -> JSONResponse:
    return JSONResponse(gather_scorecard(_parse_as_of(as_of)))


@app.get("/api/trade-book")
async def api_trade_book(
    as_of: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
) -> JSONResponse:
    return JSONResponse(_jsonable(gather_trade_book(_parse_as_of(as_of), limit=limit)))


@app.get("/api/debates")
async def api_debates(as_of: Optional[str] = Query(None)) -> JSONResponse:
    return JSONResponse(_jsonable(gather_debates_panel(_parse_as_of(as_of))))


@app.get("/api/hypothesis-graph-sample")
async def api_graph_sample(seed: Optional[int] = Query(None)) -> JSONResponse:
    return JSONResponse(_jsonable(gather_hypothesis_graph_sample(seed=seed)))


@app.get("/api/persona-diffs")
async def api_persona_diffs(as_of: Optional[str] = Query(None)) -> JSONResponse:
    return JSONResponse(_jsonable(gather_persona_diffs(_parse_as_of(as_of))))


@app.get("/api/weakness-map")
async def api_weakness_map(as_of: Optional[str] = Query(None)) -> JSONResponse:
    return JSONResponse(_jsonable(gather_weakness_map(_parse_as_of(as_of))))


@app.get("/api/tool-atlas")
async def api_tool_atlas(as_of: Optional[str] = Query(None)) -> JSONResponse:
    return JSONResponse(_jsonable(gather_tool_atlas(_parse_as_of(as_of))))


# ============================================================================
# Veto endpoint — blocks the 24h auto-promote per v2 line 150
# ============================================================================

@app.post("/api/veto/{candidate_id}")
async def api_veto(
    candidate_id: str,
    reason: str = Query(..., min_length=3, max_length=1000),
    from_agent: str = Query("human_reviewer"),
) -> JSONResponse:
    """Write an `agent_messages` row with `message_kind='veto'` against the
    given persona mutation candidate. The Phase 5 evolution loop is
    expected to read these and refuse to promote within the 24h veto
    window."""
    msg_id = write_veto_message(
        candidate_id=candidate_id, reason=reason, from_agent=from_agent,
    )
    return JSONResponse({
        "message_id": msg_id,
        "candidate_id": candidate_id,
        "from_agent": from_agent,
        "ok": True,
    })


# ============================================================================
# Liveness
# ============================================================================

@app.get("/healthz", response_class=PlainTextResponse)
async def healthz() -> PlainTextResponse:
    return PlainTextResponse("ok")


# ============================================================================
# Internals
# ============================================================================

def _jsonable(obj: Any) -> Any:
    """Make sqlite3.Row + datetime safe for JSON. Recursive."""
    if obj is None:
        return None
    if isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(x) for x in obj]
    if isinstance(obj, datetime):
        return obj.isoformat()
    # sqlite3.Row -> dict
    try:
        return _jsonable(dict(obj))
    except Exception:
        return str(obj)

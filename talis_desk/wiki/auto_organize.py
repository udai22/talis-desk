"""Knowledge compounding wiki — delta-aware auto-organizer.

End-of-cycle hook that:
  - Calls the existing `generate_wiki` to project desk.db state into
    markdown files under `~/.talis/wiki/`.
  - Computes deltas vs the prior cycle's wiki snapshot:
      * new hypotheses
      * resolved hypotheses
      * posterior moves > 0.2
      * new themes added by the Director
      * dead theses (status=abandoned this cycle)
  - Writes a delta page at `wiki/<cycle_id>/_delta.md`.
  - If `~/.talis/wiki/.git` exists, runs `git add . && git commit` to
    record the cycle snapshot.

NO STUBS. If git isn't available, we skip the commit step quietly.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ..store import get_desk_store
from .generator import generate_wiki


logger = logging.getLogger(__name__)


@dataclass
class CycleDelta:
    cycle_id: str
    new_hypotheses: list[dict] = field(default_factory=list)
    resolved_hypotheses: list[dict] = field(default_factory=list)
    posterior_moves: list[dict] = field(default_factory=list)
    new_themes: list[str] = field(default_factory=list)
    dead_theses: list[dict] = field(default_factory=list)


@dataclass
class AutoOrganizeResult:
    cycle_id: str
    wiki_root: str
    pages_written: int
    delta_path: Optional[str] = None
    git_committed: bool = False
    git_error: Optional[str] = None
    delta: Optional[CycleDelta] = None


def compute_cycle_delta(cycle_id: str, prior_cycle_id: Optional[str] = None) -> CycleDelta:
    """Compute the delta payload for `cycle_id` vs `prior_cycle_id`.

    If `prior_cycle_id` is None, we infer the prior cycle by looking
    up the most recent cycle_id in `hypotheses` that's lex-less than
    `cycle_id` (cycle ids include a timestamp so this is ordered).
    """
    conn = get_desk_store().conn
    delta = CycleDelta(cycle_id=cycle_id)

    # Sub-cycle expansion: same prefix logic as compose_brief.
    prefix = f"{cycle_id}__"
    new_rows = conn.execute(
        "SELECT id, title, hypothesis_text, status, posterior_prob, payload "
        "FROM hypotheses "
        "WHERE (cycle_id = ? OR cycle_id LIKE ?) "
        "  AND transaction_to IS NULL ",
        (cycle_id, prefix + "%"),
    ).fetchall()
    new_ids = set()
    for r in new_rows:
        new_ids.add(r["id"])
        try:
            payload = json.loads(r["payload"]) if r["payload"] else {}
        except Exception:
            payload = {}
        item = {
            "id": r["id"],
            "title": r["title"],
            "hypothesis_text": r["hypothesis_text"],
            "status": r["status"],
            "posterior_prob": r["posterior_prob"],
            "entity": payload.get("entity") or payload.get("instrument"),
        }
        if r["status"] in ("resolved", "supported", "contradicted"):
            delta.resolved_hypotheses.append(item)
        elif r["status"] == "abandoned":
            delta.dead_theses.append(item)
        else:
            delta.new_hypotheses.append(item)

    # Posterior moves > 0.2: compare current vs supersedes.
    try:
        rows = conn.execute(
            "SELECT h2.id, h2.title, h2.posterior_prob AS new_post, "
            "       h1.posterior_prob AS old_post "
            "FROM hypotheses h1 JOIN hypotheses h2 ON h2.supersedes = h1.id "
            "WHERE (h2.cycle_id = ? OR h2.cycle_id LIKE ?) "
            "  AND h2.transaction_to IS NULL ",
            (cycle_id, prefix + "%"),
        ).fetchall()
        for r in rows:
            old = float(r["old_post"] or 0.0)
            new = float(r["new_post"] or 0.0)
            if abs(new - old) >= 0.2:
                delta.posterior_moves.append({
                    "id": r["id"],
                    "title": r["title"],
                    "old": old,
                    "new": new,
                    "abs_move": abs(new - old),
                })
    except sqlite3.OperationalError:
        pass

    # New themes: pull from agent_messages with kind='curriculum_assignment'
    try:
        rows = conn.execute(
            "SELECT payload FROM agent_messages "
            "WHERE (from_agent = 'research_director' "
            "    OR to_agent_or_topic LIKE 'topic:%theme%') "
            "  AND transaction_to IS NULL "
            "ORDER BY posted_at DESC LIMIT 50"
        ).fetchall()
        themes_seen: set[str] = set()
        for r in rows:
            try:
                p = json.loads(r["payload"]) if r["payload"] else {}
                for t in p.get("themes", []) or []:
                    themes_seen.add(str(t))
                for t in p.get("cross_cutting_themes", []) or []:
                    themes_seen.add(str(t))
            except Exception:
                continue
        delta.new_themes = sorted(themes_seen)[:20]
    except sqlite3.OperationalError:
        pass

    return delta


def render_delta_page(delta: CycleDelta) -> str:
    """Render the cycle delta as markdown."""
    lines = [f"# Cycle Delta — {delta.cycle_id}", ""]
    lines.append(f"_Generated {datetime.now(timezone.utc).isoformat()}_")
    lines.append("")
    lines.append(f"## Summary")
    lines.append("")
    lines.append(f"- New hypotheses: **{len(delta.new_hypotheses)}**")
    lines.append(f"- Resolved hypotheses: **{len(delta.resolved_hypotheses)}**")
    lines.append(f"- Posterior moves ≥0.2: **{len(delta.posterior_moves)}**")
    lines.append(f"- Dead theses: **{len(delta.dead_theses)}**")
    lines.append(f"- Active themes: **{len(delta.new_themes)}**")
    lines.append("")
    if delta.new_themes:
        lines.append("## Active themes")
        lines.append("")
        for t in delta.new_themes:
            lines.append(f"- {t}")
        lines.append("")
    if delta.posterior_moves:
        lines.append("## Posterior moves ≥0.2")
        lines.append("")
        lines.append("| Hypothesis | Old | New | Δ |")
        lines.append("|---|---|---|---|")
        for m in sorted(delta.posterior_moves, key=lambda x: x["abs_move"], reverse=True)[:25]:
            lines.append(
                f"| {(m['title'] or '')[:80]} | {m['old']:.2f} | {m['new']:.2f} | "
                f"{m['new'] - m['old']:+.2f} |"
            )
        lines.append("")
    if delta.dead_theses:
        lines.append("## Dead theses this cycle")
        lines.append("")
        for d in delta.dead_theses[:20]:
            lines.append(f"- **{d['id']}** — {(d['title'] or '')[:120]}")
        lines.append("")
    if delta.new_hypotheses:
        lines.append("## New hypotheses (top 25 by posterior)")
        lines.append("")
        for h in sorted(delta.new_hypotheses,
                        key=lambda x: (x.get("posterior_prob") or 0.0),
                        reverse=True)[:25]:
            ent = h.get("entity") or "—"
            post = h.get("posterior_prob")
            ps = f"p={post:.2f}" if post is not None else ""
            lines.append(
                f"- **{ent}** [{ps}] {(h['title'] or '')[:120]}"
            )
        lines.append("")
    return "\n".join(lines)


def maybe_git_commit(wiki_root: Path, cycle_id: str) -> tuple[bool, Optional[str]]:
    """If `wiki_root/.git` exists, stage everything + commit. Returns
    (committed, error). Non-fatal."""
    if not (wiki_root / ".git").exists():
        return False, None
    try:
        subprocess.run(
            ["git", "-C", str(wiki_root), "add", "-A"],
            check=True, capture_output=True, timeout=30,
        )
        msg = f"wiki: cycle {cycle_id} ({datetime.now(timezone.utc).isoformat()})"
        result = subprocess.run(
            ["git", "-C", str(wiki_root), "commit", "-m", msg],
            capture_output=True, timeout=30, text=True,
        )
        if result.returncode == 0:
            return True, None
        # `git commit` returns 1 when there's nothing to commit; treat as no-op.
        if "nothing to commit" in (result.stdout or "") + (result.stderr or ""):
            return False, None
        return False, (result.stderr or result.stdout or "unknown git error").strip()
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def auto_organize(
    cycle_id: str,
    db_path: Optional[Path] = None,
    wiki_root: Optional[Path] = None,
) -> AutoOrganizeResult:
    """End-of-cycle hook. Returns AutoOrganizeResult."""
    if db_path is None:
        db_path = Path.home() / ".talis" / "desk.db"
    if wiki_root is None:
        wiki_root = Path.home() / ".talis" / "wiki"
    wiki_root.mkdir(parents=True, exist_ok=True)

    wiki = generate_wiki(db_path=db_path, cycle_id=cycle_id)
    wiki_root_str = str(getattr(wiki, "root", wiki_root))

    delta = compute_cycle_delta(cycle_id)
    delta_md = render_delta_page(delta)
    delta_path = wiki_root / "cycles" / cycle_id / "_delta.md"
    delta_path.parent.mkdir(parents=True, exist_ok=True)
    delta_path.write_text(delta_md)

    committed, git_err = maybe_git_commit(wiki_root, cycle_id)

    return AutoOrganizeResult(
        cycle_id=cycle_id,
        wiki_root=wiki_root_str,
        pages_written=int(getattr(wiki, "pages_written", 0)),
        delta_path=str(delta_path),
        git_committed=committed,
        git_error=git_err,
        delta=delta,
    )

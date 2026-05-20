"""Topology engine entry point.

Composes embed -> project -> density -> persist + render. Called at the
end of each swarm cycle by `run_swarm.py`. Output feeds back into the
Tier 0 seed generator's manifold-density routing for the next cycle.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from ..store import get_desk_store
from .density import compute_density
from .embed import EmbeddingsUnavailableError, embed_hypotheses
from .project import project_2d
from .render import write_topology_artifacts


logger = logging.getLogger(__name__)


@dataclass
class TopologyResult:
    cycle_id: str
    n_hypotheses: int
    n_regions: int
    density_rows: list[dict] = field(default_factory=list)
    projection_paths: dict[str, str] = field(default_factory=dict)
    out_dir: Optional[str] = None
    quality_flags: list[str] = field(default_factory=list)
    error: Optional[str] = None


def _load_cycle_hypotheses(cycle_id: str, max_n: int = 2000) -> list[dict]:
    """Return all hypotheses persisted under `{cycle_id}__*` or the
    base cycle_id."""
    conn = get_desk_store().conn
    prefix = f"{cycle_id}__"
    try:
        rows = conn.execute(
            "SELECT id, title, hypothesis_text, payload "
            "FROM hypotheses "
            "WHERE (cycle_id = ? OR cycle_id LIKE ?) "
            "  AND transaction_to IS NULL "
            "ORDER BY transaction_from DESC LIMIT ?",
            (cycle_id, prefix + "%", int(max_n)),
        ).fetchall()
    except sqlite3.OperationalError as e:
        logger.warning("topology hyp load failed: %s", e)
        return []
    out: list[dict] = []
    for r in rows:
        try:
            payload = json.loads(r["payload"]) if r["payload"] else {}
        except Exception:
            payload = {}
        text = (r["hypothesis_text"] or r["title"] or "").strip()
        if not text:
            continue
        d = {
            "id": r["id"],
            "text": text[:1000],
            "entity": (payload.get("entity") or "").strip() or None,
            "lens": (payload.get("lens") or "").strip() or None,
        }
        # Pull entity from entity_ids if available.
        out.append(d)
    return out


def _persist_density_rows(
    cycle_id: str, projection_view: str, rows: list[dict],
) -> None:
    conn = get_desk_store().conn
    now = datetime.now(timezone.utc).isoformat()
    try:
        for r in rows:
            rid = f"tdm_{uuid4().hex[:10]}"
            conn.execute(
                "INSERT INTO topology_density_map "
                "(id, cycle_id, projection_view, region_id, density, "
                " centroid_x, centroid_y, radius, n_members, "
                " member_hypothesis_ids, label, is_frontier, payload, "
                " valid_from, transaction_from) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    rid, cycle_id, projection_view, r["region_id"],
                    r["density"], r["centroid_x"], r["centroid_y"], r["radius"],
                    r["n_members"], json.dumps(r["member_hypothesis_ids"]),
                    r["label"], r["is_frontier"], json.dumps({}),
                    now, now,
                ),
            )
        conn.commit()
    except sqlite3.OperationalError as e:
        logger.warning("density row persist failed: %s", e)


def run_topology_cycle(
    cycle_id: str,
    out_dir: Optional[Path] = None,
    max_n: int = 2000,
) -> TopologyResult:
    """End-to-end topology pass for one cycle.

    Steps:
      1. Load hypotheses under cycle_id or its sub-cycles
      2. Embed (OpenAI text-embedding-3-small)
      3. Project to 2D
      4. Density estimate + frontier flag
      5. Persist density rows to `topology_density_map`
      6. Render SVG + JSON to wiki/topology/<cycle_id>/

    Returns a `TopologyResult`. NO STUBS: if embeddings fail we record
    `quality_flag=['topology_embeddings_unavailable']` and return the
    result with an empty density map (Tier 0 falls back to uniform).
    """
    result = TopologyResult(cycle_id=cycle_id, n_hypotheses=0, n_regions=0)
    hyps = _load_cycle_hypotheses(cycle_id, max_n=max_n)
    result.n_hypotheses = len(hyps)
    if not hyps:
        result.quality_flags.append("no_hypotheses_in_cycle")
        return result

    try:
        embedded = embed_hypotheses(hyps)
    except EmbeddingsUnavailableError as e:
        result.quality_flags.append("topology_embeddings_unavailable")
        result.error = str(e)
        return result
    except Exception as e:
        result.quality_flags.append("topology_embed_failed")
        result.error = f"{type(e).__name__}: {e}"
        return result

    projected = project_2d(embedded)
    density_rows = compute_density(projected, projection_view="umap_2d")
    result.density_rows = density_rows
    result.n_regions = len(density_rows)

    _persist_density_rows(cycle_id, "umap_2d", density_rows)

    if out_dir is None:
        out_dir = Path.home() / ".talis" / "wiki" / "topology" / cycle_id
    artifacts = write_topology_artifacts(
        out_dir, cycle_id, projected, density_rows,
        views=[("umap_2d", f"Topology — {cycle_id}")],
    )
    result.projection_paths = artifacts
    result.out_dir = str(out_dir)
    return result

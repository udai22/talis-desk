"""Render density maps to SVG + JSON for the wiki + monitor inspector.

We produce a self-contained SVG (no external CSS / JS) showing:
  - Each hypothesis as a dot, colored by lens
  - Each density cell as a rectangle, opacity proportional to density
  - Frontier cells outlined in red
"""
from __future__ import annotations

import html
import json
import logging
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)


# Color palette per lens (deterministic; visually distinct).
LENS_COLORS: dict[str, str] = {
    "macro": "#e74c3c",
    "microstructure": "#3498db",
    "options_flow": "#9b59b6",
    "smart_money": "#16a085",
    "sentiment": "#f39c12",
    "rotation": "#2ecc71",
    "factor": "#34495e",
    "vol_surface": "#d35400",
    "catalyst": "#c0392b",
    "filing": "#7f8c8d",
    "polymarket": "#1abc9c",
    "anomaly": "#e67e22",
    "on_chain": "#2980b9",
    "money_velocity": "#27ae60",
    "structural": "#8e44ad",
}
DEFAULT_LENS_COLOR = "#888888"


def render_svg(
    projected: list[dict],
    density_rows: list[dict],
    width: int = 800,
    height: int = 600,
    title: str = "Hypothesis Topology",
) -> str:
    """Return a self-contained SVG string."""
    if not projected:
        return _empty_svg(width, height, title)
    xs = [p["x"] for p in projected]
    ys = [p["y"] for p in projected]
    xmin, xmax = min(xs), max(xs)
    ymin, ymax = min(ys), max(ys)
    if xmin == xmax:
        xmax = xmin + 1.0
    if ymin == ymax:
        ymax = ymin + 1.0
    pad = 40
    plot_w = width - 2 * pad
    plot_h = height - 2 * pad

    def to_px(x: float, y: float) -> tuple[float, float]:
        px = pad + (x - xmin) / (xmax - xmin) * plot_w
        # SVG y axis flipped
        py = pad + (ymax - y) / (ymax - ymin) * plot_h
        return px, py

    svg_parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" '
        f'style="background:#0a0a0a;font-family:monospace">'
    ]
    svg_parts.append(
        f'<text x="{width // 2}" y="20" text-anchor="middle" '
        f'fill="#eee" font-size="14">{html.escape(title)}</text>'
    )

    # Density rectangles
    max_density = max((r["density"] for r in density_rows), default=1.0) or 1.0
    for r in density_rows:
        cx, cy = r["centroid_x"], r["centroid_y"]
        rad = r["radius"]
        x0, y0 = to_px(cx - rad, cy + rad)
        x1, y1 = to_px(cx + rad, cy - rad)
        w = abs(x1 - x0)
        h = abs(y1 - y0)
        opacity = min(0.5, 0.05 + 0.4 * (r["density"] / max_density))
        frontier_stroke = "#ff4444" if r.get("is_frontier") else "none"
        svg_parts.append(
            f'<rect x="{x0:.1f}" y="{y0:.1f}" width="{w:.1f}" height="{h:.1f}" '
            f'fill="#ffffff" fill-opacity="{opacity:.3f}" '
            f'stroke="{frontier_stroke}" stroke-width="1" />'
        )

    # Hypothesis dots
    for p in projected:
        px, py = to_px(p["x"], p["y"])
        color = LENS_COLORS.get(p.get("lens") or "", DEFAULT_LENS_COLOR)
        svg_parts.append(
            f'<circle cx="{px:.1f}" cy="{py:.1f}" r="3" '
            f'fill="{color}" stroke="#000" stroke-width="0.5"><title>'
            f'{html.escape((p.get("entity") or "?") + " " + (p.get("text") or "")[:80])}'
            f'</title></circle>'
        )

    # Legend
    legend_y = height - 25
    items = list(LENS_COLORS.items())[:8]
    x = pad
    for lens, color in items:
        svg_parts.append(
            f'<circle cx="{x}" cy="{legend_y}" r="4" fill="{color}" />'
        )
        svg_parts.append(
            f'<text x="{x + 8}" y="{legend_y + 4}" fill="#ddd" font-size="10">'
            f'{html.escape(lens)}</text>'
        )
        x += 90

    svg_parts.append("</svg>")
    return "\n".join(svg_parts)


def _empty_svg(width: int, height: int, title: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" style="background:#0a0a0a;font-family:monospace">'
        f'<text x="{width // 2}" y="{height // 2}" text-anchor="middle" '
        f'fill="#888" font-size="14">{html.escape(title)} (no data)</text></svg>'
    )


def write_topology_artifacts(
    out_dir: Path,
    cycle_id: str,
    projected: list[dict],
    density_rows: list[dict],
    views: list[tuple[str, str]] = (("umap_2d", "Hypothesis topology — UMAP 2D"),),
) -> dict[str, str]:
    """Write SVG + JSON files for each projection view. Returns a dict
    {view: path} of artifact locations.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, str] = {}
    for view, title in views:
        svg = render_svg(projected, density_rows, title=title)
        svg_path = out_dir / f"{view}.svg"
        svg_path.write_text(svg)
        json_path = out_dir / f"{view}.json"
        json_path.write_text(json.dumps({
            "cycle_id": cycle_id,
            "projection_view": view,
            "n_points": len(projected),
            "n_regions": len(density_rows),
            "points": projected,
            "regions": density_rows,
        }, default=str, indent=2))
        paths[f"{view}_svg"] = str(svg_path)
        paths[f"{view}_json"] = str(json_path)
    return paths

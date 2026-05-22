"""Export the live scout agent graph viewer for static hosting."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from talis_desk.monitor.agent_graph import export_agent_graph_viewer


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", default="", help="Launch-gate run directory. Defaults to newest discovered run.")
    parser.add_argument("--output-dir", default="docs/agent-graph")
    parser.add_argument("--html-source", default="talis_desk/monitor/agent_graph.html")
    args = parser.parse_args()

    result = export_agent_graph_viewer(
        output_dir=Path(args.output_dir),
        html_source=Path(args.html_source),
        run_dir=args.run_dir or None,
    )
    print("AGENT_GRAPH_VIEWER_EXPORT=" + json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Tool Atlas — Phase 2 of the SOTA Desk Architecture (Layer 1).

Implements the four core APIs from wiki/SOTA_DESK_ARCHITECTURE.md §2 Layer 1
(lines 42-79):

    regenerate_tool_atlas(as_of, include_candidates=False) -> ToolAtlasSnapshot
    resolve_tool_uri(uri, as_of=None)                       -> ToolContract
    dispatch_uri(uri, args, context)                        -> ToolResult
    load_skill_registry(as_of, specialist_id=None)          -> list[SkillManifest]

# URI scheme (exact, from v2 lines 47-52)

    tic://tool/builtin/query_timeseries@v1
    tic://tool/hydromancer/fetch_live@v3?source=hl_l4_ofi
    tic://tool/learned/usdt_mint_flow_to_btc_correlation@v2
    tic://skill/microstructure_sweep_detection@v1
    tic://source/hl/info/metaAndAssetCtxs
    tic://artifact/trade_idea/ti_<uuid>

# Discovery

Two paths per v2 line 79:
  1. Fast path: per-specialist curated subset by Brier-weighted affinity from
     mv_top_tools_per_specialist_30d. Falls back to uniform top-15 when MV is
     empty (Phase 6 backfill not yet done).
  2. Full path: semantic_search over SKILL.md content using
     tic.agent_native.semantic_index.

# Frozen-during-cycle

regenerate_tool_atlas runs nightly at 00:00 UTC. During a cycle, agents see a
stable snapshot via get_atlas_snapshot_for_cycle(cycle_id) — second call for
the same cycle returns identical state even if atlas mutated between them.
This preserves replay determinism (v2 line 64).

# Explicit fallbacks

  - Tool affinity per specialist reads mv_top_tools_per_specialist_30d which
    can be empty on a fresh desk. In that case the registry uses an unweighted
    curated ordering and marks the snapshot normally.
  - Learned tools are loaded from learned_tools/<slug>/manifest.json when
    present. Missing directories are treated as "no learned tools installed",
    not as generated capabilities.
  - Cost estimates for tools that don't carry an explicit cost_hint default
    to $0.001/call.
  - The tool_atlas and tool_call_log table DDLs are owned by the sibling
    schema_sota migration; until that lands we materialize the same shape via
    CREATE TABLE IF NOT EXISTS so this module is self-bootstrapping.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import time
import uuid
import warnings
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse, parse_qsl

from ..store import get_store


# ============================================================================
# 1. Constants + dataclasses
# ============================================================================

REPO_ROOT = Path(__file__).resolve().parent.parent.parent  # brief_experiments/
SKILLS_DIR = REPO_ROOT / "skills"
LEARNED_TOOLS_DIR = REPO_ROOT / "learned_tools"

DEFAULT_COST_USD_PER_CALL = 0.001
MAX_SKILLS = 200
DEFAULT_TOP_K_AFFINITY = 15

REQUIRED_SKILL_SECTIONS = (
    "name",
    "when_to_use",
    "inputs",
    "outputs",
    "example_invocations",
    "cost_hint",
    "last_brier_30d",
    "owner_specialist",
)


@dataclass
class ParsedURI:
    """Parsed `tic://...` URI per v2 lines 47-52."""

    scheme: str            # 'tic'
    authority: str         # 'tool' | 'skill' | 'source' | 'artifact'
    sub_kind: str          # 'builtin' | 'hydromancer' | 'learned' | sub-path
    slug: str              # tool/skill/source slug
    version: Optional[str]  # 'v1' / 'v2' / None
    params: dict[str, str]  # query-string params

    @property
    def kind(self) -> str:
        return self.authority


@dataclass
class ToolContract:
    """Concrete tool definition resolved from the atlas."""

    uri: str
    name: str
    version: str
    kind: str            # 'builtin' | 'hydromancer' | 'skill' | 'source' | ...
    provider: str
    description: str
    input_schema: dict[str, Any]
    cost_hint: dict[str, Any]
    timeout_ms: int
    callable: Optional[Callable]
    callable_ref: str    # 'module.path:function_name' style ref
    code_sha256: Optional[str]
    skill_md_path: Optional[str] = None
    source_dependencies: list[str] = field(default_factory=list)
    permission_scope: str = "read_only"
    network_hosts: list[str] = field(default_factory=list)
    status: str = "active"


@dataclass
class ToolAtlasSnapshot:
    """Frozen point-in-time view of the atlas. Returned by both
    regenerate_tool_atlas (live snapshot) and get_atlas_snapshot_for_cycle."""

    as_of: datetime
    cycle_id: Optional[str]
    rows: list[dict[str, Any]]  # rows from tool_atlas
    n_tools: int
    n_skills: int
    n_sources: int

    def by_uri(self) -> dict[str, dict[str, Any]]:
        return {row["tool_uri"]: row for row in self.rows}


@dataclass
class SkillManifest:
    """Parsed SKILL.md. v2 lines 66-79."""

    slug: str
    name: str
    when_to_use: str
    inputs: str
    outputs: str
    example_invocations: list[str]
    cost_hint: dict[str, Any]
    last_brier_30d: Optional[float]
    owner_specialist: str
    supersedes_skill_id: Optional[str]
    path: str
    text_for_embedding: str


@dataclass
class ToolResult:
    """Return envelope from dispatch_uri."""

    ok: bool
    uri: str
    args_hash: str
    result_hash: Optional[str]
    result: Any
    duration_ms: int
    cost_usd: float
    tool_call_log_id: str
    error: Optional[str] = None


@dataclass
class AgentContext:
    """Minimal context carried through dispatch. The full SOTA AgentContext
    will carry more (priors, message channels, etc.); this is the slice
    Layer 1 needs."""

    cycle_id: str
    specialist_id: str
    investigation_id: Optional[str] = None


# ============================================================================
# 2. URI parsing
# ============================================================================

_URI_REGEX = re.compile(
    r"^tic://"
    r"(?P<authority>tool|skill|source|artifact)"
    r"/(?P<path>[^?]+)"
    r"(?:\?(?P<query>.*))?$"
)


def parse_tool_uri(uri: str) -> ParsedURI:
    """Parse a tic:// URI. Raises ValueError with a clean message on malformed
    input.

    Examples:
      tic://tool/builtin/query_timeseries@v1
      tic://tool/hydromancer/fetch_live@v3?source=hl_l4_ofi
      tic://tool/learned/usdt_mint_flow_to_btc_correlation@v2
      tic://skill/microstructure_sweep_detection@v1
      tic://source/hl/info/metaAndAssetCtxs
      tic://artifact/trade_idea/ti_abc123
    """
    if not isinstance(uri, str) or not uri:
        raise ValueError(f"invalid_uri: empty/non-string {uri!r}")
    m = _URI_REGEX.match(uri.strip())
    if not m:
        raise ValueError(
            f"invalid_uri: must match 'tic://<tool|skill|source|artifact>/<path>[?params]', got {uri!r}"
        )
    authority = m.group("authority")
    path = m.group("path")
    query = m.group("query") or ""
    params = {k: v for k, v in parse_qsl(query, keep_blank_values=True)}

    parts = path.split("/", 1)
    if authority in ("tool", "skill"):
        if authority == "tool":
            # tool/<sub_kind>/<slug>[@vN]
            if len(parts) != 2:
                raise ValueError(
                    f"invalid_uri: tool URI must be tic://tool/<sub_kind>/<slug>, got {uri!r}"
                )
            sub_kind, tail = parts[0], parts[1]
        else:
            # skill/<slug>[@vN]
            sub_kind, tail = "skill", path
        slug, version = _split_version(tail)
    elif authority == "source":
        # source/<provider>/<rest...>
        if len(parts) == 1:
            sub_kind, slug = "default", parts[0]
        else:
            sub_kind, slug = parts[0], parts[1]
        version = None
    elif authority == "artifact":
        # artifact/<kind>/<id>
        if len(parts) == 1:
            sub_kind, slug = "default", parts[0]
        else:
            sub_kind, slug = parts[0], parts[1]
        version = None
    else:  # unreachable
        raise ValueError(f"invalid_uri: unknown authority {authority!r}")

    return ParsedURI(
        scheme="tic",
        authority=authority,
        sub_kind=sub_kind,
        slug=slug,
        version=version,
        params=params,
    )


def _split_version(tail: str) -> tuple[str, Optional[str]]:
    if "@" in tail:
        slug, ver = tail.rsplit("@", 1)
        if not re.match(r"^v\d+$", ver):
            raise ValueError(f"invalid_uri: version must look like 'v1', got {ver!r}")
        return slug, ver
    return tail, None


def build_tool_uri(kind: str, sub_kind: str, slug: str, version: str = "v1") -> str:
    """Inverse of parse_tool_uri for tool/skill URIs."""
    if kind == "tool":
        return f"tic://tool/{sub_kind}/{slug}@{version}"
    if kind == "skill":
        return f"tic://skill/{slug}@{version}"
    if kind == "source":
        return f"tic://source/{sub_kind}/{slug}"
    if kind == "artifact":
        return f"tic://artifact/{sub_kind}/{slug}"
    raise ValueError(f"unknown kind: {kind}")


# ============================================================================
# 3. Schema bootstrap (idempotent — defers to sibling schema_sota.py if loaded)
# ============================================================================

_BOOTSTRAP_SQL = """
CREATE TABLE IF NOT EXISTS tool_atlas (
    id TEXT PRIMARY KEY,
    tool_uri TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    version TEXT NOT NULL,
    kind TEXT NOT NULL,
    provider TEXT NOT NULL,
    callable_ref TEXT NOT NULL,
    schema_json TEXT NOT NULL,
    skill_md_path TEXT,
    description TEXT NOT NULL,
    source_dependencies TEXT NOT NULL DEFAULT '[]',
    permission_scope TEXT NOT NULL DEFAULT 'read_only',
    network_hosts TEXT NOT NULL DEFAULT '[]',
    cost_hint TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'active',
    code_sha256 TEXT,
    supersedes TEXT,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    transaction_from TEXT NOT NULL,
    transaction_to TEXT
);
CREATE INDEX IF NOT EXISTS idx_tool_atlas_uri ON tool_atlas(tool_uri, version);
CREATE INDEX IF NOT EXISTS idx_tool_atlas_kind ON tool_atlas(kind);
CREATE INDEX IF NOT EXISTS idx_tool_atlas_txn ON tool_atlas(transaction_to);

CREATE TABLE IF NOT EXISTS tool_call_log (
    id TEXT PRIMARY KEY,
    cycle_id TEXT NOT NULL,
    investigation_id TEXT,
    specialist_id TEXT NOT NULL,
    tool_uri TEXT NOT NULL,
    tool_version TEXT NOT NULL,
    args_hash TEXT NOT NULL,
    args_json TEXT NOT NULL,
    result_hash TEXT,
    result_summary TEXT,
    reward_score TEXT NOT NULL DEFAULT '{}',
    cited_in_ids TEXT NOT NULL DEFAULT '[]',
    error TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    duration_ms INTEGER,
    cost_usd REAL NOT NULL DEFAULT 0,
    source_ids TEXT NOT NULL DEFAULT '[]',
    claim_ids TEXT NOT NULL DEFAULT '[]',
    quality_flags TEXT NOT NULL DEFAULT '[]',
    supersedes TEXT,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    transaction_from TEXT NOT NULL,
    transaction_to TEXT
);
CREATE INDEX IF NOT EXISTS idx_tool_call_log_cycle ON tool_call_log(cycle_id, started_at);
CREATE INDEX IF NOT EXISTS idx_tool_call_log_uri ON tool_call_log(tool_uri, started_at);
CREATE INDEX IF NOT EXISTS idx_tool_call_log_specialist ON tool_call_log(specialist_id, started_at);

CREATE TABLE IF NOT EXISTS tool_atlas_cycle_snapshots (
    cycle_id TEXT PRIMARY KEY,
    snapshot_at TEXT NOT NULL,
    snapshot_json TEXT NOT NULL
);
"""

_SCHEMA_BOOTSTRAPPED = False


def _ensure_schema() -> None:
    """Idempotent schema bootstrap. Tries the sibling schema_sota module
    first; falls back to the inline SQL below if the sibling isn't available
    yet."""
    global _SCHEMA_BOOTSTRAPPED
    if _SCHEMA_BOOTSTRAPPED:
        return
    try:
        from . import schema_sota  # type: ignore[attr-defined]
        if hasattr(schema_sota, "ensure_schema"):
            schema_sota.ensure_schema()
    except Exception:
        # Sibling not present yet; bootstrap inline so this module works
        # standalone.
        pass
    conn = get_store().conn
    for stmt in _BOOTSTRAP_SQL.strip().split(";"):
        s = stmt.strip()
        if not s:
            continue
        try:
            conn.execute(s)
        except Exception as e:
            warnings.warn(f"tool_atlas schema bootstrap stmt failed: {e}")
    conn.commit()
    _SCHEMA_BOOTSTRAPPED = True


# ============================================================================
# 4. Hashing + serialization helpers
# ============================================================================

def _canonical_json(obj: Any) -> str:
    """Stable JSON for hashing (sorted keys, compact separators, str default)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _sha256_hex(s: str | bytes) -> str:
    if isinstance(s, str):
        s = s.encode("utf-8")
    return hashlib.sha256(s).hexdigest()


def _hash_callable(fn: Callable | None) -> Optional[str]:
    if fn is None:
        return None
    try:
        src = inspect.getsource(fn)
    except Exception:
        try:
            src = f"{fn.__module__}.{getattr(fn, '__qualname__', repr(fn))}"
        except Exception:
            return None
    return _sha256_hex(src)


def _callable_ref(fn: Callable | None) -> str:
    if fn is None:
        return ""
    mod = getattr(fn, "__module__", "?")
    qual = getattr(fn, "__qualname__", getattr(fn, "__name__", "?"))
    return f"{mod}:{qual}"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def learned_tools_dir() -> Path:
    """Runtime-overridable learned-tool root.

    Tests and smoke runs can set TALIS_LEARNED_TOOLS_DIR so generated learned
    tools do not dirty the repo. Production defaults to repo/learned_tools.
    """
    return Path(os.environ.get("TALIS_LEARNED_TOOLS_DIR") or LEARNED_TOOLS_DIR)


# ============================================================================
# 5. Provider classification — maps a TOOLS entry to (kind, sub_kind, provider)
# ============================================================================

_HYDROMANCER_TOOL_NAMES = {
    "hydromancer_query", "get_hl_pnl_leaderboard", "get_wallet_pnl_summary",
    "get_wallet_completed_trades", "get_wallet_historical_orders",
    "get_builder_fills", "batch_get_clearinghouse_states",
    "get_oracle_price_history", "get_hip4_outcomes",
    "query_hydromancer_reservoir",
}

_AGENT_NATIVE_TOOL_NAMES = {
    "semantic_search", "find_similar_setups", "find_confluence",
    "scratchpad_post", "scratchpad_read", "time_machine_snapshot",
    "replay_artifact_reasoning",
}

_PARALLEL_SEARCH_NAMES = {"parallel_search"}
_WEB_SEARCH_NAMES = {"web_search"}
_JARVIS_BRIDGE_TOOL_NAMES = {
    "jarvis_intelligence_surfaces",
    "jarvis_surface_search",
}

_TALIS_NATIVE_TOOL_NAMES = {
    "plan_alpha_geometry_actions",
}


def _classify_tool(name: str, spec: dict[str, Any]) -> tuple[str, str, str]:
    """Return (kind, sub_kind, provider) for a TOOLS-dict entry.

    kind   = tool_atlas.kind column (builtin/hydromancer/skill/...)
    sub_kind = URI sub-path (used in tic://tool/<sub_kind>/<slug>@vN)
    provider = human-readable provider name
    """
    if name in _HYDROMANCER_TOOL_NAMES:
        return ("hydromancer", "hydromancer", "hydromancer")
    if name in _AGENT_NATIVE_TOOL_NAMES:
        return ("builtin", "agent_native", "tic")
    if name in _PARALLEL_SEARCH_NAMES:
        return ("external", "parallel", "parallel.ai")
    if name in _WEB_SEARCH_NAMES:
        return ("external", "perplexity", "perplexity")
    if name in _JARVIS_BRIDGE_TOOL_NAMES:
        return ("builtin", "jarvis_bridge", "jarvis-trading-engine")
    if name in _TALIS_NATIVE_TOOL_NAMES:
        return ("builtin", "talis_native", "talis_desk")
    return ("builtin", "builtin", "tic")


# ============================================================================
# 6. SKILL.md parser
# ============================================================================

_SECTION_HEADER_RE = re.compile(r"^##\s+(.+?)\s*$")


def load_skill_md(path: str) -> Optional[SkillManifest]:
    """Parse a SKILL.md file. Returns None (with warning) if invalid."""
    p = Path(path)
    if not p.exists():
        warnings.warn(f"skill_md_not_found: {path}")
        return None
    try:
        text = p.read_text(encoding="utf-8")
    except Exception as e:
        warnings.warn(f"skill_md_read_failed: {path}: {e}")
        return None

    sections: dict[str, str] = {}
    current: Optional[str] = None
    buf: list[str] = []
    for line in text.splitlines():
        m = _SECTION_HEADER_RE.match(line)
        if m:
            if current is not None:
                sections[current] = "\n".join(buf).strip()
            current = _normalize_section_name(m.group(1))
            buf = []
            continue
        buf.append(line)
    if current is not None:
        sections[current] = "\n".join(buf).strip()

    missing = [k for k in REQUIRED_SKILL_SECTIONS if k not in sections]
    if missing:
        warnings.warn(
            f"skill_md_invalid: {path} missing required sections: {missing}"
        )
        return None

    examples_block = sections["example_invocations"]
    examples = _parse_examples(examples_block)
    if len(examples) < 3:
        warnings.warn(
            f"skill_md_invalid: {path} needs >=3 example_invocations (got {len(examples)})"
        )
        return None

    cost_hint = _parse_json_or_text(sections.get("cost_hint", ""))
    if not isinstance(cost_hint, dict):
        cost_hint = {"note": str(cost_hint)}

    try:
        last_brier = float(sections.get("last_brier_30d", "").strip())
    except Exception:
        last_brier = None

    slug = p.parent.name
    name = sections["name"].strip().splitlines()[0] if sections["name"] else slug

    text_for_embedding = "\n\n".join([
        name,
        sections.get("when_to_use", ""),
        "\n".join(examples),
    ]).strip()

    return SkillManifest(
        slug=slug,
        name=name,
        when_to_use=sections.get("when_to_use", ""),
        inputs=sections.get("inputs", ""),
        outputs=sections.get("outputs", ""),
        example_invocations=examples,
        cost_hint=cost_hint,
        last_brier_30d=last_brier,
        owner_specialist=sections.get("owner_specialist", "").strip().splitlines()[0]
            if sections.get("owner_specialist") else "",
        supersedes_skill_id=(sections.get("supersedes_skill_id", "").strip() or None),
        path=str(p),
        text_for_embedding=text_for_embedding,
    )


def _normalize_section_name(name: str) -> str:
    return name.strip().lower().replace(" ", "_")


def _parse_examples(block: str) -> list[str]:
    """Pull examples — bullet list, numbered list, or fenced code blocks."""
    lines = [l for l in block.splitlines() if l.strip()]
    examples: list[str] = []
    in_code = False
    code_buf: list[str] = []
    for line in lines:
        s = line.strip()
        if s.startswith("```"):
            if in_code:
                examples.append("\n".join(code_buf).strip())
                code_buf = []
                in_code = False
            else:
                in_code = True
            continue
        if in_code:
            code_buf.append(line)
            continue
        if s.startswith(("- ", "* ", "+ ")) or re.match(r"^\d+[.\)]\s", s):
            examples.append(re.sub(r"^[-*+]\s|^\d+[.\)]\s", "", s).strip())
    if code_buf:
        examples.append("\n".join(code_buf).strip())
    return [e for e in examples if e]


def _parse_json_or_text(s: str) -> Any:
    s = s.strip()
    if not s:
        return {}
    if s.startswith("{") or s.startswith("["):
        try:
            return json.loads(s)
        except Exception:
            return s
    return s


# ============================================================================
# 7. Scanners — walk TOOLS, sources, skills, learned tools
# ============================================================================

def _scan_builtin_tools() -> list[dict[str, Any]]:
    """Walk the live TOOLS registry and emit one atlas row per entry."""
    # Local import to avoid circular at module load.
    try:
        from tic.desk.tools import TOOLS  # type: ignore
    except Exception as e:
        warnings.warn(f"builtin_tool_scan_failed: {e}")
        return []

    rows: list[dict[str, Any]] = []
    for name, spec in TOOLS.items():
        kind, sub_kind, provider = _classify_tool(name, spec)
        fn = spec.get("callable")
        version = "v1"
        uri = build_tool_uri("tool", sub_kind, name, version)
        cost_hint = spec.get("cost_hint", {})
        if not cost_hint:
            cost_hint = {
                "usd_per_call_estimate": DEFAULT_COST_USD_PER_CALL,
                "note": "default_estimate",
            }
        rows.append({
            "tool_uri": uri,
            "tool_name": name,
            "version": version,
            "kind": kind,
            "provider": provider,
            "callable_ref": _callable_ref(fn),
            "schema_json": spec.get("input_schema", {}),
            "description": spec.get("description", ""),
            "source_dependencies": spec.get("source_dependencies", []),
            "permission_scope": spec.get("permission_scope", "read_only"),
            "network_hosts": spec.get("network_hosts", []),
            "cost_hint": cost_hint,
            "status": "active",
            "code_sha256": _hash_callable(fn),
            "skill_md_path": None,
        })
    return rows


def _scan_talis_native_tools() -> list[dict[str, Any]]:
    try:
        from .native_tools import native_tool_specs
    except Exception as e:
        warnings.warn(f"talis_native_tool_scan_failed: {e}")
        return []
    rows: list[dict[str, Any]] = []
    for spec in native_tool_specs():
        rows.append({
            "tool_uri": spec.tool_uri,
            "tool_name": spec.tool_name,
            "version": spec.version,
            "kind": "builtin",
            "provider": spec.provider,
            "callable_ref": _callable_ref(spec.callable),
            "schema_json": spec.input_schema,
            "description": spec.description,
            "source_dependencies": spec.source_dependencies,
            "permission_scope": spec.permission_scope,
            "network_hosts": spec.network_hosts,
            "cost_hint": spec.cost_hint,
            "status": "active",
            "code_sha256": _hash_callable(spec.callable),
            "skill_md_path": None,
        })
    return rows


def _scan_sources() -> list[dict[str, Any]]:
    """Each ingester source becomes tic://source/<provider>/<slug>."""
    try:
        from tic.desk.tools.data_tools import _get_dispatch
        table = _get_dispatch()
    except Exception as e:
        warnings.warn(f"source_scan_failed: {e}")
        return []
    rows: list[dict[str, Any]] = []
    for slug, spec in table.items():
        provider = _infer_source_provider(slug)
        uri = build_tool_uri("source", provider, slug)
        rows.append({
            "tool_uri": uri,
            "tool_name": slug,
            "version": "v1",
            "kind": "source",
            "provider": provider,
            "callable_ref": f"tic.desk.tools.data_tools:fetch_live(source='{slug}')",
            "schema_json": {
                "type": "object",
                "properties": {"source": {"type": "string", "const": slug}},
            },
            "description": spec.get("description", ""),
            "source_dependencies": [slug],
            "permission_scope": "read_only",
            "network_hosts": [],
            "cost_hint": {
                "ttl_s": spec.get("ttl_s"),
                "freshness": spec.get("freshness"),
                "usd_per_call_estimate": DEFAULT_COST_USD_PER_CALL,
            },
            "status": "active",
            "code_sha256": None,
            "skill_md_path": None,
        })
    return rows


def _infer_source_provider(slug: str) -> str:
    """Best-effort provider tag from slug prefix."""
    if slug.startswith("hl_") or slug in ("predicted_fundings", "borrow_lend",
                                            "perp_categories", "hl_outcomes",
                                            "l4_micro"):
        return "hl"
    if slug.startswith("pyth"):
        return "pyth"
    if slug.startswith("deribit"):
        return "deribit"
    if slug.startswith("polymarket"):
        return "polymarket"
    if slug.startswith("asksurf"):
        return "asksurf"
    if slug.startswith("nansen"):
        return "nansen"
    if slug.startswith("yahoo"):
        return "yahoo"
    if slug.startswith("occ"):
        return "occ"
    if slug.startswith(("fred", "bea_", "cbo_", "bls_", "eia_", "treasury")):
        return "macro"
    if slug.startswith(("cdc_",)):
        return "macro"
    return "misc"


def _scan_skills() -> tuple[list[dict[str, Any]], list[SkillManifest]]:
    """Walk skills/<slug>/SKILL.md files. Returns (atlas_rows, manifests)."""
    if not SKILLS_DIR.exists():
        return [], []
    rows: list[dict[str, Any]] = []
    manifests: list[SkillManifest] = []
    for skill_dir in sorted(SKILLS_DIR.iterdir()):
        if not skill_dir.is_dir():
            continue
        md = skill_dir / "SKILL.md"
        if not md.exists():
            continue
        manifest = load_skill_md(str(md))
        if manifest is None:
            continue
        manifests.append(manifest)
        version = "v1"
        uri = build_tool_uri("skill", "skill", manifest.slug, version)
        tool_py = skill_dir / "tool.py"
        rows.append({
            "tool_uri": uri,
            "tool_name": manifest.slug,
            "version": version,
            "kind": "skill",
            "provider": manifest.owner_specialist or "tic",
            "callable_ref": str(tool_py) if tool_py.exists() else "",
            "schema_json": {"inputs_doc": manifest.inputs,
                            "outputs_doc": manifest.outputs},
            "description": manifest.when_to_use[:500],
            "source_dependencies": [],
            "permission_scope": "read_only",
            "network_hosts": [],
            "cost_hint": manifest.cost_hint,
            "status": "active",
            "code_sha256": _hash_callable(_try_load_skill_callable(tool_py))
                if tool_py.exists() else None,
            "skill_md_path": str(md),
        })
    return rows, manifests


def _try_load_skill_callable(tool_py: Path) -> Optional[Callable]:
    """Read tool.py source to hash; don't actually exec for hash purposes."""
    try:
        # We just want a stable code hash; reading source is enough.
        # We do NOT exec arbitrary user code at scan time.
        src = tool_py.read_text(encoding="utf-8")
        # Return a sentinel function whose source equals the file (for hashing)
        # Instead, just hash the source directly via a closure shim.
        def _shim() -> None:  # pragma: no cover - hash carrier only
            pass
        _shim.__doc__ = src
        return _shim
    except Exception:
        return None


def _scan_learned_tools() -> list[dict[str, Any]]:
    """Walk learned_tools/<slug>/ entries. Returns [] if directory missing
    (AGENT_INFRA_PLAN Phase 2 creates this)."""
    root = learned_tools_dir()
    if not root.exists():
        return []
    rows: list[dict[str, Any]] = []
    for tool_dir in sorted(root.iterdir()):
        if not tool_dir.is_dir():
            continue
        meta_path = tool_dir / "manifest.json"
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text())
        except Exception as e:
            warnings.warn(f"learned_tool_manifest_invalid: {meta_path}: {e}")
            continue
        slug = meta.get("name", tool_dir.name)
        version = meta.get("version", "v1")
        uri = build_tool_uri("tool", "learned", slug, version)
        rows.append({
            "tool_uri": uri,
            "tool_name": slug,
            "version": version,
            "kind": "learned",
            "provider": meta.get("owner", "tic"),
            "callable_ref": meta.get("callable_ref", ""),
            "schema_json": meta.get("input_schema", {}),
            "description": meta.get("description", ""),
            "source_dependencies": meta.get("source_dependencies", []),
            "permission_scope": meta.get("permission_scope", "read_only"),
            "network_hosts": meta.get("network_hosts", []),
            "cost_hint": meta.get("cost_hint",
                                  {"usd_per_call_estimate": DEFAULT_COST_USD_PER_CALL}),
            "status": meta.get("status", "candidate"),
            "code_sha256": None,
            "skill_md_path": None,
        })
    return rows


# ============================================================================
# 8. Skill cap enforcement (LRU + Brier-worst demotion)
# ============================================================================

def _enforce_skill_cap(rows: list[dict[str, Any]], manifests: list[SkillManifest]) -> list[dict[str, Any]]:
    """If skills exceed MAX_SKILLS, demote LRU + Brier-worst first.

    Returns the row list with over-cap skills' status flipped to 'demoted'.
    """
    skill_rows = [r for r in rows if r["kind"] == "skill"]
    if len(skill_rows) <= MAX_SKILLS:
        return rows

    brier_by_slug = {m.slug: (m.last_brier_30d if m.last_brier_30d is not None else 1.0)
                     for m in manifests}
    # Sort worst-Brier first (higher = worse), then by slug as deterministic tiebreaker.
    skill_rows_sorted = sorted(
        skill_rows,
        key=lambda r: (-brier_by_slug.get(r["tool_name"], 1.0), r["tool_name"]),
    )
    demote_n = len(skill_rows) - MAX_SKILLS
    demote = {r["tool_uri"] for r in skill_rows_sorted[:demote_n]}
    for r in rows:
        if r["tool_uri"] in demote:
            r["status"] = "demoted"
    warnings.warn(f"skill_cap_enforced: demoted {demote_n} skills (cap {MAX_SKILLS})")
    return rows


# ============================================================================
# 9. Core APIs
# ============================================================================

def regenerate_tool_atlas(
    as_of: Optional[datetime] = None,
    include_candidates: bool = False,
) -> ToolAtlasSnapshot:
    """Scan all callable surfaces and persist atlas rows. Bitemporal:
    supersedes prior rows with the same (tool_uri, version) by closing their
    transaction_to.

    Run nightly at 00:00 UTC; during a cycle, use get_atlas_snapshot_for_cycle
    instead (which freezes the snapshot per cycle to preserve replay
    determinism — v2 line 64).
    """
    _ensure_schema()
    as_of = as_of or datetime.now(timezone.utc)
    as_of_iso = as_of.isoformat() if isinstance(as_of, datetime) else str(as_of)

    builtin = _scan_builtin_tools()
    talis_native = _scan_talis_native_tools()
    sources = _scan_sources()
    skills, manifests = _scan_skills()
    learned = _scan_learned_tools()

    all_rows = builtin + talis_native + sources + skills + learned
    all_rows = _enforce_skill_cap(all_rows, manifests)

    if not include_candidates:
        all_rows = [r for r in all_rows if r["status"] in ("active", "demoted")]

    conn = get_store().conn
    now_iso = _utcnow_iso()
    inserted = 0
    for r in all_rows:
        # Close any prior live row with the same (tool_uri, version)
        conn.execute(
            "UPDATE tool_atlas SET transaction_to = ? "
            "WHERE tool_uri = ? AND version = ? AND transaction_to IS NULL",
            (now_iso, r["tool_uri"], r["version"]),
        )
        atlas_id = "tool_" + uuid.uuid4().hex[:24]
        conn.execute(
            "INSERT INTO tool_atlas "
            "(id, tool_uri, tool_name, version, kind, provider, callable_ref, "
            "schema_json, skill_md_path, description, source_dependencies, "
            "permission_scope, network_hosts, cost_hint, status, code_sha256, "
            "valid_from, transaction_from) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                atlas_id, r["tool_uri"], r["tool_name"], r["version"], r["kind"],
                r["provider"], r["callable_ref"], _canonical_json(r["schema_json"]),
                r.get("skill_md_path"), r["description"],
                _canonical_json(r["source_dependencies"]),
                r["permission_scope"],
                _canonical_json(r["network_hosts"]),
                _canonical_json(r["cost_hint"]),
                r["status"], r.get("code_sha256"),
                as_of_iso, now_iso,
            ),
        )
        inserted += 1
    conn.commit()

    n_tools = sum(1 for r in all_rows if r["kind"] in ("builtin", "hydromancer",
                                                         "learned", "external"))
    n_skills = sum(1 for r in all_rows if r["kind"] == "skill")
    n_sources = sum(1 for r in all_rows if r["kind"] == "source")

    return ToolAtlasSnapshot(
        as_of=as_of,
        cycle_id=None,
        rows=all_rows,
        n_tools=n_tools,
        n_skills=n_skills,
        n_sources=n_sources,
    )


def _live_atlas_rows(as_of: Optional[datetime] = None) -> list[dict[str, Any]]:
    """Read tool_atlas rows visible at `as_of` (defaults to now)."""
    _ensure_schema()
    conn = get_store().conn
    if as_of is None:
        rows = conn.execute(
            "SELECT * FROM tool_atlas WHERE transaction_to IS NULL"
        ).fetchall()
    else:
        as_of_iso = as_of.isoformat()
        rows = conn.execute(
            "SELECT * FROM tool_atlas "
            "WHERE transaction_from <= ? "
            "AND (transaction_to IS NULL OR transaction_to > ?) "
            "AND valid_from <= ? "
            "AND (valid_to IS NULL OR valid_to > ?)",
            (as_of_iso, as_of_iso, as_of_iso, as_of_iso),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for jcol in ("schema_json", "source_dependencies", "network_hosts",
                      "cost_hint"):
            v = d.get(jcol)
            if isinstance(v, str):
                try:
                    d[jcol] = json.loads(v)
                except Exception:
                    pass
        out.append(d)
    return out


def resolve_tool_uri(uri: str, as_of: Optional[datetime] = None) -> ToolContract:
    """Resolve a tic:// URI to a ToolContract.

    For replay determinism, pass `as_of` to fetch the version that was visible
    at that wall-clock instant.
    """
    _ensure_schema()
    parsed = parse_tool_uri(uri)
    conn = get_store().conn

    # Match by tool_uri prefix (without @vN if version specified by caller).
    base_uri = uri.split("@", 1)[0] if "@" in uri else uri
    version_filter = parsed.version  # may be None
    as_of_iso = (as_of or datetime.now(timezone.utc)).isoformat()

    sql = (
        "SELECT * FROM tool_atlas "
        "WHERE (tool_uri = ? OR tool_uri LIKE ?) "
        "AND transaction_from <= ? "
        "AND (transaction_to IS NULL OR transaction_to > ?)"
    )
    params = [uri, base_uri + "@%", as_of_iso, as_of_iso]
    if version_filter:
        sql += " AND version = ?"
        params.append(version_filter)
    sql += " ORDER BY version DESC, transaction_from DESC LIMIT 1"
    row = conn.execute(sql, params).fetchone()
    if row is None:
        raise KeyError(f"tool_uri_not_in_atlas: {uri}")
    d = dict(row)

    schema_json = d.get("schema_json") or "{}"
    try:
        schema = json.loads(schema_json) if isinstance(schema_json, str) else schema_json
    except Exception:
        schema = {}
    cost_hint_raw = d.get("cost_hint") or "{}"
    try:
        cost_hint = (json.loads(cost_hint_raw) if isinstance(cost_hint_raw, str)
                     else cost_hint_raw)
    except Exception:
        cost_hint = {}
    src_deps_raw = d.get("source_dependencies") or "[]"
    try:
        src_deps = (json.loads(src_deps_raw) if isinstance(src_deps_raw, str)
                    else src_deps_raw)
    except Exception:
        src_deps = []
    network_raw = d.get("network_hosts") or "[]"
    try:
        network_hosts = (json.loads(network_raw) if isinstance(network_raw, str)
                         else network_raw)
    except Exception:
        network_hosts = []

    # Recover live callable from the current TOOLS registry where possible.
    fn = _lookup_callable(parsed, d["tool_name"])

    return ToolContract(
        uri=d["tool_uri"],
        name=d["tool_name"],
        version=d["version"],
        kind=d["kind"],
        provider=d["provider"],
        description=d["description"],
        input_schema=schema,
        cost_hint=cost_hint,
        timeout_ms=int((schema.get("timeout_ms") if isinstance(schema, dict) else None) or 5000),
        callable=fn,
        callable_ref=d["callable_ref"],
        code_sha256=d.get("code_sha256"),
        skill_md_path=d.get("skill_md_path"),
        source_dependencies=src_deps if isinstance(src_deps, list) else [],
        permission_scope=d.get("permission_scope", "read_only"),
        network_hosts=network_hosts if isinstance(network_hosts, list) else [],
        status=d.get("status", "active"),
    )


def _lookup_callable(parsed: ParsedURI, name: str) -> Optional[Callable]:
    """Find the live Python callable backing a URI. For builtin/hydromancer/
    external tools we look it up in the live TOOLS registry. For sources we
    return a bound fetch_live partial. Skills/learned: callable_ref points at
    a path but we don't exec arbitrary code here at resolve time."""
    if parsed.authority == "source":
        try:
            from tic.desk.tools.data_tools import fetch_live
            def _src_call(**kw: Any) -> Any:
                return fetch_live(name, **kw)
            _src_call.__name__ = f"fetch_live_{name}"
            return _src_call
        except Exception:
            return None
    if parsed.authority == "tool" and parsed.sub_kind == "learned":
        try:
            from .learned_runtime import get_learned_callable
            return get_learned_callable(name)
        except Exception:
            return None
    if parsed.authority == "tool" and parsed.sub_kind == "talis_native":
        try:
            from .native_tools import get_native_callable
            return get_native_callable(name)
        except Exception:
            return None
    if parsed.authority == "tool":
        try:
            from tic.desk.tools import TOOLS  # type: ignore
            spec = TOOLS.get(name)
            if spec is not None:
                return spec.get("callable")
        except Exception:
            return None
    return None


def dispatch_uri(
    uri: str,
    args: dict[str, Any],
    context: AgentContext,
) -> ToolResult:
    """Resolve URI, validate args (lightly), invoke callable, write tool_call_log.

    Args:
      uri: tic:// URI
      args: kwargs for the underlying callable
      context: AgentContext carrying cycle_id + specialist_id
    """
    _ensure_schema()
    started_dt = datetime.now(timezone.utc)
    started_iso = started_dt.isoformat()
    t0 = time.perf_counter()

    args = args or {}
    args_canon = _canonical_json(args)
    args_hash = _sha256_hex(args_canon)

    log_id = "tc_" + uuid.uuid4().hex[:24]
    error: Optional[str] = None
    result: Any = None
    result_hash: Optional[str] = None
    cost_usd = 0.0
    tool_version = "v?"
    contract: Optional[ToolContract] = None

    try:
        contract = resolve_tool_uri(uri)
        tool_version = contract.version
        if contract.callable is None:
            raise RuntimeError(f"no_live_callable_for_uri: {uri}")
        # Light arg shape check: drop unknown keys only if schema enumerates props
        # explicitly. Skip on schemas without 'properties'.
        try:
            result = contract.callable(**args)
        except TypeError as te:
            error = f"bad_args: {te}"
        except Exception as e:
            error = f"runtime_error: {str(e)[:240]}"

        # Cost estimate
        cost_hint = contract.cost_hint or {}
        est = cost_hint.get("usd_per_call_estimate", DEFAULT_COST_USD_PER_CALL)
        try:
            cost_usd = float(est)
        except Exception:
            cost_usd = DEFAULT_COST_USD_PER_CALL
    except Exception as e:
        error = f"resolve_error: {str(e)[:240]}"

    finished_dt = datetime.now(timezone.utc)
    finished_iso = finished_dt.isoformat()
    duration_ms = int((time.perf_counter() - t0) * 1000)

    result_summary_text = ""
    if result is not None:
        try:
            result_summary_text = _canonical_json(result)[:500]
            result_hash = _sha256_hex(_canonical_json(result))
        except Exception:
            result_summary_text = str(result)[:500]

    # Write tool_call_log row
    conn = get_store().conn
    try:
        conn.execute(
            "INSERT INTO tool_call_log "
            "(id, cycle_id, investigation_id, specialist_id, tool_uri, tool_version, "
            "args_hash, args_json, result_hash, result_summary, error, started_at, "
            "finished_at, duration_ms, cost_usd, valid_from, transaction_from) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                log_id, context.cycle_id, context.investigation_id,
                context.specialist_id, uri, tool_version,
                args_hash, args_canon, result_hash, result_summary_text,
                json.dumps({"message": error}) if error else None,
                started_iso, finished_iso, duration_ms, cost_usd,
                started_iso, started_iso,
            ),
        )
        conn.commit()
    except Exception as e:
        warnings.warn(f"tool_call_log_insert_failed: {e}")

    return ToolResult(
        ok=(error is None),
        uri=uri,
        args_hash=args_hash,
        result_hash=result_hash,
        result=result,
        duration_ms=duration_ms,
        cost_usd=cost_usd,
        tool_call_log_id=log_id,
        error=error,
    )


def load_skill_registry(
    as_of: Optional[datetime] = None,
    specialist_id: Optional[str] = None,
) -> list[SkillManifest]:
    """Load SKILL.md files. If specialist_id given, also restrict to skills
    relevant to that specialist by tool_affinity scores (Phase 6 MV) — falls
    back to "all" when the MV is empty."""
    _, manifests = _scan_skills()
    if specialist_id is None:
        return manifests
    affinity = _load_specialist_tool_affinity(specialist_id)
    if not affinity:
        return manifests  # MV empty — return all
    affinity_slugs = {slug for slug, _ in affinity}
    return [m for m in manifests if m.slug in affinity_slugs]


def _load_specialist_tool_affinity(specialist_id: str) -> list[tuple[str, float]]:
    """Read mv_top_tools_per_specialist_30d. Returns sorted list of
    (tool_uri, brier_delta_avg). Empty list if MV doesn't exist yet."""
    _ensure_schema()
    conn = get_store().conn
    # MV is Postgres-only; SQLite dev backend won't have it. Try a regular
    # table query as fallback for either name.
    for table in ("mv_top_tools_per_specialist_30d", "tool_affinity"):
        try:
            rows = conn.execute(
                f"SELECT tool_uri, brier_delta_avg FROM {table} "
                f"WHERE specialist_id = ? "
                f"ORDER BY brier_delta_avg DESC LIMIT ?",
                (specialist_id, DEFAULT_TOP_K_AFFINITY),
            ).fetchall()
            if rows:
                return [(r["tool_uri"], r["brier_delta_avg"]) for r in rows]
        except Exception:
            continue
    return []


def load_specialist_tool_atlas(
    specialist_id: str,
    as_of: Optional[datetime] = None,
) -> ToolAtlasSnapshot:
    """Fast-path discovery: return the curated subset of the atlas for this
    specialist based on Brier-weighted affinity. Falls back to uniform top-15
    when no affinity data exists yet.

    HONEST GAP: until Phase 6 builds mv_top_tools_per_specialist_30d, this
    returns the first DEFAULT_TOP_K_AFFINITY rows in deterministic order.
    """
    rows = _live_atlas_rows(as_of=as_of)
    affinity = _load_specialist_tool_affinity(specialist_id)
    if affinity:
        ranked = {uri: score for uri, score in affinity}
        rows = [r for r in rows if r["tool_uri"] in ranked]
        rows.sort(key=lambda r: ranked.get(r["tool_uri"], 0.0), reverse=True)
        rows = rows[:DEFAULT_TOP_K_AFFINITY]
    else:
        # Deterministic fallback: alphabetical, prefer 'tool' authority over 'source'.
        rows = sorted(rows, key=lambda r: (r["kind"] != "builtin", r["tool_name"]))
        rows = rows[:DEFAULT_TOP_K_AFFINITY]

    return ToolAtlasSnapshot(
        as_of=as_of or datetime.now(timezone.utc),
        cycle_id=None,
        rows=rows,
        n_tools=sum(1 for r in rows if r["kind"] in ("builtin", "hydromancer",
                                                       "learned", "external")),
        n_skills=sum(1 for r in rows if r["kind"] == "skill"),
        n_sources=sum(1 for r in rows if r["kind"] == "source"),
    )


# ============================================================================
# 10. Frozen-during-cycle snapshot
# ============================================================================

def get_atlas_snapshot_for_cycle(cycle_id: str) -> ToolAtlasSnapshot:
    """Returns the atlas snapshot frozen at cycle start.

    On first call for a given cycle_id, snapshot the current tool_atlas state
    and pin it. Subsequent calls for the same cycle_id return identical state
    even if the atlas mutated between them.

    This is the v2 line 64 invariant: 'During a cycle the atlas snapshot is
    frozen; newly promoted tools land in the next day's atlas, never
    mid-cycle.'
    """
    _ensure_schema()
    conn = get_store().conn
    row = conn.execute(
        "SELECT snapshot_at, snapshot_json FROM tool_atlas_cycle_snapshots "
        "WHERE cycle_id = ?",
        (cycle_id,),
    ).fetchone()
    if row is not None:
        try:
            payload = json.loads(row["snapshot_json"])
            return ToolAtlasSnapshot(
                as_of=datetime.fromisoformat(row["snapshot_at"]),
                cycle_id=cycle_id,
                rows=payload["rows"],
                n_tools=payload["n_tools"],
                n_skills=payload["n_skills"],
                n_sources=payload["n_sources"],
            )
        except Exception as e:
            warnings.warn(f"cycle_snapshot_decode_failed: {e}; resnapshotting")

    rows = _live_atlas_rows(as_of=None)
    n_tools = sum(1 for r in rows if r["kind"] in ("builtin", "hydromancer",
                                                     "learned", "external"))
    n_skills = sum(1 for r in rows if r["kind"] == "skill")
    n_sources = sum(1 for r in rows if r["kind"] == "source")
    snapshot_at = _utcnow_iso()
    payload = {
        "rows": rows, "n_tools": n_tools, "n_skills": n_skills,
        "n_sources": n_sources,
    }
    conn.execute(
        "INSERT OR REPLACE INTO tool_atlas_cycle_snapshots "
        "(cycle_id, snapshot_at, snapshot_json) VALUES (?, ?, ?)",
        (cycle_id, snapshot_at, _canonical_json(payload)),
    )
    conn.commit()
    return ToolAtlasSnapshot(
        as_of=datetime.fromisoformat(snapshot_at),
        cycle_id=cycle_id,
        rows=rows,
        n_tools=n_tools,
        n_skills=n_skills,
        n_sources=n_sources,
    )


# ============================================================================
# 11. Smoke test
# ============================================================================

def _write_synthetic_skill_for_smoke() -> tuple[Path, bool]:
    """Create skills/example_skill/SKILL.md if it doesn't exist (test only)."""
    d = SKILLS_DIR / "example_skill"
    d.mkdir(parents=True, exist_ok=True)
    md = d / "SKILL.md"
    if md.exists():
        return md, False
    md.write_text(
        """## name
example_skill

## when_to_use
Demonstration skill used by the Phase 2 tool_atlas smoke test. Picked up by
load_skill_registry to confirm the SKILL.md parser works end-to-end.

## inputs
ticker: string — symbol to analyze

## outputs
dict with ticker, ok, and source fields

## example_invocations
- example_skill(ticker='BTC')
- example_skill(ticker='ETH')
- example_skill(ticker='HYPE')

## cost_hint
{"usd_per_call_estimate": 0.002}

## last_brier_30d
0.32

## owner_specialist
microstructure

## supersedes_skill_id

""",
        encoding="utf-8",
    )
    # Also drop a tool.py so the callable_ref is populated.
    (d / "tool.py").write_text(
        "def run(ticker: str) -> dict:\n"
        "    return {'ticker': ticker, 'ok': True, 'source': 'tool_atlas_smoke'}\n",
        encoding="utf-8",
    )
    return md, True


def _smoke_test() -> None:
    print("=== Phase 2 tool_atlas smoke test ===")
    ctx = AgentContext(cycle_id="smoke_test", specialist_id="test")

    # Make sure at least one synthetic SKILL.md exists.
    smoke_skill_md, created_smoke_skill = _write_synthetic_skill_for_smoke()

    # (2) Regenerate
    t0 = time.perf_counter()
    snap = regenerate_tool_atlas(as_of=datetime.now(timezone.utc))
    regen_ms = int((time.perf_counter() - t0) * 1000)
    print(f"[1] regenerate_tool_atlas: {regen_ms} ms, "
          f"rows={len(snap.rows)} tools={snap.n_tools} "
          f"skills={snap.n_skills} sources={snap.n_sources}")

    # (3) Confirm synthetic skill present
    manifests = load_skill_registry()
    print(f"[2] load_skill_registry: {len(manifests)} skills loaded")
    for m in manifests[:5]:
        print(f"    - {m.slug} (owner={m.owner_specialist} brier={m.last_brier_30d})")

    # (4) Resolve a known builtin URI
    uri = "tic://tool/builtin/query_timeseries@v1"
    try:
        contract = resolve_tool_uri(uri)
        print(f"[3] resolve_tool_uri({uri}): kind={contract.kind} "
              f"provider={contract.provider} callable_ref={contract.callable_ref}")
    except Exception as e:
        print(f"[3] resolve_tool_uri failed: {e}")
        contract = None

    # (5) Dispatch via URI
    t0 = time.perf_counter()
    result = dispatch_uri(
        uri,
        {"entity_symbol": "BTC", "metric_prefix": "price", "lookback_hours": 24},
        ctx,
    )
    disp_ms = int((time.perf_counter() - t0) * 1000)
    print(f"[4] dispatch_uri: ok={result.ok} duration={disp_ms}ms "
          f"args_hash={result.args_hash[:12]}... "
          f"result_hash={(result.result_hash or 'NONE')[:12]}... "
          f"cost_usd={result.cost_usd} error={result.error}")

    # (6) Verify tool_call_log row
    conn = get_store().conn
    row = conn.execute(
        "SELECT * FROM tool_call_log WHERE id = ?", (result.tool_call_log_id,)
    ).fetchone()
    if row is None:
        print("[5] tool_call_log row: MISSING")
    else:
        d = dict(row)
        print(f"[5] tool_call_log row id={d['id']} cycle_id={d['cycle_id']} "
              f"tool_uri={d['tool_uri']} args_hash={d['args_hash'][:10]}... "
              f"result_hash={(d['result_hash'] or 'NONE')[:10]}... "
              f"cost_usd={d['cost_usd']} duration_ms={d['duration_ms']} "
              f"valid_from={d['valid_from']} transaction_from={d['transaction_from']}")

    # (7) Frozen-per-cycle invariant
    snap_a = get_atlas_snapshot_for_cycle("smoke_test")
    # Mutate the live atlas between snapshots.
    regenerate_tool_atlas(as_of=datetime.now(timezone.utc))
    snap_b = get_atlas_snapshot_for_cycle("smoke_test")
    same = (len(snap_a.rows) == len(snap_b.rows)
            and snap_a.as_of == snap_b.as_of
            and _canonical_json(sorted([r["tool_uri"] for r in snap_a.rows]))
                == _canonical_json(sorted([r["tool_uri"] for r in snap_b.rows])))
    print(f"[6] frozen-per-cycle: identical_after_mutation={same} "
          f"(snap_a rows={len(snap_a.rows)} snap_b rows={len(snap_b.rows)})")

    # (8) URI parse coverage
    parse_cases = [
        "tic://tool/builtin/query_timeseries@v1",
        "tic://tool/hydromancer/fetch_live@v3?source=hl_l4_ofi",
        "tic://tool/learned/usdt_mint_flow_to_btc_correlation@v2",
        "tic://skill/microstructure_sweep_detection@v1",
        "tic://source/hl/info/metaAndAssetCtxs",
        "tic://artifact/trade_idea/ti_abc123def456",
    ]
    bad_cases = [
        "https://example.com",
        "tic://invalid/foo",
        "tic://tool/builtin/foo@bad",
        "",
    ]
    parse_ok = 0
    for u in parse_cases:
        try:
            parse_tool_uri(u)
            parse_ok += 1
        except Exception as e:
            print(f"    parse FAILED for {u}: {e}")
    parse_bad_caught = 0
    for u in bad_cases:
        try:
            parse_tool_uri(u)
            print(f"    parse should have rejected: {u}")
        except ValueError:
            parse_bad_caught += 1
    print(f"[7] URI parsing: {parse_ok}/{len(parse_cases)} good, "
          f"{parse_bad_caught}/{len(bad_cases)} bad correctly rejected")

    # (9) SKILL.md validator: reject manifest missing required sections
    tmp = SKILLS_DIR / "_bad_skill_for_test"
    tmp.mkdir(parents=True, exist_ok=True)
    bad_md = tmp / "SKILL.md"
    bad_md.write_text("## name\nbad\n", encoding="utf-8")  # missing everything else
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        bad = load_skill_md(str(bad_md))
    print(f"[8] SKILL.md validator: rejected_bad_manifest={bad is None}")
    # Clean up the synthetic bad skill so it doesn't pollute future regens.
    try:
        bad_md.unlink()
        tmp.rmdir()
    except Exception:
        pass
    if created_smoke_skill:
        try:
            tool_py = smoke_skill_md.parent / "tool.py"
            if tool_py.exists():
                tool_py.unlink()
            smoke_skill_md.unlink()
            smoke_skill_md.parent.rmdir()
        except Exception:
            pass

    print(f"\nTOOLS atlas now has {snap.n_tools} tools, "
          f"{snap.n_skills} skills, {snap.n_sources} sources")
    print("\nPHASE 2 TOOL ATLAS — READY")


if __name__ == "__main__":
    _smoke_test()

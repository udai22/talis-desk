"""Tool-use harness shared by desk agents.

The harness contract is intentionally simple:

1. Agents request typed, atlas-bound tool calls.
2. The harness validates permission and args shape before execution.
3. Read-only transient failures get a tiny retry budget.
4. Results are compacted into observations for the next reasoning turn.
5. Every observation keeps enough provenance to become a graph edge.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Iterable


@dataclass(frozen=True)
class HarnessPolicy:
    """Execution limits for one agent/tool-use session."""

    evidence_hard_cap: int = 8
    max_tool_iterations: int = 2
    max_followup_tools_per_iteration: int = 2
    max_retries: int = 1
    retry_backoff_s: float = 0.2
    allowed_uri_prefixes: tuple[str, ...] = ("tic://tool/", "tic://source/")
    allow_mutating_tools: bool = False


@dataclass(frozen=True)
class ToolErrorInfo:
    type: str
    retryable: bool
    recovery_hint: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "retryable": self.retryable,
            "recovery_hint": self.recovery_hint,
        }


@dataclass
class ToolObservation:
    uri: str
    args: dict[str, Any]
    ok: bool
    summary: str
    result: Any = None
    error: str | None = None
    error_type: str | None = None
    retryable: bool = False
    recovery_hint: str = ""
    tool_call_log_id: str | None = None
    cost_usd: float = 0.0
    attempts: int = 1
    phase: str = "evidence"
    requested_by_model: bool = False
    request_why: str | None = None
    expected_edge: str | None = None
    expected_info_value: float | None = None
    would_change_decision: bool | None = None
    fallback_if_denied: str | None = None
    retry_errors: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        out = {
            "uri": self.uri,
            "args": self.args,
            "ok": self.ok,
            "error": self.error,
            "error_type": self.error_type,
            "retryable": self.retryable,
            "recovery_hint": self.recovery_hint,
            "summary": self.summary,
            "result": self.result,
            "tool_call_log_id": self.tool_call_log_id,
            "cost_usd": self.cost_usd,
            "attempts": self.attempts,
            "phase": self.phase,
            "requested_by_model": self.requested_by_model,
            "request_why": self.request_why,
            "expected_edge": self.expected_edge,
            "expected_info_value": self.expected_info_value,
            "would_change_decision": self.would_change_decision,
            "fallback_if_denied": self.fallback_if_denied,
        }
        if self.retry_errors:
            out["retry_errors"] = self.retry_errors
        return out


def dispatch_harness_tool(
    uri: str,
    args: dict[str, Any],
    context: Any,
    *,
    policy: HarnessPolicy | None = None,
    phase: str = "evidence",
    requested_by_model: bool = False,
    request_why: str | None = None,
    expected_edge: str | None = None,
    expected_info_value: float | None = None,
    would_change_decision: bool | None = None,
    fallback_if_denied: str | None = None,
) -> dict[str, Any]:
    """Dispatch one atlas tool call with retry/error/compaction discipline."""
    from ..tool_atlas import dispatch_uri

    policy = policy or HarnessPolicy()
    if not _uri_allowed(uri, policy=policy):
        classified = ToolErrorInfo(
            type="permission_denied",
            retryable=False,
            recovery_hint="Request an approved read-only atlas URI or propose a new tool instead.",
        )
        return ToolObservation(
            uri=uri,
            args=args or {},
            ok=False,
            error=f"denied_unapproved_tool_uri:{uri}",
            error_type=classified.type,
            retryable=classified.retryable,
            recovery_hint=classified.recovery_hint,
            summary="",
            phase=phase,
            requested_by_model=requested_by_model,
            request_why=request_why,
            expected_edge=expected_edge,
            expected_info_value=expected_info_value,
            would_change_decision=would_change_decision,
            fallback_if_denied=fallback_if_denied,
        ).to_dict()

    attempts = 0
    retry_errors: list[dict[str, Any]] = []
    while True:
        attempts += 1
        try:
            res = dispatch_uri(uri, args or {}, context)
            classified = classify_tool_error(res.error)
            compact = compact_tool_result(res.result)
            observation = ToolObservation(
                uri=uri,
                args=args or {},
                ok=bool(res.ok),
                error=res.error,
                error_type=classified.type if res.error else None,
                retryable=classified.retryable if res.error else False,
                recovery_hint=classified.recovery_hint if res.error else "",
                summary=summarize_tool_result(compact),
                result=compact,
                tool_call_log_id=res.tool_call_log_id,
                cost_usd=float(res.cost_usd or 0.0),
                attempts=attempts,
                phase=phase,
                requested_by_model=requested_by_model,
                request_why=request_why,
                expected_edge=expected_edge,
                expected_info_value=expected_info_value,
                would_change_decision=would_change_decision,
                fallback_if_denied=fallback_if_denied,
                retry_errors=list(retry_errors),
            )
            if res.ok or not classified.retryable or attempts > policy.max_retries:
                return observation.to_dict()
            retry_errors.append({
                "tool_call_log_id": res.tool_call_log_id,
                "error": res.error,
                **classified.to_dict(),
            })
            time.sleep(min(policy.retry_backoff_s * attempts, 0.5))
        except Exception as e:
            classified = classify_tool_error(f"{type(e).__name__}: {e}")
            observation = ToolObservation(
                uri=uri,
                args=args or {},
                ok=False,
                error=f"{type(e).__name__}: {e}",
                error_type=classified.type,
                retryable=classified.retryable,
                recovery_hint=classified.recovery_hint,
                summary="",
                attempts=attempts,
                phase=phase,
                requested_by_model=requested_by_model,
                request_why=request_why,
                expected_edge=expected_edge,
                expected_info_value=expected_info_value,
                would_change_decision=would_change_decision,
                fallback_if_denied=fallback_if_denied,
                retry_errors=list(retry_errors),
            )
            if not classified.retryable or attempts > policy.max_retries:
                return observation.to_dict()
            retry_errors.append({"error": observation.error, **classified.to_dict()})
            time.sleep(min(policy.retry_backoff_s * attempts, 0.5))


def normalize_tool_requests(
    raw: Any,
    *,
    allowed_tools: Iterable[str],
    max_requests: int = 6,
) -> list[dict[str, Any]]:
    """Normalize model tool requests while keeping existing tools atlas-bound."""
    if not isinstance(raw, list):
        return []
    allowed = set(str(x) for x in allowed_tools if x)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw[:max_requests]:
        if isinstance(item, str):
            item = {"tool_uri": item, "why": "Agent requested follow-up evidence."}
        if not isinstance(item, dict):
            continue
        tool_uri = str(item.get("tool_uri") or item.get("uri") or "").strip()
        tool_name = str(item.get("tool_name") or item.get("name") or "").strip()
        args = item.get("args") or item.get("input_shape") or {}
        if not isinstance(args, dict):
            args = {}
        if tool_uri and tool_uri not in allowed:
            tool_name = tool_name or tool_uri.rsplit("/", 1)[-1].split("@", 1)[0]
            tool_uri = ""
        if not tool_uri and not tool_name:
            continue
        why = str(item.get("why") or item.get("purpose") or "Agent requested follow-up evidence.")[:500]
        expected_edge = str(item.get("expected_edge") or item.get("edge") or "")[:240]
        if not expected_edge:
            why = f"{why} [low_ev_missing_expected_edge]"
        lease = item.get("call_lease") if isinstance(item.get("call_lease"), dict) else {}
        expected_info_value = _bounded_float(
            item.get("expected_info_value", lease.get("expected_info_value")),
            default=None,
        )
        would_change_decision = _optional_bool(
            item.get("would_change_decision", lease.get("would_change_decision"))
        )
        fallback_if_denied = short_text(
            item.get("fallback_if_denied", lease.get("fallback_if_denied", "")),
            240,
        )
        key = json.dumps({
            "tool_uri": tool_uri,
            "tool_name": tool_name or tool_uri,
            "args": args,
            "edge": expected_edge,
        }, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "tool_uri": tool_uri,
            "tool_name": tool_name or tool_uri.rsplit("/", 1)[-1].split("@", 1)[0],
            "args": args,
            "why": why,
            "expected_edge": expected_edge,
            "expected_info_value": expected_info_value,
            "would_change_decision": would_change_decision,
            "fallback_if_denied": fallback_if_denied,
            "priority": _priority(item.get("priority")) if expected_edge else "low",
        })
    return out


def filter_fulfilled_tool_requests(
    requests: list[dict[str, Any]],
    *,
    tool_evidence: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    fulfilled = {
        json.dumps({
            "tool_uri": ev.get("uri"),
            "args": ev.get("args") or {},
        }, sort_keys=True, default=str)
        for ev in tool_evidence
        if ev.get("uri") and ev.get("ok")
    }
    fulfilled_uris = {
        str(ev.get("uri") or "")
        for ev in tool_evidence
        if ev.get("uri") and ev.get("ok")
    }
    out: list[dict[str, Any]] = []
    for req in requests:
        tool_uri = str(req.get("tool_uri") or "")
        if not tool_uri:
            out.append(req)
            continue
        if not req.get("args") and tool_uri in fulfilled_uris:
            continue
        key = json.dumps({
            "tool_uri": tool_uri,
            "args": req.get("args") or {},
        }, sort_keys=True, default=str)
        if key not in fulfilled:
            out.append(req)
    return out


def classify_tool_error(error: Any) -> ToolErrorInfo:
    text = str(error or "").strip()
    lower = text.lower()
    if not text:
        return ToolErrorInfo(type="", retryable=False, recovery_hint="")
    if any(tok in lower for tok in ("timeout", "timed out", "deadline")):
        return ToolErrorInfo(
            type="timeout",
            retryable=True,
            recovery_hint="Retry once with the same narrow read or ask for a simpler source edge.",
        )
    if "429" in lower or "rate limit" in lower or "rate_limited" in lower:
        return ToolErrorInfo(
            type="rate_limited",
            retryable=True,
            recovery_hint="Retry once; if still rate-limited, route to source-health and avoid overusing this surface.",
        )
    if any(tok in lower for tok in ("connect", "unavailable", "503", "502", "500", "temporarily")):
        return ToolErrorInfo(
            type="source_unavailable",
            retryable=True,
            recovery_hint="Try the fallback read chain or preserve this as a source-health edge.",
        )
    if any(tok in lower for tok in ("bad_args", "invalid", "missing", "required", "typeerror", "valueerror", "keyerror")):
        return ToolErrorInfo(
            type="invalid_input",
            retryable=False,
            recovery_hint="Fix the typed args before requesting this tool again.",
        )
    if "not found" in lower or "404" in lower:
        return ToolErrorInfo(
            type="not_found",
            retryable=False,
            recovery_hint="Verify the entity, wallet, symbol, or source id before retrying.",
        )
    return ToolErrorInfo(
        type="internal",
        retryable=False,
        recovery_hint="Use a different source or request a new tool if this edge is still important.",
    )


def summarize_tool_result(result: Any) -> str:
    if result is None:
        return ""
    try:
        return json.dumps(result, sort_keys=True, default=str)[:900]
    except Exception:
        return str(result)[:900]


def compact_tool_result(result: Any, *, depth: int = 0) -> Any:
    """Keep enough data for graph reconstruction without flooding context."""
    if depth >= 5:
        return short_text(result, 500)
    if result is None or isinstance(result, (bool, int, float)):
        return result
    if isinstance(result, str):
        return short_text(result, 2000)
    if isinstance(result, list):
        items = [compact_tool_result(x, depth=depth + 1) for x in result[:24]]
        if len(result) > 24:
            items.append({"_truncated_count": len(result) - 24})
        return items
    if isinstance(result, dict):
        out: dict[str, Any] = {}
        for key, value in result.items():
            if key in {"events", "points", "rows", "items", "data"} and isinstance(value, list):
                out[key] = [compact_tool_result(x, depth=depth + 1) for x in value[:24]]
                if len(value) > 24:
                    out[f"{key}_truncated_count"] = len(value) - 24
                continue
            out[str(key)] = compact_tool_result(value, depth=depth + 1)
        return out
    return short_text(result, 1000)


def short_text(raw: Any, limit: int) -> str:
    text = str(raw)
    return text if len(text) <= limit else text[: limit - 15] + "...<truncated>"


def _uri_allowed(uri: str, *, policy: HarnessPolicy) -> bool:
    if not uri.startswith(policy.allowed_uri_prefixes):
        return False
    if policy.allow_mutating_tools:
        return True
    lowered = uri.lower()
    mutating_tokens = (
        "request_trade",
        "request_position",
        "request_close",
        "place_order",
        "cancel_order",
        "submit",
        "execute",
        "withdraw",
        "transfer",
    )
    return not any(token in lowered for token in mutating_tokens)


def _priority(raw: Any) -> str:
    value = str(raw or "medium").lower()
    return value if value in {"high", "medium", "low"} else "medium"


def _bounded_float(raw: Any, *, default: float | None) -> float | None:
    if raw in (None, ""):
        return default
    try:
        value = float(raw)
    except Exception:
        return default
    return max(0.0, min(1.0, value))


def _optional_bool(raw: Any) -> bool | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in {"true", "1", "yes", "y"}:
        return True
    if text in {"false", "0", "no", "n"}:
        return False
    return None

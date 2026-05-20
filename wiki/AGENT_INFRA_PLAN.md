# Talis Daily Agent Infrastructure Plan

This plan extends the frozen Layer 1 v1.0 surface into a persistent research-desk runtime: specialists keep memory across days, build tools that compound, communicate durably, execute real Python in Modal sandboxes, and remain replayable under one point-in-time clock.

Layer 1 already gives the desk 86 data sources, 47 registered tools, `source_health`, `quality_flags`, Confidence 7-level, time-machine reads, contextual retrieval, hybrid RRF semantic search, and the subprocess `run_code` cap. This plan does not replace that. It adds a new agent-native layer around it with additive tables and bitemporal slices.

## 1. System diagram (ASCII)

```text
                           Talis Daily cycle runner
                                      |
       +------------------------------+------------------------------+
       |                              |                              |
+------v------+               +-------v-------+              +-------v-------+
| specialist  |               | specialist    |              | specialist    |
| macro_regime|               | smart_money   |              | semis         |
+------+------+               +-------+-------+              +-------+-------+
       |                              |                              |
       | pre-run hydration            | durable messages             | tool calls
       |                              |                              |
+------v------------------------------v------------------------------v------+
| Agent-native control plane                                               |
|                                                                          |
|  tic/desk/specialist_state.py     tic/agent_native/messages.py           |
|  - append-only state rows         - durable topic/direct message bus      |
|  - open hypotheses                - request_review / devil's advocate     |
|  - priors / outcomes              - delivery confirmation                 |
|                                                                          |
|  tic/agent_native/learned_tools.py                                        |
|  - tool manifests, versions, Brier gate, registry as-of                   |
+---------+----------------------------+---------------------------+---------+
          |                            |                           |
          | read/query via TIC APIs    | TOOLS merge               | sandbox spawn
          |                            |                           |
+---------v---------+        +---------v----------+       +--------v---------+
| TIC store          |        | Tool registry       |       | Modal sandbox    |
| SQLite/Postgres    |        | built-in + learned  |       | per specialist   |
| claims/events/TS   |        | approved + candidate|       | per cycle        |
| forecasts/artifacts|        | valid_from/valid_to |       | network-limited  |
| source_health      |        +---------+----------+       +--------+---------+
| semantic_index     |                  |                           |
+---------+---------+                  | imports                   |
          |                            |                           |
          | append-only writes outside |                           |
          | sandbox only               |                           |
          |                            |                           |
+---------v----------------------------v---------------------------v---------+
| Modal persistent volume: /mnt/talis_learned_tools                         |
| learned_tools/<slug>/v<N>/{tool.py, manifest.json, tests/, history.json}  |
| single mount, pre-baked scientific image, no per-tool install             |
+---------------------------------------------------------------------------+
```

## 2. Modal sandbox runtime

File: `tic/desk/tools/run_code_modal.py`.

The current `tic/desk/tools/run_code.py` is the correct local contract: subprocess isolation, read-only TIC DB, `get_ts/get_claims/get_events/list_entities`, typed errors, bounded result size, and no exceptions escaping to the desk. `run_code_modal.py` should be a drop-in sibling that preserves the response envelope while lifting the 10s/512MB/no-network cap when the desk chooses the Modal backend.

Modal APIs referenced here:

- Sandboxes guide: https://modal.com/docs/guide/sandbox
- Sandbox API reference: https://modal.com/docs/reference/modal.Sandbox
- Images: https://modal.com/docs/guide/custom-container and https://modal.com/docs/reference/modal.Image
- Sandbox filesystem / volumes: https://modal.com/docs/guide/sandbox-files
- Sandbox networking: https://modal.com/docs/guide/sandbox-networking

Runtime image:

```python
image = (
    modal.Image.debian_slim(python_version="3.13")
    .uv_pip_install(
        "numpy",
        "pandas",
        "scipy",
        "statsmodels",
        "scikit-learn",
        "pyarrow",
        "httpx",
        "pydantic",
        "duckdb",
    )
    .env({
        "PYTHONUNBUFFERED": "1",
        "TALIS_SANDBOX": "1",
    })
)
```

Mounted volumes:

- `/mnt/talis_learned_tools`: Modal `Volume.from_name("talis-learned-tools", create_if_missing=True)`. Contains importable `learned_tools/<slug>/v<N>/tool.py` snapshots and a generated compatibility package so `import learned_tools.<slug>` resolves in under 1s.
- `/mnt/talis_inputs`: read-only cycle input bundle generated by the runner. Includes `cycle_id`, specialist metadata, a point-in-time DB export or parquet slices, and a signed short-lived read token for the control-plane TIC read API if the DB is too large to copy.
- No prod DB mount. The sandbox never receives a writeable TIC connection.

Provisioning rule:

- The runner creates no warm pool by default.
- Each specialist gets a lazy `SandboxSession` object at the start of its draft loop.
- On first `run_code_modal` or learned-tool call, create one Modal Sandbox with the pre-baked image and volume mounts.
- Reuse that sandbox for the specialist's cycle. Tool calls execute via `sb.exec("python", "/opt/talis/sandbox_runner.py", ...)` or a long-lived worker protocol if call volume justifies it.
- Tear down after the specialist finishes or after idle timeout, whichever comes first. Always call `terminate()` during cycle cleanup and tag incomplete exits.

Cost meter:

```python
class AgentCycleBudget(BaseModel):
    cycle_id: str
    specialist_id: str
    cap_usd: Decimal = Decimal("5.00")
    approved_cap_usd: Decimal = Decimal("5.00")
    spent_usd: Decimal = Decimal("0.00")
    reserved_usd: Decimal = Decimal("0.00")
    kill_at_usd: Decimal = Decimal("4.50")  # 90 percent of default cap
    extension_status: Literal["none", "requested", "approved", "denied"] = "none"
```

Every tool call gets an estimated reserve before execution and a final debit after execution. Estimates use Modal runtime seconds, CPU class, memory, and network bytes. If exact Modal cost is delayed, the runner uses pessimistic local pricing constants and reconciles later. At 90 percent of approved cap, no new sandbox call starts unless the call is explicitly marked `required_for_cleanup`. At the cap, terminate the sandbox, return a typed budget envelope, and tag any partial artifact with `quality_flags=["budget_capped"]`.

Extension:

```python
request_budget_extension(amount=10, reason: str) -> {
  "status": "approved" | "queued_for_human" | "denied",
  "approved_cap_usd": 10.0 | 5.0,
  "reason": str
}
```

The only allowed extension is `$5 -> $10`. The agent must provide a justification string. Auto-approve when both are true:

- Specialist's last 5 resolved forecasts have positive Brier improvement versus the baseline/ablation cohort.
- Cost so far this cycle has been productive: at least one tool call returned a non-empty result that was cited in an artifact, forecast, hypothesis, or message.

Otherwise the request is persisted as `budget_extension_requests(status="pending_human")`. No hidden overrun is allowed while waiting.

Network policy:

- Deny by default.
- Allowlisted external hosts:
  - `api.hyperliquid.xyz`
  - `api-ui.hyperliquid.xyz`
  - `hermes.pyth.network`
  - `benchmarks.pyth.network`
  - `api.ask-surf.ai` or the configured Surf production host
  - `api.parallel.ai` if Parallel search is intentionally delegated into the sandbox
  - `api.perplexity.ai` only if web search is intentionally delegated into the sandbox
- Allowlisted internal endpoints:
  - Read-only TIC API host, for example `talis-tic-read.<env>.internal`
  - TIC insert gateway, for structured insert APIs only, if proposal writes need to leave the sandbox
- Explicit deny:
  - RFC1918 and link-local ranges except approved internal TIC endpoints
  - AWS metadata service `169.254.169.254`
  - AWS Secrets Manager, STS, SSM, ECR, S3 prod buckets, RDS, Redis, and any Jarvis trading/execution endpoint
  - Any host not declared in a candidate learned-tool manifest

Enforcement should happen twice: a sandbox HTTP shim that agents are expected to use, plus an egress proxy / resolver allowlist at the Modal environment boundary. The shim logs every outbound request to `agent_network_audit`.

Failure envelope:

```json
{
  "result": null,
  "stdout": "",
  "runtime_ms": 9281,
  "cost_usd": 0.14,
  "error": {
    "type": "TimeoutError | OOMError | NetworkDenied | NetworkError | ExecError | BudgetExceeded | SerializationError",
    "message": "bounded text",
    "traceback": "bounded tail"
  },
  "quality_flags": ["modal_timeout"],
  "sandbox": {
    "backend": "modal",
    "sandbox_id": "sb-...",
    "image_version": "talis-agent-py313-20260520",
    "tool_snapshot_as_of": "2026-05-20T08:00:00Z"
  }
}
```

No Modal failure should crash the desk run. The specialist can degrade to `compute_stat`, built-in read tools, or a lower-confidence artifact.

## 3. Persistent tool library (`learned_tools/`)

Files:

- `tic/agent_native/learned_tools.py`
- Modal volume path: `/mnt/talis_learned_tools/learned_tools`
- Local dev mirror: `tic/agent_native/learned_tools_volume_dev/`

Directory layout:

```text
learned_tools/
  __init__.py
  usdt_mint_flow_to_btc_correlation/
    manifest.json              # latest pointer for convenience
    history.json               # append-only event log
    v1/
      tool.py
      manifest.json
      tests/
        test_cases.json
    v2/
      tool.py
      manifest.json
      tests/
        test_cases.json
```

Manifest schema:

```json
{
  "manifest_id": "ltm_...",
  "slug": "usdt_mint_flow_to_btc_correlation",
  "name": "USDT mint flow to BTC correlation",
  "signature": "def run(entity: str, lookback_days: int = 14) -> dict",
  "description": "Computes rolling correlation between USDT supply shocks and BTC forward returns.",
  "author_agent": "smart_money",
  "created_at": "2026-05-20T09:00:00Z",
  "status": "candidate",
  "brier_delta_30d": 0.0,
  "n_calls_30d": 0,
  "last_promoted_at": null,
  "version": 1,
  "supersedes_version": null,
  "declared_network_hosts": [],
  "valid_from": null,
  "valid_to": null,
  "code_sha256": "..."
}
```

Tool proposal API, callable from inside the sandbox:

```python
def propose_tool(
    slug: str,
    code: str,
    description: str,
    test_inputs: list[dict],
    test_outputs: list[dict],
    signature: str | None = None,
    declared_network_hosts: list[str] | None = None,
) -> dict:
    """Return {"manifest_id": "...", "status": "candidate", "version": 1}."""
```

The sandbox does not write the prod store or the mounted approved namespace directly. It sends the proposal to the outside TIC insert API. The control plane writes candidate files into a staging path, runs tests in a clean Modal sandbox, verifies imports and manifest shape, and then atomically appends the manifest row.

Promotion gate:

Daily cron evaluates candidates:

```sql
SELECT tool_slug, version
FROM learned_tool_call_outcomes
WHERE called_at >= now() - interval '30 days'
GROUP BY tool_slug, version
HAVING COUNT(*) FILTER (WHERE produced_forecast_id IS NOT NULL) >= 5
   AND AVG(brier_delta_vs_ablation) > 0
   AND SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END)
       FILTER (WHERE call_rank_desc <= 10) = 0;
```

The operational version in SQLite can compute the same conditions in Python. `brier_delta_vs_ablation` means: for forecasts that cited the tool output, compare resolved Brier against an ablation replay of the same specialist prompt and available store state without that tool output. Positive means the tool improved the forecast. This requires Track B forecast resolver to be running; before Track B, the cron can only record candidate telemetry, not promote.

Demotion:

- `n_calls_30d == 0`: status flips to `demoted`, `valid_to` set for registry purposes.
- `avg brier_delta_30d < 0` over at least 5 scored calls: demote as harmful.
- Any network-call candidate with undeclared or non-allowlisted host: never promote.

Auto-register hook:

`tic/desk/tools/__init__.py` already merges contract dictionaries additively. Add one more merge source:

```python
from ...agent_native.learned_tools import get_learned_tool_contracts

LEARNED_TOOL_CONTRACTS = get_learned_tool_contracts(as_of=cycle_as_of)
```

For the current import-time `TOOLS` dict, default to `as_of=now()` for normal runs. The replay runner must call `build_tools_registry(as_of=t)` rather than relying on module import state.

Versioning:

- Every approved version lives at `learned_tools/<slug>/v<N>/`.
- `manifest.json` is append-only per version; updates create a new version.
- `supersedes_version` lets agents say "use v2, which replaces v1" while old artifacts still reference v1.
- Replays use the version whose `valid_from <= as_of < valid_to`.

## 4. Durable inter-agent messaging

Do not reuse `scratchpads`. That table remains fast in-cycle chatter keyed by `cycle_id`. Add `tic/agent_native/messages.py` and an append-only `agent_messages` table for cross-cycle delivery.

Schema:

```sql
CREATE TABLE IF NOT EXISTS agent_messages (
    id TEXT PRIMARY KEY,
    from_agent TEXT NOT NULL,
    to_agent_or_topic TEXT NOT NULL,
    message_kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    posted_at TEXT NOT NULL,
    read_by TEXT NOT NULL DEFAULT '[]',
    replied_by TEXT NOT NULL DEFAULT '[]',
    expires_at TEXT,
    dedupe_key TEXT,
    day_bucket TEXT NOT NULL,
    related_artifact_id TEXT,
    related_claim_id TEXT,
    related_forecast_id TEXT,
    delivery_status TEXT NOT NULL DEFAULT 'posted'
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_messages_dedupe
ON agent_messages(from_agent, dedupe_key, day_bucket)
WHERE dedupe_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_agent_messages_route
ON agent_messages(to_agent_or_topic, posted_at);
```

Message kinds:

- `observation`
- `question`
- `cross_ref`
- `flag`
- `request_review`
- `request_devils_advocate`
- `hand_off`

Topics:

- `funding`
- `flow`
- `macro`
- `smart_money`
- `semis`
- `defi`
- `prediction_markets`
- `hl_specific`
- Agent-specific routes: `@macro_regime`, `@smart_money`, `@semis`, `@defi`, etc.

Tools:

```python
def post_message(to: str, kind: str, payload: dict, expires_in_hours: int = 72,
                 dedupe_key: str | None = None) -> dict: ...

def read_unread_messages(topic_or_self: str, limit: int = 50) -> list[dict]: ...

def mark_replied(message_id: str, reply_artifact_id: str) -> dict: ...

def request_review(other_specialist: str, draft: dict,
                   claim_under_review: dict) -> dict: ...

def request_devils_advocate(other_specialist: str, claim: dict,
                            deadline: str) -> dict: ...
```

Delivery semantics:

- At-least-once. Specialists must treat messages as idempotent.
- Dedupe key is `(from_agent, dedupe_key, day_bucket)`.
- `read_unread_messages("@macro_regime")` marks delivery but not reply.
- `mark_replied` appends to `replied_by` and links the reply artifact.
- `request_review` and `request_devils_advocate` first try synchronous in-cycle routing if the target specialist is active and the runner can schedule it within the cycle deadline. Otherwise the message is queued for the target's next cycle. The requester gets `{"status": "queued"}` and must not block indefinitely.

## 5. Day-to-day specialist continuity

File: `tic/desk/specialist_state.py`.

Specialist state is append-only, one row per `(specialist_id, day)` plus corrections via `supersedes`.

Schema:

```sql
CREATE TABLE IF NOT EXISTS specialist_state (
    id TEXT PRIMARY KEY,
    specialist_id TEXT NOT NULL,
    day TEXT NOT NULL,
    as_of TEXT NOT NULL,
    state_json TEXT NOT NULL,
    supersedes TEXT,
    created_at TEXT NOT NULL,
    source_cycle_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_specialist_state_lookup
ON specialist_state(specialist_id, day, as_of);
```

State object:

```json
{
  "open_hypotheses": [
    {
      "hypothesis_text": "USDT supply acceleration is a delayed BTC bear signal.",
      "posted_at": "2026-05-19T08:00:00Z",
      "expected_resolution_at": "2026-05-23T08:00:00Z",
      "status": "active",
      "supersedes": null
    }
  ],
  "recent_tool_calls": [
    {
      "tool_name": "compute_stat",
      "called_at": "2026-05-19T08:15:00Z",
      "summary": "BTC funding zscore +2.1",
      "outcome": "cited_in_forecast",
      "cost_usd": 0.0
    }
  ],
  "recent_forecasts": [
    {
      "forecast_id": "fc_...",
      "deadline": "2026-05-22T00:00:00Z",
      "current_brier_delta": 0.04
    }
  ],
  "unread_messages_at_start": ["msg_..."],
  "prior_beliefs": {
    "regime": "risk-off",
    "btc_funding_trend": "increasing"
  },
  "tools_it_cares_about": ["usdt_mint_flow_to_btc_correlation"]
}
```

Pre-run hydration:

When `macro_regime` starts today, the runner injects:

1. Latest non-superseded `specialist_state` row for `macro_regime` as-of the cycle clock.
2. Unread direct messages for `@macro_regime` and relevant topic messages, excluding expired messages from routing but not deleting them.
3. Yesterday's brief/artifact for the same specialist via `read_yesterday_artifact`.
4. Recent forecast outcomes and Brier deltas from Track B.
5. Newly promoted learned tools matching the specialist's declared interests, topic subscriptions, or prior calls.
6. Open hypotheses whose `expected_resolution_at` has arrived, flagged for review.

Post-run dehydration:

The specialist emits a full next state object. The control plane validates it and inserts a new row. It does not overwrite yesterday. If a prior belief changes, the emitted item must include `supersedes` or `because` so replay can show the belief transition rather than a silent mutation.

## 6. Bitemporal versioning across the new surface

The frozen schema currently has useful `as_of` envelopes, but this new surface needs consistent bitemporal slicing for tools, state, messages, and replays.

Tool registry:

```sql
CREATE TABLE IF NOT EXISTS tool_registry_versions (
    id TEXT PRIMARY KEY,
    tool_name TEXT NOT NULL,
    source TEXT NOT NULL, -- built_in | learned | candidate
    version INTEGER NOT NULL,
    status TEXT NOT NULL,
    manifest_id TEXT,
    callable_ref TEXT NOT NULL,
    schema_json TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    transaction_time TEXT NOT NULL,
    supersedes TEXT
);
CREATE INDEX IF NOT EXISTS idx_tool_registry_asof
ON tool_registry_versions(tool_name, valid_from, valid_to);
```

`query_tools_as_of(t)` returns the registry in effect at wall-clock `t`: built-ins known at that time plus learned tools whose approved versions were valid then. Candidate tools remain callable only by explicit candidate manifest id inside evaluation runs, not normal desk runs.

Learned-tool manifests:

- Append-only table plus append-only filesystem snapshots.
- Filesystem path is `learned_tools/<slug>/v<N>/`.
- A version can be demoted for future runs without deleting its files.

Specialist state:

- Rows carry `as_of`, `created_at`, and `supersedes`.
- `query_specialist_state_as_of(specialist_id, t)` picks the latest valid row whose transaction time was visible at `t`.

Agent messages:

- Rows survive forever.
- `expires_at` only affects routing and unread hydration. It does not delete history.
- Replays include messages posted and visible as-of the replay clock.

Replay rule:

Every replay takes one `as_of` and slices:

- TIC claims/events/timeseries/artifacts/forecasts
- semantic index rows visible at that time
- source health visible at that time
- tool registry as-of
- learned tool versions as-of
- specialist state as-of
- durable messages as-of

No component gets to use "latest" during replay.

## 7. Cost discipline

Defaults:

- Per-agent per-cycle budget: `$5`.
- Extension request: one bump to `$10`.
- Daily total cap across all agents: `$100/day`, configurable by env var.
- Provisioning: no always-on warm pool.

Budget extension:

```python
def request_budget_extension(amount: int = 10, reason: str = "") -> dict:
    ...
```

Auto-approve if:

- `specialist_5_day_rolling_brier_delta > 0`.
- At least one prior tool call this cycle returned non-empty data and was cited or persisted.
- Daily desk budget has remaining headroom.

Otherwise queue for human. The agent receives `queued_for_human` and must produce the best available artifact under the current cap.

Dashboard:

The GH Pages site gets a per-cycle cost panel:

- Cost by specialist.
- Cost by tool.
- Modal runtime seconds.
- Budget caps and extension requests.
- Number of budget-capped artifacts.
- Cost per resolved forecast improvement once Brier outcomes arrive.

Hard kill:

- At approved cap, terminate the Modal sandbox.
- Capture stdout/stderr tail, last successful tool result, and partial artifact draft if present.
- Persist `quality_flags=["budget_capped"]`.
- The cycle runner continues with other specialists.

## 8. Security / audit

Network allowlist:

```text
api.hyperliquid.xyz
api-ui.hyperliquid.xyz
hermes.pyth.network
benchmarks.pyth.network
api.ask-surf.ai
api.parallel.ai
api.perplexity.ai
talis-tic-read.<env>.internal
talis-tic-insert.<env>.internal
```

The final internal hostnames should come from config, not hard-coded strings. Everything else is denied.

Audit table:

```sql
CREATE TABLE IF NOT EXISTS agent_network_audit (
    id TEXT PRIMARY KEY,
    cycle_id TEXT NOT NULL,
    agent TEXT NOT NULL,
    tool_name TEXT,
    ts TEXT NOT NULL,
    method TEXT NOT NULL,
    url TEXT NOT NULL,
    host TEXT NOT NULL,
    status INTEGER,
    bytes_sent INTEGER NOT NULL DEFAULT 0,
    bytes_received INTEGER NOT NULL DEFAULT 0,
    decision TEXT NOT NULL, -- allowed | denied
    reason TEXT,
    sandbox_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_network_audit_cycle
ON agent_network_audit(cycle_id, agent, ts);
```

Deny rules:

- No writes to TIC store from inside sandbox. Writes only go through typed insert APIs running outside sandbox.
- No calls to AWS Secrets Manager, STS, SSM, metadata service, prod S3, prod RDS, prod Redis, or trading/execution services.
- No reads outside mounted volumes. The runner image should set a controlled working directory and avoid mounting secrets.
- Candidate tools that wrap network calls must declare target hosts in their manifest. The promoter rejects undeclared hosts and non-allowlisted hosts.

## 9. Phased build plan

Sequenced after Track A + Track B from the existing roadmap. Track B forecast resolver must be live before Phase 3 can promote tools.

### Phase 1 - Modal-backed `run_code_modal` (drop-in replacement). ~1 day.

Files:

- Create `tic/desk/tools/run_code_modal.py`
- Edit `tic/desk/tools/__init__.py` only to optionally register `run_code_modal` behind a feature flag
- Create `tic/desk/tools/modal_sandbox_runner.py`

Acceptance criteria:

- `run_code_modal(code="result = 1 + 1")` returns the same envelope shape as `run_code`.
- Timeout, exec error, and serialization error return typed envelopes.
- No sandbox is created until the first Modal tool call in a specialist cycle.

Smoke test in `__main__`:

```python
if __name__ == "__main__":
    print(run_code_modal("result = {'ok': True, 'x': 2}", timeout_s=10))
    print(run_code_modal("while True: pass", timeout_s=2))
    print(run_code_modal("raise ValueError('boom')", timeout_s=5))
```

### Phase 2 - Persistent volume + `learned_tools/` + `propose_tool`. ~2 days.

Files:

- Create `tic/agent_native/learned_tools.py`
- Create local dev mirror `tic/agent_native/learned_tools_volume_dev/README.md`
- Edit `tic/desk/tools/run_code_modal.py` to expose `propose_tool`

Acceptance criteria:

- Proposal creates `learned_tools/<slug>/v1/{tool.py,manifest.json,tests/test_cases.json}` in staging.
- Manifest validates required fields and declared network hosts.
- Candidate tests run in a clean sandbox before manifest status becomes `candidate`.

Smoke test:

```python
if __name__ == "__main__":
    print(propose_tool(
        slug="rolling_corr_demo",
        code="def run(x, y): return {'n': len(x), 'corr': 1.0}",
        description="demo",
        test_inputs=[{"x": [1,2], "y": [1,2]}],
        test_outputs=[{"n": 2, "corr": 1.0}],
    ))
```

### Phase 3 - Brier-gated promotion + auto-registration in TOOLS. ~2 days.

Files:

- Edit `tic/agent_native/learned_tools.py`
- Edit `tic/desk/tools/__init__.py` to merge `get_learned_tool_contracts(as_of=...)`
- Create `tic/eval/tool_ablation.py`

Acceptance criteria:

- Candidate with `n_calls >= 5`, positive average Brier delta, and no failure in last 10 calls promotes to `approved`.
- Approved tool appears in the next cycle's tool registry.
- Demoted tool remains replayable but is absent from current default `TOOLS`.

Smoke test:

```python
if __name__ == "__main__":
    seed_fake_tool_outcomes(slug="rolling_corr_demo", positive=True)
    print(evaluate_tool_promotions(as_of=now_utc()))
    print("rolling_corr_demo" in get_learned_tool_contracts())
```

### Phase 4 - Durable `agent_messages` table + tools + cross-cycle delivery. ~2 days.

Files:

- Create `tic/agent_native/messages.py`
- Edit `tic/desk/tools/agent_native_tools.py` to expose message tools
- Add schema bootstrap through existing additive startup path

Acceptance criteria:

- Direct message to `@macro_regime` appears in next `read_unread_messages("@macro_regime")`.
- Topic message to `macro` is routed to subscribed specialists.
- `request_devils_advocate` queues when target is inactive and records reply with `mark_replied`.

Smoke test:

```python
if __name__ == "__main__":
    msg = post_message("@macro_regime", "question", {"text": "Check funding divergence"})
    print(read_unread_messages("@macro_regime"))
    print(mark_replied(msg["id"], "artifact_demo"))
```

### Phase 5 - Specialist state hydration / dehydration. ~1 day.

Files:

- Create `tic/desk/specialist_state.py`
- Edit the specialist runner to call `hydrate_specialist_context` and `dehydrate_specialist_state`

Acceptance criteria:

- Hydration injects prior state, unread messages, yesterday artifact, recent forecast outcomes, and newly promoted tools.
- Dehydration inserts a new state row without overwriting yesterday.
- Changed prior beliefs require `supersedes` or `because`.

Smoke test:

```python
if __name__ == "__main__":
    ctx = hydrate_specialist_context("macro_regime", cycle_id="smoke", as_of=now_utc())
    print(ctx.keys())
    print(dehydrate_specialist_state("macro_regime", "smoke", ctx["state"]))
```

### Phase 6 - Bitemporal slicing for replay. ~2 days.

Files:

- Edit `tic/agent_native/learned_tools.py`
- Edit `tic/desk/specialist_state.py`
- Create `tic/desk/replay_context.py`

Acceptance criteria:

- `build_replay_context(as_of=t)` returns consistent store, messages, state, and tool registry slices.
- A demoted tool is present in replay only if it was valid at `t`.
- Specialist state replay never reads a newer prior belief.

Smoke test:

```python
if __name__ == "__main__":
    ctx = build_replay_context("2026-05-15T08:00:00Z", specialist_id="macro_regime")
    print(ctx.tool_registry.version_summary())
    print(ctx.specialist_state["as_of"])
```

### Phase 7 - Cost dashboard + extension gate. ~1 day.

Files:

- Create `tic/desk/costs.py`
- Edit `build_data_layer_site.py` to render cost dashboard panel
- Edit `run_code_modal.py` to debit budget per call

Acceptance criteria:

- Per-agent cap hard-stops sandbox calls at `$5` by default.
- Extension request auto-approves only under Brier/productivity gate.
- GH Pages site shows cycle cost by specialist and tool.

Smoke test:

```python
if __name__ == "__main__":
    b = AgentCycleBudget(cycle_id="smoke", specialist_id="macro_regime")
    print(debit_tool_call(b, estimated_usd=Decimal("4.60")))
    print(request_budget_extension("macro_regime", 10, "Need ablation run"))
```

### Phase 8 - Security audit log + allowlist enforcement. ~1 day.

Files:

- Create `tic/desk/sandbox_network.py`
- Edit `run_code_modal.py` to route HTTP through the allowlist shim
- Add `agent_network_audit` schema bootstrap

Acceptance criteria:

- Allowed host call is logged with `decision="allowed"`.
- Denied host call returns `NetworkDenied` and is logged.
- Candidate tool with undeclared network host fails promotion.

Smoke test:

```python
if __name__ == "__main__":
    print(fetch_url("https://api.hyperliquid.xyz/info", method="POST", body={"type": "meta"}))
    print(fetch_url("https://169.254.169.254/latest/meta-data/"))
```

Total build estimate: ~12 focused engineering days. Each phase ships independently and keeps the current Layer 1 surface usable.

## 10. Concrete day-1 first steps

First commit:

Files to create:

- `tic/desk/tools/run_code_modal.py`
- `tic/desk/tools/modal_sandbox_runner.py`

Minimal image definition:

```python
import modal

APP_NAME = "talis-agent-sandbox"
LEARNED_TOOLS_MOUNT = "/mnt/talis_learned_tools"

app = modal.App(APP_NAME)
learned_tools_volume = modal.Volume.from_name("talis-learned-tools", create_if_missing=True)

image = (
    modal.Image.debian_slim(python_version="3.13")
    .uv_pip_install("numpy", "pandas", "scipy", "statsmodels", "httpx", "pydantic")
    .env({"PYTHONUNBUFFERED": "1", "TALIS_SANDBOX": "1"})
)
```

First smoke test:

```python
def _smoke():
    assert run_code_modal("result = {'sum': 1 + 1}")["result"]["sum"] == 2
    assert run_code_modal("raise RuntimeError('x')")["error"]["type"] == "RuntimeError"
    assert run_code_modal("while True: pass", timeout_s=2)["error"]["type"] == "TimeoutError"
```

How a human runs it locally:

```bash
cd /Users/udaikhattar/jarvis-ios/docs/research/brief_experiments
python -m tic.desk.tools.run_code_modal
```

Expected behavior: the first call authenticates to Modal if needed, builds or reuses the pre-baked image, creates a sandbox only when the smoke test calls `run_code_modal`, returns the same envelope shape as local `run_code`, and terminates the sandbox at the end.

## 11. Honest risks + mitigations

Tool sprawl without the Brier gate:

Agents will propose many cute tools that feel useful but do not improve forecasts. Mitigation: candidate tools are callable only by explicit manifest id, never auto-registered. Promotion requires usage, positive Brier delta versus ablation, and recent reliability. Demotion is automatic for 30d unused or harmful tools.

Cost runaway from agents in long loops:

Modal makes it easy to run bigger analyses, which makes loops expensive. Mitigation: no warm pool, lazy provisioning, per-call reserves, 90 percent soft stop, hard kill at cap, single extension gate, daily `$100` cap, and dashboard visibility. The tool envelope always returns `BudgetExceeded` rather than letting the agent continue blindly.

Replay non-determinism if learned tools mutate:

If `learned_tools/foo/tool.py` is edited in place, old research becomes unreplayable. Mitigation: no in-place mutation. Every approved snapshot lives under `v<N>`, manifest includes `code_sha256`, and registry rows carry `valid_from/valid_to`. Replays import by snapshot path, not latest package name.

Security: outbound network access changes the threat model:

The local subprocess had no network; Modal introduces egress. Mitigation: deny-by-default allowlist, outbound audit table, explicit internal deny rules, no secrets in sandbox, no prod DB write path, no AWS control-plane access, and promotion checks for declared hosts.

Bitemporal complexity making the system hard to reason about:

Full bitemporal state can become a maze. Mitigation: one replay API, `build_replay_context(as_of=t)`, owns all slicing. Normal code asks for a context object rather than hand-writing `valid_from` predicates. Tables remain append-only with clear `supersedes` links.

Forecast resolver maturity:

The current resolver is scaffolded. Brier-gated promotion is not meaningful until Track B resolves forecasts deterministically. Mitigation: Phase 2 can collect telemetry immediately, but Phase 3 is explicitly blocked on Track B and should not fake promotion with qualitative "looked useful" scoring.

## 12. What this enables that no Bloomberg / Refinitiv terminal can do

This turns the desk into a compounding research organism rather than a better screen. Example: `macro_regime` posts, "I think USDT supply going parabolic is a delayed bear signal; @smart_money please verify with on-chain flows." The next morning `smart_money` hydrates its state, sees the durable message, runs `compute_stat` plus a promoted learned tool `usdt_mint_flow_to_btc_correlation`, and replies: "r=0.61 over the last 14d, strongest when mint growth leads BTC by 36h; supports the thesis but only during rising funding regimes." Three days later both specialists' priors carry that update, the forecast resolver scores whether the thesis helped, and the brief cites the original message, the tool version, the exact forecast, and the Brier outcome. A terminal can show data and news. This system remembers the debate, builds the instrument it needed, scores whether it helped, and changes tomorrow's analyst behavior.

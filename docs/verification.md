# Verification and compatibility

## Verified boundaries

Target host: Hermes `v0.21.5+4775.g3ebbaf5`, commit
`3ebbaf524344f93943169e63854cb952541563f9`. Version 0.2.0 completed independent
re-review and maintainer acceptance within the isolated scope. The accepted
implementation and repository tests are retained byte-for-byte in this distribution.
Writer source/archive suites each passed 75
cases; independent focused runs are separate evidence: initial source 31 passed
and archive 7 passed, each with one additional fixture failure before plugin
dispatch. After correcting only that scratch fixture, supplemental source 8 and
archive 5 cases passed. The already-green cases were not rerun. This acceptance
does not establish live UI, model execution, delivery or adoption.

`tests/test_heartbeat.py` retains the 30 existing Messaging cases.
`tests/test_local_surfaces.py` adds native local-surface verification:

- Host PluginManager discovery/registration, SessionDB, HeartbeatManager and exact
  readback. Source and extracted archive run the same suite and Plugin Doctor.
- Fresh/cached Gateway binder, native context executor, sequential executor,
  `model_tools`, registry dispatch; cached empty context ID remains supported,
  nonempty identity disagreements remain rejected.
- Desktop's **unmodified native init/binder/launch-home functions** and live registry lookup,
  CLI's **unmodified native REPL attach function** and invocation-time `_cli_ref`.
  All five controls use ordinary executor/registry dispatch, no injected parent agent.
- Native CLI cached manager identity, unarmed `set`/`resume` starting the actual
  watchdog, busy and queued-human deferral, FIFO loop consumption and self-clear.
  Read-only status after a simulated reopen does not arm the watchdog.
- Native Desktop notification scoped loop and heartbeat tick reading fresh DB state,
  idle turn claim, submit entry, self-clear, and canonical Gateway viewer skip.
- Real native compression locking/publication/migration; child `clear` and `set`
  before local owner re-anchor, preservation after re-anchor and no revival from
  repeated migration. Owner, cache and routes are not prematurely changed by plugin.
- Refusal of ended/reset rows, switched or closed local owners, different DB/home,
  released leases, delegated children, explicit branches, one-shot with a CLI ref,
  missing native host/ref, and stale UI record/agent. Protected state/routes unchanged.
- Compute-host native existing-record reconciliation and native borrowed lease
  adoption allow child control without treating it as delegated execution.

`tests/test_cli_review_regressions.py` covers the independent-review corrections:

- Unmodified native `_chat_settle_turn` / `_transfer_session_yolo`, with no artificial
  CLI lease transfer. Two compressions, five ordinary executor controls after each
  settlement, original lease unchanged, native cached watchdog reading child state.
- Original same-home lease registry proof, PID/live identity and competing child
  owner refusal; released/missing/corrupt/foreign-home leases, invalid owner reference,
  agent DB mismatch and branches cannot write protected state or routing.
- Lower-layer `Thread.start` failure with native start helper retained; persisted
  state is distinguished from failed arming, and retries after compression/native
  scratch plugin reload stay fail-closed. Status/pause/clear remain available.
- A second real native watchdog cannot authorize the failed owner. A new CLI owner
  arms normally and idempotently. Failed persistence is not reported as arm failure.

Agent, CLI and UI records are **shells**. Desktop server entry-point functions are
compiled unmodified from their AST; the server facade contains only native registry
state, not a running backend. Split UI functions are rebound with the host's
`method_ctx.rebind`. Clock/cadence, retirement admission scope, unrelated notification
sinks, browser identity helper, prompt-submit/model/transport and per-input CLI
turn dispatch are harnessed. The CLI FIFO loop itself is native. CLI post-turn
settlement executes the unmodified native function and leaves its original lease
untouched; Desktop re-anchor assignment remains simulated with native lease transfer.
Failed-start latch reset appears only in scratch fixture teardown (no thread exists
to run native exit cleanup), never in plugin code. Full turn/compression execution
is not claimed. Tests never import full `cli`,
`run_agent`, or `tui_gateway.server` entry points.

## Known limits, not success claims

- No rendered Desktop app, real interactive terminal session, full `run_conversation`,
  compression model, paid inference, or external message delivery was exercised.
- The isolated verification did not exercise production install, enable, hot reload,
  conversation reset/switch or heartbeat mutation. Earlier Messaging deployment
  evidence does not verify adoption of the new code. Tools/schema remain next-session
  deferred; verify adoption separately for each hosting process.
- CLI `--resume` alone does not arm the existing native watchdog. Explicit tool or
  built-in `/heartbeat resume` / `set` is required; status is intentionally read-only.
- Native queued CLI wakes are plain text. This plugin does not add generation fencing
  at consumption, and cannot promise atomic exclusion of a concurrent reset/switch
  after admission. Already queued/started work can survive a subsequent clear.
- **Two-idle-poller race reproduced**: synchronized real DB loads on two native UI
  tick functions, with separate parent/child-like locks, admit two submit entries
  while persisted `fire_count` is one. The test deliberately records this inherited
  limit, not exactly-once success. Compute-host full child startup and live lifecycle
  are unverified; the probe does not establish that every live configuration races.
  A robust cross-process driver ownership / atomic tick claim fix belongs in the
  host and needs separate approval. No core change or production monkeypatch is made.
- Concurrent native slash/reset operations are not made transactional by the plugin's
  lock. Ownership rechecks and readback detect some conflicts, not every race window.

## Private host contracts

Registration uses public `ctx.register_tool`. Controls additionally depend on:

- `hermes_cli.heartbeat`: native manager/state/interval parser.
- `hermes_state_registry`, SessionDB metadata, routing and compression-tip APIs.
- `gateway.session_context`, platform definitions, native delegation predicate.
- CLI `ctx._manager._cli_ref`, manager home, CLI/agent DB/current agent/interactive state,
  native active-session lease, `_get_heartbeat_manager`, `_start_heartbeat_watchdog`.
  Original CLI lease proof reads native `active_sessions._read_entries(strict=True)`
  under its pinned `_FileLock`; it performs no prune, transfer or registry rewrite.
  Observed arm failure is a plugin-only `_session_heartbeat_arm_failed` boolean on
  the CLI owner (not on a per-session adapter/handler). It survives compression and
  plugin reload without replacing any host method or modifying the host start latch.
- Already-loaded Desktop `tui_gateway.server` live `_sessions` and lock, own profile
  DB/current agent/live lease and notification stop token; a native `profile_home: null`
  record uses the server's `_launch_home()` (never the active thread's arbitrary home).

`plugin/host_surfaces.py` is the small private adapter. It never imports the server
for tool use, replaces a callable, injects a message, installs a timer, or repoints
native state. Missing ownership fails closed. Live current-owner-to-compression-tip
proof is same-DB only, not arbitrary ancestry or branch authorization. Native
borrowed compute leases are accepted, but do not bypass the delegation predicate.
These are not stable public extension APIs; revalidate on each host update.

## Running the checks

Prerequisites: Hermes source checkout, an interpreter with host dependencies and
`pytest` already installed. No package installation is performed. The plugin has no
additional Python dependency. `check.py` itself uses the standard library.

```sh
HERMES_SOURCE="$HOME/.hermes/hermes-agent" python3 scripts/check.py
```

Set `HERMES_SOURCE` for a different checkout, and `HERMES_PYTHON` to the absolute
host interpreter path if necessary. Default interpreter:
`$HERMES_SOURCE/venv/bin/python`. Checkout `venv/lib/python*/site-packages` is pinned
in-process when available; other layouts are unverified.

Scratch files use `TMPDIR`, defaulting to `$HOME/.hermes/cache/scratch`. Child
processes receive a minimal credential-free environment. Before host imports,
verification redirects HOME/HERMES_HOME, disables lazy installation and project/
bundled plugin discovery, prohibits network connections and full entry-point
imports, and supplies only an unused rejecting bootstrap network export.
The guard is read back after tests. All timers and threads used by the harness
are stopped; DBs/config/plugin copies are temporary. No service lifecycle action.

`check.py` runs source tests/Doctor, builds a deterministic versioned archive from
manifest + **all plugin Python modules**, compares every extracted byte, then runs
artifact tests/Doctor. It records real output and host Git revision/worktree state
under ignored `evidence/` (override with `HERMES_EVIDENCE_DIR` to retain earlier
evidence). Logs can contain private local paths; sanitize before
sharing. Test descriptions containing `PASS` for a native limit mean the limitation
was reproduced, not fixed. Use the actual pytest summary to assess test completion.

## Archive

`dist/session-heartbeat-0.2.0.tar.gz` contains eight files under `session-heartbeat/`:

- `plugin.yaml`
- `__init__.py`
- `heartbeat_tool.py`
- `host_surfaces.py`
- `README.md`
- `README.ja.md` (Japanese user guide)
- `LICENSE`
- `docs/verification.md`

Development tests/runners come from the repository, not the tar. README relative
links resolve inside the archive. The archive is a separately verified distribution
artifact. Independent re-review and maintainer acceptance cover the isolated
scope only; they do not verify production installation or running-host adoption.

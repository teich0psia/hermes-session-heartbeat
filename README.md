# Hermes Session Heartbeat

English | [日本語](README.ja.md)

A model-facing tool for controlling the **current conversation's** native Hermes
Session Heartbeat on a messaging Gateway, Desktop, or interactive CLI.
Ask the agent to check CI periodically in this conversation and clear the heartbeat
when the job finishes.

This exposes Hermes' existing [`/heartbeat`](https://hermes-agent.nousresearch.com/docs/user-guide/features/heartbeat/)
as `session_heartbeat`. It adds no scheduler, cron job, core modification, or runtime
monkeypatch. For manual control, the built-in `/heartbeat` is sufficient.

## Compatibility and scope

Version **0.2.0** has completed independent re-review and maintainer acceptance
within the isolated verification scope. This does not establish live UI behavior,
model execution, delivery, or adoption by a running host.
Tested host: Hermes `v0.21.5+4775.g3ebbaf5`, commit
`3ebbaf524344f93943169e63854cb952541563f9`. Other revisions need revalidation because
this plugin depends on private host APIs.

| Surface | Support |
| --- | --- |
| Resident messaging Gateway | Current canonical route and SessionDB must be in the same profile home, using the standard `$HERMES_HOME/sessions` scope |
| Multiplex Gateway | Same-home routes only; no cross-home route search |
| Native Desktop conversation | Live UI record, current agent, profile DB and native lease must agree; state is discovered by the native notification poller |
| Interactive classic CLI | Invocation-time native REPL binding; controls update its cached manager, and `set` / `resume` arm its native watchdog |
| CLI reopened with `--resume` | Saved state remains; **explicit `resume` or `set` is required** to arm the watchdog. `status` does not restart work |
| API / ACP / cron / one-shot / delegated agents | Unsupported |
| Other local surfaces / custom routing scopes | Not claimed supported |

Native compression continuations are accepted from the **current owner's** live tip
in the same DB, including the interval before UI/CLI re-anchoring. The plugin does
not repoint the owner, CLI cache, or routing index itself. Native CLI settlement
updates the owner ID but leaves its lease at the original ID: the adapter verifies
that exact same-home registry lease (PID/live identity), its native live compression
tip, and absence of a competing lease on that continuation. Branches, released or
missing leases and foreign owners are refused. Registry reads do not prune or
transfer leases; even an uncertain competing entry is conservatively rejected.
Missing or stale ownership is rejected rather than guessed.

Isolation tests exercise ordinary tool dispatch and native idle admission, not a
rendered Desktop/CLI session, a full agent conversation, model inference, or external
delivery. See [verification and limits](docs/verification.md).

**Native limits remain native limits.** An already queued CLI prompt is plain text,
not a generation-fenced envelope; a concurrent switch/reset after admission is not
atomically protected by this plugin. A deterministic two-idle-UI-poller harness
also reproduces two admissions for one due tick, relevant to Desktop compute-host
parent/child polling. Full compute-host launch and exactly-once ownership across
processes are unverified. Do not rely on exactly-once recurring work. Fixing these
host driver guarantees would require separately approved core work, not a plugin
monkeypatch.

## Installation and adoption

The native directory package is **`plugin/`**, not the repository root. No additional
Python package or plugin-specific API key is needed; Hermes still needs its normal
model and surface configuration. The commands below install code from the GitHub
default branch; check the manifest version or pin a published commit:

```sh
hermes plugins install 'https://github.com/teich0psia/hermes-session-heartbeat.git#plugin' --no-enable
hermes plugins enable session-heartbeat --no-allow-tool-override
hermes plugins list --user --json
```

To pin a published version, add `--ref <full-40-character-commit-SHA>` to install.
Branch names, tags and shortened SHAs are not accepted on the tested host. For named
profiles, use `hermes --profile <name>` and align the plugin and conversation homes.
This is not a `pip install` package.

Enabling permits trusted Python code to run and can request supported Gateway / serve
backend hot reload. A service restart is **not inherently required by this plugin**;
that does not prove an existing host has adopted new code. Verify each hosting
process's reload response. Python tools/schema are deferred to the **next session**,
not inserted into existing chats. Start a new CLI process to adopt an updated CLI
plugin; Desktop needs backend adoption as well as a new conversation. If supported
activation fails, any app/backend restart needs separate approval. Do not reset an
existing conversation merely to test availability. Installing/enabling alone starts
no recurring job.

## Tool usage

Use only for recurring work the user actually authorized. Include an end condition.
The tool and toolset are both named `session_heartbeat`; no new slash command is
registered. These are tool argument examples, not shell commands:

```json
{"action":"status"}
```

```json
{"action":"set","interval":"10m","prompt":"Check CI and report meaningful changes only. When it finishes, call session_heartbeat clear."}
```

```json
{"action":"pause"}
```

```json
{"action":"resume"}
```

```json
{"action":"clear"}
```

| Action | Behavior |
| --- | --- |
| `status` | Read current state; `heartbeat: null` when unset. Does not arm CLI |
| `set` | Require interval and nonempty prompt; replace the one schedule, reset timer and fire count |
| `pause` | Keep the instruction, stop subsequent ticks |
| `resume` | Re-anchor at now; no stale immediate tick. Arm interactive CLI |
| `clear` | Remove the schedule; idempotent when unset |

Intervals include `90s`, `10m`, `2h`, `1d`; minimum 60 seconds. Even when changing
only the interval or prompt, supply both to `set`. Unset `pause` / `resume` returns
`no_heartbeat`. No session ID, profile, or path argument is accepted.

The native owner must remain running. Busy work and pending human input defer the
idle tick; exact wall-clock timing is not guaranteed. `pause` / `clear` do not cancel
an already admitted/started turn. Recurring work can consume model/tool usage and
incur costs. Do not put secrets into the prompt.

## Results and errors

Success includes `ok`, `action`, `changed`, `session_id`, `heartbeat`, `persisted`,
`next_due_in_seconds`, `driver`, and `wakeup`. `persisted: true` means exact DB
readback, **not** successful model execution or delivery. Drivers are
`native_gateway_recovery_poller`, `native_desktop_notification_poller`, or
`native_cli_heartbeat_watchdog`. CLI `set` / `resume` also verifies native arming.
Other operations do not claim to have armed a watchdog. Normal arming checks the
native helper's return and start latch; it is not continuous thread supervision.

A native `Thread.start()` failure can leave the host's start latch set. The plugin
remembers an observed arm failure on that **live CLI owner**, across compression and
plugin reload, without changing the native latch or replacing methods. Subsequent
`set` / `resume` still save/read back state but return `driver_arm_failed`, not armed
success. `status`, `pause` and `clear` remain available when ownership/storage are
valid. The conservative failure marker lasts until that CLI owner is discarded;
reopen CLI and explicitly resume/set to retry. A different CLI owner's watchdog
cannot prove this one started. Automatic same-owner repair requires separate core
approval.

Failure returns `ok: false`, `error_code`, and `error`:

| Code | Meaning |
| --- | --- |
| `invalid_arguments` | Invalid action, interval or prompt; extra fields are not accepted |
| `unsupported_surface` | No proven supported local owner, or headless/delegated surface |
| `missing_session_context` / `profile_mismatch` | Missing/mismatched dispatch identity or profile home; do not work around with IDs |
| `not_current_route` | Messaging conversation lacks the required canonical live route |
| `no_heartbeat` | Set a schedule before pause/resume |
| `persistence_unverified` / `route_changed` | Save or ownership may have raced; recheck status, do not blindly retry a mutation |
| `invalid_stored_state` / `storage_error` | Storage is invalid or inconsistent; never automatically overwrite corrupt state |
| `driver_arm_failed` | CLI state saved/read back, but arming failed or previously failed on this owner. Includes `persisted: true`, `driver_armed: false`, action/changed/session/heartbeat/driver; no wakeup success claim |
| `unsupported_host` | A required internal API or driver contract is unavailable |

Controls serialize this plugin's calls, check ownership before/after saving, and
verify exact persisted state. They do not add an atomic transaction across native
slash commands, reset, scheduler admission and consumption.

## Development

See [verification](docs/verification.md) for prerequisites, reproducible isolated
checks, real host versus harness boundaries, and archive contents.

## License

[MIT](LICENSE).

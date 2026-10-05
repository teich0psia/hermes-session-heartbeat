# Hermes Session Heartbeat

English | [日本語](README.ja.md)

A plugin that lets the agent set, inspect, pause, resume, or clear the Session Heartbeat for the current Hermes messaging conversation. For example, you can ask it to check periodically whether CI has finished and clear the heartbeat when it does.

The plugin exposes Hermes' built-in [`/heartbeat`](https://hermes-agent.nousresearch.com/docs/user-guide/features/heartbeat/) functionality as the model-callable `session_heartbeat` tool. It does not create its own scheduler or cron jobs, or replace Hermes core code. If you only need manual control, the built-in `/heartbeat` command is enough.

## Supported environments

The plugin version is **0.1.0**. It was verified against Hermes `v0.21.5+4775.g3ebbaf5` (commit `3ebbaf524344f93943169e63854cb952541563f9`). Because it uses internal Heartbeat APIs, compatibility with other Hermes versions has not been verified.

| Surface or configuration | Support |
| --- | --- |
| Current conversation in a persistent messaging Gateway | Supported when the conversation DB and routing information are in the same profile home and use the standard `$HERMES_HOME/sessions` directory |
| Gateway serving multiple profiles (multiplex) | Supported only when the same-home requirement above is met. Routing information in other homes is not searched |
| CLI / TUI / Desktop / API / ACP | Not supported. This differs from the supported surfaces of the built-in `/heartbeat` command itself |
| cron / one-shot / subagents | Not supported |
| Custom session storage locations or conversations spanning different homes | Not supported |

The isolated tests use Telegram-style conversations; they do not verify live message delivery on each messaging service. Tool visibility to the model in a new session, real model execution, and actual recurring wakeups and message delivery remain unverified. See [Verification and compatibility (Japanese)](docs/verification.md) for the verification scope.

## Installation

Use [Hermes plugin management](https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins). The plugin itself is in **`plugin/`**, not the repository root. No additional Python packages or plugin-specific API key are required. At runtime, Hermes and the target Gateway's normal model and messaging configuration are required.

```sh
# Install only; do not enable yet
hermes plugins install 'https://github.com/teich0psia/hermes-session-heartbeat.git#plugin' --no-enable

# Review the code before enabling. Do not grant permission to override built-in tools
hermes plugins enable session-heartbeat --no-allow-tool-override

# List user plugins
hermes plugins list --user --json
```

To pin a specific revision, pass `--ref` and the published commit's full **40-character SHA** to the install command. Branch names, tag names, and abbreviated SHAs are not accepted. For a named profile, use `hermes --profile <profile-name>` with each command, and make sure the installation profile matches the profile used by the Gateway.

Enabling a plugin authorizes execution of trusted Python code. It may also request a plugin hot reload on a running Gateway. On the verified host, registration succeeded without a restart, but **Python tool availability was deferred until a new session**. Do not assume the tool has been added to an existing conversation, or reset a conversation without permission just to test it. Installing or enabling the plugin does not start recurring work.

This repository is a native directory plugin, not a package for `pip install`.

## Using it in a conversation

In a supported messaging conversation, ask the agent something like:

> In this conversation, check every 10 minutes whether CI has finished. Report only meaningful changes, and clear the heartbeat when CI is complete.

Both the tool name and the toolset name are `session_heartbeat`. The plugin does not add any custom slash commands. The following examples are tool-call arguments, not commands to run in a shell.

```json
{"action":"status"}
```

```json
{"action":"set","interval":"10m","prompt":"Check whether CI has finished and report only meaningful changes. When CI is complete, call session_heartbeat with the clear action."}
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
| `status` | Inspect the current configuration. If none is set, `heartbeat` is `null` |
| `set` | Requires both `interval` and a nonempty `prompt`. Replaces the single configuration for the conversation and resets the timer and execution count |
| `pause` | Retain the instruction while stopping future recurring runs |
| `resume` | Resume with the timer recalculated from the current time. Does not immediately execute an old schedule from before the pause |
| `clear` | Remove the configuration. Also succeeds without making changes if none is set |

Specify intervals such as `90s`, `10m`, `2h`, or `1d`; the minimum is 60 seconds. Even if you only want to change the interval or the instruction, pass both to `set`. Calling `pause` or `resume` without a configured heartbeat returns a `no_heartbeat` error.

## Recurring execution: important notes

- Use this only for recurring work authorized by the user. Include a stopping condition in the instruction, and use `clear` when the heartbeat is no longer needed.
- Recurring execution involves normal model calls and tool execution, so it may consume usage and incur costs. Do not include secrets in the prompt.
- While the Gateway is running, Hermes' built-in monitoring process discovers saved configurations. It defers execution if the conversation is busy or user input is waiting. Execution at the exact specified interval is not guaranteed.
- `pause` and `clear` do not cancel work that has already started. For manual control, you can also use the built-in `/heartbeat status`, `/heartbeat pause`, and `/heartbeat clear` commands.
- Session IDs, profiles, and paths cannot be supplied as arguments. The plugin validates the current conversation context supplied by the host and rejects operations from other conversations or subagents.
- Conversation ownership is checked before and after persistence, but this does not provide atomic protection against every concurrent operation through the built-in commands.

## Results and errors

Successful results include `ok`, `action`, `changed`, `session_id`, `heartbeat`, `persisted`, `next_due_in_seconds`, `driver`, and `wakeup`. `persisted: true` means the saved state was verified by reading it back from the DB; it does not indicate a successful model response or message delivery.

Failures return `ok: false`, `error_code`, and `error`.

| Error code | What to check |
| --- | --- |
| `invalid_arguments` | Check the action name, interval, and required instruction. Do not pass an interval or instruction to actions other than `set` |
| `unsupported_surface` | Check that this is a conversation in a supported persistent messaging Gateway |
| `missing_session_context` / `profile_mismatch` | The host-supplied conversation context and profile do not match. Do not try to bypass this by entering an ID manually |
| `not_current_route` | Check that the current conversation has a live route and uses the standard same-home configuration |
| `no_heartbeat` | Configure a heartbeat before calling `pause` or `resume` |
| `persistence_unverified` / `route_changed` | Persistence or conversation state may have encountered a conflict. Check with `status`; do not blindly repeat a state-changing operation |
| `invalid_stored_state` / `storage_error` | Investigate the host's storage. Do not automatically overwrite corrupted data |
| `unsupported_host` | Required internal Hermes APIs are unavailable. Check how the host differs from the verified version |

## Development and verification

Source code, tests, and isolated verification scripts are included. See [Verification and compatibility (Japanese)](docs/verification.md) for environment requirements, execution instructions, and the distinction between mocked behavior and real host APIs.

## License

[MIT License](LICENSE).

# Engine JSON-RPC API

The engine reads newline-delimited JSON-RPC 2.0 requests from stdin and writes
responses and notifications to stdout. Logs never use stdout. Request IDs may
be strings or integers; a notification has no ID and receives no response.

## Methods

All parameters are JSON objects. The result shapes below describe normal
successful results.

| Method | Parameters | Result |
| --- | --- | --- |
| `app/status` | `{}` | `{state, protocol, engineVersion, dataDir, runningJobs}` |
| `health/ping` | `{}` | `{status: "ok"}` |
| `shutdown` | `{}` | `{stopping: true, exitCode: 0}` |
| `setup/checklist` | `{}` | An array of setup items |
| `setup/runChecks` | `{}` | `{items: [...]}` |
| `setup/markManual` | `{item}` | `{item, status: "manual_done"}` |
| `settings/get` | `{}` | Current safe settings, never secrets |
| `settings/set` | `connectorPath`, `workerCount`, `retentionDays`, `claudeModel`, `codexModel`, optionally `confirmModelChange` | Updated safe settings; model changes require confirmation |
| `projects/list` | `{}` | An array of `{project, title}` |
| `projects/create` | `{project, title}` | `{project, title}` |
| `files/list` | `{project}` | An array of `{name, size}` |
| `chat/send` | `{project, text}` | `{status, project}`; status is `completed` or `stopped` |
| `chat/stop` | `{project}` | `{status: "stopped", project}` |
| `job/status` | `{job}` | Stored job status and counts |
| `job/approvePlan` | `{job, expectedSpecVersion}` | Current job status |
| `job/approveSample` | `{job, expectedSpecVersion}` | Current job status |
| `job/startFull` | `{job}` | Current job status |
| `job/pause` | `{job}` | Current job status |
| `job/resume` | `{job}` | Current job status |
| `job/rebuildRules` | `{job, rules}` | `{job, path}` |
| `auth/claudeLogin` | `{}` | `{started: true, vendor: "claude"}` |
| `auth/codexLogin` | `{}` | `{started: true, vendor: "codex"}` |
| `auth/status` | `{}` | Safe Claude and Codex login/plan status |
| `os/suspendHint` | `{}` | `{status: "accepted"}` |
| `os/resumeHint` | `{}` | `{status: "accepted", actions: [...]}` |
| `os/powerStatus` | `{onAc: bool|null, percent: int|null}` | `{status: "accepted"}` |

`chat/send` starts one masked, subscription-backed Claude turn for a project.
It is exempt from the generic 30-second request timeout, but it can be
cancelled with `chat/stop` or by shutdown. Only one turn per project runs at a
time. Chat events are streamed while the turn runs. Operator-facing file
listing excludes chat, model, raw, masked, and other engine-internal files.

Job requests validate and schedule work; they do not perform long-running job
work before returning. There is one driver per job. A pause is persisted
before its driver is stopped. A quiet stop stops dispatch and leaves a
recoverable persisted state. After restart, interrupted work is recovered only
after a valid configuration is available.

## Errors

The engine uses these JSON-RPC errors:

* `-32600`: invalid request.
* `-32601`: method not found.
* `-32602`: invalid parameters.
* `-32603`: internal error, with no sensitive data.
* `-32002`: setup is required, with `{reason: "setup_required"}` and possibly
  a safe explanatory `message`.
* `-32001`: an operational or job error. Its data contains a `reason` where
  applicable. Known reasons include `job_not_found`, `job_not_running`,
  `job_not_paused`, `job_stalled`, `sample_not_approved`,
  `stale_spec_version`, `invalid_job_state`, `background_task_failed`,
  `app_server_unavailable`, `claude_turn_failed`, `filesystem_unavailable`,
  and `stopping`.

Job errors may also include a safe `stallReason`; project and duplicate-turn
errors identify the affected job or project. Malformed JSON uses `-32700`.

## Notifications

Notifications have JSON-RPC method names and a `params` object:

* `job/state`: `{job, state}`. Public states include `planned`, `plan_approved`,
  `running`, `sample_ready`, `paused`, `stalled`, `done`, and
  `done_with_issues`. Internal phases such as pulling, masking, sample work,
  full work, quote checks, output building, and verification map to `running`.
  Exactly one notification is emitted for each stored transition.
* `job/progress`: `{job, done, total, remaining}`.
* `chat/event`: `{project, event}` for safe, masked Claude events.
* `engine/event`: the engine event, including `kind` and, for job events, `job`.

Notifications contain application data only. Credentials, tokens, API keys,
and credential-file contents are never returned.

## Lifecycle and shutdown

The engine may start recovery after a valid configuration is loaded. Each
background job has one driver, and persisted state is the source of truth.
Pause writes the paused state first; quiet stop prevents new dispatch while
allowing the current driver to reach its safe stop point.

On `shutdown`, and also on stdin EOF, every active chat turn is stopped first:
its complete process tree is killed and its temporary MCP configuration is
deleted. Job drivers and login children are then stopped. The shutdown reply
is sent without waiting for unrelated in-flight requests; those requests are
cancelled. The stdio process exits after cleanup, with `exitCode: 0` for a
normal shutdown. A server failure is logged and still reaches process exit
with a non-zero code.

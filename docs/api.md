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
| `settings/get` | `{}` | `{keyPath, connectorPath, connectorDir, keyLocated, workerCount, workerCountMode, retentionDays, claudeModel, codexModel}` |
| `settings/set` | `keyPath` (or deprecated `connectorPath`), `workerCount` (`null` means automatic), `retentionDays`, `claudeModel`, `codexModel`, optionally `confirmModelChange` | Updated safe settings; model changes require confirmation |
| `projects/list` | `{}` | `[{project, title, createdAt, activeJob, lastJob}]` |
| `projects/jobs` | `{project}` | `{jobs: [{job, title, state, step, createdAt, updatedAt, specVersion}]}` |
| `projects/create` | `{project, title}` | `{project, title}` |
| `files/list` | `{project}` | `[{name, size, modified, kind}]`, newest first |
| `files/path` | `{project, name}` | `{path, projectDir}` |
| `chat/history` | `{project, before?, limit?}` | `{entries, more}`; limit defaults to 100 and is capped at 500 |
| `allowance/status` | `{}` | `{codex, claude}` safe subscription allowance snapshots |
| `ids/attach` | `{project, text}` | `{name, raw, distinct, duplicates, malformed}` |
| `chat/send` | `{project, text}` | `{status, project}`; status is `completed` or `stopped` |
| `chat/stop` | `{project}` | `{status: "stopped", project}` |
| `job/status` | `{job}` | Stored job status, counts, and `stall` (`{reason, text}` or `null`) |
| `job/plan` | `{job, specVersion?}` | Plan card from the stored spec and preflight |
| `job/sample` | `{job}` | Masked sample card and sample rows |
| `job/snapshot` | `{job}` | Window-safe snapshot with progress, activity and checklist |
| `job/outputs` | `{job}` | Done view with output files and reconciliation |
| `job/approvePlan` | `{job, expectedSpecVersion}` | Current job status |
| `job/approveSample` | `{job, expectedSpecVersion}` | Current job status |
| `job/startFull` | `{job}` | Current job status |
| `job/pause` | `{job}` | Current job status |
| `job/resume` | `{job}` | Current job status |
| `job/rebuildRules` | `{job, rules}` | `{job, name, path, rebuild}`; writes `results.v{spec}.rebuild{n}.xlsx` |
| `auth/claudeLogin` | `{}` | `{started: true, vendor: "claude"}` |
| `auth/codexLogin` | `{}` | `{started: true, vendor: "codex"}` |
| `auth/status` | `{}` | Safe Claude and Codex login/plan status |
| `os/suspendHint` | `{}` | `{status: "accepted"}` |
| `os/resumeHint` | `{}` | `{status: "accepted", actions: [...]}` |
| `os/powerStatus` | `{onAc: bool|null, percent: int|null}` | `{status: "accepted"}` |

The shell only forwards an allowlisted subset of methods to the UI; see `shell/src-tauri/src/lib.rs`.

## Shell commands

`open_project_file(project, name)` and `open_project_folder(project)` are Tauri
commands invoked directly with `invoke`; they are not sent through
`engine_call`. Both return `null` on success and reject with
`{code, message, data: {reason}}`. The shell derives the project directory from
`app/status.dataDir`, validates the project slug and containment, and reports
`open_failed` when the desktop opener cannot open the validated path.
Files are limited to `summary.md` or `xlsx`, `md`, `csv`, and `html` files
under `jobs/<job>/output/`; internal chat, raw, masked, and state files are
never opened. The shell opens the canonical path it checked, but a same-user
process can still replace a directory in the remaining window before the OS
opener. Shell command rejection reasons are `file_not_found`,
`outside_project`, `type_not_allowed`, `open_failed`, `invalid_slug`,
`engine_unavailable`,
`timeout`, `invalid_name`, and `project_not_found`.

`chat/send` starts one masked, subscription-backed Claude turn for a project.
It is exempt from the generic 30-second request timeout, but it can be
cancelled with `chat/stop` or by shutdown. Only one turn per project runs at a
time. Chat events are streamed while the turn runs. Operator-facing file
listing excludes chat, model, `jobs/*/raw`, `jobs/*/masked`, and other engine-internal files.

Job specifications and preflight records live in the canonical folder
`<dataDir>/projects/<project>/jobs/<job>/`. Each proposal writes
`spec.vN.json` and `preflight.vN.json`; the latter contains bucket counts,
scan bytes, capped leads, and id-intake counts. An approved scan may vary by
up to 10% before the runner refuses it.

`job/rebuildRules` parses and validates rules against the stored output schema.
Invalid rules return `-32602` with
`{reason: "invalid_rules", detail: "..."}`. Rebuilds do not change stored
answers and use a per-spec rebuild number.

After output verification, the job folder contains
`output/verify-report.vN.md` and `output/reconciliation.vN.json`. The report
includes the reconciliation equation and issue-group counts; the JSON contains
integer status terms, `selected_distinct_ids`, `duplicates_removed`, and
`adds_up`.

The public `job/outputs.reconciliation` maps these persisted snake_case fields
to `selected`, `read`, `noTranscript`, `transcriptionFailed`, `tooShort`,
`notFound`, `heldBack`, `failed`, `notStarted`, and `addsUp`.

Each `job/outputs.files` entry is `{name, path, kind, specVersion, rebuild}`.
`name` is the file name only; `path` is POSIX-relative to the project folder,
such as `jobs/job-one/output/results.v1.rebuild2.xlsx` or `summary.md`.
`specVersion` and `rebuild` are integers or `null`. Only workbooks, verify
reports, and the project summary are listed. `job/rebuildRules` returns the
same project-relative `path` and file `name`.

Each `job/plan.rules` entry includes `definition`, the complete JSON rule
definition accepted by `_rule`; these are the rules of the newest rebuild when
one exists. `outputColumns` includes `nullable` and `values` (the enum values,
or `null`).

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
  `job_not_paused`, `job_stalled`, `job_running`, `sample_not_approved`,
  `stale_spec_version`, `invalid_job_state`, `background_task_failed`,
  `app_server_unavailable`, `claude_turn_failed`, `claude_extra_usage_on`,
  `claude_limit_reached`, `filesystem_unavailable`,
  `rules_unreadable`, and `stopping`.

Job errors may also include a safe `stallReason`; project and duplicate-turn
errors identify the affected job or project. Malformed JSON uses `-32700`.

## Notifications

Notifications have JSON-RPC method names and a `params` object:

* `job/state`: `{job, project, state, step}`. `state` is the public vocabulary;
  `step` is the raw internal state. Exactly one notification is emitted for
  each stored transition.
* `job/progress`: `{job, project, phase, done, inFlight, retrying, failed,
  heldBack, remaining, total}`. All count fields are integers. `remaining`
  means not started, and `done + inFlight + retrying + failed + heldBack +
  remaining == total`.
* `engine/event` kinds `counts` and `eta` are also emitted at least every ten
  seconds while a sample or full phase runs, and after lead completion.
  `eta` is `{low, high, seeded, known}` in seconds, with null bounds while
  unknown. The timer also emits `activity` as
  `{last_activity_ts, in_flight, workers}`. `last_activity_ts` and
  `pause.resume_at` are epoch-seconds integers or floats.
* `chat/event`: `{project, event}` for safe, masked Claude events.
* `engine/event`: the engine event, including `kind` and, for job events, `job`.
  Pause reasons are `quota`, `user`, `sleep`, `credit`, `auth`,
  `model_unavailable`, `error_storm`, `network`, `spend_control`, and `power`.

Notifications contain application data only. Credentials, tokens, API keys,
and credential-file contents are never returned.

Chat history is append-only JSONL at `<dataDir>/projects/<project>/chat/history.jsonl`.
Entries have increasing project-local IDs; partial final lines are ignored. Live
chat events include `turn` and `entryId`. `settings.json` stores worker and
retention settings atomically; a changed worker count applies on the next job
start or resume, not to a running pool. File paths are POSIX-relative names and
are returned as absolute OS-native paths. `projects/jobs` uses public state names.

Examples:

```json
{"jsonrpc":"2.0","id":1,"method":"projects/jobs","params":{"project":"demo"}}
{"jsonrpc":"2.0","id":5,"method":"projects/list","params":{}}
{"jsonrpc":"2.0","id":6,"method":"files/list","params":{"project":"demo"}}
{"jsonrpc":"2.0","id":2,"method":"files/path","params":{"project":"demo","name":"report.md"}}
{"jsonrpc":"2.0","id":7,"method":"chat/history","params":{"project":"demo","limit":100}}
{"jsonrpc":"2.0","id":3,"method":"allowance/status","params":{}}
{"jsonrpc":"2.0","id":4,"method":"ids/attach","params":{"project":"demo","text":"101, 102 102\nnope"}}
```

Job read-model examples:

```json
{"jsonrpc":"2.0","id":20,"method":"job/plan","params":{"job":"job-one","specVersion":1}}
{"jsonrpc":"2.0","id":21,"method":"job/sample","params":{"job":"job-one"}}
{"jsonrpc":"2.0","id":22,"method":"job/snapshot","params":{"job":"job-one"}}
{"jsonrpc":"2.0","id":23,"method":"job/outputs","params":{"job":"job-one"}}
```

The plan and sample responses are read from the versioned job folder and the
store. Sample answer and quote strings are masked again at the API boundary.
Unknown jobs return `-32001` with `{reason: "job_not_found"}` for all four
read methods.

`job/plan.counts` uses the public camelCase keys `selected`, `usable`,
`noTranscript`, `transcriptionFailed`, `tooShort`, `notFound`, `capped`,
`duplicates`, and `malformed`. `job/snapshot` includes `planApprovedAt` and
`sampleApprovedAt`, each an epoch-seconds timestamp or `null` when that gate
has not been approved. It also includes `stall`, which is `{reason, text}` for
a stalled job and `null` otherwise. The reason is the latest stalled
state-event reason; the text is a safe plain-language explanation and never
contains a stack trace or path.

### Public job states

| Internal step | Public state |
| --- | --- |
| `draft`, `plan_proposed` | `planned` |
| `plan_approved` | `plan_approved` |
| `pulling`, `masking_sample`, `sample_running`, `full_running`, `quote_check`, `building_output`, `verifying` | `running` |
| `sample_ready`, `sample_approved`, `paused`, `stalled`, `done`, `done_with_issues`, `failed`, `cancelled` | unchanged |

There is no cancel; a stalled or paused job stays until it is resumed or a new
job is started.

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

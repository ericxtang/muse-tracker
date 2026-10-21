# Records reference

## Row contract

Each saved query returns rows with these column names (use `AS` aliases). Every
row needs `task_id`: whatever groups one agent task or run in `muse.db`. Rows
with the same `task_id` become one trace. Unknown extra columns are ignored.

| Query | Required | Optional |
| --- | --- | --- |
| `tool_calls` | `task_id`, `tool`, `started_at` | `id` or `seq` (stable per call), `ended_at`, `args`, `result`, `success`, `server` |
| `model_calls` | `task_id`, `at` | `message_id` or `id`, `ended_at`, `input_tokens`, `output_tokens`, `model`, `output_text`, `input_text`, `note` |
| `tasks` | `task_id`, `start`, `end` | `request_text`, `context_text`, `output_text`, `agent_id`, `surface`, `session_id`, `parent_task_id`, `status`, `kind`, `model`, `depth` |

Where the optional columns come from in `muse.db` (see `sample/schema.sql`):

| Column | Source | Used for |
| --- | --- | --- |
| `session_id` | `agent.agents.root_session_id`, else `session_id` | Groups a conversation with its subagents (Langfuse sessions) |
| `parent_task_id` | `agent.agents.parent_agent_id` | Which agent spawned a subagent |
| `output_text` (tasks) | `agent.agents.last_assistant_message` | The task's final answer, the root's output |
| `status`, `kind`, `model`, `depth` | `agent.agents` (`kind` or `agent_type`) | Filtering; `kind` names the trace in Langfuse |
| `output_text` (model calls) | `runtime.messages.body` for the usage row's `message_id` | The message the tokens paid for |
| `server` | `runtime.tool_calls.server_name` | Which MCP server a tool came from |

- Times may be ISO-8601 strings or epoch seconds/ms/µs/ns.
- `success` may be boolean, `0`/`1` or text such as `"ok"`/`"error"`. Leave it
  null when unknown; the span status then stays unset.
- `args` and `result` may be text or JSON. The CLI redacts common key shapes
  (bearer tokens, `sk-…`, GitHub and Slack tokens, AWS key IDs) and truncates
  each value to `maxAttributeChars`. In tool mode, also cap long columns in SQL
  (`substr(result, 1, 4000)`) to keep the tool output small.
- Stable `id`/`seq`/`message_id` values make span IDs stable. Without them the
  CLI derives an ID from the start time, tool name and arguments.
- `tasks` rows supply the root's input and surface: one row per agent, even
  for agents with no user message. Without one, a task's bounds come from its
  calls.
  - `request_text` is the agent's first user message.
  - `context_text` is the preamble a machine-spawned agent starts with instead,
    such as a subagent's context item that begins `[Subagent Context]`. The root
    shows `request_text` when there is one, otherwise `context_text`, and
    `metadata.request_source` says which (`user` or `context`). Without it,
    subagent, hook-worker and pipeline roots have no input.
  - `end` is the agent's last activity, and the query pages on it
    (`tasksTimeColumn: "end"`): an agent's row comes back in every window where
    it does something, so its root keeps the latest status and answer, and an
    agent that started before the first window still gets its input. `start`
    (its first activity) sets the root's start time.

## Query rules

- One read-only `SELECT` (or `WITH … SELECT`) per query. No `;`.
- Filter on the record's own time column, half-open: `>= :since_iso AND < :until_iso`
  (or the `_ms` pair). Compare like with like: if the column stores
  `2026-10-20 12:00:00`, compare `julianday(col)` with `julianday(:since_iso)`.
- Filter and order on the contract's time column for that query
  (`tool_calls.started_at`, `model_calls.at`, `tasks.end`). The CLI pages on
  it and rejects rows outside the window (`QUERY_MISMATCH`).
- `tool_calls` and `model_calls` must end with `LIMIT :limit`; give `tasks`
  one too (it must have one to be re-sent on its own). A full result
  ends the page at its last timestamp; the next page starts there. Rows at that
  timestamp move to the next page, so none are lost or split. More than
  `maxRows` rows at one timestamp stops with `PAGE_STUCK`; raise `maxRows`.
- In tool mode, `plan` inlines the parameters, so the `muse.db` tool receives
  plain SQL. In direct mode the CLI binds them.

Example shape (column names are illustrative; read the real schema first):

```sql
SELECT ci.run_id AS task_id, ci.seq AS seq, ci.tool_name AS tool,
       substr(ci.input_json, 1, 4000) AS args, substr(ci.output_text, 1, 4000) AS result,
       ci.created_at AS started_at, ci.completed_at AS ended_at, ci.ok AS success
FROM agent.context_items ci
WHERE ci.kind = 'tool_call'
  AND julianday(ci.created_at) >= julianday(:since_iso)
  AND julianday(ci.created_at) <  julianday(:until_iso)
ORDER BY ci.created_at, ci.seq
LIMIT :limit
```

## Leave out this skill's own runs

Every export's own tool calls land in `muse.db` and would be exported in the
next run, so telemetry keeps reporting on itself. If `muse.db` records which
worker lane a call ran in (earlier exports saw lanes such as
`event-hook:<hook id>` in `runtime.execute_resolve_runs`), filter out the
`event-hook:muse-tracker` lane in each query (and `event-hook:braintrust-telemetry`,
this skill's hook id before 0.0.6, if it ever ran). Record the filter you used
in `queries` so it is reviewed with them.

## What is known about muse.db

Observed 2026-10-14 to 2026-10-20 while building the first export by hand:

- Populated sources: `agent.context_items` (tool calls and outputs) and
  `agent.agent_message_token_usage` (per-message token counts).
- `runtime.tool_calls` and `agent.token_usage` were empty.
- Token usage is sparse: only user-visible assistant messages get
  `agent_message_token_usage` rows. Internal turns carry no token counts, so
  `llm` spans cover only those messages.
- `runtime.messages` and `runtime.events` hold message bodies and may supply
  `request_text` for the `tasks` query.

Observed 2026-10-21: `muse.db` creates an agent for every subagent, hook worker
and pipeline stage, not only conversations. Of 4,456 agents, 2,187 ever
received a user message. The rest got their instructions as a context
preamble: a subagent's first context item begins "[Subagent Context] You are
running as a subagent (depth 1/2)…". Read a few before choosing `substr`
bounds, so the part that states the task survives the cut. Some agents
(58 of 4,456) hold only that preamble: no calls, start equal to end.

## Runs, pages and duplicates

- **A run sends everything since the last export.** It covers the watermark
  minus `overlapMinutes` up to the moment it starts. The first run covers the
  past `backfillDays` (7), or from `startAt`; `backfill` sends older history
  later as a separate run that leaves the watermark alone. `backfill --resend`
  re-sends history already sent, rebuilt with the current queries, and
  `--only tasks` limits that to the task roots. Pages split it by row count; the worker, or
  `export` itself in direct mode, repeats until `more: false`.
- **The overlap catches late rows.** A tool call written when it finishes can
  carry a start time before the last export; re-reading the last 30 minutes
  picks it up.
- **Row ids make re-sends harmless.** Each row's `id` is
  `<root_span_id>-<span_id>`, both SHA-256 derived from `agentId`, `task_id`
  and the call's identity. Braintrust's insert API replaces an existing row
  with the same id (documented for `_is_merge` false, the default). Sending a
  record twice, after a crash, a lost state directory or a reinstall, leaves
  one row.
- **Unchanged rows are not re-sent.** The state keeps a hash of each row sent
  recently; overlap rows with identical content are skipped. A task's root row
  changes when later calls widen its bounds and is replaced in place.
- A failed page stays pending; the retry skips requests that already landed.
  Braintrust must confirm every row id or the page counts as failed.
- A run untouched for 15 minutes is treated as abandoned; the next hook wake
  resumes it from its cursor.

## Transport

`POST https://api.braintrust.dev/v1/project_logs/<projectId>/insert` with
`{"events": [...]}` and the vault-supplied bearer key. `check` reads
`GET /v1/project/<projectId>`. 429 and 5xx responses are retried with
exponential backoff, honoring `Retry-After`. Requests stay under
`maxPayloadBytes` (the insert endpoint's practical limit is about 6 MB).

| Row | `span_attributes` | Fields |
| --- | --- | --- |
| Task root | `agent_task`, type `task` | `input` = request text, else the context preamble; `metadata.task_id`, `agent_id`, `surface`, `request_source`; `metrics.start/end` |
| Model call | `model_call`, type `llm` | `metrics.prompt_tokens`, `completion_tokens`, `tokens`; `metadata.model`, `message_id` |
| Tool call | tool name, type `tool` | `input` = args, `output` = result; failures set `error`; `metadata.seq`, `success` |

Children carry `span_parents: [<root span_id>]`; times are unix seconds.

### Langfuse

Spans go as OTLP/HTTP JSON to `<langfuseHost>/api/public/otel/v1/traces` with
`x-langfuse-ingestion-version: 4`. Auth is `Authorization: Bearer <secret key>`
(what a Muse `api_key` card sends; Langfuse v4 resolves a bearer secret key to
full project access) or `Basic base64(public:secret)`. A bearer *public* key
only reaches scores and is refused for traces. The
trace id and span ids are the same as for Braintrust (32 and 16 hex characters),
so a task is one Langfuse trace.

| Our span | Langfuse observation | Attributes |
| --- | --- | --- |
| Task root | `agent` | `input` (request or preamble), `output` (`output_text`), task metadata |
| Model call | `generation` | `model.name`, `usage_details` `{input, output}`, `output` (message text) |
| Tool call | `tool` | `input` = args, `output` = result; a failure sets level `ERROR` and `status_message` |

Every span also carries the trace-level attributes, which Langfuse filters on
only when all spans have them: `langfuse.trace.name` (`muse <kind>`),
`langfuse.session.id` (`session_id`), `langfuse.trace.tags` (`muse-tracker`,
`muse-agent:<agentId>`, `kind`, `request_source`), `langfuse.trace.metadata.*`
(`task_id`, `agent_id`, `parent_task_id`, `status`, `surface`),
`langfuse.release`, and `langfuse.user.id` / `langfuse.environment` when the
config sets `userId` / `environment`.

**Each span is sent once.** Langfuse stores every write: a span sent again
with the same id is stored again ([docs](https://langfuse.com/faq/all/tracing-data-updates)).

- Children are sent once; the sent registry is checked by id, so the overlap
  re-reads nothing twice. A child waits until its task's row has been seen
  (the row comes with the task's last activity), so it carries the task's
  session and tags; the end of a run releases any still waiting.
- A task's root is held in the ledger until a run reaches the present and the
  task has been idle for `rootSettleMinutes`, then sent once with its final
  bounds. Activity after that (a task resumed hours later) still arrives as
  children of the same root, but the root's own end time does not move.
- A run that may repeat earlier sends (the first run, after lost state, or a
  backfill) asks Langfuse once whether it already holds this agent's spans in
  the window; if so, every page is checked before sending. A check is one paged
  query over the page's time range filtered by the `muse-agent:<agentId>` tag,
  so it costs a few reads however many traces the page touches.
- A write that fails without an answer (5xx, timeout) may have been stored: the
  page fails with `SEND_UNCERTAIN`, and its retry checks those traces first,
  after waiting until the failed send is `langfuseVisibilitySeconds` (30) old:
  Langfuse makes new spans readable a few seconds after accepting them.
- `backfill --resend` is refused; to redo traces, delete them in Langfuse first.
  `reconcile`, `find` and `probe` read back from Braintrust only.

**Plan limits** ([limits](https://langfuse.com/faq/all/api-limits)) and how the
export stays inside them:

| Limit (Hobby) | Effect | What the export does |
| --- | --- | --- |
| 30 reads a minute | Existence checks could be throttled | Checks are one query per page window, not per trace; reads are spaced `langfuseReadSpacingSeconds` (2.1) apart; a 429 waits for `Retry-After` |
| Reads and the UI cover the last 30 days | Older spans can't be checked for duplicates, and aren't shown | A backfill older than `langfuseReadWindowDays` (30) needs `--beyond-read-window`, meaning "never sent to this project"; checks never reach past the window |
| 1,000 ingestion requests a minute; 5 MB a request | Large batches refused | Batches are sized on the OTLP body, at most 4 MB; a 429 is retried |
| 50 deletes a day | — | The export never deletes |

On Core (90 days) or Pro (3 years), raise `langfuseReadWindowDays` and lower
`langfuseReadSpacingSeconds` to match the plan.

## Config

| Field | Default | Meaning |
| --- | --- | --- |
| `destination` | `braintrust` | The observability tool to send to: `braintrust` or `langfuse` |
| `langfuseHost` | `https://cloud.langfuse.com` | Langfuse origin (`us.`, `jp.`, `hipaa.` cloud regions, or self-hosted) |
| `tasksTimeColumn` | `start` | The tasks query's paging column: `end` (last activity; Setup writes this) or `start` (configs before setup version 5) |
| `rootSettleMinutes` | 120 | Langfuse: how long a task must be idle before its root is sent |
| `langfuseReadWindowDays` | 30 | Langfuse: how far back the plan lets reads (and the UI) see |
| `langfuseReadSpacingSeconds` | 2.1 | Langfuse: minimum time between reads (Hobby allows 30 a minute) |
| `langfuseVisibilitySeconds` | 30 | Langfuse: how long to wait before checking a send that failed without an answer |
| `userId`, `environment` | none | Langfuse: sent as the trace's user and environment |
| `provider` | `muse` | `muse` uses the Secure Vault; `env` reads `BRAINTRUST_API_KEY` (local testing) |
| `connector` | `custom.braintrust` | Vault connector name |
| `helperPath` | built-in | Path to Muse's `dynamic_credentials.py` |
| `projectId` | required | Braintrust project ID |
| `agentId` | `muse` | Seeds trace IDs; keep it fixed once exports start |
| `queries` | required | `tool_calls`, `model_calls`, optional `tasks` |
| `databases` | none | Schema → SQLite path; enables direct mode |
| `intervalMinutes` | 60 | Time between exports |
| `overlapMinutes` | 30 | How far each run reaches back before the last export |
| `settleMinutes` | 0 | Hold back records newer than this |
| `maxRows` | 500 | Rows per query per page |
| `maxAttributeChars` | 8000 | Truncation per text attribute |
| `maxPayloadBytes` | 2000000 | Largest insert request |
| `backfillDays` | 7 | How far back the first run reaches |
| `startAt` | none | Exact start for the first run, overriding `backfillDays` |

State (watermark, run in progress, recently sent row hashes, task bounds, hook timer) lives in
`~/hooks/state/muse-tracker-<hash of config path>/`. Keep it across
updates. Deleting it re-sends everything from `startAt`, which replaces the
same rows rather than duplicating them.

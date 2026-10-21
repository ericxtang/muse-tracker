# Records reference

## Row contract

Each saved query returns rows with these column names (use `AS` aliases). Every
row needs `task_id`: whatever groups one agent task or run in `muse.db`. Rows
with the same `task_id` become one trace. Unknown extra columns are ignored.

| Query | Required | Optional |
| --- | --- | --- |
| `tool_calls` | `task_id`, `tool`, `started_at` | `id` or `seq` (stable per call), `ended_at`, `args`, `result`, `success` |
| `model_calls` | `task_id`, `at` | `message_id` or `id`, `ended_at`, `input_tokens`, `output_tokens`, `model`, `note` |
| `tasks` | `task_id` | `start`, `end`, `request_text`, `agent_id`, `surface` |

- Times may be ISO-8601 strings or epoch seconds/ms/µs/ns.
- `success` may be boolean, `0`/`1` or text such as `"ok"`/`"error"`. Leave it
  null when unknown; the span status then stays unset.
- `args` and `result` may be text or JSON. The CLI redacts common key shapes
  (bearer tokens, `sk-…`, GitHub and Slack tokens, AWS key IDs) and truncates
  each value to `maxAttributeChars`. In tool mode, also cap long columns in SQL
  (`substr(result, 1, 4000)`) to keep the tool output small.
- Stable `id`/`seq`/`message_id` values make span IDs stable. Without them the
  CLI derives an ID from the start time, tool name and arguments.
- `tasks` rows supply the request text and surface. Without one, a task's
  bounds come from its calls.

## Query rules

- One read-only `SELECT` (or `WITH … SELECT`) per query. No `;`.
- Filter on the record's own time column, half-open: `>= :since_iso AND < :until_iso`
  (or the `_ms` pair). Compare like with like: if the column stores
  `2026-10-20 12:00:00`, compare `julianday(col)` with `julianday(:since_iso)`.
- Filter and order on the contract's time column for that query
  (`tool_calls.started_at`, `model_calls.at`, `tasks.start`). The CLI pages on
  it and rejects rows outside the window (`QUERY_MISMATCH`).
- `tool_calls` and `model_calls` must end with `LIMIT :limit`. A full result
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
`event-hook:braintrust-telemetry` lane in each query. Record the filter you used
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

## Runs, pages and duplicates

- **A run sends everything since the last export.** It covers the watermark
  minus `overlapMinutes` up to the moment it starts. The first run covers the
  past `backfillDays` (7), or from `startAt`; `backfill` sends older history
  later as a separate run that leaves the watermark alone. Pages split it by row count; the worker, or
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
| Task root | `agent_task`, type `task` | `input` = request text; `metadata.task_id`, `agent_id`, `surface`; `metrics.start/end` |
| Model call | `model_call`, type `llm` | `metrics.prompt_tokens`, `completion_tokens`, `tokens`; `metadata.model`, `message_id` |
| Tool call | tool name, type `tool` | `input` = args, `output` = result; failures set `error`; `metadata.seq`, `success` |

Children carry `span_parents: [<root span_id>]`; times are unix seconds.

## Config

| Field | Default | Meaning |
| --- | --- | --- |
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
`~/hooks/state/braintrust-telemetry-<hash of config path>/`. Keep it across
updates. Deleting it re-sends everything from `startAt`, which replaces the
same rows rather than duplicating them.

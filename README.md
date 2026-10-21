# muse-braintrust-bridge

Ship a Muse agent's real activity — tasks, model calls with token usage,
and tool calls — from Muse's internal records database (`muse.db`) to a
[Braintrust](https://www.braintrust.dev) project as OpenTelemetry traces.

Why this exists: Braintrust's setup tooling instruments applications and
coding-agent CLIs you create; it cannot hook a running Muse agent, and the
Muse runtime exposes no live instrumentation hooks. But `muse.db` holds
complete execution records, so this bridge exports them instead:

1. **Extract** execution records from `muse.db`
   (`agent.context_items` for tool calls/outputs,
   `agent.agent_message_token_usage` for per-message token counts)
   into `records.json`.
2. **Convert** with `bridge/build_otlp.py` into an OTLP/HTTP JSON payload
   (`otlp.json`), one trace per agent task: a root `task` span, `llm` spans
   for model calls, `tool` spans for tool calls.
3. **Send** with `bin/send_trace.py`, which POSTs to
   `https://api.braintrust.dev/otel/v1/traces` with the API key supplied by
   Muse's Secure Vault (the script never sees the key) and an
   `x-bt-parent: project_id:<id>` routing header.

## Attribute mapping

Braintrust only maps documented OpenTelemetry attribute names into its
structured fields — anything else lands in `metadata`, span types default
to `task`, and input/output stay empty. The builder therefore uses:

| Span | Attributes |
| --- | --- |
| Root task | `braintrust.span_attributes.type` = `task`, `braintrust.input` = the user request |
| Model call | type `llm`; `gen_ai.usage.prompt_tokens` / `gen_ai.usage.completion_tokens` → token metrics |
| Tool call | type `tool`; `gen_ai.tool.name`; `braintrust.input` = args, `braintrust.output` = result; failures also set `exception.message` |

See `CONNECTOR.md` for the full connector notes, including the extraction
sources and their limits (token usage is recorded only for user-visible
assistant messages; the bridge ships historical records, not a live span
stream).

## Sample database

So you can develop against the bridge without a live Muse install, `sample/`
carries the muse.db schema for the tables the bridge touches:

- `sample/schema.sql` — PostgreSQL DDL with the exact column definitions
  from the real muse.db catalog: `agent.context_items` and
  `agent.agent_message_token_usage` (the bridge's sources), plus the tables
  their foreign keys point at (`agent.agents`, `runtime.messages`,
  `runtime.tool_calls`, `runtime.tool_outputs`). muse.db has ~200 tables in
  total; this is the self-contained subgraph the bridge needs.
- `sample/make_sample.py` — builds `sample_muse.db`, a SQLite file with the
  same tables/columns (schema-qualified names, quoted) and fully synthetic
  seed data: one agent runs one short task — book a restaurant table; first
  attempt fails, second succeeds — so you can see how a task arc is laid out
  across the tables.

The generated `sample_muse.db` is not committed (it's a build artifact);
run the script to create it. End-to-end development loop:

```bash
python3 sample/make_sample.py                              # build the sample db
python3 bridge/extract_records.py --db sample/sample_muse.db --out records.json
python3 bridge/build_otlp.py                               # records.json -> otlp.json
python3 bin/send_trace.py otlp.json                        # optional: needs a Braintrust key
```

`bridge/extract_records.py` is the reference extractor: it pairs each
`tool_call` context item with the `tool_output` that follows it, unwraps
the JSON-string-encoded tool arguments, and joins token usage to the
assistant messages it belongs to.

## Not included

`records.json` / `otlp.json` from a real run are generated from actual agent
activity and are deliberately not committed (the sample pipeline above
produces synthetic equivalents).

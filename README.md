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

## Not included

`records.json` / `otlp.json` are generated from real agent activity and are
deliberately not committed.

# Braintrust connector (custom.braintrust)

Sends OpenTelemetry traces to the user's Braintrust project. Built 2026-10-14
for tracing the Muse agent's own activity (tool calls + per-message token
usage read from muse.db).

## Setup

- `credentials.request_api_access` with provider `braintrust`,
  api_hosts `["api.braintrust.dev"]`, auth_scheme `api_key`, placement
  `bearer_header`. The user creates the key at braintrust.dev → Settings →
  API Keys (needs write permission on the target project) and enters it in
  the secure form. The key lives in the Secure Vault; no tool reads it back.

## Operations

### Send an OTLP trace

`python3 ~/workspace/custom-connectors/custom.braintrust/bin/send_trace.py <otlp.json>`

- POSTs OTLP/HTTP JSON to `https://api.braintrust.dev/otel/v1/traces`
- Headers: `Authorization: Bearer <key>` (via vault surrogate),
  `x-bt-parent: project_id:22e9848a-b189-4236-a72d-b4474a771d6b`
- Traces land in the project's Logs view in the Braintrust dashboard.
- Verified working 2026-10-20: first send returned HTTP 200 and spans were confirmed in the project via BTQL (project_logs query needs the project id quoted). Project "My Project", org "PersonalEric".

## Bridge (trace construction)

`~/workspace/braintrust/bridge/`:
- `records.json` — execution records extracted via `muse.db` from
  `agent.context_items` (tool calls/outputs) and
  `agent.agent_message_token_usage` (per-message tokens).
- `build_otlp.py` — converts records to OTLP JSON (`otlp.json`), writes the
  trace id to `trace_id.txt`.

### Attribute mapping (per Braintrust OTel docs, verified 2026-10-20)

Braintrust only maps documented attribute names into structured fields;
anything else sits in `metadata`, span types default to `task`, and
input/output stay empty. The builder therefore uses:

- Root task span: `braintrust.span_attributes.type` = `task`,
  `braintrust.input` = the user request text.
- Model calls: `braintrust.span_attributes.type` = `llm`,
  `gen_ai.usage.prompt_tokens` / `gen_ai.usage.completion_tokens` (ints)
  → `metrics.prompt_tokens` / `metrics.completion_tokens`.
- Tool calls: `braintrust.span_attributes.type` = `tool`,
  `gen_ai.tool.name`, `braintrust.input` = args, `braintrust.output` =
  result; failures also set `exception.message` (populates `error`).

Verified by BTQL after the corrected send (trace 90a01694…): 2 llm spans
with token metrics, 16 tool spans with input/output, root task span with
input. The earlier send (trace 36cb7892…) used ad-hoc names
(`llm.input_tokens`, `tool.name`, `task.request`) and stored flat — kept
in the project as the before/after comparison.

## Notes

- Token usage is recorded sparsely: only user-visible assistant messages get
  `agent_message_token_usage` rows; internal turns have no token attribution.
- `runtime.tool_calls` / `agent.token_usage` tables are empty; the populated
  sources are `agent.context_items` and `agent.agent_message_token_usage`.
- There is no live runtime hook: the bridge ships historical records, not a
  live span stream. A cron can re-run extraction + send periodically.

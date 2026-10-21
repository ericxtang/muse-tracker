# muse-tracker

A Muse skill that tracks what a Muse agent does (its tasks, model calls with
token counts, and tool calls with inputs and outputs) and sends that record, on
a schedule, to an observability tool as traces.

**Destinations.** Braintrust and Langfuse are supported. The config's
`destination` setting picks one; the record model, scheduling and paging are
shared. See
[Destinations](#destinations) for what is shared, what is Braintrust-specific
today, and what a new tool must offer.

## Install

Tell Muse:

> Follow https://github.com/ericxtang/muse-tracker/releases/latest/download/setup.md to set up the Muse tracker.

Muse downloads the release, checks its hash against the manifest, installs it,
writes the queries, runs a dry run and the first export, and installs the hook.
It reuses an existing `custom.braintrust` vault card and the project an
existing Braintrust connector names, so setup asks no questions when those
exist.

**Moving from `braintrust-telemetry`.** This skill had that name before 0.0.6.
The setup guide detects an old install and runs `migrate`, which copies its
config and export state (watermark, run in progress, sent rows) to the new
names, so the export continues where it stopped. Row ids do not depend on the
skill's name, so nothing already sent is duplicated. Until it moves, an old
install keeps exporting with the files it has; it no longer receives updates.

## Why muse.db

Muse exposes no live instrumentation hooks, and tracing tools' setup flows
cannot attach to a running Muse agent. But Muse's records database, `muse.db`,
holds complete execution records. The skill reads those after the fact, so it
needs no change to Muse and sees everything Muse recorded, including history
from before it was installed.

## Destinations

| Destination | Status | How rows are written | Read-back for verification |
| --- | --- | --- | --- |
| [Braintrust](https://www.braintrust.dev) | Supported | `POST /v1/project_logs/<project>/insert`; replaces a row with the same `id` | BTQL (`reconcile`, `find`, `probe`) |
| [Langfuse](https://langfuse.com) | Supported | OTLP to `/api/public/otel/v1/traces`; stores every write, so each span is sent once | Not yet (use the Langfuse UI) |

**The two differ in one way that shapes everything: Braintrust replaces a row
sent again; Langfuse stores it again.** Against Braintrust the export can
re-send freely (the overlap, retries, a root whose bounds grew). Against
Langfuse it sends each span once: the sent registry is checked by id, a task's
root waits until the task is idle, and runs that may repeat earlier sends ask
Langfuse what it already holds. Details are in the
[records reference](skills/muse-tracker/references/records.md#langfuse).

**Shared by every destination:** the hook, scheduling, the `muse.db` queries
and row contract, paging, the watermark, the record-derived ids, redaction, and
the release and update machinery.

**Per destination:** the transport and auth (`send`/`langfuse_send`), the
row format (Braintrust log rows; OTLP spans with Langfuse attributes), the
Secure Vault card (`custom.braintrust` or `custom.langfuse`), and `check`. The
read-back commands (`reconcile`, `find`, `probe`) exist for Braintrust only.

**What a new destination must offer:**

1. **Idempotent writes.** Writing a row twice under the same id must leave one
   row. Without that, the overlap and every retry create duplicates. Check this
   in the tool's documentation and with a probe before adding it. This is why
   the Braintrust export uses its insert API rather than OpenTelemetry: Braintrust
   does not document how OTel ingestion assigns row ids.
2. **A trace model** with parent/child spans, start and end times, and numeric
   metrics for token counts.
3. **An API key** that a Secure Vault card can hold, restricted to the tool's
   API host. Muse never sees the key; the vault adds it to each request.
   Langfuse v4 accepts its secret key as a bearer token, so a standard Muse
   `api_key` card works; Setup falls back to a card carrying a `Basic` header.
4. **A way to read rows back** by id or trace, so `reconcile` can compare what
   `muse.db` holds with what the tool stored. Optional, but without it a
   destination cannot be verified.

## Layout

| Path | What it is |
| --- | --- |
| `skills/muse-tracker/` | The skill: `SKILL.md`, `assets/export-hook.sh`, `scripts/muse_tracker.py`, `references/records.md` |
| `docs/agent-setup.md` | The install, update and migration guide, served as `setup.md` |
| `docs/verification.md` | Step-by-step check of a live install |
| `build.py` | Stamps the version and release address into the skill, builds a release and publishes it to GitHub |
| `tests/` | End-to-end tests against a mock Braintrust API and a SQLite stand-in for `muse.db` |
| `sample/` | `muse.db` schema (from the real catalog) and a synthetic sample database |
| `bin/bt-local` | Runs the CLI on this Mac with the Braintrust key from the macOS keychain |

## Hosting

Releases are [GitHub releases](https://github.com/ericxtang/muse-tracker/releases)
of this public repo; there is no server. Each release carries four assets: the
skill archive (`muse-tracker-<version>-<sha256>.zip`), `manifest.json`,
`setup.md` and `SKILL.md`. Installs read the manifest at
`releases/latest/download/manifest.json`, which GitHub redirects to the newest
release.

**Records never pass through GitHub.** Muse reads `muse.db` and sends rows
straight to the destination's API with the vault-held key. GitHub sees only
manifest checks (when an export is due) and downloads (on install and update).

## Releases and updates

**Installs always run the latest release.** Manifest reads bypass every cache,
and each release's archive lives at an address that contains its version and
hash (`muse-tracker-<version>-<sha256>.zip`, cached as immutable), so a cached
archive can never pass for a newer one. When an export is due, the hook compares
the installed files with the latest manifest; if they differ, Muse runs
`self-update`, which downloads that exact archive, checks it and every file, and
replaces the bundled files in place.

**A version always names the same content.** `VERSION` holds it, and it is
stamped into the CLI, `SKILL.md`, the manifest and the archive name.
`releases.json` records each version's source digest, and `build.py` refuses
sources that differ from their version's record. To check an install:

```sh
python3 scripts/muse_tracker.py version
# {"installed": "0.0.6", "latest": "0.0.6", "upToDate": true, ...}
```

`upToDate` compares file hashes, not version numbers, so a hand-edited or
half-updated install reports `false`.

**Setup changes.** When a release changes something Setup wrote (a query, the
hook), bump `SETUP_VERSION` in `build.py` and describe the change under "Setup
changes" in `SKILL.md`. Installs report `setupRequired: true` on every wake
until Muse makes the change and runs `setup-done`, so a change cut short is
finished at the next wake.

To release:

```sh
echo 0.0.7 > VERSION                # bump
python3 build.py --release          # record the new sources
python3 -m unittest discover -s tests
git push origin main
python3 build.py --publish          # creates GitHub release vVERSION; installs update at their next export
```

`--publish` refuses a version that already has a release.

Add an entry to [`CHANGELOG.md`](CHANGELOG.md).

## Verifying a live install

[`docs/verification.md`](docs/verification.md) walks through it: probe the
destination's replace-by-id behaviour, onboard Muse, trace a known task,
reconcile `muse.db` against what was stored, inject faults, then soak for a
week.

## Sample database

`sample/schema.sql` is the PostgreSQL DDL, taken from the real `muse.db`
catalog, for the tables the skill reads (`agent.context_items`,
`agent.agent_message_token_usage`) and the tables their foreign keys point at.
`sample/make_sample.py` builds `sample/sample_muse.db`, a SQLite twin with
synthetic seed data. `bridge/extract_records.py` is the earlier reference
extractor, kept for its pairing of each `tool_call` with its `tool_output`.

```sh
python3 sample/make_sample.py      # writes sample/sample_muse.db (not committed)
```

## Tests

```sh
python3 -m unittest discover -s tests -v
python3 tests/e2e.py                 # end-to-end run of a built release; prints a report
```

With `--langfuse-live URL` (and `LIVE_LANGFUSE_PUBLIC_KEY` / `LIVE_LANGFUSE_SECRET_KEY`),
the Langfuse phases run against a real server instead, through a stand-in for
Muse's Secure Vault (`tests/live_langfuse.py`): the CLI runs with provider
`muse` and holds only a surrogate, and a TLS-intercepting proxy swaps in the
secret, as Muse's egress proxy does. A local server takes one command:
`docker compose up` with Langfuse's
[compose file](https://github.com/langfuse/langfuse/blob/main/docker-compose.yml)
and its `LANGFUSE_INIT_*` variables.

`tests/e2e.py` builds a release, publishes it to a mock GitHub, installs it as
`setup.md` does, and drives the installed CLI the way a Muse worker does
against a `muse.db` with the real table layout, once for a mock Braintrust and
once for a mock Langfuse that keeps every write. It checks the mapping, the
overlap, lost state, failed and uncertain sends, held roots, `reconcile`, and
self-update, and exits non-zero if any check fails.

The mock Braintrust stores rows by id with replace semantics; the queries run
against a real SQLite file. Covered: a large first run across many pages, rows
sharing a page-boundary timestamp, late rows caught by the overlap, a full
re-export after losing state (still one row per record), retry after a partial
send, 401 and 429 handling, a task spanning two runs, run resumption, subagent
roots from their preamble, a tasks-only resend that repairs roots already sent,
migration from `braintrust-telemetry` mid-run, the hook script under a stub
runtime, reproducible builds, self-update and `setupRequired`.

## Known limits

- **Cost on busy agents.** Muse's `muse.db` is PostgreSQL, reachable only
  through the `muse.db` tool, so every page of rows passes through the agent's
  context. An agent with tens of thousands of calls a week spends real tokens on
  each export. Shorter `substr` caps in the saved queries are the main lever.
- **Direct mode** (the CLI reading SQLite files itself) works in tests but does
  not apply to a real `muse.db`.
- **`find`'s arrival lag is always 0**: rows are sent with `created` set to the
  event's start, so Braintrust's `created` is not an arrival time.
- **Unverified against live data:** how stable `muse.db`'s task and call ids
  are (row ids derive from them), and the exact layout of subagent preambles.

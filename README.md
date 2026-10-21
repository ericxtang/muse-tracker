# muse-braintrust-bridge

A Muse skill, `braintrust-telemetry`, that ships a Muse agent's real activity
(tasks, model calls with token usage, tool calls) from Muse's records database
(`muse.db`) to a [Braintrust](https://www.braintrust.dev) project as traces, on
a schedule, through Muse's native hooks.

Muse exposes no live instrumentation hooks, and Braintrust's setup tools cannot
attach to a running Muse agent. But `muse.db` holds complete execution records,
so the skill exports those instead.

## How it works

```
native hook (every 5 min)          worker (when an export is due)
assets/export-hook.sh ──wake──▶  plan ─▶ muse.db queries ─▶ export ─▶ Braintrust
  local timer only; no              next page    saved at       insert API,
  credentials, no worker            of the run   setup          vault key
  unless an export is due           └──────── repeat until more: false ────────┘
```

- **Each wake sends everything since the last export.** A run covers the last
  export's end, minus a 30-minute overlap for rows written late, up to now. The
  first run covers the past 7 days; `backfill --days N` sends older history on
  request. Big runs are paged by row count; the worker
  loops until `more: false` (direct mode does it in one command). A run cut
  short is resumed by the next wake.
- **Re-sending never duplicates.** Every row goes to Braintrust's
  `project_logs/<id>/insert` with an `id` derived from the record (SHA-256 of
  agent, task and call identity). Braintrust replaces a row with an existing
  id, so a retry, the overlap, a lost state directory or a full re-export all
  leave one row per record. A local hash of recently sent rows also skips
  unchanged ones, so the overlap costs almost nothing.
- **Queries** are written once at setup against the real `muse.db` schema. In
  *tool mode* the worker runs them through the `muse.db` tool; in *direct mode*
  (SQLite files readable from the shell) the CLI runs them itself.
- **The hook** checks a local timer and wakes a worker only when an export is
  due (`intervalMinutes`, default 60). It backs off after repeated failures and
  never loads credentials. The person hears about a failure once (again after
  24 hours if it persists).

OTLP was dropped on purpose: Braintrust does not document how its OTel
ingestion assigns row ids, so a re-sent span might duplicate. The insert API
documents replace-by-id.

See [`SKILL.md`](skills/braintrust-telemetry/SKILL.md) for the agent's
procedure and [`references/records.md`](skills/braintrust-telemetry/references/records.md)
for the row contract, attribute mapping and config.

## Layout

| Path | What it is |
| --- | --- |
| `skills/braintrust-telemetry/` | The skill source: `SKILL.md`, `assets/export-hook.sh`, `scripts/braintrust_telemetry.py`, `references/records.md` |
| `docs/agent-setup.md` | The install/update guide served as `setup.md` |
| `build.py` | Renders `{{BASE_URL}}` and writes the release to `dist/` |
| `serve.py`, `Dockerfile` | The Railway service: builds the release for its own URL at startup and serves it |
| `tests/` | End-to-end tests against a mock Braintrust API and a real SQLite stand-in for `muse.db` |

## Release and install

Installation follows the Let's Swap skill: Muse reads a published setup guide,
downloads a hashed ZIP, checks it against the manifest, installs the folder and
runs `setup-check`, which compares the installed bytes with the manifest.

**Latest version, always.** Every manifest read bypasses caches (`no-store`,
plus a unique query string). Each release's archive lives at an address that
contains its hash (`braintrust-telemetry-<sha256>.zip`, cached as immutable),
so a cached archive can never pass for a newer release. `self-update` fetches
the manifest, downloads that exact archive, checks it and every file, and swaps
the four bundled files in place. The hook compares the installed files with
the latest manifest on every wake and has the worker run `self-update` before
exporting when they differ.

```sh
python3 build.py --base-url https://skill-production-44c7.up.railway.app
# dist/skills/braintrust-telemetry.zip
# dist/skills/braintrust-telemetry/{manifest.json,SKILL.md,setup.md}
```

Hosted on Railway (project `muse-tracker`, service `skill`), linked to this repo's `main` branch: a push deploys. The service runs `serve.py`, which builds `dist/` for its own public
URL at startup. The archive is reproducible, so the same source always gives
the same hash. Then tell Muse:

> Follow https://skill-production-44c7.up.railway.app/skills/braintrust-telemetry/setup.md to set up Braintrust telemetry.

Setup reuses the existing `custom.braintrust` vault card if there is one. The
earlier hand-run export went to project "My Project" (org "PersonalEric"),
`22e9848a-b189-4236-a72d-b4474a771d6b`. Its two hand-sent traces (`36cb7892…`,
`90a01694…`) used random ids, so the skill's first run will add its own copy of
that task; delete the old rows if they get in the way.

## Versions and releasing

The release version lives in `VERSION` and is stamped into the CLI
(`braintrust_telemetry.py --version`), `SKILL.md` frontmatter, the manifest
(`version`) and the archive name (`braintrust-telemetry-<version>-<sha256>.zip`).
To check an install:

```sh
python3 scripts/braintrust_telemetry.py version
# {"installed": "0.0.1", "latest": "0.0.1", "upToDate": true, ...}
```

`upToDate` compares the installed files with the published ones, so a
hand-edited or half-updated install reports `false` even when the numbers
match. The hook payload carries `installedVersion` and `latestVersion` too.

A version always names the same content. `releases.json` records each
version's source digest, and `build.py` (which the Railway service runs at
startup) refuses sources that differ from their version's record. To release:

```sh
echo 0.0.2 > VERSION                # bump
python3 build.py --release          # record the new sources
python3 -m unittest discover -s tests
railway up --service skill          # deploy; installs pick it up on their next wake
```

Add an entry to `CHANGELOG.md` for people. Bump `SETUP_VERSION` in `build.py`
only when installed agents must change what Setup wrote (for example, a
query or the hook prompt), and describe the change under "Setup changes" in
`SKILL.md`. Installed agents see `setupRequired: true` on every wake until they
make it and run `setup-done`.

## Verifying a live install

[`docs/verification.md`](docs/verification.md) is the step-by-step check:
probe Braintrust, onboard Muse, trace a known task, reconcile, inject faults,
then soak for a week. `bin/bt-local` runs `check`, `probe` and `find` on this
Mac with the key read from the macOS keychain.

## Sample database

`sample/schema.sql` is the PostgreSQL DDL, taken from the real `muse.db`
catalog, for the tables the skill reads (`agent.context_items`,
`agent.agent_message_token_usage`) and the tables their foreign keys point at.
`sample/make_sample.py` builds `sample/sample_muse.db`, a SQLite twin with
synthetic seed data (one short task: a restaurant booking that fails, then
succeeds). `bridge/extract_records.py` is the earlier reference extractor: it
pairs each `tool_call` item with the `tool_output` after it and unwraps the
JSON-string-encoded arguments. It fed the removed OTLP builder; keep it for
the pairing logic.

```sh
python3 sample/make_sample.py      # writes sample/sample_muse.db (not committed)
```

## Test

```sh
python3 -m unittest discover -s tests -v
```

The mock Braintrust stores rows by id with replace semantics; tool mode is
tested by running the plan's rendered SQL against a real SQLite file. Covered:
all history in one wake across many pages, rows sharing a page-boundary
timestamp, late rows caught by the overlap, a full re-export after losing
state (still one row per record), retry after a partial send, 401 and 429
handling, a task spanning two runs, direct mode, run resumption, subagent
roots taking their input from their preamble, a tasks-only resend that
repairs roots already sent, the hook script under a stub runtime, and
reproducible builds with `setup-check` and `setupRequired`.

## Not verified

Tests stub the Muse runtime and Braintrust. Still needs a live Muse run:

- the real `muse.db` column names (setup writes the queries from them), and
  that `task_id` and per-call ids there are stable (row ids derive from them);
- whether `muse.db` is reachable as files (direct mode) or only through the tool;
- how long a large first run takes in tool mode, where every page passes
  through the agent's context.

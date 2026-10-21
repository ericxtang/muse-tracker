# Changelog

Versions follow MAJOR.MINOR.PATCH. `releases.json` records the source digest of
each version; the build refuses sources that do not match their version.

## 0.0.8 — 2026-10-21

- **Langfuse destination** (`"destination": "langfuse"`). Spans go to
  Langfuse's OTLP endpoint as `agent`, `generation` and `tool` observations
  with input, output, model, token usage, error level and trace-level session,
  tags and metadata. Langfuse stores every write, so each span is sent once:
  children on first sight, a task's root once the task is idle
  (`rootSettleMinutes`), and runs that may repeat earlier sends check Langfuse
  first. A send that fails without an answer is checked before its retry.
- More of `muse.db` in every trace, for both destinations: optional
  `session_id`, `parent_task_id`, `status`, `kind`, `model`, `depth` and
  `output_text` on tasks, `output_text` on model calls, `server` on tool calls.
- Setup version 4: saved queries add those columns.

## 0.0.7 — 2026-10-21

- Releases are GitHub releases of the public `ericxtang/muse-tracker` repo;
  the Railway server is gone. Installs read
  `releases/latest/download/manifest.json` and follow GitHub's redirects, but
  only over HTTPS to github.com and GitHub's download hosts; the archive's
  SHA-256 is still checked against the manifest.
- `build.py --publish` creates the release for `VERSION` and refuses one that
  already exists.

## 0.0.6 — 2026-10-21 (never published; superseded by 0.0.7)

- The skill is now **`muse-tracker`** (was `braintrust-telemetry`): new install
  folder, config folder, hook id, CLI name (`scripts/muse_tracker.py`) and
  release address (`/skills/muse-tracker/`).
- `migrate --from OLD_CONFIG` carries a `braintrust-telemetry` install over,
  config and export state included, so its export continues where it stopped
  without duplicating rows. The setup guide runs it when it finds an old install.
- The config's `destination` (default `braintrust`) names the observability
  tool; Braintrust is the only one supported so far.
- Setup version 3: saved queries leave out the new hook's own runs
  (`event-hook:muse-tracker`).

## 0.0.5 — 2026-10-21 (never published)

- `setupRequired` no longer goes away after one `self-update`. The config
  records its `setupVersion` (missing means 1), and the wake payload,
  `self-update`, `setup-check` and `status` all report `setupRequired: true`
  until the new `setup-done` command records the changes. A setup change cut
  short is finished at the next wake.
- The setup version is stamped into the CLI; new configs start with it.

## 0.0.4 — 2026-10-21 (never deployed; superseded by 0.0.5)

- Roots for agents with no user message (subagents, hook workers, pipeline
  stages) take their input from the preamble they started with, such as
  `[Subagent Context] …`, via the new `context_text` column of the `tasks`
  query. `metadata.request_source` says `user` or `context`.
- `backfill --resend` re-sends history already sent, rebuilt with the current
  queries; `--only tasks` limits it to task roots.
- A `tasks` query with `LIMIT :limit` now pages like the others.
- Setup version 2: installed agents rewrite their `tasks` query and repair the
  roots already sent, on their own, at the next wake.

## 0.0.3 — 2026-10-21

- The first export sends the past 7 days (`backfillDays`) instead of all
  history, and setup no longer asks how far back to go.
- Setup takes the project from an existing Braintrust connector or bridge
  notes and asks only when none is found.
- `backfill --days N` (or `--since`) sends older history on request without
  moving the hourly watermark; `status` shows `historyFrom`.

## 0.0.2 — 2026-10-21

- Verification commands: `reconcile` (muse.db vs. what Braintrust stored:
  missing, extra, duplicate and mismatched rows), `find` (marker search with
  arrival lag) and `probe` (replace-by-id check on a scratch project).
- Tool mode sizes pages to stay near `maxPageBytes` (100 KB) so a page fits in
  the agent's context; the row limit adapts between pages.
- Every row carries `metadata.source = "muse.db"`.
- `status` reports `lagMinutes` and run, page, row and failure counters.

## 0.0.1 — 2026-10-21

First versioned release.

- Each wake sends everything since the last export, paging by row count; the
  first run sends all history.
- Rows go to Braintrust's `project_logs` insert API with ids derived from the
  record, so re-sending replaces rather than duplicates.
- `self-update` installs the latest release in place; archives are
  content-addressed; the hook checks installed files on every wake.
- `version` reports installed vs latest; the version is stamped into the CLI,
  `SKILL.md`, the manifest and the archive name.

Earlier builds were unversioned and never installed.

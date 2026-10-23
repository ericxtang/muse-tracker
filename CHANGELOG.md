# Changelog

Versions follow MAJOR.MINOR.PATCH. `releases.json` records the source digest of
each version; the build refuses sources that do not match their version.

## 0.0.12 — 2026-10-23

- **Stop recurring GitHub permission prompts.** Hook polls, dry runs and
  export wakes use the installed skill without checking GitHub. Updates now
  happen only on request with `self-update`; `version` is offline unless
  called with `--check-updates`. Missing setup receipts and old wake payloads
  must not start background update attempts.
- An abandoned export now honors the retry timer after a recovery wake,
  preventing another worker from starting on every five-minute poll while
  the first one has not made progress.
- Release fetches use stable URLs with cache-revalidation headers instead of
  random query strings. Archive and file hash verification is preserved.
- Setup version 7 refreshes the hook prompt to prohibit background release
  checks and preserves a paused hook's disabled state. Setup explains the
  difference between Muse's site approval and a one-task approval, and stops
  when release access is denied or expires instead of creating more cards.

## 0.0.11 — 2026-10-21

- **An agent that is still active keeps its root.** The tasks query's `end` now
  stops at the window's end (`created_at < :until_iso`). Before, an agent active
  while the query ran (such as the conversation running the export) had its
  last activity past the window, dropped out of every window, and its root had
  no input until it went quiet.
- The CLI rejects a limited query whose rows are not in its paging column's
  order (`QUERY_MISMATCH`). Such a query lets `LIMIT` keep the wrong rows, and
  the page cut then drops the rest without a trace; a tasks query still ordered
  on `start` after setup version 5 is the case this catches.
- Setup version 6: bound `end` in `queries.tasks`, and guard each query's
  own-run filter against NULLs (with `NOT IN`, one NULL empties the result). Setup version 5 now queues
  its root re-send before exporting, and retries it in the same turn after a
  `BUSY`.

## 0.0.10 — 2026-10-21

- **Task roots follow the agent's last activity.** The `tasks` query now pages
  on `end` (`tasksTimeColumn: "end"`), so an agent's row comes back whenever
  it does something new. Roots of agents that started before the first export
  window now get their input, kind, session and status, and a root's status and
  final answer stay current instead of freezing at the agent's start.
- Langfuse: a child span waits until its task's row has been seen, so every span
  carries the task's session and tags (`childrenHeld` in `export` and `status`).
- Setup version 5: rewrite `queries.tasks` to page on `end`; Braintrust installs
  then re-send task roots once to repair those already sent.

## 0.0.9 — 2026-10-21

- **Langfuse auth works with a standard Muse card.** Verified against a real
  Langfuse v4.67.0: a bearer *secret* key has full project access (a bearer
  public key reaches only scores). Setup now requests the same `api_key` /
  `bearer_header` card as Braintrust, holding the secret key, with a
  `Basic`-header card as the fallback.
- **Langfuse plan limits.** Existence checks are one query per page window
  (by the agent's tag) instead of one per trace; reads are spaced to stay under
  Hobby's 30 a minute; batches are sized on the OTLP body (≤ 4 MB); a backfill
  older than the plan's read window (`langfuseReadWindowDays`, 30) needs
  `--beyond-read-window`.
- A send that failed without an answer is checked only once it is
  `langfuseVisibilitySeconds` old: real Langfuse makes spans readable a few
  seconds after accepting them.
- `tests/e2e.py --langfuse-live URL` runs the Langfuse phases against a real
  server through a stand-in for Muse's Secure Vault.

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

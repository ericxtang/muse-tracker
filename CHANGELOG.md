# Changelog

Versions follow MAJOR.MINOR.PATCH. `releases.json` records the source digest of
each version; the build refuses sources that do not match their version.

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

# Verifying braintrust-telemetry

Seven phases, about 1.5 hours of your time spread over a week. Each step says
what to do, what passing looks like, and what to stop on. Commands starting
with `!` run in a Claude Code session in this repo, so the output lands where
I can read it. Messages in quotes go to Muse.

| Phase | What it proves | Time |
| --- | --- | --- |
| 0. Prepare | Keys, projects, local runner | 5 min |
| 1. Probe Braintrust | Re-sending replaces rows instead of adding them | 3 min |
| 2. Onboard Muse | Setup works and installs the latest release | 20 min |
| 3. Known task | One real task arrives exactly right | 15 min |
| 4. Reconcile | Nothing missing, extra, duplicated or altered | 10 min, then daily |
| 5. Break it | Recovery from each failure | 30 min over days 2–6 |
| 6. Soak | It keeps working unattended | 2 min a day for 7 days |

## 0. Prepare

1. In Braintrust, create a project called **telemetry-probe**. Copy its ID
   (project settings) and the ID of the project that will receive telemetry
   (today: "My Project", `22e9848a-b189-4236-a72d-b4474a771d6b`).
2. Create an API key that can read, write and delete logs in both projects.
   Muse uses the same key, so you can reuse the one in its vault or make a new
   one.
3. Put the key in the macOS keychain. The command prompts for it, so it never
   appears on screen or in this chat:
   ```sh
   security add-generic-password -s braintrust-api -a "$USER" -w
   ```
4. `local/config.json` already points at My Project. If telemetry goes
   elsewhere, edit `projectId` there.

## 1. Probe Braintrust

```
! bin/bt-local check
! bin/bt-local probe --project <telemetry-probe ID>
```

- **Pass:** `check` returns `projectName`. `probe` returns `"ok": true` with
  every check true, most importantly `oneRowPerIdAfterThreeSends` and
  `resendReplacesContent`. Note `visibleAfterSeconds`, how long Braintrust
  takes before a write can be read back.
- **Stop if** `oneRowPerIdAfterThreeSends` or `resendReplacesContent` is false.
  The no-duplicates design rests on that behavior. Send me the output.
- `cleanedUp: false` only means a delete was slow to show; check the scratch
  project a minute later.

## 2. Onboard Muse

1. In a Muse chat:
   > Follow https://skill-production-44c7.up.railway.app/skills/braintrust-telemetry/setup.md to set up Braintrust telemetry.
2. It should ask nothing when a working `custom.braintrust` card and a known
   project already exist. Otherwise it asks only to connect the card or for the
   project ID. **Pass:** no question about history (the past 7 days is the
   default), done in about 15 minutes, never asks you to paste the key into
   chat, and the closing message names the project and says older history is
   available on request.
3. Before it installs the hook, it shows you a dry-run preview. Ask:
   > Show me the saved queries and the first five rows of the dry-run preview.

   Paste both here. I check that the queries filter and order on the right time
   column, cut long columns with `substr`, leave out the telemetry hook's own
   runs, and that the preview groups calls into the right tasks.
4. When setup finishes, ask:
   > Show me the output of `version`, `check`, `status`, and the hook's saved definition.

   | Pass | Where |
   | --- | --- |
   | `upToDate: true`, `installed` = `latest` | `version` |
   | `projectName` is your project | `check` |
   | `watermark` set, `runInProgress: null`, `failure: null` | `status` |
   | id `braintrust-telemetry`, enabled, poll 300 s, delivery = this chat, prompt names the skill and config paths | hook |
   | Rows from your history appear under Logs | Braintrust |
5. **Failure paths** (optional, 5 min): paste a fake key into chat (it must
   refuse to use it), and give a wrong project ID once (`check` must fail with
   `PROJECT_NOT_FOUND`).

## 3. Known task

1. Pick a marker, e.g. `canary-` plus today's date and three random letters.
   In Muse:
   > Run exactly these three shell commands, one at a time: `echo canary-1021-qzx A`, `echo canary-1021-qzx B`, `ls /canary-1021-qzx-missing`. Then reply "done".
2. Then:
   > Run the Braintrust telemetry export now (On wake).
3. Locally:
   ```
   ! bin/bt-local find --marker canary-1021-qzx
   ```
   **Pass:** three tool rows (the two `echo` outputs and the failed `ls`). In
   Braintrust, open their trace:
   - one trace for the task;
   - three tool spans in order, with the `ls` span marked as an error;
   - the start times match when you ran them (a whole-hour offset means a
     timezone bug);
   - Muse's reply appears as an `llm` span with token counts.
4. Ask Muse:
   > Show the token usage row muse.db has for your "done" message.

   Its counts must equal the `llm` span's `prompt_tokens` and
   `completion_tokens`.

## 4. Reconcile

1. In Muse, for the history sent so far (use 6-hour ranges if it reports
   `RANGE_TOO_LARGE`):
   > Run braintrust telemetry reconcile with --since 2026-10-14T00:00:00Z --until 2026-10-21T00:00:00Z and show me the result.

   **Pass:** `missing`, `extra`, `duplicates` and `mismatched` are all 0, and
   the `expected` token totals equal `stored`.
2. **Independent count.** Reconcile uses the saved queries, so it cannot catch
   a query that skips a whole category of calls. Ask:
   > Using muse.db directly (not the saved queries), count all tool calls in agent.context_items and all rows in agent.agent_message_token_usage between those times.

   **Pass:** these counts match reconcile's `expected.tool` and
   `expected.llm`, less the telemetry hook's own runs if the queries leave
   them out. If they differ, send me both numbers.

## 5. Break it

Run one per day after onboarding. After each, run a reconcile covering the
affected time.

| Do | Expect |
| --- | --- |
| **Revoke the key** in Braintrust. Wait for the next hourly export. | Exactly one message about the key. Later wakes stay silent (`status` → `failure.count` grows, wakes slow to at most daily). Reconnect the card as the message says: the next export catches up and reconcile is clean. |
| **Interrupt a run.** Ask "Run the export now", then press stop while it is paging. | `status` shows `runInProgress`. About 15 minutes after the last page, the hook resumes it and `runInProgress` returns to `null`. |
| **Lose local state.** Ask Muse to delete the directory `status` reports as `stateDir`, then run the export. | It re-sends the past 7 days. Reconcile: `duplicates: 0`. |
| **Ask for older history.** "Send my Braintrust telemetry from the past 30 days." | Muse estimates the pages, runs `backfill --days 30` and pages through it; `status` → `historyFrom` moves back 30 days; reconcile over days 8–30 is clean, and the hourly export keeps running. |
| **Ship an update.** Tell me "release 0.0.3". | At the next wake the hook reports `installedVersion` 0.0.2 → `latestVersion` 0.0.3. Muse updates itself, `version` says `upToDate: true`, and exports continue. |

## 6. Soak (7 days)

Once a day, ask Muse:
> Show braintrust telemetry `status`.

| Signal | Pass |
| --- | --- |
| `lagMinutes` | under 75 (the hourly interval plus margin) |
| `failure` | `null`, except during a Phase 5 test |
| `stats.runs` | grows about 24 a day |
| `stats.rowsSent` per day | roughly flat; steady growth means the exporter is exporting its own runs |
| Muse usage | token cost per export is acceptable to you |

At the end, run one reconcile over the whole week. **Done when** it is clean,
every Phase 5 test recovered, and the soak signals held all week.

## What to send me

- Phase 1: the `probe` output.
- Phase 2: the saved queries, the preview rows, and the four outputs from
  step 4.
- Phase 3: the `find` output and anything off in the trace.
- Phase 4 onward: any non-zero reconcile count, any surprise message from
  Muse, any `status` signal outside its pass range.

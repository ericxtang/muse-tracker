# Set up the Muse tracker

This guide installs or updates the `muse-tracker` skill to the latest
release, currently **{{VERSION}}**. It contains no account information or credentials. Always install
from a manifest you have just fetched; an installed skill, a saved ZIP, an
earlier download or a remembered hash does not establish the latest version.

GitHub access is for this installation/update task only. Routine exports use
the installed skill and never check GitHub. If Muse offers **Allow for this
site**, the person may save a matching permission for this repository's release
downloads (which can redirect to `release-assets.githubusercontent.com`). If it
only offers **Allow for this task**, finish under that approval; do not try to
create a permanent grant by repeatedly requesting access. If approval is denied
or expires, stop this attempt. Do not start background retries.

1. **Installed already?** If `~/workspace/skills/muse-tracker/scripts/muse_tracker.py`
   exists, run from that folder:

   ```sh
   python3 scripts/muse_tracker.py self-update --config ~/workspace/muse-tracker/config.json
   ```

   (Omit `--config` if no config exists yet.) It fetches the latest manifest,
   downloads that release's archive, checks the archive and every file against
   the manifest, and replaces only the four bundled files. Config, state, the
   Secure Vault card and the hook are kept. If the command is missing (an older
   install) or fails with anything except `NETWORK_ERROR`, do step 2 over the
   existing folder; do not delete it.

2. **First install.** Fetch {{MANIFEST_URL}}
   without a cached copy (it redirects to the newest release). Download the archive at its `archiveUrl` (the
   address contains the release hash) and check that its SHA-256 equals the
   manifest's `sha256`. Inspect its files, then extract them into
   `~/workspace/skills/muse-tracker` and run
   `chmod +x assets/export-hook.sh scripts/muse_tracker.py`. Then run
   step 1's `self-update` once; it confirms nothing newer was published
   meanwhile.

3. **Verify.** Re-read the installed `SKILL.md` and run
   `python3 scripts/muse_tracker.py version` (offline) to see the installed
   version. Run `setup-check --config ~/workspace/muse-tracker/config.json`
   (omit `--config` if it does not exist yet), which contacts GitHub, must report
   `ok: true`, and records the verified version when a config exists. On
   `SKILL_UPDATE_REQUIRED`, repeat step 1 once. If it still fails or the
   release is unreachable, explain that setup is blocked and stop; do not run
   unverified files.

4. **Moving from `braintrust-telemetry`?** This skill was called
   `braintrust-telemetry` before 0.0.6. If `~/workspace/skills/braintrust-telemetry`
   exists, do these instead of step 5's first-installation Setup:
   1. Record whether the `braintrust-telemetry` native hook is enabled, then
      disable it so no export runs during the move.
   2. Run `python3 scripts/muse_tracker.py migrate --from ~/workspace/braintrust-telemetry/config.json --config ~/workspace/muse-tracker/config.json`.
      It copies the config and the export state (watermark, run in progress,
      sent rows), so the export continues where it stopped; the Secure Vault
      card is reused. Rows already sent are not duplicated.
   3. Install this skill's hook (Setup step 6 of the new `SKILL.md`), dry-run,
      restore the original hook's enabled/disabled state and read it back. A
      paused hook must stay disabled. Then remove the `braintrust-telemetry` hook and
      the `~/workspace/skills/braintrust-telemetry` folder. Keep
      `~/workspace/braintrust-telemetry/` until an export under the new hook
      succeeds.
   4. Skip Setup steps 1–5. `migrate` reports `setupRequired: true`: make the
      Setup changes in the new `SKILL.md`. If the hook was paused, defer changes
      that export or backfill until the person resumes exports. Run `setup-done`
      only after all required changes are complete.

5. **Configure.** For a first installation, follow the skill's Setup. Use its
   defaults (the past 7 days of history; the project an existing Braintrust
   connector names) instead of asking; the person can ask for more later. For an
   update, keep the config, export progress and hook's enabled/disabled state.
   Never re-enable a paused hook just to update it. If `self-update` reports
   `setupRequired: true`, make the changes in the new `SKILL.md`'s Setup
   changes section, deferring export/backfill work while paused. Run
   `setup-done` only after all required changes are complete; deferred changes
   must keep `setupRequired: true`.

After setup, updates happen only when the person asks. Never run `self-update`,
`setup-check` or `version --check-updates` as part of an export wake or a status
check. A previous approval for one task is not a standing GitHub grant.

Never ask for the API key in chat. Keep update details out of the
conversation unless setup is blocked.

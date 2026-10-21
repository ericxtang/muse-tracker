# Set up the Muse tracker

This guide installs or updates the `muse-tracker` skill to the latest
release, currently **{{VERSION}}**. It contains no account information or credentials. Always install
from a manifest you have just fetched; an installed skill, a saved ZIP, an
earlier download or a remembered hash does not establish the latest version.

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

2. **First install.** Fetch {{BASE_URL}}/skills/muse-tracker/manifest.json
   without a cached copy. Download the archive at its `archiveUrl` (the
   address contains the release hash) and check that its SHA-256 equals the
   manifest's `sha256`. Inspect its files, then extract them into
   `~/workspace/skills/muse-tracker` and run
   `chmod +x assets/export-hook.sh scripts/muse_tracker.py`. Then run
   step 1's `self-update` once; it confirms nothing newer was published
   meanwhile.

3. **Verify.** Re-read the installed `SKILL.md` and run
   `python3 scripts/muse_tracker.py version`. It must report
   `upToDate: true`, with `installed` equal to `latest`. Then run `setup-check`,
   which must report `ok: true` and records the verified version in the config. On
   `SKILL_UPDATE_REQUIRED`, repeat step 1 once. If it still fails or the
   release is unreachable, explain that setup is blocked and stop; do not run
   unverified files.

4. **Moving from `braintrust-telemetry`?** This skill was called
   `braintrust-telemetry` before 0.0.6. If `~/workspace/skills/braintrust-telemetry`
   exists, do these instead of step 5's first-installation Setup:
   1. Disable the `braintrust-telemetry` native hook, so no export runs during
      the move.
   2. Run `python3 scripts/muse_tracker.py migrate --from ~/workspace/braintrust-telemetry/config.json --config ~/workspace/muse-tracker/config.json`.
      It copies the config and the export state (watermark, run in progress,
      sent rows), so the export continues where it stopped; the Secure Vault
      card is reused. Rows already sent are not duplicated.
   3. Install this skill's hook (Setup step 6 of the new `SKILL.md`), dry-run,
      enable and read it back. Then remove the `braintrust-telemetry` hook and
      the `~/workspace/skills/braintrust-telemetry` folder. Keep
      `~/workspace/braintrust-telemetry/` until an export under the new hook
      succeeds.
   4. Skip Setup steps 1–5. `migrate` reports `setupRequired: true`: make the
      Setup changes in the new `SKILL.md`, then run `setup-done`.

5. **Configure.** For a first installation, follow the skill's Setup. Use its
   defaults (the past 7 days of history; the project an existing Braintrust
   connector names) instead of asking; the person can ask for more later. For an
   update, keep the config and hook. If `self-update` reports
   `setupRequired: true`, make the changes in the new `SKILL.md`'s Setup
   changes section, then run `setup-done`.

Never ask for the API key in chat. Keep update details out of the
conversation unless setup is blocked.

# Set up Braintrust telemetry

This guide installs or updates the `braintrust-telemetry` skill to the latest
release, currently **{{VERSION}}**. It contains no account information or credentials. Always install
from a manifest you have just fetched; an installed skill, a saved ZIP, an
earlier download or a remembered hash does not establish the latest version.

1. **Installed already?** If `~/workspace/skills/braintrust-telemetry/scripts/braintrust_telemetry.py`
   exists, run from that folder:

   ```sh
   python3 scripts/braintrust_telemetry.py self-update --config ~/workspace/braintrust-telemetry/config.json
   ```

   (Omit `--config` if no config exists yet.) It fetches the latest manifest,
   downloads that release's archive, checks the archive and every file against
   the manifest, and replaces only the four bundled files. Config, state, the
   Secure Vault card and the hook are kept. If the command is missing (an older
   install) or fails with anything except `NETWORK_ERROR`, do step 2 over the
   existing folder; do not delete it.

2. **First install.** Fetch {{BASE_URL}}/skills/braintrust-telemetry/manifest.json
   without a cached copy. Download the archive at its `archiveUrl` (the
   address contains the release hash) and check that its SHA-256 equals the
   manifest's `sha256`. Inspect its files, then extract them into
   `~/workspace/skills/braintrust-telemetry` and run
   `chmod +x assets/export-hook.sh scripts/braintrust_telemetry.py`. Then run
   step 1's `self-update` once; it confirms nothing newer was published
   meanwhile.

3. **Verify.** Re-read the installed `SKILL.md` and run
   `python3 scripts/braintrust_telemetry.py version`. It must report
   `upToDate: true`, with `installed` equal to `latest`. Then run `setup-check`,
   which must report `ok: true` and records the verified version in the config. On
   `SKILL_UPDATE_REQUIRED`, repeat step 1 once. If it still fails or the
   release is unreachable, explain that setup is blocked and stop; do not run
   unverified files.

4. **Configure.** For a first installation, follow the skill's Setup. Use its
   defaults (the past 7 days of history; the project an existing Braintrust
   connector names) instead of asking; the person can ask for more later. For an
   update, keep the config and hook. If `self-update` reports
   `setupRequired: true`, make the changes in the new `SKILL.md`'s Setup
   changes section, then run `setup-done`.

Never ask for the API key in chat. Keep update details out of the
conversation unless setup is blocked.

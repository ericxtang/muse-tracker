#!/usr/bin/env python3
"""Build a release of the skill and publish it as a GitHub release.

    python3 build.py --release      # record the current sources as VERSION
    python3 build.py --publish      # build and create GitHub release vVERSION
    python3 build.py --base-url URL [--out dist]   # build only (tests, previews)

VERSION holds the release version. releases.json maps each released version to
a digest of its sources, and the build refuses to publish sources that differ
from what their version recorded: change anything, bump VERSION, run --release.

A release's assets, as GitHub serves them under BASE_URL
(https://github.com/OWNER/REPO/releases):
    download/vVERSION/manifest.json, setup.md, SKILL.md
    download/vVERSION/muse-tracker-VERSION-SHA256.zip   (manifest.archiveUrl)
    latest/download/<asset>   GitHub's redirect to the newest release's asset
Installs read latest/download/manifest.json; the archive address contains
its version and hash, so a cached archive can never pass for a newer one.
"""
import argparse
from datetime import date
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parent
SKILL = "muse-tracker"
REPO = "ericxtang/muse-tracker"
DEFAULT_BASE_URL = f"https://github.com/{REPO}/releases"
FILES = ("SKILL.md", "assets/export-hook.sh", "references/records.md", "scripts/muse_tracker.py")
EXECUTABLE = {"assets/export-hook.sh", "scripts/muse_tracker.py"}
# Bump when an update requires installed agents to repeat setup.
SETUP_VERSION = 4


SEMVER = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")
SETUP_GUIDE = ROOT / "docs" / "agent-setup.md"
RELEASES = ROOT / "releases.json"


def render(text, base_url, version):
    return (text.replace("{{MANIFEST_URL}}", manifest_url(base_url)).replace("{{SETUP_URL}}", setup_url(base_url))
            .replace("{{BASE_URL}}", base_url).replace("{{VERSION}}", version)
            .replace("{{SETUP_VERSION}}", str(SETUP_VERSION)))


def manifest_url(base_url):
    return f"{base_url.rstrip('/')}/latest/download/manifest.json"


def setup_url(base_url):
    return f"{base_url.rstrip('/')}/latest/download/setup.md"


def current_version():
    version = (ROOT / "VERSION").read_text().strip()
    if not SEMVER.fullmatch(version):
        raise SystemExit(f"VERSION must be MAJOR.MINOR.PATCH, not {version!r}")
    return version


def source_digest(source=None):
    """Digest of everything a release is built from, before rendering."""
    source = Path(source) if source else ROOT / "skills" / SKILL
    digest = hashlib.sha256()
    for name, path in [(f, source / f) for f in FILES] + [("setup.md", SETUP_GUIDE)]:
        digest.update(name.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def releases():
    return json.loads(RELEASES.read_text()) if RELEASES.exists() else {}


def check_release(version, digest):
    recorded = releases().get(version)
    if recorded is None:
        raise SystemExit(f"Version {version} is not recorded. Run: python3 build.py --release")
    if recorded["digest"] != digest:
        raise SystemExit(f"The sources changed since {version} was recorded. Bump VERSION, then run: python3 build.py --release")


def record_release():
    version, digest = current_version(), source_digest()
    history = releases()
    if version in history:
        if history[version]["digest"] == digest:
            return {"version": version, "recorded": False, "digest": digest}
        raise SystemExit(f"{version} was already released with other sources; bump VERSION.")
    newest = max(history, key=lambda v: tuple(map(int, v.split("."))), default=None)
    if newest and tuple(map(int, version.split("."))) <= tuple(map(int, newest.split("."))):
        raise SystemExit(f"VERSION {version} must be newer than the last release, {newest}.")
    history[version] = {"digest": digest, "date": date.today().isoformat()}
    RELEASES.write_text(json.dumps(history, indent=2) + "\n")
    return {"version": version, "recorded": True, "digest": digest}


def build(base_url, out, source=None, version=None):
    """Write a release. Without an explicit version (tests), the current sources
    must match what releases.json recorded for VERSION."""
    base_url = base_url.rstrip("/")
    if not (base_url.startswith("https://") or base_url.startswith("http://127.0.0.1")):
        raise SystemExit("--base-url must be https:// (or http://127.0.0.1 for tests)")
    if version is None:
        version = current_version()
        check_release(version, source_digest(source))
    source = Path(source) if source else ROOT / "skills" / SKILL
    rendered = {name: render((source / name).read_text(), base_url, version).encode() for name in FILES}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in FILES:
            # Fixed timestamps and modes make the archive hash reproducible.
            info = zipfile.ZipInfo(f"{SKILL}/{name}", date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o100755 if name in EXECUTABLE else 0o100644) << 16
            archive.writestr(info, rendered[name])
    data = buffer.getvalue()
    sha256 = hashlib.sha256(data).hexdigest()
    manifest = {
        "version": version,
        "setupVersion": SETUP_VERSION,
        "sha256": sha256,
        "files": {name: hashlib.sha256(rendered[name]).hexdigest() for name in FILES},
        "baseUrl": base_url,
        "manifestUrl": manifest_url(base_url),
        "archiveUrl": f"{base_url}/download/v{version}/{archive_name(version, sha256)}",
        "setupUrl": setup_url(base_url),
    }
    out = Path(out)
    folder = out / "download" / f"v{version}"
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    (folder / archive_name(version, sha256)).write_bytes(data)
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (folder / "SKILL.md").write_bytes(rendered["SKILL.md"])
    (folder / "setup.md").write_text(render(SETUP_GUIDE.read_text(), base_url, version))
    (out / "latest.txt").write_text(version + "\n")  # what latest/download resolves to in a local preview
    return manifest


def archive_name(version, sha256):
    return f"{SKILL}-{version}-{sha256}.zip"


def release_notes(version):
    """The CHANGELOG section for a version."""
    text = (ROOT / "CHANGELOG.md").read_text()
    match = re.search(rf"^## {re.escape(version)}\b.*?$(.*?)(?=^## |\Z)", text, re.M | re.S)
    return match.group(1).strip() if match else f"muse-tracker {version}"


def publish():
    """Create GitHub release vVERSION with the built assets and mark it latest.
    A published version is never replaced: bump VERSION instead."""
    version = current_version()
    tag = f"v{version}"
    if subprocess.run(["gh", "release", "view", tag, "--repo", REPO], capture_output=True).returncode == 0:
        raise SystemExit(f"Release {tag} already exists. Bump VERSION, then run: python3 build.py --release")
    out = Path(tempfile.mkdtemp())
    manifest = build(DEFAULT_BASE_URL, out)
    folder = out / "download" / tag
    notes = out / "notes.md"
    notes.write_text(release_notes(version) + "\n")
    subprocess.run(["gh", "release", "create", tag, *sorted(str(p) for p in folder.iterdir()), "--repo", REPO,
                    "--title", f"muse-tracker {version}", "--notes-file", str(notes), "--latest"], check=True)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url")
    parser.add_argument("--out", default=str(ROOT / "dist"))
    parser.add_argument("--release", action="store_true", help="Record the current sources as VERSION in releases.json")
    parser.add_argument("--publish", action="store_true", help="Build and create the GitHub release for VERSION")
    args = parser.parse_args()
    if args.release:
        print(json.dumps(record_release(), indent=2))
    elif args.publish:
        print(json.dumps(publish(), indent=2))
    elif args.base_url:
        print(json.dumps(build(args.base_url, args.out), indent=2))
    else:
        parser.error("pass --release, --publish or --base-url")


if __name__ == "__main__":
    main()

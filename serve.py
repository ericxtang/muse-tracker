#!/usr/bin/env python3
"""Build the release for this host's public URL and serve it.

The base URL comes from PUBLIC_BASE_URL, or https://$RAILWAY_PUBLIC_DOMAIN on
Railway. Only the release files and /health are served.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import tempfile

import build

TYPES = {".zip": "application/zip", ".json": "application/json", ".md": "text/markdown; charset=utf-8"}


def base_url():
    if os.environ.get("PUBLIC_BASE_URL"):
        return os.environ["PUBLIC_BASE_URL"]
    if os.environ.get("RAILWAY_PUBLIC_DOMAIN"):
        return "https://" + os.environ["RAILWAY_PUBLIC_DOMAIN"]
    raise SystemExit("Set PUBLIC_BASE_URL or give the service a Railway domain.")


def routes(out):
    files = sorted((out / "skills").glob("*.zip")) + sorted((out / "skills" / build.SKILL).iterdir())
    return {"/" + str(f.relative_to(out)): (f.read_bytes(), TYPES[f.suffix]) for f in files}


def main():
    out = Path(tempfile.mkdtemp())
    manifest = build.build(base_url(), out)
    table = routes(out)
    table["/health"] = (b"ok\n", "text/plain")
    immutable_path = "/" + manifest["archiveUrl"].split("/", 3)[3]
    setup = f"{manifest['setupUrl']}\n".encode()

    class Handler(BaseHTTPRequestHandler):
        def send(self, status, body, kind, head=False, immutable=False):
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            # Everything but the content-addressed archive must be revalidated,
            # so the manifest and the "latest" names are never stale.
            self.send_header("Cache-Control", "public, max-age=31536000, immutable" if immutable else "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if not head:
                self.wfile.write(body)

        def respond(self, head):
            path = self.path.split("?", 1)[0]
            if path in table:
                self.send(200, *table[path], head=head, immutable=path == immutable_path)
            elif path == "/":
                self.send(200, f"Muse tracker skill, version {manifest['version']}.\nSetup guide: ".encode() + setup, "text/plain", head=head)
            else:
                self.send(404, b"not found\n", "text/plain", head=head)

        def do_GET(self):
            self.respond(False)

        def do_HEAD(self):
            self.respond(True)

    port = int(os.environ.get("PORT", "8080"))
    print(f"serving version {manifest['version']} {manifest['archiveUrl']} on :{port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()

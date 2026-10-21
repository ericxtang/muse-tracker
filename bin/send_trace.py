#!/usr/bin/env python3
"""Send an OTLP/HTTP (JSON) trace payload to Braintrust.

Usage: python3 send_trace.py /path/to/otlp.json

Auth comes from the Secure Vault via the custom.braintrust connection;
this script never sees the API key. Routing header x-bt-parent targets
the user's Braintrust project.
"""
import json
import sys
import urllib.request

sys.path.insert(0, "/opt/muse/skills/custom-connector-setup/bin")
from dynamic_credentials import add_surrogate_to_request  # noqa: E402

OTLP_URL = "https://api.braintrust.dev/otel/v1/traces"
PROJECT_ID = "22e9848a-b189-4236-a72d-b4474a771d6b"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        raise RuntimeError("redirect refused, verify the endpoint")


def main() -> int:
    payload_path = sys.argv[1]
    with open(payload_path, "rb") as f:
        body = f.read()

    request = urllib.request.Request(
        OTLP_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "x-bt-parent": f"project_id:{PROJECT_ID}",
        },
    )
    add_surrogate_to_request(
        request, "custom.braintrust", allowed_hosts=["api.braintrust.dev"]
    )
    try:
        with urllib.request.build_opener(NoRedirect()).open(
            request, timeout=30
        ) as response:
            raw = response.read()
            print(json.dumps({
                "ok": True,
                "status": response.status,
                "response_bytes": len(raw),
                "response_preview": raw[:300].decode("utf-8", "replace"),
            }))
            return 0
    except urllib.error.HTTPError as e:
        detail = e.read()[:500].decode("utf-8", "replace")
        print(json.dumps({"ok": False, "status": e.code, "detail": detail}))
        return 1


if __name__ == "__main__":
    sys.exit(main())

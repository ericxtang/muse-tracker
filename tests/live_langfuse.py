"""Run e2e.py's Langfuse phases against a real Langfuse server, through a
stand-in for Muse's Secure Vault.

On Muse the CLI never holds the key: the vault helper puts a surrogate in the
request, and Muse's egress proxy (Sentinel) intercepts TLS and swaps in the
real credential, only for the hosts the card allows. VaultProxy does the same
locally: it terminates TLS for a made-up https host with a throwaway CA, swaps
the surrogate for the real secret, and forwards to the real server. The CLI
runs unchanged with provider "muse". The proxy can also answer 500 after the
server stored a request, to test an uncertain send for real.
"""
from datetime import datetime, timedelta, timezone
import base64
import http.client
import json
from pathlib import Path
import socket
import ssl
import subprocess
import threading
import time
import urllib.parse
import urllib.request

HOST = "langfuse.vault.test"
SURROGATE = "hsurr-e2e-surrogate"

HELPER = '''
"""Stand-in for Muse's dynamic_credentials helper: attaches a surrogate, never the key."""
SURROGATE = %r

def add_surrogate_to_request(request, connector, allowed_hosts):
    if request.host.split(":")[0] not in allowed_hosts:
        raise PermissionError("host not allowed for " + connector)
    request.add_header("Authorization", "Bearer " + SURROGATE)
''' % SURROGATE


def make_ca(folder):
    """A throwaway CA and a certificate for HOST signed by it."""
    folder.mkdir(parents=True, exist_ok=True)
    run = lambda *a: subprocess.run(a, cwd=folder, check=True, capture_output=True)
    run("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=e2e vault CA",
        "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign",
        "-keyout", "ca.key", "-out", "ca.pem")
    run("openssl", "req", "-newkey", "rsa:2048", "-nodes", "-subj", f"/CN={HOST}", "-keyout", "host.key", "-out", "host.csr")
    (folder / "ext.cnf").write_text(f"subjectAltName=DNS:{HOST}\nkeyUsage=critical,digitalSignature,keyEncipherment\n"
                                    "extendedKeyUsage=serverAuth\nauthorityKeyIdentifier=keyid\n")
    run("openssl", "x509", "-req", "-in", "host.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial",
        "-days", "1", "-extfile", "ext.cnf", "-out", "host.pem")
    return folder / "ca.pem", folder / "host.pem", folder / "host.key"


class VaultProxy:
    def __init__(self, folder, upstream, secret):
        self.ca, cert, key = make_ca(folder)
        self.upstream = urllib.parse.urlsplit(upstream)
        self.secret, self.fail_after_store, self.swaps, self.refused = secret, 0, 0, 0
        self.tls = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        self.tls.load_cert_chain(cert, key)
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(32)
        self.url = f"http://127.0.0.1:{self.sock.getsockname()[1]}"
        self.helper = folder / "dynamic_credentials.py"
        self.helper.write_text(HELPER)
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self.handle, args=(conn,), daemon=True).start()

    def handle(self, conn):
        try:
            head = self.read_head(conn)
            line = head.split(b"\r\n", 1)[0].decode()
            method, target, _ = line.split(" ", 2)
            if method != "CONNECT" or target.split(":")[0] != HOST:
                self.refused += 1  # only the card's host is reachable, as with Sentinel
                conn.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                return
            conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            tls = self.tls.wrap_socket(conn, server_side=True)
            self.forward(tls)
        except Exception:
            pass
        finally:
            conn.close()

    def read_head(self, conn):
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(65536)
            if not chunk:
                break
            data += chunk
        return data

    def forward(self, tls):
        data = self.read_head(tls)
        head, _, rest = data.partition(b"\r\n\r\n")
        lines = head.decode().split("\r\n")
        method, path, _ = lines[0].split(" ", 2)
        headers = dict(l.split(": ", 1) for l in lines[1:] if ": " in l)
        length = int(headers.get("Content-Length", 0))
        while len(rest) < length:
            rest += tls.recv(65536)
        auth = headers.get("Authorization", "")
        if SURROGATE in auth:
            headers["Authorization"] = auth.replace(SURROGATE, self.secret)
            self.swaps += 1
        headers["Host"] = self.upstream.netloc
        headers.pop("Proxy-Connection", None)
        upstream = http.client.HTTPConnection(self.upstream.hostname, self.upstream.port, timeout=60)
        upstream.request(method, path, body=rest or None, headers=headers)
        response = upstream.getresponse()
        body = response.read()
        status, reason = response.status, response.reason
        if self.fail_after_store and method == "POST" and path.startswith("/api/public/otel/") and status == 200:
            self.fail_after_store -= 1
            status, reason, body = 500, "Internal Server Error", b'{"error":"crashed after storing"}'
        out = f"HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n" \
              "Connection: close\r\n\r\n"
        tls.sendall(out.encode() + body)

    def close(self):
        self.sock.close()


class RealLangfuse:
    """Reads back what a real Langfuse stored, in the mock's shape."""

    def __init__(self, url, public, secret, proxy, agent_tag="muse-tracker"):
        self.url, self.proxy, self.tag = url, proxy, agent_tag
        self.auth = "Basic " + base64.b64encode(f"{public}:{secret}".encode()).decode()

    @property
    def fail_after_store(self):
        return self.proxy.fail_after_store

    @fail_after_store.setter
    def fail_after_store(self, value):
        self.proxy.fail_after_store = value

    def fetch(self):
        rows, cursor = [], None
        lo = (datetime.now(timezone.utc) - timedelta(days=29)).isoformat()
        hi = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        while True:
            query = {"fields": "core,basic,io,metadata,model,usage,trace_context", "limit": 1000,
                     "fromStartTime": lo, "toStartTime": hi,
                     "filter": json.dumps([{"type": "arrayOptions", "column": "tags", "operator": "any of",
                                            "value": [self.tag]}])}
            if cursor:
                query["cursor"] = cursor
            request = urllib.request.Request(f"{self.url}/api/public/v2/observations?{urllib.parse.urlencode(query)}",
                                             headers={"Authorization": self.auth})
            page = json.loads(urllib.request.urlopen(request, timeout=30).read())
            rows += page.get("data") or []
            cursor = (page.get("meta") or {}).get("cursor")
            if not cursor:
                return rows

    @property
    def spans(self):
        # Let ingestion settle: read until two reads a second apart agree.
        previous, rows = None, []
        for _ in range(30):
            rows = self.fetch()
            if previous is not None and len(rows) == previous:
                break
            previous = len(rows)
            time.sleep(1)
        return [self.shape(r) for r in rows]

    def shape(self, r):
        meta = r.get("metadata") or {}
        def find(key):
            for k in (key, f"attributes.langfuse.observation.metadata.{key}", f"attributes.langfuse.trace.metadata.{key}"):
                if meta.get(k) not in (None, ""):
                    return meta[k]
            return None
        attrs = {"langfuse.observation.type": (r.get("type") or "").lower(),
                 "langfuse.observation.input": r.get("input"), "langfuse.observation.output": r.get("output"),
                 "langfuse.observation.level": r.get("level"), "langfuse.observation.status_message": r.get("statusMessage"),
                 "langfuse.observation.model.name": r.get("model"),
                 "langfuse.observation.usage_details": json.dumps(r.get("usageDetails") or {}),
                 "langfuse.session.id": r.get("sessionId") or None, "langfuse.trace.tags": r.get("tags") or [],
                 "langfuse.trace.name": r.get("traceName"),
                 "langfuse.trace.metadata.task_id": find("task_id"),
                 "langfuse.trace.metadata.parent_task_id": find("parent_task_id")}
        to_ns = lambda t: int(datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp() * 1e9) if t else 0
        return {"traceId": r["traceId"], "spanId": r["id"], "parentSpanId": r.get("parentObservationId"),
                "name": r.get("name"), "start": to_ns(r.get("startTime")), "end": to_ns(r.get("endTime")),
                "status": {"code": 2 if r.get("level") == "ERROR" else 0}, "attrs": attrs, "raw": r}

    def of_type(self, kind):
        return [s for s in self.spans if s["attrs"]["langfuse.observation.type"] == kind]

    def duplicates(self):
        ids = [s["spanId"] for s in self.spans]
        return sorted({i for i in ids if ids.count(i) > 1})

    @property
    def posts(self):
        return []

    def close(self):
        self.proxy.close()

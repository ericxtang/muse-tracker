#!/usr/bin/env python3
"""Ship a Muse agent's muse.db execution records to Braintrust as traces.

Standard library only. Every command prints one JSON object; failures print
{"code", "message", ...} and exit nonzero.
"""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

BASE_URL = "{{BASE_URL}}"
SKILL = "braintrust-telemetry"
VERSION = "{{VERSION}}"  # stamped by build.py from the repo's VERSION file
API_URL = "https://api.braintrust.dev"
API_HOST = "api.braintrust.dev"
# Tests point provider env at a local server; the vault path always uses API_URL.
API_OVERRIDE_OK = False
BUNDLE_FILES = ("SKILL.md", "assets/export-hook.sh", "references/records.md", "scripts/braintrust_telemetry.py")
EXECUTABLE_FILES = ("assets/export-hook.sh", "scripts/braintrust_telemetry.py")
MAX_ARCHIVE_BYTES = 5_000_000
# The first path is the helper verified with custom.braintrust on 2026-10-20.
HELPERS = ("/opt/muse/skills/custom-connector-setup/bin/dynamic_credentials.py",
           "/opt/hatch/skills/skill-creator/bin/dynamic_credentials.py")
QUERIES = ("tasks", "tool_calls", "model_calls")
REQUIRED_QUERIES = ("tool_calls", "model_calls")
PARAM = re.compile(r":(since_iso|until_iso|since_ms|until_ms|limit)\b")
DEFAULTS = {"intervalMinutes": 60, "overlapMinutes": 30, "settleMinutes": 0, "maxRows": 500,
            "maxAttributeChars": 8000, "maxPayloadBytes": 2_000_000, "maxPageBytes": 100_000,
            "backfillDays": 7}
# Tool mode passes every page through the agent's context, so its row limit
# adapts to keep a page's rows near maxPageBytes (~25k tokens by default).
TOOL_START_ROWS = 50
TOOL_MIN_ROWS = 5
# A run untouched this long is treated as abandoned and resumed by the next wake.
RUN_LEASE_MS = 15 * 60_000
RENOTIFY_MS = 24 * 3_600_000
LEDGER_LIMIT = 5000
REDACTIONS = (
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{12,}"), r"\1[redacted]"),
    (re.compile(r"\b(sk|pk|rk)-[A-Za-z0-9_-]{16,}"), "[redacted]"),
    (re.compile(r"\b(ghp|gho|ghs|github_pat)_[A-Za-z0-9_]{20,}"), "[redacted]"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), "[redacted]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[redacted]"),
)


class Failure(Exception):
    def __init__(self, code, message, exit_code=2, **extra):
        self.payload = {"ok": False, "code": code, "message": message, **extra}
        self.exit_code = exit_code


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# ---------- files and state ----------

def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, ValueError):
        return {} if default is None else default


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w") as out:
            json.dump(value, out)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_config(path):
    if not path:
        raise Failure("SETUP_ERROR", "Pass --config /absolute/path/config.json.", 6)
    path = Path(path).resolve()
    config = read_json(path, None)
    if not isinstance(config, dict) or not config:
        raise Failure("SETUP_ERROR", "The config file is missing or is not a JSON object.", 6)
    return path, {**DEFAULTS, **config}


def state_dir(config_path):
    key = hashlib.sha256(str(config_path).encode()).hexdigest()[:16]
    root = os.environ.get("BRAINTRUST_TELEMETRY_STATE_DIR", str(Path.home() / "hooks/state"))
    return Path(root) / (SKILL + "-" + key)


class Locked:
    def __init__(self, directory, wait=True):
        self.directory, self.wait = directory, wait

    def __enter__(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        self.handle = (self.directory / "export.lock").open("a")
        try:
            fcntl.flock(self.handle, fcntl.LOCK_EX if self.wait else fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.handle.close()
            raise Failure("BUSY", "An export is running.", 3)
        return self.directory

    def __exit__(self, *exc):
        self.handle.close()


# ---------- time ----------

def to_ms(value):
    """Accept ISO-8601 strings or epoch seconds/ms/us/ns, as muse.db columns vary."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        if v > 1e17:
            return int(v / 1e6)
        if v > 1e14:
            return int(v / 1e3)
        if v > 1e11:
            return int(v)
        return int(v * 1000)
    text = str(value).strip()
    if re.fullmatch(r"-?\d+(\.\d+)?", text):
        return to_ms(float(text))
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="milliseconds")


def now_ms():
    return int(time.time() * 1000)


# ---------- queries ----------

def checked_query(name, sql):
    if not isinstance(sql, str):
        raise Failure("SETUP_ERROR", f"queries.{name} must be a SQL string.", 6)
    sql = sql.strip().rstrip(";").strip()
    if not re.match(r"(?is)^(select|with)\b", sql) or ";" in sql:
        raise Failure("SETUP_ERROR", f"queries.{name} must be one read-only SELECT statement.", 6)
    if name in REQUIRED_QUERIES and ":limit" not in sql:
        raise Failure("SETUP_ERROR", f"queries.{name} must end with LIMIT :limit so oversized windows are detected.", 6)
    return sql


def configured_queries(config):
    queries = config.get("queries")
    if not isinstance(queries, dict) or any(q not in queries for q in REQUIRED_QUERIES):
        raise Failure("SETUP_ERROR", "Config needs queries.tool_calls and queries.model_calls (queries.tasks is optional).", 6)
    unknown = set(queries) - set(QUERIES)
    if unknown:
        raise Failure("SETUP_ERROR", "Unknown query names: " + ", ".join(sorted(unknown)), 6)
    return {name: checked_query(name, queries[name]) for name in QUERIES if name in queries}


def params(window, config):
    return {"since_iso": iso(window["since"]), "until_iso": iso(window["until"]),
            "since_ms": window["since"], "until_ms": window["until"],
            "limit": int(window.get("limit") or config["maxRows"])}


def page_limit(config, state):
    if config.get("databases"):
        return int(config["maxRows"])
    return max(TOOL_MIN_ROWS, min(int(config["maxRows"]), int(state.get("pageLimit") or TOOL_START_ROWS)))


def adapt_page_limit(config, state, limit, page_bytes, more):
    """Shrink the tool-mode limit at once when a page ran over budget; grow it
    while pages stay small and more remain."""
    budget = int(config["maxPageBytes"])
    if page_bytes > budget:
        new = int(limit * budget / page_bytes * 0.8)
    elif more and page_bytes < budget / 3:
        new = limit * 2
    else:
        return
    state["pageLimit"] = max(TOOL_MIN_ROWS, min(int(config["maxRows"]), new))


def render(sql, values):
    """Inline parameters for the muse.db tool, which takes plain SQL text."""
    def literal(match):
        value = values[match.group(1)]
        return str(value) if isinstance(value, int) else "'" + str(value).replace("'", "''") + "'"
    return PARAM.sub(literal, sql)


def run_direct(config, queries, values):
    databases = config["databases"]
    if not isinstance(databases, dict) or not databases:
        raise Failure("SETUP_ERROR", "databases must map schema names to SQLite file paths.", 6)
    connection = sqlite3.connect("file::memory:", uri=True)
    try:
        for schema, path in databases.items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema) or schema in ("main", "temp"):
                raise Failure("SETUP_ERROR", f"Invalid schema name {schema!r}.", 6)
            if not Path(path).is_file():
                raise Failure("SOURCE_UNAVAILABLE", f"Database file for {schema} is missing.", 5)
            uri = "file:" + urllib.parse.quote(str(Path(path).resolve())) + "?mode=ro"
            connection.execute(f"ATTACH DATABASE ? AS {schema}", (uri,))
        connection.execute("PRAGMA query_only = ON")
        rows = {}
        for name, sql in queries.items():
            cursor = connection.execute(sql, {k: v for k, v in values.items() if ":" + k in sql})
            columns = [c[0] for c in cursor.description]
            rows[name] = [dict(zip(columns, r)) for r in cursor.fetchall()]
        return rows
    except sqlite3.Error as error:
        raise Failure("QUERY_FAILED", "A saved query failed: " + str(error)[:300], 5)
    finally:
        connection.close()


def load_rows(path):
    try:
        data = json.loads(Path(path).read_text())
    except FileNotFoundError:
        return None
    except ValueError:
        raise Failure("INVALID_ROWS", f"{Path(path).name} is not valid JSON.", 2)
    if isinstance(data, dict) and "rows" in data:
        columns = data.get("columns")
        if columns:
            names = [c["name"] if isinstance(c, dict) else c for c in columns]
            data = [dict(zip(names, r)) if isinstance(r, list) else r for r in data["rows"]]
        else:
            data = data["rows"]
    if not isinstance(data, list) or not all(isinstance(r, dict) for r in data):
        raise Failure("INVALID_ROWS", f"{Path(path).name} must be a JSON array of row objects.", 2)
    return data


# ---------- rows → Braintrust log events ----------

def hexid(seed, length):
    return hashlib.sha256(seed.encode()).hexdigest()[:length]


def clean(value, limit):
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    for pattern, replacement in REDACTIONS:
        text = pattern.sub(replacement, text)
    if len(text) > limit:
        text = text[:limit] + f"… [truncated {len(text) - limit} chars]"
    return text


def as_success(value):
    if value is None or value == "":
        return None
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "t", "yes", "ok", "success", "succeeded")
    return bool(value)


def as_int(value):
    try:
        return None if value is None or value == "" else int(value)
    except (TypeError, ValueError):
        return None


def event(trace, span, parent, name, kind, start, end, metadata, input_=None, output=None, metrics=None, error=None):
    """One Braintrust log row. Its id is derived from the trace and span ids, so
    sending it again replaces the same row instead of adding one."""
    row = {"id": f"{trace}-{span}", "span_id": span, "root_span_id": trace,
           "span_parents": [parent] if parent else [],
           "span_attributes": {"name": name, "type": kind}, "created": iso(start),
           "metrics": {"start": start / 1000, "end": end / 1000, **(metrics or {})},
           "metadata": {"source": "muse.db", **{k: v for k, v in metadata.items() if v not in (None, "")}}}
    if input_ is not None:
        row["input"] = input_
    if output is not None:
        row["output"] = output
    if error is not None:
        row["error"] = error
    return row


def start_ms(row):
    return round(row["metrics"]["start"] * 1000)


def root_request(task, seen, limit):
    """A root's input and where it came from: the person's request, else the
    preamble a machine-spawned agent (a subagent) was started with."""
    if task.get("request_text"):
        return clean(task["request_text"], limit), "user"
    if seen.get("request") and seen.get("requestSource", "user") == "user":
        return seen["request"], "user"
    if task.get("context_text"):
        return clean(task["context_text"], limit), "context"
    return seen.get("request", ""), seen.get("requestSource") if seen.get("request") else None


def build_traces(rows, ledger, config):
    """Group rows by task_id. Returns (task_id, events, ledger_entry) per task."""
    limit = int(config["maxAttributeChars"])
    agent = str(config.get("agentId") or "muse")
    tasks = {}
    for kind in QUERIES:
        for row in rows.get(kind) or []:
            task_id = row.get("task_id")
            if task_id is None or task_id == "":
                raise Failure("INVALID_ROWS", f"Every {kind} row needs a task_id.", 2)
            entry = tasks.setdefault(str(task_id), {"task": {}, "tool_calls": [], "model_calls": []})
            if kind == "tasks":
                entry["task"] = row
            else:
                entry[kind].append(row)
    out = []
    for task_id, entry in tasks.items():
        trace = hexid(f"{agent}:{task_id}", 32)
        root = hexid(f"{trace}:root", 16)
        task, events, times = entry["task"], [], []
        for tc in entry["tool_calls"]:
            start = to_ms(tc.get("started_at"))
            end = to_ms(tc.get("ended_at")) or start
            times += [start, end]
            ok = as_success(tc.get("success"))
            name = str(tc.get("tool") or "unknown")
            seed = tc.get("id") if tc.get("id") is not None else tc.get("seq")
            if seed is None:
                seed = f"{start}:{name}:{hashlib.sha256(clean(tc.get('args'), 10**9).encode()).hexdigest()}"
            result = clean(tc.get("result"), limit)
            events.append(event(trace, hexid(f"{trace}:tool:{seed}", 16), root, name, "tool", start, end,
                                {"task_id": task_id, "seq": as_int(tc.get("seq")), "success": ok},
                                input_=clean(tc.get("args"), limit), output=result,
                                error=result or "tool call failed" if ok is False else None))
        for mc in entry["model_calls"]:
            at = to_ms(mc.get("at"))
            end = to_ms(mc.get("ended_at")) or at
            times += [at, end]
            seed = next((mc[k] for k in ("message_id", "id") if mc.get(k) is not None), at)
            tokens = {}
            for column, key in (("input_tokens", "prompt_tokens"), ("output_tokens", "completion_tokens")):
                if as_int(mc.get(column)) is not None:
                    tokens[key] = as_int(mc[column])
            if tokens:
                tokens["tokens"] = sum(tokens.values())
            events.append(event(trace, hexid(f"{trace}:model:{seed}", 16), root, "model_call", "llm", at, end,
                                {"task_id": task_id, "model": mc.get("model"), "message_id": mc.get("message_id"),
                                 "note": clean(mc.get("note"), limit) if mc.get("note") else None},
                                metrics=tokens))
        # A task can span runs. Its root row is rebuilt from the widest known
        # bounds and replaced in place; the ledger keeps what earlier runs saw.
        seen = ledger.get(task_id, {})
        times += [t for t in (to_ms(task.get("start")), to_ms(task.get("end")), seen.get("start"), seen.get("end")) if t]
        if not times:
            continue
        start, end = min(times), max(times)
        request, source = root_request(task, seen, limit)
        metadata = {"task_id": task_id, "agent_id": task.get("agent_id") or seen.get("agent_id"),
                    "surface": task.get("surface") or seen.get("surface"), "request_source": source}
        root_event = event(trace, root, None, "agent_task", "task", start, end, metadata, input_=request or None)
        out.append((task_id, [root_event] + sorted(events, key=lambda e: (start_ms(e), e["span_id"])),
                    {"start": start, "end": end, "request": request[:2000], "requestSource": source,
                     "agent_id": metadata["agent_id"], "surface": metadata["surface"]}))
    return sorted(out, key=lambda t: (start_ms(t[1][0]), t[0]))


def preview(config, events):
    return {"projectId": config.get("projectId"), "events": events}


def chunks(spans, max_bytes):
    """Split spans into requests under max_bytes."""
    batches, current, size = [], [], 0
    for span in spans:
        span_size = len(json.dumps(span, ensure_ascii=False).encode())
        if current and size + span_size > max_bytes:
            batches.append(current)
            current, size = [], 0
        current.append(span)
        size += span_size
    if current:
        batches.append(current)
    return batches


def span_key(row):
    return row["id"]


def span_digest(row):
    return [hexid(json.dumps(row, sort_keys=True, ensure_ascii=False), 16), start_ms(row)]


def prune_sent(sent, horizon_ms):
    """Forget spans that started before the horizon; only recent ones can be re-read."""
    return {key: value for key, value in sent.items() if value[1] >= horizon_ms}


# ---------- transport ----------

def load_helper(config):
    candidates = [config["helperPath"]] if config.get("helperPath") else list(HELPERS)
    for candidate in candidates:
        path = Path(candidate)
        if path.is_file():
            sys.path.insert(0, str(path.parent))
            spec = importlib.util.spec_from_file_location("dynamic_credentials", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    raise Failure("SETUP_ERROR", "Muse dynamic_credentials helper not found; set helperPath in the config.", 6)


def authorize(request, config):
    if request.host != API_HOST and not (config.get("provider") == "env" and API_OVERRIDE_OK):
        raise Failure("INVALID_HOST", "Requests must go to api.braintrust.dev.", 6)
    provider = config.get("provider", "muse")
    if provider == "env":
        key = os.environ.get("BRAINTRUST_API_KEY", "")
        if not key or any(c.isspace() for c in key):
            raise Failure("SETUP_ERROR", "Set BRAINTRUST_API_KEY for provider env.", 6)
        request.add_header("Authorization", "Bearer " + key)
        return
    if provider != "muse":
        raise Failure("SETUP_ERROR", "provider must be muse or env.", 6)
    module = load_helper(config)
    connector = config.get("connector", "custom.braintrust")
    try:
        if hasattr(module, "add_surrogate_to_request"):
            module.add_surrogate_to_request(request, connector, allowed_hosts=[API_HOST])
        else:
            entry = module.dynamic_credential_entry(connector)
            if entry.get("placement") != "bearer_header":
                raise Failure("SETUP_ERROR", "The connector must use bearer_header placement.", 6)
            request.add_header("Authorization", "Bearer " + entry["surrogate"])
    except Failure:
        raise
    except Exception:
        raise Failure("VAULT_UNAVAILABLE", "Could not obtain the Braintrust credential from the Secure Vault.", 6)


def project_id(config):
    value = config.get("projectId")
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9-]{8,64}", value):
        raise Failure("SETUP_ERROR", "Set projectId to the Braintrust project's ID.", 6)
    return value


def api_url():
    return os.environ.get("BRAINTRUST_API_URL", API_URL) if API_OVERRIDE_OK else API_URL


def request_json(config, method, path, body=None, retries=4):
    """Call the Braintrust API with the vault credential. Retries 429 and 5xx."""
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
    opener = urllib.request.build_opener(NoRedirect())
    for n in range(retries + 1):
        request = urllib.request.Request(api_url() + path, data=data, method=method,
                                         headers={"Content-Type": "application/json", "Accept": "application/json"})
        authorize(request, config)
        try:
            with opener.open(request, timeout=60) as response:
                raw = response.read()
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as error:
            detail = error.read()[:300].decode("utf-8", "replace")
            if (error.code == 429 or error.code >= 500) and n < retries:
                try:
                    wait = min(float(error.headers.get("Retry-After") or 0), 60)
                except ValueError:
                    wait = 0
                time.sleep(wait or 2 ** n)
                continue
            if error.code in (401, 403):
                raise Failure("UNAUTHORIZED", "Braintrust rejected the API key. Reconnect the Braintrust card.", 4, status=error.code)
            if error.code == 404:
                raise Failure("PROJECT_NOT_FOUND", "Braintrust has no project with this projectId for this key.", 6, status=404)
            if 300 <= error.code < 400:
                location = error.headers.get("Location") or ""
                if path == "/btql" and location.startswith("https://"):
                    # Large query results redirect to a stored copy; never send the key there.
                    with urllib.request.build_opener(NoRedirect()).open(location, timeout=60) as response:
                        return json.loads(response.read())
                raise Failure("REDIRECT_REFUSED", "Braintrust redirected the request; nothing was sent.", 5, status=error.code)
            raise Failure("SEND_FAILED", f"Braintrust returned HTTP {error.code}.", 5, status=error.code, detail=detail)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            if n == retries:
                raise Failure("NETWORK_ERROR", "Could not reach Braintrust; the page will be retried.", 5)
            time.sleep(2 ** n)
        except ValueError:
            raise Failure("INVALID_RESPONSE", "Braintrust did not return JSON.", 5)


def send(config, events):
    result = request_json(config, "POST", f"/v1/project_logs/{project_id(config)}/insert", {"events": events})
    ids = result.get("row_ids") if isinstance(result, dict) else None
    if not isinstance(ids, list) or sorted(ids) != sorted(e["id"] for e in events):
        raise Failure("SEND_FAILED", "Braintrust did not confirm every row id; the page will be retried.", 5)
    return len(ids)


def fetch(url, timeout):
    """GET from the release origin, bypassing every cache on the way."""
    if not (BASE_URL.startswith("https://") or BASE_URL.startswith("http://127.0.0.1")):
        raise Failure("SETUP_ERROR", "This copy of the skill was not built with a publish URL.", 6)
    if not url.startswith(BASE_URL.rstrip("/") + "/"):
        raise Failure("INVALID_RESPONSE", "The release points outside its own origin.", 5)
    fresh = url + ("&" if "?" in url else "?") + "fresh=" + uuid.uuid4().hex
    request = urllib.request.Request(fresh, headers={"Cache-Control": "no-cache", "Pragma": "no-cache"})
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout) as response:
            return response.read(MAX_ARCHIVE_BYTES + 1)
    except (urllib.error.URLError, TimeoutError, OSError):
        raise Failure("NETWORK_ERROR", "Could not reach the skill release.", 5)


def fetch_manifest(timeout=15):
    try:
        manifest = json.loads(fetch(BASE_URL.rstrip("/") + f"/skills/{SKILL}/manifest.json", timeout))
    except ValueError:
        raise Failure("INVALID_RESPONSE", "The published skill manifest is not JSON.", 5)
    if (not isinstance(manifest, dict) or manifest.get("baseUrl") != BASE_URL
            or set(manifest.get("files") or {}) != set(BUNDLE_FILES)
            or not re.fullmatch(r"[0-9a-f]{64}", str(manifest.get("sha256")))
            or not re.fullmatch(r"\d+\.\d+\.\d+", str(manifest.get("version")))
            or not isinstance(manifest.get("archiveUrl"), str)):
        raise Failure("INVALID_RESPONSE", "The published skill manifest is invalid.", 5)
    return manifest


def skill_root():
    return Path(__file__).resolve().parents[1]


def installed_files():
    files = {}
    for name in BUNDLE_FILES:
        try:
            files[name] = hashlib.sha256((skill_root() / name).read_bytes()).hexdigest()
        except OSError:
            files[name] = None
    return files


def stale_files(manifest):
    files = installed_files()
    return [name for name in BUNDLE_FILES if files[name] != manifest["files"][name]], files


# ---------- commands ----------

def receipt(manifest, files):
    return {"ok": True, "version": manifest["version"], "setupVersion": manifest.get("setupVersion"),
            "sha256": manifest["sha256"], "files": files, "verifiedAt": now_ms()}


def version_info():
    """Is the installed skill the latest release? Compares bytes, not just numbers."""
    manifest = fetch_manifest()
    changed, _ = stale_files(manifest)
    result = {"ok": True, "installed": VERSION, "latest": manifest["version"], "upToDate": not changed,
              "latestSha256": manifest["sha256"]}
    if changed:
        result["changedFiles"] = changed
        result["next"] = "run self-update"
        if manifest["version"] == VERSION:
            result["note"] = "installed files differ from the published files of this version"
    return result


def setup_check():
    """Verify the installed bytes against the latest published manifest."""
    manifest = fetch_manifest()
    changed, files = stale_files(manifest)
    if changed:
        raise Failure("SKILL_UPDATE_REQUIRED", "Run self-update, re-read SKILL.md, then run setup-check again.",
                      2, changedFiles=changed, archiveUrl=manifest["archiveUrl"], setupUrl=manifest.get("setupUrl"))
    return receipt(manifest, files)


def self_update(previous_receipt):
    """Install the latest release in place. Only the four bundled files change;
    config, state and anything else in the folder are kept."""
    for attempt in range(2):
        manifest = fetch_manifest()
        changed, _ = stale_files(manifest)
        if not changed:
            break
        try:
            archive = fetch(manifest["archiveUrl"], 60)
        except Failure:
            if attempt == 0:
                continue  # a deploy may be rolling over; read the manifest again
            raise
        if len(archive) > MAX_ARCHIVE_BYTES or hashlib.sha256(archive).hexdigest() != manifest["sha256"]:
            if attempt == 0:
                continue
            raise Failure("UPDATE_FAILED", "The downloaded archive does not match the manifest; nothing was changed.", 5)
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            expected = {f"{SKILL}/{name}" for name in BUNDLE_FILES}
            if set(bundle.namelist()) != expected:
                raise Failure("UPDATE_FAILED", "The archive's file list is unexpected; nothing was changed.", 5)
            contents = {name: bundle.read(f"{SKILL}/{name}") for name in BUNDLE_FILES}
        bad = [n for n, data in contents.items() if hashlib.sha256(data).hexdigest() != manifest["files"][n]]
        if bad:
            raise Failure("UPDATE_FAILED", "Archive files do not match the manifest; nothing was changed.", 5, files=bad)
        root = skill_root()
        staged = {}
        for name, data in contents.items():  # write everything first, then swap
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix="." + target.name, dir=target.parent)
            with os.fdopen(fd, "wb") as out:
                out.write(data)
            os.chmod(temporary, 0o755 if name in EXECUTABLE_FILES else 0o644)
            staged[name] = temporary
        for name, temporary in staged.items():
            os.replace(temporary, root / name)
        changed_now, files = stale_files(manifest)
        if changed_now:
            raise Failure("UPDATE_FAILED", "Installed files still differ after the update.", 5, files=changed_now)
        old_version = (previous_receipt or {}).get("setupVersion") or 0
        return {**receipt(manifest, files), "updated": True, "fromVersion": VERSION, "changedFiles": changed,
                "from": (previous_receipt or {}).get("sha256"),
                "setupRequired": (manifest.get("setupVersion") or 0) > old_version and old_version > 0,
                "next": "re-read SKILL.md before continuing"}
    else:
        raise Failure("UPDATE_FAILED", "Could not download a release matching the manifest.", 5)
    _, files = stale_files(manifest)
    return {**receipt(manifest, files), "updated": False}


TIME_COLUMN = {"tool_calls": "started_at", "model_calls": "at", "tasks": "start"}


def run_page(config, state, now):
    """The next page of the current run. A run covers everything from the last
    export (minus overlapMinutes, to catch rows written late) up to the moment
    it started; pages split it by row count. The first run reaches back
    backfillDays (or to startAt); `backfill` queues older history later."""
    run = state.get("run")
    if run is None:
        watermark = state.get("watermark")
        if watermark is not None:
            since = watermark - int(config["overlapMinutes"]) * 60_000
        elif config.get("startAt"):
            since = to_ms(config["startAt"])
        else:
            since = now - int(config["backfillDays"]) * 86_400_000
        until = now - int(config["settleMinutes"]) * 60_000
        if until - since < 60_000:
            return None
        run = {"since": since, "until": until, "cursor": since, "pages": 0, "spans": 0, "sentSpans": 0, "startedAt": now}
        state["run"] = run
        state.setdefault("coveredFrom", since)
    run["touchedAt"] = now
    return {"since": run["cursor"], "until": run["until"], "limit": page_limit(config, state)}


def run_queries(queries, run):
    """The queries a run reads: all of them, or the subset backfill --only named."""
    only = run.get("only")
    return {name: sql for name, sql in queries.items() if not only or name in only}


def rows_dir(directory, page):
    return directory / "rows" / f"{page['since']}-{page['until']}"


def window_json(page):
    return {"since": iso(page["since"]), "until": iso(page["until"])}


def plan(config_path, config):
    """Fix the next page of the run and return the exact queries for it."""
    queries = configured_queries(config)
    with Locked(state_dir(config_path)) as directory:
        state = read_json(directory / "export.json")
        page = run_page(config, state, now_ms())
        if page is None:
            return {"ok": True, "due": False, "reason": "nothing new since the last export"}
        queries = run_queries(queries, state["run"])
        atomic_json(directory / "export.json", state)
    result = {"ok": True, "due": True, "window": window_json(page)}
    if config.get("databases"):
        return {**result, "mode": "direct", "next": "run export"}
    folder = rows_dir(directory, page)
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    values = params(page, config)
    return {**result, "mode": "tool", "pageRows": values["limit"],
            "queries": [{"name": name, "sql": render(sql, values), "saveTo": str(folder / f"{name}.json")}
                        for name, sql in queries.items()],
            "next": "run each query with the muse.db tool, save its rows as a JSON array to saveTo, then run export"}


def record_failure(directory, failure, slot="failure"):
    """Set shouldNotify on the first failure of a kind, and again after a day."""
    if failure.payload["code"] in ("NO_PLAN", "REPLAN"):
        return failure
    state = read_json(directory / "export.json")
    previous = state.get(slot) or {}
    now = now_ms()
    code = failure.payload["code"]
    notify = previous.get("code") != code or now - previous.get("notifiedAt", 0) >= RENOTIFY_MS
    stats = state.setdefault("stats", {})
    stats[slot + "s"] = stats.get(slot + "s", 0) + 1
    state[slot] = {"code": code, "message": failure.payload["message"],
                        "firstAt": previous.get("firstAt", now) if previous.get("code") == code else now,
                        "count": previous.get("count", 0) + 1 if previous.get("code") == code else 1,
                        "notifiedAt": now if notify else previous.get("notifiedAt", 0)}
    atomic_json(directory / "export.json", state)
    failure.payload["shouldNotify"] = notify
    failure.payload["failureCount"] = state[slot]["count"]
    return failure


def export(config_path, config, dry_run=False, output=None):
    queries = configured_queries(config)
    with Locked(state_dir(config_path)) as directory:
        try:
            try:
                if not config.get("databases"):
                    return export_page(directory, config, queries, dry_run, output)
                # Direct mode drains the whole run in one command.
                total = None
                while True:
                    result = export_page(directory, config, queries, dry_run, output)
                    if not result.get("due", True):
                        return total or result
                    total = result if total is None else {**result, "pages": result["pages"],
                                                          "traces": total["traces"] + result["traces"]}
                    if not result.get("more"):
                        return total
                    if dry_run:
                        return {**total, "note": "dry run shows the first page only"}
            except (ValueError, KeyError, TypeError) as error:
                raise Failure("INVALID_ROWS", f"Could not read the rows: {type(error).__name__}: {str(error)[:200]}", 2)
        except Failure as failure:
            raise failure if dry_run else record_failure(directory, failure)


def limited(queries):
    """Queries that take :limit; a full result from any of them ends the page."""
    return [name for name, sql in queries.items() if ":limit" in sql]


def cut_page(rows, page, limit, bounded=REQUIRED_QUERIES):
    """Check every row lies in the page, then, if a limited query came back
    full, cut the page at its last timestamp so no row is lost or split."""
    times = {}
    for name, items in rows.items():
        column = TIME_COLUMN[name]
        values = []
        for row in items:
            t = to_ms(row.get(column))
            if t is None or not page["since"] <= t < page["until"]:
                raise Failure("QUERY_MISMATCH", f"{name} returned a row whose {column} is outside the window; "
                              f"filter on {column} with >= since AND < until.", 2, query=name, value=str(row.get(column)))
            values.append(t)
        times[name] = values
    full = [name for name in bounded if name in rows and len(rows[name]) >= limit]
    if not full:
        return rows, page["until"], False
    cut = min(max(times[name]) for name in full)
    if cut <= page["since"]:
        raise Failure("PAGE_STUCK", f"More than maxRows ({limit}) rows share one timestamp; raise maxRows.", 2, queries=full)
    kept = {name: [r for r, t in zip(items, times[name]) if t < cut] for name, items in rows.items()}
    return kept, cut, True


def export_page(directory, config, queries, dry_run, output):
    state = read_json(directory / "export.json")
    now = now_ms()
    direct = bool(config.get("databases"))
    if state.get("run") is None and not direct:
        raise Failure("NO_PLAN", "Run plan first, then run its queries.", 2)
    page = run_page(config, state, now)
    if page is None:
        return {"ok": True, "due": False, "reason": "nothing new since the last export"}
    queries = run_queries(queries, state["run"])
    values = params(page, config)
    if direct:
        rows = run_direct(config, queries, values)
    else:
        folder = rows_dir(directory, page)
        rows = {name: load_rows(folder / f"{name}.json") for name in queries}
        missing = [name for name, value in rows.items() if value is None]
        if missing:
            raise Failure("MISSING_ROWS", "Run every planned query and save its rows, even when empty ([]).", 2, missing=missing)
        page_bytes = sum((folder / f"{name}.json").stat().st_size for name in queries)
    try:
        rows, cut, more = cut_page(rows, page, values["limit"], limited(queries))
    except Failure as failure:
        if direct or failure.payload["code"] != "PAGE_STUCK" or values["limit"] >= int(config["maxRows"]):
            raise
        # Too many rows share one timestamp for this page size: grow it and re-plan.
        state["pageLimit"] = min(int(config["maxRows"]), values["limit"] * 4)
        atomic_json(directory / "export.json", state)
        shutil.rmtree(rows_dir(directory, page), ignore_errors=True)
        raise Failure("REPLAN", "The page needs more rows; run plan again.", 3, nextPageRows=state["pageLimit"])
    ledger = state.get("ledger", {})
    traces = build_traces(rows, ledger, config)
    spans = [span for _, task_spans, _ in traces for span in task_spans]
    sent = state.get("sent", {})
    fresh = [span for span in spans if sent.get(span_key(span)) != span_digest(span)]
    # A span already sent with identical content is skipped; a changed one (a
    # task root whose bounds grew) is sent again under the same id.
    batches = chunks(fresh, int(config["maxPayloadBytes"]))
    if dry_run:
        if output:
            atomic_json(Path(output), preview(config, fresh))
        return {"ok": True, "dryRun": True, "window": window_json({"since": page["since"], "until": cut}), "more": more,
                "rows": {name: len(value) for name, value in rows.items()}, "traces": len(traces),
                "spans": len(spans), "unsent": len(fresh), "requests": len(batches), "pages": 1, "output": output}
    if not direct:
        atomic_json(directory / "export.json", state)
    for batch in batches:
        send(config, batch)
        # Record each request as it lands, so a retry after a partial send skips it.
        for span in batch:
            sent[span_key(span)] = span_digest(span)
        state["sent"] = sent
        atomic_json(directory / "export.json", state)
    for task_id, _, entry in traces:
        ledger[task_id] = {**entry, "seenAt": now}
    if len(ledger) > LEDGER_LIMIT:
        ledger = dict(sorted(ledger.items(), key=lambda kv: kv[1]["seenAt"])[-LEDGER_LIMIT:])
    run = state["run"]
    run.update(cursor=cut, pages=run["pages"] + 1, spans=run["spans"] + len(spans), sentSpans=run["sentSpans"] + len(fresh))
    summary = {"at": now, "since": iso(run["since"]), "until": iso(run["until"]), "pages": run["pages"],
               "spans": run["spans"], "sentSpans": run["sentSpans"]}
    # Keep the registry small during a long first run: rows before the cursor
    # are not read again in this run, and a re-sent task root only replaces itself.
    state.update(ledger=ledger, failure=None,
                 sent=prune_sent(sent, cut - 2 * int(config["overlapMinutes"]) * 60_000 - 86_400_000))
    stats = state.setdefault("stats", {})
    stats.update(pages=stats.get("pages", 0) + 1, rowsSent=stats.get("rowsSent", 0) + len(fresh))
    if not direct:
        adapt_page_limit(config, state, values["limit"], page_bytes, more)
    if not more and run.get("backfill"):
        # Older history is in; the hourly watermark is untouched.
        state.update(coveredFrom=min(run["since"], state.get("coveredFrom", run["since"])), run=None,
                     lastBackfill=summary)
    elif not more:
        stats["runs"] = stats.get("runs", 0) + 1
        state.update(watermark=run["until"], run=None, lastExport=summary)
    atomic_json(directory / "export.json", state)
    if not direct:
        shutil.rmtree(rows_dir(directory, page), ignore_errors=True)
    result = {"ok": True, **summary, "traces": len(traces), "pageSpans": len(spans), "pageSent": len(fresh), "more": more,
              **({"backfill": True} if run.get("backfill") else {})}
    if not direct:
        result.update(pageBytes=page_bytes, nextPageRows=page_limit(config, state))
    return result


# ---------- verification: read back what Braintrust stored ----------

BTQL_SPACING = 3.2  # Braintrust allows about 20 BTQL queries a minute


def sql_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def btql(config, sql):
    result = request_json(config, "POST", "/btql", {"query": sql, "fmt": "json", "brainstore_realtime": True})
    spacing = float(os.environ.get("BRAINTRUST_BTQL_SPACING", BTQL_SPACING)) if API_OVERRIDE_OK else BTQL_SPACING
    time.sleep(spacing)
    rows = result.get("data") if isinstance(result, dict) else None
    if not isinstance(rows, list):
        raise Failure("INVALID_RESPONSE", "BTQL did not return a data array.", 5)
    return rows


FIELDS = "id, span_id, root_span_id, span_parents, span_attributes, input, output, error, metrics, metadata, _xact_id"


def rows_for_traces(config, project, traces, limit=1000):
    """Every stored row of the given traces. A full result is split and re-read,
    so nothing is silently cut off."""
    out, pending = [], [list(traces[i:i + 20]) for i in range(0, len(traces), 20)]
    while pending:
        chunk = pending.pop()
        rows = btql(config, f"SELECT {FIELDS} FROM project_logs({sql_literal(project)}) "
                            f"WHERE root_span_id IN ({', '.join(sql_literal(t) for t in chunk)}) LIMIT {limit}")
        if len(rows) >= limit:
            if len(chunk) == 1:
                raise Failure("TRACE_TOO_LARGE", f"Trace {chunk[0]} has more than {limit} rows; check it by hand.", 2)
            pending += [chunk[:len(chunk) // 2], chunk[len(chunk) // 2:]]
            continue
        out += rows
    return out


def differences(expected, actual):
    """Fields this skill wrote that Braintrust stored differently."""
    diffs = [k for k in ("input", "output", "error") if (expected.get(k) or None) != (actual.get(k) or None)]
    stored = actual.get("span_attributes") or {}
    diffs += ["span_attributes." + k for k in ("name", "type") if expected["span_attributes"][k] != stored.get(k)]
    if expected["span_parents"] != (actual.get("span_parents") or []):
        diffs.append("span_parents")
    metrics = actual.get("metrics") or {}
    for key, value in expected["metrics"].items():
        got = metrics.get(key)
        if key in ("start", "end"):
            if not isinstance(got, (int, float)) or abs(got - value) > 0.001:
                diffs.append("metrics." + key)
        elif got != value:
            diffs.append("metrics." + key)
    metadata = actual.get("metadata") or {}
    diffs += ["metadata." + k for k, v in expected["metadata"].items() if metadata.get(k) != v]
    return diffs


def reconcile_dir(config_path, since, until):
    return state_dir(config_path) / "reconcile" / f"{since}-{until}"


def reconcile(config_path, config, since_text, until_text, limit=10_000):
    """Compare what muse.db holds for [since, until) with what Braintrust stored."""
    if not since_text or not until_text:
        raise Failure("INVALID_INPUT", "reconcile needs --since and --until (ISO-8601).", 2)
    queries = configured_queries(config)
    project = project_id(config)
    since, until = to_ms(since_text), to_ms(until_text)
    state = read_json(state_dir(config_path) / "export.json")
    watermark = state.get("watermark")
    if watermark is None:
        raise Failure("NOTHING_EXPORTED", "Nothing has been exported yet.", 2)
    clipped = until > watermark
    until = min(until, watermark)  # only compare what has been exported
    if until <= since:
        raise Failure("INVALID_INPUT", "The range ends before it starts (or after the last export).", 2)
    window = {"since": since, "until": until, "limit": limit}
    values = params(window, config)
    folder = reconcile_dir(config_path, since, until)
    if config.get("databases"):
        rows = run_direct(config, queries, values)
    else:
        rows = {name: load_rows(folder / f"{name}.json") for name in queries}
        if any(v is None for v in rows.values()):
            folder.mkdir(parents=True, exist_ok=True)
            os.chmod(folder, 0o700)
            return {"ok": True, "mode": "tool", "window": window_json(window), "clippedToWatermark": clipped,
                    "queries": [{"name": n, "sql": render(q, values), "saveTo": str(folder / f"{n}.json")}
                                for n, q in queries.items()],
                    "next": "run each query with the muse.db tool, save the rows to saveTo, then run reconcile again"}
    full = [name for name in limited(queries) if len(rows[name]) >= limit]
    if full:
        raise Failure("RANGE_TOO_LARGE", f"More than {limit} rows; reconcile a shorter range.", 2, queries=full)
    cut_page(rows, window, limit + 1)  # every row must lie in the range
    expected = [e for _, events, _ in build_traces(rows, {}, config) for e in events]
    traces = sorted({e["root_span_id"] for e in expected})
    actual = rows_for_traces(config, project, traces) if traces else []
    by_id = {r["id"]: r for r in actual}
    want = {e["id"]: e for e in expected}
    roots = {e["id"] for e in expected if not e["span_parents"]}
    in_range = [r for r in actual if (r.get("span_parents") or [])
                and isinstance((r.get("metrics") or {}).get("start"), (int, float))
                and since <= round(r["metrics"]["start"] * 1000) < until]
    missing = sorted(i for i in want if i not in by_id)
    extra = sorted(r["id"] for r in in_range if r["id"] not in want)
    spans = {}
    for r in actual:
        spans.setdefault((r["root_span_id"], r["span_id"]), set()).add(r["id"])
    duplicates = sorted(f"{t}/{s}" for (t, s), ids in spans.items() if len(ids) > 1)
    mismatched = {}
    for row_id, event in want.items():
        if row_id in by_id and row_id not in roots:
            diffs = differences(event, by_id[row_id])
            if diffs:
                mismatched[row_id] = diffs
    shutil.rmtree(folder, ignore_errors=True)
    sample = lambda items: items[:10]
    return {"ok": not (missing or extra or duplicates or mismatched), "window": window_json(window),
            "clippedToWatermark": clipped, "traces": len(traces),
            "expected": {"rows": len(want), "tool": sum(1 for e in expected if e["span_attributes"]["type"] == "tool"),
                         "llm": sum(1 for e in expected if e["span_attributes"]["type"] == "llm"),
                         "promptTokens": sum(e["metrics"].get("prompt_tokens", 0) for e in expected),
                         "completionTokens": sum(e["metrics"].get("completion_tokens", 0) for e in expected)},
            "stored": {"rows": sum(1 for r in actual if r["id"] in want),
                       "promptTokens": sum((by_id[i].get("metrics") or {}).get("prompt_tokens") or 0 for i in want if i in by_id),
                       "completionTokens": sum((by_id[i].get("metrics") or {}).get("completion_tokens") or 0 for i in want if i in by_id)},
            "missing": len(missing), "extra": len(extra), "duplicates": len(duplicates), "mismatched": len(mismatched),
            "samples": {"missing": sample(missing), "extra": sample(extra), "duplicates": sample(duplicates),
                        "mismatched": dict(list(mismatched.items())[:10])}}


def find(config, marker, hours):
    """Search recently received rows for a marker string, with event-to-arrival lag."""
    if not marker or len(marker) < 6:
        raise Failure("INVALID_INPUT", "find needs a --marker of at least 6 characters.", 2)
    since = iso(now_ms() - int(hours) * 3_600_000)
    pattern = sql_literal("%" + marker.replace("%", "").replace("_", "") + "%")
    rows = btql(config, f"SELECT id, root_span_id, span_attributes, input, output, metrics, created "
                        f"FROM project_logs({sql_literal(project_id(config))}) WHERE created >= {sql_literal(since)} "
                        f"AND metadata.source = 'muse.db' AND (input ILIKE {pattern} OR output ILIKE {pattern}) LIMIT 50")
    found = []
    for r in rows:
        start = (r.get("metrics") or {}).get("start")
        arrived = to_ms(r.get("created")) if r.get("created") else None
        found.append({"id": r["id"], "trace": r.get("root_span_id"), "type": (r.get("span_attributes") or {}).get("type"),
                      "name": (r.get("span_attributes") or {}).get("name"),
                      "at": iso(round(start * 1000)) if isinstance(start, (int, float)) else None,
                      "lagMinutes": round((arrived - start * 1000) / 60_000, 1) if arrived and isinstance(start, (int, float)) else None,
                      "input": clean(r.get("input"), 200), "output": clean(r.get("output"), 200)})
    return {"ok": True, "found": len(found), "rows": found}


def probe(config, scratch_project, wait_seconds=120):
    """Check Braintrust's insert semantics on a scratch project, then delete what it wrote."""
    if not scratch_project:
        raise Failure("INVALID_INPUT", "probe needs --project SCRATCH_PROJECT_ID (not your real project).", 2)
    if scratch_project == config.get("projectId"):
        raise Failure("INVALID_INPUT", "probe writes and deletes test rows; use a scratch project, not projectId.", 2)
    probe_config = {**config, "projectId": scratch_project}
    project_id(probe_config)
    trace, now = hexid(uuid.uuid4().hex, 32), now_ms()
    root_span, child_span = hexid(trace + ":root", 16), hexid(trace + ":child", 16)
    meta = {"source": "probe"}
    root = event(trace, root_span, None, "probe_task", "task", now - 2000, now, meta, input_="probe")
    child = event(trace, child_span, root_span, "probe_llm", "llm", now - 1500, now - 500, meta,
                  input_="question", output="first", metrics={"prompt_tokens": 7, "completion_tokens": 3, "tokens": 10})
    changed = {**child, "output": "second"}
    checks, started = {}, time.time()
    send(probe_config, [root, child])
    send(probe_config, [child])            # identical re-send
    send(probe_config, [changed])          # same id, new content
    sql = (f"SELECT {FIELDS} FROM project_logs({sql_literal(scratch_project)}) "
           f"WHERE root_span_id = {sql_literal(trace)} LIMIT 100")
    rows, visible_after = [], None
    while time.time() - started < wait_seconds:
        rows = btql(probe_config, sql)
        stored = {r["id"]: r for r in rows}
        if child["id"] in stored and root["id"] in stored and stored[child["id"]].get("output") == "second":
            visible_after = round(time.time() - started, 1)
            break
    stored = {r["id"]: r for r in rows}
    checks["readBackWithinWait"] = visible_after is not None
    checks["oneRowPerIdAfterThreeSends"] = len([r for r in rows if r["id"] == child["id"]]) == 1 and len(rows) == 2
    checks["resendReplacesContent"] = stored.get(child["id"], {}).get("output") == "second"
    checks["childNestsUnderRoot"] = (stored.get(child["id"], {}).get("span_parents") or []) == [root_span]
    checks["metricsRoundTrip"] = not [d for d in differences(changed, stored[child["id"]]) if d.startswith("metrics.")] \
        if child["id"] in stored else False
    checks["metadataRoundTrip"] = (stored.get(child["id"], {}).get("metadata") or {}).get("source") == "probe"
    send(probe_config, [{"id": root["id"], "_object_delete": True}, {"id": child["id"], "_object_delete": True}])
    deleted = False
    for _ in range(10):
        if not btql(probe_config, sql):
            deleted = True
            break
    checks["cleanedUp"] = deleted
    return {"ok": all(checks.values()), "checks": checks, "visibleAfterSeconds": visible_after,
            "rowsSeen": len(rows), "trace": trace}


def backfill(config_path, config, since_text, days, resend=False, only=None):
    """Queue older history: everything from `since` up to what is already sent.
    With resend, everything from `since` up to the last export instead, so rows
    already sent are rebuilt with the current queries and replaced in place.
    `only` limits the run to some queries. It runs through the usual plan/export loop."""
    if bool(since_text) == bool(days):
        raise Failure("INVALID_INPUT", "backfill needs exactly one of --since ISO or --days N.", 2)
    queries = configured_queries(config)
    if only:
        unknown = [name for name in only if name not in queries]
        if unknown:
            raise Failure("INVALID_INPUT", "--only names queries that are not configured: " + ", ".join(unknown), 2)
        unbounded = [name for name in only if ":limit" not in queries[name]]
        if unbounded:
            raise Failure("SETUP_ERROR", f"queries.{unbounded[0]} must end with LIMIT :limit to be sent on its own.", 6)
    with Locked(state_dir(config_path)) as directory:
        state = read_json(directory / "export.json")
        covered = state.get("coveredFrom")
        if covered is None or (resend and state.get("watermark") is None):
            raise Failure("NOTHING_EXPORTED", "Run the first export before backfilling.", 2)
        if state.get("run"):
            raise Failure("BUSY", "An export is in progress; finish it (plan/export until more: false), then backfill.", 3)
        since = to_ms(since_text) if since_text else now_ms() - int(days) * 86_400_000
        until = state["watermark"] if resend else covered
        if since >= until:
            return {"ok": True, "due": False, "reason": f"history from {iso(covered)} is already sent"}
        now = now_ms()
        state["run"] = {"since": since, "until": until, "cursor": since, "pages": 0, "spans": 0, "sentSpans": 0,
                        "startedAt": now, "touchedAt": now, "backfill": True,
                        **({"only": sorted(only)} if only else {}), **({"resend": True} if resend else {})}
        atomic_json(directory / "export.json", state)
    return {"ok": True, "due": True, "window": window_json({"since": since, "until": until}),
            **({"only": sorted(only)} if only else {}),
            "next": "run plan and export until more: false, as in On wake steps 2-3"}


def check(config):
    """Prove the key reaches the project without writing anything."""
    project = request_json(config, "GET", f"/v1/project/{project_id(config)}", retries=1)
    return {"ok": True, "projectId": project.get("id"), "projectName": project.get("name")}


def status(config_path, config):
    directory = state_dir(config_path)
    state = read_json(directory / "export.json")
    timer = read_json(directory / "timer.json")
    watermark = state.get("watermark")
    run = state.get("run")
    checked = read_json(config_path).get("setupReceipt") or {}
    covered = state.get("coveredFrom")
    return {"ok": True, "version": VERSION, "lastVerifiedLatest": checked.get("version"),
            "historyFrom": iso(covered) if covered else None, "lastBackfill": state.get("lastBackfill"),
            "stateDir": str(directory), "watermark": iso(watermark) if watermark else None,
            "runInProgress": {"since": iso(run["since"]), "cursor": iso(run["cursor"]), "until": iso(run["until"]), "backfill": bool(run.get("backfill")),
                              "only": run.get("only"),
                              "pages": run["pages"]} if run else None,
            "lastExport": state.get("lastExport"), "failure": state.get("failure"),
            "updateFailure": state.get("updateFailure"),
            "sentRegistry": len(state.get("sent", {})),
            "lagMinutes": round((now_ms() - watermark) / 60_000, 1) if watermark else None,
            "stats": state.get("stats", {}), "nextPageRows": page_limit(config, state),
            "nextDue": iso(timer["nextDue"]) if timer.get("nextDue") else None,
            "mode": "direct" if config.get("databases") else "tool"}


def hook(config_path, config, dry_run=False):
    """Decide whether an export is due. Never loads credentials."""
    try:
        with Locked(state_dir(config_path), wait=False) as directory:
            return due_wake(directory, config_path, config, dry_run)
    except Failure as failure:
        if failure.payload["code"] != "BUSY":
            raise
        return {"decision": "silent", "reason": "export running"}


def due_wake(directory, config_path, config, dry_run):
    timer = read_json(directory / "timer.json")
    state = read_json(directory / "export.json")
    now = now_ms()
    interval = int(config["intervalMinutes"]) * 60_000
    # An export since the last wake (including the one run at setup) sets the
    # next slot; otherwise the slot chosen at the last wake stands.
    exported_at = (state.get("lastExport") or {}).get("at", 0)
    if exported_at > timer.get("lastWakeAt", 0):
        due = exported_at + interval
    elif state.get("run") and not state.get("failure"):
        # A worker is paging through a run. Wait while it is active; resume it
        # once its lease lapses (the worker ended early).
        due = state["run"].get("touchedAt", 0) + RUN_LEASE_MS
    else:
        due = timer.get("nextDue", 0)
    if not dry_run and now < due:
        return {"decision": "silent", "reason": "export not due"}
    failures = (state.get("failure") or {}).get("count", 0)
    # Fallback if this wake's export never runs. Repeated failures back off
    # to at most once a day.
    delay = min(interval * 2 ** (failures - 2), max(interval, 24 * 3_600_000)) if failures >= 3 else interval
    # Compare the files actually installed with the latest release, so a partial
    # or edited install is caught too, not just a stale receipt.
    try:
        manifest = fetch_manifest(timeout=10)
        skill_sha, update_required = manifest["sha256"], bool(stale_files(manifest)[0])
        latest_version = manifest["version"]
    except Failure:
        skill_sha, update_required, latest_version = None, False, None
    payload_ = {"attemptId": str(uuid.uuid4()), "wakeId": str(uuid.uuid4()), "wakeEmittedAt": now,
                "config": str(config_path), "reasons": ["setup"] if dry_run else ["export"],
                "installedVersion": VERSION, "latestVersion": latest_version,
                "skillSha256": skill_sha, "updateRequired": update_required, "failureCount": failures,
                "watermark": iso(state["watermark"]) if state.get("watermark") else None}
    if not dry_run:
        timer.update(nextDue=now + delay, lastWakeAt=now, lastWakeId=payload_["wakeId"])
        atomic_json(directory / "timer.json", timer)
    return {"decision": "wake", "reason": "telemetry export due", "payload": payload_}


def main():
    parser = argparse.ArgumentParser(description="Muse → Braintrust telemetry. Every command prints one JSON object.")
    parser.add_argument("command", choices=["version", "setup-check", "self-update", "check", "plan", "export", "status",
                                            "hook", "probe", "reconcile", "find", "backfill"])
    parser.add_argument("--version", action="version", version=VERSION, help="Print the installed version and exit")
    parser.add_argument("--config", default=os.environ.get("BRAINTRUST_TELEMETRY_CONFIG"))
    parser.add_argument("--attempt", help="Wake attemptId; recorded for traceability only")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output", help="With export --dry-run, write the events that would be sent here")
    parser.add_argument("--project", help="probe: a scratch Braintrust project ID (rows are written, then deleted)")
    parser.add_argument("--since", help="reconcile: range start, ISO-8601")
    parser.add_argument("--until", help="reconcile: range end, ISO-8601 (clipped to the last export)")
    parser.add_argument("--marker", help="find: text to search for in exported inputs and outputs")
    parser.add_argument("--hours", type=int, default=48, help="find: how far back to search by arrival time")
    parser.add_argument("--days", type=int, help="backfill: send history from this many days ago")
    parser.add_argument("--resend", action="store_true",
                        help="backfill: rebuild and re-send history already sent, up to the last export")
    parser.add_argument("--only", help="backfill: comma-separated queries to read, e.g. tasks")
    args = parser.parse_args()
    if args.command == "version":
        return version_info()
    if args.command in ("setup-check", "self-update"):
        path = load_config(args.config)[0] if args.config else None
        previous = read_json(path).get("setupReceipt") if path else None
        if args.command == "setup-check":
            result = setup_check()
        else:
            try:
                result = self_update(previous)
            except Failure as failure:
                if path:
                    with Locked(state_dir(path)) as directory:
                        raise record_failure(directory, failure, slot="updateFailure")
                raise
            if path:
                with Locked(state_dir(path)) as directory:
                    state = read_json(directory / "export.json")
                    if state.pop("updateFailure", None) is not None:
                        atomic_json(directory / "export.json", state)
        if path:
            saved = {k: result[k] for k in ("version", "setupVersion", "sha256", "files", "verifiedAt")}
            atomic_json(path, {**read_json(path), "setupReceipt": saved})
        return result
    path, config = load_config(args.config)
    global API_OVERRIDE_OK
    API_OVERRIDE_OK = config.get("provider") == "env"
    if args.command == "check":
        return check(config)
    if args.command == "plan":
        return plan(path, config)
    if args.command == "export":
        return export(path, config, args.dry_run, args.output)
    if args.command == "status":
        return status(path, config)
    if args.command == "probe":
        return probe(config, args.project)
    if args.command == "reconcile":
        return reconcile(path, config, args.since, args.until)
    if args.command == "find":
        return find(config, args.marker, args.hours)
    if args.command == "backfill":
        only = [name.strip() for name in args.only.split(",") if name.strip()] if args.only else None
        return backfill(path, config, args.since, args.days, args.resend, only)
    return hook(path, config, args.dry_run)


if __name__ == "__main__":
    try:
        print(json.dumps(main(), ensure_ascii=False))
    except Failure as error:
        print(json.dumps(error.payload, ensure_ascii=False))
        sys.exit(error.exit_code)
    except (ValueError, KeyError, TypeError, OSError) as error:
        print(json.dumps({"ok": False, "code": "INVALID_INPUT", "message": f"{type(error).__name__}: {str(error)[:200]}"}))
        sys.exit(2)

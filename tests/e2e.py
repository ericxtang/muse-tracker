#!/usr/bin/env python3
"""End-to-end run of a released muse-tracker against mock backends.

    python3 tests/e2e.py [--report PATH]
    LIVE_LANGFUSE_PUBLIC_KEY=… LIVE_LANGFUSE_SECRET_KEY=… \
        python3 tests/e2e.py --langfuse-live http://localhost:3000

With --langfuse-live, the Langfuse phases run against that real server through
a stand-in for Muse's Secure Vault (tests/live_langfuse.py): the CLI runs with
provider "muse" and only ever holds a surrogate.

It builds a release, publishes it to a mock GitHub (with the latest/download
redirect), installs it the way setup.md does, and drives the installed CLI the
way a Muse worker does (plan, run each query on muse.db, export) against a
SQLite muse.db with the real table layout (sample/make_sample.py). Every phase
runs for Braintrust (replaces a row sent again) and Langfuse (keeps every
write), and records what it observed. Exit status is 0 only if every check
passes.
"""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "sample"))
import build  # noqa: E402
import make_sample  # noqa: E402
from test_muse_tracker import Braintrust, Langfuse, PROJECT  # noqa: E402

NOW = datetime.now(timezone.utc).replace(microsecond=0)
PREAMBLE = ("[Subagent Context] You are running as a subagent (depth 1/2). "
            "Task: find three vegetarian restaurants near Union Square that take reservations.")

# The saved queries, as Muse would write them for this schema.
TASKS_SQL = """
SELECT * FROM (
  SELECT a.agent_id AS task_id, a.agent_id AS agent_id,
    (SELECT MIN(c.created_at) FROM "agent.context_items" c WHERE c.agent_id = a.agent_id) AS start,
    (SELECT MAX(c.created_at) FROM "agent.context_items" c WHERE c.agent_id = a.agent_id
      AND julianday(c.created_at) < julianday(:until_iso)) AS "end",
    (SELECT substr(c.text_content, 1, 2000) FROM "agent.context_items" c
      WHERE c.agent_id = a.agent_id AND c.item_kind = 'message_text' AND c.role = 'user'
        AND c.text_content NOT LIKE '[Subagent Context]%' ORDER BY c.seq LIMIT 1) AS request_text,
    (SELECT substr(c.text_content, 1, 2000) FROM "agent.context_items" c
      WHERE c.agent_id = a.agent_id AND c.text_content LIKE '[Subagent Context]%' ORDER BY c.seq LIMIT 1) AS context_text,
    substr(a.last_assistant_message, 1, 2000) AS output_text,
    COALESCE(a.root_session_id, a.session_id) AS session_id, a.parent_agent_id AS parent_task_id,
    a.status AS status, COALESCE(a.agent_type, a.kind) AS kind, a.model AS model, a.depth AS depth
  FROM "agent.agents" a
) t
WHERE julianday(t."end") >= julianday(:since_iso) AND julianday(t."end") < julianday(:until_iso)
ORDER BY julianday(t."end"), t.task_id LIMIT :limit"""

TOOLS_SQL = """
SELECT c.agent_id AS task_id, c.seq AS seq, c.tool_name AS tool, rc.server_name AS server,
  substr(rc.arguments_json, 1, 2000) AS args,
  substr(COALESCE(ro.output_text, ro.error_text), 1, 2000) AS result,
  c.created_at AS started_at, o.created_at AS ended_at, o.success AS success
FROM "agent.context_items" c
LEFT JOIN "runtime.tool_calls" rc ON rc.tool_call_id = c.tool_call_id
LEFT JOIN "agent.context_items" o ON o.agent_id = c.agent_id AND o.item_kind = 'tool_output' AND o.call_id = c.call_id
LEFT JOIN "runtime.tool_outputs" ro ON ro.tool_output_id = o.tool_output_id
WHERE c.item_kind = 'tool_call'
  AND julianday(c.created_at) >= julianday(:since_iso) AND julianday(c.created_at) < julianday(:until_iso)
ORDER BY julianday(c.created_at), c.seq LIMIT :limit"""

MODELS_SQL = """
SELECT u.agent_id AS task_id, u.message_id AS message_id, u.created_at AS at,
  u.input_tokens AS input_tokens, u.output_tokens AS output_tokens, a.model AS model,
  substr(m.body, 1, 2000) AS output_text
FROM "agent.agent_message_token_usage" u
LEFT JOIN "agent.agents" a ON a.agent_id = u.agent_id
LEFT JOIN "runtime.messages" m ON m.message_id = u.message_id
WHERE u.created_at * 1000 >= :since_ms AND u.created_at * 1000 < :until_ms
ORDER BY u.created_at, u.message_id LIMIT :limit"""


class MuseDB:
    """A SQLite muse.db with the real table layout, filled with a short story."""

    def __init__(self, path):
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.executescript(make_sample.DDL)
        self.event = 0

    def ts(self, at):
        return at.isoformat(timespec="seconds")

    def agent(self, agent_id, at, kind, session, parent=None, answer=None, model="claude-opus-5-5", depth=0):
        self.conn.execute('INSERT INTO "agent.agents" (agent_id, id, session_id, root_session_id, kind, agent_type, '
                          'parent_agent_id, model, status, depth, created_at, updated_at, last_assistant_message) '
                          'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                          (agent_id, agent_id, session, session, kind, kind, parent, model, "completed", depth,
                           int(at.timestamp()), int(at.timestamp()), answer))
        self.seq = getattr(self, "seq", {})
        self.seq[agent_id] = 0

    def item(self, agent_id, at, kind, **fields):
        self.seq[agent_id] += 1
        cols = {"agent_id": agent_id, "seq": self.seq[agent_id], "item_kind": kind, "created_at": self.ts(at), **fields}
        self.conn.execute(f'INSERT INTO "agent.context_items" ({", ".join(cols)}) VALUES ({", ".join("?" * len(cols))})',
                          list(cols.values()))

    def message(self, agent_id, at, role, text, tokens=None):
        self.event += 1
        message_id = f"msg-{agent_id[:4]}-{self.event}"
        self.conn.execute('INSERT INTO "runtime.messages" (message_id, event_seq, role, created_at, body) VALUES (?,?,?,?,?)',
                          (message_id, self.event, role, self.ts(at), text))
        self.item(agent_id, at, "message_text", role=role, message_id=message_id, text_content=text)
        if tokens:
            self.conn.execute('INSERT INTO "agent.agent_message_token_usage" VALUES (?,?,?,?,?)',
                              (agent_id, message_id, tokens[0], tokens[1], int(at.timestamp())))

    def tool(self, agent_id, at, name, args, output, ok=True, server=None, seconds=4):
        self.event += 1
        call_id = f"call-{self.event}"
        cur = self.conn.execute('INSERT INTO "runtime.tool_calls" (event_seq, call_id, tool_name, server_name, status, '
                                'created_at, arguments_json) VALUES (?,?,?,?,?,?,?)',
                                (self.event, call_id, name, server, "completed", self.ts(at), json.dumps(args)))
        self.item(agent_id, at, "tool_call", tool_call_id=cur.lastrowid, call_id=call_id, tool_name=name)
        self.event += 1
        done = at + timedelta(seconds=seconds)
        cur = self.conn.execute('INSERT INTO "runtime.tool_outputs" (event_seq, call_id, status, created_at, output_text, '
                                'error_text) VALUES (?,?,?,?,?,?)',
                                (self.event, call_id, "ok" if ok else "error", self.ts(done),
                                 output if ok else None, None if ok else output))
        self.item(agent_id, done, "tool_output", tool_output_id=cur.lastrowid, call_id=call_id, tool_name=name,
                  success=1 if ok else 0)

    def commit(self):
        self.conn.commit()


def seed(db):
    """A conversation, the subagent it spawned, and an empty hook-worker shell."""
    t = NOW - timedelta(hours=3)
    db.agent("conv-a1", t, "root", "sess-dinner", answer="Booked Lupa for Friday 7pm for two; confirmation LP-2291.")
    db.message("conv-a1", t, "user", "Book a table for two somewhere vegetarian near Union Square on Friday at 7.")
    db.tool("conv-a1", t + timedelta(seconds=20), "spawn_subagent", {"task": "find restaurants"}, "subagent sub-b2 started")
    sub = t + timedelta(seconds=25)
    db.agent("sub-b2", sub, "subagent", "sess-dinner", parent="conv-a1", depth=1,
             answer="Lupa, Dirt Candy and Blossom; all take reservations.")
    db.message("sub-b2", sub, "user", PREAMBLE)
    db.tool("sub-b2", sub + timedelta(seconds=5), "web_search", {"q": "vegetarian union square reservations"},
            "10 results", server="search")
    db.tool("sub-b2", sub + timedelta(seconds=15), "web_search", {"q": "lupa menu"}, "menu page", server="search")
    db.message("sub-b2", sub + timedelta(seconds=30), "assistant", "Lupa, Dirt Candy and Blossom; all take reservations.",
               tokens=(5200, 140))
    db.tool("conv-a1", t + timedelta(minutes=2), "reserve_table", {"venue": "Lupa", "time": "19:00"},
            "slot taken", ok=False, server="opentable")
    db.tool("conv-a1", t + timedelta(minutes=3), "reserve_table", {"venue": "Lupa", "time": "19:15"},
            "LP-2291 Authorization: Bearer sk-live-abcdefghijklmnopqrstuvwx", server="opentable")
    db.message("conv-a1", t + timedelta(minutes=4), "assistant", "Booked Lupa for Friday 7pm for two; confirmation LP-2291.",
               tokens=(8100, 95))
    # The person replies while the export runs: activity after the run's end must not hide the agent's row.
    db.item("conv-a1", NOW + timedelta(hours=1), "message_text", role="user", text_content="Thanks!")
    shell = NOW - timedelta(hours=2)
    db.agent("hook-c3", shell, "hook_worker", None, depth=1)
    db.message("hook-c3", shell, "user", "[Subagent Context] You are running as a hook worker. Check the inbox.")
    db.commit()


class Run:
    def __init__(self, report, live=None):
        self.report, self.failed, self.live = report, 0, live
        self.tmp = Path(tempfile.mkdtemp(prefix="muse-tracker-e2e-"))
        self.github = Braintrust(static=self.tmp / "github")   # serves releases (and doubles as Braintrust)
        self.bt = self.github
        if live:
            import live_langfuse
            self.secret = os.environ["LIVE_LANGFUSE_SECRET_KEY"]
            self.vault = live_langfuse.VaultProxy(self.tmp / "vault", live, self.secret)
            # A real server keeps earlier runs' spans: give this run its own agent (trace ids derive from it).
            self.agent = "muse-e2e-" + __import__("uuid").uuid4().hex[:8]
            self.lf = live_langfuse.RealLangfuse(live, os.environ["LIVE_LANGFUSE_PUBLIC_KEY"], self.secret, self.vault,
                                                 agent_tag=f"muse-agent:{self.agent}")
        else:
            self.agent = "muse-e2e"
            self.lf = Langfuse()

    def check(self, phase, what, ok, evidence):
        self.failed += not ok
        self.report.append(f"| {phase} | {what} | {'PASS' if ok else '**FAIL**'} | {evidence} |")

    def env(self, state):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("LANGFUSE_", "LIVE_LANGFUSE_"))}
        env.update(MUSE_TRACKER_STATE_DIR=str(state), BRAINTRUST_API_KEY="test-key", BRAINTRUST_API_URL=self.bt.url,
                   BRAINTRUST_BTQL_SPACING="0")
        if self.live:  # Muse's way: egress through the vault proxy, which trusts its own CA
            env.update(HTTPS_PROXY=self.vault.url, SSL_CERT_FILE=str(self.vault.ca), NO_PROXY="127.0.0.1")
        else:
            env.update(LANGFUSE_HOST=self.lf.url, LANGFUSE_PUBLIC_KEY="pk-test", LANGFUSE_SECRET_KEY="sk-test")
        return env

    def cli(self, config, *args, state=None, expect=0):
        proc = subprocess.run([sys.executable, str(self.script), *args, "--config", str(config)], capture_output=True,
                              text=True, env=self.env(state or self.tmp / "state"))
        out = json.loads(proc.stdout) if proc.stdout.strip() else {}
        if proc.returncode != expect:
            raise AssertionError(f"{args}: exit {proc.returncode}, expected {expect}: {proc.stdout}{proc.stderr}")
        return out

    def wake(self, config, db, state=None):
        """One worker wake in tool mode: plan, run each query on muse.db, export, until done."""
        results = []
        while True:
            plan = self.cli(config, "plan", state=state)
            if not plan.get("due"):
                return results
            conn = sqlite3.connect(db)
            for q in plan["queries"]:
                cur = conn.execute(q["sql"])
                names = [c[0] for c in cur.description]
                Path(q["saveTo"]).write_text(json.dumps([dict(zip(names, r)) for r in cur.fetchall()]))
            conn.close()
            results.append(self.cli(config, "export", state=state))
            if not results[-1]["more"]:
                return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", default=str(Path(tempfile.gettempdir()) / "muse-tracker-e2e.md"))
    parser.add_argument("--langfuse-live", metavar="URL", help="run the Langfuse phases against this real server")
    args = parser.parse_args()
    report = ["| Phase | Check | Result | Evidence |", "| --- | --- | --- | --- |"]
    run = Run(report, args.langfuse_live)
    samples = {}
    try:
        # 1. Release and install, as setup.md does it.
        manifest = build.build(run.github.url, run.github.static)
        latest = json.loads(urllib.request.urlopen(manifest["manifestUrl"]).read())
        archive = urllib.request.urlopen(latest["archiveUrl"]).read()
        run.check("Install", "latest/download redirects to the newest manifest", latest["version"] == manifest["version"],
                  f"version {latest['version']}")
        run.check("Install", "archive SHA-256 matches the manifest", hashlib.sha256(archive).hexdigest() == latest["sha256"],
                  latest["sha256"][:16] + "…")
        home = run.tmp / "home"
        with zipfile.ZipFile(__import__("io").BytesIO(archive)) as z:
            z.extractall(home / "skills")
        run.script = home / "skills" / "muse-tracker" / "scripts" / "muse_tracker.py"
        info = subprocess.run([sys.executable, str(run.script), "version"], capture_output=True, text=True,
                              env=run.env(run.tmp / "state")).stdout
        run.check("Install", "installed copy reports upToDate", json.loads(info)["upToDate"], info.strip()[:90])

        db = run.tmp / "muse.db"
        muse = MuseDB(db)
        seed(muse)
        queries = {"tasks": TASKS_SQL, "tool_calls": TOOLS_SQL, "model_calls": MODELS_SQL}
        configs = {}
        for dest in ("braintrust", "langfuse"):
            config = run.tmp / dest / "config.json"
            config.parent.mkdir()
            base = {"provider": "env", "agentId": run.agent, "tasksTimeColumn": "end", "destination": dest, "queries": queries, "maxRows": 5,
                    "setupVersion": build.SETUP_VERSION}
            if dest == "braintrust":
                base["projectId"] = PROJECT
            else:
                base.update(rootSettleMinutes=30, langfuseReadSpacingSeconds=0, langfuseVisibilitySeconds=10)
                if run.live:
                    import live_langfuse
                    base.update(provider="muse", connector="custom.langfuse", helperPath=str(run.vault.helper),
                                langfuseHost=f"https://{live_langfuse.HOST}")
            config.write_text(json.dumps(base))
            configs[dest] = config
            run.cli(config, "setup-check")
            checked = run.cli(config, "check")
            run.check(f"Setup · {dest}", "check reaches the project with the key", bool(checked.get("projectName")),
                      f"project {checked.get('projectName')!r}")

        # 2. The hook, under a stub of Muse's runtime.
        stub = run.tmp / "runtime.sh"
        stub.write_text('wake() { echo "WAKE|$1"; exit 0; }\nsilent() { echo "SILENT|$1"; exit 0; }\nlog() { :; }\n')
        hook = home / "skills" / "muse-tracker" / "assets" / "export-hook.sh"
        hook.chmod(0o755)
        out = [subprocess.run(["bash", str(hook), str(configs["braintrust"])], capture_output=True, text=True,
                              env={**run.env(run.tmp / "state"), "HATCH_HOOK_RUNTIME": str(stub)}).stdout.strip()
               for _ in range(2)]
        run.check("Hook", "first poll wakes a worker, the next stays silent",
                  out[0].startswith("WAKE") and out[1].startswith("SILENT"), " / ".join(out))

        for dest, config in configs.items():
            phase = dest.capitalize()
            plan = run.cli(config, "plan")
            conn = sqlite3.connect(db)
            for q in plan["queries"]:
                cur = conn.execute(q["sql"])
                names = [c[0] for c in cur.description]
                Path(q["saveTo"]).write_text(json.dumps([dict(zip(names, r)) for r in cur.fetchall()]))
            conn.close()
            dry = run.cli(config, "export", "--dry-run", "--output", str(run.tmp / f"{dest}-preview.json"))
            run.check(phase, "dry run previews without sending",
                      dry["dryRun"] and not (run.bt.inserts if dest == "braintrust" else run.lf.posts),
                      f"{dry['unsent']} spans previewed, 0 sent")
            # wake() ends after the first plan of a new run that is not due; the dry run planned page 1.
            first = run.wake(config, db)
            run.check(phase, "first wake pages through everything", len(first) >= 2 and not first[-1]["more"],
                      f"{len(first)} pages, {sum(r['pageSent'] for r in first)} spans sent")

        # 3. What arrived, per destination.
        bt_rows = [r for r in run.bt.rows.values()]
        roots = {r["metadata"]["task_id"]: r for r in bt_rows if r["span_attributes"]["type"] == "task"}
        run.check("Braintrust", "one trace per agent, roots for all three", sorted(roots) == ["conv-a1", "hook-c3", "sub-b2"],
                  ", ".join(sorted(roots)))
        run.check("Braintrust", "subagent root input is its [Subagent Context] preamble",
                  roots["sub-b2"].get("input") == PREAMBLE and roots["sub-b2"]["metadata"]["request_source"] == "context",
                  roots["sub-b2"].get("input", "")[:60] + "…")
        run.check("Braintrust", "conversation root carries request, answer, session",
                  roots["conv-a1"]["output"].startswith("Booked Lupa") and roots["conv-a1"]["metadata"]["session_id"] == "sess-dinner",
                  f"output {roots['conv-a1']['output'][:28]!r}…, session sess-dinner")
        run.check("Braintrust", "an agent active while the export runs still gets its root",
                  roots["conv-a1"].get("input", "").startswith("Book a table"), "reply at +1h; root has its request")
        run.check("Braintrust", "subagent links to its parent",
                  roots["sub-b2"]["metadata"].get("parent_task_id") == "conv-a1", "parent_task_id conv-a1")
        llm = [r for r in bt_rows if r["span_attributes"]["type"] == "llm"]
        run.check("Braintrust", "token counts and message text on llm spans",
                  sorted((r["metrics"]["prompt_tokens"], r["metrics"]["completion_tokens"]) for r in llm) == [(5200, 140), (8100, 95)]
                  and all(r.get("output") for r in llm), "5200/140 and 8100/95 with message text")
        failed = [r for r in bt_rows if r.get("error")]
        run.check("Braintrust", "failed tool call marked as an error", len(failed) == 1 and failed[0]["error"] == "slot taken",
                  "reserve_table → error 'slot taken'")
        run.check("Braintrust", "secrets redacted before sending", "sk-live-abcdefghijklmnopqrstuvwx" not in json.dumps(run.bt.inserts),
                  "bearer token replaced with [redacted]")

        lf = run.lf
        types = sorted({s["attrs"]["langfuse.observation.type"] for s in lf.spans})
        run.check("Langfuse", "observations typed agent / generation / tool", types == ["agent", "generation", "tool"], ", ".join(types))
        agents = {s["attrs"]["langfuse.trace.metadata.task_id"]: s for s in lf.of_type("agent")}
        run.check("Langfuse", "every root sent once the task was idle", sorted(agents) == ["conv-a1", "hook-c3", "sub-b2"],
                  f"{len(agents)} roots")
        run.check("Langfuse", "subagent root input is its preamble",
                  json.loads(agents["sub-b2"]["attrs"]["langfuse.observation.input"]) == PREAMBLE, "input = [Subagent Context] …")
        sess = {s["attrs"].get("langfuse.session.id") for s in lf.spans if s["attrs"]["langfuse.trace.metadata.task_id"] != "hook-c3"}
        run.check("Langfuse", "conversation and subagent share one session on every span", sess == {"sess-dinner"}, "session sess-dinner")
        gens = lf.of_type("generation")
        run.check("Langfuse", "usage_details and model on generations",
                  sorted(json.loads(g["attrs"]["langfuse.observation.usage_details"])["input"] for g in gens) == [5200, 8100]
                  and all(g["attrs"]["langfuse.observation.model.name"] == "claude-opus-5-5" for g in gens), "input 5200 / 8100, model set")
        errs = [s for s in lf.of_type("tool") if s["attrs"].get("langfuse.observation.level") == "ERROR"]
        run.check("Langfuse", "failed tool call has level ERROR", len(errs) == 1 and errs[0]["status"]["code"] == 2, "1 ERROR span")
        run.check("Langfuse", "no duplicate spans", lf.duplicates() == [], f"{len(lf.spans)} spans, 0 duplicate ids")
        samples["braintrust"] = sorted((r for r in bt_rows if r["root_span_id"] == roots["sub-b2"]["root_span_id"]),
                                       key=lambda r: r["metrics"]["start"])
        samples["langfuse"] = sorted((s for s in lf.spans if s["traceId"] == agents["sub-b2"]["traceId"]), key=lambda s: s["start"])

        # 4. Later activity: a new task, plus a late row the overlap must catch.
        muse.agent("conv-d4", NOW - timedelta(minutes=8), "root", "sess-email", answer="Drafted the reply.")
        muse.message("conv-d4", NOW - timedelta(minutes=8), "user", "Draft a reply to Sam about Friday.")
        muse.tool("conv-d4", NOW - timedelta(minutes=7), "gmail_draft", {"to": "sam"}, "draft saved", server="gmail")
        late = datetime.fromtimestamp(json.loads((next((run.tmp / "state").glob("muse-tracker-*")) / "export.json")
                                                 .read_text())["watermark"] / 1000, timezone.utc) - timedelta(minutes=5)
        muse.tool("conv-a1", late, "calendar_add", {"event": "Lupa"}, "added")  # written late, starts before the watermark
        muse.commit()
        before = {"braintrust": len(run.bt.rows), "langfuse": len(lf.spans)}
        for dest, config in configs.items():
            results = run.wake(config, db)
            phase = dest.capitalize()
            if dest == "braintrust":
                ids = [e["id"] for i in run.bt.inserts for e in i["events"]]
                run.check(phase, "second wake catches the late row and the new task",
                          any(r["span_attributes"]["name"] == "calendar_add" for r in run.bt.rows.values())
                          and any(r["metadata"].get("task_id") == "conv-d4" for r in run.bt.rows.values()),
                          f"rows {before[dest]} → {len(run.bt.rows)}")
            else:
                names = {s["name"] for s in lf.spans}
                held = results[-1].get("rootsHeld")
                run.check(phase, "late row and new task's calls arrive; the busy task's root is held",
                          "calendar_add" in names and "gmail_draft" in names and held == 1
                          and "conv-d4" not in {s["attrs"]["langfuse.trace.metadata.task_id"] for s in lf.of_type("agent")},
                          f"spans {before[dest]} → {len(lf.spans)}, rootsHeld {held}")
                run.check(phase, "no duplicates after the overlap re-read", lf.duplicates() == [], "0 duplicate ids")

        # 5. Braintrust reconcile: muse.db against what was stored.
        recon_args = ["reconcile", "--since", (NOW - timedelta(days=1)).isoformat(), "--until", NOW.isoformat()]
        first = run.cli(configs["braintrust"], *recon_args)
        conn = sqlite3.connect(db)
        for q in first.get("queries", []):
            cur = conn.execute(q["sql"])
            names = [c[0] for c in cur.description]
            Path(q["saveTo"]).write_text(json.dumps([dict(zip(names, r)) for r in cur.fetchall()]))
        recon = run.cli(configs["braintrust"], *recon_args)
        counts = {k: recon.get(k) for k in ("missing", "extra", "duplicates", "mismatched")}
        run.check("Braintrust", "reconcile: nothing missing, extra, duplicated or altered",
                  all(v in (0, []) for v in counts.values()), json.dumps(counts))

        # 6. Lost state: the whole history is exported again.
        for dest, config in configs.items():
            state = run.tmp / f"state-lost-{dest}"
            n = len(run.bt.rows) if dest == "braintrust" else len(lf.spans)
            run.wake(config, db, state=state)
            after = len(run.bt.rows) if dest == "braintrust" else len(lf.spans)
            how = "rows replaced in place" if dest == "braintrust" else "Langfuse asked what it already holds"
            run.check(dest.capitalize(), "re-export after lost state adds nothing", after == n and
                      (dest == "braintrust" or lf.duplicates() == []), f"{n} → {after}; {how}")

        # 7. Failures: Braintrust 500s are retried; a Langfuse 500 after storing is checked before the retry.
        muse.tool("conv-d4", NOW - timedelta(minutes=1), "gmail_send", {"draft": 1}, "sent", server="gmail")
        muse.commit()
        run.bt.responses = [500, 500]
        res = run.wake(configs["braintrust"], db)
        run.check("Braintrust", "two 500s in a row are retried; the page lands", res and res[-1]["ok"],
                  f"pageSent {res[-1]['pageSent'] if res else None}")
        lf.fail_after_store = 1
        plan = run.cli(configs["langfuse"], "plan")
        conn = sqlite3.connect(db)
        for q in plan["queries"]:
            cur = conn.execute(q["sql"])
            names = [c[0] for c in cur.description]
            Path(q["saveTo"]).write_text(json.dumps([dict(zip(names, r)) for r in cur.fetchall()]))
        failed = run.cli(configs["langfuse"], "export", expect=5)
        stored = len(lf.spans)
        run.wake(configs["langfuse"], db)
        run.check("Langfuse", "500 after storing → SEND_UNCERTAIN; retry checks first, no duplicate",
                  failed["code"] == "SEND_UNCERTAIN" and lf.duplicates() == [] and len(lf.spans) == stored,
                  f"{failed['code']}; spans {stored} → {len(lf.spans)}, 0 duplicates")

        # 8. The held root goes once the task is idle.
        cfg = json.loads(configs["langfuse"].read_text())
        configs["langfuse"].write_text(json.dumps({**cfg, "rootSettleMinutes": 0}))
        run.wake(configs["langfuse"], db)
        roots_now = [s["attrs"]["langfuse.trace.metadata.task_id"] for s in lf.of_type("agent")]
        run.check("Langfuse", "held root sent once the task is idle, exactly once",
                  roots_now.count("conv-d4") == 1 and lf.duplicates() == [], f"roots {sorted(roots_now)}")
        run.check("Langfuse", "resend refused (it would duplicate)",
                  run.cli(configs["langfuse"], "backfill", "--resend", "--days", "7", expect=2)["code"] == "NOT_SUPPORTED",
                  "NOT_SUPPORTED")

        if run.live:
            leaked = [str(f) for f in run.tmp.rglob("*") if f.is_file() and f.suffix in (".json", ".py", ".md", "")
                      and run.secret.encode() in f.read_bytes()]
            run.check("Vault", "every Langfuse request went through the vault stand-in with a surrogate",
                      run.vault.swaps > 0 and run.vault.refused == 0,
                      f"{run.vault.swaps} surrogates swapped, {run.vault.refused} requests refused")
            run.check("Vault", "the secret key never appears in config, state or helper files", not leaked,
                      "0 files contain it" if not leaked else ", ".join(leaked[:3]))

        # 9. Update: a newer release on the mock GitHub reaches the install.
        source = run.tmp / "source-next"
        shutil.copytree(ROOT / "skills" / "muse-tracker", source)
        with (source / "SKILL.md").open("a") as f:
            f.write("\nnext release\n")
        nxt = build.build(run.github.url, run.github.static, source=source, version="9.9.9")
        payload = run.cli(configs["braintrust"], "hook", "--dry-run")["payload"]
        updated = run.cli(configs["braintrust"], "self-update")
        info = json.loads(subprocess.run([sys.executable, str(run.script), "version"], capture_output=True, text=True,
                                         env=run.env(run.tmp / "state")).stdout)
        run.check("Update", "hook sees the new release; self-update installs and verifies it",
                  payload["updateRequired"] and updated["updated"] and info["installed"] == "9.9.9" and info["upToDate"],
                  f"{payload['installedVersion']} → {info['installed']}, upToDate {info['upToDate']}")
    except Exception as error:  # report, don't hide
        run.check("Run", "completed without an exception", False, f"{type(error).__name__}: {str(error)[:300]}")
    finally:
        run.github.close()
        run.lf.close()

    passed = sum(1 for line in report[2:] if "| PASS |" in line)
    where = f"real Langfuse at {args.langfuse_live} via the vault stand-in" if args.langfuse_live else "mock Langfuse"
    text = [f"# muse-tracker end-to-end run", "",
            f"{NOW.isoformat()} · release {build.current_version()} · Braintrust: mock · Langfuse: {where} · "
            f"{passed}/{len(report) - 2} checks passed", ""] + report
    for dest, rows in samples.items():
        text += ["", f"## Sample: the subagent's trace as stored by mock {dest.capitalize()}", "", "```json",
                 json.dumps(trim(rows), indent=1, ensure_ascii=False)[:6000], "```"]
    Path(args.report).write_text("\n".join(text) + "\n")
    print("\n".join(text[:len(report) + 4]))
    print(f"\nFull report with sample traces: {args.report}")
    shutil.rmtree(run.tmp, ignore_errors=True)
    return 1 if run.failed else 0


def trim(rows):
    out = []
    for r in rows:
        r = json.loads(json.dumps(r))
        for key in ("_xact_id",):
            r.pop(key, None)
        out.append(r)
    return out


if __name__ == "__main__":
    sys.exit(main())

"""End-to-end tests for the braintrust-telemetry skill. Run: python3 -m unittest -v

A mock Braintrust keeps rows keyed by id, replacing on re-insert as the real
insert API documents. A real SQLite file stands in for muse.db: direct mode
reads it, and tool mode runs the plan's rendered SQL against it the way the
agent would through the muse.db tool.
"""
from datetime import datetime, timedelta, timezone
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / "skills" / "braintrust-telemetry"
CLI = SKILL_DIR / "scripts" / "braintrust_telemetry.py"
sys.path.insert(0, str(ROOT))
import build  # noqa: E402

PROJECT = "test-project-0001"
SCRATCH = "scratch-project-0001"


class Braintrust:
    """Mock API: project lookup and project_logs insert with replace-by-id."""

    def __init__(self, static=None):
        self.inserts, self.rows, self.responses, self.static, self.gets = [], {}, [], static, []
        self.queries, self.hidden_reads = [], 0  # hidden_reads: BTQL reads that see nothing yet
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, code, body):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                outer.gets.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}})
                if self.path == f"/v1/project/{PROJECT}":
                    return self.reply(200, {"id": PROJECT, "name": "Test"})
                path = outer.static / self.path.split("?", 1)[0].lstrip("/") if outer.static else None
                if path and path.is_file():
                    return self.reply(200, path.read_bytes())
                self.reply(404, {"error": "not found"})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                code = outer.responses.pop(0) if outer.responses else 200
                if code != 200:
                    return self.reply(code, {"error": "no"})
                if self.path == "/btql":
                    return self.reply(200, {"data": outer.btql(body["query"])})
                match = re.fullmatch(r"/v1/project_logs/([\w-]+)/insert", self.path)
                if not match or match.group(1) not in (PROJECT, SCRATCH):
                    return self.reply(404, {"error": "unknown project"})
                outer.inserts.append({"headers": {k.lower(): v for k, v in self.headers.items()}, "events": body["events"]})
                for event in body["events"]:
                    if event.get("_object_delete"):
                        outer.rows.pop(event["id"], None)
                    else:
                        outer.rows[event["id"]] = {**event, "_xact_id": str(len(outer.inserts))}  # replace, never duplicate
                self.reply(200, {"row_ids": [e["id"] for e in body["events"]]})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def btql(self, query):
        """Just enough SQL for the CLI's own queries."""
        self.queries.append(query)
        if self.hidden_reads:
            self.hidden_reads -= 1
            return []
        rows = list(self.rows.values())
        traces = re.search(r"root_span_id (?:IN \((.*?)\)|= ('[^']*'))", query)
        if traces:
            wanted = set(re.findall(r"'([^']*)'", traces.group(1) or traces.group(2)))
            rows = [r for r in rows if r["root_span_id"] in wanted]
        like = re.search(r"input ILIKE '%(.*?)%'", query)
        if like:
            needle = like.group(1).lower()
            rows = [r for r in rows if r.get("metadata", {}).get("source") == "muse.db" and
                    any(needle in str(r.get(k) or "").lower() for k in ("input", "output"))]
            rows = [{**r, "created": datetime.now(timezone.utc).isoformat()} for r in rows]
        limit = int(re.search(r"LIMIT (\d+)", query).group(1))
        return rows[:limit]

    def sent(self):
        return [e for request in self.inserts for e in request["events"]]

    def of_type(self, kind):
        return [r for r in self.rows.values() if r["span_attributes"]["type"] == kind]


def iso(dt):
    return dt.isoformat(timespec="seconds")


TOOL_SQL = ("SELECT task_id, seq, tool, args, result, started_at, ended_at, success FROM agent.tools "
            "WHERE julianday(started_at) >= julianday(:since_iso) AND julianday(started_at) < julianday(:until_iso) "
            "ORDER BY julianday(started_at), seq LIMIT :limit")
MODEL_SQL = ("SELECT task_id, message_id, at, input_tokens, output_tokens, model FROM agent.models "
             "WHERE julianday(at) >= julianday(:since_iso) AND julianday(at) < julianday(:until_iso) "
             "ORDER BY julianday(at) LIMIT :limit")
TASK_SQL = ("SELECT task_id, start, request_text, context_text, surface FROM agent.tasks "
            "WHERE julianday(start) >= julianday(:since_iso) AND julianday(start) < julianday(:until_iso)")
# What installs set up before 0.0.4 saved: no preamble column and no LIMIT.
OLD_TASK_SQL = ("SELECT task_id, start, request_text, surface FROM agent.tasks "
                "WHERE julianday(start) >= julianday(:since_iso) AND julianday(start) < julianday(:until_iso)")
LIMITED_TASK_SQL = TASK_SQL + " ORDER BY julianday(start) LIMIT :limit"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.bt = Braintrust()
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.db = self.tmp / "agent.db"
        with sqlite3.connect(self.db) as conn:
            conn.execute("CREATE TABLE tools (task_id, seq, tool, args, result, started_at, ended_at, success)")
            conn.execute("CREATE TABLE models (task_id, message_id, at, input_tokens, output_tokens, model)")
            conn.execute("CREATE TABLE tasks (task_id, start, request_text, context_text, surface)")
        self.config_path = self.tmp / "config.json"
        self.write_config()

    def tearDown(self):
        self.bt.close()

    def write_config(self, **extra):
        config = {"provider": "env", "projectId": PROJECT, "agentId": "dot",
                  "queries": {"tool_calls": TOOL_SQL, "model_calls": MODEL_SQL, "tasks": TASK_SQL}, **extra}
        self.config_path.write_text(json.dumps(config))

    def cli(self, *args, script=CLI, expect=0):
        env = {**os.environ, "BRAINTRUST_TELEMETRY_STATE_DIR": str(self.tmp / "state"),
               "BRAINTRUST_API_KEY": "test-key", "BRAINTRUST_API_URL": self.bt.url, "BRAINTRUST_BTQL_SPACING": "0"}
        proc = subprocess.run([sys.executable, str(script), *args, "--config", str(self.config_path)],
                              capture_output=True, text=True, env=env)
        self.assertEqual(proc.returncode, expect, proc.stdout + proc.stderr)
        return json.loads(proc.stdout)

    # --- muse.db stand-in ---

    def add_tool(self, task, seq, at, tool="bash", result="ok", success=1):
        with sqlite3.connect(self.db) as conn:
            conn.execute("INSERT INTO tools VALUES (?,?,?,?,?,?,?,?)",
                         (task, seq, tool, json.dumps({"cmd": f"echo {seq}"}), result, iso(at),
                          iso(at + timedelta(seconds=5)), success))

    def add_model(self, task, message, at, tokens_in=100, tokens_out=10):
        with sqlite3.connect(self.db) as conn:
            conn.execute("INSERT INTO models VALUES (?,?,?,?,?,?)", (task, message, iso(at), tokens_in, tokens_out, "claude"))

    def add_task(self, task, at, request, context=None):
        with sqlite3.connect(self.db) as conn:
            conn.execute("INSERT INTO tasks VALUES (?,?,?,?,?)", (task, iso(at), request, context, "main"))

    def run_tool_queries(self, plan):
        """What the worker does with the muse.db tool: run the SQL as given, save the rows."""
        conn = sqlite3.connect(":memory:")
        conn.execute("ATTACH DATABASE ? AS agent", (str(self.db),))
        for query in plan["queries"]:
            self.assertNotRegex(query["sql"], r":(since|until|limit)", "plan must inline every parameter")
            cursor = conn.execute(query["sql"])
            names = [c[0] for c in cursor.description]
            Path(query["saveTo"]).write_text(json.dumps([dict(zip(names, r)) for r in cursor.fetchall()]))
        conn.close()

    def wake(self):
        """One tool-mode worker wake: plan, query, export until the run is finished."""
        results = []
        while True:
            plan = self.cli("plan")
            if not plan["due"]:
                return results
            self.run_tool_queries(plan)
            results.append(self.cli("export"))
            if not results[-1]["more"]:
                return results

    def state_file(self):
        return next((self.tmp / "state").glob("*/export.json"))

    def state(self):
        return json.loads(self.state_file().read_text())

    def set_state(self, state):
        self.state_file().write_text(json.dumps(state))


class Export(Base):
    def test_full_cycle_and_event_mapping(self):
        t0 = self.now - timedelta(hours=2)
        self.add_task("t1", t0, "summarize")
        self.add_tool("t1", 0, t0 + timedelta(minutes=1), result="Authorization: Bearer abcdefghijklmnopqrstu")
        self.add_tool("t1", 1, t0 + timedelta(minutes=2), result="x" * 9000)
        self.add_tool("t1", 2, t0 + timedelta(minutes=3), result="boom", success=0)
        self.add_model("t1", "m1", t0 + timedelta(minutes=4), 1200, 80)
        [result] = self.wake()
        self.assertEqual((result["spans"], result["sentSpans"], result["more"]), (5, 5, False))
        self.assertEqual(self.bt.inserts[0]["headers"]["authorization"], "Bearer test-key")

        [root] = self.bt.of_type("task")
        trace = hashlib.sha256(b"dot:t1").hexdigest()[:32]
        self.assertEqual((root["root_span_id"], root["span_parents"], root["input"]), (trace, [], "summarize"))
        self.assertTrue(all(r["metadata"]["source"] == "muse.db" for r in self.bt.rows.values()))
        self.assertEqual(root["id"], f"{trace}-{root['span_id']}")
        [llm] = self.bt.of_type("llm")
        self.assertEqual((llm["metrics"]["prompt_tokens"], llm["metrics"]["tokens"]), (1200, 1280))
        self.assertEqual((llm["metadata"]["model"], llm["span_parents"]), ("claude", [root["span_id"]]))
        tools = sorted(self.bt.of_type("tool"), key=lambda r: r["metadata"]["seq"])
        self.assertIn("[redacted]", tools[0]["output"])
        self.assertNotIn("abcdefghijklmnopqrstu", json.dumps(self.bt.inserts))
        self.assertIn("[truncated 1000 chars]", tools[1]["output"])
        self.assertEqual((tools[2]["error"], tools[2]["metadata"]["success"]), ("boom", False))
        self.assertAlmostEqual(tools[0]["metrics"]["start"], (t0 + timedelta(minutes=1)).timestamp())

        status = self.cli("status")
        self.assertIsNone(status["runInProgress"])
        self.assertIsNotNone(status["watermark"])
        self.assertEqual(list((self.tmp / "state").glob("*/rows/*/*.json")), [], "rows are deleted after export")

    def test_first_run_sends_the_past_week_in_one_wake(self):
        # 23 tool calls across a year with pages of 4.
        for i in range(23):
            self.add_tool(f"t{i % 5}", i, self.now - timedelta(days=330) + timedelta(days=15 * i))
        self.add_tool("t9", 99, self.now - timedelta(days=6, hours=23))
        self.write_config(maxRows=4)
        self.wake()
        recent = sorted(r["metadata"]["seq"] for r in self.bt.of_type("tool"))
        self.assertEqual(recent, [22, 99], "only the past 7 days on the first run")
        status = self.cli("status")
        self.assertAlmostEqual(datetime.fromisoformat(status["historyFrom"]).timestamp(),
                               (self.now - timedelta(days=7)).timestamp(), delta=120)

    def test_backfill_sends_older_history_without_moving_the_watermark(self):
        for i in range(23):
            self.add_tool(f"t{i % 5}", i, self.now - timedelta(days=330) + timedelta(days=15 * i))
        self.write_config(maxRows=4)
        self.wake()
        watermark = self.state()["watermark"]
        queued = self.cli("backfill", "--days", "400")
        self.assertTrue(queued["due"])
        self.assertEqual(self.cli("backfill", "--days", "400", expect=3)["code"], "BUSY")
        results = self.wake()
        self.assertGreater(len(results), 3)
        self.assertTrue(all(r["backfill"] for r in results))
        self.assertEqual(sorted(r["metadata"]["seq"] for r in self.bt.of_type("tool")), list(range(23)))
        children = [e for e in self.bt.sent() if e["span_attributes"]["type"] != "task"]
        self.assertEqual(len(children), len({e["id"] for e in children}), "each call is sent once")
        state = self.state()
        self.assertEqual(state["watermark"], watermark, "the hourly export's place is unchanged")
        self.assertIn("lastBackfill", state)
        self.assertFalse(self.cli("backfill", "--days", "400")["due"], "already sent")

    def test_subagent_root_takes_its_input_from_its_preamble(self):
        t0 = self.now - timedelta(hours=2)
        preamble = "[Subagent Context] You are running as a subagent (depth 1/2). Task: list open PRs."
        self.add_task("person", t0, "summarize", context="[Subagent Context] unused")
        self.add_task("sub", t0 + timedelta(minutes=1), None, context=preamble)
        self.add_task("bare", t0 + timedelta(minutes=2), None)
        self.add_tool("sub", 0, t0 + timedelta(minutes=1, seconds=5))
        self.wake()
        roots = {r["metadata"]["task_id"]: r for r in self.bt.of_type("task")}
        self.assertEqual((roots["person"]["input"], roots["person"]["metadata"]["request_source"]), ("summarize", "user"))
        self.assertEqual((roots["sub"]["input"], roots["sub"]["metadata"]["request_source"]), (preamble, "context"))
        self.assertEqual((roots["bare"].get("input"), roots["bare"]["metadata"].get("request_source")), (None, None))

    def test_resending_tasks_fixes_roots_already_sent_without_resending_calls(self):
        t0 = self.now - timedelta(days=3)
        for i in range(7):
            self.add_task(f"sub{i}", t0 + timedelta(hours=i), None, context=f"[Subagent Context] job {i}")
            self.add_tool(f"sub{i}", i, t0 + timedelta(hours=i, minutes=1))
        self.write_config(queries={"tool_calls": TOOL_SQL, "model_calls": MODEL_SQL, "tasks": OLD_TASK_SQL})
        self.wake()
        self.assertTrue(all(r.get("input") is None for r in self.bt.of_type("task")), "the old query has no preamble")
        watermark, sent_before = self.state()["watermark"], len(self.bt.sent())

        # Setup version 2: the rewritten query, then a tasks-only resend.
        self.write_config(maxRows=5, queries={"tool_calls": TOOL_SQL, "model_calls": MODEL_SQL, "tasks": TASK_SQL})
        self.assertEqual(self.cli("backfill", "--resend", "--only", "tasks", "--days", "7", expect=6)["code"], "SETUP_ERROR",
                         "a tasks query without LIMIT :limit cannot be paged on its own")
        self.write_config(maxRows=5, queries={"tool_calls": TOOL_SQL, "model_calls": MODEL_SQL, "tasks": LIMITED_TASK_SQL})
        self.assertFalse(self.cli("backfill", "--days", "7")["due"], "without --resend, sent history is skipped")
        queued = self.cli("backfill", "--resend", "--only", "tasks", "--days", "7")
        self.assertEqual(queued["only"], ["tasks"])
        results = self.wake()
        self.assertEqual([r["pageSpans"] for r in results], [4, 3], "paged on the tasks query alone (pages of 5 rows)")
        resent = self.bt.sent()[sent_before:]
        self.assertEqual({e["span_attributes"]["type"] for e in resent}, {"task"}, "only roots are re-sent")
        roots = {r["metadata"]["task_id"]: r for r in self.bt.of_type("task")}
        self.assertEqual(len(roots), 7, "replaced in place, one row per task")
        self.assertEqual(roots["sub4"]["input"], "[Subagent Context] job 4")
        tool = next(r for r in self.bt.of_type("tool") if r["metadata"]["task_id"] == "sub4")
        self.assertLessEqual(roots["sub4"]["metrics"]["start"], tool["metrics"]["start"])
        self.assertGreaterEqual(roots["sub4"]["metrics"]["end"], tool["metrics"]["end"], "root bounds still cover the calls")
        state = self.state()
        self.assertEqual((state["watermark"], state["run"]), (watermark, None))

    def test_page_cut_keeps_rows_that_share_the_boundary_timestamp(self):
        base = self.now - timedelta(hours=3)
        minutes = [0, 1, 2, 3, 3, 3, 4, 5]  # three rows share minute 3, which straddles a page of 4
        for seq, minute in enumerate(minutes):
            self.add_tool("t1", seq, base + timedelta(minutes=minute))
        self.write_config(maxRows=4)
        self.wake()
        self.assertEqual(sorted(r["metadata"]["seq"] for r in self.bt.of_type("tool")), list(range(len(minutes))))

    def test_tool_mode_pages_stay_near_the_byte_budget(self):
        for seq in range(120):
            self.add_tool("t1", seq, self.now - timedelta(days=5) + timedelta(minutes=seq), result="r" * 900)
        self.write_config(maxPageBytes=20_000)
        results = self.wake()
        self.assertEqual(len(self.bt.of_type("tool")), 120)
        first, rest = results[0], results[1:-1]
        self.assertGreater(first["pageBytes"], 20_000, "the first page (50 rows) is over budget")
        self.assertTrue(rest and all(r["pageBytes"] <= 20_000 for r in rest), "later pages fit the budget")
        self.assertLess(first["nextPageRows"], 50)

    def test_small_pages_grow_when_rows_share_a_timestamp(self):
        at = self.now - timedelta(hours=1)
        for seq in range(8):
            self.add_tool("t1", seq, at)
        self.add_tool("t1", 8, at + timedelta(minutes=1))
        self.write_config(startAt=iso(at))  # the first page starts on the crowded timestamp
        state_dir = self.tmp / "state"
        self.cli("status")
        key = hashlib.sha256(str(self.config_path.resolve()).encode()).hexdigest()[:16]
        (state_dir / f"braintrust-telemetry-{key}").mkdir(parents=True, exist_ok=True)
        (state_dir / f"braintrust-telemetry-{key}" / "export.json").write_text(json.dumps({"pageLimit": 5}))
        self.run_tool_queries(self.cli("plan"))
        replan = self.cli("export", expect=3)
        self.assertEqual((replan["code"], replan["nextPageRows"]), ("REPLAN", 20))
        self.assertNotIn("shouldNotify", replan)
        self.wake()
        self.assertEqual(len(self.bt.of_type("tool")), 9)

    def test_too_many_rows_at_one_timestamp_is_reported(self):
        at = self.now - timedelta(hours=1)
        for seq in range(5):
            self.add_tool("t1", seq, at)
        self.write_config(maxRows=3)
        self.run_tool_queries(self.cli("plan"))
        self.assertTrue(self.cli("export")["more"], "the first page moves the cursor up to the crowded timestamp")
        self.run_tool_queries(self.cli("plan"))
        result = self.cli("export", expect=2)
        self.assertEqual((result["code"], result["shouldNotify"]), ("PAGE_STUCK", True))
        self.assertEqual(self.bt.inserts, [])

    def test_rows_outside_the_window_are_rejected(self):
        self.add_tool("t1", 0, self.now - timedelta(hours=1))
        plan = self.cli("plan")
        self.run_tool_queries(plan)
        saved = Path(next(q["saveTo"] for q in plan["queries"] if q["name"] == "tool_calls"))
        rows = json.loads(saved.read_text())
        rows[0]["started_at"] = iso(self.now + timedelta(days=1))
        saved.write_text(json.dumps(rows))
        self.assertEqual(self.cli("export", expect=2)["code"], "QUERY_MISMATCH")

    def test_missing_rows_is_an_error_not_an_empty_export(self):
        plan = self.cli("plan")
        Path(next(q["saveTo"] for q in plan["queries"] if q["name"] == "tool_calls")).write_text("[]")
        result = self.cli("export", expect=2)
        self.assertEqual((result["code"], sorted(result["missing"])), ("MISSING_ROWS", ["model_calls", "tasks"]))
        self.assertIsNotNone(self.cli("status")["runInProgress"])

    def test_export_requires_plan(self):
        self.assertEqual(self.cli("export", expect=2)["code"], "NO_PLAN")

    def test_unsafe_queries_are_refused(self):
        self.write_config(queries={"tool_calls": "DELETE FROM x LIMIT :limit", "model_calls": MODEL_SQL})
        self.assertEqual(self.cli("plan", expect=6)["code"], "SETUP_ERROR")
        self.write_config(queries={"tool_calls": "SELECT 1", "model_calls": MODEL_SQL})
        self.assertIn("LIMIT :limit", self.cli("plan", expect=6)["message"])


class Duplicates(Base):
    def test_overlap_catches_late_rows_without_resending_unchanged_ones(self):
        self.add_tool("t1", 0, self.now - timedelta(hours=1))
        self.add_tool("t1", 1, self.now - timedelta(minutes=10))
        self.wake()
        first = len(self.bt.sent())
        # A long tool call that started before the last export is only written now.
        self.add_tool("t1", 2, self.now - timedelta(minutes=20))
        state = self.state()
        state["watermark"] += 60_000  # the next run must still reach back past it
        self.set_state(state)
        self.wake()
        late = [e for e in self.bt.sent()[first:] if e["span_attributes"]["type"] == "tool"]
        self.assertEqual([e["metadata"]["seq"] for e in late], [2], "the late row is sent; unchanged seq 1 is not")
        self.assertEqual(sorted(r["metadata"]["seq"] for r in self.bt.of_type("tool")), [0, 1, 2])

    def test_resending_everything_creates_no_duplicates(self):
        t0 = self.now - timedelta(hours=2)
        self.add_task("t1", t0, "go")
        for seq in range(4):
            self.add_tool("t1", seq, t0 + timedelta(minutes=seq))
        self.add_model("t1", "m1", t0 + timedelta(minutes=5))
        self.wake()
        strip = lambda rows: {k: {f: v for f, v in r.items() if f != "_xact_id"} for k, r in rows.items()}
        before = strip(self.bt.rows)
        # Lose all local state (reinstall, wiped state dir) and export again from scratch.
        self.state_file().unlink()
        self.wake()
        self.assertEqual(len(self.bt.sent()), 2 * len(before), "everything really was sent twice")
        self.assertEqual(strip(self.bt.rows), before, "same ids and content: still one row per record")

    def test_task_across_runs_keeps_one_root_row(self):
        t0 = self.now - timedelta(hours=3)
        self.add_task("t1", t0, "summarize")
        self.add_tool("t1", 0, t0 + timedelta(minutes=1))
        self.wake()
        self.add_tool("t1", 1, self.now - timedelta(minutes=1))
        self.wake()
        roots = [e for e in self.bt.sent() if e["span_attributes"]["type"] == "task"]
        self.assertEqual((len(roots), len({e["id"] for e in roots})), (2, 1), "the root is replaced, not added")
        [stored] = self.bt.of_type("task")
        self.assertGreater(stored["metrics"]["end"], roots[0]["metrics"]["end"])
        self.assertEqual(stored["metrics"]["start"], roots[0]["metrics"]["start"])
        self.assertEqual(stored["input"], "summarize", "request text survives from the ledger")

    def test_retry_after_partial_send_skips_delivered_requests(self):
        t0 = self.now - timedelta(hours=1)
        for seq in range(6):
            self.add_tool("t1", seq, t0 + timedelta(minutes=seq), result="y" * 400)
        self.write_config(maxPayloadBytes=1500)
        self.run_tool_queries(self.cli("plan"))
        dry = self.cli("export", "--dry-run", "--output", str(self.tmp / "preview.json"))
        self.assertGreater(dry["requests"], 2)
        self.assertEqual(len(json.loads((self.tmp / "preview.json").read_text())["events"]), 7)
        self.assertEqual(self.bt.inserts, [], "a dry run sends nothing")
        self.bt.responses = [200, 400]
        self.assertEqual(self.cli("export", expect=5)["code"], "SEND_FAILED")
        self.assertGreater(len(self.bt.sent()), 0)
        self.assertTrue(self.cli("export")["ok"])
        self.assertEqual(len(self.bt.sent()), 7, "the retry sent only what had not landed")

    def test_unauthorized_notifies_once_then_recovers(self):
        self.add_tool("t1", 0, self.now - timedelta(hours=1))
        self.bt.responses = [401, 401]
        plan = self.cli("plan")
        self.run_tool_queries(plan)
        first = self.cli("export", expect=4)
        self.assertEqual((first["code"], first["shouldNotify"]), ("UNAUTHORIZED", True))
        second = self.cli("export", expect=4)
        self.assertEqual((second["shouldNotify"], second["failureCount"]), (False, 2))
        self.assertEqual(self.cli("plan")["window"], plan["window"], "a failed page is retried unchanged")
        self.assertTrue(self.cli("export")["ok"])
        self.assertIsNone(self.cli("status")["failure"])

    def test_rate_limit_is_retried(self):
        self.add_tool("t1", 0, self.now - timedelta(hours=1))
        self.bt.responses = [429]
        self.assertTrue(self.wake()[-1]["ok"])
        self.assertEqual(len(self.bt.rows), 2)


class DirectMode(Base):
    def test_one_command_drains_the_whole_run(self):
        for i in range(10):
            self.add_tool("t1", i, self.now - timedelta(days=5) + timedelta(hours=i))
        self.write_config(databases={"agent": str(self.db)}, maxRows=3)
        self.assertEqual(self.cli("plan")["mode"], "direct")
        result = self.cli("export")
        self.assertFalse(result["more"])
        self.assertGreater(result["pages"], 3)
        self.assertEqual(len(self.bt.of_type("tool")), 10)
        before, inserts = self.db.read_bytes(), len(self.bt.inserts)
        again = self.cli("export")  # re-reads the overlap; nothing is new
        self.assertEqual((again["pageSent"], len(self.bt.inserts)), (0, inserts))
        self.assertEqual(self.db.read_bytes(), before, "direct mode opens databases read-only")


class Verify(Base):
    def exported(self):
        t0 = self.now - timedelta(hours=3)
        self.add_task("t1", t0, "summarize")
        for seq in range(3):
            self.add_tool("t1", seq, t0 + timedelta(minutes=seq + 1), result=f"out {seq}")
        self.add_model("t1", "m1", t0 + timedelta(minutes=5), 300, 20)
        self.add_tool("t2", 0, t0 + timedelta(minutes=30), result="braintrust-canary-7f3a")
        self.wake()
        return t0

    def reconcile(self, since, until, expect=0):
        first = self.cli("reconcile", "--since", iso(since), "--until", iso(until))
        if first.get("mode") != "tool":
            return first
        self.run_tool_queries(first)
        return self.cli("reconcile", "--since", iso(since), "--until", iso(until), expect=expect)

    def test_clean_export_reconciles(self):
        t0 = self.exported()
        result = self.reconcile(t0 - timedelta(minutes=1), self.now)
        self.assertTrue(result["ok"], result)
        self.assertEqual((result["expected"]["tool"], result["expected"]["llm"], result["traces"]), (4, 1, 2))
        self.assertEqual((result["expected"]["promptTokens"], result["stored"]["promptTokens"]), (300, 300))
        self.assertEqual(list((self.tmp / "state").glob("*/reconcile/*/*.json")), [], "saved rows are deleted")

    def test_reconcile_reports_every_kind_of_problem(self):
        t0 = self.exported()
        tools = sorted(self.bt.of_type("tool"), key=lambda r: (r["metadata"]["task_id"], r["metadata"]["seq"]))
        del self.bt.rows[tools[0]["id"]]                                   # missing
        self.bt.rows[tools[1]["id"]]["output"] = "changed in transit"      # mismatched
        dup = {**tools[2], "id": "foreign-row-id"}                         # same span, second row
        self.bt.rows[dup["id"]] = dup
        result = self.reconcile(t0 - timedelta(minutes=1), self.now)
        self.assertFalse(result["ok"])
        self.assertEqual((result["missing"], result["mismatched"], result["duplicates"], result["extra"]), (1, 1, 1, 1))
        self.assertEqual(result["samples"]["mismatched"][tools[1]["id"]], ["output"])
        self.assertEqual(result["samples"]["missing"], [tools[0]["id"]])

    def test_reconcile_is_clipped_to_the_last_export(self):
        t0 = self.exported()
        self.add_tool("t3", 0, datetime.now(timezone.utc) + timedelta(seconds=30))  # after the last export
        result = self.reconcile(t0 - timedelta(minutes=1), self.now + timedelta(hours=1))
        self.assertTrue(result["ok"] and result["clippedToWatermark"], result)

    def test_reconcile_in_direct_mode(self):
        self.write_config(databases={"agent": str(self.db)})
        t0 = self.now - timedelta(hours=2)
        for seq in range(3):
            self.add_tool("t1", seq, t0 + timedelta(minutes=seq))
        self.cli("export")
        self.assertTrue(self.reconcile(t0 - timedelta(minutes=1), self.now)["ok"])

    def test_find_locates_a_marker_with_its_lag(self):
        self.exported()
        result = self.cli("find", "--marker", "braintrust-canary-7f3a")
        self.assertEqual(result["found"], 1)
        self.assertEqual(result["rows"][0]["type"], "tool")
        self.assertGreater(result["rows"][0]["lagMinutes"], 0)
        self.assertEqual(self.cli("find", "--marker", "not-there-at-all")["found"], 0)

    def test_probe_checks_replace_and_cleans_up(self):
        self.bt.hidden_reads = 2  # Braintrust shows nothing for the first two reads
        result = self.cli("probe", "--project", SCRATCH)
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(self.bt.rows), 0, "probe rows are deleted")
        self.assertEqual(self.cli("probe", "--project", PROJECT, expect=2)["code"], "INVALID_INPUT")


class Check(Base):
    def test_check_reads_the_project(self):
        self.assertEqual(self.cli("check")["projectName"], "Test")
        self.write_config(projectId="missing-project")
        self.assertEqual(self.cli("check", expect=6)["code"], "PROJECT_NOT_FOUND")


class Hook(Base):
    def test_timer(self):
        dry = self.cli("hook", "--dry-run")
        self.assertEqual((dry["decision"], dry["payload"]["reasons"]), ("wake", ["setup"]))
        first = self.cli("hook")
        self.assertEqual(first["decision"], "wake")
        self.assertEqual(first["payload"]["config"], str(self.config_path.resolve()))
        self.assertIsNone(first["payload"]["skillSha256"], "unbuilt skill has no manifest")
        self.assertEqual(self.cli("hook")["decision"], "silent")

    def test_finished_export_sets_next_slot(self):
        self.add_tool("t1", 0, self.now - timedelta(hours=1))
        self.wake()
        self.assertEqual(self.cli("hook")["reason"], "export not due", "the setup export counts as the last run")
        state = self.state()
        state["lastExport"]["at"] -= 61 * 60_000
        self.set_state(state)
        self.assertEqual(self.cli("hook")["decision"], "wake")

    def test_unfinished_run_is_resumed_after_its_lease(self):
        for i in range(6):
            self.add_tool("t1", i, self.now - timedelta(days=6 - i))
        self.write_config(maxRows=2)
        self.run_tool_queries(self.cli("plan"))
        self.assertTrue(self.cli("export")["more"])  # the worker stops after one page
        self.assertEqual(self.cli("hook")["reason"], "export not due", "a worker holds the run")
        state = self.state()
        state["run"]["touchedAt"] -= 16 * 60_000
        self.set_state(state)
        self.assertEqual(self.cli("hook")["decision"], "wake", "an abandoned run is resumed")
        self.wake()
        self.assertEqual(len(self.bt.of_type("tool")), 6)

    def test_hook_skips_while_export_runs(self):
        key = hashlib.sha256(str(self.config_path.resolve()).encode()).hexdigest()[:16]
        lock_dir = self.tmp / "state" / f"braintrust-telemetry-{key}"
        lock_dir.mkdir(parents=True)
        with (lock_dir / "export.lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            self.assertEqual(self.cli("hook")["reason"], "export running")

    def test_shell_script_with_runtime(self):
        runtime = self.tmp / "runtime.sh"
        runtime.write_text('wake() { echo "WAKE|$1|$2"; exit 0; }\nsilent() { echo "SILENT|$1"; exit 0; }\nlog() { echo "LOG|$1" >&2; }\n')
        env = {**os.environ, "HATCH_HOOK_RUNTIME": str(runtime), "BRAINTRUST_TELEMETRY_STATE_DIR": str(self.tmp / "state")}
        script = SKILL_DIR / "assets" / "export-hook.sh"
        out = [subprocess.run(["bash", str(script), str(self.config_path)], capture_output=True, text=True, env=env).stdout
               for _ in range(2)]
        kind, reason, payload = out[0].strip().split("|", 2)
        self.assertEqual((kind, reason), ("WAKE", "telemetry export due"))
        self.assertIn("attemptId", json.loads(payload))
        self.assertEqual(out[1].strip(), "SILENT|export not due")
        broken = subprocess.run(["bash", str(script), str(self.tmp / "missing.json")], capture_output=True, text=True, env=env)
        self.assertEqual((broken.returncode, broken.stdout.strip()), (0, "SILENT|export check failed; retry on the next invocation"))


class Release(Base):
    def install(self, release):
        """Extract a built release the way setup.md does; returns the skill's CLI path."""
        archive = (release / "skills" / "braintrust-telemetry.zip").read_bytes()
        target = self.tmp / "install"
        with zipfile.ZipFile(io.BytesIO(archive)) as z:
            self.assertEqual(sorted(z.namelist()), sorted("braintrust-telemetry/" + f for f in build.FILES))
            z.extractall(target)
        (target / "braintrust-telemetry/assets/export-hook.sh").chmod(0o755)
        return target / "braintrust-telemetry" / "scripts" / "braintrust_telemetry.py"

    def newer_source(self, note):
        source = self.tmp / "source-v2"
        if not source.exists():
            import shutil
            shutil.copytree(SKILL_DIR, source)
        with (source / "SKILL.md").open("a") as f:
            f.write(f"\n{note}\n")
        return source

    def test_build_is_reproducible_and_content_addressed(self):
        a = build.build(self.bt.url, self.tmp / "dist")
        b = build.build(self.bt.url, self.tmp / "dist2")
        self.assertEqual(a["sha256"], b["sha256"])
        self.assertEqual(a["version"], (ROOT / "VERSION").read_text().strip())
        self.assertTrue(a["archiveUrl"].endswith(f"/skills/braintrust-telemetry-{a['version']}-{a['sha256']}.zip"))
        versioned = (self.tmp / "dist" / "skills" / f"braintrust-telemetry-{a['version']}-{a['sha256']}.zip").read_bytes()
        self.assertEqual(hashlib.sha256(versioned).hexdigest(), a["sha256"])
        self.assertIn(self.bt.url, (self.tmp / "dist/skills/braintrust-telemetry/setup.md").read_text())

    def test_released_version_matches_the_sources(self):
        """Fails when sources change without a VERSION bump and `build.py --release`."""
        version = build.current_version()
        self.assertEqual(build.releases()[version]["digest"], build.source_digest())

    def test_build_refuses_changed_sources_under_an_old_version(self):
        with self.assertRaises(SystemExit) as raised:
            build.build(self.bt.url, self.tmp / "dist", source=self.newer_source("unreleased change"))
        self.assertIn("Bump VERSION", str(raised.exception))

    def test_installed_version_is_printed_offline(self):
        build.build(self.bt.url, self.tmp / "v1")
        cli = self.install(self.tmp / "v1")
        proc = subprocess.run([sys.executable, str(cli), "--version"], capture_output=True, text=True)
        self.assertEqual(proc.stdout.strip(), build.current_version())

    def test_setup_check_reads_a_fresh_manifest_and_checks_installed_bytes(self):
        self.bt.static = self.tmp / "dist"
        release = build.build(self.bt.url, self.bt.static)
        cli = self.install(self.bt.static)
        self.assertNotIn("{{", cli.read_text())
        self.assertEqual(self.cli("setup-check", script=cli)["sha256"], release["sha256"])
        manifest_gets = [g for g in self.bt.gets if "manifest.json" in g["path"]]
        self.assertTrue(manifest_gets and all("fresh=" in g["path"] and g["headers"].get("cache-control") == "no-cache"
                                              for g in manifest_gets), "every manifest read bypasses caches")
        with (cli.parents[1] / "SKILL.md").open("a") as f:
            f.write("\nlocal edit\n")
        stale = self.cli("setup-check", script=cli, expect=2)
        self.assertEqual((stale["code"], stale["changedFiles"]), ("SKILL_UPDATE_REQUIRED", ["SKILL.md"]))

    def test_self_update_installs_the_latest_release_in_place(self):
        v1 = build.build(self.bt.url, self.tmp / "v1")
        cli = self.install(self.tmp / "v1")
        skill = cli.parents[1]
        (skill / "notes.txt").write_text("keep me")
        self.cli("setup-check", script=cli, expect=5)  # nothing served yet
        self.bt.static = self.tmp / "served"
        v2 = build.build(self.bt.url, self.bt.static, source=self.newer_source("release two"), version="9.2.0")
        self.assertNotEqual(v1["sha256"], v2["sha256"])

        hook = self.cli("hook", script=cli)["payload"]
        self.assertEqual((hook["skillSha256"], hook["updateRequired"]), (v2["sha256"], True))
        self.assertEqual((hook["installedVersion"], hook["latestVersion"]), (v1["version"], "9.2.0"))
        info = self.cli("version", script=cli)
        self.assertEqual((info["installed"], info["latest"], info["upToDate"]), (v1["version"], "9.2.0", False))

        result = self.cli("self-update", script=cli)
        self.assertEqual((result["updated"], result["changedFiles"], result["sha256"]), (True, ["SKILL.md", "scripts/braintrust_telemetry.py"], v2["sha256"]),
                         "the version is stamped into the CLI too")
        self.assertEqual((result["fromVersion"], result["version"]), (v1["version"], "9.2.0"))
        info = self.cli("version", script=cli)
        self.assertEqual((info["installed"], info["latest"], info["upToDate"]), ("9.2.0", "9.2.0", True))
        self.assertIn("version: 9.2.0", (skill / "SKILL.md").read_text())
        self.assertIn("release two", (skill / "SKILL.md").read_text())
        self.assertEqual((skill / "notes.txt").read_text(), "keep me", "other files in the folder are kept")
        self.assertTrue(os.access(skill / "assets/export-hook.sh", os.X_OK))
        saved = json.loads(self.config_path.read_text())["setupReceipt"]
        self.assertEqual((saved["sha256"], saved["version"]), (v2["sha256"], "9.2.0"))
        self.assertEqual(self.cli("setup-check", script=cli)["sha256"], v2["sha256"])
        self.assertFalse(self.cli("self-update", script=cli)["updated"])

        # A third release: the updated CLI keeps following the latest.
        self.assertFalse(self.cli("self-update", script=cli)["updated"])
        # A third release that changes setup: the updated install is told to redo it.
        with mock.patch.object(build, "SETUP_VERSION", build.SETUP_VERSION + 1):
            v3 = build.build(self.bt.url, self.bt.static, source=self.newer_source("release three"), version="9.3.0")
        result = self.cli("self-update", script=cli)
        self.assertEqual((result["version"], result["setupRequired"]), ("9.3.0", True))
        self.assertEqual(self.cli("status", script=cli)["version"], "9.3.0")
        hook = self.cli("hook", "--dry-run", script=cli)
        self.assertFalse(hook["payload"]["updateRequired"])

    def test_setup_change_is_reported_until_setup_done(self):
        build.build(self.bt.url, self.tmp / "v1")
        cli = self.install(self.tmp / "v1")
        self.bt.static = self.tmp / "served"
        build.build(self.bt.url, self.bt.static)
        # A config made by 0.0.3 or earlier has no setupVersion: it was set up with version 1.
        self.assertTrue(self.cli("status", script=cli)["setupRequired"])
        self.assertTrue(self.cli("hook", "--dry-run", script=cli)["payload"]["setupRequired"])
        self.assertTrue(self.cli("self-update", script=cli)["setupRequired"])
        self.assertTrue(self.cli("self-update", script=cli)["setupRequired"], "a repeated self-update keeps it")
        self.assertTrue(self.cli("setup-check", script=cli)["setupRequired"])
        done = self.cli("setup-done", script=cli)
        self.assertEqual((done["from"], done["setupVersion"]), (1, build.SETUP_VERSION))
        self.assertFalse(self.cli("status", script=cli)["setupRequired"])
        self.assertFalse(self.cli("hook", "--dry-run", script=cli)["payload"]["setupRequired"])
        self.assertFalse(self.cli("self-update", script=cli)["setupRequired"])

    def test_self_update_refuses_a_tampered_archive(self):
        build.build(self.bt.url, self.tmp / "v1")
        cli = self.install(self.tmp / "v1")
        before = {p: p.read_bytes() for p in cli.parents[1].rglob("*") if p.is_file()}
        self.bt.static = self.tmp / "served"
        v2 = build.build(self.bt.url, self.bt.static, source=self.newer_source("release two"), version="9.2.0")
        archive = self.bt.static / "skills" / f"braintrust-telemetry-9.2.0-{v2['sha256']}.zip"
        data = bytearray(archive.read_bytes())
        data[len(data) // 2] ^= 0xFF  # corrupt one byte in transit
        archive.write_bytes(bytes(data))
        first = self.cli("self-update", script=cli, expect=5)
        self.assertEqual((first["code"], first["shouldNotify"]), ("UPDATE_FAILED", True))
        self.assertFalse(self.cli("self-update", script=cli, expect=5).get("shouldNotify"), "reported once")
        after = {p: p.read_bytes() for p in cli.parents[1].rglob("*") if p.is_file()}
        self.assertEqual(after, before, "a failed update changes nothing")

    def test_server_caches_only_the_content_addressed_archive(self):
        import socket
        import time
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        server = subprocess.Popen([sys.executable, str(ROOT / "serve.py")], cwd=ROOT, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, env={**os.environ, "PUBLIC_BASE_URL": url, "PORT": str(port)})
        try:
            server.stdout.readline()
            import urllib.request
            for _ in range(50):
                try:
                    manifest = json.loads(urllib.request.urlopen(url + "/skills/braintrust-telemetry/manifest.json").read())
                    break
                except OSError:
                    time.sleep(0.1)
            def cache(path):
                with urllib.request.urlopen(url + path) as response:
                    return response.headers["Cache-Control"]
            self.assertEqual(cache("/skills/braintrust-telemetry/manifest.json"), "no-store")
            self.assertEqual(cache("/skills/braintrust-telemetry.zip"), "no-store")
            self.assertIn("immutable", cache(manifest["archiveUrl"][len(url):]))
        finally:
            server.terminate()
            server.wait()


if __name__ == "__main__":
    unittest.main()

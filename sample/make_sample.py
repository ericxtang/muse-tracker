#!/usr/bin/env python3
"""Build sample/sample_muse.db -- a SQLite sample of muse.db for development.

The tables and columns mirror schema.sql (the PostgreSQL DDL taken from the
real muse.db catalog) exactly: same schema-qualified names (quoted, since
SQLite has no schemas), same column names/order/nullability. Types use
SQLite affinities: bigint -> INTEGER, boolean -> INTEGER (0/1),
timestamp with time zone -> TEXT (ISO-8601 UTC), arrays -> TEXT (JSON).

Seed data is fully synthetic: one agent runs one short task (book a
restaurant table; first attempt fails, second succeeds) so the rows show
how a task arc is laid out across the tables.

Usage: python3 sample/make_sample.py [output-path]
Default output: sample/sample_muse.db next to this script.
"""
import json
import os
import sqlite3
import sys

DDL = """
CREATE TABLE "agent.agents" (
    agent_id TEXT PRIMARY KEY,
    id TEXT UNIQUE,
    session_id TEXT,
    kind TEXT NOT NULL DEFAULT 'root',
    parent_id TEXT,
    parent_agent_id TEXT,
    root_session_id TEXT,
    request_id TEXT,
    model TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    depth INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    last_assistant_message TEXT,
    seen_at_ms INTEGER,
    prompt_floor_seq INTEGER,
    ephemeral INTEGER NOT NULL DEFAULT 0,
    last_assistant_resources TEXT,
    agent_type TEXT,
    presentation_root_session_id TEXT,
    spawn_metadata_json TEXT,
    originating_location_context_json TEXT,
    handoff_context_high_water_seq INTEGER
);
CREATE TABLE "runtime.messages" (
    message_id TEXT PRIMARY KEY,
    event_seq INTEGER NOT NULL UNIQUE,
    role TEXT NOT NULL,
    prompt_rendering_id INTEGER,
    author_label TEXT,
    provider_message_id TEXT,
    created_at TEXT NOT NULL,
    body TEXT
);
CREATE TABLE "runtime.tool_calls" (
    tool_call_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_seq INTEGER NOT NULL UNIQUE,
    call_id TEXT NOT NULL UNIQUE,
    tool_name TEXT NOT NULL,
    server_name TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    arguments_json TEXT
);
CREATE TABLE "runtime.tool_outputs" (
    tool_output_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_seq INTEGER NOT NULL UNIQUE,
    call_id TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    output_text TEXT,
    error_text TEXT
);
CREATE TABLE "agent.context_items" (
    context_item_id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    item_kind TEXT NOT NULL,
    message_id TEXT REFERENCES "runtime.messages" (message_id),
    tool_call_id INTEGER REFERENCES "runtime.tool_calls" (tool_call_id),
    tool_output_id INTEGER REFERENCES "runtime.tool_outputs" (tool_output_id),
    role TEXT,
    call_id TEXT,
    tool_name TEXT,
    success INTEGER,
    message_source TEXT,
    created_at TEXT NOT NULL,
    data_fbids TEXT,
    data_message_ids TEXT,
    data_cleanup_checked_at TEXT,
    data_summarized_at TEXT,
    data_thread_ids TEXT,
    data_expires_at TEXT,
    text_content TEXT,
    item_json TEXT,
    UNIQUE (agent_id, seq)
);
CREATE TABLE "agent.agent_message_token_usage" (
    agent_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (agent_id, message_id)
);
"""

AGENT_ID = "11111111-2222-3333-4444-555555555555"
# Synthetic timeline: 2026-01-05 10:00 UTC onwards.
T = [
    "2026-01-05T10:00:00+00:00",
    "2026-01-05T10:00:09+00:00",
    "2026-01-05T10:00:14+00:00",
    "2026-01-05T10:00:15+00:00",
    "2026-01-05T10:00:21+00:00",
    "2026-01-05T10:00:22+00:00",
    "2026-01-05T10:00:28+00:00",
    "2026-01-05T10:00:29+00:00",
    "2026-01-05T10:00:36+00:00",
    "2026-01-05T10:00:44+00:00",
]
EPOCH0 = 1767607200  # 2026-01-05T10:00:00Z

USER_TEXT = "Book a table for two this Friday at 7 PM near the office."
ASST0_TEXT = "I'll check Friday 7 PM availability near your office."
ASST1_TEXT = "Booked: Friday 7:30 PM at Bistro Verde, confirmation R-12345."


def args_text(d):
    """Real context_items store tool args as a JSON string literal whose
    content is the arguments JSON."""
    return json.dumps(json.dumps(d))


def seed(con):
    con.execute(
        'INSERT INTO "agent.agents" (agent_id, id, session_id, kind, model,'
        " status, depth, created_at, updated_at, last_assistant_message,"
        " ephemeral) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (AGENT_ID, "sample-agent-1", "sample-session-1", "root",
         "sample-model-1", "completed", 0, EPOCH0, EPOCH0 + 44, ASST1_TEXT, 0),
    )

    con.executemany(
        'INSERT INTO "runtime.messages" (message_id, event_seq, role,'
        " created_at, body) VALUES (?,?,?,?,?)",
        [
            ("sample-msg-user-1", 1, "user", T[0], USER_TEXT),
            ("sample-msg-asst-0", 2, "assistant", T[1], ASST0_TEXT),
            ("sample-msg-asst-1", 10, "assistant", T[9], ASST1_TEXT),
        ],
    )

    calls = [
        (3, "sample-call-1", "search_restaurants", "completed", T[2],
         json.dumps({"date": "friday", "time": "19:00", "party_size": 2,
                     "near": "office"})),
        (5, "sample-call-2", "book_table", "failed", T[4],
         json.dumps({"restaurant": "Bistro Verde", "time": "19:00",
                     "party_size": 2})),
        (7, "sample-call-3", "book_table", "completed", T[7],
         json.dumps({"restaurant": "Bistro Verde", "time": "19:30",
                     "party_size": 2})),
    ]
    con.executemany(
        'INSERT INTO "runtime.tool_calls" (event_seq, call_id, tool_name,'
        " status, created_at, arguments_json) VALUES (?,?,?,?,?,?)", calls)

    outputs = [
        (4, "sample-call-1", "completed", T[3],
         json.dumps({"results": [{"name": "Bistro Verde", "times": ["19:30"]}]}),
         None),
        (6, "sample-call-2", "failed", T[5], None,
         "no availability at 19:00"),
        (8, "sample-call-3", "completed", T[8],
         json.dumps({"confirmation": "R-12345", "time": "19:30"}), None),
    ]
    con.executemany(
        'INSERT INTO "runtime.tool_outputs" (event_seq, call_id, status,'
        " created_at, output_text, error_text) VALUES (?,?,?,?,?,?)", outputs)

    # (seq, kind, message_id, tool_call_id, tool_output_id, role, call_id,
    #  tool_name, success, at, text_content)
    items = [
        (1, "message_text", "sample-msg-user-1", None, None, "user", None,
         None, None, T[0], USER_TEXT),
        (2, "message_text", "sample-msg-asst-0", None, None, "assistant",
         None, None, None, T[1], ASST0_TEXT),
        (3, "tool_call", None, 1, None, None, "sample-call-1",
         "search_restaurants", None, T[2],
         args_text({"date": "friday", "time": "19:00", "party_size": 2,
                    "near": "office"})),
        (4, "tool_output", None, None, 1, None, "sample-call-1", None, 1,
         T[3], outputs[0][4]),
        (5, "tool_call", None, 2, None, None, "sample-call-2", "book_table",
         None, T[4],
         args_text({"restaurant": "Bistro Verde", "time": "19:00",
                    "party_size": 2})),
        (6, "tool_output", None, None, 2, None, "sample-call-2", None, 0,
         T[5], outputs[1][5]),
        (7, "text_segments", None, None, None, None, None, None, None, T[6],
         json.dumps({"type": "commentary_text",
                     "text": "7 PM is full; trying 7:30."})),
        (8, "tool_call", None, 3, None, None, "sample-call-3", "book_table",
         None, T[7],
         args_text({"restaurant": "Bistro Verde", "time": "19:30",
                    "party_size": 2})),
        (9, "tool_output", None, None, 3, None, "sample-call-3", None, 1,
         T[8], outputs[2][4]),
        (10, "message_text", "sample-msg-asst-1", None, None, "assistant",
         None, None, None, T[9], ASST1_TEXT),
    ]
    con.executemany(
        'INSERT INTO "agent.context_items" (agent_id, seq, item_kind,'
        " message_id, tool_call_id, tool_output_id, role, call_id, tool_name,"
        " success, message_source, created_at, text_content)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(AGENT_ID, seq, kind, mid, tcid, toid, role, call, tool, ok,
          "sample", at, text)
         for (seq, kind, mid, tcid, toid, role, call, tool, ok, at, text)
         in items],
    )

    con.executemany(
        'INSERT INTO "agent.agent_message_token_usage" (agent_id, message_id,'
        " input_tokens, output_tokens, created_at) VALUES (?,?,?,?,?)",
        [
            (AGENT_ID, "sample-msg-asst-0", 811, 42, EPOCH0 + 9),
            (AGENT_ID, "sample-msg-asst-1", 1523, 96, EPOCH0 + 44),
        ],
    )


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "sample_muse.db")
    if os.path.exists(out):
        os.remove(out)
    con = sqlite3.connect(out)
    try:
        con.executescript(DDL)
        seed(con)
        con.commit()
        counts = {}
        for (name,) in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
                " AND name NOT LIKE 'sqlite_%' ORDER BY name"):
            counts[name] = con.execute(
                f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
    finally:
        con.close()
    print(f"wrote {out}")
    for name, n in counts.items():
        print(f"  {name}: {n} rows")


if __name__ == "__main__":
    main()

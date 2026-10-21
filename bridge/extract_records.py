#!/usr/bin/env python3
"""Extract bridge records from a muse.db-shaped SQLite database.

Reads `agent.context_items` and `agent.agent_message_token_usage` (see
sample/schema.sql) and writes the records.json shape that build_otlp.py
consumes:

    task:        first user message + task time span
    model_calls: one entry per agent_message_token_usage row
    tool_calls:  each tool_call item paired with the tool_output item
                 that follows it

Develop against the synthetic sample:
    python3 sample/make_sample.py
    python3 bridge/extract_records.py --db sample/sample_muse.db --out records.json
    python3 bridge/build_otlp.py

Against real muse.db the same queries run through the muse.db SQL tool;
the extraction logic (pairing, arg unwrapping) is identical.
"""
import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone


def unwrap_args(text):
    """context_items store tool args as a JSON string literal whose content
    is the arguments JSON. Unwrap to a compact JSON string."""
    if text is None:
        return ""
    try:
        inner = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text
    if isinstance(inner, str):
        try:
            return json.dumps(json.loads(inner), separators=(", ", ": "))
        except json.JSONDecodeError:
            return inner
    return json.dumps(inner, separators=(", ", ": "))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="SQLite database path")
    ap.add_argument("--out", default="records.json")
    ap.add_argument("--agent", default=None,
                    help="agent_id (default: the only agent in context_items)")
    ap.add_argument("--surface", default="sample")
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    try:
        agent_id = args.agent
        if agent_id is None:
            found = [r[0] for r in con.execute(
                'SELECT DISTINCT agent_id FROM "agent.context_items"')]
            if len(found) != 1:
                print(f"error: expected exactly one agent, found {found};"
                      " pass --agent", file=sys.stderr)
                return 1
            agent_id = found[0]

        items = con.execute(
            'SELECT * FROM "agent.context_items" WHERE agent_id = ?'
            " ORDER BY seq", (agent_id,)).fetchall()
        if not items:
            print(f"error: no context items for agent {agent_id}",
                  file=sys.stderr)
            return 1

        first_user = next(
            (r for r in items
             if r["item_kind"] == "message_text" and r["role"] == "user"),
            None)
        if first_user is None:
            print("error: no user message_text item found", file=sys.stderr)
            return 1

        task = {
            "agent_id": agent_id,
            "start": first_user["created_at"],
            "end": items[-1]["created_at"],
            "request_text": first_user["text_content"] or "",
            "source": "sample_muse.db (SQLite) agent.context_items +"
                      " agent.agent_message_token_usage",
            "surface": args.surface,
        }

        tool_calls = []
        pending = None
        for r in items:
            if r["item_kind"] == "tool_call":
                pending = r
            elif r["item_kind"] == "tool_output" and pending is not None:
                tool_calls.append({
                    "seq": pending["seq"],
                    "tool": pending["tool_name"],
                    "args": unwrap_args(pending["text_content"]),
                    "result": r["text_content"] or "",
                    "success": bool(r["success"]),
                    "at": pending["created_at"],
                    "end": r["created_at"],
                })
                pending = None
        if pending is not None:
            print(f"warning: tool_call seq {pending['seq']} has no output",
                  file=sys.stderr)

        by_message = {r["message_id"]: r for r in items if r["message_id"]}
        model_calls = []
        for u in con.execute(
                'SELECT * FROM "agent.agent_message_token_usage"'
                " WHERE agent_id = ? ORDER BY created_at", (agent_id,)):
            note = ""
            msg = by_message.get(u["message_id"])
            if msg is not None and msg["text_content"]:
                note = "assistant reply: " + msg["text_content"][:60]
            model_calls.append({
                "at": datetime.fromtimestamp(
                    u["created_at"], tz=timezone.utc).isoformat(),
                "input_tokens": u["input_tokens"],
                "output_tokens": u["output_tokens"],
                "message_id": u["message_id"],
                "note": note,
            })
    finally:
        con.close()

    records = {"task": task, "model_calls": model_calls,
               "tool_calls": tool_calls}
    with open(args.out, "w") as f:
        json.dump(records, f, indent=1)
    print(f"wrote {args.out}: 1 task, {len(model_calls)} model calls,"
          f" {len(tool_calls)} tool calls")
    return 0


if __name__ == "__main__":
    sys.exit(main())

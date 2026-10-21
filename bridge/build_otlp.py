#!/usr/bin/env python3
"""Build an OTLP/HTTP (JSON) trace payload from Muse execution records.

Reads records.json (extracted from muse.db: agent.context_items tool calls
plus agent.agent_message_token_usage rows) and writes otlp.json plus the
trace id to trace_id.txt.
"""
import hashlib
import json
import os
import secrets
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))


def to_nano(ts: str) -> str:
    dt = datetime.fromisoformat(ts.replace("+00:00", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return str(int(dt.timestamp() * 1_000_000_000))


def span_id(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()[:16]


def sval(v: str):
    return {"stringValue": v}


def ival(v: int):
    return {"intValue": str(v)}


def bval(v: bool):
    return {"boolValue": v}


def main():
    with open(os.path.join(HERE, "records.json")) as f:
        rec = json.load(f)

    trace_id = secrets.token_hex(16)
    with open(os.path.join(HERE, "trace_id.txt"), "w") as f:
        f.write(trace_id)

    root_id = span_id(f"{trace_id}:root")
    task = rec["task"]
    spans = []

    spans.append({
        "traceId": trace_id,
        "spanId": root_id,
        "name": "muse.agent_task",
        "kind": 1,
        "startTimeUnixNano": to_nano(task["start"]),
        "endTimeUnixNano": to_nano(task["end"]),
        "attributes": [
            {"key": "braintrust.span_attributes.type", "value": sval("task")},
            {"key": "braintrust.input", "value": sval(task["request_text"])},
            {"key": "agent.id", "value": sval(task["agent_id"])},
            {"key": "chat.surface", "value": sval(task["surface"])},
            {"key": "records.source", "value": sval(task["source"])},
        ],
        "status": {"code": 1},
    })

    for mc in rec["model_calls"]:
        spans.append({
            "traceId": trace_id,
            "spanId": span_id(f"{trace_id}:model:{mc['message_id']}"),
            "parentSpanId": root_id,
            "name": "muse.model_call",
            "kind": 1,
            "startTimeUnixNano": to_nano(mc["at"]),
            "endTimeUnixNano": to_nano(mc["at"]),
            "attributes": [
                {"key": "braintrust.span_attributes.type", "value": sval("llm")},
                {"key": "gen_ai.usage.prompt_tokens", "value": ival(mc["input_tokens"])},
                {"key": "gen_ai.usage.completion_tokens", "value": ival(mc["output_tokens"])},
                {"key": "message.id", "value": sval(mc["message_id"])},
                {"key": "note", "value": sval(mc["note"])},
            ],
            "status": {"code": 1},
        })

    for tc in rec["tool_calls"]:
        attrs = [
            {"key": "braintrust.span_attributes.type", "value": sval("tool")},
            {"key": "gen_ai.tool.name", "value": sval(tc["tool"])},
            {"key": "braintrust.input", "value": sval(tc["args"])},
            {"key": "braintrust.output", "value": sval(tc["result"])},
            {"key": "task.seq", "value": ival(tc["seq"])},
            {"key": "success", "value": bval(bool(tc["success"]))},
        ]
        if not tc["success"]:
            attrs.append({"key": "exception.message", "value": sval(tc["result"])})
        spans.append({
            "traceId": trace_id,
            "spanId": span_id(f"{trace_id}:tool:{tc['seq']}"),
            "parentSpanId": root_id,
            "name": f"muse.tool_call.{tc['tool']}",
            "kind": 1,
            "startTimeUnixNano": to_nano(tc["at"]),
            "endTimeUnixNano": to_nano(tc["end"]),
            "attributes": attrs,
            "status": {"code": 1 if tc["success"] else 2},
        })

    payload = {
        "resourceSpans": [{
            "resource": {"attributes": [
                {"key": "service.name", "value": sval("muse-agent")},
                {"key": "service.namespace", "value": sval("muse")},
            ]},
            "scopeSpans": [{
                "scope": {"name": "muse.braintrust-bridge", "version": "0.1.0"},
                "spans": spans,
            }],
        }]
    }

    out = os.path.join(HERE, "otlp.json")
    with open(out, "w") as f:
        json.dump(payload, f)
    print(f"wrote {out}: {len(spans)} spans, trace_id={trace_id}")


if __name__ == "__main__":
    main()

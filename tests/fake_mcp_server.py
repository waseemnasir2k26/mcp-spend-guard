"""A minimal MCP-shaped stdio server used as a test fixture.

It speaks newline-delimited JSON-RPC 2.0 and needs no network and no API key,
so the whole proxy can be exercised end to end.

Tools:
  echo        -> returns the text it was given
  fail        -> returns an MCP tool error (result.isError = true)
  hard_fail   -> returns a JSON-RPC error object
  slow        -> sleeps `sleep_ms` then echoes
  expensive   -> like echo, exists so cost tables have something to price
"""

from __future__ import annotations

import json
import sys
import time

TOOLS = [
    {"name": "echo", "description": "Echo the given text", "inputSchema": {"type": "object"}},
    {"name": "fail", "description": "Always returns a tool error", "inputSchema": {"type": "object"}},
    {
        "name": "hard_fail",
        "description": "Always returns a JSON-RPC error",
        "inputSchema": {"type": "object"},
    },
    {"name": "slow", "description": "Sleeps then echoes", "inputSchema": {"type": "object"}},
    {"name": "expensive", "description": "A pricey tool", "inputSchema": {"type": "object"}},
]


def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def text_result(text, is_error=False):
    out = {"content": [{"type": "text", "text": text}]}
    if is_error:
        out["isError"] = True
    return out


def handle(msg):
    method = msg.get("method")
    mid = msg.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": mid,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-mcp-echo", "version": "1.0.0"},
            },
        }

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}

    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        if name == "fail":
            return {
                "jsonrpc": "2.0",
                "id": mid,
                "result": text_result("the tool failed on purpose", is_error=True),
            }
        if name == "hard_fail":
            return {
                "jsonrpc": "2.0",
                "id": mid,
                "error": {"code": -32603, "message": "internal fake-server failure"},
            }
        if name == "slow":
            time.sleep(float(args.get("sleep_ms", 100)) / 1000.0)
            return {"jsonrpc": "2.0", "id": mid, "result": text_result(str(args.get("text", "")))}
        if name in ("echo", "expensive"):
            return {"jsonrpc": "2.0", "id": mid, "result": text_result(str(args.get("text", "")))}
        return {
            "jsonrpc": "2.0",
            "id": mid,
            "error": {"code": -32602, "message": f"unknown tool: {name}"},
        }

    if mid is None:
        # A notification. Nothing to answer.
        return None

    return {
        "jsonrpc": "2.0",
        "id": mid,
        "error": {"code": -32601, "message": f"method not found: {method}"},
    }


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        response = handle(msg)
        if response is not None:
            send(response)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""A tiny MCP server over stdio for tests (stands in for an npm/PyPI package; nothing is installed).

Tools: echo(text) → "echo: …" (adds " [token set]" when ECHO_TOKEN is in the environment),
whoami() → the ECHO_MODE environment value.
"""

import json
import os
import sys

TOOLS = [{"name": "echo", "description": "Repeat the text back", "inputSchema": {"type": "object"}},
         {"name": "whoami", "description": "Show the mode", "inputSchema": {"type": "object"}}]

for line in sys.stdin:
    try:
        message = json.loads(line)
    except ValueError:
        continue
    if "id" not in message:
        continue
    method = message.get("method")
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake-stdio"}}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        args = message["params"].get("arguments") or {}
        if message["params"]["name"] == "echo":
            text = f"echo: {args.get('text', args.get('query', ''))}" + (" [token set]" if os.environ.get("ECHO_TOKEN") else "")
        else:
            text = os.environ.get("ECHO_MODE", "")
        result = {"content": [{"type": "text", "text": text}]}
    else:
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "no"}}), flush=True)
        continue
    print("some log line that is not JSON", file=sys.stderr, flush=True)
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)

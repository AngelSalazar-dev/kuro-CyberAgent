#!/usr/bin/env python3
"""Kuro JSON-RPC server for desktop app communication.

Wraps the Kuro agent as a JSON-RPC server that communicates over stdin/stdout
with the Tauri frontend.
"""

import json
import sys
import os
import asyncio
from pathlib import Path

# Add kuro package to path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kuro.agent import Agent
from kuro.config import Config
from kuro.tools import execute as tool_execute


def send_response(obj):
    """Send a JSON-RPC response to stdout."""
    print(json.dumps(obj), flush=True)


def handle_request(request):
    """Handle a JSON-RPC request."""
    method = request.get("method")
    params = request.get("params", {})
    req_id = request.get("id")

    if method == "send_message":
        text = params.get("text", "")
        # In a full implementation, we'd stream the agent response here
        send_response({
            "jsonrpc": "2.0",
            "method": "text_delta",
            "params": {"text": f"Received: {text}"}
        })
        send_response({
            "jsonrpc": "2.0",
            "result": {"done": True},
            "id": req_id
        })
    elif method == "get_status":
        send_response({
            "jsonrpc": "2.0",
            "result": {"status": "Kuro Desktop v1.0.0"},
            "id": req_id
        })
    else:
        send_response({
            "jsonrpc": "2.0",
            "error": {"code": -32601, "message": f"Method not found: {method}"},
            "id": req_id
        })


def main():
    """Main loop: read JSON-RPC from stdin, write responses to stdout."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            handle_request(request)
        except json.JSONDecodeError as e:
            send_response({
                "jsonrpc": "2.0",
                "error": {"code": -32700, "message": f"Parse error: {e}"}
            })
        except Exception as e:
            send_response({
                "jsonrpc": "2.0",
                "error": {"code": -32603, "message": f"Internal error: {e}"}
            })


if __name__ == "__main__":
    main()

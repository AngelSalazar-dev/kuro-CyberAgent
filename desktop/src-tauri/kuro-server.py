#!/usr/bin/env python3
"""Kuro Desktop backend.

Speaks JSON-RPC over stdin/stdout. The Tauri frontend sends requests; this
process drives the Kuro agent and streams each turn back as events.

Requests:
    {"method": "send_message", "params": {"text": ..., "model": ..., "mode": ...}}
    {"method": "permission_response", "params": {"id": 1, "decision": "yes"}}
    {"method": "get_status"}

Events (method + params):
    ready | status | text_delta | reasoning_delta | tool_call | tool_result
    error | done | permission_request
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import threading
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _candidate_roots() -> list[Path]:
    roots: list[Path] = []
    for value in (
        os.environ.get("KURO_ROOT"),
        str(Path(os.environ["LOCALAPPDATA"]) / "kuro") if os.environ.get("LOCALAPPDATA") else None,
        str(HERE.parent),
        str(HERE.parent.parent),
    ):
        if not value:
            continue
        try:
            root = Path(value).expanduser().resolve()
        except OSError:
            continue
        if root not in roots:
            roots.append(root)
    return roots


ROOTS = _candidate_roots()
KURO_ROOT = next((r for r in ROOTS if (r / "kuro" / "__init__.py").is_file()), None)

if KURO_ROOT is not None:
    sys.path.insert(0, str(KURO_ROOT))

try:
    from kuro.agent import Agent
    from kuro.config import Config

    IMPORT_ERROR = ""
except Exception as exc:  # noqa: BLE001
    Agent = None
    Config = None
    IMPORT_ERROR = f"{type(exc).__name__}: {exc}"


def emit(method: str, params: dict) -> None:
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "method": method, "params": params}, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def kuro_home() -> Path:
    value = os.environ.get("KURO_HOME")
    if value:
        return Path(value).expanduser()
    return Path.home() / ".kuro"


def current_source() -> str:
    marker = kuro_home() / "current-source"
    if marker.is_file():
        return marker.read_text(encoding="utf-8", errors="ignore").strip()
    return ""


def load_profile() -> None:
    inherited = os.environ.get("ANTHROPIC_BASE_URL", "")
    if inherited and "localhost" not in inherited and "127.0.0.1" not in inherited:
        return
    home = kuro_home()
    source = current_source()
    candidates = []
    if source:
        candidates.append(home / source / "source")
    candidates.append(home / "logfare" / "source")
    pattern = re.compile(
        r"""^\s*export\s+([A-Za-z_][A-Za-z0-9_]*)=(?:\"([^\"]*)\"|'([^']*)'|(\S*))\s*$"""
    )
    for path in candidates:
        if not path.is_file():
            continue
        try:
            raw = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for line in raw.splitlines():
            match = pattern.match(line)
            if match:
                value = match.group(2)
                if value is None:
                    value = match.group(3)
                if value is None:
                    value = match.group(4) or ""
                os.environ[match.group(1)] = value
        return


def proxy_answers(port: str) -> bool:
    url = f"http://127.0.0.1:{port}/health/liveliness"
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            return response.status == 200
    except Exception:  # noqa: BLE001
        return False


def ensure_proxy() -> str:
    base = os.environ.get("ANTHROPIC_BASE_URL", "")
    host = urllib.parse.urlparse(base).hostname or ""
    if host not in ("localhost", "127.0.0.1"):
        return ""
    port = str(urllib.parse.urlparse(base).port or 4001)
    if proxy_answers(port):
        return f"proxy on :{port}"
    home = kuro_home()
    source = current_source()
    service = home / source / "start-litellm.ps1" if source else None
    if service is None or not service.is_file():
        service = home / "logfare" / "start-litellm.ps1"
    if service is None or not service.is_file():
        return ""
    try:
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(service),
                "start",
            ],
            check=False,
            capture_output=True,
            timeout=120,
        )
    except Exception as exc:  # noqa: BLE001
        return f"proxy start failed: {type(exc).__name__}"
    if proxy_answers(port):
        return f"proxy started on :{port}"
    return f"proxy not answering on :{port}"


def endpoint_state() -> str:
    base = os.environ.get("ANTHROPIC_BASE_URL", "")
    token = os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY") or ""
    if not base:
        return "no endpoint configured: run setup.ps1, or set ANTHROPIC_BASE_URL in the environment"
    parsed = urllib.parse.urlparse(base)
    if parsed.hostname in ("localhost", "127.0.0.1"):
        port = str(parsed.port or 4001)
        if not proxy_answers(port):
            return f"the local proxy on :{port} is not answering: run setup.ps1 to start it"
        if not token:
            return f"the proxy on :{port} is up, but no Logfare key is configured: write lfu_... into the profile"
    if not token:
        return "the configured endpoint has no credential"
    return ""


class Backend:
    def __init__(self) -> None:
        self.agent = None
        self.model = os.environ.get("KURO_MODEL", "deepseek-v4.1-flash")
        self.mode = os.environ.get("KURO_MODE", "auto")
        self.pending: dict[int, asyncio.Future] = {}
        self.counter = 0
        self.busy = False

    def build_agent(self):
        if self.agent is not None:
            return self.agent
        if Agent is None:
            raise RuntimeError(f"the kuro package is not importable ({IMPORT_ERROR})")
        cfg = Config.from_env(model=self.model, mode=self.mode)
        self.agent = Agent(cfg, permission_handler=self.ask_permission)
        return self.agent

    async def ask_permission(self, name: str, args: dict, preview) -> str:
        self.counter += 1
        ident = self.counter
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self.pending[ident] = future
        emit(
            "permission_request",
            {
                "id": ident,
                "name": name,
                "args": args,
                "preview": str(preview)[:4000] if preview is not None else "",
            },
        )
        try:
            return await future
        finally:
            self.pending.pop(ident, None)

    def resolve_permission(self, ident: int, decision: str) -> bool:
        future = self.pending.get(ident)
        if future is None or future.done():
            return False
        future.set_result(decision)
        return True

    async def run_turn(self, text: str) -> None:
        self.busy = True
        try:
            agent = self.build_agent()
            async for event in agent.run_turn(text):
                kind = event.get("type", "")
                if kind == "text_delta":
                    emit("text_delta", {"text": event.get("text", "")})
                elif kind == "reasoning_delta":
                    emit("reasoning_delta", {"text": event.get("text", "")})
                elif kind == "tool_call":
                    emit(
                        "tool_call",
                        {
                            "name": event.get("name", ""),
                            "id": event.get("id"),
                            "args": event.get("args"),
                            "preview": str(event.get("preview") or "")[:4000],
                        },
                    )
                elif kind == "tool_result":
                    emit(
                        "tool_result",
                        {
                            "name": event.get("name", ""),
                            "id": event.get("id"),
                            "is_error": bool(event.get("is_error")),
                            "output": str(event.get("output") or "")[:4000],
                        },
                    )
                elif kind == "error":
                    emit("error", {"message": str(event.get("message", ""))})
                elif kind in ("iteration_cap", "continuing", "compacting", "compacted", "truncated", "retry"):
                    emit("status", {"event": kind, "detail": event})
        except Exception as exc:  # noqa: BLE001
            emit("error", {"message": f"{type(exc).__name__}: {exc}"})
        finally:
            self.busy = False
            emit(
                "done",
                {
                    "cost": getattr(self.agent, "last_turn_cost", 0.0) if self.agent else 0.0,
                    "input_tokens": getattr(self.agent, "session_input_tokens", 0) if self.agent else 0,
                    "output_tokens": getattr(self.agent, "session_output_tokens", 0) if self.agent else 0,
                },
            )


def stdin_reader(loop: asyncio.AbstractEventLoop, queue: asyncio.Queue) -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        loop.call_soon_threadsafe(queue.put_nowait, line)
    loop.call_soon_threadsafe(queue.put_nowait, None)


def main() -> None:
    load_profile()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    backend = Backend()
    queue: asyncio.Queue = asyncio.Queue()

    reader = threading.Thread(target=stdin_reader, args=(loop, queue), daemon=True)
    reader.start()

    async def pump() -> None:
        while True:
            raw = await queue.get()
            if raw is None:
                break
            try:
                request = json.loads(raw)
            except json.JSONDecodeError as exc:
                emit("error", {"message": f"parse error: {exc}"})
                continue
            method = request.get("method", "")
            params = request.get("params") or {}
            if method == "send_message":
                text = str(params.get("text", ""))
                model = params.get("model")
                mode = params.get("mode")
                if model:
                    backend.model = str(model)
                if mode:
                    backend.mode = str(mode)
                if backend.agent is not None:
                    backend.agent.set_model(backend.model)
                    backend.agent.set_mode(backend.mode)
                asyncio.ensure_future(backend.run_turn(text))
            elif method == "permission_response":
                ident = int(params.get("id", 0))
                decision = str(params.get("decision", "no"))
                backend.resolve_permission(ident, decision)
            elif method == "get_status":
                emit(
                    "status",
                    {
                        "ready": backend.agent is not None or Agent is not None,
                        "model": backend.model,
                        "mode": backend.mode,
                        "busy": backend.busy,
                    },
                )

    async def boot() -> None:
        detail = ensure_proxy()
        emit(
            "status",
            {
                "ready": True,
                "model": backend.model,
                "mode": backend.mode,
                "proxy": detail,
                "notice": endpoint_state(),
            },
        )
        try:
            backend.build_agent()
        except Exception as exc:  # noqa: BLE001
            emit("error", {"message": f"{type(exc).__name__}: {exc}"})

    loop.create_task(boot())
    loop.run_until_complete(pump())


if __name__ == "__main__":
    main()

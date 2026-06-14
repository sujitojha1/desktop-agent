"""The single doorway to the `cua-driver` daemon.

Every desktop operation in the Computer skill — launch, scan, click, type, kill —
goes through `call` / `acall` here. There is no `cua` Python SDK and no PowerShell
`Start-Process` anywhere in `computer/`: the daemon (a Rust binary listening on a
Windows named pipe) is the one and only backend, and this module is the one and
only way to reach it. `mcp_server.py` imports the same `call`, so the MCP tool
surface and the in-skill surface can never drift to two different transports.

Invocation is `cua-driver call <tool> <json-args>` as a subprocess argv list
(never through a shell), so JSON — including Windows paths with backslashes — is
passed verbatim as one argv element without any quoting hazard. Failures come back
as ``{"error": "..."}`` rather than raising, so the agent loop can read the message
and recover (re-scan, pick another element) instead of crashing the run.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from typing import Any

# Override the binary (tests / non-PATH installs) with CUA_DRIVER_BIN.
CUA_DRIVER_BIN = os.environ.get("CUA_DRIVER_BIN", "cua-driver")

DEFAULT_TIMEOUT = 60


def call(tool: str, args: dict | None = None, *, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Invoke one cua-driver tool through the running daemon, return parsed JSON.

    Never raises: a spawn failure, non-zero exit, or unparseable output is folded
    into ``{"error": ...}`` (or ``{"raw": ...}`` for non-JSON stdout, ``{"ok": True}``
    for an empty-but-successful response) so callers branch on the dict, not on
    exceptions."""
    exe = shutil.which(CUA_DRIVER_BIN) or CUA_DRIVER_BIN
    try:
        proc = subprocess.run(
            [exe, "call", tool, json.dumps(args or {})],
            capture_output=True, text=True, timeout=timeout,
            # Force UTF-8: get_window_state's base64 screenshot + box-drawing chars
            # in tree_markdown aren't decodable under the default cp1252 console
            # codepage, which would otherwise crash the stdout reader thread.
            encoding="utf-8", errors="replace",
        )
    except Exception as e:  # noqa: BLE001
        return {"error": f"cua-driver {tool} failed to spawn: {type(e).__name__}: {e}"}
    if proc.returncode != 0:
        return {"error": (proc.stderr or proc.stdout or "non-zero exit").strip()}
    out = (proc.stdout or "").strip()
    if not out:
        return {"ok": True}
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"raw": out}


async def acall(tool: str, args: dict | None = None, *,
                timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Async wrapper over :func:`call` — the daemon call is a blocking subprocess,
    so it runs in a worker thread to keep the skill's event loop free."""
    return await asyncio.to_thread(call, tool, args, timeout=timeout)


def is_error(res: dict | None) -> bool:
    """True when a driver response carries an ``error`` key."""
    return bool(res) and "error" in res


def error_text(res: dict | None) -> str:
    """The error message from a driver response, or '' when there is none."""
    return str(res.get("error", "")) if res else ""

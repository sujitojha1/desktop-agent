"""S9 Computer-Use skill — desktop driver via cua.Localhost.

Direct, unsandboxed host control through `cua.Localhost.connect()` (NOT a VM
Sandbox, NOT the macOS Rust cua-driver binary). Perception is screenshot-based
and actions are coordinate-based — there is no UIA accessibility tree — so the
cascade is deterministic (launch + scripted steps) → vision (V9 /v1/vision).
Reuses browser.client.V9Client for the gateway; no new gateway.
"""
from .app_registry import (
    AppEntry,
    list_apps,
    load_registry,
    resolve_app,
)
from .skill import (
    ACTION_SCHEMA,
    ComputerOutput,
    ComputerSkill,
    SYSTEM_PROMPT_VISION,
)
from .tools import (
    TOOLS,
    ToolContext,
    ToolSpec,
    enumerate_windows,
    front_and_maximize,
    launch_process,
    list_tools,
    run_tool,
    tool_names,
)

__all__ = [
    "ACTION_SCHEMA",
    "AppEntry",
    "ComputerOutput",
    "ComputerSkill",
    "SYSTEM_PROMPT_VISION",
    "list_apps",
    "load_registry",
    "resolve_app",
    "TOOLS",
    "ToolContext",
    "ToolSpec",
    "enumerate_windows",
    "front_and_maximize",
    "launch_process",
    "list_tools",
    "run_tool",
    "tool_names",
]

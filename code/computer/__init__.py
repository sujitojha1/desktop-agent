"""S9 Computer-Use skill — desktop driver via the cua-driver daemon.

Every desktop operation goes through the `cua-driver` daemon (a Rust binary on a
Windows named pipe); there is no `cua` Python SDK and no PowerShell. Perception is
the daemon's per-window UIA tree + screenshot (`get_window_state`); the cascade is
deterministic (launch + scripted tool steps) → hybrid drive (element_index over
the UIA tree, with a per-window vision fallback). Reuses browser.client.V9Client
for the gateway; no new gateway. See docs/cua_driver_tools.md.
"""
from . import driver
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
    SYSTEM_PROMPT_TREE,
    SYSTEM_PROMPT_VISION,
)
from .tools import (
    TOOLS,
    ToolContext,
    ToolSpec,
    enumerate_windows,
    launch_app,
    list_tools,
    run_tool,
    scan,
    tool_names,
)

__all__ = [
    "ACTION_SCHEMA",
    "AppEntry",
    "ComputerOutput",
    "ComputerSkill",
    "SYSTEM_PROMPT_TREE",
    "SYSTEM_PROMPT_VISION",
    "driver",
    "list_apps",
    "load_registry",
    "resolve_app",
    "TOOLS",
    "ToolContext",
    "ToolSpec",
    "enumerate_windows",
    "launch_app",
    "list_tools",
    "run_tool",
    "scan",
    "tool_names",
]

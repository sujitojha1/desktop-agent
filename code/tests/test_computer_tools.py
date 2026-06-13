"""Smoke tests for the Computer skill's named tool surface (issue #4).

These run without a desktop: they assert the registry enumerates the expected
tools and that dispatch routes by name through `run_tool` against a fake host.
The live deterministic calc run (launch → type → key → '=') is exercised
separately on Windows; CI has no display, so it is not encoded here.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from computer.tools import ToolContext, list_tools, run_tool, tool_names


# Tools the registry must expose (the ✅/⚠️ rows of the #3 coverage table).
EXPECTED = {
    "click", "double_click", "right_click", "move", "drag", "scroll",
    "type", "key", "hotkey", "launch", "kill_app", "bring_to_front",
    "list_windows", "get_screen_size", "get_cursor_position",
    "screenshot", "zoom", "wait",
}


def test_registry_enumerates_expected_tools():
    names = set(tool_names())
    assert EXPECTED <= names, f"missing tools: {EXPECTED - names}"


def test_list_tools_shape():
    for t in list_tools():
        assert set(t) == {"name", "description", "schema"}
        assert t["schema"]["type"] == "object"
        assert isinstance(t["description"], str) and t["description"]


def test_unknown_tool_is_a_clean_error():
    ctx = ToolContext(host=None)
    out = asyncio.run(run_tool("nope", {}, ctx))
    assert out.startswith("error: unknown tool")


# ── dispatch routes by name against a fake async host ────────────────────────
class _Recorder:
    def __init__(self):
        self.calls: list[tuple] = []

    def __getattr__(self, group):
        rec = self

        class _Group:
            def __getattr__(self, method):
                async def _call(*args, **kw):
                    rec.calls.append((f"{group}.{method}", args, kw))
                return _call
        return _Group()


def test_dispatch_routes_and_scales_coordinates():
    host = _Recorder()
    # scale 2.0 → (10,20) becomes (20,40); proves coords flow through ToolContext.
    ctx = ToolContext(host=host, scale_x=2.0, scale_y=2.0)
    out = asyncio.run(run_tool("click", {"x": 10, "y": 20}, ctx))
    assert out == "ok"
    assert host.calls == [("mouse.click", (20, 40, "left"), {})]


def test_type_and_key_route_to_keyboard():
    host = _Recorder()
    ctx = ToolContext(host=host)
    asyncio.run(run_tool("type", {"value": "56"}, ctx))
    asyncio.run(run_tool("key", {"value": "Enter"}, ctx))
    assert host.calls == [
        ("keyboard.type", ("56",), {}),
        ("keyboard.keypress", ("Enter",), {}),
    ]


def test_missing_coords_is_validation_error_not_crash():
    ctx = ToolContext(host=_Recorder())
    out = asyncio.run(run_tool("click", {}, ctx))
    assert out.startswith("error: click needs x,y")

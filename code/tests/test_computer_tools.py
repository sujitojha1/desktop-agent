"""Smoke tests for the Computer skill's named tool surface (computer/tools.py).

These run without the cua-driver daemon or a desktop: every tool dispatches
through `computer.driver.acall`, which we replace with a recorder so we can assert
the exact `(driver_tool, args)` payload each handler builds. No SDK, no PowerShell,
no fake `host` object — the only boundary is the driver call.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from computer import tools as tools_mod
from computer.tools import (
    ToolContext,
    enumerate_windows,
    launch_app,
    list_tools,
    run_tool,
    tool_names,
)


# ── driver recorder ──────────────────────────────────────────────────────────
class _Driver:
    """Stand-in for computer.driver: records every acall and returns canned
    responses keyed by tool name (default {"ok": True})."""
    def __init__(self, responses=None):
        self.calls: list[tuple[str, dict]] = []
        self.responses = responses or {}

    async def acall(self, tool, args=None, **kw):
        self.calls.append((tool, dict(args or {})))
        return self.responses.get(tool, {"ok": True})


def _install(monkeypatch, responses=None) -> _Driver:
    rec = _Driver(responses)
    monkeypatch.setattr(tools_mod.driver, "acall", rec.acall)
    # Make launch's settle-sleep instant.
    async def _nosleep(*_a, **_k):
        return None
    monkeypatch.setattr(tools_mod.asyncio, "sleep", _nosleep)
    return rec


def _run(coro):
    return asyncio.run(coro)


# ── registry shape ────────────────────────────────────────────────────────────
EXPECTED = {
    "scan", "click", "double_click", "right_click", "type", "key", "hotkey",
    "set_value", "scroll", "drag", "launch", "kill_app", "bring_to_front",
    "list_windows", "list_apps", "get_screen_size", "get_cursor_position",
    "zoom", "wait",
}


def test_registry_enumerates_expected_tools():
    assert EXPECTED <= set(tool_names())


def test_list_tools_shape():
    for t in list_tools():
        assert set(t) == {"name", "description", "schema"}
        assert t["schema"]["type"] == "object"
        assert isinstance(t["description"], str) and t["description"]


def test_unknown_tool_is_a_clean_error():
    out = _run(run_tool("nope", {}, ToolContext()))
    assert out.startswith("error: unknown tool")


# ── addressing: element_index vs pixels, context targeting ────────────────────
def test_click_by_element_index_targets_context_window(monkeypatch):
    rec = _install(monkeypatch)
    ctx = ToolContext(pid=10, window_id=20)
    assert _run(run_tool("click", {"element_index": 5}, ctx)) == "ok"
    assert rec.calls == [("click", {"pid": 10, "window_id": 20, "element_index": 5})]


def test_click_by_pixels_when_no_element(monkeypatch):
    rec = _install(monkeypatch)
    ctx = ToolContext(pid=10, window_id=20)
    _run(run_tool("click", {"x": 100, "y": 200, "button": "right"}, ctx))
    assert rec.calls == [("click", {"pid": 10, "window_id": 20,
                                    "x": 100, "y": 200, "button": "right"})]


def test_click_needs_pid(monkeypatch):
    _install(monkeypatch)
    out = _run(run_tool("click", {"element_index": 5}, ToolContext()))
    assert out.startswith("error: click needs a target pid")


def test_click_needs_address(monkeypatch):
    _install(monkeypatch)
    out = _run(run_tool("click", {}, ToolContext(pid=10, window_id=20)))
    assert out.startswith("error: click needs element_index or x,y")


def test_action_pid_override_wins(monkeypatch):
    rec = _install(monkeypatch)
    ctx = ToolContext(pid=10, window_id=20)
    _run(run_tool("click", {"element_index": 1, "pid": 99, "window_id": 88}, ctx))
    assert rec.calls[0][1]["pid"] == 99 and rec.calls[0][1]["window_id"] == 88


def test_session_flows_onto_payload(monkeypatch):
    rec = _install(monkeypatch)
    ctx = ToolContext(pid=10, window_id=20, session="run-1")
    _run(run_tool("click", {"element_index": 1}, ctx))
    assert rec.calls[0][1]["session"] == "run-1"


# ── keyboard / value tools map to driver tool names ───────────────────────────
def test_type_maps_to_type_text(monkeypatch):
    rec = _install(monkeypatch)
    _run(run_tool("type", {"value": "hi"}, ToolContext(pid=7)))
    assert rec.calls == [("type_text", {"pid": 7, "text": "hi"})]


def test_type_alias_and_element_index(monkeypatch):
    rec = _install(monkeypatch)
    _run(run_tool("type_text", {"text": "x", "element_index": 3}, ToolContext(pid=7, window_id=8)))
    assert rec.calls == [("type_text", {"pid": 7, "window_id": 8, "text": "x", "element_index": 3})]


def test_key_maps_to_press_key(monkeypatch):
    rec = _install(monkeypatch)
    _run(run_tool("key", {"value": "enter"}, ToolContext(pid=7)))
    assert rec.calls == [("press_key", {"pid": 7, "key": "enter"})]


def test_key_accepts_keys_list_for_single_key(monkeypatch):
    # The model sometimes emits a single key as `keys: ["down"]`; absorb it
    # instead of failing with "key needs a key name".
    rec = _install(monkeypatch)
    assert _run(run_tool("key", {"keys": ["down"]}, ToolContext(pid=7))) == "ok"
    assert rec.calls == [("press_key", {"pid": 7, "key": "down"})]


def test_key_with_multiple_keys_routes_to_hotkey(monkeypatch):
    rec = _install(monkeypatch)
    assert _run(run_tool("key", {"keys": ["ctrl", "s"]}, ToolContext(pid=7))) == "ok"
    assert rec.calls == [("hotkey", {"pid": 7, "keys": ["ctrl", "s"]})]


def test_hotkey_requires_two_keys(monkeypatch):
    rec = _install(monkeypatch)
    assert _run(run_tool("hotkey", {"keys": ["ctrl", "s"]}, ToolContext(pid=7))) == "ok"
    assert rec.calls == [("hotkey", {"pid": 7, "keys": ["ctrl", "s"]})]
    out = _run(run_tool("hotkey", {"keys": ["ctrl"]}, ToolContext(pid=7)))
    assert out.startswith("error: hotkey needs")


def test_set_value_requires_window_and_index(monkeypatch):
    rec = _install(monkeypatch)
    out = _run(run_tool("set_value", {"element_index": 2, "value": "5"},
                        ToolContext(pid=7, window_id=8)))
    assert out == "ok"
    assert rec.calls == [("set_value", {"pid": 7, "window_id": 8,
                                        "element_index": 2, "value": "5"})]


def test_scroll_validates_direction(monkeypatch):
    rec = _install(monkeypatch)
    assert _run(run_tool("scroll", {"direction": "down", "amount": 4}, ToolContext(pid=7))) == "ok"
    assert rec.calls == [("scroll", {"pid": 7, "direction": "down", "amount": 4})]
    out = _run(run_tool("scroll", {"direction": "sideways"}, ToolContext(pid=7)))
    assert out.startswith("error: scroll needs direction")


# ── driver errors are folded into the outcome string ──────────────────────────
def test_driver_error_becomes_outcome(monkeypatch):
    _install(monkeypatch, {"click": {"error": "background_unavailable"}})
    out = _run(run_tool("click", {"element_index": 1}, ToolContext(pid=7, window_id=8)))
    assert out == "error: background_unavailable"


# ── enumerate_windows wraps driver list_windows ───────────────────────────────
def test_enumerate_windows_filters_by_title(monkeypatch):
    _install(monkeypatch, {"list_windows": {"windows": [
        {"pid": 1, "window_id": 11, "title": "Calculator"},
        {"pid": 2, "window_id": 22, "title": "VS Code"}]}})
    wins = _run(enumerate_windows(title="calc"))
    assert wins == [{"pid": 1, "window_id": 11, "title": "Calculator"}]


# ── launch recovery: stub/UWP windows reconciled by title ─────────────────────
def test_launch_reconciles_window_by_title(monkeypatch):
    # launch_app returns a stub (pid 0, no windows); list_windows recovers the
    # real on-screen window by the registry's window_title ("Calculator").
    rec = _install(monkeypatch, {
        "launch_app": {"pid": 0, "windows": []},
        "list_windows": {"windows": [
            {"pid": 555, "window_id": 999, "title": "Calculator", "is_on_screen": True}]},
    })
    res = _run(launch_app("calculator"))
    assert res["pid"] == 555 and res["window_id"] == 999
    # AUMID launch field came from the registry.
    assert rec.calls[0][0] == "launch_app"
    assert "aumid" in rec.calls[0][1]


def test_launch_tool_updates_context(monkeypatch):
    _install(monkeypatch, {
        "launch_app": {"pid": 0, "windows": []},
        "list_windows": {"windows": [
            {"pid": 555, "window_id": 999, "title": "Calculator", "is_on_screen": True}]},
    })
    ctx = ToolContext()
    out = _run(run_tool("launch", {"app": "calculator"}, ctx))
    assert "ok: launched calculator" in out
    assert ctx.pid == 555 and ctx.window_id == 999 and ctx.launched_pids == {555}


def test_kill_app_uses_context_pid(monkeypatch):
    rec = _install(monkeypatch)
    _run(run_tool("kill_app", {}, ToolContext(pid=42)))
    assert rec.calls == [("kill_app", {"pid": 42})]


# ── app registry (apps.yaml) ──────────────────────────────────────────────────
from computer.app_registry import list_apps as registry_apps
from computer.app_registry import load_registry, resolve_app


def test_registry_resolves_key_alias_and_case():
    load_registry(force=True)
    assert resolve_app("calculator").launch_field == "aumid"
    assert resolve_app("CALC").name == "calculator"
    assert resolve_app("  vs  code ").name == "vscode"      # space-collapsed alias
    assert resolve_app("nonesuch") is None
    assert resolve_app("") is None


def test_registry_launch_fields():
    assert resolve_app("vscode").launch_args() == {"name": "code"}
    assert resolve_app("excel").launch_field == "path"
    assert resolve_app("calculator").launch_value.endswith("!App")


def test_registry_window_title_default():
    assert resolve_app("excel").window_title == "Excel"


def test_list_apps_deduplicates_across_aliases():
    names = {e.name for e in registry_apps()}
    assert {"calculator", "vscode", "excel"} <= names
    assert sum(e.name == "calculator" for e in registry_apps()) == 1


def test_unknown_app_falls_through():
    load_registry(force=True)
    assert resolve_app("notepad++") is None  # not configured → launched by name


def test_list_apps_tool_reports_configured_apps(monkeypatch):
    _install(monkeypatch)
    out = _run(run_tool("list_apps", {}, ToolContext()))
    assert out.startswith("ok:") and "calculator" in out and "vscode" in out

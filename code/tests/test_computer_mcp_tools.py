"""Smoke tests for the cua-driver-backed computer_* MCP tools (mcp_server.py).

The tools shell out to `cua-driver call <tool> <json>`; here we monkeypatch
`subprocess.run` so the tests need neither the driver daemon nor a desktop.
We assert (a) the correct cua-driver tool + JSON args are built, and (b) the
process result / errors are surfaced sanely.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mcp_server


class _Proc:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


def _patch(monkeypatch, *, stdout="", stderr="", returncode=0):
    """Capture the argv cua-driver would be called with."""
    seen = {}

    def fake_run(argv, capture_output, text, timeout):
        seen["argv"] = argv
        return _Proc(stdout=stdout, stderr=stderr, returncode=returncode)

    monkeypatch.setattr(mcp_server.subprocess, "run", fake_run)
    monkeypatch.setattr(mcp_server.shutil, "which", lambda _x: "cua-driver")
    return seen


def test_get_window_state_builds_correct_call(monkeypatch):
    seen = _patch(monkeypatch, stdout=json.dumps(
        {"element_count": 2, "tree_markdown": '[5] Button "Seven"'}))
    out = mcp_server.computer_get_window_state(34384, 3342692, "ax", "Button")
    argv = seen["argv"]
    assert argv[:3] == ["cua-driver", "call", "get_window_state"]
    args = json.loads(argv[3])
    assert args == {"pid": 34384, "window_id": 3342692,
                    "capture_mode": "ax", "query": "Button"}
    assert out["element_count"] == 2 and "Button" in out["tree_markdown"]


def test_click_prefers_element_index_and_omits_unset_xy(monkeypatch):
    seen = _patch(monkeypatch, stdout="{}")
    mcp_server.computer_click(34384, 3342692, element_index=5)
    args = json.loads(seen["argv"][3])
    assert args["element_index"] == 5 and "x" not in args and "y" not in args


def test_click_uses_coords_when_no_element(monkeypatch):
    seen = _patch(monkeypatch, stdout="{}")
    mcp_server.computer_click(34384, x=120, y=340)
    args = json.loads(seen["argv"][3])
    assert args["x"] == 120 and args["y"] == 340 and "element_index" not in args


def test_press_key_and_hotkey(monkeypatch):
    seen = _patch(monkeypatch, stdout="{}")
    mcp_server.computer_press_key(7, "enter")
    assert json.loads(seen["argv"][3]) == {"pid": 7, "key": "enter"}
    mcp_server.computer_hotkey(7, ["ctrl", "s"])
    assert json.loads(seen["argv"][3]) == {"pid": 7, "keys": ["ctrl", "s"]}


def test_nonzero_exit_surfaces_error(monkeypatch):
    _patch(monkeypatch, stderr="No window with window_id 999 exists", returncode=1)
    out = mcp_server.computer_list_windows(999)
    assert "error" in out and "999" in out["error"]


def test_large_tree_is_truncated(monkeypatch):
    big = "x" * 9000
    _patch(monkeypatch, stdout=json.dumps({"element_count": 1, "tree_markdown": big}))
    out = mcp_server.computer_get_window_state(1, 2)
    assert len(out["tree_markdown"]) < 9000 and "truncated" in out["tree_markdown"]


def test_get_accessibility_tree_builds_correct_call(monkeypatch):
    seen = _patch(monkeypatch, stdout=json.dumps({"windows": []}))
    out = mcp_server.computer_get_accessibility_tree()
    argv = seen["argv"]
    assert argv[:3] == ["cua-driver", "call", "get_accessibility_tree"]
    args = json.loads(argv[3])
    assert args == {}
    assert out == {"windows": []}

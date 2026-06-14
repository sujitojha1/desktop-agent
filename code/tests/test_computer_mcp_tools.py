"""Smoke tests for the cua-driver-backed computer_* MCP tools (mcp_server.py).

The MCP tools and the ComputerSkill share ONE transport — `computer.driver.call`
→ `cua-driver call <tool> <json>`. Here we monkeypatch `subprocess.run` on that
shared module, so the tests need neither the driver daemon nor a desktop. We
assert (a) the correct cua-driver tool + JSON args are built, and (b) the process
result / errors are surfaced sanely.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mcp_server
from computer import driver


class _Proc:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


def _patch(monkeypatch, *, stdout="", stderr="", returncode=0):
    """Capture the argv cua-driver would be called with, on the shared driver."""
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return _Proc(stdout=stdout, stderr=stderr, returncode=returncode)

    monkeypatch.setattr(driver.subprocess, "run", fake_run)
    monkeypatch.setattr(driver.shutil, "which", lambda _x: "cua-driver")
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


def test_double_click_builds_correct_call(monkeypatch):
    seen = _patch(monkeypatch, stdout="{}")
    mcp_server.computer_double_click(34384, 3342692, element_index=5)
    args = json.loads(seen["argv"][3])
    assert args == {"pid": 34384, "window_id": 3342692, "element_index": 5, "dispatch": "background", "from_zoom": False}


def test_right_click_builds_correct_call(monkeypatch):
    seen = _patch(monkeypatch, stdout="{}")
    mcp_server.computer_right_click(34384, x=10.0, y=20.0)
    args = json.loads(seen["argv"][3])
    assert args == {"pid": 34384, "x": 10.0, "y": 20.0, "dispatch": "background", "from_zoom": False}


def test_drag_builds_correct_call(monkeypatch):
    seen = _patch(monkeypatch, stdout="{}")
    mcp_server.computer_drag(34384, from_x=10.0, from_y=10.0, to_x=100.0, to_y=100.0)
    args = json.loads(seen["argv"][3])
    assert args == {
        "pid": 34384, "from_x": 10.0, "from_y": 10.0, "to_x": 100.0, "to_y": 100.0,
        "button": "left", "steps": 20, "duration_ms": 500, "dispatch": "background", "from_zoom": False
    }


def test_move_cursor_builds_correct_call(monkeypatch):
    seen = _patch(monkeypatch, stdout="{}")
    mcp_server.computer_move_cursor(10.0, 20.0, cursor_id="agent")
    args = json.loads(seen["argv"][3])
    assert args == {"x": 10.0, "y": 20.0, "cursor_id": "agent"}


def test_get_cursor_position_builds_correct_call(monkeypatch):
    seen = _patch(monkeypatch, stdout=json.dumps({"x": 100, "y": 200}))
    out = mcp_server.computer_get_cursor_position()
    args = json.loads(seen["argv"][3])
    assert args == {}
    assert out == {"x": 100, "y": 200}


def test_bring_to_front_builds_correct_call(monkeypatch):
    seen = _patch(monkeypatch, stdout="{}")
    mcp_server.computer_bring_to_front(34384, 3342692)
    args = json.loads(seen["argv"][3])
    assert args == {"pid": 34384, "window_id": 3342692}


def test_kill_app_builds_correct_call(monkeypatch):
    seen = _patch(monkeypatch, stdout="{}")
    mcp_server.computer_kill_app(34384)
    args = json.loads(seen["argv"][3])
    assert args == {"pid": 34384}


def test_debug_window_info_builds_correct_call(monkeypatch):
    seen = _patch(monkeypatch, stdout="{}")
    mcp_server.computer_debug_window_info(34384)
    args = json.loads(seen["argv"][3])
    assert args == {"pid": 34384}


def test_zoom_builds_correct_call(monkeypatch):
    seen = _patch(monkeypatch, stdout="{}")
    mcp_server.computer_zoom(34384, 3342692, 10.0, 10.0, 100.0, 100.0)
    args = json.loads(seen["argv"][3])
    assert args == {"pid": 34384, "window_id": 3342692, "x1": 10.0, "y1": 10.0, "x2": 100.0, "y2": 100.0}


def test_page_builds_correct_call(monkeypatch):
    seen = _patch(monkeypatch, stdout=json.dumps({"ok": True}))
    out = mcp_server.computer_page(action="get_text", pid=34384)
    argv = seen["argv"]
    assert argv[:3] == ["cua-driver", "call", "page"]
    args = json.loads(argv[3])
    assert args == {"action": "get_text", "pid": 34384}
    assert out == {"ok": True}


def test_start_recording_builds_correct_call(monkeypatch):
    seen = _patch(monkeypatch, stdout="{}")
    mcp_server.computer_start_recording(output_dir="/tmp/run1", record_video=True)
    argv = seen["argv"]
    assert argv[:3] == ["cua-driver", "call", "start_recording"]
    args = json.loads(argv[3])
    assert args == {"output_dir": "/tmp/run1", "record_video": True}


def test_stop_recording_builds_correct_call(monkeypatch):
    seen = _patch(monkeypatch, stdout="{}")
    mcp_server.computer_stop_recording()
    argv = seen["argv"]
    assert argv[:3] == ["cua-driver", "call", "stop_recording"]
    args = json.loads(argv[3])
    assert args == {}


def test_get_recording_state_builds_correct_call(monkeypatch):
    seen = _patch(monkeypatch, stdout="{}")
    mcp_server.computer_get_recording_state()
    argv = seen["argv"]
    assert argv[:3] == ["cua-driver", "call", "get_recording_state"]
    args = json.loads(argv[3])
    assert args == {}


def test_replay_trajectory_builds_correct_call(monkeypatch):
    seen = _patch(monkeypatch, stdout="{}")
    mcp_server.computer_replay_trajectory(dir="/tmp/run1", delay_ms=200, stop_on_error=False)
    argv = seen["argv"]
    assert argv[:3] == ["cua-driver", "call", "replay_trajectory"]
    args = json.loads(argv[3])
    assert args == {"dir": "/tmp/run1", "delay_ms": 200, "stop_on_error": False}


def test_install_ffmpeg_builds_correct_call(monkeypatch):
    seen = _patch(monkeypatch, stdout="{}")
    mcp_server.computer_install_ffmpeg(confirm=True)
    argv = seen["argv"]
    assert argv[:3] == ["cua-driver", "call", "install_ffmpeg"]
    args = json.loads(argv[3])
    assert args == {"confirm": True}


def test_start_session_builds_correct_call(monkeypatch):
    seen = _patch(monkeypatch, stdout="{}")
    mcp_server.computer_start_session("session-abc")
    argv = seen["argv"]
    assert argv[:3] == ["cua-driver", "call", "start_session"]
    assert json.loads(argv[3]) == {"session": "session-abc"}


def test_end_session_builds_correct_call(monkeypatch):
    seen = _patch(monkeypatch, stdout="{}")
    mcp_server.computer_end_session("session-abc")
    argv = seen["argv"]
    assert argv[:3] == ["cua-driver", "call", "end_session"]
    assert json.loads(argv[3]) == {"session": "session-abc"}


def test_set_agent_cursor_enabled_builds_correct_call(monkeypatch):
    seen = _patch(monkeypatch, stdout="{}")
    mcp_server.computer_set_agent_cursor_enabled(True, cursor_id="custom-cursor")
    argv = seen["argv"]
    assert argv[:3] == ["cua-driver", "call", "set_agent_cursor_enabled"]
    assert json.loads(argv[3]) == {"enabled": True, "cursor_id": "custom-cursor"}


def test_set_agent_cursor_style_builds_correct_call(monkeypatch):
    seen = _patch(monkeypatch, stdout="{}")
    mcp_server.computer_set_agent_cursor_style(
        bloom_color="#00FFFF",
        cursor_id="cursor-1",
        gradient_colors=["#FF0000", "#0000FF"],
        image_path="/path/to/icon.png"
    )
    argv = seen["argv"]
    assert argv[:3] == ["cua-driver", "call", "set_agent_cursor_style"]
    assert json.loads(argv[3]) == {
        "bloom_color": "#00FFFF",
        "cursor_id": "cursor-1",
        "gradient_colors": ["#FF0000", "#0000FF"],
        "image_path": "/path/to/icon.png"
    }


def test_set_agent_cursor_motion_builds_correct_call(monkeypatch):
    seen = _patch(monkeypatch, stdout="{}")
    mcp_server.computer_set_agent_cursor_motion(
        cursor_id="cursor-1",
        cursor_icon="crosshair",
        cursor_color="#FF00FF",
        cursor_label="agent-1",
        cursor_size=20.0,
        cursor_opacity=0.9,
        arc_size=0.5,
        arc_flow=0.2,
        start_handle=0.4,
        end_handle=0.4,
        spring=0.8,
        glide_duration_ms=150.0,
        dwell_after_click_ms=100.0,
        idle_hide_ms=5000.0,
        turn_radius=90.0
    )
    argv = seen["argv"]
    assert argv[:3] == ["cua-driver", "call", "set_agent_cursor_motion"]
    assert json.loads(argv[3]) == {
        "cursor_id": "cursor-1",
        "cursor_icon": "crosshair",
        "cursor_color": "#FF00FF",
        "cursor_label": "agent-1",
        "cursor_size": 20.0,
        "cursor_opacity": 0.9,
        "arc_size": 0.5,
        "arc_flow": 0.2,
        "start_handle": 0.4,
        "end_handle": 0.4,
        "spring": 0.8,
        "glide_duration_ms": 150.0,
        "dwell_after_click_ms": 100.0,
        "idle_hide_ms": 5000.0,
        "turn_radius": 90.0
    }


def test_get_agent_cursor_state_builds_correct_call(monkeypatch):
    seen = _patch(monkeypatch, stdout="{}")
    mcp_server.computer_get_agent_cursor_state()
    argv = seen["argv"]
    assert argv[:3] == ["cua-driver", "call", "get_agent_cursor_state"]
    assert json.loads(argv[3]) == {}


def test_get_config_builds_correct_call(monkeypatch):
    seen = _patch(monkeypatch, stdout="{}")
    mcp_server.computer_get_config()
    argv = seen["argv"]
    assert argv[:3] == ["cua-driver", "call", "get_config"]
    assert json.loads(argv[3]) == {}


def test_set_config_builds_correct_call(monkeypatch):
    # Test Swift-style key/value write
    seen = _patch(monkeypatch, stdout="{}")
    mcp_server.computer_set_config(key="capture_mode", value="som")
    argv = seen["argv"]
    assert argv[:3] == ["cua-driver", "call", "set_config"]
    assert json.loads(argv[3]) == {"key": "capture_mode", "value": "som"}

    # Test legacy per-field writes
    seen = _patch(monkeypatch, stdout="{}")
    mcp_server.computer_set_config(
        capture_mode="vision",
        max_image_dimension=1024,
        experimental_pip=True,
        experimental_pip_geometry="480x360"
    )
    argv = seen["argv"]
    assert argv[:3] == ["cua-driver", "call", "set_config"]
    assert json.loads(argv[3]) == {
        "capture_mode": "vision",
        "max_image_dimension": 1024,
        "experimental_pip": True,
        "experimental_pip_geometry": "480x360"
    }


def test_check_permissions_builds_correct_call(monkeypatch):
    seen = _patch(monkeypatch, stdout="{}")
    mcp_server.computer_check_permissions()
    argv = seen["argv"]
    assert argv[:3] == ["cua-driver", "call", "check_permissions"]
    assert json.loads(argv[3]) == {}


def test_check_for_update_builds_correct_call(monkeypatch):
    seen = _patch(monkeypatch, stdout="{}")
    mcp_server.computer_check_for_update()
    argv = seen["argv"]
    assert argv[:3] == ["cua-driver", "call", "check_for_update"]
    assert json.loads(argv[3]) == {}

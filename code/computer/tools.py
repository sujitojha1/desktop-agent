"""Computer-Use skill — named tool surface over cua.Localhost.

Closes the "34-tool surface" gap recorded in #3 (issue #4): instead of an
inline if/elif dispatcher, every desktop operation is a **named tool** with a
JSON-schema and a handler. The registry is enumerable (`list_tools()`) so the
agent / gateway can discover the surface, and `ComputerSkill` executes both its
deterministic and vision cascades through `run_tool()`.

This wraps the async `cua.Localhost` SDK plus the synchronous `cua_auto`
window/screen helpers (wrapped in `asyncio.to_thread`). It is **not** the Rust
`cua-driver`'s 34-tool socket surface — several of those tools need an
accessibility tree / CDP / agent-cursor overlay / recording subsystem the SDK
does not expose.

Coverage vs cua-driver's 34 tools (verified 2026-06-14, Windows 11):

  MAPPED / SYNTHESISED (implemented here):
    get_screen_size       -> screen.size
    get_cursor_position   -> cua_auto.screen.cursor_position
    click/double_click/right_click/move -> mouse.*
    drag                  -> mouse.drag
    scroll                -> mouse.scroll
    type   (type_text)    -> keyboard.type
    key    (press_key)    -> keyboard.keypress(str)
    hotkey                -> keyboard.keypress(list)
    launch (launch_app)   -> shell.run("start ...")
    kill_app              -> shell.run("taskkill ...")
    bring_to_front        -> cua_auto.window.activate_window + maximize_window
    list_windows          -> cua_auto.window.get_windows_with_title (title-scoped)
    zoom                  -> crop of screen.screenshot
    screenshot (get_window_state, screenshot mode) -> screen.screenshot_base64
    wait                  -> asyncio.sleep  (utility; not a cua-driver tool)

  N/A — needs a layer the SDK lacks (tracked in #3, out of scope for #4):
    get_accessibility_tree / get_window_state (AX)          -> needs AX/UIA
    set_value                                               -> needs AX/UIA targeting
    list_apps                                               -> needs app enumeration
    page (CDP DOM)                                          -> needs CDP
    start/end_session + agent-cursor overlay (x7)           -> not in SDK
    start/stop/get_recording, replay_trajectory (x4)        -> needs recording layer
    get_config/set_config/check_permissions/check_for_update-> N/A for SDK
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


# ── execution context ────────────────────────────────────────────────────────
@dataclass
class ToolContext:
    """Carries the live cua.Localhost host and the screenshot→logical click
    scale into every tool handler. `scale_*` is 1.0 on a non-HiDPI display;
    coordinate tools multiply incoming (x, y) by it before acting."""
    host: Any
    scale_x: float = 1.0
    scale_y: float = 1.0
    launched_pids: set[int] = field(default_factory=set)

    def scaled_xy(self, x, y, *, default_center: bool = False):
        if x is None or y is None:
            return (0, 0) if default_center else (None, None)
        return int(round(x * self.scale_x)), int(round(y * self.scale_y))


Handler = Callable[[ToolContext, dict], Awaitable[str]]


@dataclass
class ToolSpec:
    name: str
    description: str
    schema: dict          # JSON-schema for this tool's arguments
    handler: Handler


TOOLS: dict[str, ToolSpec] = {}


def _reg(name: str, description: str, schema: dict, handler: Handler) -> None:
    TOOLS[name] = ToolSpec(name, description, schema, handler)


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "additionalProperties": False,
            "properties": props, "required": required or []}


_XY = {"x": {"type": "integer"}, "y": {"type": "integer"}}
_BTN = {"button": {"type": "string", "enum": ["left", "right", "middle"]}}


# ── enumeration / dispatch entry points ──────────────────────────────────────
def list_tools() -> list[dict]:
    """The discoverable tool surface: name + description + arg schema."""
    return [{"name": t.name, "description": t.description, "schema": t.schema}
            for t in TOOLS.values()]


def tool_names() -> list[str]:
    return list(TOOLS.keys())


async def run_tool(name: str, args: dict | None, ctx: ToolContext) -> str:
    """Look up `name` and execute its handler. Returns an outcome string
    ("ok" / "ok: …" / "error: …"); never raises — handler exceptions are
    folded into an error string so the cascade loop can record and continue."""
    if args is None:
        args = {}
    else:
        args = dict(args)

    # Normalize tool name aliases
    name = name.lower().strip()
    aliases = {
        "keys": "type",
        "type_text": "type",
        "write": "type",
        "text": "type",
        "press": "key",
        "press_key": "key",
        "launch_app": "launch",
        "open": "launch",
        "kill": "kill_app",
        "click_at": "click",
        "double_click_at": "double_click",
        "right_click_at": "right_click",
    }
    name = aliases.get(name, name)

    # Normalize argument key aliases
    if name == "type":
        val = args.get("value") or args.get("keys") or args.get("text") or ""
        if isinstance(val, list):
            val = "".join(str(v) for v in val)
        args["value"] = val
    elif name == "key":
        val = args.get("value") or args.get("keys") or args.get("key") or ""
        if isinstance(val, list):
            val = val[0] if val else ""
        args["value"] = val

        # If it's a multi-character sequence and not a special key, treat it as a type tool!
        val_str = str(val)
        special_keys = {
            "enter", "return", "escape", "esc", "tab", "backspace", "space", "delete", "del",
            "insert", "ins", "home", "end", "pageup", "pgup", "pagedown", "pgdn", "up", "down",
            "left", "right", "f1", "f2", "f3", "f4", "f5", "f6", "f7", "f8", "f9", "f10", "f11", "f12",
            "ctrl", "alt", "shift", "win", "capslock", "numlock", "scrolllock", "pause", "printscreen"
        }
        if len(val_str) > 1 and val_str.lower() not in special_keys:
            name = "type"
            args["value"] = val_str
    elif name == "hotkey":
        val = args.get("keys") or args.get("value") or []
        if isinstance(val, str):
            val = [val]
        args["keys"] = val
    elif name in ("launch", "kill_app"):
        args["app"] = args.get("app") or args.get("value") or ""

    spec = TOOLS.get(name)
    if not spec:
        return f"error: unknown tool {name!r}"
    try:
        return await spec.handler(ctx, args)
    except Exception as e:                                     # noqa: BLE001
        return f"error: {type(e).__name__}: {e}"


# ── shared helper (also exposed as the bring_to_front tool) ───────────────────
async def front_and_maximize(title_hint: str) -> str:
    """Bring every window matching `title_hint` to the front and maximize it via
    the synchronous `cua_auto` backend (the localhost wrapper has no activate/
    maximize). Two passes — the first realizes/raises the window, the second
    makes the maximize stick once it is foregrounded. `activate_window` often
    returns False under Windows' foreground-lock, but `maximize_window` still
    raises the window to the top."""
    if not title_hint:
        return "error: bring_to_front needs a title"

    def _do() -> str:
        import time as _t
        try:
            import cua_auto
        except Exception as e:                                # noqa: BLE001
            return f"error: cua_auto unavailable: {e}"
        try:
            handles = cua_auto.window.get_windows_with_title(title_hint) or []
        except Exception as e:                                # noqa: BLE001
            return f"error: get_windows_with_title failed: {e}"
        if not handles:
            return f"error: no window matched {title_hint!r}"
        activated = maximized = 0
        for _pass in range(2):
            for h in handles:
                try:
                    if cua_auto.window.activate_window(h):
                        activated += 1
                    if cua_auto.window.maximize_window(h):
                        maximized += 1
                except Exception:                             # noqa: BLE001
                    pass
            _t.sleep(0.4)
        return (f"ok: front+maximize {title_hint!r}: {len(handles)} window(s), "
                f"activated={activated}, maximized={maximized}")

    return await asyncio.to_thread(_do)


# ── handlers ─────────────────────────────────────────────────────────────────
async def _click(ctx, a):
    x, y = ctx.scaled_xy(a.get("x"), a.get("y"))
    if x is None:
        return "error: click needs x,y"
    await ctx.host.mouse.click(x, y, a.get("button", "left"))
    return "ok"


async def _double_click(ctx, a):
    x, y = ctx.scaled_xy(a.get("x"), a.get("y"))
    if x is None:
        return "error: double_click needs x,y"
    await ctx.host.mouse.double_click(x, y)
    return "ok"


async def _right_click(ctx, a):
    x, y = ctx.scaled_xy(a.get("x"), a.get("y"))
    if x is None:
        return "error: right_click needs x,y"
    await ctx.host.mouse.right_click(x, y)
    return "ok"


async def _move(ctx, a):
    x, y = ctx.scaled_xy(a.get("x"), a.get("y"))
    if x is None:
        return "error: move needs x,y"
    await ctx.host.mouse.move(x, y)
    return "ok"


async def _drag(ctx, a):
    fx, fy = ctx.scaled_xy(a.get("from_x"), a.get("from_y"))
    tx, ty = ctx.scaled_xy(a.get("to_x"), a.get("to_y"))
    if None in (fx, fy, tx, ty):
        return "error: drag needs from_x,from_y,to_x,to_y"
    await ctx.host.mouse.drag(fx, fy, tx, ty, a.get("button", "left"))
    return "ok"


async def _scroll(ctx, a):
    x, y = ctx.scaled_xy(a.get("x"), a.get("y"), default_center=True)
    await ctx.host.mouse.scroll(x, y, int(a.get("dx", 0)), int(a.get("dy", 3)))
    return "ok"


async def _type(ctx, a):
    await ctx.host.keyboard.type(str(a.get("value", "")))
    return "ok"


async def _key(ctx, a):
    await ctx.host.keyboard.keypress(str(a.get("value", "Enter")))
    return "ok"


async def _hotkey(ctx, a):
    keys = a.get("keys") or []
    if not keys:
        return "error: hotkey needs keys"
    await ctx.host.keyboard.keypress([str(k) for k in keys])
    return "ok"


async def _launch(ctx, a):
    app = str(a.get("app", "") or a.get("value", ""))
    if not app:
        return "error: launch needs app"
    cmd = f"powershell -NoProfile -Command \"(Start-Process '{app}' -PassThru).Id\""
    res = await ctx.host.shell.run(cmd, timeout=15)
    stdout = getattr(res, "stdout", "").strip()
    pid_str = ""
    if stdout.isdigit():
        pid = int(stdout)
        ctx.launched_pids.add(pid)
        pid_str = f" (PID {pid})"
    await asyncio.sleep(1.0)
    return f"ok: launched {app}{pid_str}"


async def _kill_app(ctx, a):
    name = str(a.get("app", "") or a.get("value", ""))
    if not name:
        return "error: kill_app needs app"
    image = name if name.lower().endswith(".exe") else name + ".exe"
    await ctx.host.shell.run(f'taskkill /IM "{image}" /F', timeout=15)
    return f"ok: killed {image}"


async def _bring_to_front(ctx, a):
    return await front_and_maximize(str(a.get("title", "") or a.get("value", "")))


def _parse_ps_json(stdout: str) -> list[dict]:
    """PowerShell `ConvertTo-Json` emits a bare object for one item, an array
    for many, and empty/whitespace for none. Normalise all three to a list."""
    s = (stdout or "").strip()
    if not s:
        return []
    try:
        data = json.loads(s)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    return []


async def enumerate_windows(host, title: str | None = None) -> list[dict]:
    """All visible top-level windows as ``[{"pid": int, "title": str}]``, via
    the SDK shell (`Get-Process`) — no win32 code, no new dependency. Optional
    case-insensitive `title` substring filter. Windows-only (PowerShell); on
    other platforms returns [] so the caller can branch."""
    ps = ("Get-Process | Where-Object {$_.MainWindowTitle} | "
          "Select-Object Id,MainWindowTitle | ConvertTo-Json -Compress")
    res = await host.shell.run(f'powershell -NoProfile -Command "{ps}"', timeout=20)
    rows = _parse_ps_json(getattr(res, "stdout", "") or "")
    wins = [{"pid": r.get("Id"), "title": r.get("MainWindowTitle", "") or ""}
            for r in rows]
    if title:
        t = title.lower()
        wins = [w for w in wins if t in w["title"].lower()]
    return wins


async def _list_windows(ctx, a):
    title = a.get("title") or None
    wins = await enumerate_windows(ctx.host, title)
    suffix = f" matching {title!r}" if title else ""
    return f"ok: {len(wins)} window(s){suffix}: {json.dumps(wins)}"


async def _get_screen_size(ctx, a):
    w, h = await ctx.host.screen.size()
    return f"ok: {w}x{h}"


async def _get_cursor_position(ctx, a):
    def _do():
        import cua_auto
        return cua_auto.screen.cursor_position()

    x, y = await asyncio.to_thread(_do)
    return f"ok: cursor at {x},{y}"


async def _screenshot(ctx, a):
    b64 = await ctx.host.screen.screenshot_base64()
    return f"ok: screenshot {len(b64)} b64 chars"


async def _zoom(ctx, a):
    """Crop a region of the screen — the SoM/vision substitute for cua-driver's
    `zoom`. Region is `x,y,w,h` in screenshot pixels; writes to `out_file` if
    given, else just reports the crop size."""
    import base64
    import io
    from PIL import Image

    raw = base64.b64decode(await ctx.host.screen.screenshot_base64())
    im = Image.open(io.BytesIO(raw))
    x, y = int(a.get("x", 0)), int(a.get("y", 0))
    w, h = int(a.get("w", im.width)), int(a.get("h", im.height))
    crop = im.crop((x, y, min(x + w, im.width), min(y + h, im.height)))
    out = a.get("out_file")
    if out:
        crop.save(out)
        return f"ok: zoom {crop.size[0]}x{crop.size[1]} -> {out}"
    return f"ok: zoom {crop.size[0]}x{crop.size[1]}"


async def _wait(ctx, a):
    await asyncio.sleep(float(a.get("seconds", 0.5)))
    return "ok"


# ── registry ─────────────────────────────────────────────────────────────────
_reg("click", "Left-click the pixel (x, y).", _obj({**_XY, **_BTN}, ["x", "y"]), _click)
_reg("double_click", "Double-click the pixel (x, y).", _obj(_XY, ["x", "y"]), _double_click)
_reg("right_click", "Right-click the pixel (x, y).", _obj(_XY, ["x", "y"]), _right_click)
_reg("move", "Move the cursor to (x, y) without clicking.", _obj(_XY, ["x", "y"]), _move)
_reg("drag", "Press at (from_x, from_y) and release at (to_x, to_y).",
     _obj({"from_x": {"type": "integer"}, "from_y": {"type": "integer"},
           "to_x": {"type": "integer"}, "to_y": {"type": "integer"}, **_BTN},
          ["from_x", "from_y", "to_x", "to_y"]), _drag)
_reg("scroll", "Scroll at (x, y); dy>0 scrolls down.",
     _obj({**_XY, "dx": {"type": "integer"}, "dy": {"type": "integer"}}), _scroll)
_reg("type", "Type a string at the current focus.",
     _obj({"value": {"type": "string"}}, ["value"]), _type)
_reg("key", "Press one key, e.g. 'Enter', 'Escape', 'Tab', '='.",
     _obj({"value": {"type": "string"}}, ["value"]), _key)
_reg("hotkey", "Press a chord, e.g. ['ctrl','s'] or ['alt','F4'].",
     _obj({"keys": {"type": "array", "items": {"type": "string"}}}, ["keys"]), _hotkey)
_reg("launch", "Launch an app by name, e.g. 'calc', 'notepad'.",
     _obj({"app": {"type": "string"}, "value": {"type": "string"}}), _launch)
_reg("kill_app", "Force-terminate an app by image name, e.g. 'CalculatorApp'.",
     _obj({"app": {"type": "string"}, "value": {"type": "string"}}), _kill_app)
_reg("bring_to_front", "Activate + maximize every window whose title matches.",
     _obj({"title": {"type": "string"}, "value": {"type": "string"}}), _bring_to_front)
_reg("list_windows",
     "List all visible top-level windows (pid + title). Optional `title` "
     "filters case-insensitively.",
     _obj({"title": {"type": "string"}}), _list_windows)
_reg("get_screen_size", "Return the logical screen size (w, h).", _obj({}), _get_screen_size)
_reg("get_cursor_position", "Return the current cursor (x, y).", _obj({}), _get_cursor_position)
_reg("screenshot", "Capture the screen (base64 PNG) — perception for the vision layer.",
     _obj({}), _screenshot)
_reg("zoom", "Crop a screen region (x, y, w, h) in pixels; optional out_file.",
     _obj({**_XY, "w": {"type": "integer"}, "h": {"type": "integer"},
           "out_file": {"type": "string"}}), _zoom)
_reg("wait", "Pause for `seconds` to let the UI settle.",
     _obj({"seconds": {"type": "number"}}), _wait)

"""Computer-Use skill — named tool surface over the cua-driver daemon.

Every desktop operation is a **named tool** with a JSON schema and a handler that
calls one `cua-driver` tool through :mod:`computer.driver`. The registry is
enumerable (`list_tools()`), and `ComputerSkill` runs its whole cascade through
`run_tool()`. There is no `cua` Python SDK and no PowerShell here — the daemon is
the only backend (see computer/driver.py and docs/cua_driver_tools.md).

Addressing follows the driver's model: actions target a window by ``(pid,
window_id)`` carried on the :class:`ToolContext`, and address a control by
``element_index`` (from the last `get_window_state` scan) or, as a fallback for
canvas / custom-drawn surfaces, by window-local ``x, y`` pixels — the same pixel
space the scan screenshot is in, so no coordinate scaling is needed.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from . import driver
from .app_registry import list_apps as _registry_apps
from .app_registry import resolve_app

# How long to let a freshly-launched window realize before reconciling it against
# the live window list (stub shims and UWP frame hosts need a moment).
_LAUNCH_SETTLE_S = 2.0


# ── execution context ────────────────────────────────────────────────────────
@dataclass
class ToolContext:
    """The window every tool acts on: ``pid`` + ``window_id`` (set by the loop
    after launch / window selection), plus the driver ``session`` and the set of
    PIDs this run launched (for teardown). An action dict may override `pid` /
    `window_id` inline; otherwise the context's are used."""
    pid: int | None = None
    window_id: int | None = None
    session: str | None = None
    launched_pids: set[int] = field(default_factory=set)

    def target(self, a: dict) -> dict:
        """Base ``{pid, window_id}`` for a driver call — action keys win over the
        context, and zero/None values are dropped so the driver auto-resolves."""
        out: dict[str, int] = {}
        pid = a.get("pid", self.pid)
        wid = a.get("window_id", self.window_id)
        if pid:
            out["pid"] = int(pid)
        if wid:
            out["window_id"] = int(wid)
        if self.session:
            out["session"] = self.session  # type: ignore[assignment]
        return out


Handler = Callable[["ToolContext", dict], Awaitable[str]]


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


# Action names the model may emit that map onto a canonical tool. Kept small and
# data-driven — the prompt teaches the canonical names; this only absorbs the
# common synonyms so a near-miss isn't a hard failure.
_ALIASES = {
    "type_text": "type", "write": "type", "text": "type",
    "press": "key", "press_key": "key",
    "launch_app": "launch", "open": "launch",
    "kill": "kill_app",
    "get_window_state": "scan",
}


def _outcome(res: dict, ok: str = "ok") -> str:
    """Fold a driver response into the cascade's outcome-string contract."""
    if driver.is_error(res):
        return f"error: {driver.error_text(res)}"
    return ok


# ── enumeration / dispatch entry points ──────────────────────────────────────
def list_tools() -> list[dict]:
    """The discoverable tool surface: name + description + arg schema."""
    return [{"name": t.name, "description": t.description, "schema": t.schema}
            for t in TOOLS.values()]


def tool_names() -> list[str]:
    return list(TOOLS.keys())


async def run_tool(name: str, args: dict | None, ctx: ToolContext) -> str:
    """Look up `name` (after alias folding) and run its handler. Returns an
    outcome string ("ok" / "ok: …" / "error: …"); never raises — handler
    exceptions become an error string so the cascade can record and continue."""
    args = dict(args or {})
    name = _ALIASES.get(name.lower().strip(), name.lower().strip())
    spec = TOOLS.get(name)
    if not spec:
        return f"error: unknown tool {name!r}"
    try:
        return await spec.handler(ctx, args)
    except Exception as e:                                     # noqa: BLE001
        return f"error: {type(e).__name__}: {e}"


# ── pointer tools (element_index- or pixel-addressed) ─────────────────────────
def _pointer(tool: str, extras: tuple[str, ...] = ()) -> Handler:
    """Build a click-family handler. Addresses by `element_index` when given,
    else window-local `x, y`; forwards the named `extras` (button/count/…) when
    present. Single implementation for click / double_click / right_click."""
    async def handler(ctx: ToolContext, a: dict) -> str:
        args = ctx.target(a)
        if "pid" not in args:
            return f"error: {tool} needs a target pid"
        ei = a.get("element_index")
        if ei is not None and int(ei) >= 0:
            args["element_index"] = int(ei)
        elif a.get("x") is not None and a.get("y") is not None:
            args["x"], args["y"] = a["x"], a["y"]
        else:
            return f"error: {tool} needs element_index or x,y"
        for k in (*extras, "from_zoom", "dispatch"):
            if a.get(k) is not None:
                args[k] = a[k]
        return _outcome(await driver.acall(tool, args))
    return handler


# ── keyboard / value tools ────────────────────────────────────────────────────
async def _type(ctx: ToolContext, a: dict) -> str:
    text = a.get("value")
    if text is None:
        text = a.get("text", "")
    args = ctx.target(a)
    if "pid" not in args:
        return "error: type needs a target pid"
    args["text"] = str(text)
    if a.get("element_index") is not None and int(a["element_index"]) >= 0:
        args["element_index"] = int(a["element_index"])
    return _outcome(await driver.acall("type_text", args))


async def _key(ctx: ToolContext, a: dict) -> str:
    key = a.get("value") or a.get("key") or ""
    args = ctx.target(a)
    if "pid" not in args:
        return "error: key needs a target pid"
    if not key:
        return "error: key needs a key name"
    args["key"] = str(key)
    if a.get("modifiers"):
        args["modifiers"] = list(a["modifiers"])
    return _outcome(await driver.acall("press_key", args))


async def _hotkey(ctx: ToolContext, a: dict) -> str:
    keys = a.get("keys") or ([a["value"]] if a.get("value") else [])
    args = ctx.target(a)
    if "pid" not in args:
        return "error: hotkey needs a target pid"
    if not keys or len(keys) < 2:
        return "error: hotkey needs at least a modifier + a key"
    args["keys"] = [str(k) for k in keys]
    return _outcome(await driver.acall("hotkey", args))


async def _set_value(ctx: ToolContext, a: dict) -> str:
    args = ctx.target(a)
    if "pid" not in args or "window_id" not in args:
        return "error: set_value needs pid and window_id"
    if a.get("element_index") is None:
        return "error: set_value needs element_index"
    args["element_index"] = int(a["element_index"])
    args["value"] = str(a.get("value", ""))
    return _outcome(await driver.acall("set_value", args))


async def _scroll(ctx: ToolContext, a: dict) -> str:
    args = ctx.target(a)
    if "pid" not in args:
        return "error: scroll needs a target pid"
    direction = a.get("direction")
    if direction not in ("up", "down", "left", "right"):
        return "error: scroll needs direction up/down/left/right"
    args["direction"] = direction
    if a.get("amount") is not None:
        args["amount"] = int(a["amount"])
    if a.get("by"):
        args["by"] = a["by"]
    return _outcome(await driver.acall("scroll", args))


async def _drag(ctx: ToolContext, a: dict) -> str:
    args = ctx.target(a)
    if "pid" not in args:
        return "error: drag needs a target pid"
    for k in ("from_x", "from_y", "to_x", "to_y"):
        if a.get(k) is None:
            return "error: drag needs from_x,from_y,to_x,to_y"
        args[k] = a[k]
    for k in ("button", "duration_ms", "steps", "from_zoom"):
        if a.get(k) is not None:
            args[k] = a[k]
    return _outcome(await driver.acall("drag", args))


# ── launch / lifecycle ────────────────────────────────────────────────────────
def _launch_args(app: str) -> dict:
    """Turn a friendly app name into `launch_app` args via the registry. The
    registry entry names the launch field explicitly (aumid/path/name); an
    unconfigured name falls through as `name` (driver does AppsFolder→PATH)."""
    entry = resolve_app(app)
    if entry is None:
        return {"name": app}
    return entry.launch_args()


async def launch_app(app: str, *, session: str | None = None) -> dict:
    """Launch `app` and return ``{pid, window_id, windows, raw}``.

    `launch_app`'s own pid/window can't be trusted as the interaction target:
    a `.cmd`/packaged shim returns pid:0 with no windows, and UWP apps
    (Calculator, Notepad) are launched by an activation pid that doesn't own the
    visible window — that's a frame-host process found only via `list_windows`.
    So when the entry declares a `window_title`, we settle briefly and reconcile
    against the live window list by that title (preferring on-screen windows),
    falling back to whatever `launch_app` reported. Shared by the `launch` tool
    and the skill's setup launch."""
    if not app:
        return {"error": "launch needs an app"}
    entry = resolve_app(app)
    args = _launch_args(app)
    if session:
        args["session"] = session
    res = await driver.acall("launch_app", args)
    if driver.is_error(res):
        return res

    pid = res.get("pid") or 0
    windows = res.get("windows") or []
    title = (entry.window_title if entry else app) or app

    # Reconcile against the live window list when we have a title to match, or
    # when launch_app gave us nothing usable to act on.
    if title or not pid or not windows:
        await asyncio.sleep(_LAUNCH_SETTLE_S)
        listed = await driver.acall("list_windows")
        matches = [w for w in (listed.get("windows") or [])
                   if title and title.lower() in str(w.get("title", "")).lower()]
        matches.sort(key=lambda w: not w.get("is_on_screen"))  # on-screen first
        if matches:
            windows = matches
            pid = matches[0].get("pid") or pid

    window_id = windows[0].get("window_id") if windows else None
    return {"pid": pid or None, "window_id": window_id,
            "windows": windows, "raw": res}


async def _launch(ctx: ToolContext, a: dict) -> str:
    app = str(a.get("app") or a.get("value") or a.get("name") or "")
    if not app:
        return "error: launch needs app"
    res = await launch_app(app, session=ctx.session)
    if driver.is_error(res):
        return f"error: {driver.error_text(res)}"
    pid, wid = res.get("pid"), res.get("window_id")
    if pid:
        ctx.pid = pid
        ctx.launched_pids.add(pid)
    if wid:
        ctx.window_id = wid
    return f"ok: launched {app} (pid {pid}, window {wid})"


async def _kill_app(ctx: ToolContext, a: dict) -> str:
    pid = a.get("pid", ctx.pid)
    if not pid:
        return "error: kill_app needs a pid"
    return _outcome(await driver.acall("kill_app", {"pid": int(pid)}),
                    ok=f"ok: killed pid {pid}")


async def _bring_to_front(ctx: ToolContext, a: dict) -> str:
    args = ctx.target(a)
    if "pid" not in args:
        return "error: bring_to_front needs a pid"
    return _outcome(await driver.acall("bring_to_front", args),
                    ok=f"ok: fronted pid {args['pid']}")


# ── discovery / perception ────────────────────────────────────────────────────
async def enumerate_windows(title: str | None = None,
                            pid: int | None = None) -> list[dict]:
    """All top-level windows as ``[{"pid", "window_id", "title", ...}]`` via the
    driver's `list_windows`. Optional case-insensitive `title` substring filter
    and `pid` scope. Returns [] on driver error so callers can branch."""
    res = await driver.acall("list_windows", {"pid": int(pid)} if pid else {})
    wins = res.get("windows") or []
    if title:
        t = title.lower()
        wins = [w for w in wins if t in str(w.get("title", "")).lower()]
    return wins


async def _list_windows(ctx: ToolContext, a: dict) -> str:
    wins = await enumerate_windows(a.get("title"), a.get("pid"))
    suffix = f" matching {a['title']!r}" if a.get("title") else ""
    slim = [{"pid": w.get("pid"), "window_id": w.get("window_id"),
             "title": w.get("title")} for w in wins]
    return f"ok: {len(slim)} window(s){suffix}: {json.dumps(slim)}"


async def _list_apps(ctx: ToolContext, a: dict) -> str:
    """The apps configured in apps.yaml — the launchable surface the agent can
    `launch` by friendly name."""
    apps = [{"name": e.name, "window_title": e.window_title} for e in _registry_apps()]
    return f"ok: {len(apps)} app(s): {json.dumps(apps)}"


async def scan(ctx: ToolContext, *, capture_mode: str = "ax",
               query: str | None = None) -> dict:
    """Raw `get_window_state` for the context's window — the SCAN phase. Returns
    the driver dict (`element_count`, `tree_markdown`, screenshot fields)."""
    args = ctx.target({})
    if "pid" not in args or "window_id" not in args:
        return {"error": "scan needs pid and window_id"}
    args["capture_mode"] = capture_mode
    if query:
        args["query"] = query
    return await driver.acall("get_window_state", args)


async def _scan(ctx: ToolContext, a: dict) -> str:
    res = await scan(ctx, capture_mode=a.get("capture_mode", "ax"),
                     query=a.get("query"))
    if driver.is_error(res):
        return f"error: {driver.error_text(res)}"
    return f"ok: {res.get('element_count', 0)} element(s)"


async def _get_screen_size(ctx: ToolContext, a: dict) -> str:
    res = await driver.acall("get_screen_size")
    if driver.is_error(res):
        return f"error: {driver.error_text(res)}"
    return f"ok: {res.get('width')}x{res.get('height')}"


async def _get_cursor_position(ctx: ToolContext, a: dict) -> str:
    res = await driver.acall("get_cursor_position")
    if driver.is_error(res):
        return f"error: {driver.error_text(res)}"
    return f"ok: cursor at {res.get('x')},{res.get('y')}"


async def _zoom(ctx: ToolContext, a: dict) -> str:
    args = ctx.target(a)
    if "pid" not in args or "window_id" not in args:
        return "error: zoom needs pid and window_id"
    for k in ("x1", "y1", "x2", "y2"):
        if a.get(k) is None:
            return "error: zoom needs x1,y1,x2,y2"
        args[k] = a[k]
    return _outcome(await driver.acall("zoom", args), ok="ok: zoomed")


async def _wait(ctx: ToolContext, a: dict) -> str:
    await asyncio.sleep(float(a.get("seconds", 0.5)))
    return "ok"


# ── schemas ────────────────────────────────────────────────────────────────────
_ADDR = {"element_index": {"type": "integer"},
         "x": {"type": "integer"}, "y": {"type": "integer"}}
_BTN = {"button": {"type": "string", "enum": ["left", "right", "middle"]}}


# ── registry ─────────────────────────────────────────────────────────────────
_reg("scan", "Scan the target window's UIA tree; tags actionable controls with "
     "[element_index N]. capture_mode ax|som|vision; optional query filter.",
     _obj({"capture_mode": {"type": "string", "enum": ["ax", "som", "vision"]},
           "query": {"type": "string"}}), _scan)
_reg("click", "Click a control by element_index (preferred) or window-local x,y.",
     _obj({**_ADDR, **_BTN, "count": {"type": "integer"}}), _pointer("click", ("button", "count")))
_reg("double_click", "Double-click a control by element_index or x,y.",
     _obj(_ADDR), _pointer("double_click"))
_reg("right_click", "Right-click a control by element_index or x,y.",
     _obj(_ADDR), _pointer("right_click"))
_reg("type", "Type text into the focused control (or element_index if given).",
     _obj({"value": {"type": "string"}, "element_index": {"type": "integer"}},
          ["value"]), _type)
_reg("key", "Press one key, e.g. 'enter', 'escape', 'tab'. Optional modifiers[].",
     _obj({"value": {"type": "string"},
           "modifiers": {"type": "array", "items": {"type": "string"}}},
          ["value"]), _key)
_reg("hotkey", "Press a chord, e.g. ['ctrl','s'] or ['alt','F4'].",
     _obj({"keys": {"type": "array", "items": {"type": "string"}}}, ["keys"]), _hotkey)
_reg("set_value", "Set a UIA element's value directly (text field / slider / combo).",
     _obj({"element_index": {"type": "integer"}, "value": {"type": "string"}},
          ["element_index", "value"]), _set_value)
_reg("scroll", "Scroll the window. direction up|down|left|right; optional amount.",
     _obj({"direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
           "amount": {"type": "integer"}, "by": {"type": "string", "enum": ["line", "page"]}},
          ["direction"]), _scroll)
_reg("drag", "Drag from (from_x,from_y) to (to_x,to_y) in window-local pixels.",
     _obj({"from_x": {"type": "integer"}, "from_y": {"type": "integer"},
           "to_x": {"type": "integer"}, "to_y": {"type": "integer"}, **_BTN},
          ["from_x", "from_y", "to_x", "to_y"]), _drag)
_reg("launch", "Launch an app by friendly name (resolved via apps.yaml).",
     _obj({"app": {"type": "string"}, "value": {"type": "string"}}), _launch)
_reg("kill_app", "Force-terminate the target app by pid.",
     _obj({"pid": {"type": "integer"}}), _kill_app)
_reg("bring_to_front", "Raise the target window to the foreground.",
     _obj({"pid": {"type": "integer"}, "window_id": {"type": "integer"}}), _bring_to_front)
_reg("list_windows", "List top-level windows (pid + window_id + title). "
     "Optional title filter / pid scope.",
     _obj({"title": {"type": "string"}, "pid": {"type": "integer"}}), _list_windows)
_reg("list_apps", "List the apps configured in apps.yaml that `launch` can start.",
     _obj({}), _list_apps)
_reg("get_screen_size", "Return the logical screen size (w, h).", _obj({}), _get_screen_size)
_reg("get_cursor_position", "Return the current cursor (x, y).", _obj({}), _get_cursor_position)
_reg("zoom", "Zoom a window region (x1,y1,x2,y2 in screenshot pixels) to native res.",
     _obj({"x1": {"type": "integer"}, "y1": {"type": "integer"},
           "x2": {"type": "integer"}, "y2": {"type": "integer"}},
          ["x1", "y1", "x2", "y2"]), _zoom)
_reg("wait", "Pause for `seconds` to let the UI settle.",
     _obj({"seconds": {"type": "number"}}), _wait)

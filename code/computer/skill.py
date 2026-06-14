"""Session 9: the Computer-Use skill — desktop driver via the cua-driver daemon.

The wrapper translates the orchestrator's NodeSpec contract into the typed
ComputerOutput / AgentResult contract, mirroring code/browser/skill.py. Every
desktop operation goes through the `cua-driver` daemon (see computer/driver.py
and docs/cua_driver_tools.md) — there is no `cua` Python SDK and no PowerShell.

It owns a small cascade over the *real* desktop:

    Layer 1 — deterministic : launch an app + caller-supplied tool steps
                              (metadata.actions), dispatched by name through the
                              tool registry.
    Layer 3 — hybrid drive  : per turn, SCAN the target window with
                              `get_window_state`. When the UIA tree has elements,
                              decide the next action over the *text* tree and
                              address controls by `element_index` (path="a11y").
                              When the tree is empty (canvas / game / opaque
                              app), fall back to the per-window screenshot and let
                              the vision model emit window-local pixels
                              (path="vision"). This is the driver guide's
                              recommended architecture.

Every LLM/vision call routes through the V9 gateway tagged `agent="computer"`,
exactly as BrowserSkill tags `agent="browser"`.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from schemas import AgentResult, NodeSpec

# The V9 gateway client is generic — reuse the one shipped with Browser.
from browser.client import V9Client

from . import driver
from .app_registry import resolve_app
from .tools import (
    ToolContext,
    enumerate_windows,
    launch_app,
    run_tool,
    scan,
)


# ─── action vocabulary ───────────────────────────────────────────────────────
# One schema covers both decision paths. On the UIA (a11y) path the model
# addresses controls by `element_index` from the scan; on the vision fallback it
# uses window-local `x, y` pixels (the same space as the scan screenshot). The
# skill always targets the active (pid, window_id) for the run, so the model
# never emits those.
ACTION_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["thinking", "actions"],
    "properties": {
        "thinking": {"type": "string", "description": "1–2 sentences of reasoning"},
        "actions": {
            "type": "array",
            "minItems": 1,
            "maxItems": 3,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["type"],
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["click", "double_click", "right_click", "type",
                                 "key", "hotkey", "set_value", "scroll", "drag",
                                 "launch", "wait", "done"],
                    },
                    "element_index": {"type": "integer"},
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                    "from_x": {"type": "integer"},
                    "from_y": {"type": "integer"},
                    "to_x": {"type": "integer"},
                    "to_y": {"type": "integer"},
                    "button": {"type": "string"},
                    "count": {"type": "integer"},
                    "value": {"type": "string"},
                    "keys": {"type": "array", "items": {"type": "string"}},
                    "modifiers": {"type": "array", "items": {"type": "string"}},
                    "direction": {"type": "string"},
                    "amount": {"type": "integer"},
                    "app": {"type": "string"},
                    "seconds": {"type": "number"},
                    "success": {"type": "boolean"},
                    "note": {"type": "string"},
                },
            },
        },
    },
}

SYSTEM_PROMPT_TREE = (
    "You are a desktop-driving agent operating one Windows app window through its "
    "accessibility (UIA) tree. Each turn you receive the window's element tree: "
    "every actionable control is tagged `[element_index N]` with its role and "
    "label. Address controls by that index — it is stable, focus-free, and exact. "
    "Make progress toward the goal by emitting a short list of actions:\n"
    "  click {element_index}            — invoke a control\n"
    "  double_click / right_click {element_index}\n"
    "  type {value}                     — type into the focused field (optionally element_index)\n"
    "  set_value {element_index, value} — set a field/slider/combo directly\n"
    "  key {value}                      — one key: 'enter','escape','tab',…\n"
    "  hotkey {keys}                    — chord, e.g. ['ctrl','s']\n"
    "  scroll {direction, amount?}      — direction up/down/left/right\n"
    "  launch {app}                     — start another app by name\n"
    "  wait {seconds}                   — let the UI settle\n"
    "  done {success, note}             — finish; success=true if the goal is met\n"
    "Prefer one decisive action and re-read the next scan. Be terse in `thinking`."
)

SYSTEM_PROMPT_VISION = (
    "You are a desktop-driving agent operating one Windows app window. The UIA "
    "tree is empty (a canvas / game / custom-drawn surface), so you get a "
    "screenshot of the window instead. The image is W×H pixels, origin (0,0) at "
    "the TOP-LEFT; x grows right, y grows down. Address the screen by pixel "
    "coordinate:\n"
    "  click {x, y} / double_click / right_click {x, y}\n"
    "  drag {from_x, from_y, to_x, to_y}\n"
    "  type {value} / key {value} / hotkey {keys}\n"
    "  scroll {direction, amount?}\n"
    "  wait {seconds} / done {success, note}\n"
    "Emit one action and re-read the next screenshot. Be terse in `thinking`."
)


class ComputerOutput(BaseModel):
    """Typed payload the Computer skill writes into AgentResult.output. `path` is
    the cascade layer actually used (deterministic | a11y | vision), read by
    replay and the Planner's failure routing the same way as BrowserOutput.path."""

    goal: str
    path: str = Field(description="deterministic | a11y | vision")
    turns: int = 0
    content: str | None = None
    actions: list[dict] = Field(default_factory=list)
    final_title: str | None = None


class ComputerSkill:
    NAME = "computer"

    def __init__(self, *, gateway_url: str = "http://localhost:8109",
                 agent_tag: str = "computer",
                 vision_provider_pin: str | None = None,
                 artifacts_root: str | None = None,
                 max_steps: int = 12,
                 max_failures: int = 3,
                 pause_between_steps: float = 0.4,
                 session: str | None = None):
        self.gateway_url = gateway_url
        self.agent_tag = agent_tag
        self.vision_provider_pin = vision_provider_pin
        self.artifacts_root = Path(artifacts_root) if artifacts_root else None
        self.max_steps = max_steps
        self.max_failures = max_failures
        self.pause_between_steps = pause_between_steps
        self.session = session

    # ── public entry point ─────────────────────────────────────────────────
    async def run(self, node: NodeSpec) -> AgentResult:
        goal = node.metadata.get("goal") or (node.inputs[0] if node.inputs else "")
        if not goal:
            return self._pack_error("", "no goal given (metadata.goal or inputs[0])")
        app = node.metadata.get("app")
        det_actions = node.metadata.get("actions") or []
        max_steps = int(node.metadata.get("max_steps") or self.max_steps)

        t0 = time.time()
        session = self.session or f"computer-{int(t0)}"
        client = V9Client(base_url=self.gateway_url, agent=self.agent_tag,
                          session=session)
        artifacts_dir = (
            self.artifacts_root / f"computer_{int(t0)}" if self.artifacts_root else None
        )
        if artifacts_dir:
            artifacts_dir.mkdir(parents=True, exist_ok=True)

        ctx = ToolContext(session=session)
        prelude: list[dict] = []
        # Declare the run's session so the agent-cursor overlay is keyed to it.
        await driver.acall("start_session", {"session": session})
        try:
            # ── Layer 1: launch ────────────────────────────────────────────
            if app:
                res = await launch_app(app, session=session)
                if driver.is_error(res):
                    prelude.append({"turn": 0, "thinking": "setup: launch",
                                    "actions": [{"type": "launch", "app": app}],
                                    "outcome": f"error: {driver.error_text(res)}"})
                else:
                    ctx.pid = res.get("pid")
                    ctx.window_id = res.get("window_id")
                    if ctx.pid:
                        ctx.launched_pids.add(ctx.pid)
                    await driver.acall("bring_to_front", ctx.target({}))
                    prelude.append({"turn": 0, "thinking": "setup: launch + front",
                                    "actions": [{"type": "launch", "app": app}],
                                    "outcome": f"ok: pid {ctx.pid}, window {ctx.window_id}"})
                    await asyncio.sleep(self.pause_between_steps)

            # ── Layer 1b: deterministic steps ──────────────────────────────
            if det_actions:
                steps = prelude + await self._run_deterministic(ctx, det_actions)
                return self._pack("deterministic", goal, steps,
                                  content=(det_actions[-1].get("note") if det_actions else None),
                                  final_title=await self._window_title(ctx),
                                  elapsed=time.time() - t0)

            # ── Layer 3: hybrid drive ──────────────────────────────────────
            if not ctx.pid or not ctx.window_id:
                return self._pack_error(
                    goal, "computer skill needs a target window: pass metadata.app "
                          "to launch, or metadata.actions to set one up.",
                    elapsed=time.time() - t0)

            steps, success, note = await self._drive(
                ctx, goal, client, artifacts_dir, max_steps, prelude=prelude)
            out = self._pack(self._dominant_path(steps), goal, steps, content=note,
                             final_title=await self._window_title(ctx),
                             elapsed=time.time() - t0)
            out.success = success
            return out
        except Exception as e:                                # noqa: BLE001
            return self._pack_error(goal, f"computer skill error: {type(e).__name__}: {e}",
                                    elapsed=time.time() - t0)
        finally:
            await self._teardown(ctx, session)

    # ── hybrid drive loop ───────────────────────────────────────────────────
    async def _drive(self, ctx, goal, client, artifacts_dir, max_steps,
                     prelude: list[dict] | None = None):
        steps: list[dict] = list(prelude or [])
        failures = 0
        for turn in range(1, max_steps + 1):
            # SCAN — tree + screenshot in one call.
            state = await scan(ctx, capture_mode="som")
            if driver.is_error(state):
                # Window may have closed/relaunched — try to re-resolve it once.
                if not await self._refresh_window(ctx):
                    steps.append({"turn": turn, "thinking": "", "actions": [],
                                  "outcome": f"error: scan: {driver.error_text(state)}"})
                    failures += 1
                    if failures >= self.max_failures:
                        return steps, False, "giveup: target window unreadable"
                    continue
                state = await scan(ctx, capture_mode="som")

            element_count = int(state.get("element_count") or 0)
            if artifacts_dir:
                self._save_shot(artifacts_dir, f"turn_{turn:02d}.png", state)

            # DECIDE — tree path when UIA has elements, else vision fallback.
            if element_count > 0:
                path = "a11y"
                prompt = (f"GOAL: {goal}\n\nWINDOW ELEMENT TREE:\n"
                          f"{self._trim(state.get('tree_markdown', ''))}\n\n"
                          f"RECENT ACTIONS:\n{self._history(steps)}\n\n"
                          f"What is the next set of actions?")
                result = await client.chat(
                    prompt, system=SYSTEM_PROMPT_TREE, schema=ACTION_SCHEMA,
                    schema_name="AgentOutput", max_tokens=1024,
                    provider=self.vision_provider_pin)
            else:
                path = "vision"
                data_url = self._data_url(state)
                if not data_url:
                    steps.append({"turn": turn, "thinking": "", "actions": [],
                                  "outcome": "error: empty tree and no screenshot"})
                    failures += 1
                    if failures >= self.max_failures:
                        return steps, False, "giveup: nothing to perceive"
                    continue
                w, h = state.get("screenshot_width"), state.get("screenshot_height")
                prompt = (f"GOAL: {goal}\n\nSCREEN: {w}x{h} pixels (origin top-left)\n"
                          f"RECENT ACTIONS:\n{self._history(steps)}\n\n"
                          f"What is the next set of actions?")
                result = await client.vision(
                    data_url, prompt, system=SYSTEM_PROMPT_VISION, schema=ACTION_SCHEMA,
                    schema_name="AgentOutput", max_tokens=1024,
                    provider=self.vision_provider_pin)

            parsed = result.parsed
            if not parsed:
                steps.append({"turn": turn, "path": path, "thinking": "", "actions": [],
                              "outcome": f"error: no parsed output; raw={result.text[:120]!r}"})
                failures += 1
                if failures >= self.max_failures:
                    return steps, False, "giveup: model returned no parseable action"
                continue

            # ACT
            actions = parsed.get("actions") or []
            outcomes, done_seen, success_seen, done_note = await self._apply(ctx, actions)
            outcome_str = " | ".join(outcomes) or "ok"
            steps.append({"turn": turn, "path": path,
                          "thinking": parsed.get("thinking", ""),
                          "actions": actions, "outcome": outcome_str})

            if "error" in outcome_str:
                failures += 1
                if failures >= self.max_failures:
                    return steps, False, f"giveup after {failures} consecutive failures"
            else:
                failures = 0
            if done_seen:
                return steps, success_seen, done_note
        return steps, False, f"step cap reached ({max_steps})"

    async def _apply(self, ctx, actions):
        """Dispatch a turn's actions through the tool registry. Stops at the first
        error or `done`. Returns (outcomes, done_seen, success, note)."""
        outcomes: list[str] = []
        for a in actions:
            if a.get("type") == "done":
                ok = bool(a.get("success", False))
                outcomes.append(f"done({ok})")
                return outcomes, True, ok, a.get("note", "")
            outcome = await run_tool(a.get("type", ""), a, ctx)
            outcomes.append(outcome)
            if outcome.startswith("error"):
                break
            await asyncio.sleep(self.pause_between_steps)
        return outcomes, False, False, ""

    async def _run_deterministic(self, ctx, det_actions) -> list[dict]:
        steps: list[dict] = []
        for i, a in enumerate(det_actions, start=1):
            outcome = await run_tool(a.get("type", ""), a, ctx)
            steps.append({"turn": i, "thinking": "deterministic",
                          "actions": [a], "outcome": outcome})
            if outcome.startswith("error"):
                break
            await asyncio.sleep(self.pause_between_steps)
        return steps

    # ── helpers ───────────────────────────────────────────────────────────────
    async def _refresh_window(self, ctx) -> bool:
        """Re-resolve ctx.window_id from the live window list for ctx.pid (the
        window can change HWND after a relaunch / dialog). Returns True on success."""
        wins = await enumerate_windows(pid=ctx.pid)
        on_screen = [w for w in wins if w.get("is_on_screen")] or wins
        if on_screen:
            ctx.window_id = on_screen[0].get("window_id")
            return bool(ctx.window_id)
        return False

    async def _window_title(self, ctx) -> str | None:
        if not ctx.pid:
            return None
        for w in await enumerate_windows(pid=ctx.pid):
            if w.get("window_id") == ctx.window_id:
                return w.get("title")
        return None

    async def _teardown(self, ctx, session):
        for pid in ctx.launched_pids:
            await driver.acall("kill_app", {"pid": pid})
        await driver.acall("end_session", {"session": session})

    @staticmethod
    def _data_url(state: dict) -> str | None:
        b64 = state.get("screenshot_png_b64")
        if not b64:
            return None
        mime = state.get("screenshot_mime_type", "image/png")
        return f"data:{mime};base64,{b64}"

    @staticmethod
    def _save_shot(artifacts_dir: Path, name: str, state: dict) -> None:
        b64 = state.get("screenshot_png_b64")
        if not b64:
            return
        import base64
        try:
            (artifacts_dir / name).write_bytes(base64.b64decode(b64))
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _trim(md: str, limit: int = 6000) -> str:
        return md if len(md) <= limit else md[:limit] + "\n…[truncated]"

    @staticmethod
    def _dominant_path(steps: list[dict]) -> str:
        paths = [s.get("path") for s in steps if s.get("path")]
        return paths[-1] if paths else "vision"

    @staticmethod
    def _history(steps: list[dict]) -> str:
        if not steps:
            return "(no actions yet)"
        lines = []
        for s in steps[-5:]:
            acts = ", ".join(
                f"{a.get('type')}({a.get('element_index', a.get('value', a.get('x', '')))})"
                for a in (s.get("actions") or [])[:3])
            lines.append(f"turn {s.get('turn')}: {acts} → {s.get('outcome')}")
        return "\n".join(lines)

    # ── packers ───────────────────────────────────────────────────────────────
    def _pack(self, path, goal, steps, *, content=None,
              final_title=None, elapsed=0.0) -> AgentResult:
        out = ComputerOutput(goal=goal, path=path, turns=len(steps),
                             content=content, actions=steps, final_title=final_title)
        return AgentResult(success=True, agent_name=self.NAME,
                           output=out.model_dump(), elapsed_s=elapsed)

    def _pack_error(self, goal, msg, *, elapsed=0.0) -> AgentResult:
        out = ComputerOutput(goal=goal, path="vision", turns=0, content=None)
        return AgentResult(success=False, agent_name=self.NAME,
                           output=out.model_dump(), error=msg,
                           error_code="interaction_failed", elapsed_s=elapsed)

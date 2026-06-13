"""Session 9: the Computer-Use skill — desktop driver via cua.Localhost.

The wrapper translates the orchestrator's NodeSpec contract into the typed
ComputerOutput / AgentResult contract, mirroring code/browser/skill.py. It
owns a small layer cascade over the *real* desktop:

    Layer 1 — deterministic : launch an app (shell) + caller-supplied
                              coordinate/keystroke steps (metadata.actions)
    Layer 2 — a11y          : NOT AVAILABLE on this driver. cua_auto exposes
                              no UIA accessibility tree, so there is no
                              semantic element_index layer; we skip straight
                              to vision. (A real a11y layer would need a
                              separate Windows UIA lib inside this module.)
    Layer 3 — vision        : screenshot → V9 /v1/vision → pixel coordinates
                              → mouse/keyboard, in a scan → act → verify loop.

The driver underneath is `cua.Localhost.connect()` — direct, unsandboxed
host control (NOT a VM Sandbox, NOT the macOS Rust cua-driver binary). Its
interface is async-native, so unlike the synchronous `cua_auto` backend it
needs no asyncio.to_thread bridge. Every LLM/vision call routes through the
V9 gateway tagged `agent="computer"`, exactly as BrowserSkill tags
`agent="browser"` — no new gateway, no provider-specific code here.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Optional

import cua
from pydantic import BaseModel, Field

from schemas import AgentResult, NodeSpec

# The V9 gateway client is generic — reuse the one shipped with Browser
# rather than cloning it. "No new gateway / no new client."
from browser.client import V9Client

# The named tool surface (#4). Every desktop action dispatches through this
# registry instead of an inline if/elif, and the registry is enumerable.
from .tools import ToolContext, front_and_maximize, run_tool, tool_names


# ─── action vocabulary (coordinate-based; no element marks) ──────────────────
# cua.Localhost has no accessibility tree, so the vision model addresses the
# screen by raw pixels (origin top-left). Coordinates are in screenshot space;
# the skill rescales them to the driver's logical click space before acting.
# Only ACTION tools the vision model should emit mid-loop. Diagnostic/utility
# tools (list_windows, get_screen_size, get_cursor_position, screenshot, zoom)
# and dangerous tools (kill_app, bring_to_front) are excluded from the enum
# so the model can't emit them during the vision loop. They remain in
# computer/tools.py for deterministic-layer and programmatic use.
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
                        "enum": ["click", "double_click", "right_click", "move",
                                 "drag", "scroll", "type", "key", "hotkey",
                                 "launch", "wait", "done"],
                    },
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                    "from_x": {"type": "integer"},
                    "from_y": {"type": "integer"},
                    "to_x": {"type": "integer"},
                    "to_y": {"type": "integer"},
                    "button": {"type": "string"},
                    "value": {"type": "string"},
                    "keys": {"type": "array", "items": {"type": "string"}},
                    "dx": {"type": "integer"},
                    "dy": {"type": "integer"},
                    "app": {"type": "string"},
                    "seconds": {"type": "number"},
                    "success": {"type": "boolean"},
                    "note": {"type": "string"},
                },
            },
        },
    },
}

SYSTEM_PROMPT_VISION = (
    "You are a desktop-driving agent operating a real Windows machine. Each "
    "turn you receive a full-screen screenshot. The screen is W×H pixels with "
    "the origin (0,0) at the TOP-LEFT; x grows right, y grows down. There are "
    "no element ids — you address the screen by pixel coordinate. Make "
    "progress toward the user's goal by emitting a short list of actions:\n"
    "  click(x, y)                — left-click that pixel\n"
    "  double_click(x, y) / right_click(x, y)\n"
    "  move(x, y)                 — move the cursor without clicking\n"
    "  type(value)                — type a string at the current focus\n"
    "  key(value)                 — press one key, e.g. 'Enter', 'Escape', 'Tab'\n"
    "  hotkey(keys)               — chord, e.g. ['ctrl','s'] or ['alt','F4']\n"
    "  scroll(x, y, dx?, dy?)     — scroll at (x,y); dy>0 scrolls down\n"
    "  launch(app)                — start an app by name, e.g. 'calc', 'notepad'\n"
    "  wait(seconds)              — pause to let the UI settle\n"
    "  done(success, note)        — finish; success=true if the goal is met\n"
    "Emit MULTIPLE actions in a turn only when their effect is obvious; "
    "otherwise emit one action and let the next screenshot inform the next "
    "step. Always re-read the screenshot before declaring done. Be terse in "
    "`thinking` — one or two sentences."
)


class ComputerOutput(BaseModel):
    """Typed payload the Computer skill writes into AgentResult.output.

    `path` is the cascade layer the skill actually used — replay and the
    Planner's failure routing read it the same way they read BrowserOutput.path.
    """

    goal: str
    path: str = Field(description="deterministic | a11y | vision")
    turns: int = 0
    content: str | None = None              # final note / extracted text
    actions: list[dict] = Field(default_factory=list)
    final_title: str | None = None          # active-window title at end of run


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
        # Forwarded to V9 so the gateway ledger attributes each call to the
        # orchestrator session that drove it.
        self.session = session

    # ── public entry point ─────────────────────────────────────────────────
    async def run(self, node: NodeSpec) -> AgentResult:
        goal = node.metadata.get("goal") or (node.inputs[0] if node.inputs else "")
        if not goal:
            return self._pack_error("", "no goal given (metadata.goal or inputs[0])")
        app = node.metadata.get("app")                       # optional launch target
        det_actions = node.metadata.get("actions") or []     # optional deterministic steps
        max_steps = int(node.metadata.get("max_steps") or self.max_steps)

        t0 = time.time()
        client = V9Client(base_url=self.gateway_url, agent=self.agent_tag,
                          session=self.session)
        artifacts_dir = (
            self.artifacts_root / f"computer_{int(t0)}"
            if self.artifacts_root else None
        )
        if artifacts_dir:
            artifacts_dir.mkdir(parents=True, exist_ok=True)

        # cua.Localhost.connect() supports both `await` and `async with`; the
        # plain-await form keeps the connect/teardown explicit and lets the
        # `finally` guarantee disconnect even on a mid-run exception.
        host = await cua.Localhost.connect()
        prelude: list[dict] = []
        launched_pids: set[int] = set()
        try:
            # Track PIDs before launching to identify the new process
            pids_before = set()
            try:
                pids_before = {w["pid"] for w in await enumerate_windows(host) if w.get("pid")}
            except Exception:
                pass

            # ── Layer 1: deterministic ──────────────────────────────────────
            if app:
                await self._launch(host, app)
                # Give a freshly-launched window time to realize before we try
                # to find + maximize it; a 1 s wait was too short and the
                # maximize landed on a not-yet-visible window.
                await asyncio.sleep(2.0)

                # Identify new process PIDs
                try:
                    pids_after = await enumerate_windows(host)
                    for w in pids_after:
                        pid = w.get("pid")
                        if pid and pid not in pids_before:
                            launched_pids.add(pid)
                except Exception:
                    pass

                # Front-and-maximize the launched window so perception sees a
                # clean, full-screen target instead of one small window buried
                # in desktop clutter. cua.Localhost.window can only read the
                # active title, so we drop to the synchronous cua_auto backend
                # (wrapped in to_thread) for the activate/maximize it omits.
                title_hint = node.metadata.get("window_title") or await self._safe_title(host)
                if title_hint:
                    outcome = await self._front_and_maximize(title_hint)
                    prelude.append({"turn": 0, "thinking": "setup: front + maximize",
                                    "actions": [{"type": "front_maximize", "value": title_hint}],
                                    "outcome": outcome})
                    await asyncio.sleep(0.6)
                    # If launched_pids is still empty, try to match by title_hint
                    if not launched_pids:
                        try:
                            matching_wins = await enumerate_windows(host, title_hint)
                            for w in matching_wins:
                                if w.get("pid"):
                                    launched_pids.add(w["pid"])
                        except Exception:
                            pass
            if det_actions:
                steps = prelude + await self._run_deterministic(host, det_actions)
                final_title = await self._safe_title(host)
                return self._pack("deterministic", goal, steps,
                                  content=det_actions[-1].get("note") if det_actions else None,
                                  final_title=final_title, elapsed=time.time() - t0)

            # ── Layer 2 (a11y) is intentionally absent — see module docstring.
            # ── Layer 3: vision scan → act → verify loop ────────────────────
            steps, success, note = await self._drive_vision(
                host, goal, client, artifacts_dir, max_steps, prelude=prelude,
            )
            # Fix 6: capture a final verification screenshot after done so
            # there is a post-action record for auditing / replay.
            if artifacts_dir:
                try:
                    _, _, _, final_raw = await self._screenshot(host)
                    (artifacts_dir / "final.png").write_bytes(final_raw)
                except Exception:  # noqa: BLE001
                    pass
            final_title = await self._safe_title(host)
            out = self._pack("vision", goal, steps, content=note,
                             final_title=final_title, elapsed=time.time() - t0)
            out.success = success
            return out
        except Exception as e:                                # noqa: BLE001
            return self._pack_error(goal, f"computer skill error: {type(e).__name__}: {e}",
                                    elapsed=time.time() - t0)
        finally:
            # Fix 4: graceful close → wait → fallback force-kill → delay
            if launched_pids:
                for pid in launched_pids:
                    try:
                        # Step 1: ask the window to close gracefully
                        close_cmd = (
                            f'powershell -NoProfile -Command "'
                            f'$p = Get-Process -Id {pid} -ErrorAction SilentlyContinue; '
                            f'if ($p) {{ $p.CloseMainWindow() | Out-Null }}"'
                        )
                        await host.shell.run(close_cmd, timeout=10)
                    except Exception:  # noqa: BLE001
                        pass
                # Give windows time to animate closed
                await asyncio.sleep(2.0)
                # Step 2: force-kill any that survived (unsaved-dialog, etc.)
                for pid in launched_pids:
                    try:
                        kill_cmd = (
                            f'powershell -NoProfile -Command "'
                            f'$p = Get-Process -Id {pid} -ErrorAction SilentlyContinue; '
                            f'if ($p) {{ Stop-Process -Id {pid} -Force -ErrorAction SilentlyContinue }}"'
                        )
                        await host.shell.run(kill_cmd, timeout=10)
                    except Exception:  # noqa: BLE001
                        pass
            # Small delay before disconnect to let final I/O settle
            await asyncio.sleep(1.0)
            try:
                await host.disconnect()
            except Exception:                                 # noqa: BLE001
                pass

    # ── vision loop ─────────────────────────────────────────────────────────
    async def _drive_vision(self, host, goal, client, artifacts_dir, max_steps,
                            prelude: list[dict] | None = None):
        # `prelude` carries non-vision setup steps (e.g. front+maximize) so
        # they show up in the recorded trace / replay alongside the vision turns.
        steps: list[dict] = list(prelude or [])
        failures = 0
        for turn in range(1, max_steps + 1):
            # scan
            shot_w, shot_h, data_url, raw = await self._screenshot(host)
            scale_x, scale_y = await self._click_scale(host, shot_w, shot_h)
            if artifacts_dir:
                (artifacts_dir / f"turn_{turn:02d}.png").write_bytes(raw)

            prompt = (
                f"GOAL: {goal}\n\n"
                f"SCREEN: {shot_w}x{shot_h} pixels (origin top-left)\n"
                f"RECENT ACTIONS:\n{self._history(steps)}\n\n"
                f"What is the next set of actions?"
            )
            result = await client.vision(
                data_url, prompt, system=SYSTEM_PROMPT_VISION,
                schema=ACTION_SCHEMA, schema_name="AgentOutput",
                max_tokens=1024, provider=self.vision_provider_pin,
            )
            parsed = result.parsed
            if not parsed:
                steps.append({"turn": turn, "thinking": "", "actions": [],
                              "outcome": f"error: no parsed output; raw={result.text[:120]!r}"})
                failures += 1
                if failures >= self.max_failures:
                    return steps, False, "giveup: vision returned no parseable action"
                continue

            # act
            thinking = parsed.get("thinking", "")
            actions = parsed.get("actions") or []
            outcomes: list[str] = []
            done_seen = success_seen = False
            done_note = ""
            for a in actions:
                if a.get("type") == "done":
                    done_seen = True
                    success_seen = bool(a.get("success", False))
                    done_note = a.get("note", "")
                    outcomes.append(f"done({success_seen})")
                    break
                try:
                    outcome = await self._dispatch(host, a, scale_x, scale_y)
                except Exception as e:                        # noqa: BLE001
                    outcome = f"error: {type(e).__name__}: {e}"
                outcomes.append(outcome)
                if outcome.startswith("error"):
                    break
                await asyncio.sleep(self.pause_between_steps)

            outcome_str = " | ".join(outcomes) or "ok"
            steps.append({"turn": turn, "thinking": thinking,
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

    # ── action dispatch ──────────────────────────────────────────────────────
    async def _dispatch(self, host, a: dict, scale_x: float, scale_y: float) -> str:
        """Execute one action through the named tool registry (computer/tools.py).
        The action's `type` is the tool name; the action dict is its arguments."""
        return await run_tool(a.get("type", ""), a,
                              ToolContext(host, scale_x, scale_y))

    async def _run_deterministic(self, host, det_actions) -> list[dict]:
        steps: list[dict] = []
        for i, a in enumerate(det_actions, start=1):
            try:
                outcome = await self._dispatch(host, a, 1.0, 1.0)
            except Exception as e:                            # noqa: BLE001
                outcome = f"error: {type(e).__name__}: {e}"
            steps.append({"turn": i, "thinking": "deterministic",
                          "actions": [a], "outcome": outcome})
            if outcome.startswith("error"):
                break
            await asyncio.sleep(self.pause_between_steps)
        return steps

    # ── helpers ───────────────────────────────────────────────────────────────
    async def _launch(self, host, app: str) -> None:
        """Start an app by name. cua.Localhost.window has no launch(), so we go
        through the shell. `start` returns immediately on Windows."""
        if not app:
            return
        await host.shell.run(f'start "" {app}', timeout=15)

    async def _front_and_maximize(self, title_hint: str) -> str:
        # Delegates to the shared helper, which also backs the bring_to_front
        # tool — single implementation for the activate/maximize the localhost
        # wrapper omits (cua_auto fallback).
        return await front_and_maximize(title_hint)

    async def _screenshot(self, host):
        b64 = await host.screen.screenshot_base64()
        import base64 as _b64
        raw = _b64.b64decode(b64)
        # Pixel dims from the PNG header (bytes 16..24) — avoids a Pillow import.
        shot_w = int.from_bytes(raw[16:20], "big")
        shot_h = int.from_bytes(raw[20:24], "big")
        return shot_w, shot_h, f"data:image/png;base64,{b64}", raw

    async def _click_scale(self, host, shot_w: int, shot_h: int):
        """Map screenshot-pixel coords to the driver's logical click space.
        1:1 on a non-scaled display; the ratio corrects for HiDPI where the
        screenshot is larger than the logical coordinate space."""
        try:
            log_w, log_h = await host.screen.size()
            sx = log_w / shot_w if shot_w else 1.0
            sy = log_h / shot_h if shot_h else 1.0
            return sx, sy
        except Exception:                                     # noqa: BLE001
            return 1.0, 1.0

    async def _safe_title(self, host) -> str | None:
        try:
            return await host.window.get_active_title()
        except Exception:                                     # noqa: BLE001
            return None

    @staticmethod
    def _history(steps: list[dict]) -> str:
        if not steps:
            return "(no actions yet)"
        lines = []
        for s in steps[-5:]:
            acts = ", ".join(
                f"{a.get('type')}({a.get('x', a.get('value', ''))})"
                for a in (s.get("actions") or [])[:3]
            )
            lines.append(f"turn {s['turn']}: {acts} → {s['outcome']}")
        return "\n".join(lines)

    # ── packers ───────────────────────────────────────────────────────────────
    def _pack(self, path, goal, steps, *, content=None,
              final_title=None, elapsed=0.0) -> AgentResult:
        out = ComputerOutput(
            goal=goal, path=path, turns=len(steps),
            content=content, actions=steps, final_title=final_title,
        )
        return AgentResult(
            success=True, agent_name=self.NAME,
            output=out.model_dump(), elapsed_s=elapsed,
        )

    def _pack_error(self, goal, msg, *, elapsed=0.0) -> AgentResult:
        out = ComputerOutput(goal=goal, path="vision", turns=0, content=None)
        return AgentResult(
            success=False, agent_name=self.NAME,
            output=out.model_dump(), error=msg, error_code="interaction_failed",
            elapsed_s=elapsed,
        )

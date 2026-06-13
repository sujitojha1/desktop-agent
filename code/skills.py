"""Session 8 skill registry + per-skill execution.

The orchestrator (flow.py) treats every node as a `Skill` object loaded
from agent_config.yaml. There is no Python class per skill — that
abstraction would have to be added at the point where a skill needs
behaviour the orchestrator can't infer from the yaml. Today every skill
either calls the gateway or (for sandbox_executor) calls sandbox.py.

What lives here:
  - Skill / SkillRegistry
  - input resolution (`n:...`, `art:...`, `USER_QUERY`, literals)
  - prompt rendering (template + inputs + optional failure report)
  - JSON parsing of the model's reply (single top-level object)
  - the MCP tool schemas exposed to tool-using skills
  - `run_skill(...)` — the dispatcher
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import yaml
from pydantic import ValidationError

import artifacts as artifacts_svc
from gateway import LLM
from schemas import AgentResult, NodeSpec

ROOT = Path(__file__).parent
AGENT_CONFIG_PATH = ROOT / "agent_config.yaml"


# ── catalogue ────────────────────────────────────────────────────────────────

class Skill:
    def __init__(self, name: str, cfg: dict):
        self.name = name
        self.prompt_path = ROOT / cfg["prompt"]
        self.description = cfg.get("description", "")
        self.tools_allowed: list[str] = cfg.get("tools_allowed", []) or []
        self.internal_successors: list[str] = cfg.get("internal_successors", []) or []
        self.critic: bool = bool(cfg.get("critic", False))
        self.provider_pin: str | None = cfg.get("provider_pin")
        # P2 #10: per-skill temperature / max_tokens come from the yaml so
        # tuning a single skill no longer requires a code edit. Defaults
        # are deliberately conservative; a skill that wants exploration
        # (Researcher) bumps temperature; a skill that wants determinism
        # (Critic, Distiller) drops it to ~0.
        self.temperature: float = float(cfg.get("temperature", 0.3))
        self.max_tokens: int = int(cfg.get("max_tokens", 2048))

    def prompt_template(self) -> str:
        if not self.prompt_path.exists():
            return f"You are the {self.name} skill. (Prompt file missing.)"
        return self.prompt_path.read_text()


class SkillRegistry:
    def __init__(self):
        cfg = yaml.safe_load(AGENT_CONFIG_PATH.read_text())
        self._skills: dict[str, Skill] = {n: Skill(n, c) for n, c in cfg.items()}

    def get(self, name: str) -> Skill:
        if name not in self._skills:
            raise KeyError(f"unknown skill: {name}")
        return self._skills[name]

    def names(self) -> list[str]:
        return list(self._skills)


# ── input resolution + prompt rendering ──────────────────────────────────────

def resolve_inputs(node_inputs: list[str], graph_nodes, query: str) -> list[dict]:
    """Materialise each input id into a dict the prompt can serialise.

    Recognised input forms:
      - "USER_QUERY"  → the original user query text
      - "n:<i>"       → the AgentResult.output of that completed node
      - "art:<sha>"   → the bytes of an artifact, decoded as utf-8 best-effort
      - any other     → passed through as a free-form string

    `graph_nodes` is the nx node-view dict from flow.Graph; we read each
    upstream node's `result` attribute (set when the orchestrator marks
    the node complete).
    """
    out = []
    for inp in node_inputs:
        if inp == "USER_QUERY":
            out.append({"id": "USER_QUERY", "kind": "query", "value": query})
        elif inp.startswith("n:") and inp in graph_nodes:
            upstream = graph_nodes[inp].get("result")
            if isinstance(upstream, AgentResult):
                out.append({"id": inp, "kind": "upstream",
                            "skill": upstream.agent_name, "output": upstream.output})
            else:
                out.append({"id": inp, "kind": "upstream-missing", "output": None})
        elif inp.startswith("art:"):
            try:
                blob = artifacts_svc.get_bytes(inp)
                text = blob.decode("utf-8", errors="replace")
                out.append({"id": inp, "kind": "artifact", "text": text[:20_000]})
            except Exception as e:
                out.append({"id": inp, "kind": "artifact-missing", "error": str(e)})
        else:
            out.append({"id": inp, "kind": "literal", "value": inp})
    return out


def _format_memory_hits(hits: list) -> str:
    """Compact rendering of FAISS-ranked MemoryItem hits for the prompt.

    Each hit is shown as one line: kind, descriptor, source, plus a 400-char
    preview of `value.chunk` when present (indexed-document chunks) or of
    `value.raw` (classifier facts). The full chunk would blow the prompt,
    but the descriptor + preview is enough for the Planner to decide
    whether memory already covers the query and for downstream skills to
    synthesise from indexed material without an extra Retriever round-trip.
    """
    if not hits:
        return ""
    lines = []
    for h in hits[:8]:  # cap to keep the prompt bounded
        kind = getattr(h, "kind", "?")
        desc = (getattr(h, "descriptor", "") or "")[:200]
        source = getattr(h, "source", "")
        val = getattr(h, "value", {}) or {}
        chunk = val.get("chunk")
        raw = val.get("raw")
        line = f"  - [{kind}] {desc}"
        if source:
            line += f"\n      source: {source}"
        if isinstance(chunk, str) and chunk.strip():
            preview = chunk[:2000].replace("\n", " ")
            more = " …" if len(chunk) > 2000 else ""
            line += f"\n      chunk: {preview}{more}"
        elif isinstance(raw, str) and raw.strip():
            raw_more = " …" if len(raw) > 2000 else ""
            line += f"\n      raw: {raw[:2000]}{raw_more}"
        lines.append(line)
    return "\n".join(lines)


def render_prompt(skill: Skill, query: str, resolved: list[dict],
                  failure_report: str | None = None,
                  memory_hits: list | None = None,
                  question: str | None = None) -> str:
    parts = [skill.prompt_template().rstrip()]
    # USER_QUERY top-line: only when the Planner wired USER_QUERY into this
    # node's inputs. Earlier versions added it unconditionally, which
    # leaked the full original query into every fan-out worker — three
    # researcher siblings spawned to "find population of A / B / C" all
    # saw the same "compare A, B, C" query and each one ended up
    # searching for all three. Per-node scoping now travels through
    # `metadata.question` (rendered as QUESTION below) and the INPUTS
    # block; USER_QUERY is present only when the Planner asked for it.
    user_query_in_inputs = any(
        isinstance(r, dict) and r.get("id") == "USER_QUERY" for r in resolved
    )
    if user_query_in_inputs:
        parts += ["", f"USER_QUERY: {query}"]
    # QUESTION: the per-node sub-question the Planner attached via
    # `metadata.question`. This is how a fan-out worker learns *its*
    # slice of the user's request without seeing the whole query.
    if isinstance(question, str) and question.strip():
        parts += ["", f"QUESTION: {question.strip()}"]
    if failure_report:
        parts += ["", f"FAILURE:\n{failure_report}"]
    # Memory hits — FAISS-ranked MemoryItems from session-start memory.read.
    # Same hits flow into every skill's prompt this run (the S7 contract:
    # every cognitive role can see what the agent already knows).
    hits_block = _format_memory_hits(memory_hits or [])
    if hits_block:
        parts += ["", f"MEMORY HITS ({len(memory_hits)} from FAISS):", hits_block]
    parts += ["", "INPUTS:", json.dumps(resolved, indent=2, default=str)[:20_000]]
    return "\n".join(parts)


def parse_skill_json(text: str) -> dict:
    """Skills return a single top-level JSON object. Strip markdown fences
    if the model added them despite being told not to."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.strip("`")
        t = t.split("\n", 1)[1] if "\n" in t else t
        if t.endswith("```"):
            t = t[:-3]
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        start, end = t.find("{"), t.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(t[start:end + 1])
            except json.JSONDecodeError:
                pass
    return {}


# ── MCP tool schemas exposed through the gateway tools= channel ──────────────

_TOOL_CATALOG = {
    "web_search": {
        "name": "web_search",
        "description": "Search the web (Tavily primary, DDG fallback). Hard-capped at 5 results.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer", "default": 3},
            },
            "required": ["query"],
        },
    },
    "fetch_url": {
        "name": "fetch_url",
        "description": "Fetch clean markdown from a URL via crawl4ai.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
    "search_knowledge": {
        "name": "search_knowledge",
        "description": "Vector search over the agent's indexed knowledge base.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "k": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
    },
    # ── cua-driver computer-use tools (backed by mcp_server.computer_*) ──────
    # Schemas the gateway shows the model. Dispatch is handled by the matching
    # @mcp.tool() in mcp_server.py, which shells out to `cua-driver call`.
    "computer_list_apps": {
        "name": "computer_list_apps",
        "description": "List running + installed Windows apps with pids. Start here to find a target app's pid.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    "computer_list_windows": {
        "name": "computer_list_windows",
        "description": "List top-level windows (title, pid, window_id=HWND). pid>0 scopes to one app.",
        "input_schema": {
            "type": "object",
            "properties": {"pid": {"type": "integer", "default": 0}},
            "required": [],
        },
    },
    "computer_launch_app": {
        "name": "computer_launch_app",
        "description": "Launch a Windows app; returns its real pid + windows[] (each with window_id). Prefer over list_windows for Store apps (Calculator, Notepad).",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    "computer_get_window_state": {
        "name": "computer_get_window_state",
        "description": "SCAN: walk the window's UIA tree → Markdown element list, each control tagged [element_index N], plus element_count. Call once per turn before any element action; the index map is replaced by the next scan. capture_mode: ax|som|vision. query filters the markdown.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pid": {"type": "integer"},
                "window_id": {"type": "integer"},
                "capture_mode": {"type": "string", "enum": ["ax", "som", "vision"], "default": "ax"},
                "query": {"type": "string", "default": ""},
            },
            "required": ["pid", "window_id"],
        },
    },
    "computer_click": {
        "name": "computer_click",
        "description": "ACT: click. Prefer element_index (from the last get_window_state) for focus-free semantic clicks; use x,y only when no element covers the target. Re-scan to verify.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pid": {"type": "integer"},
                "window_id": {"type": "integer", "default": 0},
                "element_index": {"type": "integer", "default": -1},
                "x": {"type": "integer", "default": -1},
                "y": {"type": "integer", "default": -1},
                "button": {"type": "string", "enum": ["left", "right", "middle"], "default": "left"},
                "count": {"type": "integer", "default": 1},
            },
            "required": ["pid"],
        },
    },
    "computer_type_text": {
        "name": "computer_type_text",
        "description": "ACT: type text into the focused control (or element_index if given).",
        "input_schema": {
            "type": "object",
            "properties": {
                "pid": {"type": "integer"},
                "text": {"type": "string"},
                "window_id": {"type": "integer", "default": 0},
                "element_index": {"type": "integer", "default": -1},
            },
            "required": ["pid", "text"],
        },
    },
    "computer_press_key": {
        "name": "computer_press_key",
        "description": "ACT: press one key, e.g. 'enter', 'escape', 'tab', '='.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pid": {"type": "integer"},
                "key": {"type": "string"},
                "window_id": {"type": "integer", "default": 0},
            },
            "required": ["pid", "key"],
        },
    },
    "computer_hotkey": {
        "name": "computer_hotkey",
        "description": "ACT: press a key chord, e.g. ['ctrl','s'] or ['alt','F4'].",
        "input_schema": {
            "type": "object",
            "properties": {
                "pid": {"type": "integer"},
                "keys": {"type": "array", "items": {"type": "string"}},
                "window_id": {"type": "integer", "default": 0},
            },
            "required": ["pid", "keys"],
        },
    },
    "computer_scroll": {
        "name": "computer_scroll",
        "description": "ACT: scroll the focused region. direction in up/down/left/right.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pid": {"type": "integer"},
                "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
                "window_id": {"type": "integer", "default": 0},
                "amount": {"type": "integer", "default": 3},
            },
            "required": ["pid", "direction"],
        },
    },
    "computer_set_value": {
        "name": "computer_set_value",
        "description": "ACT: set a UIA element's value directly via ValuePattern (text fields, sliders) — faster than typing. element_index from the last get_window_state.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pid": {"type": "integer"},
                "window_id": {"type": "integer"},
                "element_index": {"type": "integer"},
                "value": {"type": "string"},
            },
            "required": ["pid", "window_id", "element_index", "value"],
        },
    },
    "computer_get_accessibility_tree": {
        "name": "computer_get_accessibility_tree",
        "description": "Return a lightweight snapshot of the desktop: running processes and on-screen visible windows with their bounds and owner pid.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    "computer_get_screen_size": {
        "name": "computer_get_screen_size",
        "description": "Return the main display's logical size and backing scale factor.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    "computer_double_click": {
        "name": "computer_double_click",
        "description": "ACT: double-click against a target pid. Prefer element_index (from the last get_window_state) for semantic double-clicks; use x,y window-local pixels only when no element covers the target.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pid": {"type": "integer"},
                "window_id": {"type": "integer", "default": 0},
                "element_index": {"type": "integer", "default": -1},
                "x": {"type": "number", "default": -1.0},
                "y": {"type": "number", "default": -1.0},
                "modifier": {"type": "array", "items": {"type": "string"}},
                "dispatch": {"type": "string", "enum": ["background", "foreground", "auto"], "default": "background"},
                "from_zoom": {"type": "boolean", "default": False},
            },
            "required": ["pid"],
        },
    },
    "computer_right_click": {
        "name": "computer_right_click",
        "description": "ACT: right-click against a target pid. Prefer element_index (from the last get_window_state) for semantic right-clicks; use x,y window-local pixels only when no element covers the target.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pid": {"type": "integer"},
                "window_id": {"type": "integer", "default": 0},
                "element_index": {"type": "integer", "default": -1},
                "x": {"type": "number", "default": -1.0},
                "y": {"type": "number", "default": -1.0},
                "modifier": {"type": "array", "items": {"type": "string"}},
                "dispatch": {"type": "string", "enum": ["background", "foreground", "auto"], "default": "background"},
                "from_zoom": {"type": "boolean", "default": False},
            },
            "required": ["pid"],
        },
    },
    "computer_drag": {
        "name": "computer_drag",
        "description": "ACT: press-drag-release gesture from (from_x, from_y) to (to_x, to_y) in window-local screenshot pixels.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pid": {"type": "integer"},
                "from_x": {"type": "number"},
                "from_y": {"type": "number"},
                "to_x": {"type": "number"},
                "to_y": {"type": "number"},
                "button": {"type": "string", "enum": ["left", "right", "middle"], "default": "left"},
                "window_id": {"type": "integer", "default": 0},
                "steps": {"type": "integer", "default": 20},
                "duration_ms": {"type": "integer", "default": 500},
                "dispatch": {"type": "string", "enum": ["background", "foreground", "auto"], "default": "background"},
                "from_zoom": {"type": "boolean", "default": False},
                "modifier": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["pid", "from_x", "from_y", "to_x", "to_y"],
        },
    },
    "computer_move_cursor": {
        "name": "computer_move_cursor",
        "description": "ACT: move the agent cursor overlay to (x, y). Does NOT move the real mouse cursor.",
        "input_schema": {
            "type": "object",
            "properties": {
                "x": {"type": "number"},
                "y": {"type": "number"},
                "cursor_id": {"type": "string", "default": ""},
            },
            "required": ["x", "y"],
        },
    },
    "computer_get_cursor_position": {
        "name": "computer_get_cursor_position",
        "description": "Return the current mouse cursor position in screen points (origin top-left).",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    "computer_bring_to_front": {
        "name": "computer_bring_to_front",
        "description": "ACT: activate pid's window (or window_id if specified) -- bring it to the OS foreground.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pid": {"type": "integer"},
                "window_id": {"type": "integer", "default": 0},
            },
            "required": ["pid"],
        },
    },
    "computer_kill_app": {
        "name": "computer_kill_app",
        "description": "ACT: force-terminate a process by pid.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pid": {"type": "integer"},
            },
            "required": ["pid"],
        },
    },
    "computer_debug_window_info": {
        "name": "computer_debug_window_info",
        "description": "Diagnostic: dump everything cua-driver sees about a pid's top-level windows from the daemon's session perspective.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pid": {"type": "integer"},
            },
            "required": ["pid"],
        },
    },
    "computer_zoom": {
        "name": "computer_zoom",
        "description": "Zoom into a rectangular region of a window screenshot at full (native) resolution.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pid": {"type": "integer"},
                "window_id": {"type": "integer"},
                "x1": {"type": "number"},
                "y1": {"type": "number"},
                "x2": {"type": "number"},
                "y2": {"type": "number"},
            },
            "required": ["pid", "window_id", "x1", "y1", "x2", "y2"],
        },
    },
    "computer_page": {
        "name": "computer_page",
        "description": "Interact with the browser page DOM loaded in a running app (CDP/WebKit).",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "execute_javascript",
                        "get_text",
                        "query_dom",
                        "click_element",
                        "enable_javascript_apple_events"
                    ],
                },
                "pid": {"type": "integer", "default": 0},
                "window_id": {"type": "integer", "default": 0},
                "selector": {"type": "string", "default": ""},
                "css_selector": {"type": "string", "default": ""},
                "javascript": {"type": "string", "default": ""},
                "attributes": {"type": "array", "items": {"type": "string"}},
                "bundle_id": {"type": "string", "default": ""},
                "user_has_confirmed_enabling": {"type": "boolean", "default": False},
            },
            "required": ["action"],
        },
    },
    "computer_start_recording": {
        "name": "computer_start_recording",
        "description": "Start trajectory recording to the specified directory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "output_dir": {"type": "string"},
                "record_video": {"type": "boolean", "default": False},
            },
            "required": ["output_dir"],
        },
    },
    "computer_stop_recording": {
        "name": "computer_stop_recording",
        "description": "Stop trajectory recording.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    "computer_get_recording_state": {
        "name": "computer_get_recording_state",
        "description": "Report the current trajectory recorder state.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    "computer_replay_trajectory": {
        "name": "computer_replay_trajectory",
        "description": "Replay a recorded trajectory by re-invoking every turn's tool call in order.",
        "input_schema": {
            "type": "object",
            "properties": {
                "dir": {"type": "string"},
                "delay_ms": {"type": "integer", "default": 500},
                "stop_on_error": {"type": "boolean", "default": True},
            },
            "required": ["dir"],
        },
    },
    "computer_install_ffmpeg": {
        "name": "computer_install_ffmpeg",
        "description": "Install the ffmpeg binary used by start_recording's video capture (Windows/Linux only).",
        "input_schema": {
            "type": "object",
            "properties": {
                "confirm": {"type": "boolean", "default": False},
            },
            "required": [],
        },
    },
    "computer_start_session": {
        "name": "computer_start_session",
        "description": "Declare a session — a named, color-coded identity for THIS agent run. Pass a stable `session` id; the agent cursor, per-session config, and recording all key on it, and it follows the run across any apps/windows. Concurrent runs/subagents each pass their own `session` to get their own cursor.",
        "input_schema": {
            "type": "object",
            "properties": {
                "session": {
                    "type": "string",
                    "description": "Stable session id for this run (e.g. \"research-run-1\")."
                }
            },
            "required": ["session"]
        }
    },
    "computer_end_session": {
        "name": "computer_end_session",
        "description": "End a session declared with `start_session`: removes its agent cursor, stops any recording it owns, and clears its per-session config.",
        "input_schema": {
            "type": "object",
            "properties": {
                "session": {
                    "type": "string",
                    "description": "The session id to end."
                }
            },
            "required": ["session"]
        }
    },
    "computer_set_agent_cursor_enabled": {
        "name": "computer_set_agent_cursor_enabled",
        "description": "Toggle the visual agent-cursor overlay. Disabling removes the overlay immediately.",
        "input_schema": {
            "type": "object",
            "properties": {
                "enabled": {
                    "type": "boolean",
                    "description": "True to show the overlay cursor; false to hide."
                },
                "cursor_id": {
                    "type": "string",
                    "description": "Rust-only: multi-cursor instance id. Default 'default'."
                }
            },
            "required": ["enabled"]
        }
    },
    "computer_set_agent_cursor_style": {
        "name": "computer_set_agent_cursor_style",
        "description": "Update the visual style of the agent cursor overlay. All parameters are optional.",
        "input_schema": {
            "type": "object",
            "properties": {
                "bloom_color": {
                    "type": "string",
                    "description": "Hex bloom/halo colour (e.g. '#00FFFF'). '' = revert to default."
                },
                "cursor_id": {
                    "type": "string",
                    "description": "Cursor instance. Default: 'default'."
                },
                "gradient_colors": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "CSS hex gradient stops tip to tail (e.g. ['#FF0000','#0000FF']). [] = revert to default."
                },
                "image_path": {
                    "type": "string",
                    "description": "Path to PNG/JPEG/SVG/ICO cursor image. '' = revert to arrow."
                }
              },
              "required": []
        }
    },
    "computer_set_agent_cursor_motion": {
        "name": "computer_set_agent_cursor_motion",
        "description": "Configure the visual appearance and motion curve of an agent cursor instance. All parameters are optional.",
        "input_schema": {
            "type": "object",
            "properties": {
                "cursor_id": {
                    "type": "string",
                    "description": "Cursor instance. Default: 'default'."
                },
                "cursor_icon": {
                    "type": "string",
                    "description": "Built-in ('arrow','crosshair','hand','dot') or PNG/SVG file path."
                },
                "cursor_color": {
                    "type": "string",
                    "description": "Hex color e.g. '#00FFFF' or CSS name."
                },
                "cursor_label": {
                    "type": "string",
                    "description": "Short text shown near the cursor."
                },
                "cursor_size": {
                    "type": "number",
                    "description": "Dot radius in points. Default 16."
                },
                "cursor_opacity": {
                    "type": "number",
                    "description": "0.0–1.0 (default 0.85)."
                },
                "arc_size": {
                    "type": "number",
                    "description": "Arc deflection as fraction of path length [0,1]. Default 0.25."
                },
                "arc_flow": {
                    "type": "number",
                    "description": "Asymmetry bias [-1,1]. Default 0.0."
                },
                "start_handle": {
                    "type": "number",
                    "description": "Start-handle fraction [0,1]. Default 0.3."
                },
                "end_handle": {
                    "type": "number",
                    "description": "End-handle fraction [0,1]. Default 0.3."
                },
                "spring": {
                    "type": "number",
                    "description": "Settle damping [0.3,1.0]. Default 0.72."
                },
                "glide_duration_ms": {
                    "type": "number",
                    "minimum": 50,
                    "maximum": 5000,
                    "description": "Fixed flight duration per move in ms; omit for speed-based timing (the default)."
                },
                "dwell_after_click_ms": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 5000,
                    "description": "Pause after click ripple in ms. Default 80."
                },
                "idle_hide_ms": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 60000,
                    "description": "Auto-hide delay in ms. 0=never. Default 20000."
                },
                "turn_radius": {
                    "type": "number",
                    "minimum": 1,
                    "maximum": 1000,
                    "description": "Minimum turning radius of the glide path in points; smaller = tighter curves. Default 80."
                }
            },
            "required": []
        }
    },
    "computer_get_agent_cursor_state": {
        "name": "computer_get_agent_cursor_state",
        "description": "Report the current agent-cursor configuration: enabled flag, motion knobs, glide duration, post-click dwell, and idle-hide delay. Pure read-only.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    "computer_get_config": {
        "name": "computer_get_config",
        "description": "Report the current persistent driver config. Pure read-only. Returns defaults when the underlying state is unset.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    "computer_set_config": {
        "name": "computer_set_config",
        "description": "Write a setting into the persistent driver config. Values take effect immediately. Supports both Swift-compatible dotted paths (key/value) and legacy per-field writes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Dotted snake_case path to a leaf config field (Swift-compatible shape). Pair with `value`."
                },
                "value": {
                    "description": "New value for `key`. JSON type depends on the key."
                },
                "capture_mode": {
                    "type": "string",
                    "enum": ["som", "vision", "ax"],
                    "description": "Legacy per-field shape."
                },
                "max_image_dimension": {
                    "type": "integer",
                    "description": "Legacy per-field shape."
                },
                "experimental_pip": {
                    "type": "boolean",
                    "description": "Legacy per-field shape. Enables PiP preview (applies next restart)."
                },
                "experimental_pip_geometry": {
                    "type": "string",
                    "description": "Legacy per-field shape. PiP window size + optional position (WxH or WxH+X+Y)."
                }
            },
            "required": []
        }
    },
    "computer_check_permissions": {
        "name": "computer_check_permissions",
        "description": "Check required permissions for cua-driver-rs on Windows.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    "computer_check_for_update": {
        "name": "computer_check_for_update",
        "description": "Check whether a newer cua-driver-rs release is available on GitHub. Returns the current and latest versions, an `update_available` boolean, the install one-liner, and the release notes URL. Read-only.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
}


def tool_payload(tool_names: list[str]) -> list[dict] | None:
    if not tool_names:
        return None
    return [_TOOL_CATALOG[n] for n in tool_names if n in _TOOL_CATALOG]


# ── per-node execution ───────────────────────────────────────────────────────

async def run_skill(skill: Skill, node_id: str, graph_nodes,
                    session_id: str, query: str,
                    failure_report: str | None,
                    *, memory_hits: list | None = None) -> tuple[AgentResult, str]:
    """Dispatch one node. Returns (result, rendered_prompt).

    `memory_hits` is the FAISS-ranked MemoryItem list captured once at
    session start by Executor.run and threaded through here so every
    skill's prompt can see the same hits. This is the S7 promise carried
    forward — Memory works in S8 because the orchestrator delivers the
    hits, not just because the FAISS index is on disk.

    sandbox_executor bypasses the gateway: it picks the `code` field out of
    its upstream coder node and runs sandbox.run_python directly. All other
    skills are LLM-backed and route through the V8 gateway with
    agent=<skill_name> so agent_routing.yaml + cost-by-agent kick in."""
    resolved = resolve_inputs(graph_nodes[node_id]["inputs"], graph_nodes, query)
    # Per-node sub-question from the Planner's `metadata.question`. Travels
    # into the rendered prompt as a QUESTION: block so a fan-out worker
    # (e.g. one of three researchers spawned to cover three cities) can
    # see *its* slice of the user's request even when USER_QUERY is not
    # in its inputs.
    node_meta = graph_nodes[node_id].get("metadata") or {}
    question = node_meta.get("question") if isinstance(node_meta, dict) else None
    rendered = render_prompt(skill, query, resolved, failure_report,
                             memory_hits=memory_hits, question=question)
    started = time.time()

    if skill.name == "sandbox_executor":
        code = ""
        for r in resolved:
            if r.get("kind") == "upstream" and isinstance(r.get("output"), dict):
                code = r["output"].get("code") or code
        if not code:
            return AgentResult(
                success=False, agent_name=skill.name,
                error="no code in upstream coder output",
                elapsed_s=time.time() - started,
            ), rendered
        from sandbox import run_python
        out = run_python(code)
        return AgentResult(
            success=(out["exit_code"] == 0 and not out["timed_out"]),
            agent_name=skill.name, output=out,
            elapsed_s=time.time() - started,
        ), rendered

    if skill.name == "browser":
        # Same shape as sandbox_executor: the Browser skill owns its own
        # cascade (extract → deterministic → a11y → vision) and never
        # touches the LLM tool/text channel — so we bypass render_prompt
        # and the gateway-chat dispatch entirely and hand off to
        # BrowserSkill.run(NodeSpec).
        node_dict = graph_nodes[node_id]
        node_spec = NodeSpec(
            skill="browser",
            inputs=node_dict.get("inputs") or [],
            metadata=node_dict.get("metadata") or {},
        )
        from browser.skill import BrowserSkill
        sk = BrowserSkill(
            artifacts_root=str(ROOT / "state" / "sessions" / session_id / "browser"),
            session=session_id,
        )
        result = await sk.run(node_spec)
        if not result.elapsed_s:
            result.elapsed_s = time.time() - started
        return result, rendered

    tools = tool_payload(skill.tools_allowed)
    if tools:
        # Multi-turn tool-use loop. mcp_runner opens one MCP stdio session
        # per skill invocation, dispatches each tool_call the model emits,
        # and feeds the results back until the model produces final text.
        from mcp_runner import run_with_tools
        reply = await run_with_tools(
            prompt=rendered,
            tools_payload=tools,
            agent=skill.name,
            session_id=session_id,
            provider_pin=skill.provider_pin,
            max_tokens=skill.max_tokens,
            temperature=skill.temperature,
        )
    else:
        reply = await asyncio.to_thread(
            LLM().chat,
            prompt=rendered,
            agent=skill.name,
            session=session_id,
            provider=skill.provider_pin,
            max_tokens=skill.max_tokens,
            temperature=skill.temperature,
        )
    parsed = parse_skill_json(reply.get("text", ""))

    # Lift orchestrator-recognised fields out of the skill's JSON.
    # NOTES_RUNS feedback P0 #1: malformed successors used to be silently
    # dropped, which left students chasing "missing node" bugs for an hour.
    # Now: log the offending JSON + the validation error, then fail the
    # node so the failure path (and replay) surfaces it.
    raw_successors = parsed.pop("successors", []) or []
    successors: list[NodeSpec] = []
    rejected: list[str] = []
    for s in raw_successors:
        try:
            successors.append(NodeSpec.model_validate(s))
        except ValidationError as ve:
            rejected.append(f"successor={s!r}  error={ve}")
    if skill.name == "planner":
        for s in parsed.get("nodes", []) or []:
            try:
                successors.append(NodeSpec.model_validate(s))
            except ValidationError as ve:
                rejected.append(f"node={s!r}  error={ve}")

    if rejected:
        err = (
            f"{skill.name}: {len(rejected)} malformed NodeSpec(s) emitted.\n"
            + "\n".join(f"  - {line}" for line in rejected)
        )
        print(f"[skills] {err}")
        return AgentResult(
            success=False, agent_name=skill.name,
            output=parsed, successors=successors,
            elapsed_s=time.time() - started,
            provider=reply.get("provider", ""),
            error=err,
        ), rendered

    return AgentResult(
        success=True,
        agent_name=skill.name,
        output=parsed,
        successors=successors,
        elapsed_s=time.time() - started,
        provider=reply.get("provider", ""),
    ), rendered

# cua-driver tool reference (Windows)

Source of truth for the **computer** skill cleanup (issue #20). Generated from the
live install on this machine — regenerate after a driver upgrade:

```bash
cua-driver list-tools                 # one line per tool
cua-driver describe <tool>            # full JSON input_schema for one tool
```

- **Version documented:** `cua-driver 0.5.3` (Windows / Rust backend)
- **Daemon transport:** Windows named pipe `\\.\pipe\cua-driver`. The daemon must be
  running (`cua-driver status` → "Cua Driver daemon is running"). Every `cua-driver
  call` proxies through it, so the per-`(pid, window_id)` element-index cache is shared
  across calls.
- **38 tools** total (37 callable + the CLI surface).

## Calling convention

```bash
cua-driver call <tool> '<json-args>'           # positional JSON
'<json-args>' | cua-driver call <tool>         # JSON via stdin (preferred)
```

Prefer **stdin** for any payload containing Windows paths: PowerShell 5.1 strips quotes
around JSON field names in positional multi-field args, and backslashes in a positional
arg trip the JSON parser (`invalid escape`). Building the JSON in code and piping it in
side-steps both. All output is JSON on stdout; tool errors come back as
`{"isError": true, ...}` or a plain `error: ...` / `Failed to ...` line on stderr with a
non-zero exit.

## The canonical loop

Every interaction with a window is **scan → act → verify**:

```
SCAN    get_window_state(pid, window_id)   → tree_markdown with [element_index N] tags
ACT     click / type_text / press_key ...  → address by element_index
VERIFY  get_window_state(pid, window_id)   → confirm the state changed
```

Two invariants:
- **A.** Call `get_window_state` once per turn per `(pid, window_id)` before any
  element-indexed action — that call builds the cache.
- **B.** Each `get_window_state` snapshot **replaces** the previous index map for that
  window. Treat `element_index` values as turn-scoped tokens.

`element_index` works on backgrounded / minimized / off-screen windows and is preferred
over pixel coordinates. Reach for `x, y` only on canvas / video / WebGL / custom-drawn
surfaces with no UIA peer.

### `dispatch` (on all input tools)

`click`, `double_click`, `right_click`, `drag`, `scroll`, `press_key`, `hotkey`,
`type_text` accept `dispatch`:

- `"background"` (default) — never raises the window; routes via UIA Invoke / PostMessage
  and falls back to coordinate injection for targets that drop posted input. Returns a
  `background_unavailable` error only for inputs injection can't express.
- `"foreground"` — accepts a brief `SetForegroundWindow` swap (SendInput path).
- `"auto"` — historical heuristics.

---

## 1. Discovery (no pid required)

| Tool | Required args | Output |
|---|---|---|
| `list_apps` | — | `{apps:[…], processes:[…]}`. Each app: `name, pid, running, active, kind ("desktop"\|"uwp"), launch_path, bundle_id, last_used, windows[]`. `pid` is 0 when not running. Use for "is X installed / running?". |
| `list_windows` | — (opt `pid`, `on_screen_only`) | `{windows:[…]}`. Each: `window_id (HWND), pid, app_name, title, bounds{x,y,width,height}, layer, z_index, is_on_screen`. Use for window-level reasoning. |
| `get_accessibility_tree` | — | `{processes:[{name,pid}…], windows:[…]}`. Fast desktop snapshot; for a single window's actionable tree use `get_window_state`. |
| `get_screen_size` | — | `{width, height, scale_factor}` (logical points). |
| `get_cursor_position` | — | `{x, y}` (screen points, top-left origin). |

Sample outputs (this machine):

```jsonc
// get_screen_size
{ "width": 1536, "height": 960, "scale_factor": 1.0 }
// get_cursor_position
{ "x": 930, "y": 581 }
// list_windows[0]
{ "window_id": 1116708, "pid": 35436, "app_name": "WindowsTerminal.exe",
  "title": "…", "bounds": {"x":347,"y":164,"width":1483,"height":762},
  "layer": 0, "z_index": 13, "is_on_screen": true }
// list_apps.apps[0]
{ "name": "WindowsTerminal.exe", "pid": 35436, "running": true, "active": true,
  "kind": "desktop", "launch_path": null, "bundle_id": null, "last_used": null,
  "windows": [] }
```

## 2. Launch & lifecycle

| Tool | Required args | Notes |
|---|---|---|
| `launch_app` | one of `path` / `name` / `aumid` / `bundle_id` / `launch_path` / `urls` | Launches **hidden** (`SW_SHOWNOACTIVATE`, no focus steal). Returns `{pid, name, running, active, bundle_id, windows[]}`. |
| `kill_app` | `pid` | `taskkill /F` equivalent. Prefer clicking the X / WM_CLOSE first; escalate to this for UWP/WinUI3 apps that suspend instead of exit. |
| `bring_to_front` | `pid` (opt `window_id`) | **Deliberately** raises the window to foreground (breaks the no-foreground contract). Returns `{previous_fg_hwnd, now_fg_hwnd}`. Windows-only. |

### `launch_app` routing (read this before configuring apps.yaml)

Precedence: `launch_path` > `path` > `aumid` / `bundle_id` (with `!`) > `name`.

- **`aumid` / packaged** — `IApplicationActivationManager::ActivateApplication`, returns
  the **real packaged pid**. Required on Win11 for built-in apps (Notepad, Calculator,
  Paint) whose System32 `.exe` is a ~7 KB stub that exits immediately.
- **`name`** — tries `shell:AppsFolder` first (packaged path); on a miss falls back to
  `ShellExecuteEx` PATH search.
- **`path`** — full `.exe`. **Windows-only canonical form** for desktop apps.
- `electron_debugging_port` / `webkit_inspector_port` / `creates_new_application_instance`
  are accepted for parity but **no-op on Windows**. `additional_arguments` *is* honored.
- `start_minimized: true` → `SW_SHOWMINNOACTIVE` (UIA/background dispatch still work
  minimized; only `screenshot` and `dispatch:"foreground"` need it restored).

> ⚠️ **Launcher-stub trap (verified, issue #20 background).** Launching a `.cmd`/`.bat`
> shim (e.g. VS Code's `code` on PATH) via `name` returns `running:true` but **`pid:0`
> and `windows:[]`** — the shim spawns the real exe and exits, so the driver can't capture
> a pid. Recover the real pid with `list_windows` a moment later. Also: launching a
> per-user `.exe` by `path` while the daemon runs **elevated** can fail with
> `ERROR_CANCELLED (0x800704C7)` — an integrity-level mismatch.

```jsonc
// launch_app {"name":"code"}  → stub shim, not trackable
{ "active": false, "bundle_id": null, "name": "code", "pid": 0,
  "running": true, "windows": [] }
```

## 3. Perception

| Tool | Required args | Output |
|---|---|---|
| `get_window_state` | `pid`, `window_id` | `{element_count, pid, window_id, tree_markdown}` (+ screenshot on `som`/`vision`). `capture_mode`: `som` (tree+shot, default) \| `vision` (shot only, no index cache) \| `ax` (tree only). `query` = case-insensitive substring filter on `tree_markdown` (indices unchanged). |
| `zoom` | `pid`, `window_id`, `x1,y1,x2,y2` | Full-res JPEG crop (≤500px wide, +20% padding). Pass `from_zoom=true` to later click/type to translate coords back. |

`tree_markdown` shape (each actionable node tagged `[element_index N]`):

```
- Window "⠐ Debug VSCode start process code"
  - [0] List [id=TabListView actions=[scroll]]
    - [1] TabItem "PowerShell" [actions=[select]]
      - [3] Button "Close Tab" [id=CloseButton actions=[invoke]]
```

> If `element_count == 0` with an empty `tree_markdown`, the window isn't realized /
> readable yet (not necessarily a permission failure on Windows) — re-scan, don't address
> stale indices.

## 4. Action — pointer

All take `pid` (required) and `dispatch`. Address by **`element_index` + `window_id`**
(preferred) **or** **`x, y`** window-local screenshot pixels (exactly one mode).

| Tool | Required | Extra |
|---|---|---|
| `click` | `pid` + (`element_index`+`window_id`) or (`x`+`y`) | `button: left\|right\|middle`, `count: 1-3`, `from_zoom`. |
| `double_click` | same | `from_zoom`, `modifier` (no-op on Win). |
| `right_click` | same | `from_zoom`, `modifier` (no-op). |
| `drag` | `pid`, `from_x, from_y, to_x, to_y` | `button`, `duration_ms` (def 500), `steps` (def 20), `window_id`, `from_zoom`. Pixel-only, single-window. |
| `scroll` | `pid`, `direction` (`up\|down\|left\|right`) | `by: line\|page` (def line), `amount` (def 3), `window_id`. |
| `move_cursor` | `x, y` | Moves the **agent-cursor overlay** only, not the real mouse. |

## 5. Action — keyboard / value

| Tool | Required | Notes |
|---|---|---|
| `type_text` | `pid`, `text` | Char-by-char `WM_CHAR`, no focus steal. `delay_ms` 0–200 (def 30). **XAML/WinUI3/UWP hosts** (modern Notepad, Calculator, Settings…) require `element_index`+`window_id` and route via UIA `ValuePattern.SetValue`; calling without it on such a host returns an actionable error. |
| `press_key` | `pid`, `key` | Single key via PostMessage. Vocab: return, tab, escape, up/down/left/right, space, delete, home, end, pageup, pagedown, f1-f12, letters, digits. Opt `modifiers[]`. `element_index` no-op on Win. |
| `hotkey` | `pid`, `keys[]` (≥2) | Combo, e.g. `["ctrl","c"]`. Modern XAML → UIA AcceleratorKey; legacy Win32 + modifiers → SendInput (brief fg swap, needs daemon UIAccess); legacy Win32 no-modifier → PostMessage. Modifiers: ctrl/control, shift, alt, win. |
| `set_value` | `pid`, `window_id`, `element_index`, `value` | Direct UIA `ValuePattern.SetValue` (text fields, sliders, combo selection). For free-form web inputs prefer `type_text`. |

## 6. Browser / DOM

| Tool | Required | Actions |
|---|---|---|
| `page` | `action` (+ `pid`/`window_id`) | `execute_javascript` (needs `javascript`), `get_text`, `query_dom` (needs `css_selector`, opt `attributes[]`), `click_element` (needs `selector`; animates the agent cursor), `enable_javascript_apple_events` (macOS only). Chromium/Electron via CDP when launched with a debug port. |

## 7. Sessions & agent cursor

| Tool | Required | Notes |
|---|---|---|
| `start_session` | `session` | Named, color-coded identity for a run; cursor/config/recording key on it. Idempotent. |
| `end_session` | `session` | Removes the run's cursor, stops its recording, clears its config. Idempotent — call when a run finishes. |
| `set_agent_cursor_enabled` | `enabled` | Toggle the visual overlay (default on). |
| `set_agent_cursor_style` | — | `gradient_colors[]`, `bloom_color`, `image_path`. |
| `set_agent_cursor_motion` | — | Appearance + Bezier motion knobs (`arc_size`, `spring`, `glide_duration_ms`, …). |
| `get_agent_cursor_state` | — | Read-only current cursor config. |

## 8. Recording & replay

| Tool | Required | Notes |
|---|---|---|
| `start_recording` | `output_dir` | Writes `turn-NNNNN/` (app_state.json, screenshot.png, action.json, click.png) per action tool. `record_video:true` → mp4 (needs ffmpeg on Win). |
| `stop_recording` | — | Finalizes the mp4; unconditional. |
| `get_recording_state` | — | Read-only recorder state. |
| `replay_trajectory` | `dir` | Re-invokes recorded calls in lexical order. Element-indexed actions fail across sessions (indices are per-snapshot); pixel + keyboard replay cleanly. `delay_ms`, `stop_on_error`. |

## 9. Config & diagnostics

| Tool | Required | Notes |
|---|---|---|
| `get_config` | — | `{schema_version, version, platform, capture_mode, max_image_dimension, agent_cursor, …}`. |
| `set_config` | — | Swift shape `{key, value}` (preferred) or per-field. Keys: `capture_mode` (`som\|vision\|ax`), `max_image_dimension`, `experimental_pip*`. |
| `check_permissions` | — | Windows permission check. |
| `check_for_update` | — | Read-only GitHub release check; never installs. |
| `debug_window_info` | `pid` | Dumps window classes, owning exe, focused UIA element + supported patterns. For designing input routing on XAML/UWP. |
| `install_ffmpeg` | — | Two-step; reports the command unless `confirm:true`. For `start_recording` video on Win/Linux. |

---

## Notes for the computer skill

- The skill should drive **only** these tools, exclusively through `cua-driver call`
  (no `cua` Python SDK, no PowerShell `Start-Process`).
- Launch must tolerate the `pid:0` stub case (§2) by recovering the real pid via
  `list_windows(pid)` / matching on window title.
- The vision loop uses `get_window_state` with `capture_mode:"vision"` for screenshots
  and `som`/`ax` when it wants the element tree.
- `bring_to_front` is the Windows way to front+activate (the SDK's `front_and_maximize`
  helper is replaced by this); maximize is a `hotkey`/window op, not a dedicated tool.

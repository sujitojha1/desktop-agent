# Desktop Agent

A computer-use agent that drives **real Windows apps** to complete tasks. It plugs a
`computer` skill into the Session 9 orchestrator: a planner decomposes the goal, then
the skill runs a **Scan → Act → Verify** loop over the live desktop through the
[`cua-driver`](https://github.com/trycua/cua) daemon (UI Automation), escalating only when
the cheap layer fails:

- **Layer 1 / 2a — deterministic** — launch + known hotkeys; no LLM, no vision.
- **Layer 2b — AX tree + cheap text LLM** — read the UIA tree as markdown, click by element index.
- **Layer 3 — vision** — only when the tree is empty (canvas/game): screenshot → set-of-marks → VLM.

All LLM/vision calls route through the local **V9 gateway** (no paid APIs).

## Setup

**Requirements:** Windows 10/11, Python 3.11+ with [`uv`](https://docs.astral.sh/uv/).


```bash
# 1. Start the V9 gateway (port 8109)
cd llm_gatewayV9 && uv run python main.py

# 2. Install + start the cua-driver daemon (Windows, UI Automation)
#    https://github.com/trycua/cua  →  cua-driver must be on PATH
#    (override the binary with CUA_DRIVER_BIN)

# 3. Run a task
cd code && uv run python flow.py "open calculator and compute 7 * 8"
```

Apps are registered data-only in [`code/computer/apps.yaml`](code/computer/apps.yaml) — add an
entry to teach the agent a new app, no Python change.

## The 5 Windows scenarios

Each exercises a different point on the cascade. Demo links are placeholders — replace once recorded.

| # | Scenario | App | Layer | Demo |
|---|----------|-----|-------|------|
| 1 | Arithmetic via hotkeys (zero vision) | Calculator | 2a deterministic | [demo](#) |
| 2 | Type & save a note, verify content | Notepad | 2b AX tree | [demo](#) |
| 3 | Fill grid cells, commit per cell | Excel | 2b AX tree | [demo](#) |
| 4 | Drive an Electron app via CDP | VS Code | Electron / CDP | [demo](#) |
| 5 | Act on a canvas with no AX tree | Canva | 3 vision | [demo](#) |

## Tests

```bash
cd code && uv run pytest tests/ -v        # skill/registry/recovery suites (no daemon needed)
```

## License

MIT.

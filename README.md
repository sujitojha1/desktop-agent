# 🖥️ Desktop Agent

![License](https://img.shields.io/badge/license-MIT-blue.svg)
![Python](https://img.shields.io/badge/python-3.11+-brightgreen.svg)
![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-0078D4?logo=windows)
![Status](https://img.shields.io/badge/status-active-success)

> A computer-use agent that drives **real Windows apps** to complete tasks autonomously — no paid APIs required.

It plugs a `computer` skill into the **Session 9 orchestrator**: a planner decomposes the goal, then the skill runs a **Scan → Act → Verify** loop over the live desktop through the [`cua-driver`](https://github.com/trycua/cua) daemon (UI Automation), escalating only when a cheaper layer fails.

---

## 📋 Table of Contents

- [Architecture](#architecture)
- [Setup](#setup)
- [Demo Scenarios](#demo-scenarios)
- [Tests](#tests)
- [License](#license)

---

## 🏗️ Architecture

The agent uses a **3-layer cascade** — starting cheap and deterministic, escalating to vision only when necessary:

| Layer | Name | Trigger | Method |
|-------|------|---------|--------|
| 1 / 2a | **Deterministic** | Always tried first | Launch app + known hotkeys — zero LLM, zero vision |
| 2b | **AX Tree + Text LLM** | Tree is available | Read UIA tree as markdown, click by element index |
| 3 | **Vision** | Tree is empty (canvas/game) | Screenshot → set-of-marks → VLM |

All LLM/vision calls route through the local **V9 gateway** (port `8109`) — no paid APIs needed.

---

## ⚙️ Setup

**Requirements:** Windows 10/11 · Python 3.11+ · [`uv`](https://docs.astral.sh/uv/)

### 1 — Start the V9 Gateway

```bash
cd llm_gatewayV9 && uv run python main.py
```

### 2 — Install & Start the `cua-driver` Daemon

Follow the setup guide at [trycua/cua](https://github.com/trycua/cua) — the `cua-driver` binary must be on your `PATH`.
You can override the binary path with the `CUA_DRIVER_BIN` environment variable.

### 3 — Run a Task

```bash
cd code && uv run python flow.py "open calculator and compute 7 * 8"
```

> **Tip:** Apps are registered data-only in [`code/computer/apps.yaml`](code/computer/apps.yaml). Add an entry to teach the agent a new app — no Python changes needed.

---

## 🎬 Demo Scenarios

Each scenario exercises a different point on the layer cascade:

| # | Scenario | App | Layer | Demo |
|---|----------|-----|-------|------|
| 1 | Arithmetic via hotkeys (zero vision) | Calculator | 2a — Deterministic | [▶ demo](#) |
| 2 | Type & save a note, verify content | Notepad | 2b — AX Tree | [▶ demo](#) |
| 3 | Fill grid cells, commit per cell | Excel | 2b — AX Tree | [▶ demo](#) |
| 4 | Drive an Electron app via CDP | VS Code | Electron / CDP | [▶ demo](#) |
| 5 | Act on a canvas with no AX tree | Canva | 3 — Vision | [▶ demo](#) |

> 📹 Demo links will be updated once recordings are available.

---

## 🧪 Tests

Run the full skill/registry/recovery test suites — no daemon required:

```bash
cd code && uv run pytest tests/ -v
```

---

## 📄 License

This project is licensed under the [MIT License](LICENSE).

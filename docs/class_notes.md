# Class Notes — Session 10: Desktop Automation (Computer-Use Agents)

> EAG V3 · Session recorded 2026-06-13 · Distilled key notes.
> Source materials (transcript, slides PDF, assignment) live in the gitignored
> `docs/class notes/` folder. This file is the committed, distilled artifact.

---

## 1. The big shift

- In 2024 a computer-use agent meant building a **custom vision pipeline** from scratch:
  icon detection, OCR, button classifiers, VLM coordinate mapping (with DPR/DPI pain).
- Today the **accessibility tree (AX tree)** is mature across all major OSes, so we no
  longer go "direct vision" on desktops. Vision is now the **last resort**, not the default.
- Net effect: a desktop agent is now essentially the **same problem as browser-use** from
  the previous session — you drop a new skill into Session 9, no core changes needed.

## 2. Cua — the driver we build on

- **Cua** (`cua.ai`, MIT license) is a well-maintained, cross-platform driver. A Rust binary
  that reads the AX tree.
- One **unified driver core** exposes all three desktop OSes (and Android). Same **34 tools**
  work across Windows / macOS / Linux.
- It **launches apps, walks the AX tree, synthesizes** clicks / keystrokes / drags / scrolls /
  hotkeys / screenshots, and **records trajectories**.
- Speaks **JSON over a Unix socket**. A background **daemon** must be running per-OS
  (install the OS-specific daemon first).
- **What Cua does NOT do:** planning, goal decomposition, perception filtering, error
  recovery, vision judgment. **That is our job** — Cua is the low-level tool, we build the agent.

## 3. Three kinds of target application

| Type | How to drive it | Notes |
|------|-----------------|-------|
| **Native** (calculator, notes, mail, settings, office) | AX tree free from the OS | Easiest — everything works in your favor |
| **Electron** (VS Code, Slack, Discord, Notion, Cursor, Obsidian, Linear, 1Password, anti-gravity, codex, claude code) | Launch with **CDP** (Chrome DevTools Protocol) via `electron_debugging_port` | Electron ships Chromium → it's a browser inside. Cua must be **told** it's Electron or you get count zero |
| **Canvas / OpenGL / games** (Figma, Google Maps, browser games) | **Vision only** — screenshot + set-of-marks + VLM | No AX tree exists. Expensive |
| **Protected** (Kindle, streaming apps, bank apps) | Nothing — AX deliberately disabled, synthetic input forbidden | OS returns black screen on capture |

## 4. The five-layer cascade (cost discipline)

Escalate only when the cheaper layer fails. **Spend money as late as possible.**

- **Layer 1 — Extract / Read.** Just read AX content, clipboard, or open file. No interaction.
  e.g. "open this PDF and tell me my payment due." Cheapest.
- **Layer 2a — Deterministic hotkeys.** Known key sequences (e.g. calculator `4 * 5 =`,
  `Ctrl+T`). **No LLM in the functioning** — the *sequence* is chosen by the LLM, but the
  key-presses themselves run via Cua with zero vision. Very cheap.
- **Layer 2b — AX tree + cheap text LLM.** Return AX tree as markdown with element indices;
  a cheap text LLM decides what to click/type. **This is where most assignment work lives.**
- **Layer 3 — Vision (VLM).** AX tree comes back **empty / count zero**, or the goal is
  inherently visual. Screenshot → set-of-marks → VLM picks a numbered box → Cua clicks the
  coordinate. **~10× more expensive** than a 40–50 token text LLM call.
- **(Cross-cutting) — Permissions / access.** macOS **TCC** (accessibility + screen
  recording), Windows **UAC**, Linux **portals**; **CDP** for Electron; elevated/admin mode
  for sensitive actions. Without these the lower layers silently fail.

> Clarification from Q&A: even "no-LLM" deterministic steps still had an LLM choose the
> sequence. The distinction is *internal functioning* (no per-action LLM) vs *which buttons*
> (LLM/AX decided up front). The **actions are always executed by Cua.**

## 5. Scan → Act → Verify (the core loop)

This single discipline decides whether your agent works.

1. **Scan** — get current window state (read / deterministic / AX / vision).
2. **Act** — do one thing.
3. **Verify** — re-scan and confirm the state actually changed. **Do not assume.**

Software gives **no physical feedback** (unlike touching a live socket) — you must re-open
your eyes (re-read AX) to know a click landed.

### The element-index trap (two silent invariants)

- **Element indices keep changing.** Enlarge a window → formatting icons appear → the Send
  button that was index 5 is now index 50. You click Bold thinking you sent the email.
- **Invariant 1 — stale cache:** click idx 5 after acting → "I don't know where 5 is, not in
  cache."
- **Invariant 2 — snapshot replacement:** each new `get_window_state` replaces the previous
  snapshot; the LLM may still reason over the old one.
- **Verify must do two things:** (a) get the fresh state, **and** (b) update the LLM with the
  new state. Skip the rescan and you loop forever (`5? 55? 555?` → Gemini 503s at you).
- The AX tree is **cheap — run it often** ("120 fps, it's free"). Re-scan after every action.

## 6. Six ways you get `element count == 0`

1. **Permission not granted** — TCC / portal / UAC denied.
2. **Background launch** — app opened behind; AX sees nothing. **Wait for foreground** before acting.
3. **Qt app** — `QT_ACCESSIBILITY=1` not set (Qt is an accessibility nightmare).
4. **Cache miss** — UI reflowed between calls; just re-fetch.
5. **Electron not launched via CDP** — Windows is opaque to AX; needs the debugging port.
6. **Canvas / game / protected target** — no AX tree exists; escalate to vision.

> Safeguard: write a small scaffold that checks (1) granted, (2) activated, and surfaces a
> clear message to the user when count is zero.

## 7. Architecture mindset (Q&A highlights)

- **Think like the Matrix architect** — enumerate every scenario that can change/break
  *before* writing code. Write it down yourself first; then debate with the AI. Don't let the
  agent jump straight to code.
- **Fail fast** — like learning a video game; competence comes from iterations.
- No durable "architecture course" exists because the tooling changes too fast (Cua didn't
  exist two years ago). Skill = decompose a problem into a thousand pieces.
- **Detecting app type:** don't hardcode millions of apps. Build a scaffold that tries
  layer 1 → 2 → 3 and falls through (fast, ~2s, no LLM). For apps you *always* use (VS Code),
  add them to a **hot list** to skip the probing.
- **Cost/quality table is mandatory for clients:** cheap ≈ 62% success; balanced ≈ 86%;
  expensive ≈ 94% at ₹X/action. Show this — clients expect miracles otherwise.

## 8. Safety / enterprise

- Run Cua on the **real host** = it has all your access. Use a **separate guest account** with
  minimal data exposure.
- Always keep a **backup** of any data the agent can touch (it may delete things).
- **Verify every single step** in enterprise.
- Keep a **kill switch** (kill-app / remote Ctrl+Z) to stop damage in progress.
- Vet every MCP tool before enterprise use; ideally an LLM audits the MCP code for leaks.
- Full **sandboxing** (containerized, network-clipped functions) is **next session's** topic.

## 9. Misc clarifications

- **Cua vs Playwright:** Playwright is browser-only (rich: instances, cookies, headless).
  Cua is desktop, limited to its 34 functions, **always needs a visible browser/app open**,
  and **always reports to an external LLM** (no internal LLM).
- **Browser-use** can only control a browser; it cannot drive native or Electron desktop apps
  (those need OS-level access).
- **CPU-only models:** too slow. Need a GPU — a single **Mac mini / 4090** (~₹60k) can serve
  a local VLM to many nodes and keep everything on-prem.
- AppleScript / `osascript` activation can bring an app to the foreground and gives broader
  control on macOS.

---

## 10. The assignment (Session 10)

Build a **Computer-Use skill** that drops into the Session 9 catalogue and solves **three real
tasks** on your primary OS, respecting the five-layer cascade so the discipline is visible in
code. **Record every run** with `start_recording`; submit the trajectory directory as evidence.

**Pick 3 of these 6 tasks:**
1. Calculator / arithmetic via deterministic hotkeys (Layer 2a).
2. Spreadsheet or notes app via AX tree + cheap text LLM (Layer 2b).
3. Electron app (VS Code, Slack, Cursor, Notion, Discord) via the page tool + `electron_debugging_port`.
4. Canvas / game target forcing Layer 3 vision (browser game, Figma desktop, sketch app with no ARIA).
5. Email / message draft composition exercising Layer 2b with strong verification.
6. Multi-app workflow switching between two apps and moving data between them.

**Constraints:**
- ≥ 1 task uses **vision**.
- ≥ 1 task uses the **Electron / CDP** path.
- ≥ 1 task completes with **zero vision calls** (pure deterministic).
- Use the **V9 gateway** for LLM/vision (no paid APIs; keep tasks simple/safe — no real
  TDS filing, no real purchases).

**Submission:** a GitHub repo **README** (architecture / five layers, the three tasks,
cascade decisions, failure modes encountered) **+ a YouTube demo** showing the agent operating
live for at least one task with the **agent-cursor overlay visible**.

> Next session (11): **channels** — "what made open-claw."

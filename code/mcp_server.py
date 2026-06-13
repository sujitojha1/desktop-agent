"""
MCP server for EAGV3 Session 7.

Eleven tools, stdio transport:
    web_search, fetch_url, get_time, currency_convert,
    read_file, list_dir, create_file, update_file, edit_file,
    index_document, search_knowledge

web_search:        Tavily primary, DuckDuckGo fallback. Hard-capped at 5 results.
fetch_url:         crawl4ai only. Clean markdown via headless Chromium.
index_document:    Chunks a sandbox file or artifact and writes the chunks as
                   fact records into Memory, where they become FAISS-searchable.
search_knowledge:  Vector search over indexed facts. Same backend as
                   memory.read but exposed to the model as a tool.

Usage for tavily and duckduckgo is logged to ./usage.json with monthly
rollover and a soft cap of 950/1000 on Tavily.

File tools are sandboxed under ./sandbox/. Run:  python mcp_server.py
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from ddgs import DDGS
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

# Same-directory imports for the Memory and Artifact services so that the
# new index_document / search_knowledge tools can delegate into them.
import sys
sys.path.insert(0, str(Path(__file__).parent))
import artifacts as _artifacts  # noqa: E402
import memory as _memory  # noqa: E402
from computer.backend import CuaBackend
_backend = CuaBackend.shared()

MAX_SEARCH_RESULTS = 5  # hard cap — Tavily prices per result

load_dotenv(Path(__file__).parent / ".env")

mcp = FastMCP("eagv3-s7-server")

SANDBOX = Path(__file__).parent / "sandbox"
SANDBOX.mkdir(exist_ok=True)

USAGE_PATH = Path(__file__).parent / "usage.json"
MONTHLY_CAP = 950  # leave 50/mo headroom on Tavily
_usage_lock = threading.Lock()


def _safe(path: str) -> Path:
    p = (SANDBOX / path).resolve()
    base = SANDBOX.resolve()
    if p != base and base not in p.parents:
        raise ValueError(f"Path '{path}' escapes the sandbox")
    return p


def _empty_usage(month: str) -> dict:
    return {
        "month": month,
        "tavily": {"count": 0, "errors": 0},
        "duckduckgo": {"count": 0, "errors": 0},
    }


def _load_usage() -> dict:
    month = datetime.now().strftime("%Y-%m")
    if not USAGE_PATH.exists():
        return _empty_usage(month)
    try:
        data = json.loads(USAGE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _empty_usage(month)
    if data.get("month") != month:
        return _empty_usage(month)
    for k in ("tavily", "duckduckgo"):
        data.setdefault(k, {"count": 0, "errors": 0})
    return data


def _save_usage(data: dict) -> None:
    USAGE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _bump(provider: str, field: str = "count") -> None:
    with _usage_lock:
        data = _load_usage()
        data[provider][field] = data[provider].get(field, 0) + 1
        _save_usage(data)


def _under_cap(provider: str) -> bool:
    return _load_usage()[provider]["count"] < MONTHLY_CAP


def _tavily_search(query: str, max_results: int) -> list[dict]:
    from tavily import TavilyClient

    client = TavilyClient(os.environ["TAVILY_API_KEY"])
    resp = client.search(query=query, max_results=max_results, search_depth="advanced")
    return [
        {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": r.get("content", ""),
        }
        for r in resp.get("results", [])
    ]


def _ddg_search(query: str, max_results: int) -> list[dict]:
    hits: list[dict] = []
    with DDGS() as ddgs:
        for backend in ("auto", "html", "lite"):
            try:
                hits = list(ddgs.text(query, max_results=max_results, backend=backend))
            except Exception:
                hits = []
            if hits:
                break
    return [
        {
            "title": h.get("title", ""),
            "url": h.get("href", ""),
            "snippet": h.get("body", ""),
        }
        for h in hits
    ]


async def _crawl4ai_fetch(url: str) -> dict:
    from crawl4ai import AsyncWebCrawler

    # crawl4ai uses Rich which writes via its own captured stdout reference, so
    # contextlib.redirect_stdout doesn't catch it. Redirect at the file-descriptor
    # level — crawl4ai's banner / [FETCH] / [SCRAPE] markers would otherwise
    # corrupt the MCP stdio JSON-RPC stream.
    saved_fd = os.dup(1)
    os.dup2(2, 1)
    try:
        async with AsyncWebCrawler(verbose=False) as crawler:
            r = await crawler.arun(url=url)
    finally:
        os.dup2(saved_fd, 1)
        os.close(saved_fd)
    # r.markdown is a str subclass (StringCompatibleMarkdown) that Pydantic
    # serializes as {} because its real field is private. Pull the raw string
    # out and force a plain str so FastMCP serializes correctly.
    md = r.markdown
    raw = (
        getattr(md, "raw_markdown", None)
        or getattr(md, "fit_markdown", None)
        or md
        or r.cleaned_html
        or r.html
        or ""
    )
    text = str(raw)
    return {
        "status": int(getattr(r, "status_code", None) or 200),
        "content_type": "text/markdown",
        "length_bytes": len(text.encode("utf-8")),
        "text": text,
    }


@mcp.tool()
def web_search(query: str, max_results: int = 5) -> list[dict]:
    """Search the web (Tavily primary, DDG fallback). Hard-capped at 5 results. Example: web_search("python asyncio tutorial", 3)."""
    max_results = max(1, min(max_results, MAX_SEARCH_RESULTS))
    if os.environ.get("TAVILY_API_KEY") and _under_cap("tavily"):
        try:
            results = _tavily_search(query, max_results)
            if results:
                _bump("tavily")
                return results
        except Exception:
            _bump("tavily", "errors")
    results = _ddg_search(query, max_results)
    _bump("duckduckgo")
    return results


@mcp.tool()
async def fetch_url(url: str, timeout: int = 20) -> dict:
    """Fetch clean markdown from a URL via crawl4ai (headless Chromium). Example: fetch_url("https://example.com")."""
    return await _crawl4ai_fetch(url)


@mcp.tool()
def get_time(timezone: str = "UTC") -> dict:
    """Current time in a named IANA timezone. Example: get_time("Asia/Kolkata")."""
    tz = ZoneInfo(timezone)
    now = datetime.now(tz)
    offset = now.utcoffset()
    offset_hours = offset.total_seconds() / 3600 if offset else 0.0
    return {
        "iso": now.isoformat(),
        "human": now.strftime("%A, %d %B %Y %H:%M:%S %Z"),
        "timezone": timezone,
        "offset_hours": offset_hours,
    }


@mcp.tool()
def currency_convert(amount: float, from_currency: str, to_currency: str) -> dict:
    """Convert money between ISO-3 currencies via frankfurter.dev. Example: currency_convert(100, "USD", "INR")."""
    f = from_currency.upper()
    t = to_currency.upper()
    url = f"https://api.frankfurter.dev/v1/latest?amount={amount}&base={f}&symbols={t}"
    with httpx.Client(timeout=20, follow_redirects=True) as client:
        r = client.get(url)
        r.raise_for_status()
        data = r.json()
    converted = data["rates"][t]
    return {
        "amount": amount,
        "from": f,
        "to": t,
        "rate": converted / amount if amount else 0.0,
        "converted": converted,
        "date": data["date"],
        "source": "frankfurter.dev",
    }


@mcp.tool()
def read_file(path: str) -> dict:
    """Read a UTF-8 text file from the sandbox. Example: read_file("notes.txt")."""
    p = _safe(path)
    text = p.read_text(encoding="utf-8")
    return {
        "path": path,
        "size_bytes": p.stat().st_size,
        "content": text,
        "encoding": "utf-8",
    }


@mcp.tool()
def list_dir(path: str = ".") -> dict:
    """List a directory inside the sandbox. Example: list_dir(".")."""
    # NOTES_RUNS §6 (1): a list[dict] return was being rendered as one MCP
    # TextContent per entry. After agent7.py's 300-char clip and decision.py's
    # downstream slicing, only the first 2-3 file dicts survived into the
    # Decision prompt, and Decision then declared the directory complete at
    # whatever it could see. Returning a single dict with `count` and a flat
    # `names` list keeps the cardinality visible even under truncation.
    p = _safe(path)
    entries = []
    names: list[str] = []
    for child in sorted(p.iterdir()):
        is_dir = child.is_dir()
        entries.append({
            "name": child.name,
            "type": "dir" if is_dir else "file",
            "size_bytes": 0 if is_dir else child.stat().st_size,
        })
        names.append(child.name)
    return {"path": path, "count": len(entries), "names": names, "entries": entries}


@mcp.tool()
def create_file(path: str, content: str) -> dict:
    """Create a new file in the sandbox; errors if it exists. Example: create_file("hello.txt", "hi")."""
    p = _safe(path)
    if p.exists():
        raise ValueError(f"File '{path}' already exists")
    if not p.parent.exists():
        raise ValueError(f"Parent directory of '{path}' does not exist")
    p.write_text(content, encoding="utf-8")
    return {"ok": True, "path": path, "size_bytes": p.stat().st_size}


@mcp.tool()
def update_file(path: str, content: str) -> dict:
    """Overwrite an existing sandbox file. Example: update_file("hello.txt", "new body")."""
    p = _safe(path)
    if not p.exists():
        raise ValueError(f"File '{path}' does not exist")
    p.write_text(content, encoding="utf-8")
    return {"ok": True, "path": path, "size_bytes": p.stat().st_size}


@mcp.tool()
def edit_file(path: str, find: str, replace: str, replace_all: bool = False) -> dict:
    """Find-and-replace inside a sandbox file. Example: edit_file("hello.txt", "foo", "bar")."""
    p = _safe(path)
    text = p.read_text(encoding="utf-8")
    count = text.count(find)
    if count == 0:
        raise ValueError(f"'{find}' not found in '{path}'")
    if count > 1 and not replace_all:
        raise ValueError(
            f"'{find}' occurs {count} times in '{path}'; pass replace_all=True"
        )
    new_text = text.replace(find, replace) if replace_all else text.replace(find, replace, 1)
    p.write_text(new_text, encoding="utf-8")
    replacements = count if replace_all else 1
    return {
        "ok": True,
        "path": path,
        "replacements": replacements,
        "size_bytes": p.stat().st_size,
    }


# ── document indexing (Session 7) ───────────────────────────────────────────

def _read_for_index(path: str) -> tuple[str, str]:
    """Return (content, source_label) for an indexable file or artifact."""
    if path.startswith("art:"):
        return _artifacts.get_bytes(path).decode("utf-8", errors="replace"), path
    p = _safe(path)
    return p.read_text(encoding="utf-8"), f"sandbox:{path}"


def _chunk_text(text: str, size: int = 400, overlap: int = 80) -> list[str]:
    """Sliding-window chunking by word count. S7 default; semantic chunking
    arrives in Session 8."""
    words = text.split()
    if not words:
        return []
    chunks: list[str] = []
    stride = max(1, size - overlap)
    i = 0
    while i < len(words):
        chunks.append(" ".join(words[i:i + size]))
        if i + size >= len(words):
            break
        i += stride
    return chunks


@mcp.tool()
def index_document(path: str, chunk_size: int = 400, overlap: int = 80) -> dict:
    """Chunk a sandbox file or artifact and write each chunk into Memory as a searchable `fact`. Use this when the content must remain retrievable across later turns or runs (an indexing step before later vector queries). For one-shot inspection of a known file's contents in this turn, prefer `read_file` instead. Example: index_document("notes/spec.md")."""
    text, source = _read_for_index(path)
    if not text.strip():
        return {"path": path, "source": source, "chunks_indexed": 0, "warning": "empty content"}
    chunks = _chunk_text(text, size=chunk_size, overlap=overlap)
    run_id = f"index-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    indexed = 0
    for i, chunk in enumerate(chunks):
        preview = chunk[:120].replace("\n", " ")
        descriptor = f"[{source} chunk {i+1}/{len(chunks)}] {preview}"
        _memory.add_fact(
            descriptor=descriptor,
            value={
                "chunk": chunk,
                "chunk_index": i,
                "total_chunks": len(chunks),
                "source": source,
            },
            source=source,
            run_id=run_id,
        )
        indexed += 1
    return {
        "path": path,
        "source": source,
        "chunks_indexed": indexed,
        "chunk_size": chunk_size,
        "overlap": overlap,
    }


@mcp.tool()
def search_knowledge(query: str, k: int = 5) -> list[dict]:
    """Vector search over indexed `fact` chunks. Returns up to k ranked chunks with provenance. Call this rather than re-fetching URLs or re-reading source files whenever Memory already contains indexed chunks for the topic — that is the whole point of having indexed the corpus. Example: search_knowledge("authentication flow", 5)."""
    items = _memory.read(query, kinds=["fact"], top_k=k)
    return [
        {
            "id": item.id,
            "descriptor": item.descriptor,
            "source": item.source,
            "chunk": item.value.get("chunk") or "",
            "metadata": {k_: v for k_, v in item.value.items() if k_ != "chunk"},
        }
        for item in items
    ]


# ── Computer-use tools: consolidated SDK-primary + daemon fallback ────────────
# ┌─────────────────────────────────────────────────────────────────────────┐
# │ Each tool tries the cua.Localhost SDK first (Surface A: async, direct, │
# │ no subprocess).  If the SDK is unavailable or the operation needs      │
# │ pid-scoping / UIA element indices that the SDK lacks, the tool falls   │
# │ back to `cua-driver call` subprocess (Surface B: daemon, UIA-capable). │
# │                                                                        │
# │ Session lifecycle:                                                     │
# │   computer_start_session(id) → connects SDK + notifies daemon          │
# │   computer_end_session(id)   → disconnects SDK + notifies daemon       │
# │ Session ID flows from the orchestrator (flow.py → skills.py →          │
# │ ComputerSkill → here) so every tool call is attributable.              │
# └─────────────────────────────────────────────────────────────────────────┘
import asyncio as _aio
# The local daemon helper and SDK connection are now managed by _backend in computer/backend.py.


@mcp.tool()
def computer_list_apps() -> dict:
    """List running + installed Windows apps with pids. Start here to find a target app's pid. Example: computer_list_apps()."""
    return _cua("list_apps")


@mcp.tool()
def computer_list_windows(pid: int = 0) -> dict:
    """List top-level windows (title, pid, window_id=HWND). Pass pid>0 to scope to one app. The window_id is what you pass to computer_get_window_state and the action tools. Example: computer_list_windows(0)."""
    return _cua("list_windows", {"pid": pid} if pid else {})


@mcp.tool()
async def computer_launch_app(name: str) -> dict:
    """Launch a Windows app and return its real pid plus windows[] (each with window_id). Prefer this over list_windows for Store apps (Calculator, Notepad), whose visible window is owned by a frame host. Example: computer_launch_app("calc")."""
    # SDK primary: launch via shell, then get pid from daemon
    try:
        host = await _ensure_sdk()
        await host.shell.run(f'start "" {name}', timeout=15)
        await _aio.sleep(1.5)
        # Get proper pid/window_id from daemon
        return _cua("list_windows")
    except Exception:  # noqa: BLE001
        pass
    return _cua("launch_app", {"name": name})


@mcp.tool()
def computer_get_window_state(pid: int, window_id: int,
                              capture_mode: str = "ax", query: str = "") -> dict:
    """SCAN. Walk the window's UIA tree; returns a Markdown element list with every actionable control tagged [element_index N], plus element_count. Pass those indices to computer_click / computer_type_text / computer_set_value. Call once per turn per (pid, window_id) before any element action — the index map is replaced by the next scan. capture_mode: 'ax' (tree only, fastest), 'som' (tree+screenshot), 'vision' (screenshot only). `query` filters the markdown to matching lines + ancestors. Example: computer_get_window_state(34384, 3342692, "ax", "Button")."""
    args = {"pid": pid, "window_id": window_id, "capture_mode": capture_mode}
    if query:
        args["query"] = query
    res = _cua("get_window_state", args)
    md = res.get("tree_markdown")
    if isinstance(md, str) and len(md) > 6000:
        res["tree_markdown"] = md[:6000] + "\n…[truncated — pass `query` to filter]"
    return res


@mcp.tool()
async def computer_click(pid: int, window_id: int = 0, element_index: int = -1,
                         x: int = -1, y: int = -1, button: str = "left",
                         count: int = 1) -> dict:
    """ACT: click. Prefer element_index (from the last get_window_state) for semantic, focus-free clicks; use x,y window-local pixels only when no element covers the target. Re-scan to verify. Example: computer_click(34384, 3342692, element_index=5)."""
    # SDK primary: coordinate clicks (no element_index)
    if element_index < 0 and x >= 0 and y >= 0:
        try:
            host = await _ensure_sdk()
            for _ in range(count):
                await host.mouse.click(x, y, button)
            return {"ok": True, "path": "sdk", "session": _sdk_session_id}
        except Exception:  # noqa: BLE001
            pass  # fall through to daemon
    # Daemon fallback: pid-scoped, element-indexed
    args: dict = {"pid": pid, "button": button, "count": count}
    if window_id:
        args["window_id"] = window_id
    if element_index >= 0:
        args["element_index"] = element_index
    if x >= 0 and y >= 0:
        args["x"], args["y"] = x, y
    return _cua("click", args)


@mcp.tool()
async def computer_type_text(pid: int, text: str, window_id: int = 0,
                             element_index: int = -1) -> dict:
    """ACT: type `text` into the focused control (or `element_index` if given). Example: computer_type_text(34384, "hello", 3342692)."""
    # SDK primary: type at current focus (no element targeting)
    if element_index < 0:
        try:
            host = await _ensure_sdk()
            await host.keyboard.type(text)
            return {"ok": True, "path": "sdk", "session": _sdk_session_id}
        except Exception:  # noqa: BLE001
            pass
    # Daemon fallback: pid-scoped, element-targeted
    args: dict = {"pid": pid, "text": text}
    if window_id:
        args["window_id"] = window_id
    if element_index >= 0:
        args["element_index"] = element_index
    return _cua("type_text", args)


@mcp.tool()
async def computer_press_key(pid: int, key: str, window_id: int = 0) -> dict:
    """ACT: press one key, e.g. 'enter', 'escape', 'tab', '='. Example: computer_press_key(34384, "enter")."""
    # SDK primary: keypress at active window
    try:
        host = await _ensure_sdk()
        await host.keyboard.keypress(key)
        return {"ok": True, "path": "sdk", "session": _sdk_session_id}
    except Exception:  # noqa: BLE001
        pass
    # Daemon fallback: pid-scoped
    args: dict = {"pid": pid, "key": key}
    if window_id:
        args["window_id"] = window_id
    return _cua("press_key", args)


@mcp.tool()
async def computer_hotkey(pid: int, keys: list[str], window_id: int = 0) -> dict:
    """ACT: press a key chord, e.g. ['ctrl','s'] or ['alt','F4']. Example: computer_hotkey(34384, ["ctrl","s"])."""
    # SDK primary: chord at active window
    try:
        host = await _ensure_sdk()
        await host.keyboard.keypress([str(k) for k in keys])
        return {"ok": True, "path": "sdk", "session": _sdk_session_id}
    except Exception:  # noqa: BLE001
        pass
    # Daemon fallback: pid-scoped
    args: dict = {"pid": pid, "keys": keys}
    if window_id:
        args["window_id"] = window_id
    return _cua("hotkey", args)


@mcp.tool()
async def computer_scroll(pid: int, direction: str, window_id: int = 0,
                          amount: int = 3) -> dict:
    """ACT: scroll the focused region. direction in up/down/left/right. Example: computer_scroll(34384, "down", amount=5)."""
    # SDK primary: scroll at screen center with direction→delta mapping
    try:
        host = await _ensure_sdk()
        dx = amount if direction == "right" else (-amount if direction == "left" else 0)
        dy = amount if direction == "down" else (-amount if direction == "up" else 0)
        w, h = await host.screen.size()
        await host.mouse.scroll(w // 2, h // 2, dx, dy)
        return {"ok": True, "path": "sdk", "session": _sdk_session_id}
    except Exception:  # noqa: BLE001
        pass
    # Daemon fallback: pid-scoped
    args: dict = {"pid": pid, "direction": direction, "amount": amount}
    if window_id:
        args["window_id"] = window_id
    return _cua("scroll", args)


@mcp.tool()
def computer_set_value(pid: int, window_id: int, element_index: int, value: str) -> dict:
    """ACT: set a UIA element's value directly via ValuePattern (text fields, sliders) — faster and more reliable than typing. element_index from the last get_window_state. Example: computer_set_value(34384, 3342692, 12, "56")."""
    return _cua("set_value", {"pid": pid, "window_id": window_id,
                              "element_index": element_index, "value": value})


@mcp.tool()
def computer_get_accessibility_tree() -> dict:
    """Return a lightweight snapshot of the desktop: running processes and on-screen visible windows with their bounds and owner pid. Example: computer_get_accessibility_tree()."""
    return _cua("get_accessibility_tree")


@mcp.tool()
async def computer_get_screen_size() -> dict:
    """Return the main display's logical size and backing scale factor. Example: computer_get_screen_size()."""
    # SDK primary
    try:
        host = await _ensure_sdk()
        w, h = await host.screen.size()
        return {"width": w, "height": h, "path": "sdk", "session": _sdk_session_id}
    except Exception:  # noqa: BLE001
        pass
    return _cua("get_screen_size")


@mcp.tool()
async def computer_double_click(pid: int, window_id: int = 0, element_index: int = -1,
                                x: float = -1.0, y: float = -1.0,
                                modifier: list[str] | None = None, dispatch: str = "background",
                                from_zoom: bool = False) -> dict:
    """ACT: double-click against a target pid. Two addressing modes: element_index + window_id, or x,y window-local pixels. Example: computer_double_click(34384, 3342692, element_index=5)."""
    # SDK primary: coordinate double-clicks
    if element_index < 0 and x >= 0 and y >= 0:
        try:
            host = await _ensure_sdk()
            await host.mouse.double_click(int(x), int(y))
            return {"ok": True, "path": "sdk", "session": _sdk_session_id}
        except Exception:  # noqa: BLE001
            pass
    # Daemon fallback
    args: dict = {"pid": pid, "dispatch": dispatch, "from_zoom": from_zoom}
    if window_id:
        args["window_id"] = window_id
    if element_index >= 0:
        args["element_index"] = element_index
    if x >= 0 and y >= 0:
        args["x"], args["y"] = x, y
    if modifier:
        args["modifier"] = modifier
    return _cua("double_click", args)


@mcp.tool()
async def computer_right_click(pid: int, window_id: int = 0, element_index: int = -1,
                               x: float = -1.0, y: float = -1.0,
                               modifier: list[str] | None = None, dispatch: str = "background",
                               from_zoom: bool = False) -> dict:
    """ACT: right-click against a target pid. Two addressing modes: element_index + window_id, or x,y window-local pixels. Example: computer_right_click(34384, 3342692, element_index=5)."""
    # SDK primary: coordinate right-clicks
    if element_index < 0 and x >= 0 and y >= 0:
        try:
            host = await _ensure_sdk()
            await host.mouse.right_click(int(x), int(y))
            return {"ok": True, "path": "sdk", "session": _sdk_session_id}
        except Exception:  # noqa: BLE001
            pass
    # Daemon fallback
    args: dict = {"pid": pid, "dispatch": dispatch, "from_zoom": from_zoom}
    if window_id:
        args["window_id"] = window_id
    if element_index >= 0:
        args["element_index"] = element_index
    if x >= 0 and y >= 0:
        args["x"], args["y"] = x, y
    if modifier:
        args["modifier"] = modifier
    return _cua("right_click", args)


@mcp.tool()
async def computer_drag(pid: int, from_x: float, from_y: float, to_x: float, to_y: float,
                        button: str = "left", window_id: int = 0, steps: int = 20,
                        duration_ms: int = 500, dispatch: str = "background",
                        from_zoom: bool = False, modifier: list[str] | None = None) -> dict:
    """ACT: press-drag-release gesture from (from_x, from_y) to (to_x, to_y) in window-local pixels. Example: computer_drag(34384, 100, 100, 200, 200)."""
    # SDK primary: coordinate drag
    try:
        host = await _ensure_sdk()
        await host.mouse.drag(int(from_x), int(from_y), int(to_x), int(to_y), button)
        return {"ok": True, "path": "sdk", "session": _sdk_session_id}
    except Exception:  # noqa: BLE001
        pass
    # Daemon fallback
    args: dict = {
        "pid": pid, "from_x": from_x, "from_y": from_y,
        "to_x": to_x, "to_y": to_y, "button": button,
        "steps": steps, "duration_ms": duration_ms,
        "dispatch": dispatch, "from_zoom": from_zoom,
    }
    if window_id:
        args["window_id"] = window_id
    if modifier:
        args["modifier"] = modifier
    return _cua("drag", args)


@mcp.tool()
def computer_move_cursor(x: float, y: float, cursor_id: str = "") -> dict:
    """ACT: move the agent cursor overlay to (x, y). Does NOT move the real mouse cursor. Example: computer_move_cursor(100, 200)."""
    args: dict = {"x": x, "y": y}
    if cursor_id:
        args["cursor_id"] = cursor_id
    return _cua("move_cursor", args)


@mcp.tool()
async def computer_get_cursor_position() -> dict:
    """Return the current mouse cursor position in screen points (origin top-left). Example: computer_get_cursor_position()."""
    # SDK primary: via cua_auto
    try:
        import cua_auto
        x, y = await _aio.to_thread(cua_auto.screen.cursor_position)
        return {"x": x, "y": y, "path": "sdk", "session": _sdk_session_id}
    except Exception:  # noqa: BLE001
        pass
    return _cua("get_cursor_position")


@mcp.tool()
async def computer_bring_to_front(pid: int, window_id: int = 0) -> dict:
    """ACT: activate pid's window (or window_id if specified) -- bring it to the OS foreground. Example: computer_bring_to_front(34384)."""
    # SDK primary: activate via cua_auto window title matching for pid
    try:
        def _do():
            import cua_auto
            # Get the window title for this pid, then activate + maximize
            wins = cua_auto.window.get_all_windows() if hasattr(cua_auto.window, 'get_all_windows') else []
            for w in wins:
                try:
                    if hasattr(w, 'pid') and w.pid == pid:
                        cua_auto.window.activate_window(w)
                        cua_auto.window.maximize_window(w)
                        return True
                except Exception:  # noqa: BLE001
                    pass
            return False
        activated = await _aio.to_thread(_do)
        if activated:
            return {"ok": True, "path": "sdk", "session": _sdk_session_id}
    except Exception:  # noqa: BLE001
        pass
    # Daemon fallback: native bring-to-front by pid
    args: dict = {"pid": pid}
    if window_id:
        args["window_id"] = window_id
    return _cua("bring_to_front", args)


@mcp.tool()
async def computer_kill_app(pid: int) -> dict:
    """ACT: force-terminate a process by pid. Example: computer_kill_app(34384)."""
    # SDK primary: graceful close then force-kill via shell
    try:
        host = await _ensure_sdk()
        cmd = (f'powershell -NoProfile -Command "'
               f'$p = Get-Process -Id {pid} -ErrorAction SilentlyContinue; '
               f'if ($p) {{ $p.CloseMainWindow() | Out-Null; Start-Sleep -Seconds 1; '
               f'if (!$p.HasExited) {{ Stop-Process -Id {pid} -Force }} }}"')
        await host.shell.run(cmd, timeout=15)
        return {"ok": True, "path": "sdk", "session": _sdk_session_id}
    except Exception:  # noqa: BLE001
        pass
    return _cua("kill_app", {"pid": pid})


@mcp.tool()
def computer_debug_window_info(pid: int) -> dict:
    """Diagnostic: dump everything cua-driver sees about a pid's top-level windows from the daemon's session perspective. Example: computer_debug_window_info(34384)."""
    return _cua("debug_window_info", {"pid": pid})


@mcp.tool()
def computer_zoom(pid: int, window_id: int, x1: float, y1: float, x2: float, y2: float) -> dict:
    """Zoom into a rectangular region of a window screenshot at full resolution. Example: computer_zoom(34384, 3342692, 10, 10, 100, 100)."""
    return _cua("zoom", {"pid": pid, "window_id": window_id, "x1": x1, "y1": y1, "x2": x2, "y2": y2})


@mcp.tool()
def computer_page(action: str, pid: int = 0, window_id: int = 0,
                  selector: str = "", css_selector: str = "",
                  javascript: str = "", attributes: list[str] | None = None,
                  bundle_id: str = "", user_has_confirmed_enabling: bool = False) -> dict:
    """ACT/PERCEPTION: Interact with the browser page DOM loaded in a running app (CDP/WebKit). Example: computer_page(action="get_text", pid=34384)."""
    args: dict = {"action": action}
    if pid:
        args["pid"] = pid
    if window_id:
        args["window_id"] = window_id
    if selector:
        args["selector"] = selector
    if css_selector:
        args["css_selector"] = css_selector
    if javascript:
        args["javascript"] = javascript
    if attributes:
        args["attributes"] = attributes
    if bundle_id:
        args["bundle_id"] = bundle_id
    if user_has_confirmed_enabling:
        args["user_has_confirmed_enabling"] = user_has_confirmed_enabling
    return _cua("page", args)


@mcp.tool()
def computer_start_recording(output_dir: str, record_video: bool = False) -> dict:
    """ACT: start trajectory recording. Every subsequent action-tool invocation writes a turn folder under output_dir. Example: computer_start_recording(output_dir="/tmp/run1")."""
    return _cua("start_recording", {"output_dir": output_dir, "record_video": record_video})


@mcp.tool()
def computer_stop_recording() -> dict:
    """ACT: stop trajectory recording and finalize the mp4 if video was enabled. Example: computer_stop_recording()."""
    return _cua("stop_recording")


@mcp.tool()
def computer_get_recording_state() -> dict:
    """Return the current trajectory recorder state: whether recording is enabled, the output directory, and the counters. Example: computer_get_recording_state()."""
    return _cua("get_recording_state")


@mcp.tool()
def computer_replay_trajectory(dir: str, delay_ms: int = 500, stop_on_error: bool = True) -> dict:
    """ACT: replay a recorded trajectory by re-invoking every turn's tool call in lexical order. Example: computer_replay_trajectory(dir="/tmp/run1")."""
    return _cua("replay_trajectory", {"dir": dir, "delay_ms": delay_ms, "stop_on_error": stop_on_error})


@mcp.tool()
def computer_install_ffmpeg(confirm: bool = False) -> dict:
    """ACT: install the ffmpeg binary used by start_recording's video capture (Windows/Linux only). Example: computer_install_ffmpeg(confirm=True)."""
    return _cua("install_ffmpeg", {"confirm": confirm})


@mcp.tool()
async def computer_start_session(session: str) -> dict:
    """ACT: Declare a session — a named, color-coded identity for THIS agent run. Initialises the SDK connection and notifies the daemon. Example: computer_start_session("research-run-1")."""
    global _sdk_session_id
    _sdk_session_id = session
    # Initialise SDK connection for this session
    try:
        await _ensure_sdk()
    except Exception:  # noqa: BLE001
        pass  # SDK unavailable; daemon still works
    # Notify the daemon
    return _cua("start_session", {"session": session})


@mcp.tool()
async def computer_end_session(session: str) -> dict:
    """ACT: End a session declared with start_session. Disconnects SDK, removes agent cursor, stops recording, clears per-session config. Example: computer_end_session("research-run-1")."""
    global _sdk_session_id
    _sdk_session_id = None
    # Tear down SDK connection
    await _disconnect_sdk()
    # Notify the daemon
    return _cua("end_session", {"session": session})


@mcp.tool()
def computer_set_agent_cursor_enabled(enabled: bool, cursor_id: str = "") -> dict:
    """ACT: Toggle the visual agent-cursor overlay. True to show, false to hide. Example: computer_set_agent_cursor_enabled(True)."""
    args: dict = {"enabled": enabled}
    if cursor_id:
        args["cursor_id"] = cursor_id
    return _cua("set_agent_cursor_enabled", args)


@mcp.tool()
def computer_set_agent_cursor_style(bloom_color: str = "", cursor_id: str = "",
                                    gradient_colors: list[str] | None = None,
                                    image_path: str = "") -> dict:
    """ACT: Update the visual style of the agent cursor overlay. Example: computer_set_agent_cursor_style(bloom_color="#00FFFF")."""
    args: dict = {}
    if bloom_color:
        args["bloom_color"] = bloom_color
    if cursor_id:
        args["cursor_id"] = cursor_id
    if gradient_colors is not None:
        args["gradient_colors"] = gradient_colors
    if image_path:
        args["image_path"] = image_path
    return _cua("set_agent_cursor_style", args)


@mcp.tool()
def computer_set_agent_cursor_motion(cursor_id: str = "", cursor_icon: str = "",
                                     cursor_color: str = "", cursor_label: str = "",
                                     cursor_size: float = -1.0, cursor_opacity: float = -1.0,
                                     arc_size: float = -1.0, arc_flow: float = -999.0,
                                     start_handle: float = -1.0, end_handle: float = -1.0,
                                     spring: float = -1.0, glide_duration_ms: float = -1.0,
                                     dwell_after_click_ms: float = -1.0, idle_hide_ms: float = -1.0,
                                     turn_radius: float = -1.0) -> dict:
    """ACT: Configure the visual appearance and motion curve of an agent cursor instance. Example: computer_set_agent_cursor_motion(cursor_label="Agent")."""
    args: dict = {}
    if cursor_id:
        args["cursor_id"] = cursor_id
    if cursor_icon:
        args["cursor_icon"] = cursor_icon
    if cursor_color:
        args["cursor_color"] = cursor_color
    if cursor_label:
        args["cursor_label"] = cursor_label

    for k, v in [
        ("cursor_size", cursor_size),
        ("cursor_opacity", cursor_opacity),
        ("arc_size", arc_size),
        ("start_handle", start_handle),
        ("end_handle", end_handle),
        ("spring", spring),
        ("glide_duration_ms", glide_duration_ms),
        ("dwell_after_click_ms", dwell_after_click_ms),
        ("idle_hide_ms", idle_hide_ms),
        ("turn_radius", turn_radius),
    ]:
        if v >= 0:
            args[k] = v

    if arc_flow != -999.0:
        args["arc_flow"] = arc_flow

    return _cua("set_agent_cursor_motion", args)


@mcp.tool()
def computer_get_agent_cursor_state() -> dict:
    """Return the current agent-cursor configuration state. Example: computer_get_agent_cursor_state()."""
    return _cua("get_agent_cursor_state")


@mcp.tool()
def computer_get_config() -> dict:
    """Return the current persistent driver configuration. Example: computer_get_config()."""
    return _cua("get_config")


@mcp.tool()
def computer_set_config(key: str = "", value: str | int | bool | None = None,
                        capture_mode: str = "", max_image_dimension: int = -1,
                        experimental_pip: bool | None = None,
                        experimental_pip_geometry: str = "") -> dict:
    """ACT: Write a setting into the persistent driver config. Example: computer_set_config(key="capture_mode", value="som")."""
    args: dict = {}
    if key:
        args["key"] = key
        if value is not None:
            args["value"] = value
    if capture_mode:
        args["capture_mode"] = capture_mode
    if max_image_dimension >= 0:
        args["max_image_dimension"] = max_image_dimension
    if experimental_pip is not None:
        args["experimental_pip"] = experimental_pip
    if experimental_pip_geometry:
        args["experimental_pip_geometry"] = experimental_pip_geometry
    return _cua("set_config", args)


@mcp.tool()
def computer_check_permissions() -> dict:
    """Diagnostic: Check required permissions for cua-driver-rs on Windows. Example: computer_check_permissions()."""
    return _cua("check_permissions")


@mcp.tool()
def computer_check_for_update() -> dict:
    """Diagnostic: Check whether a newer cua-driver-rs release is available on GitHub. Example: computer_check_for_update()."""
    return _cua("check_for_update")


if __name__ == "__main__":
    mcp.run(transport="stdio")

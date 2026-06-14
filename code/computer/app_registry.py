"""Generic, data-driven app launch registry for the Computer-use skill.

The launch surface used to hard-code ``Start-Process '<name>'``, which only
worked when an app's friendly name *was* an executable on PATH (`calc`,
`notepad`). Real apps live behind long absolute paths (Office, VS Code) or
store-launcher stubs, so this module makes launching config-driven: `apps.yaml`
maps a friendly name → concrete launch target, and `launch_process` resolves
through it. Unknown names fall through unchanged, so anything that worked before
keeps working — adding an app is a yaml edit, not a code change.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

APPS_CONFIG_PATH = Path(__file__).with_name("apps.yaml")


@dataclass(frozen=True)
class AppEntry:
    """One resolved app: what to launch (`target` + optional `args`) and the
    `window_title` substring used to front+maximize it afterward."""
    name: str
    target: str
    window_title: Optional[str] = None
    args: str = ""


_REGISTRY: dict[str, AppEntry] | None = None


def _normalize(name: str) -> str:
    """Case- and whitespace-insensitive key: 'MS  Excel' → 'ms excel'."""
    return " ".join(str(name).strip().lower().split())


def load_registry(*, force: bool = False) -> dict[str, AppEntry]:
    """Parse apps.yaml into a ``{normalized_name: AppEntry}`` lookup, with every
    alias pointing at the same entry. Cached after first read; pass ``force=True``
    to reload (tests / hot config edits). A missing or empty file yields an empty
    registry rather than raising — callers then launch names verbatim."""
    global _REGISTRY
    if _REGISTRY is not None and not force:
        return _REGISTRY

    registry: dict[str, AppEntry] = {}
    try:
        raw = yaml.safe_load(APPS_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        raw = {}

    for key, spec in (raw.get("apps") or {}).items():
        spec = spec or {}
        entry = AppEntry(
            name=str(key),
            target=str(spec.get("target") or key),
            window_title=str(spec.get("window_title") or key),
            args=str(spec.get("args") or ""),
        )
        for alias in [key, *(spec.get("aliases") or [])]:
            registry[_normalize(alias)] = entry

    _REGISTRY = registry
    return registry


def resolve_app(name: str) -> Optional[AppEntry]:
    """Resolve a friendly app name (key or alias, case/space-insensitive) to its
    AppEntry, or None when the registry has no match — the signal for callers to
    fall back to launching the raw name."""
    if not name:
        return None
    return load_registry().get(_normalize(name))


def list_apps() -> list[AppEntry]:
    """The distinct configured apps (deduplicated across aliases), for discovery
    and the `list_apps` tool."""
    seen: dict[str, AppEntry] = {}
    for entry in load_registry().values():
        seen.setdefault(entry.name, entry)
    return list(seen.values())

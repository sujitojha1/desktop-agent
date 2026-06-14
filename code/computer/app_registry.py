"""Data-driven app launch registry for the Computer-use skill.

`apps.yaml` maps a friendly name → how the cua-driver `launch_app` tool should
start it. Launching is config, not code: to teach the agent a new app, add a yaml
entry; unknown names fall through and are launched by `name` (the driver tries the
Start-Menu AppsFolder index, then a PATH search), so anything already on PATH
keeps working without an entry.

Each entry names exactly one launch field, matching `launch_app`'s routing
(see docs/cua_driver_tools.md §2):

  name   a display name / PATH command (e.g. `code`, `notepad`) — most portable.
  path   a full path to an .exe.
  aumid  an App User Model ID for a packaged/Store app (Calculator, Notepad on
         Win11) — returns the real packaged pid, unlike the System32 stub .exe.

`target` is still accepted for back-compat: a value that looks like a path
(contains a slash or ends in .exe) becomes `path`, otherwise `name`.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

APPS_CONFIG_PATH = Path(__file__).with_name("apps.yaml")

# The launch fields cua-driver's launch_app accepts, in resolution precedence.
_LAUNCH_FIELDS = ("aumid", "path", "name")


@dataclass(frozen=True)
class AppEntry:
    """One resolved app: the single `launch_app` field/value that starts it and
    the `window_title` substring used to find its window after a stub launch."""
    name: str
    launch_field: str           # one of _LAUNCH_FIELDS
    launch_value: str
    window_title: Optional[str] = None

    def launch_args(self) -> dict:
        """The args dict to hand cua-driver's `launch_app`."""
        return {self.launch_field: self.launch_value}


_REGISTRY: dict[str, AppEntry] | None = None


def _normalize(name: str) -> str:
    """Case- and whitespace-insensitive key: 'MS  Excel' → 'ms excel'."""
    return " ".join(str(name).strip().lower().split())


def _resolve_launch(key: str, spec: dict) -> tuple[str, str]:
    """Pick the launch field + value for one yaml entry. An explicit
    name/path/aumid wins; else `target` is classified; else the key is the name."""
    for field in _LAUNCH_FIELDS:
        if spec.get(field):
            return field, str(spec[field])
    target = str(spec.get("target") or key)
    looks_like_path = ("\\" in target or "/" in target
                       or target.lower().endswith(".exe"))
    return ("path" if looks_like_path else "name"), target


def load_registry(*, force: bool = False) -> dict[str, AppEntry]:
    """Parse apps.yaml into ``{normalized_name: AppEntry}``, every alias pointing
    at the same entry. Cached after first read; pass ``force=True`` to reload. A
    missing/empty file yields an empty registry (callers then launch names
    verbatim) rather than raising."""
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
        launch_field, launch_value = _resolve_launch(key, spec)
        entry = AppEntry(
            name=str(key),
            launch_field=launch_field,
            launch_value=launch_value,
            window_title=str(spec.get("window_title") or key),
        )
        for alias in [key, *(spec.get("aliases") or [])]:
            registry[_normalize(alias)] = entry

    _REGISTRY = registry
    return registry


def resolve_app(name: str) -> Optional[AppEntry]:
    """Resolve a friendly app name (key or alias, case/space-insensitive) to its
    AppEntry, or None when the registry has no match — the signal for callers to
    launch the raw name."""
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

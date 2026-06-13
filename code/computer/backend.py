from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from typing import Any

import cua
import cua_auto

CUA_DRIVER_BIN = os.environ.get("CUA_DRIVER_BIN", "cua-driver")


class CuaBackend:
    _instance: CuaBackend | None = None

    @classmethod
    def shared(cls) -> CuaBackend:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self) -> None:
        self._host: cua.Localhost | None = None
        self._session_id: str | None = None

    @property
    def host(self) -> cua.Localhost | None:
        return self._host

    @property
    def session_id(self) -> str | None:
        return self._session_id

    async def ensure_sdk(self) -> cua.Localhost:
        if self._host is None:
            self._host = await cua.Localhost.connect()
        return self._host

    async def disconnect(self) -> None:
        if self._host is not None:
            try:
                await self._host.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._host = None

    def _cua(self, tool: str, args: dict | None = None) -> dict:
        exe = shutil.which(CUA_DRIVER_BIN) or CUA_DRIVER_BIN
        try:
            proc = subprocess.run(
                [exe, "call", tool, json.dumps(args or {})],
                capture_output=True, text=True, timeout=60,
            )
        except Exception as e:  # noqa: BLE001
            return {"error": f"cua-driver {tool} failed to spawn: {type(e).__name__}: {e}"}
        if proc.returncode != 0:
            return {"error": (proc.stderr or proc.stdout or "non-zero exit").strip()}
        out = (proc.stdout or "").strip()
        if not out:
            return {"ok": True}
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return {"raw": out}

    async def start_session(self, session: str) -> dict:
        self._session_id = session
        try:
            await self.ensure_sdk()
        except Exception:  # noqa: BLE001
            pass
        return self._cua("start_session", {"session": session})

    async def end_session(self, session: str) -> dict:
        self._session_id = None
        await self.disconnect()
        return self._cua("end_session", {"session": session})

    def daemon(self, tool: str, args: dict | None = None) -> dict:
        return self._cua(tool, args)

    # ── 14 overlapping tools ──────────────────────────────────────────────────
    async def click(self, pid: int, window_id: int = 0, element_index: int = -1,
                    x: float = -1.0, y: float = -1.0, button: str = "left", count: int = 1) -> dict:
        if element_index < 0 and x >= 0 and y >= 0:
            try:
                host = await self.ensure_sdk()
                for _ in range(count):
                    await host.mouse.click(int(x), int(y), button)
                return {"ok": True, "path": "sdk", "session": self._session_id}
            except Exception:  # noqa: BLE001
                pass
        args: dict = {"pid": pid, "button": button, "count": count}
        if window_id:
            args["window_id"] = window_id
        if element_index >= 0:
            args["element_index"] = element_index
        if x >= 0 and y >= 0:
            args["x"], args["y"] = x, y
        return self._cua("click", args)

    async def type_text(self, pid: int, text: str, window_id: int = 0, element_index: int = -1) -> dict:
        if element_index < 0:
            try:
                host = await self.ensure_sdk()
                await host.keyboard.type(text)
                return {"ok": True, "path": "sdk", "session": self._session_id}
            except Exception:  # noqa: BLE001
                pass
        args: dict = {"pid": pid, "text": text}
        if window_id:
            args["window_id"] = window_id
        if element_index >= 0:
            args["element_index"] = element_index
        return self._cua("type_text", args)

    async def press_key(self, pid: int, key: str, window_id: int = 0) -> dict:
        try:
            host = await self.ensure_sdk()
            await host.keyboard.keypress(key)
            return {"ok": True, "path": "sdk", "session": self._session_id}
        except Exception:  # noqa: BLE001
            pass
        args: dict = {"pid": pid, "key": key}
        if window_id:
            args["window_id"] = window_id
        return self._cua("press_key", args)

    async def hotkey(self, pid: int, keys: list[str], window_id: int = 0) -> dict:
        try:
            host = await self.ensure_sdk()
            await host.keyboard.keypress([str(k) for k in keys])
            return {"ok": True, "path": "sdk", "session": self._session_id}
        except Exception:  # noqa: BLE001
            pass
        args: dict = {"pid": pid, "keys": keys}
        if window_id:
            args["window_id"] = window_id
        return self._cua("hotkey", args)

    async def scroll(self, pid: int, direction: str, window_id: int = 0, amount: int = 3) -> dict:
        try:
            host = await self.ensure_sdk()
            dx = amount if direction == "right" else (-amount if direction == "left" else 0)
            dy = amount if direction == "down" else (-amount if direction == "up" else 0)
            w, h = await host.screen.size()
            await host.mouse.scroll(w // 2, h // 2, dx, dy)
            return {"ok": True, "path": "sdk", "session": self._session_id}
        except Exception:  # noqa: BLE001
            pass
        args: dict = {"pid": pid, "direction": direction, "amount": amount}
        if window_id:
            args["window_id"] = window_id
        return self._cua("scroll", args)

    async def double_click(self, pid: int, window_id: int = 0, element_index: int = -1,
                           x: float = -1.0, y: float = -1.0,
                           modifier: list[str] | None = None, dispatch: str = "background",
                           from_zoom: bool = False) -> dict:
        if element_index < 0 and x >= 0 and y >= 0:
            try:
                host = await self.ensure_sdk()
                await host.mouse.double_click(int(x), int(y))
                return {"ok": True, "path": "sdk", "session": self._session_id}
            except Exception:  # noqa: BLE001
                pass
        args: dict = {"pid": pid, "dispatch": dispatch, "from_zoom": from_zoom}
        if window_id:
            args["window_id"] = window_id
        if element_index >= 0:
            args["element_index"] = element_index
        if x >= 0 and y >= 0:
            args["x"], args["y"] = x, y
        if modifier:
            args["modifier"] = modifier
        return self._cua("double_click", args)

    async def right_click(self, pid: int, window_id: int = 0, element_index: int = -1,
                          x: float = -1.0, y: float = -1.0,
                          modifier: list[str] | None = None, dispatch: str = "background",
                          from_zoom: bool = False) -> dict:
        if element_index < 0 and x >= 0 and y >= 0:
            try:
                host = await self.ensure_sdk()
                await host.mouse.right_click(int(x), int(y))
                return {"ok": True, "path": "sdk", "session": self._session_id}
            except Exception:  # noqa: BLE001
                pass
        args: dict = {"pid": pid, "dispatch": dispatch, "from_zoom": from_zoom}
        if window_id:
            args["window_id"] = window_id
        if element_index >= 0:
            args["element_index"] = element_index
        if x >= 0 and y >= 0:
            args["x"], args["y"] = x, y
        if modifier:
            args["modifier"] = modifier
        return self._cua("right_click", args)

    async def drag(self, pid: int, from_x: float, from_y: float, to_x: float, to_y: float,
                   button: str = "left", window_id: int = 0, steps: int = 20,
                   duration_ms: int = 500, dispatch: str = "background",
                   from_zoom: bool = False, modifier: list[str] | None = None) -> dict:
        try:
            host = await self.ensure_sdk()
            await host.mouse.drag(int(from_x), int(from_y), int(to_x), int(to_y), button)
            return {"ok": True, "path": "sdk", "session": self._session_id}
        except Exception:  # noqa: BLE001
            pass
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
        return self._cua("drag", args)

    async def launch_app(self, name: str) -> dict:
        try:
            host = await self.ensure_sdk()
            await host.shell.run(f'start "" "{name}"', timeout=15)
            await asyncio.sleep(1.5)
            return self._cua("list_windows")
        except Exception:  # noqa: BLE001
            pass
        return self._cua("launch_app", {"name": name})

    async def kill_app(self, pid: int) -> dict:
        try:
            host = await self.ensure_sdk()
            cmd = (f'powershell -NoProfile -Command "'
                   f'$p = Get-Process -Id {pid} -ErrorAction SilentlyContinue; '
                   f'if ($p) {{ $p.CloseMainWindow() | Out-Null; Start-Sleep -Seconds 1; '
                   f'if (!$p.HasExited) {{ Stop-Process -Id {pid} -Force }} }}"')
            await host.shell.run(cmd, timeout=15)
            return {"ok": True, "path": "sdk", "session": self._session_id}
        except Exception:  # noqa: BLE001
            pass
        return self._cua("kill_app", {"pid": pid})

    async def bring_to_front(self, pid: int, window_id: int = 0) -> dict:
        try:
            def _do():
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
            activated = await asyncio.to_thread(_do)
            if activated:
                return {"ok": True, "path": "sdk", "session": self._session_id}
        except Exception:  # noqa: BLE001
            pass
        args: dict = {"pid": pid}
        if window_id:
            args["window_id"] = window_id
        return self._cua("bring_to_front", args)

    async def list_windows(self, pid: int = 0) -> dict:
        try:
            host = await self.ensure_sdk()
            from .tools import enumerate_windows
            wins = await enumerate_windows(host)
            return {"windows": wins, "path": "sdk", "session": self._session_id}
        except Exception:  # noqa: BLE001
            pass
        return self._cua("list_windows", {"pid": pid} if pid else {})

    async def get_screen_size(self) -> dict:
        try:
            host = await self.ensure_sdk()
            w, h = await host.screen.size()
            return {"width": w, "height": h, "path": "sdk", "session": self._session_id}
        except Exception:  # noqa: BLE001
            pass
        return self._cua("get_screen_size")

    async def get_cursor_position(self) -> dict:
        try:
            def _do():
                return cua_auto.screen.cursor_position()
            x, y = await asyncio.to_thread(_do)
            return {"x": x, "y": y, "path": "sdk", "session": self._session_id}
        except Exception:  # noqa: BLE001
            pass
        return self._cua("get_cursor_position")

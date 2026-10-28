"""Held-key registry: long presses are always released, even if the agent forgets."""
from __future__ import annotations

import atexit
import signal
import threading
import time

import winenv

_LOCK = threading.RLock()
_HELD: dict[str, dict] = {}
_AUTO_RELEASED: list[dict] = []
_WATCHDOG: threading.Thread | None = None
_STOP = threading.Event()


def _watchdog() -> None:
    while not _STOP.wait(0.2):
        cleanup()


def _ensure_watchdog() -> None:
    global _WATCHDOG
    with _LOCK:
        if _WATCHDOG is None or not _WATCHDOG.is_alive():
            _STOP.clear()
            _WATCHDOG = threading.Thread(target=_watchdog, name="cu-key-watchdog", daemon=True)
            _WATCHDOG.start()


def press(key: str, timeout: float = 10.0) -> dict:
    _ensure_watchdog()
    name = key.strip().lower()
    with _LOCK:
        if name in _HELD:
            _release_locked(name, reason="repress")
        winenv.key_down(name)
        _HELD[name] = {"pressed_at": time.time(), "timeout": float(timeout)}
    return held_state()


def _release_locked(name: str, reason: str = "manual") -> bool:
    entry = _HELD.pop(name, None)
    if entry is None:
        return False
    try:
        winenv.key_up(name)
    except Exception:
        pass
    if reason == "timeout":
        _AUTO_RELEASED.append({"key": name, "held_seconds": round(time.time() - entry["pressed_at"], 1)})
    return True


def release(key: str | None = None) -> dict:
    with _LOCK:
        if key:
            released = _release_locked(key.strip().lower())
            return {"released": [key] if released else [], "held_keys": sorted(_HELD)}
        names = list(_HELD)
        for name in names:
            _release_locked(name)
        return {"released": names, "held_keys": []}


def release_all() -> None:
    with _LOCK:
        for name in list(_HELD):
            _release_locked(name)


def cleanup() -> None:
    now = time.time()
    with _LOCK:
        expired = [name for name, entry in _HELD.items() if now - entry["pressed_at"] >= entry["timeout"]]
        for name in expired:
            _release_locked(name, reason="timeout")


def held_state() -> dict:
    now = time.time()
    with _LOCK:
        return {
            "held_keys": sorted(_HELD),
            "held_seconds": {name: round(now - entry["pressed_at"], 1) for name, entry in _HELD.items()},
        }


def take_auto_released() -> list[dict]:
    with _LOCK:
        events = list(_AUTO_RELEASED)
        _AUTO_RELEASED.clear()
        return events


def _shutdown(*_args) -> None:
    _STOP.set()
    release_all()


atexit.register(_shutdown)
for _sig in (signal.SIGTERM, signal.SIGINT):
    try:
        signal.signal(_sig, lambda *a: _shutdown())
    except Exception:
        pass

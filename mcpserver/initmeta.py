"""Project-scoped environment metadata written to ``<cwd>/.computer-use/env.json``."""
from __future__ import annotations

import json
import os
import platform
import sys
import time

import cache
import capture
import sysres
import winenv


def collect() -> dict:
    foreground = winenv.foreground_hwnd()
    monitors = winenv.list_monitors()
    env = {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "timestamp": time.time(),
        "cwd": os.getcwd(),
        "python": sys.executable,
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
        "dpi_awareness": winenv.DPI_AWARENESS,
        "virtual_screen": winenv.virtual_screen(),
        "monitors": monitors,
        "cache_root": str(cache.root()),
        "active_window": {
            "hwnd": foreground,
            "title": winenv.window_title(foreground),
            "class": winenv.class_name(foreground),
            "rect": winenv.window_rect(foreground),
        },
        "windows": sysres.windows_list()[:60],
    }
    try:
        img, meta = capture.grab_monitor(0)
        env["capture_check"] = {"size": list(img.size), "offset": meta["offset"], "ok": True}
    except Exception as exc:
        env["capture_check"] = {"ok": False, "error": str(exc)}
    return env


def init(force: bool = False) -> dict:
    path = cache.root() / "env.json"
    if path.exists() and not force:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return {"ok": True, "env_path": str(path), "cached": True, "env": data}
        except Exception:
            pass
    env = collect()
    path.write_text(json.dumps(env, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "ok": True,
        "env_path": str(path),
        "cached": False,
        "cwd": env["cwd"],
        "python": env["python"],
        "dpi_awareness": env["dpi_awareness"],
        "virtual_screen": env["virtual_screen"],
        "monitors": env["monitors"],
        "active_window": env["active_window"],
        "windows": env["windows"],
        "created_at": env["created_at"],
    }

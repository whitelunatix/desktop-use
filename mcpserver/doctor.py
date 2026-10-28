"""Environment self-check and pointer calibration."""
from __future__ import annotations

import os
import time

import cache
import capture
import recognize
import winenv


def _writable(path) -> bool:
    try:
        probe = path / ".doctor-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:
        return False


def report(calibrate: bool = False, check_ocr: bool = False) -> dict:
    root = cache.root()
    monitors = winenv.list_monitors()
    foreground = winenv.foreground_hwnd()
    result = {
        "ok": True,
        "dpi_awareness": winenv.DPI_AWARENESS,
        "virtual_screen": winenv.virtual_screen(),
        "monitors": monitors,
        "cursor": list(winenv.cursor_pos()),
        "foreground": {
            "hwnd": foreground,
            "title": winenv.window_title(foreground),
            "class": winenv.class_name(foreground),
            "rect": winenv.window_rect(foreground),
        },
        "cache_root": str(root),
        "cache_writable": _writable(root),
        "python": {"executable": os.sys.executable},
    }

    try:
        img, meta = capture.grab_monitor(0)
        result["capture"] = {
            "size": list(img.size),
            "offset": meta["offset"],
            "matches_monitor": list(img.size) == monitors[0]["size"] if monitors else False,
            "stats": capture.image_stats(img),
        }
    except Exception as exc:
        result["capture"] = {"error": str(exc)}

    uia_started = time.time()
    elements, truncated = recognize.uia_elements(foreground, timeout=2.0)
    result["uia"] = {"elements": len(elements), "truncated": truncated,
                     "elapsed": round(time.time() - uia_started, 2)}

    if check_ocr:
        started = time.time()
        ocr = recognize.ocr_elements(img, meta.get("offset"))
        result["ocr"] = {"elements": len(ocr), "elapsed": round(time.time() - started, 2)}

    if calibrate:
        original = winenv.cursor_pos()
        deltas = []
        for point in ((120, 120), (monitors[0]["size"][0] - 60, monitors[0]["size"][1] - 60)):
            best = None
            for _attempt in range(3):
                winenv.move_to(*point)
                time.sleep(0.05)
                actual = winenv.cursor_pos()
                delta = [actual[0] - point[0], actual[1] - point[1]]
                if best is None or sum(abs(v) for v in delta) < sum(abs(v) for v in best):
                    best = delta
                if best == [0, 0]:
                    break
            deltas.append(best)
        winenv.move_to(*original)
        result["calibration"] = {
            "deltas": deltas,
            "max_abs_delta": max(abs(v) for pair in deltas for v in pair),
            "ok": all(abs(v) <= 2 for pair in deltas for v in pair),
        }
    return result

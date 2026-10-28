"""Screen and window capture. Every pixel here is a physical screen pixel."""
from __future__ import annotations

import ctypes
from ctypes import wintypes

import mss
import numpy as np
from PIL import Image

import winenv

user32 = winenv.user32
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_ulong),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", ctypes.c_ushort),
        ("biBitCount", ctypes.c_ushort),
        ("biCompression", ctypes.c_ulong),
        ("biSizeImage", ctypes.c_ulong),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", ctypes.c_ulong),
        ("biClrImportant", ctypes.c_ulong),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", ctypes.c_ulong * 3)]


def grab_rect(rect: list[int]) -> tuple[Image.Image, dict]:
    """Capture an absolute desktop rectangle (left, top, right, bottom)."""
    left, top, right, bottom = (int(v) for v in rect)
    width, height = max(1, right - left), max(1, bottom - top)
    with mss.mss() as sct:
        shot = sct.grab({"left": left, "top": top, "width": width, "height": height})
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    return img, {"offset": [left, top], "size": [width, height]}


def grab_monitor(index: int | str = "auto") -> tuple[Image.Image, dict]:
    monitors = winenv.list_monitors()
    if not monitors:
        raise RuntimeError("no monitor found")
    if index == "auto":
        hwnd = winenv.foreground_hwnd()
        rect = winenv.window_rect(hwnd)
        cx, cy = (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2
        chosen = None
        for m in monitors:
            l, t, r, b = m["bounds"]
            if l <= cx < r and t <= cy < b:
                chosen = m
                break
        monitor = chosen or next((m for m in monitors if m["primary"]), monitors[0])
    else:
        monitor = monitors[int(index)]
    img, meta = grab_rect(monitor["bounds"])
    meta.update({"monitor": monitor["index"], "dpi_scale": monitor["dpi_scale"], "monitor_bounds": monitor["bounds"]})
    return img, meta


def grab_virtual() -> tuple[Image.Image, dict]:
    vs = winenv.virtual_screen()
    rect = [vs["left"], vs["top"], vs["left"] + vs["width"], vs["top"] + vs["height"]]
    img, meta = grab_rect(rect)
    meta.update({"monitor": "all", "dpi_scale": 1.0, "monitor_bounds": rect})
    return img, meta


def grab_window(hwnd: int) -> tuple[Image.Image, dict]:
    """PrintWindow capture: works for occluded windows and GPU-composited apps."""
    if winenv.is_minimized(hwnd):
        raise RuntimeError("window_minimized")
    rect = winenv.window_rect(hwnd)
    width, height = rect[2] - rect[0], rect[3] - rect[1]
    if width <= 0 or height <= 0:
        raise RuntimeError("window_has_no_area")

    hdc_window = user32.GetWindowDC(hwnd)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_window)
    bitmap = gdi32.CreateCompatibleBitmap(hdc_window, width, height)
    old = gdi32.SelectObject(hdc_mem, bitmap)
    try:
        # 2 = PW_RENDERFULLCONTENT, required for DirectComposition / GPU windows
        ok = user32.PrintWindow(hwnd, hdc_mem, 2)
        if not ok:
            raise RuntimeError("printwindow_failed")
        info = BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = -height  # top-down
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = 0  # BI_RGB
        buffer = ctypes.create_string_buffer(width * height * 4)
        gdi32.GetDIBits(hdc_mem, bitmap, 0, height, buffer, ctypes.byref(info), 0)
        img = Image.frombuffer("RGB", (width, height), buffer, "raw", "BGRX", 0, 1)
    finally:
        gdi32.SelectObject(hdc_mem, old)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(hwnd, hdc_window)

    return img.copy(), {"offset": [rect[0], rect[1]], "size": [width, height], "monitor": "window"}


def save_png(img: Image.Image, path: str) -> str:
    img.save(path, "PNG")
    return path


def image_stats(img: Image.Image) -> dict:
    arr = np.asarray(img.convert("L"), dtype=np.uint8)
    std = float(arr.std())
    unique = int(np.unique(arr[::7, ::7]).size)
    return {"std": round(std, 2), "unique": unique, "blank_suspect": bool(std < 2.0 and unique < 8)}


def fingerprint(img: Image.Image) -> np.ndarray:
    """Small grayscale thumbnail used to detect whether the screen changed."""
    small = img.convert("L").resize((32, 24), Image.BILINEAR)
    return np.asarray(small, dtype=np.float32)


def difference(a: np.ndarray, b: np.ndarray) -> float:
    if a.shape != b.shape:
        return 255.0
    return float(np.abs(a - b).mean())


def change_metrics(a: np.ndarray, b: np.ndarray) -> dict:
    """Mean diff plus the share of visibly changed cells.

    A small dropdown menu barely moves the global mean, so the fraction of
    changed thumbnail cells is what actually catches local UI changes.
    """
    if a.shape != b.shape:
        return {"mean": 255.0, "fraction": 1.0, "max": 255.0}
    delta = np.abs(a - b)
    return {
        "mean": round(float(delta.mean()), 2),
        "fraction": round(float((delta > 6).mean()), 4),
        "max": round(float(delta.max()), 1),
    }

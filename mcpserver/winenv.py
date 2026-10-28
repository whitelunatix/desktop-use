"""Windows environment layer: DPI awareness, monitors, pixels, input injection.

All coordinates in this module are physical pixels on the virtual desktop,
whose origin is the top-left of the bounding box of every monitor.
"""
from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
try:
    shcore = ctypes.WinDLL("shcore", use_last_error=True)
except OSError:  # pragma: no cover - Windows 8+
    shcore = None

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]

    def as_tuple(self) -> list[int]:
        return [self.left, self.top, self.right, self.bottom]


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("rcMonitor", RECT),
        ("rcWork", RECT),
        ("dwFlags", ctypes.c_ulong),
    ]


def _current_awareness() -> str:
    """Report the awareness actually in force (the host may preset it)."""
    try:
        ctx = user32.GetThreadDpiAwarenessContext()
        checks = [
            ("per-monitor-v2", -4),
            ("per-monitor", -3),
            ("system", -2),
            ("unaware", -1),
        ]
        for label, value in checks:
            if user32.AreDpiAwarenessContextsEqual(ctx, ctypes.c_void_p(value)):
                return label
    except Exception:
        pass
    return "unknown"


def _enable_dpi_awareness() -> str:
    """Per-monitor-v2 where available; must run before any capture/input."""
    if shcore is not None:
        try:
            # 2 = PROCESS_PER_MONITOR_DPI_AWARE; returns S_OK or a failure HRESULT.
            hr = shcore.SetProcessDpiAwareness(2)
            if hr == 0:
                return _current_awareness()
        except Exception:
            pass
    try:
        if user32.SetProcessDPIAware():
            return _current_awareness()
    except Exception:
        pass
    return _current_awareness()


DPI_AWARENESS = _enable_dpi_awareness()


def virtual_screen() -> dict:
    return {
        "left": user32.GetSystemMetrics(SM_XVIRTUALSCREEN),
        "top": user32.GetSystemMetrics(SM_YVIRTUALSCREEN),
        "width": user32.GetSystemMetrics(SM_CXVIRTUALSCREEN),
        "height": user32.GetSystemMetrics(SM_CYVIRTUALSCREEN),
    }


def _monitor_dpi(hmon) -> float:
    if shcore is None:
        return 1.0
    try:
        dx = ctypes.c_uint(96)
        dy = ctypes.c_uint(96)
        # 0 = MDT_EFFECTIVE_DPI
        if shcore.GetDpiForMonitor(hmon, 0, ctypes.byref(dx), ctypes.byref(dy)) == 0:
            return round(dx.value / 96.0, 4)
    except Exception:
        pass
    return 1.0


def list_monitors() -> list[dict]:
    monitors: list[dict] = []

    def _callback(hmon, hdc, rect_ptr, data):
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            r = info.rcMonitor
            w = info.rcWork
            monitors.append(
                {
                    "hwnd": int(hmon or 0),
                    "bounds": [r.left, r.top, r.right, r.bottom],
                    "work_area": [w.left, w.top, w.right, w.bottom],
                    "size": [r.right - r.left, r.bottom - r.top],
                    "primary": bool(info.dwFlags & 1),
                    "dpi_scale": _monitor_dpi(hmon),
                }
            )
        return 1

    proc = ctypes.WINFUNCTYPE(
        ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(RECT), ctypes.c_void_p
    )(_callback)
    user32.EnumDisplayMonitors(None, None, proc, 0)
    monitors.sort(key=lambda m: (m["bounds"][0], m["bounds"][1]))
    for index, m in enumerate(monitors):
        m["index"] = index
    return monitors


def monitor_for_point(x: int, y: int) -> dict | None:
    for m in list_monitors():
        l, t, r, b = m["bounds"]
        if l <= x < r and t <= y < b:
            return m
    return None


# ---------------------------------------------------------------------------
# Input injection
# ---------------------------------------------------------------------------

ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", ctypes.c_ulong),
        ("wParamL", ctypes.c_ushort),
        ("wParamH", ctypes.c_ushort),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_ulong), ("u", _INPUTUNION)]


INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x01000
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wintypes.UINT

VK = {
    "backspace": 0x08,
    "tab": 0x09,
    "enter": 0x0D,
    "return": 0x0D,
    "shift": 0x10,
    "ctrl": 0x11,
    "control": 0x11,
    "alt": 0x12,
    "pause": 0x13,
    "capslock": 0x14,
    "esc": 0x1B,
    "escape": 0x1B,
    "space": 0x20,
    "pageup": 0x21,
    "pagedown": 0x22,
    "end": 0x23,
    "home": 0x24,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "insert": 0x2D,
    "delete": 0x2E,
    "del": 0x2E,
    "win": 0x5B,
    "lwin": 0x5B,
    "rwin": 0x5C,
    "apps": 0x5D,
}
for _i in range(1, 13):
    VK[f"f{_i}"] = 0x6F + _i


def vk_for(name: str) -> int:
    key = name.strip().lower()
    if key in VK:
        return VK[key]
    if len(key) == 1:
        code = ord(key.upper())
        if 0x30 <= code <= 0x5A or code in (0xBA, 0xBB, 0xBC, 0xBD, 0xBE, 0xBF, 0xC0, 0xDB, 0xDC, 0xDD, 0xDE):
            return code
    raise ValueError(f"unknown key: {name!r}")


def _send(inputs: list[INPUT]) -> None:
    if not inputs:
        return
    array = (INPUT * len(inputs))(*inputs)
    sent = user32.SendInput(len(inputs), array, ctypes.sizeof(INPUT))
    if sent != len(inputs):
        err = ctypes.get_last_error()
        raise RuntimeError(f"SendInput failed ({sent}/{len(inputs)}), error={err}")


def _normalize(x: int, y: int) -> tuple[int, int]:
    vs = virtual_screen()
    nx = int(round((x - vs["left"]) * 65535 / max(vs["width"] - 1, 1)))
    ny = int(round((y - vs["top"]) * 65535 / max(vs["height"] - 1, 1)))
    return nx, ny


def _mouse_input(flags: int, x: int | None = None, y: int | None = None, data: int = 0) -> INPUT:
    inp = INPUT(type=INPUT_MOUSE)
    inp.mi = MOUSEINPUT(0, 0, data, flags, 0, 0)
    if x is not None and y is not None:
        nx, ny = _normalize(x, y)
        inp.mi = MOUSEINPUT(nx, ny, data, flags | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, 0, 0)
    return inp


def move_to(x: int, y: int) -> None:
    _send([_mouse_input(MOUSEEVENTF_MOVE, x, y)])


def cursor_pos() -> tuple[int, int]:
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def mouse_down(button: str = "left") -> None:
    flag = {"left": MOUSEEVENTF_LEFTDOWN, "right": MOUSEEVENTF_RIGHTDOWN, "middle": MOUSEEVENTF_MIDDLEDOWN}[button]
    _send([_mouse_input(flag)])


def mouse_up(button: str = "left") -> None:
    flag = {"left": MOUSEEVENTF_LEFTUP, "right": MOUSEEVENTF_RIGHTUP, "middle": MOUSEEVENTF_MIDDLEUP}[button]
    _send([_mouse_input(flag)])


def click_at(x: int, y: int, button: str = "left", count: int = 1) -> None:
    for _ in range(max(1, count)):
        move_to(x, y)
        time.sleep(0.01)
        mouse_down(button)
        time.sleep(0.02)
        mouse_up(button)
        if count > 1:
            time.sleep(0.06)


def scroll_at(x: int, y: int, dy: int, dx: int = 0) -> None:
    move_to(x, y)
    if dy:
        _send([_mouse_input(MOUSEEVENTF_WHEEL, data=int(dy))])
    if dx:
        _send([_mouse_input(MOUSEEVENTF_HWHEEL, data=int(dx))])


def key_down(name: str) -> None:
    vk = vk_for(name)
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.ki = KEYBDINPUT(vk, 0, 0, 0, 0)
    _send([inp])


def key_up(name: str) -> None:
    vk = vk_for(name)
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.ki = KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP, 0, 0)
    _send([inp])


def tap(name: str, times: int = 1) -> None:
    for _ in range(max(1, times)):
        key_down(name)
        key_up(name)
        time.sleep(0.01)


def combo(spec: str) -> None:
    """Send a key chord such as ``ctrl+shift+s`` or a plain key name."""
    parts = [p for p in spec.replace(" ", "").split("+") if p]
    if not parts:
        raise ValueError("empty key combo")
    modifiers, final = parts[:-1], parts[-1]
    pressed: list[str] = []
    try:
        for mod in modifiers:
            key_down(mod)
            pressed.append(mod)
        tap(final)
    finally:
        for mod in reversed(pressed):
            try:
                key_up(mod)
            except Exception:
                pass


def type_unicode(text: str) -> None:
    """Type arbitrary Unicode text without touching the clipboard."""
    units = text.encode("utf-16-le")
    inputs: list[INPUT] = []
    for i in range(0, len(units), 2):
        code = units[i] | (units[i + 1] << 8)
        down = INPUT(type=INPUT_KEYBOARD)
        down.ki = KEYBDINPUT(0, code, KEYEVENTF_UNICODE, 0, 0)
        up = INPUT(type=INPUT_KEYBOARD)
        up.ki = KEYBDINPUT(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, 0)
        inputs.extend((down, up))
    for start in range(0, len(inputs), 64):
        _send(inputs[start : start + 64])
        time.sleep(0.005)


def window_rect(hwnd: int) -> list[int]:
    rect = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return [0, 0, 0, 0]
    return rect.as_tuple()


def foreground_hwnd() -> int:
    return int(user32.GetForegroundWindow() or 0)


def window_from_point(x: int, y: int) -> int:
    return int(user32.WindowFromPoint(wintypes.POINT(x, y)) or 0)


def root_window(hwnd: int) -> int:
    GA_ROOT = 2
    return int(user32.GetAncestor(hwnd, GA_ROOT) or hwnd)


def is_minimized(hwnd: int) -> bool:
    return bool(user32.IsIconic(hwnd))


def window_title(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


# ---------------------------------------------------------------------------
# Window geometry, ownership and DPI
# ---------------------------------------------------------------------------

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010

GW_OWNER = 4
GA_ROOTOWNER = 3

SM_CXSIZEFRAME = 32
SM_CYSIZEFRAME = 33
SM_CXPADDEDBORDER = 92

WS_EX_TOOLWINDOW = 0x00000080
WS_POPUP = 0x80000000

SW_RESTORE = 9
SW_MINIMIZE = 6
SW_MAXIMIZE = 3


def _rect_intersection_area(a: list[int], b: list[int]) -> int:
    w = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    h = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    return w * h


def monitor_for_rect(rect: list[int]) -> dict | None:
    """Monitor with the largest overlap with ``rect`` (absolute pixels)."""
    monitors = list_monitors()
    if not monitors:
        return None
    best = max(monitors, key=lambda m: _rect_intersection_area(rect, m["bounds"]))
    if _rect_intersection_area(rect, best["bounds"]) > 0:
        return best
    cx, cy = (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2
    return (
        monitor_for_point(cx, cy)
        or next((m for m in monitors if m["primary"]), monitors[0])
    )


def set_window_pos(hwnd: int, x: int, y: int, w: int, h: int,
                   flags: int = SWP_NOZORDER | SWP_NOACTIVATE) -> bool:
    return bool(user32.SetWindowPos(int(hwnd), 0, int(x), int(y), int(w), int(h), int(flags)))


def is_zoomed(hwnd: int) -> bool:
    return bool(user32.IsZoomed(hwnd))


def window_owner(hwnd: int) -> int:
    return int(user32.GetWindow(hwnd, GW_OWNER) or 0)


def root_owner(hwnd: int) -> int:
    return int(user32.GetAncestor(hwnd, GA_ROOTOWNER) or 0)


def get_last_active_popup(hwnd: int) -> int:
    return int(user32.GetLastActivePopup(hwnd) or 0)


def window_process_id(hwnd: int) -> int:
    pid = ctypes.c_ulong(0)
    user32.GetWindowThreadProcessId(int(hwnd), ctypes.byref(pid))
    return int(pid.value)


def window_ex_style(hwnd: int) -> int:
    try:
        value = user32.GetWindowLongPtrW(int(hwnd), -20)  # GWL_EXSTYLE
        return int(value or 0)
    except Exception:
        return 0


def window_style(hwnd: int) -> int:
    try:
        value = user32.GetWindowLongPtrW(int(hwnd), -16)  # GWL_STYLE
        return int(value or 0) & 0xFFFFFFFF
    except Exception:
        return 0


def is_popup_window(hwnd: int) -> bool:
    """WS_POPUP style: the window is a menu/dropdown/transient panel, not a main frame."""
    return bool(window_style(hwnd) & WS_POPUP)


def is_window_enabled(hwnd: int) -> bool:
    return bool(user32.IsWindowEnabled(int(hwnd)))


def is_window_visible(hwnd: int) -> bool:
    return bool(user32.IsWindowVisible(int(hwnd)))


def dpi_for_window(hwnd: int) -> float:
    try:
        getter = user32.GetDpiForWindow
        value = int(getter(int(hwnd)) or 0)
        if value > 0:
            return round(value / 96.0, 4)
    except Exception:
        pass
    m = monitor_for_rect(window_rect(hwnd))
    return float(m["dpi_scale"]) if m else 1.0


def _system_metric_for_dpi(metric: int, dpi: float) -> int:
    try:
        getter = user32.GetSystemMetricsForDpi
        value = int(getter(metric, int(round(dpi * 96))))
        if value > 0:
            return value
    except Exception:
        pass
    return int(user32.GetSystemMetrics(metric))


def resize_border_metrics(hwnd: int = 0) -> dict:
    """Invisible resize frame thickness (physical pixels on this window's monitor)."""
    dpi = dpi_for_window(hwnd) if hwnd else 1.0
    frame_x = _system_metric_for_dpi(SM_CXSIZEFRAME, dpi)
    frame_y = _system_metric_for_dpi(SM_CYSIZEFRAME, dpi)
    padded = _system_metric_for_dpi(SM_CXPADDEDBORDER, dpi)
    return {
        "dpi": dpi,
        "left": frame_x + padded,
        "right": frame_x + padded,
        "top": frame_y + padded,
        "bottom": frame_y + padded,
    }


def enum_child_texts(hwnd: int) -> list[str]:
    """Concatenated window texts of all descendants (tooltips keep text on a child)."""
    texts: list[str] = []

    def _callback(child, _lparam):
        title = window_title(child)
        if title.strip():
            texts.append(title.strip())
        return True

    proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)(_callback)
    user32.EnumChildWindows(int(hwnd), proc, 0)
    return texts


class WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [
        ("length", ctypes.c_uint),
        ("flags", ctypes.c_uint),
        ("showCmd", ctypes.c_uint),
        ("ptMinPosition", wintypes.POINT),
        ("ptMaxPosition", wintypes.POINT),
        ("rcNormalPosition", RECT),
    ]


def window_placement(hwnd: int) -> dict | None:
    """Placement info including the restored (normal) rectangle of a zoomed window."""
    place = WINDOWPLACEMENT()
    place.length = ctypes.sizeof(WINDOWPLACEMENT)
    if not user32.GetWindowPlacement(int(hwnd), ctypes.byref(place)):
        return None
    return {
        "show_cmd": int(place.showCmd),
        "minimized": bool(place.showCmd == SW_MINIMIZE),
        "maximized": bool(place.showCmd == SW_MAXIMIZE),
        "normal_rect": place.rcNormalPosition.as_tuple(),
    }

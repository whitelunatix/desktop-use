"""Window, Explorer-folder and clipboard primitives."""
from __future__ import annotations

import ctypes
import os
import subprocess
import time
from urllib.parse import unquote

import winenv

WM_CLOSE = 0x0010
SW_RESTORE = 9
SW_MINIMIZE = 6
SW_MAXIMIZE = 3

_ENUM_PROC = None


def _window_list(include_untitled: bool = False) -> list[dict]:
    result: list[dict] = []

    def _callback(hwnd, _lparam):
        if not winenv.user32.IsWindowVisible(hwnd):
            return True
        title = winenv.window_title(hwnd)
        if not title and not include_untitled:
            return True
        rect = winenv.window_rect(hwnd)
        result.append(
            {
                "hwnd": int(hwnd),
                "title": title,
                "class": winenv.class_name(hwnd),
                "rect": rect,
                "minimized": winenv.is_minimized(hwnd),
            }
        )
        return True

    global _ENUM_PROC
    import ctypes

    _ENUM_PROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)(_callback)
    winenv.user32.EnumWindows(_ENUM_PROC, 0)
    return result


def windows_list(title: str | None = None) -> list[dict]:
    items = _window_list()
    if title:
        needle = title.lower()
        items = [w for w in items if needle in w["title"].lower()]
    foreground = winenv.foreground_hwnd()
    for item in items:
        item["foreground"] = item["hwnd"] == foreground
    return items


def windows_all(include_untitled: bool = True) -> list[dict]:
    """Raw enumeration including untitled helpers (tooltips keep their text on a child)."""
    items = _window_list(include_untitled=include_untitled)
    foreground = winenv.foreground_hwnd()
    for item in items:
        item["foreground"] = item["hwnd"] == foreground
    return items


def _match_window(title: str | None = None, hwnd: int | None = None) -> dict | None:
    items = _window_list()
    if hwnd:
        for item in items:
            if item["hwnd"] == int(hwnd):
                return item
        return None
    if title:
        needle = title.lower()
        for item in items:
            if item["title"].lower() == needle:
                return item
        hits = [item for item in items if needle in item["title"].lower()]
        for item in hits:
            if item.get("foreground"):
                return item
        if hits:
            return hits[0]
    return None


def window_focus(title: str | None = None, hwnd: int | None = None) -> dict:
    match = _match_window(title, hwnd)
    if match is None:
        return {"ok": False, "error": "window_not_found", "hint": "use cu_window list to see titles"}
    target = match["hwnd"]
    # Already in front and not minimized: re-asserting the foreground can break
    # in-place edit sessions, so leave the window alone.
    if not match["minimized"] and winenv.foreground_hwnd() == target:
        return {"ok": True, "window": match, "foreground": True, "already_foreground": True}
    try:
        if match["minimized"]:
            winenv.user32.ShowWindow(target, SW_RESTORE)
        winenv.user32.SetForegroundWindow(target)
        time.sleep(0.08)
        if winenv.foreground_hwnd() != target:
            # Foreground lock workaround: tap ALT, then retry once.
            winenv.tap("alt")
            winenv.user32.SetForegroundWindow(target)
            time.sleep(0.08)
    except Exception as exc:
        return {"ok": False, "error": "focus_failed", "hint": str(exc)}
    return {"ok": True, "window": match, "foreground": winenv.foreground_hwnd() == target}


def window_signature() -> dict[int, dict]:
    """Cheap snapshot of visible top-level windows: hwnd -> title/class/rect.

    Used to notice that an action opened something (menu, dropdown, dialog) even
    when the pixel diff around the click point stays flat.
    """
    noise = ("tooltips_class32", "#32774", "SysShadow", "MSCTFIME UI")
    items = _window_list(include_untitled=True)
    out: dict[int, dict] = {}
    for item in items:
        if item["minimized"]:
            continue
        if any(deny.lower() in str(item["class"]).lower() for deny in noise):
            continue
        rect = item["rect"]
        if rect[2] - rect[0] <= 0 or rect[3] - rect[1] <= 0:
            continue
        out[int(item["hwnd"])] = {"title": item["title"], "class": item["class"], "rect": rect}
    return out


def new_windows(before: dict[int, dict] | None, after: dict[int, dict] | None = None) -> list[dict]:
    """Windows present in ``after`` but not in ``before`` (empty when nothing is known)."""
    if not before:
        return []
    after = window_signature() if after is None else after
    fresh = []
    for hwnd, info in after.items():
        if hwnd in before:
            continue
        fresh.append({"hwnd": int(hwnd), **info})
    return fresh


def window_close(title: str | None = None, hwnd: int | None = None) -> dict:
    match = _match_window(title, hwnd)
    if match is None:
        return {"ok": False, "error": "window_not_found"}
    winenv.user32.PostMessageW(match["hwnd"], WM_CLOSE, 0, 0)
    time.sleep(0.15)
    still = _match_window(hwnd=match["hwnd"])
    return {"ok": True, "closed": match, "still_open": still is not None}


def window_move(title: str | None = None, hwnd: int | None = None, rect: list[int] | None = None,
                state: str | None = None) -> dict:
    match = _match_window(title, hwnd)
    if match is None:
        return {"ok": False, "error": "window_not_found"}
    target = match["hwnd"]
    if rect and len(rect) == 4:
        x, y, w, h = rect
        winenv.set_window_pos(target, x, y, w, h, winenv.SWP_NOZORDER | winenv.SWP_NOACTIVATE)
    if state:
        code = {"restore": SW_RESTORE, "minimize": SW_MINIMIZE, "maximize": SW_MAXIMIZE}.get(state)
        if code is None:
            return {"ok": False, "error": "bad_state", "hint": "restore|minimize|maximize"}
        winenv.user32.ShowWindow(target, code)
    return {"ok": True, "window": _match_window(hwnd=target) or match}


# Popups that must never be mistaken for a dialog.
_POPUP_DENY_CLASS = ("#32768", "tooltips_class32", "#32774", "SysShadow", "IME", "MSCTFIME UI")
_ANCHORS = {
    "top-left", "top", "top-right", "left", "center", "right",
    "bottom-left", "bottom", "bottom-right",
}


def _window_ok(hwnd: int, origin: int) -> bool:
    if not hwnd or hwnd == origin:
        return False
    if not winenv.is_window_visible(hwnd) or winenv.is_minimized(hwnd):
        return False
    if not winenv.is_window_enabled(hwnd):
        return False
    cls = winenv.class_name(hwnd)
    if any(deny.lower() in cls.lower() for deny in _POPUP_DENY_CLASS):
        return False
    tool_window = bool(winenv.window_ex_style(hwnd) & winenv.WS_EX_TOOLWINDOW)
    if tool_window and cls != "#32770" and not winenv.is_popup_window(hwnd):
        return False
    rect = winenv.window_rect(hwnd)
    return rect[2] - rect[0] > 0 and rect[3] - rect[1] > 0


def _popup_payload(candidate: int, reason: str, origin: int) -> dict:
    title = winenv.window_title(candidate)
    return {
        "hwnd": candidate,
        "title": title,
        "class": winenv.class_name(candidate),
        "rect": winenv.window_rect(candidate),
        "owner_hwnd": winenv.window_owner(candidate) or origin,
        "target_hwnd": origin,
        "reason": reason,
        "untitled": not title,
    }


def _owned_popup_candidate(candidate: int, origin: int) -> bool:
    """True when ``candidate`` is a visible popup owned by ``origin``.

    A candidate needs a caption *or* the WS_POPUP style: menus and modern tool
    panels are usually untitled.
    """
    if not winenv.is_window_visible(candidate) or winenv.is_minimized(candidate):
        return False
    if winenv.window_owner(candidate) != origin:
        return False
    if not winenv.window_title(candidate) and not winenv.is_popup_window(candidate):
        return False
    return _window_ok(candidate, origin)


def _owned_popup_windows(origin: int) -> list[int]:
    """Visible top-level windows owned by ``origin`` (z-order)."""
    found: list[int] = []

    def _callback(candidate, _lparam):
        if _owned_popup_candidate(int(candidate), origin):
            found.append(int(candidate))
        return True

    proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)(_callback)
    winenv.user32.EnumWindows(proc, 0)
    return found


def popup_for(hwnd: int) -> dict | None:
    """Visible popup/dialog that belongs to ``hwnd`` (strict owner checks)."""
    origin_pid = winenv.window_process_id(hwnd)
    candidates: list[tuple[int, str]] = []
    last = winenv.get_last_active_popup(hwnd)
    if last and last != hwnd:
        candidates.append((last, "last_active_popup"))
    foreground = winenv.foreground_hwnd()
    if foreground and foreground != hwnd:
        owner = winenv.window_owner(foreground)
        if owner == hwnd or winenv.root_owner(foreground) == hwnd:
            candidates.append((foreground, "owner"))
        elif (winenv.class_name(foreground) == "#32770"
              and winenv.window_process_id(foreground) == origin_pid):
            candidates.append((foreground, "dialog_same_pid"))
    for candidate, reason in candidates:
        if _window_ok(candidate, hwnd):
            return _popup_payload(candidate, reason, hwnd)
    # A dialog that never took focus is invisible to the checks above; only now
    # pay for a full enumeration.
    for candidate in _owned_popup_windows(hwnd):
        return _popup_payload(candidate, "owned_enum", hwnd)
    return None


def resize_plan(rect: list[int], width: int | None, height: int | None,
                anchor: str = "top-left", min_width: int = 160,
                min_height: int = 120) -> dict:
    """Compute the new rect for a resize request. Pure math, no side effects."""
    anchor = str(anchor or "top-left").strip().lower()
    if anchor not in _ANCHORS:
        return {"ok": False, "error": "bad_anchor", "hint": "|".join(sorted(_ANCHORS))}
    left, top, right, bottom = (int(v) for v in rect)
    old_w, old_h = right - left, bottom - top
    new_w = int(width) if width is not None else old_w
    new_h = int(height) if height is not None else old_h
    if new_w <= 0 or new_h <= 0:
        return {"ok": False, "error": "bad_size", "hint": "width/height must be positive"}
    clamped = new_w < min_width or new_h < min_height
    new_w, new_h = max(new_w, min_width), max(new_h, min_height)
    tokens = set(anchor.split("-"))
    if "left" in tokens:
        x = left
    elif "right" in tokens:
        x = right - new_w
    else:  # center horizontally
        x = left + old_w // 2 - new_w // 2
    if "top" in tokens:
        y = top
    elif "bottom" in tokens:
        y = bottom - new_h
    else:  # center vertically
        y = top + old_h // 2 - new_h // 2
    return {
        "ok": True,
        "rect": [x, y, x + new_w, y + new_h],
        "requested": [new_w, new_h],
        "clamped": clamped,
        "anchor": anchor,
    }


def _restore_for_resize(hwnd: int, dry_run: bool) -> dict:
    """Return the rect resize should start from, restoring minimized/maximized windows."""
    placement = winenv.window_placement(hwnd)
    minimized = winenv.is_minimized(hwnd)
    zoomed = winenv.is_zoomed(hwnd)
    if dry_run and placement:
        base = list(placement["normal_rect"])
        return {"rect": base, "from": "minimized" if minimized else ("maximized" if zoomed else None)}
    if minimized:
        winenv.user32.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.15)
        return {"rect": winenv.window_rect(hwnd), "from": "minimized"}
    if zoomed:
        winenv.user32.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.15)
        return {"rect": winenv.window_rect(hwnd), "from": "maximized"}
    return {"rect": winenv.window_rect(hwnd), "from": None}


def window_resize(title: str | None = None, hwnd: int | None = None,
                  width: int | None = None, height: int | None = None,
                  anchor: str = "top-left", dry_run: bool = False,
                  restore: bool = True) -> dict:
    """Resize via SetWindowPos (MoveWindow fallback). Reports what actually happened."""
    match = _match_window(title, hwnd)
    if match is None:
        return {"ok": False, "error": "window_not_found"}
    target = match["hwnd"]
    if not restore and not dry_run and (winenv.is_zoomed(target) or winenv.is_minimized(target)):
        return {"ok": False, "error": "restore_failed",
                "hint": "window is maximized or minimized; pass restore=true"}
    started = winenv.window_rect(target)
    if dry_run and winenv.is_zoomed(target):
        placement = winenv.window_placement(target)
        before = list(placement["normal_rect"]) if placement else started
        restored_from = "maximized"
    else:
        restored = _restore_for_resize(target, dry_run=dry_run)
        before = restored["rect"]
        restored_from = restored["from"]
    plan = resize_plan(before, width, height, anchor)
    if not plan["ok"]:
        return plan
    requested = plan["rect"]
    if dry_run:
        return {
            "ok": True, "action": "resize", "dry_run": True,
            "before": before, "requested": requested, "requested_size": plan["requested"],
            "anchor": plan["anchor"], "restored_from": restored_from,
            "clamped": plan["clamped"],
        }
    x, y = requested[0], requested[1]
    w, h = requested[2] - requested[0], requested[3] - requested[1]
    method = "setwindowpos"
    ok = winenv.set_window_pos(target, x, y, w, h)
    if not ok:
        method = "movewindow"
        ok = bool(winenv.user32.MoveWindow(target, x, y, w, h, True))
    time.sleep(0.2)
    after = winenv.window_rect(target)
    verified = all(abs(after[i] - requested[i]) <= 3 for i in range(4))
    changed = after != before
    clamped = plan["clamped"] or any(abs(after[i] - requested[i]) > 3 for i in range(4))
    result = {
        "ok": True, "action": "resize", "hwnd": target,
        "title": winenv.window_title(target),
        "before": before, "after": after, "requested": requested,
        "requested_size": plan["requested"], "anchor": plan["anchor"],
        "mode_used": "api", "method": method, "changed": changed,
        "verified": verified, "clamped": clamped,
        "restored_from": restored_from,
    }
    if restored_from:
        result["hint"] = f"window was {restored_from}; restored before resizing"
    if not changed:
        result.update({
            "ok": False, "error": "resize_failed",
            "hint": "the window refused SetWindowPos/MoveWindow; try mode=drag or it has a fixed size",
        })
    return result


# ---------------------------------------------------------------------------
# Explorer folders
# ---------------------------------------------------------------------------

def _shell_windows() -> list[dict]:
    try:
        import win32com.client

        shell = win32com.client.Dispatch("Shell.Application")
        result = []
        for window in shell.Windows():
            try:
                hwnd = int(window.HWND)
                url = str(window.LocationURL or "")
                path = unquote(url[8:]).replace("/", "\\") if url.startswith("file:///") else url
                result.append({"hwnd": hwnd, "path": path.rstrip("\\"), "title": str(window.LocationName or "")})
            except Exception:
                continue
        return result
    except Exception:
        return []


def folder_open(path: str, select: str | None = None) -> dict:
    target = os.path.abspath(os.path.expanduser(path))
    if not os.path.exists(target):
        return {"ok": False, "error": "path_not_found", "hint": target}
    if select:
        candidate = select if os.path.isabs(select) else os.path.join(target, select)
        if not os.path.exists(candidate):
            return {"ok": False, "error": "select_not_found", "hint": candidate}
        subprocess.Popen(["explorer.exe", f"/select,{candidate}"])
        return {"ok": True, "opened": target, "selected": candidate}
    subprocess.Popen(["explorer.exe", target])
    return {"ok": True, "opened": target}


def folder_list() -> list[dict]:
    return _shell_windows()


def folder_focus(path: str) -> dict:
    target = os.path.normcase(os.path.abspath(path))
    match = None
    for _attempt in range(4):
        for item in _shell_windows():
            if os.path.normcase(item["path"]) == target:
                match = item
                break
        if match:
            break
        time.sleep(0.25)
    if not match:
        return {"ok": False, "error": "folder_window_not_found", "hint": path}
    result = window_focus(hwnd=match["hwnd"])
    if result.get("ok"):
        return result
    # freshly opened Explorer windows may still have an empty title and be absent
    # from the visible-window enumeration; act on the Shell hwnd directly.
    hwnd = match["hwnd"]
    try:
        winenv.user32.ShowWindow(hwnd, SW_RESTORE)
        winenv.user32.SetForegroundWindow(hwnd)
        time.sleep(0.1)
        if winenv.foreground_hwnd() != hwnd:
            winenv.tap("alt")
            winenv.user32.SetForegroundWindow(hwnd)
            time.sleep(0.1)
    except Exception as exc:
        return {"ok": False, "error": "focus_failed", "hint": str(exc)}
    return {"ok": True, "window": match, "foreground": winenv.foreground_hwnd() == hwnd}


def folder_close(path: str | None = None, close_all: bool = False) -> dict:
    windows = _shell_windows()
    if not close_all:
        if not path:
            return {"ok": False, "error": "missing_path"}
        target = os.path.normcase(os.path.abspath(path))
        windows = [w for w in windows if os.path.normcase(w["path"]) == target]
    closed = []
    for item in windows:
        winenv.user32.PostMessageW(item["hwnd"], WM_CLOSE, 0, 0)
        closed.append(item)
    time.sleep(0.15)
    return {"ok": True, "closed": closed, "remaining": _shell_windows()}


# ---------------------------------------------------------------------------
# Clipboard
# ---------------------------------------------------------------------------

def clipboard_get() -> str:
    import win32clipboard

    win32clipboard.OpenClipboard()
    try:
        if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
            return win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
        return ""
    finally:
        win32clipboard.CloseClipboard()


def clipboard_set(text: str) -> dict:
    import win32clipboard

    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
    finally:
        win32clipboard.CloseClipboard()
    return {"ok": True, "length": len(text)}

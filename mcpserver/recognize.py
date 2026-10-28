"""Element recognition: UIA control tree + RapidOCR, merged into one inventory."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import numpy as np
from PIL import Image

import sysres
import winenv

UIA_TIMEOUT = 2.5
UIA_MAX_DEPTH = 8
UIA_MAX_NODES = 3000

_OCR = None


# ---------------------------------------------------------------------------
# UIA
# ---------------------------------------------------------------------------

def _uia_walk(hwnd: int, depth_limit: int, node_limit: int, deadline: float) -> tuple[list[dict], bool]:
    import uiautomation as auto

    if hwnd:
        root = auto.ControlFromHandle(hwnd)
        if root is None:
            return [], False
    else:
        root = auto.GetRootControl()

    found: list[dict] = []
    truncated = False
    queue: list[tuple[object, int]] = [(root, 0)]
    visited = 0

    while queue:
        if time.time() > deadline or visited >= node_limit:
            truncated = True
            break
        control, depth = queue.pop(0)
        visited += 1
        try:
            name = (control.Name or "").strip()
            rect = control.BoundingRectangle
            box = [int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)]
            if name and box[2] > box[0] and box[3] > box[1]:
                try:
                    help_text = str(control.HelpText or "").strip()
                except Exception:
                    help_text = ""
                found.append(
                    {
                        "source": "uia",
                        "type": str(control.ControlTypeName).replace("Control", "") or "Unknown",
                        "text": name,
                        "help": help_text,
                        "box": box,
                        "center": [(box[0] + box[2]) // 2, (box[1] + box[3]) // 2],
                        "hwnd": int(control.NativeWindowHandle or hwnd or 0),
                        "automation_id": str(control.AutomationId or ""),
                        "class": str(control.ClassName or ""),
                        "password": bool(control.IsPassword),
                        "offscreen": bool(control.IsOffscreen),
                    }
                )
        except Exception:
            pass
        if depth >= depth_limit:
            continue
        try:
            for child in control.GetChildren():
                if child is not None:
                    queue.append((child, depth + 1))
        except Exception:
            continue
    return found, truncated


def _uia_probe_main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    hwnd = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    timeout = float(os.environ.get("CU_UIA_TIMEOUT", UIA_TIMEOUT))
    elements, truncated = _uia_walk(hwnd, UIA_MAX_DEPTH, UIA_MAX_NODES, time.time() + timeout)
    print(json.dumps({"elements": elements, "truncated": truncated}, ensure_ascii=False))


def uia_elements(hwnd: int, timeout: float = UIA_TIMEOUT) -> tuple[list[dict], bool]:
    """Run the UIA crawl in a child process so a hung COM call cannot wedge the server."""
    if os.environ.get("CU_UIA_INPROC") == "1":
        return _uia_walk(hwnd, UIA_MAX_DEPTH, UIA_MAX_NODES, time.time() + timeout)
    env = dict(os.environ, CU_UIA_TIMEOUT=str(timeout), PYTHONIOENCODING="utf-8")
    try:
        proc = subprocess.run(
            [sys.executable, os.path.abspath(__file__), "--uia-probe", str(int(hwnd))],
            capture_output=True,
            timeout=timeout + 1.0,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return [], True
    except Exception:
        return [], True
    if proc.returncode != 0 or not proc.stdout:
        return [], True
    try:
        payload = json.loads(proc.stdout.decode("utf-8", "replace"))
    except Exception:
        return [], True
    return payload.get("elements", []), bool(payload.get("truncated"))


# ---------------------------------------------------------------------------
# OCR
# ---------------------------------------------------------------------------

def _engine():
    global _OCR
    if _OCR is None:
        from rapidocr_onnxruntime import RapidOCR

        _OCR = RapidOCR()
    return _OCR


def ocr_elements(img: Image.Image, offset: list[int] | None = None, max_width: int | None = None) -> list[dict]:
    offset = offset or [0, 0]
    max_width = int(os.environ.get("CU_OCR_MAX_WIDTH", max_width or 1600))
    scale = min(1.0, max_width / max(img.width, 1))
    work = img
    if scale < 0.999:
        work = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.LANCZOS)
    arr = np.asarray(work.convert("RGB"))[:, :, ::-1].copy()
    try:
        result, _ = _engine()(arr)
    except Exception:
        return []
    elements: list[dict] = []
    inverse = 1.0 / scale
    for item in result or []:
        try:
            box_points, text, score = item[0], str(item[1]).strip(), float(item[2])
        except Exception:
            continue
        if not text or score < 0.45:
            continue
        xs = [float(p[0]) for p in box_points]
        ys = [float(p[1]) for p in box_points]
        left = int(min(xs) * inverse) + offset[0]
        top = int(min(ys) * inverse) + offset[1]
        right = int(max(xs) * inverse) + offset[0]
        bottom = int(max(ys) * inverse) + offset[1]
        elements.append(
            {
                "source": "ocr",
                "type": "text",
                "text": text,
                "box": [left, top, right, bottom],
                "center": [(left + right) // 2, (top + bottom) // 2],
                "conf": round(score, 3),
            }
        )
    return elements


# ---------------------------------------------------------------------------
# Tooltips
# ---------------------------------------------------------------------------

TOOLTIP_MAX_WIDTH = 600
TOOLTIP_MAX_HEIGHT = 200
TOOLTIP_REACH = 60
TOOLTIP_POLL = 0.15
TOOLTIP_CLASSES = ("tooltips_class32", "#32774", "qtooltip", "tooltip", "net ui tool window")


def _near_distance(rect: list[int], point: list[int]) -> float:
    dx = 0 if rect[0] <= point[0] <= rect[2] else min(abs(point[0] - rect[0]), abs(point[0] - rect[2]))
    dy = 0 if rect[1] <= point[1] <= rect[3] else min(abs(point[1] - rect[1]), abs(point[1] - rect[3]))
    return (dx * dx + dy * dy) ** 0.5


def _contains(rect: list[int], point: list[int]) -> bool:
    return rect[0] <= point[0] <= rect[2] and rect[1] <= point[1] <= rect[3]


def _tooltip_window(cursor: list[int], before_hwnds: set[int]) -> dict | None:
    """Small window near the cursor that appeared after the move (or a known tooltip class)."""
    best: tuple[tuple[int, int], dict] | None = None
    for item in sysres.windows_all():
        rect = item["rect"]
        width, height = rect[2] - rect[0], rect[3] - rect[1]
        if width <= 0 or height <= 0 or width > TOOLTIP_MAX_WIDTH or height > TOOLTIP_MAX_HEIGHT:
            continue
        if _near_distance(rect, cursor) > TOOLTIP_REACH:
            continue
        cls = str(item.get("class") or "").lower()
        known = any(token in cls for token in TOOLTIP_CLASSES)
        new = item["hwnd"] not in before_hwnds
        if not new and not known:
            continue
        score = (1 if new else 0, 1 if known else 0)
        if best is None or score > best[0]:
            best = (score, item)
    return best[1] if best else None


def _tooltip_quick_texts(hwnd: int) -> tuple[list[str], str]:
    """Fast reads only (window title, child texts); safe to call while polling."""
    title = (winenv.window_title(hwnd) or "").strip()
    if title:
        return [title], "window-title"
    children = [t for t in winenv.enum_child_texts(hwnd) if t.strip()]
    if children:
        return list(dict.fromkeys(children)), "child-window-text"
    return [], ""


def _tooltip_uia_texts(hwnd: int) -> list[str]:
    """Slow (subprocess) UIA read, attempted once per hover."""
    elements, _ = uia_elements(hwnd, timeout=1.0)
    names: list[str] = []
    for element in elements:
        text = str(element.get("text") or "").strip()
        if text and text not in names:
            names.append(text)
    return names


def _ocr_boxes(box: list[int], cursor: list[int] | None = None, radius: int | None = None,
               exclude_containing: bool = True, limit: int = 3) -> list[dict]:
    import capture  # local: keeps capture/recognize import order free

    x1, y1, x2, y2 = (int(v) for v in box)
    if x2 <= x1 or y2 <= y1:
        return []
    try:
        image, meta = capture.grab_rect([x1, y1, x2, y2])
    except Exception:
        return []
    offset = [int(v) for v in meta.get("offset", [0, 0])]
    # Tooltip windows are often only tens of pixels wide; upscale before OCR.
    scale = 3 if (image.width < 240 or image.height < 80) else 1
    if scale > 1:
        image = image.resize((image.width * scale, image.height * scale), Image.LANCZOS)
    found: list[dict] = []
    for element in ocr_elements(image, [0, 0]):
        box_coords = [v // scale for v in element["box"]]
        center = [v // scale for v in element["center"]]
        element["box"] = [box_coords[0] + offset[0], box_coords[1] + offset[1],
                          box_coords[2] + offset[0], box_coords[3] + offset[1]]
        element["center"] = [center[0] + offset[0], center[1] + offset[1]]
        rect = element["box"]
        if cursor is not None:
            # The button's own label sits under the cursor; a tooltip never does.
            if exclude_containing and _contains(rect, cursor):
                continue
            if radius is not None and _near_distance(rect, cursor) > radius:
                continue
        found.append(element)
    if cursor is not None:
        found.sort(key=lambda e: _near_distance(e["box"], cursor))
    return found[:limit]


def read_tooltip(cursor: list[int], before_hwnds: set[int], settle: float = 0.8,
                 element_help: str = "", known_texts: list[str] | None = None) -> dict:
    """Read the tooltip for a hovered point: UIA HelpText > tooltip window text > OCR."""
    result = {
        "tooltip": None,
        "tooltip_source": "none",
        "tooltip_rect": None,
        "tooltip_candidates": [],
        "tooltip_texts": [],
        "tooltip_detail": "",
        "tooltip_hwnd": None,
        "tooltip_class": None,
    }
    help_text = str(element_help or "").strip()
    if help_text:
        result.update({
            "tooltip": help_text, "tooltip_source": "uia_help",
            "tooltip_candidates": [help_text], "tooltip_texts": [help_text],
            "tooltip_detail": "UIA HelpText",
        })
        return result

    known = {str(t).strip() for t in (known_texts or []) if str(t).strip()}
    # Explorer/ribbon tooltips can take ~1.5s to appear; settle is the user's
    # dwell request, the extra time is the platform's own show delay.
    deadline = time.time() + max(0.0, settle) + 1.5
    found_window = None
    texts: list[str] = []
    detail = ""
    source = "none"
    while time.time() < deadline:
        candidate = _tooltip_window(cursor, before_hwnds)
        if candidate is not None:
            found_window = candidate
            texts, detail = _tooltip_quick_texts(candidate["hwnd"])
            # The window is up; its text (if any) lives on a child/UIA node/OCR,
            # so stop polling and switch to the slower readers below.
            break
        time.sleep(TOOLTIP_POLL)

    rect = found_window["rect"] if found_window else None
    if found_window and not texts:
        uia_names = _tooltip_uia_texts(found_window["hwnd"])
        if uia_names:
            texts, source, detail = uia_names, "window", "uia-name"
    if texts:
        source = source if source != "none" else "window"
    candidates = [t for t in texts if t.strip()]
    if not candidates:
        if found_window and rect:
            # OCR this window only; its own text can never be the button label,
            # so don't filter against known_texts here.
            boxes = _ocr_boxes(rect, cursor=cursor, exclude_containing=False)
            detail = detail or "ocr of the tooltip window"
            candidates = [b["text"] for b in boxes if str(b["text"]).strip()]
        else:
            x, y = cursor
            # Tooltips conventionally render below the cursor; stay close to avoid
            # reading the page body as a tooltip.
            boxes = _ocr_boxes([x - 60, y + 10, x + 380, y + 150], cursor=cursor,
                               radius=40, exclude_containing=True)
            detail = "ocr near cursor"
            candidates = [b["text"] for b in boxes if str(b["text"]).strip() not in known]
        if candidates:
            source = "ocr"
            rect = boxes[0]["box"]

    unique = list(dict.fromkeys(t.strip() for t in candidates if str(t).strip()))
    if unique:
        result.update({
            "tooltip": unique[0],
            "tooltip_source": source,
            "tooltip_rect": rect,
            "tooltip_candidates": unique[:5],
            "tooltip_texts": unique[:5],
            "tooltip_detail": detail,
            "tooltip_hwnd": found_window["hwnd"] if found_window else None,
            "tooltip_class": found_window.get("class") if found_window else None,
        })
        if len(unique) > 1:
            result["tooltip_detail"] = f"{detail}; {len(unique)} candidates in tooltip_candidates"
    elif found_window:
        result.update({
            "tooltip_hwnd": found_window["hwnd"],
            "tooltip_class": found_window.get("class"),
            "tooltip_rect": rect,
            "tooltip_detail": "a tooltip window was found but no text could be read",
        })
    return result


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------

def _center_distance(a: list[int], b: list[int]) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


def _on_screen(box: list[int], screen: dict | None = None) -> bool:
    """True when the box center lies on the virtual desktop (hidden UIA nodes often don't)."""
    if screen is None:
        try:
            screen = winenv.virtual_screen()
        except Exception:
            return True
    cx, cy = (box[0] + box[2]) // 2, (box[1] + box[3]) // 2
    return (screen["left"] <= cx < screen["left"] + screen["width"]
            and screen["top"] <= cy < screen["top"] + screen["height"])


def merge(*groups: list[dict], min_distance: float = 12.0) -> list[dict]:
    """Combine sources with priority uia > ocr > tpl and stable ordering."""
    merged: list[dict] = []
    for group in groups:
        for element in group:
            if not element.get("box"):
                continue
            duplicate = False
            for existing in merged:
                if _center_distance(existing["center"], element["center"]) < min_distance:
                    # Same widget reported by two sources: keep the richer entry.
                    if not existing.get("text") and element.get("text"):
                        existing.update({k: v for k, v in element.items() if k != "id"})
                    duplicate = True
                    break
            if not duplicate:
                merged.append(dict(element))
    # Hidden UIA nodes can carry placeholder coordinates (-32000 etc.); keep them
    # last so the first ids the caller sees are actually clickable.
    try:
        screen = winenv.virtual_screen()
    except Exception:
        screen = None

    def _sort_key(element: dict) -> tuple[int, int, int]:
        box = element["box"]
        on_screen = _on_screen(box, screen) if screen else True
        return (0 if on_screen else 1, box[1], box[0])

    merged.sort(key=_sort_key)
    for index, element in enumerate(merged, start=1):
        element["id"] = index
    return merged


def texts_near(elements: list[dict], x: int, y: int, radius: int = 24) -> list[str]:
    texts = []
    for element in elements:
        box = element.get("box") or [0, 0, 0, 0]
        near = box[0] - radius <= x <= box[2] + radius and box[1] - radius <= y <= box[3] + radius
        if near and element.get("text"):
            texts.append(element["text"])
    return texts


def filter_elements(elements: list[dict], keyword: str | None) -> list[dict]:
    if not keyword:
        return elements
    needle = keyword.lower()
    return [e for e in elements if needle in str(e.get("text", "")).lower()]


def summarize(elements: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for element in elements:
        counts[element.get("source", "?")] = counts.get(element.get("source", "?"), 0) + 1
    return counts


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--uia-probe":
        _uia_probe_main()
    else:
        print("recognize.py is a library; pass --uia-probe <hwnd> to run the UIA crawler.")

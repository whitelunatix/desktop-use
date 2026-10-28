"""Composition layer: capture -> recognize -> cache, producing one ``see`` result."""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

import cache
import capture
import diff as diffmod
import locate
import recognize
import sysres
import winenv

STATE_ELEMENT_LIMIT = 500


class CaptureError(RuntimeError):
    """Machine-readable capture failure (subclasses RuntimeError for popup fallback)."""

    def __init__(self, code: str, hint: str = ""):
        super().__init__(code)
        self.code = code
        self.hint = hint


def _resolve_window(title: str | None, hwnd: int | None) -> int | None:
    if hwnd:
        target = int(hwnd)
        if not winenv.user32.IsWindow(target):
            raise CaptureError("window_not_found", f"hwnd {target} is not a window")
        if not winenv.is_window_visible(target):
            raise CaptureError("window_not_found", f"hwnd {target} is not visible")
        return target
    if not title:
        return None
    match = sysres._match_window(title)
    if match is None:
        raise CaptureError("window_not_found",
                           f"no visible window matches {title!r}; use cu_window list for exact titles")
    return match["hwnd"]


def _map_capture_error(exc: Exception) -> CaptureError:
    text = str(exc)
    if "window_minimized" in text:
        return CaptureError("window_minimized",
                            "restore the window before capturing it (cu_window action=focus)")
    if "window_has_no_area" in text or "printwindow_failed" in text:
        return CaptureError("capture_failed",
                            f"the window did not produce pixels ({text}); try mode=window or focus it")
    return CaptureError("capture_failed", text)


def _grab_target(hwnd: int, mode: str) -> tuple[Image.Image, dict]:
    if not winenv.user32.IsWindow(int(hwnd)):
        raise CaptureError("window_gone", "the window disappeared before it could be captured")
    if winenv.is_minimized(hwnd):
        raise CaptureError("window_minimized",
                           "restore the window before capturing it (cu_window action=focus)")
    rect = winenv.window_rect(hwnd)
    if rect[2] - rect[0] <= 0 or rect[3] - rect[1] <= 0:
        raise CaptureError("capture_failed", "the window has no visible area")
    try:
        if mode == "window":
            img, meta = capture.grab_window(hwnd)
            meta["capture_mode"] = "printwindow"
        else:
            img, meta = capture.grab_rect(rect)
            meta["capture_mode"] = "screen"
    except RuntimeError as exc:
        raise _map_capture_error(exc) from exc
    meta.setdefault("monitor", "window")
    meta["window_rect"] = [meta["offset"][0], meta["offset"][1],
                           meta["offset"][0] + meta["size"][0],
                           meta["offset"][1] + meta["size"][1]]
    return img, meta


def capture_target(monitor: str = "auto", window: str | None = None, hwnd: int | None = None,
                   region: list[int] | None = None, mode: str = "screen",
                   include_popups: bool = True) -> tuple[Image.Image, dict, int]:
    target_hwnd = _resolve_window(window, hwnd)
    if target_hwnd is not None:
        popup = sysres.popup_for(target_hwnd) if include_popups else None
        effective = popup["hwnd"] if popup else target_hwnd
        if popup:
            meta_extra = {"popup": popup, "scope_hwnd": target_hwnd, "target_hwnd": target_hwnd}
        else:
            meta_extra = {"scope_hwnd": target_hwnd}
        try:
            img, meta = _grab_target(effective, mode)
        except RuntimeError as exc:
            if popup is None:
                raise
            # The popup vanished between detection and capture: fall back, never fail.
            img, meta = _grab_target(target_hwnd, mode)
            meta["popup_fallback"] = str(exc)
            meta_extra = {"scope_hwnd": target_hwnd}
        meta.update(meta_extra)
        return img, meta, effective
    if region:
        img, meta = capture.grab_rect([region[0], region[1], region[0] + region[2], region[1] + region[3]])
        meta["monitor"] = "region"
        return img, meta, 0
    if monitor == "all":
        img, meta = capture.grab_virtual()
    else:
        img, meta = capture.grab_monitor(monitor)
    return img, meta, 0


def _compose_overlay(window_img: Image.Image, window_rect: list[int],
                     screen_rect: list[int]) -> tuple[Image.Image | None, dict]:
    """RGBA canvas the size of the screen, window pixels pasted opaque, everything else alpha=0."""
    sw, sh = screen_rect[2] - screen_rect[0], screen_rect[3] - screen_rect[1]
    canvas = Image.new("RGBA", (max(1, sw), max(1, sh)), (0, 0, 0, 0))
    ix1, iy1 = max(window_rect[0], screen_rect[0]), max(window_rect[1], screen_rect[1])
    ix2, iy2 = min(window_rect[2], screen_rect[2]), min(window_rect[3], screen_rect[3])
    if ix2 <= ix1 or iy2 <= iy1:
        return None, {"intersection": None}
    src = (ix1 - window_rect[0], iy1 - window_rect[1],
           ix2 - window_rect[0], iy2 - window_rect[1])
    dst = (ix1 - screen_rect[0], iy1 - screen_rect[1])
    canvas.paste(window_img.convert("RGBA").crop(src), dst)
    return canvas, {"intersection": [ix1, iy1, ix2, iy2], "paste_at": list(dst)}


def _occlusion_ratio(window_img: Image.Image, screen_img: Image.Image,
                     window_rect: list[int], screen_rect: list[int]) -> float | None:
    """Advisory only: share of visibly different pixels between PrintWindow and the screen."""
    try:
        box = (max(window_rect[0], screen_rect[0]) - screen_rect[0],
               max(window_rect[1], screen_rect[1]) - screen_rect[1],
               min(window_rect[2], screen_rect[2]) - screen_rect[0],
               min(window_rect[3], screen_rect[3]) - screen_rect[1])
        if box[2] <= box[0] or box[3] <= box[1]:
            return None
        shot = screen_img.crop(box).convert("L")
        win = window_img.convert("L").resize(shot.size, Image.BILINEAR)
        width = min(160, shot.width)
        if width < 8:
            return None
        height = max(1, round(shot.height * width / shot.width))
        a = np.asarray(shot.resize((width, height), Image.BILINEAR), dtype=np.int16)
        b = np.asarray(win.resize((width, height), Image.BILINEAR), dtype=np.int16)
        return round(float((np.abs(a - b) > 12).mean()), 3)
    except Exception:
        return None


def _reuse_screen_image(img: Image.Image, meta: dict, window_rect: list[int],
                        screen_rect: list[int]) -> Image.Image | None:
    """Crop the monitor out of a full-window screen capture when it covers the monitor."""
    if meta.get("capture_mode") != "screen":
        return None
    if not (window_rect[0] <= screen_rect[0] and window_rect[1] <= screen_rect[1]
            and window_rect[2] >= screen_rect[2] and window_rect[3] >= screen_rect[3]):
        return None
    box = (screen_rect[0] - window_rect[0], screen_rect[1] - window_rect[1],
           screen_rect[2] - window_rect[0], screen_rect[3] - window_rect[1])
    if box[2] <= box[0] or box[3] <= box[1]:
        return None
    return img.crop(box)


def _screen_products(img: Image.Image, meta: dict, hwnd: int, state_id: str,
                     overlay: bool = True) -> dict:
    """Capture the monitor around a window and compose the transparent overlay."""
    shots = cache.dirs()["shots"]
    window_rect = meta.get("window_rect") or [
        meta["offset"][0], meta["offset"][1],
        meta["offset"][0] + meta["size"][0], meta["offset"][1] + meta["size"][1],
    ]
    monitor = winenv.monitor_for_rect(window_rect)
    if monitor is None:
        return {"screen_error": "no_monitor_for_rect"}
    screen_rect = list(monitor["bounds"])
    screen_img = _reuse_screen_image(img, meta, window_rect, screen_rect)
    screen_source = "primary" if screen_img is not None else "recapture"
    if screen_img is None:
        try:
            screen_img, _ = capture.grab_rect(screen_rect)
        except Exception as exc:
            return {"screen_error": str(exc), "screen_rect": screen_rect}
    screen_path = str(shots / f"{state_id}-screen.png")
    capture.save_png(screen_img, screen_path)
    fully_visible = (window_rect[0] >= screen_rect[0] and window_rect[1] >= screen_rect[1]
                     and window_rect[2] <= screen_rect[2] and window_rect[3] <= screen_rect[3])
    overlay_path = None
    composed = {"intersection": None}
    if overlay:
        canvas, composed = _compose_overlay(img, window_rect, screen_rect)
        if canvas is not None:
            overlay_path = str(shots / f"{state_id}-overlay.png")
            canvas.save(overlay_path, "PNG")
    ratio = None
    if meta.get("capture_mode") == "printwindow" and fully_visible:
        ratio = _occlusion_ratio(img, screen_img, window_rect, screen_rect)
    placement = {
        "window_rect": window_rect,
        "screen_rect": screen_rect,
        "canvas_rect": screen_rect,
        "image_offset_in_screen": [window_rect[0] - screen_rect[0],
                                   window_rect[1] - screen_rect[1]],
        "window_size": [window_rect[2] - window_rect[0], window_rect[3] - window_rect[1]],
        "fully_visible": bool(fully_visible),
        "clipped": not fully_visible,
        "off_screen": composed.get("intersection") is None,
        "monitor": monitor["index"],
        "screen_source": screen_source,
        "occluded_ratio": ratio,
        "occluded": (ratio is not None and ratio > 0.15),
    }
    return {
        "screen_image": screen_path,
        "overlay_image": overlay_path,
        "placement": placement,
        "screen_rect": screen_rect,
    }


def see(
    monitor: str = "auto",
    window: str | None = None,
    region: list[int] | None = None,
    keyword: str | None = None,
    ocr: bool = True,
    fast: bool = False,
    mode: str = "screen",
    templates: list[str] | None = None,
    annotate: bool = False,
    max_elements: int = 120,
    redact: bool = True,
    hwnd: int | None = None,
    compare_to: str | None = None,
    diff_keyword: str | None = None,
    max_regions: int = 8,
    diff_crops: bool = True,
    only_changed: bool = True,
    diff_threshold: int = 6,
    include_screen: bool = False,
    overlay: bool = True,
    include_popups: bool = True,
    include_windows: bool = False,
) -> dict:
    started = time.time()
    img, meta, target_hwnd = capture_target(monitor=monitor, window=window, hwnd=hwnd, region=region,
                                            mode=mode, include_popups=include_popups)
    offset = list(meta.get("offset", [0, 0]))

    state_id = cache.new_state_id()
    shots = cache.dirs()["shots"]
    screen_info = None
    if include_screen and target_hwnd:
        screen_info = _screen_products(img, meta, target_hwnd, state_id, overlay=overlay)

    fg_hwnd = winenv.foreground_hwnd()
    probe_hwnd = target_hwnd or fg_hwnd
    uia_elements, truncated = ([], False) if fast else recognize.uia_elements(probe_hwnd)
    ocr_elements = [] if (fast or not ocr) else recognize.ocr_elements(img, offset)

    tpl_elements: list[dict] = []
    for template in templates or []:
        try:
            found = locate.locate(img, template, offset=offset, max_results=3)
        except Exception:
            continue
        for match in found["matches"]:
            x, y, w, h = match["box"]
            tpl_elements.append(
                {
                    "source": "tpl",
                    "type": "template",
                    "text": Path(str(template)).stem,
                    "box": [x + offset[0], y + offset[1], x + w + offset[0], y + h + offset[1]],
                    "center": match["global"],
                    "score": match["score"],
                    "scale": match["scale"],
                }
            )

    elements = recognize.merge(uia_elements, ocr_elements, tpl_elements)
    total = len(elements)
    # Keep the whole list in the state so later id/text clicks can still resolve
    # elements that were cut from this response; only the reply is trimmed.
    elements = elements[:STATE_ELEMENT_LIMIT]
    for index, element in enumerate(elements, start=1):
        element["id"] = index

    image_path = str(shots / f"{state_id}.png")
    stats = capture.image_stats(img)

    if redact:
        password_boxes = [
            e["box"] for e in uia_elements
            if e.get("password") and e.get("box")
        ]
        if password_boxes:
            draw = ImageDraw.Draw(img)
            for box in password_boxes:
                x1, y1, x2, y2 = box
                draw.rectangle([x1 - offset[0], y1 - offset[1], x2 - offset[0], y2 - offset[1]], fill=(0, 0, 0))

    diff_result = None
    if compare_to:
        ref = diffmod.load_ref(compare_to, allow_current=False)
        diff_result = diffmod.compare(
            img, ref["image"], after_offset=offset, before_offset=ref["offset"],
            threshold=diff_threshold, max_regions=max_regions, save=diff_crops,
            label=state_id, elements=elements, keyword=diff_keyword,
        )
        diff_result["compared_to"] = {
            "ref": ref["ref"], "image": ref.get("path") or ref.get("image"), "size": ref["size"],
        }

    pool = elements
    if diff_result and only_changed:
        pool = diffmod.inside_regions(elements, diff_result["regions"], key="global")
    visible = recognize.filter_elements(pool, keyword)[: max(1, int(max_elements))]

    capture.save_png(img, image_path)

    annotated_path = None
    if annotate:
        annotated_path = str(shots / f"{state_id}-annotated.png")
        locate.annotate_elements(img, visible).save(annotated_path, "PNG")

    active = {
        "hwnd": fg_hwnd,
        "title": winenv.window_title(fg_hwnd),
        "class": winenv.class_name(fg_hwnd),
        "rect": winenv.window_rect(fg_hwnd),
    }
    placement = (screen_info or {}).get("placement")
    window_rect = meta.get("window_rect") if target_hwnd else None
    popup = meta.get("popup")
    scope_hwnd = meta.get("scope_hwnd") or (target_hwnd or None)
    hints: list[str] = []
    if fast and ocr:
        hints.append("fast=true skipped UIA and OCR; drop fast (or keep only ocr=false) to get elements")
    if popup:
        hints.append(
            f"captured popup '{popup.get('title')}' ({popup.get('class')}) owned by the requested window; "
            "pass include_popups=false to capture the window itself"
        )
    if meta.get("popup_fallback"):
        hints.append(f"popup vanished before capture ({meta['popup_fallback']}); captured the base window")
    if screen_info and screen_info.get("screen_error"):
        hints.append(f"screen companion capture failed: {screen_info['screen_error']}")
    if placement and placement.get("off_screen"):
        hints.append("window is outside the captured monitor; overlay was not composed")

    state = {
        "state_id": state_id,
        "created_at": time.time(),
        "created_iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "image": image_path,
        "annotated_image": annotated_path,
        "captured": meta,
        "window": target_hwnd or fg_hwnd,
        "scope_hwnd": scope_hwnd,
        "window_target": meta.get("target_hwnd"),
        "popup": popup,
        "window_rect": window_rect,
        "screen_image": (screen_info or {}).get("screen_image"),
        "overlay_image": (screen_info or {}).get("overlay_image"),
        "placement": placement,
        "active_window": active,
        "elements": elements,
        "counts": {"uia": len(uia_elements), "ocr": len(ocr_elements), "tpl": len(tpl_elements),
                   "merged": total, "stored": len(elements), "returned": len(visible)},
        "blank_suspect": stats["blank_suspect"],
        "uia_truncated": truncated,
        "diff": diff_result,
    }
    cache.save_state(state)

    result = {
        "ok": True,
        "state_id": state_id,
        "image": image_path,
        "annotated_image": annotated_path,
        "offset": offset,
        "size": list(meta.get("size", [0, 0])),
        "window_rect": window_rect,
        "window_origin": window_rect[:2] if window_rect else None,
        "screen_image": (screen_info or {}).get("screen_image"),
        "overlay_image": (screen_info or {}).get("overlay_image"),
        "placement": placement,
        "popup": popup,
        "scope_hwnd": scope_hwnd,
        "captured": meta,
        "active_window": active,
        "elements": visible,
        "counts": state["counts"],
        "blank_suspect": stats["blank_suspect"],
        "uia_truncated": truncated,
        "diff": diff_result,
        "diff_image": (diff_result or {}).get("annotated_image"),
        "elapsed": round(time.time() - started, 2),
    }
    if include_windows:
        result["windows"] = sysres.windows_list()[:40]
    if hints:
        result["hint"] = "; ".join(hints)
    return result


def snapshot(label: str = "post", image: Image.Image | None = None,
             meta: dict | None = None) -> dict:
    """Capture used to verify an action; an already-grabbed image can be reused."""
    if image is None:
        img, meta = capture.grab_monitor("auto")
    else:
        img = image
        meta = dict(meta or {})
    state_id = cache.new_state_id()
    path = str(cache.dirs()["shots"] / f"{state_id}.png")
    capture.save_png(img, path)
    state = {
        "state_id": state_id,
        "created_at": time.time(),
        "created_iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "image": path,
        "captured": meta,
        "window": winenv.foreground_hwnd(),
        "elements": [],
        "counts": {"merged": 0, "returned": 0},
        "label": label,
    }
    cache.save_state(state)
    return {"state_id": state_id, "image": path, "meta": meta, "fingerprint": capture.fingerprint(img)}


def image_of(state: dict):
    from PIL import Image

    path = state.get("image")
    if not path or not Path(path).is_file():
        raise FileNotFoundError(f"state image missing: {path}")
    return Image.open(path).convert("RGB")

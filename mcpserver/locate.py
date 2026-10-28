"""Template location: multi-scale matching with NMS, returns image and desktop positions."""
from __future__ import annotations

import os
import time
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

import cache

DEFAULT_SCALES = (0.75, 0.9, 1.0, 1.1, 1.25)


class TemplateNotFound(Exception):
    pass


def resolve_template(ref: str) -> Path:
    candidate = Path(ref).expanduser()
    if candidate.is_file():
        return candidate
    name = ref if candidate.suffix else f"{ref}.png"
    in_cache = cache.dirs()["templates"] / name
    if in_cache.is_file():
        return in_cache
    builtin = Path(__file__).resolve().parent / "templates" / name
    if builtin.is_file():
        return builtin
    raise FileNotFoundError(f"template not found: {ref}")


def to_cv(image: Image.Image) -> np.ndarray:
    return cv2.cvtColor(np.asarray(image.convert("RGB")), cv2.COLOR_RGB2BGR)


def _load_template(path: Path) -> tuple[np.ndarray, np.ndarray | None]:
    """Read via PIL: cv2.imread cannot open non-ASCII paths on Windows."""
    try:
        with Image.open(path) as img:
            if img.mode == "RGBA":
                raw = np.asarray(img)
                return cv2.cvtColor(raw[:, :, :3], cv2.COLOR_RGB2BGR), raw[:, :, 3].copy()
            return cv2.cvtColor(np.asarray(img.convert("RGB")), cv2.COLOR_RGB2BGR), None
    except Exception as exc:
        raise FileNotFoundError(f"cannot read template: {path} ({exc})") from exc


def _iou(a: list[int], b: list[int]) -> float:
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[0] + a[2], b[0] + b[2]), min(a[1] + a[3], b[1] + b[3])
    if x2 <= x1 or y2 <= y1:
        return 0.0
    inter = (x2 - x1) * (y2 - y1)
    union = a[2] * a[3] + b[2] * b[3] - inter
    return inter / union if union else 0.0


def _nms(candidates: list[dict], max_results: int, iou_limit: float = 0.3) -> list[dict]:
    kept: list[dict] = []
    for candidate in sorted(candidates, key=lambda c: c["score"], reverse=True):
        if all(_iou(candidate["box"], other["box"]) <= iou_limit for other in kept):
            kept.append(candidate)
        if len(kept) >= max_results:
            break
    return kept


def locate(
    image: Image.Image,
    template_ref: str,
    region: list[int] | None = None,
    threshold: float = 0.87,
    scales: tuple[float, ...] = DEFAULT_SCALES,
    max_results: int = 5,
    offset: list[int] | None = None,
    debug_name: str | None = None,
) -> dict:
    """Find ``template_ref`` inside ``image``.

    ``region`` is expressed in the image's own pixel space.
    ``offset`` maps image pixels to desktop pixels (the captured origin).
    """
    offset = offset or [0, 0]
    full = to_cv(image)
    left, top = 0, 0
    search = full
    if region:
        rx, ry, rw, rh = (int(v) for v in region)
        rx = max(0, rx)
        ry = max(0, ry)
        rw = min(rw, full.shape[1] - rx)
        rh = min(rh, full.shape[0] - ry)
        if rw <= 0 or rh <= 0:
            raise ValueError("region outside image")
        search = full[ry : ry + rh, rx : rx + rw]
        left, top = rx, ry

    try:
        template_path = resolve_template(template_ref)
        tpl_color, mask = _load_template(template_path)
    except FileNotFoundError as exc:
        raise TemplateNotFound(str(exc)) from exc
    search_gray = cv2.cvtColor(search, cv2.COLOR_BGR2GRAY)

    candidates: list[dict] = []
    best_score = 0.0
    for scale in scales:
        tw = max(1, int(tpl_color.shape[1] * scale))
        th = max(1, int(tpl_color.shape[0] * scale))
        if tw > search.shape[1] or th > search.shape[0]:
            continue
        tpl = cv2.resize(tpl_color, (tw, th), interpolation=cv2.INTER_AREA)
        if mask is not None:
            m = cv2.resize(mask, (tw, th), interpolation=cv2.INTER_NEAREST)
            result = cv2.matchTemplate(search, tpl, cv2.TM_CCORR_NORMED, mask=m)
        else:
            result = cv2.matchTemplate(search_gray, cv2.cvtColor(tpl, cv2.COLOR_BGR2GRAY), cv2.TM_CCOEFF_NORMED)
        result = np.nan_to_num(result, nan=0.0, posinf=0.0, neginf=0.0)
        best_score = max(best_score, float(result.max()) if result.size else 0.0)
        ys, xs = np.where(result >= threshold)
        for y, x in zip(ys, xs):
            candidates.append(
                {
                    "box": [int(x) + left, int(y) + top, tw, th],
                    "score": round(float(result[y, x]), 4),
                    "scale": round(scale, 3),
                }
            )

    matches = _nms(candidates, max_results)
    for match in matches:
        x, y, w, h = match["box"]
        match["position"] = [x, y]
        match["center"] = [x + w // 2, y + h // 2]
        match["global"] = [offset[0] + x + w // 2, offset[1] + y + h // 2]

    if not matches:
        debug = write_debug(full, [], template_ref, best_score, debug_name)
        raise TemplateNotFound(f"template_not_found: best_score={best_score:.3f}; debug={debug}")

    return {"matches": matches, "best_score": round(best_score, 4), "template": str(template_path)}


def write_debug(image_bgr: np.ndarray, matches: list[dict], template_ref: str, best_score: float,
                debug_name: str | None = None) -> str:
    canvas = image_bgr.copy()
    for match in matches:
        x, y, w, h = match["box"]
        cv2.rectangle(canvas, (x, y), (x + w, y + h), (0, 200, 0), 2)
        cv2.putText(canvas, f"{match['score']:.2f}", (x, max(12, y - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 200, 0), 1)
    if not matches:
        cv2.putText(canvas, f"not found: {template_ref} best={best_score:.2f}", (20, 40),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 255), 2)
    name = debug_name or f"match-{time.strftime('%Y%m%d-%H%M%S')}.png"
    path = cache.dirs()["debug"] / name
    Image.fromarray(cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB)).save(path, "PNG")
    return str(path)


def annotate_elements(image: Image.Image, elements: list[dict]) -> Image.Image:
    canvas = to_cv(image).copy()
    for element in elements:
        box = element.get("box") or []
        if len(box) != 4:
            continue
        x1, y1, x2, y2 = box
        color = {"uia": (0, 160, 255), "ocr": (0, 200, 0), "tpl": (255, 0, 200)}.get(element.get("source"), (180, 180, 180))
        cv2.rectangle(canvas, (x1, y1), (x2, y2), color, 1)
        cv2.putText(canvas, str(element.get("id", "")), (x1, max(10, y1 - 3)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1)
    return Image.fromarray(cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB))

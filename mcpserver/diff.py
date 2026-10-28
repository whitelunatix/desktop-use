"""Incremental screen diff: which rectangles changed, plus cropped before/after images."""
from __future__ import annotations

import re
import time
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw

import cache

TILE = 32
DILATE = 3
MERGE_GAP = 24
PAD = 12
MIN_AREA = 400
MIN_PIXELS = 120
MAX_REGIONS = 8
THRESHOLD = 6
PIXEL_NOISE = 8

STATE_ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-z]{2,8}$")


class DiffError(Exception):
    def __init__(self, code: str, hint: str = ""):
        super().__init__(code)
        self.code = code
        self.hint = hint


# ---------------------------------------------------------------------------
# Reference resolution
# ---------------------------------------------------------------------------

def load_ref(ref: str | None, *, allow_current: bool = True) -> dict:
    """Resolve ``current`` / ``state:<id>`` / ``image:<path>`` / ``node:<tree>/<node>``."""
    ref = (ref or "current").strip()
    if ref == "current":
        if not allow_current:
            raise DiffError("bad_ref", "current is not allowed here")
        import capture
        img, meta = capture.grab_monitor("auto")
        return {"image": img, "offset": list(meta.get("offset", [0, 0])), "label": ref,
                "ref": ref, "size": list(img.size), "meta": meta}

    kind, _, value = ref.partition(":")
    if not value:
        kind, value = "", ref

    if kind == "image":
        path = Path(value).expanduser()
        return {**_from_path(path), "ref": ref, "label": ref}

    if kind == "state":
        return {**_from_state(value), "ref": ref, "label": ref}

    if kind == "node":
        import tree as tree_mod
        tree_name, _, node_ref = value.partition("/")
        if not node_ref:
            raise DiffError("bad_ref", "use node:<tree>/<node>")
        data = tree_mod.load(tree_name)
        node = tree_mod.find_node(data, node_ref)
        if node is None:
            raise DiffError("node_not_found", node_ref)
        state = cache.load_state(node.get("state_id") or "") if node.get("state_id") else None
        offset = list((state or {}).get("captured", {}).get("offset", [0, 0]))
        image = _open_image(node.get("image"))
        return {"image": image, "offset": offset, "label": ref, "ref": ref,
                "size": list(image.size), "node": node}

    if kind == "":
        if STATE_ID_RE.match(value) or cache.load_state(value):
            return {**_from_state(value), "ref": ref, "label": ref}
        path = Path(value).expanduser()
        if path.is_file():
            return {**_from_path(path), "ref": ref, "label": ref}

    raise DiffError("bad_ref", f"cannot resolve {ref!r}; use current|state:<id>|image:<path>|node:<tree>/<node>")


def _open_image(path: str | Path | None) -> Image.Image:
    if not path:
        raise DiffError("ref_image_missing", "empty image path")
    candidate = Path(path)
    if not candidate.is_file():
        raise DiffError("ref_image_missing", str(candidate))
    try:
        return Image.open(candidate).convert("RGB")
    except Exception as exc:
        raise DiffError("ref_image_missing", f"{candidate}: {exc}") from exc


def _from_path(path: Path) -> dict:
    image = _open_image(path)
    return {"image": image, "offset": [0, 0], "size": list(image.size), "path": str(path)}


def _from_state(state_id: str) -> dict:
    state = cache.load_state(state_id)
    if state is None:
        raise DiffError("ref_image_missing", f"state {state_id} not found")
    image = _open_image(state.get("image"))
    return {"image": image, "offset": list(state.get("captured", {}).get("offset", [0, 0])),
            "size": list(image.size), "state_id": state_id, "path": state.get("image"),
            "elements": state.get("elements", [])}


# ---------------------------------------------------------------------------
# Diff core
# ---------------------------------------------------------------------------

def _block_mean(delta: np.ndarray, tile: int) -> np.ndarray:
    height, width = delta.shape
    pad_h = (tile - height % tile) % tile
    pad_w = (tile - width % tile) % tile
    if pad_h or pad_w:
        delta = np.pad(delta, ((0, pad_h), (0, pad_w)), mode="edge")
    return delta.reshape(delta.shape[0] // tile, tile, delta.shape[1] // tile, tile).mean(axis=(1, 3))


def _close(a: list[int], b: list[int], gap: int) -> bool:
    return not (a[2] + gap < b[0] or b[2] + gap < a[0] or a[3] + gap < b[1] or b[3] + gap < a[1])


def _union(a: list[int], b: list[int]) -> list[int]:
    return [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]


def _merge_boxes(boxes: list[list[int]], gap: int) -> list[list[int]]:
    pending = [list(b) for b in boxes]
    merged_any = True
    while merged_any:
        merged_any = False
        result: list[list[int]] = []
        while pending:
            current = pending.pop()
            changed = True
            while changed:
                changed = False
                rest = []
                for other in pending:
                    if _close(current, other, gap):
                        current = _union(current, other)
                        changed = True
                        merged_any = True
                    else:
                        rest.append(other)
                pending = rest
            result.append(current)
        pending = result
    return pending


def _clamp_box(box: list[int], width: int, height: int) -> list[int]:
    return [max(0, box[0]), max(0, box[1]), min(width, box[2]), min(height, box[3])]


def _region_texts(elements: list[dict], box: list[int], ocr_texts: bool, crop: Image.Image | None,
                  global_origin: tuple[int, int]) -> list[str]:
    texts: list[str] = []
    for element in elements or []:
        eb = element.get("box") or []
        if len(eb) != 4:
            continue
        cx, cy = (eb[0] + eb[2]) // 2, (eb[1] + eb[3]) // 2
        if box[0] <= cx <= box[2] and box[1] <= cy <= box[3] and element.get("text"):
            if element["text"] not in texts:
                texts.append(str(element["text"]))
    if not texts and ocr_texts and crop is not None:
        try:
            import recognize
            for element in recognize.ocr_elements(crop, [global_origin[0], global_origin[1]]):
                if element.get("text") and element["text"] not in texts:
                    texts.append(str(element["text"]))
        except Exception:
            pass
    return texts[:8]


def compare(after: Image.Image, before: Image.Image, *, after_offset=(0, 0), before_offset=(0, 0),
            threshold: int = THRESHOLD, max_regions: int = MAX_REGIONS, min_area: int = MIN_AREA,
            min_pixels: int = MIN_PIXELS, pad: int = PAD, save: bool = True, label: str | None = None,
            elements: list[dict] | None = None, keyword: str | None = None,
            ocr_texts: bool = True) -> dict:
    """Return changed rectangles of ``after`` relative to ``before``.

    Boxes and ``offset`` are in ``after`` image pixels; ``global`` adds the capture origin.
    """
    after = after.convert("RGB")
    before = before.convert("RGB")
    width, height = after.size
    bw, bh = before.size
    size_match = (width, height) == (bw, bh)
    size_delta = [bw - width, bh - height]

    work_w, work_h = min(width, bw), min(height, bh)
    a_gray = np.asarray(after.crop((0, 0, work_w, work_h)).convert("L"), dtype=np.int16)
    b_gray = np.asarray(before.crop((0, 0, work_w, work_h)).convert("L"), dtype=np.int16)
    delta = np.abs(a_gray - b_gray)

    grid = _block_mean(delta, TILE)
    mask = (grid > threshold).astype(np.uint8)
    if not mask.any():
        return {"changed": False, "size_match": size_match, "size_delta": size_delta,
                "changed_area": 0.0, "regions": [], "annotated_image": None,
                "others": {"count": 0, "area": 0.0}, "threshold": threshold, "filtered": bool(keyword)}

    mask = cv2.dilate(mask, np.ones((DILATE, DILATE), np.uint8), iterations=1)
    count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)

    boxes: list[list[int]] = []
    for index in range(1, count):
        gx, gy, gw, gh, _area = stats[index]
        box = _clamp_box([int(gx) * TILE - pad, int(gy) * TILE - pad,
                          (int(gx) + int(gw)) * TILE + pad, (int(gy) + int(gh)) * TILE + pad],
                         work_w, work_h)
        if (box[2] - box[0]) * (box[3] - box[1]) >= min_area:
            boxes.append(box)
    boxes = _merge_boxes(boxes, MERGE_GAP)

    stamp = label or f"diff-{time.strftime('%Y%m%d-%H%M%S')}-{int(time.time() * 1000) % 1000:03d}"
    shots = cache.dirs()["shots"]

    regions: list[dict] = []
    for box in boxes:
        x1, y1, x2, y2 = box
        if x2 <= x1 or y2 <= y1:
            continue
        piece = delta[y1:y2, x1:x2]
        changed_pixels = int((piece > PIXEL_NOISE).sum())
        if changed_pixels < min_pixels:
            continue
        region = {
            "box": [x1, y1, x2, y2],
            "offset": [x1, y1],
            "global": [after_offset[0] + x1, after_offset[1] + y1,
                       after_offset[0] + x2, after_offset[1] + y2],
            "relative": [round(x1 / max(width, 1), 4), round(y1 / max(height, 1), 4)],
            "score": round(float(piece.mean()), 2),
            "ratio": round(changed_pixels / max(1, (x2 - x1) * (y2 - y1)), 4),
            "pixels": changed_pixels,
        }
        crop_after = after.crop((x1, y1, x2, y2))
        crop_before = before.crop((x1, y1, min(x2, bw), min(y2, bh))) if size_match else None
        region["texts"] = _region_texts(elements or [], [x1, y1, x2, y2], ocr_texts,
                                        crop_after, (after_offset[0] + x1, after_offset[1] + y1))
        if save:
            region["image"] = str(shots / f"{stamp}-diff-{len(regions) + 1}.png")
            crop_after.save(region["image"], "PNG")
            if crop_before is not None:
                region["before_image"] = str(shots / f"{stamp}-diff-{len(regions) + 1}-b.png")
                crop_before.save(region["before_image"], "PNG")
        regions.append(region)

    regions.sort(key=lambda r: r["score"], reverse=True)
    kept = regions[: max(1, int(max_regions))]
    dropped = regions[max(1, int(max_regions)):]
    for index, region in enumerate(kept, start=1):
        region["index"] = index

    changed_area = round(sum(r["pixels"] for r in kept) / max(1, work_w * work_h), 5)
    others = {"count": len(dropped), "area": round(sum(r["pixels"] for r in dropped) / max(1, work_w * work_h), 5)}

    filtered = False
    if keyword:
        needle = keyword.lower()
        kept = [r for r in kept if any(needle in str(t).lower() for t in r["texts"])]
        filtered = True

    annotated = None
    if save and kept:
        annotated = str(shots / f"{stamp}-diff.png")
        _annotate(after, kept).save(annotated, "PNG")

    return {
        "changed": bool(kept),
        "size_match": size_match,
        "size_delta": size_delta,
        "changed_area": changed_area,
        "regions": kept,
        "annotated_image": annotated,
        "others": others,
        "threshold": threshold,
        "filtered": filtered,
    }


def _annotate(image: Image.Image, regions: list[dict]) -> Image.Image:
    canvas = image.copy()
    draw = ImageDraw.Draw(canvas)
    for region in regions:
        x1, y1, x2, y2 = region["box"]
        draw.rectangle([x1, y1, x2 - 1, y2 - 1], outline=(220, 40, 40), width=2)
        draw.text((x1 + 3, max(0, y1 - 14)), f"#{region['index']} {region['score']:.1f}", fill=(220, 40, 40))
    return canvas


def inside_regions(elements: list[dict], regions: list[dict], key: str = "box") -> list[dict]:
    """Elements whose center falls inside one of the changed regions."""
    if not regions:
        return []
    selected = []
    for element in elements:
        box = element.get("box") or []
        if len(box) != 4:
            continue
        cx, cy = (box[0] + box[2]) // 2, (box[1] + box[3]) // 2
        for region in regions:
            x1, y1, x2, y2 = (region.get(key) or region["box"])[:4]
            if x1 <= cx <= x2 and y1 <= cy <= y2:
                selected.append(element)
                break
    return selected

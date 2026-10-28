"""Template cropping and template-library management."""
from __future__ import annotations

import re
import time
from pathlib import Path

from PIL import Image

import cache


def parse_ints(text: str) -> list[int]:
    values = [int(v) for v in re.findall(r"-?\d+", str(text))]
    return values


def parse_box(text: str) -> list[int]:
    values = parse_ints(text)
    if len(values) != 4:
        raise ValueError("box must be 'x,y,w,h'")
    return values


def parse_point(text: str) -> list[int]:
    values = parse_ints(text)
    if len(values) != 2:
        raise ValueError("point must be 'x,y'")
    return values


def safe_name(name: str) -> str:
    cleaned = re.sub(r"[^\w\-.]+", "_", str(name or "")).strip("._")
    return cleaned or f"tpl-{time.strftime('%Y%m%d-%H%M%S')}"


def crop_image(image: Image.Image, box: list[int], pad: int = 4, upscale: int = 1) -> tuple[Image.Image, list[int]]:
    x, y, w, h = (int(v) for v in box)
    x = max(0, x - pad)
    y = max(0, y - pad)
    w = min(image.width - x, w + pad * 2)
    h = min(image.height - y, h + pad * 2)
    if w <= 0 or h <= 0:
        raise ValueError("empty crop region")
    region = image.crop((x, y, x + w, y + h))
    if upscale and upscale > 1:
        region = region.resize((w * upscale, h * upscale), Image.LANCZOS)
    return region, [x, y, w, h]


def save_template(image: Image.Image, name: str) -> Path:
    path = cache.dirs()["templates"] / f"{safe_name(name)}.png"
    image.save(path, "PNG")
    return path


def list_templates() -> list[dict]:
    items: list[dict] = []
    seen: set[str] = set()
    for path in sorted(cache.dirs()["templates"].glob("*.png")):
        with Image.open(path) as img:
            items.append({"name": path.stem, "path": str(path), "size": list(img.size), "builtin": False})
        seen.add(path.stem)
    builtin_dir = Path(__file__).resolve().parent / "templates"
    for path in sorted(builtin_dir.glob("*.png")):
        if path.stem in seen:
            continue
        with Image.open(path) as img:
            items.append({"name": path.stem, "path": str(path), "size": list(img.size), "builtin": True})
    return items


def delete_template(name: str) -> dict:
    path = cache.dirs()["templates"] / f"{safe_name(name)}.png"
    if not path.exists():
        raise FileNotFoundError(f"template not found in cache: {name}")
    path.unlink()
    return {"deleted": str(path)}

"""UI state trees for any window, page or screen region, stored under <cwd>/.computer-use/trees."""
from __future__ import annotations

import base64
import json
import os
import re
import time
from pathlib import Path

import numpy as np
from PIL import Image

import actions
import cache
import capture
import diff
import sysres
import vision
import winenv

FP_W, FP_H = 128, 72
MAX_NODES = 200
MAX_DEPTH = 10
MATCH_THRESHOLD = 0.88
RISKY = actions.RISKY_WORDS

_SELECTOR_KINDS = ("text", "template", "xy", "key")
RISKY_COMBOS = {"alt+f4", "ctrl+w", "ctrl+shift+w"}
_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


class TreeError(Exception):
    def __init__(self, code: str, hint: str = ""):
        super().__init__(code)
        self.code = code
        self.hint = hint


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def trees_dir() -> Path:
    path = cache.dirs()["root"] / "trees"
    path.mkdir(parents=True, exist_ok=True)
    return path


def slug(name: str) -> str:
    cleaned = _INVALID.sub("-", str(name).strip()).strip(" .-")
    return cleaned[:60] or "tree"


def path_of(name: str) -> Path:
    return trees_dir() / f"{slug(name)}.json"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def scope_of(data: dict) -> dict:
    """What a tree is bound to: any window, web page, dialog or screen region."""
    return data.get("scope") or data.get("app") or {}


def _focus_scope(scope: dict) -> None:
    """Prefer the recorded hwnd (titles are ambiguous when several windows share a prefix)."""
    hwnd = scope.get("hwnd")
    if hwnd:
        try:
            if any(int(w["hwnd"]) == int(hwnd) for w in sysres.windows_list()):
                if sysres.window_focus(hwnd=int(hwnd)).get("ok"):
                    return
        except Exception:
            pass
    needle = scope.get("title_contains")
    if needle:
        try:
            sysres.window_focus(title=needle)
        except Exception:
            pass


def list_trees() -> list[dict]:
    items = []
    for path in trees_dir().glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        items.append({
            "name": data.get("name", path.stem),
            "scope": scope_of(data),
            "root": data.get("root"),
            "nodes": len(data.get("nodes", {})),
            "updated_at": data.get("updated_at"),
        })
    items.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
    return items


def save(data: dict) -> dict:
    data = dict(data)
    data["updated_at"] = _now()
    target = path_of(data["name"])
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, target)
    return data


def load(name: str | None = None) -> dict:
    if name:
        path = path_of(name)
        if not path.is_file():
            raise TreeError("tree_not_found", f"{name}; available: {[t['name'] for t in list_trees()]}")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise TreeError("tree_not_found", f"{path}: {exc}") from exc
    return pick_tree()


def pick_tree() -> dict:
    """Choose a tree by the foreground window title/class; fall back to the only tree."""
    items = list_trees()
    if not items:
        raise TreeError("tree_not_found", "no trees yet; cu_tree(action=create, name=..., app_title=...)")
    hwnd = winenv.foreground_hwnd()
    title = (winenv.window_title(hwnd) or "").lower()
    klass = (winenv.class_name(hwnd) or "").lower()
    live = {int(w["hwnd"]) for w in sysres.windows_list()}
    scored = []
    for item in items:
        scope = item.get("scope") or {}
        score = 0
        needle = str(scope.get("title_contains") or "").lower()
        if needle:
            score += 2 if needle in title else 0
        if scope.get("class") and str(scope["class"]).lower() == klass:
            score += 1
        if scope.get("hwnd") and int(scope["hwnd"]) in live and int(scope["hwnd"]) == int(hwnd):
            score += 3
        scored.append((score, item))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    if scored[0][0] > 0:
        return load(scored[0][1]["name"])
    if len(items) == 1:
        return load(items[0]["name"])
    raise TreeError("tree_not_found", f"cannot tell which tree; pass name= one of {[t['name'] for t in items]}")


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

def find_node(data: dict, ref: str | None) -> dict | None:
    if not ref:
        return None
    ref = str(ref).strip()
    nodes = data.get("nodes", {})
    if ref in nodes:
        return nodes[ref]
    needle = ref.lower()
    exact = [n for n in nodes.values() if str(n.get("title", "")).lower() == needle]
    if len(exact) == 1:
        return exact[0]
    partial = [n for n in nodes.values() if needle in str(n.get("title", "")).lower()]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        raise TreeError("node_not_found", f"{ref!r} matches several: {[n['id'] for n in partial]}")
    return None


def node_of(data: dict, ref: str | None) -> dict:
    node = find_node(data, ref)
    if node is None:
        raise TreeError("node_not_found", f"{ref!r}; nodes: {list(data.get('nodes', {}))}")
    return node


def _next_id(data: dict) -> str:
    used = [int(m.group(1)) for key in data.get("nodes", {}) for m in [re.match(r"n(\d+)$", key)] if m]
    return f"n{max(used, default=0) + 1}"


def _validate_target(target: str | None) -> str | None:
    if not target:
        return None
    kind, _, _value = str(target).partition(":")
    if kind.strip().lower() not in _SELECTOR_KINDS:
        raise TreeError("bad_selector",
                        "target must be text:..., template:..., xy:x,y or key:ctrl+o (ids are screenshot-scoped)")
    return str(target)


def _parse_region(region) -> list[int] | None:
    if region is None or region == "":
        return None
    if isinstance(region, (list, tuple)) and len(region) == 4:
        return [int(v) for v in region]
    values = [int(v) for v in re.findall(r"-?\d+", str(region))]
    if len(values) != 4:
        raise TreeError("bad_region", "region must be 'x,y,w,h' in desktop pixels")
    return values


def path_to(data: dict, node_id: str) -> list[str]:
    chain = []
    current = data["nodes"].get(node_id)
    while current is not None:
        chain.append(current["id"])
        parent = current.get("parent")
        current = data["nodes"].get(parent) if parent else None
        if len(chain) > MAX_DEPTH + 1:
            raise TreeError("cycle", "node chain is too deep; the tree may be corrupted")
    return list(reversed(chain))


def _descendants(data: dict, node_id: str) -> list[str]:
    out = []
    stack = [node_id]
    while stack:
        current = stack.pop()
        for child in data["nodes"].get(current, {}).get("children", []):
            if child in data["nodes"]:
                out.append(child)
                stack.append(child)
    return out


# ---------------------------------------------------------------------------
# Fingerprints
# ---------------------------------------------------------------------------

def _crop_region(img: Image.Image, offset, region) -> Image.Image:
    if not region:
        return img
    try:
        x, y, w, h = [int(v) for v in region]
    except Exception:
        return img
    if w <= 0 or h <= 0:
        return img
    lx, ly = x - int(offset[0]), y - int(offset[1])
    box = (max(0, lx), max(0, ly), min(img.width, lx + w), min(img.height, ly + h))
    if box[2] <= box[0] or box[3] <= box[1]:
        return img
    return img.crop(box)


def _gray_vector(img: Image.Image, offset, region) -> np.ndarray:
    piece = _crop_region(img, offset, region).convert("L").resize((FP_W, FP_H), Image.BILINEAR)
    return np.asarray(piece, dtype=np.int16)


def fingerprint(img: Image.Image, offset=(0, 0), region=None) -> str:
    return base64.b64encode(_gray_vector(img, offset, region).astype(np.uint8).tobytes()).decode()


def score(fp_b64: str, img: Image.Image, offset=(0, 0), region=None) -> float:
    try:
        stored = np.frombuffer(base64.b64decode(fp_b64), dtype=np.uint8).reshape(FP_H, FP_W).astype(np.int16)
    except Exception:
        return 0.0
    current = _gray_vector(img, offset, region)
    if stored.shape != current.shape:
        return 0.0
    return round(max(0.0, 1.0 - float(np.abs(stored - current).mean()) / 255.0), 4)


def _node_score(node: dict, img: Image.Image, offset) -> float:
    if not node.get("fingerprint"):
        return 0.0
    return score(node["fingerprint"], img, offset, node.get("region"))


# ---------------------------------------------------------------------------
# Capture helpers
# ---------------------------------------------------------------------------

def _grab() -> tuple[Image.Image, list[int]]:
    img, meta = capture.grab_monitor("auto")
    return img, list(meta.get("offset", [0, 0]))


def _snapshot_state(label: str = "node") -> tuple[dict, Image.Image]:
    shot = vision.snapshot(label)
    state = cache.load_state(shot["state_id"]) or {}
    return state, vision.image_of(state)


def _state_shot(state_id: str) -> tuple[dict, Image.Image, list[int]]:
    state = cache.load_state(state_id)
    if state is None:
        raise TreeError("ref_image_missing", f"state {state_id} is gone")
    img = vision.image_of(state)
    return state, img, list(state.get("captured", {}).get("offset", [0, 0]))


def _node_from_shot(data: dict, parent: str | None, title: str, target: str | None,
                    region, state: dict, img: Image.Image, notes: str = "",
                    tags: list[str] | None = None) -> dict:
    offset = list(state.get("captured", {}).get("offset", [0, 0]))
    now = _now()
    return {
        "id": _next_id(data),
        "title": title,
        "parent": parent,
        "children": [],
        "target": target,
        "region": region,
        "state_id": state.get("state_id"),
        "image": state.get("image"),
        "fingerprint": fingerprint(img, offset, region),
        "notes": notes,
        "tags": list(tags or []),
        "created_at": now,
        "updated_at": now,
    }


def _attach(data: dict, node: dict) -> None:
    data["nodes"][node["id"]] = node
    if node["parent"]:
        parent = data["nodes"].get(node["parent"])
        if parent is None:
            raise TreeError("node_not_found", node["parent"])
        parent.setdefault("children", []).append(node["id"])
        parent["updated_at"] = _now()
    else:
        data["root"] = node["id"]


def _public_node(node: dict) -> dict:
    view = {key: value for key, value in node.items() if key != "fingerprint"}
    view["has_fingerprint"] = bool(node.get("fingerprint"))
    return view


def _view(data: dict) -> dict:
    return {"name": data.get("name"), "scope": scope_of(data), "root": data.get("root"),
            "created_at": data.get("created_at"), "updated_at": data.get("updated_at"),
            "nodes": {key: _public_node(value) for key, value in data.get("nodes", {}).items()}}


def ascii_tree(data: dict, node_id: str | None = None) -> str:
    nodes = data.get("nodes", {})
    start = node_id or data.get("root")
    if start not in nodes:
        raise TreeError("node_not_found", str(start))
    scope = scope_of(data)
    lines = [f"{data.get('name')}  ({scope.get('title_contains') or 'unbound; matches any screen'})"]

    def walk(current: str, prefix: str, last: bool, root: bool) -> None:
        node = nodes[current]
        branch = "" if root else ("└─ " if last else "├─ ")
        extras = []
        if node.get("target"):
            extras.append(f"← {node['target']}")
        if node.get("region"):
            extras.append("region=" + ",".join(str(int(v)) for v in node["region"]))
        stamp = str(node.get("updated_at") or "")[5:16].replace("T", " ")
        extras.append(f"[{stamp}]")
        lines.append(f"{prefix}{branch}{current} {node.get('title', '')}  {' '.join(extras)}")
        children = [c for c in node.get("children", []) if c in nodes]
        child_prefix = prefix + ("" if root else ("   " if last else "│  "))
        for index, child in enumerate(children):
            walk(child, child_prefix, index == len(children) - 1, False)

    walk(start, "", True, True)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def create(name: str, title: str | None = None, app_title: str | None = None,
           force: bool = False, region=None, hwnd: int | None = None) -> dict:
    clean = slug(name)
    target = path_of(clean)
    if target.exists() and not force:
        raise TreeError("tree_exists", f"{clean}; pass force=true to rebuild it")
    if hwnd:
        result = sysres.window_focus(hwnd=int(hwnd))
        if not result.get("ok"):
            raise TreeError(result.get("error", "window_not_found"), str(hwnd))
        time.sleep(0.2)
    elif app_title:
        try:
            sysres.window_focus(title=app_title)
            time.sleep(0.2)
        except Exception:
            pass
    hwnd = int(hwnd) if hwnd else winenv.foreground_hwnd()
    bounds = _parse_region(region)
    state, img = _snapshot_state("tree-root")
    data = {
        "version": 1,
        "name": clean,
        "scope": {"title_contains": app_title, "class": winenv.class_name(hwnd),
                  "title": winenv.window_title(hwnd), "hwnd": hwnd, "region": bounds},
        "root": None,
        "created_at": _now(),
        "updated_at": _now(),
        "nodes": {},
    }
    node = _node_from_shot(data, None, title or "主界面", None, bounds, state, img)
    _attach(data, node)
    save(data)
    return {"ok": True, "tree": clean, "scope": data["scope"], "root": node["id"],
            "node": _public_node(node), "view": ascii_tree(data), "path": str(target)}


def add(name: str | None, parent: str, title: str, target: str | None = None,
        region=None, state_id: str | None = None, recapture: bool = True,
        notes: str = "", tags: list[str] | None = None) -> dict:
    data = load(name)
    parent_node = node_of(data, parent)
    if len(data["nodes"]) >= MAX_NODES:
        raise TreeError("tree_full", f"node limit {MAX_NODES}; remove unused nodes first")
    selector = _validate_target(target)
    bounds = _parse_region(region)
    if state_id:
        state, img, _ = _state_shot(state_id)
    elif recapture:
        state, img = _snapshot_state("node")
    else:
        state = cache.load_state(parent_node.get("state_id") or "") or {}
        img = vision.image_of(state)
    node = _node_from_shot(data, parent_node["id"], title, selector, bounds, state, img, notes, tags)
    _attach(data, node)
    save(data)
    return {"ok": True, "tree": data["name"], "node": _public_node(node), "view": ascii_tree(data)}


def update(name: str | None, node_ref: str, title: str | None = None, target: str | None = None,
           region=None, clear_region: bool = False, notes: str | None = None,
           tags: str | None = None, recapture: bool = False,
           state_id: str | None = None) -> dict:
    data = load(name)
    node = node_of(data, node_ref)
    if title is not None:
        node["title"] = title
    if target is not None:
        node["target"] = _validate_target(target)
    if clear_region:
        node["region"] = None
    elif region is not None:
        node["region"] = _parse_region(region)
    if notes is not None:
        node["notes"] = notes
    if tags is not None:
        node["tags"] = [t.strip() for t in str(tags).split(",") if t.strip()]
    if state_id or recapture:
        if state_id:
            state, img, _ = _state_shot(state_id)
        else:
            state, img = _snapshot_state("node")
        offset = list(state.get("captured", {}).get("offset", [0, 0]))
        node["state_id"], node["image"] = state.get("state_id"), state.get("image")
        node["fingerprint"] = fingerprint(img, offset, node.get("region"))
    elif node.get("image") and Path(node["image"]).is_file():
        # region may have changed; the fingerprint must follow the new crop
        offset = list((cache.load_state(node.get("state_id") or "") or {}).get("captured", {}).get("offset", [0, 0]))
        node["fingerprint"] = fingerprint(Image.open(node["image"]).convert("RGB"), offset, node.get("region"))
    node["updated_at"] = _now()
    save(data)
    return {"ok": True, "tree": data["name"], "node": _public_node(node), "view": ascii_tree(data)}


def remove(name: str | None, node_ref: str, recursive: bool = False) -> dict:
    data = load(name)
    node = node_of(data, node_ref)
    if node["id"] == data.get("root"):
        raise TreeError("cannot_remove_root", "delete the whole tree instead")
    children = [c for c in node.get("children", []) if c in data["nodes"]]
    if children and not recursive:
        raise TreeError("node_has_children", f"{node['id']} has {children}; pass recursive=true")
    doomed = [node["id"]] + (_descendants(data, node["id"]) if recursive else [])
    parent = data["nodes"].get(node.get("parent"))
    if parent:
        parent["children"] = [c for c in parent.get("children", []) if c not in doomed]
        parent["updated_at"] = _now()
    for key in doomed:
        data["nodes"].pop(key, None)
    save(data)
    return {"ok": True, "tree": data["name"], "removed": doomed, "view": ascii_tree(data)}


def move(name: str | None, node_ref: str, parent_ref: str) -> dict:
    data = load(name)
    node = node_of(data, node_ref)
    parent = node_of(data, parent_ref)
    if node["id"] == parent["id"] or parent["id"] in _descendants(data, node["id"]):
        raise TreeError("cycle", "cannot move a node under itself or its descendant")
    old = data["nodes"].get(node.get("parent"))
    if old:
        old["children"] = [c for c in old.get("children", []) if c != node["id"]]
    node["parent"] = parent["id"]
    parent.setdefault("children", []).append(node["id"])
    node["updated_at"] = parent["updated_at"] = _now()
    save(data)
    return {"ok": True, "tree": data["name"], "node": node["id"], "parent": parent["id"],
            "view": ascii_tree(data)}


def show(name: str | None, node_ref: str | None = None) -> dict:
    data = load(name)
    node = find_node(data, node_ref) if node_ref else None
    if node_ref and node is None:
        raise TreeError("node_not_found", node_ref)
    return {"ok": True, "tree": data["name"], "scope": scope_of(data),
            "node": _public_node(node) if node else None,
            "nodes": {key: _public_node(value) for key, value in data["nodes"].items()},
            "view": ascii_tree(data, node["id"] if node else None)}


def delete_tree(name: str) -> dict:
    path = path_of(name)
    if not path.is_file():
        raise TreeError("tree_not_found", name)
    path.unlink()
    return {"ok": True, "deleted": str(path)}


# ---------------------------------------------------------------------------
# Recognition, diff, navigation
# ---------------------------------------------------------------------------

def match(name: str | None = None, threshold: float = MATCH_THRESHOLD) -> dict:
    data = load(name)
    img, offset = _grab()
    ranked = []
    for node in data["nodes"].values():
        if not node.get("fingerprint"):
            continue
        ranked.append({"id": node["id"], "title": node.get("title"),
                       "score": _node_score(node, img, offset)})
    ranked.sort(key=lambda item: item["score"], reverse=True)
    top = ranked[:3]
    return {"ok": True, "tree": data["name"], "best": top[0] if top else None, "top": top,
            "confident": bool(top and top[0]["score"] >= threshold), "threshold": threshold,
            "suggested_action": None if (top and top[0]["score"] >= threshold)
            else "no_confident_match; use cu_tree(action=show) or update the node snapshot"}


def diff_node(name: str | None, node_ref: str, refresh: bool = False,
              diff_keyword: str | None = None, max_regions: int = 8) -> dict:
    data = load(name)
    node = node_of(data, node_ref)
    if not node.get("image") or not Path(node["image"]).is_file():
        raise TreeError("ref_image_missing", f"{node['id']} has no snapshot; update with recapture=true")
    reference = Image.open(node["image"]).convert("RGB")
    ref_offset = list((cache.load_state(node.get("state_id") or "") or {}).get("captured", {}).get("offset", [0, 0]))
    state, current = _snapshot_state("tree-diff")
    offset = list(state.get("captured", {}).get("offset", [0, 0]))
    result = diff.compare(current, reference, after_offset=offset, before_offset=ref_offset,
                          label=node["id"], keyword=diff_keyword, max_regions=max_regions)
    result["compared_to"] = {"ref": f"node:{data['name']}/{node['id']}", "image": node.get("image"),
                             "size": list(reference.size)}
    if refresh:
        node["state_id"], node["image"] = state.get("state_id"), state.get("image")
        node["fingerprint"] = fingerprint(current, offset, node.get("region"))
        node["updated_at"] = _now()
        save(data)
    return {"ok": True, "tree": data["name"], "node": _public_node(node), "refreshed": bool(refresh),
            "diff": result}


def suggest(name: str | None, node_ref: str, max_items: int = 5) -> dict:
    data = load(name)
    node = node_of(data, node_ref)
    if not node.get("image") or not Path(node["image"]).is_file():
        raise TreeError("ref_image_missing", f"{node['id']} has no snapshot")
    reference = Image.open(node["image"]).convert("RGB")
    ref_offset = list((cache.load_state(node.get("state_id") or "") or {}).get("captured", {}).get("offset", [0, 0]))
    state, current = _snapshot_state("tree-suggest")
    offset = list(state.get("captured", {}).get("offset", [0, 0]))
    result = diff.compare(current, reference, after_offset=offset, before_offset=ref_offset,
                          label=f"suggest-{node['id']}", max_regions=max_items)
    candidates = []
    for region in result["regions"]:
        texts = [t for t in region.get("texts", []) if t]
        usable = [t for t in texts if len(str(t).strip()) >= 2] or texts
        first = usable[0] if usable else None
        gx, gy, gx2, gy2 = region["global"]
        candidates.append({
            "title": first or f"区域 {region['index']}",
            "region": [gx, gy, gx2 - gx, gy2 - gy],
            "texts": texts,
            "suggested_target": f"text:{first}" if first else None,
            "box": region["box"],
            "score": region["score"],
            "image": region.get("image"),
            "persisted": False,
        })
    return {"ok": True, "tree": data["name"], "node": node["id"], "state_id": state.get("state_id"),
            "image": state.get("image"), "changed": result["changed"], "candidates": candidates,
            "diff": {k: result[k] for k in ("changed", "size_match", "changed_area", "others", "annotated_image")}}


def _live_scope_hwnd(scope: dict) -> int | None:
    """Recorded hwnd if it is still alive, else the best title match, else None."""
    try:
        hwnd = int(scope.get("hwnd") or 0)
    except (TypeError, ValueError):
        hwnd = 0
    if hwnd and any(int(w["hwnd"]) == hwnd for w in sysres.windows_list()):
        return hwnd
    needle = scope.get("title_contains")
    if needle:
        match = sysres._match_window(title=needle)
        if match:
            return int(match["hwnd"])
    return None


def goto(name: str | None, node_ref: str, allow_risky: bool = False) -> dict:
    data = load(name)
    target = node_of(data, node_ref)
    chain = path_to(data, target["id"])
    if len(chain) > MAX_DEPTH:
        raise TreeError("step_failed", f"path is {len(chain)} levels; max {MAX_DEPTH}")
    scope = scope_of(data)
    _focus_scope(scope)
    time.sleep(0.15)
    # Resolve targets against the tree's own window so a look-alike foreground
    # window cannot capture the click. Regional trees (no hwnd) stay unscoped.
    scope_hwnd = _live_scope_hwnd(scope)
    steps = []
    for index in range(1, len(chain)):
        child = data["nodes"][chain[index]]
        selector = child.get("target")
        if not selector:
            raise TreeError("step_failed",
                            f"{child['id']} has no target; cu_tree(action=update, node={child['id']}, target=text:...)")
        kind, _, value = str(selector).partition(":")
        kind = kind.strip().lower()
        if kind not in _SELECTOR_KINDS:
            raise TreeError("bad_selector", f"unsupported target {selector!r}")
        risky = bool(RISKY.search(f"{selector} {child.get('title', '')}"))
        if kind == "key" and value.strip().lower().replace(" ", "") in RISKY_COMBOS:
            risky = True
        if risky and not allow_risky:
            return {"ok": False, "error": "risky_requires_confirmation", "target_text": selector,
                    "steps": steps, "hint": "the user must agree, then repeat with allow_risky=true"}
        if kind == "text":
            state_id = vision.see(max_elements=1, hwnd=scope_hwnd,
                                  include_screen=False)["state_id"]
            clicked = actions.click(text=value, state_id=state_id, hwnd=scope_hwnd,
                                    allow_risky=allow_risky)
        elif kind == "template":
            clicked = actions.click(template=value, hwnd=scope_hwnd, allow_risky=allow_risky)
        elif kind == "key":
            clicked = actions.key_combo(value)
        else:
            x_text, y_text = value.split(",")[:2]
            clicked = actions.click(x=int(x_text), y=int(y_text), hwnd=scope_hwnd,
                                    allow_risky=allow_risky)
        if not clicked.get("ok"):
            return {"ok": False, "error": "step_failed", "steps": steps, "target_text": selector,
                    "detail": clicked.get("error"), "hint": clicked.get("hint")}
        time.sleep(0.35)
        img, offset = _grab()
        hit = _node_score(child, img, offset)
        steps.append({"node": child["id"], "target": selector, "resolved": clicked.get("resolved"),
                      "changed": clicked.get("changed"), "score": hit})
        if hit < MATCH_THRESHOLD:
            return {"ok": False, "error": "step_failed", "steps": steps, "target_text": selector,
                    "state_id": clicked.get("post_state_id"), "image": clicked.get("post_image"),
                    "hint": f"screen does not look like {child['id']} ({hit:.2f} < {MATCH_THRESHOLD}); "
                            f"cu_tree(action=update, node={child['id']}, recapture=true) after navigating by hand"}
    return {"ok": True, "tree": data["name"], "node": target["id"], "path": chain, "steps": steps}


def protect_state_ids() -> set[str]:
    protected: set[str] = set()
    try:
        names = [item["name"] for item in list_trees()]
    except Exception:
        return protected
    for name in names:
        try:
            data = load(name)
        except Exception:
            continue
        for node in data.get("nodes", {}).values():
            if node.get("state_id"):
                protected.add(node["state_id"])
    return protected

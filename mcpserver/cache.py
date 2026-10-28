"""Project-scoped state cache under ``<cwd>/.computer-use``.

State JSON files stay the source of truth; ``index.json`` is a small metadata
index (no element lists) so listing/filtering does not re-parse every state.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import threading
import time
from collections import OrderedDict
from pathlib import Path

CACHE_ENV = "COMPUTER_USE_CACHE_DIR"
MAX_STATES = 50
MAX_AGE_SECONDS = 24 * 3600
AUTO_PRUNE_AT = MAX_STATES + 10
INDEX_VERSION = 1
INDEX_NAME = "index.json"
LOG_MAX_BYTES = 2 * 1024 * 1024

_MEMORY: "OrderedDict[str, dict]" = OrderedDict()
_MEMORY_LIMIT = 20
_LOCK = threading.RLock()

_INDEX: dict[str, dict] | None = None
_INDEX_KEY: tuple[str, tuple[int, int] | None, tuple[int, int] | None] | None = None
_ROOT_MEMO: tuple[tuple[str, str], Path] | None = None
_DIRS_MEMO: tuple[Path, dict[str, Path]] | None = None


def _ensure_writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:
        return False


def root() -> Path:
    global _ROOT_MEMO
    key = (str(Path.cwd()), os.environ.get(CACHE_ENV) or "")
    with _LOCK:
        if _ROOT_MEMO is not None and _ROOT_MEMO[0] == key:
            return _ROOT_MEMO[1]
    override = os.environ.get(CACHE_ENV)
    if override:
        path = Path(override).expanduser()
    else:
        path = Path.cwd() / ".computer-use"
    if _ensure_writable(path):
        chosen = path
    else:
        chosen = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "computer-use"
        chosen.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        _ROOT_MEMO = (key, chosen)
    return chosen


def dirs() -> dict[str, Path]:
    global _DIRS_MEMO
    base = root()
    with _LOCK:
        if _DIRS_MEMO is not None and _DIRS_MEMO[0] == base and base.is_dir():
            return dict(_DIRS_MEMO[1])
    result = {
        "root": base,
        "shots": base / "shots",
        "states": base / "states",
        "templates": base / "templates",
        "debug": base / "debug",
    }
    for key, path in result.items():
        if key != "root":
            path.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        _DIRS_MEMO = (base, dict(result))
    return dict(result)


def new_state_id() -> str:
    folder = dirs()["states"]
    prefix = time.strftime("%Y%m%d-%H%M%S")
    with _LOCK:
        for _ in range(100):
            candidate = f"{prefix}-{secrets.token_hex(2)}"
            if not (folder / f"{candidate}.json").exists():
                return candidate
    return f"{prefix}-{secrets.token_hex(8)}"


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------

def _index_path() -> Path:
    return root() / INDEX_NAME


def _signature(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
        return (stat.st_mtime_ns, stat.st_size)
    except OSError:
        return None


def _states_signature() -> tuple[int, int] | None:
    """Directory identity: changes when a state file is added or removed."""
    try:
        stat = dirs()["states"].stat()
        return (stat.st_mtime_ns, stat.st_ctime_ns)
    except OSError:
        return None


def _index_state_key(path: Path) -> tuple[str, tuple[int, int] | None, tuple[int, int] | None]:
    return (str(path), _signature(path), _states_signature())


def state_window_ids(state: dict) -> set[int]:
    """Every hwnd a state can be attributed to (target, popup scope, popup owner)."""
    ids: set[int] = set()
    for key in ("window", "scope_hwnd", "window_target"):
        try:
            value = int(state.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value:
            ids.add(value)
    captured = state.get("captured") or {}
    for key in ("target_hwnd", "owner_hwnd"):
        try:
            value = int(captured.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value:
            ids.add(value)
    popup = captured.get("popup") or {}
    for key in ("hwnd", "owner_hwnd", "target_hwnd"):
        try:
            value = int(popup.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value:
            ids.add(value)
    return ids


def _state_meta(state: dict, path: Path) -> dict:
    try:
        mtime_ns = path.stat().st_mtime_ns
    except OSError:
        mtime_ns = 0
    try:
        created = float(state.get("created_at") or 0.0)
    except (TypeError, ValueError):
        created = 0.0
    elements = state.get("elements") or []
    return {
        "state_id": path.stem,
        "image": state.get("image"),
        "created_at": state.get("created_at"),
        "created_ts": created,
        "elements": len(elements),
        "has_elements": bool(elements),
        "window_ids": sorted(state_window_ids(state)),
        "mtime_ns": mtime_ns,
    }


def _sort_key(meta: dict) -> tuple[float, int, str]:
    return (float(meta.get("created_ts") or 0.0), int(meta.get("mtime_ns") or 0),
            str(meta.get("state_id") or ""))


def _read_state_file(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _read_index_file() -> dict[str, dict] | None:
    path = _index_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict) or payload.get("version") != INDEX_VERSION:
        return None
    states = payload.get("states")
    if not isinstance(states, dict):
        return None
    clean: dict[str, dict] = {}
    for state_id, meta in states.items():
        if not isinstance(state_id, str) or not isinstance(meta, dict):
            return None
        item = dict(meta)
        item["state_id"] = state_id
        clean[state_id] = item
    return clean


def _write_index_atomic(index: dict[str, dict]) -> None:
    path = _index_path()
    tmp = path.with_name(f".{INDEX_NAME}.{os.getpid()}.{secrets.token_hex(3)}.tmp")
    payload = {"version": INDEX_VERSION, "updated_at": time.time(), "states": index}
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _persist_index(index: dict[str, dict]) -> None:
    global _INDEX, _INDEX_KEY
    _INDEX = index
    try:
        _write_index_atomic(index)
        _INDEX_KEY = _index_state_key(_index_path())
    except OSError:
        # The index is an optimization; state files remain the source of truth.
        _INDEX_KEY = None


def _rebuild_index() -> dict[str, dict]:
    folder = dirs()["states"]
    index: dict[str, dict] = {}
    try:
        entries = sorted(os.scandir(folder), key=lambda item: item.name)
    except OSError:
        entries = []
    for entry in entries:
        if not entry.is_file() or not entry.name.endswith(".json"):
            continue
        path = Path(entry.path)
        state = _read_state_file(path)
        if state is None:
            continue
        index[path.stem] = _state_meta(state, path)
    _persist_index(index)
    return index


def _reconcile_files(index: dict[str, dict]) -> tuple[dict[str, dict], bool]:
    folder = dirs()["states"]
    try:
        with os.scandir(folder) as entries:
            names = {entry.name[:-5] for entry in entries
                     if entry.is_file() and entry.name.endswith(".json")}
    except OSError:
        return index, False
    changed = False
    for state_id in list(index):
        if state_id not in names:
            index.pop(state_id, None)
            changed = True
    indexed = set(index)
    for state_id in sorted(names - indexed):
        path = folder / f"{state_id}.json"
        state = _read_state_file(path)
        if state is None:
            continue
        index[state_id] = _state_meta(state, path)
        changed = True
    return index, changed


def _load_index() -> dict[str, dict]:
    global _INDEX, _INDEX_KEY
    path = _index_path()
    key = _index_state_key(path)
    with _LOCK:
        if _INDEX is not None and _INDEX_KEY == key:
            return _INDEX
        loaded = _read_index_file()
        if loaded is None:
            return _rebuild_index()
        loaded, changed = _reconcile_files(loaded)
        if changed:
            _persist_index(loaded)
        else:
            _INDEX = loaded
            _INDEX_KEY = _index_state_key(path)
        return loaded


def _sorted_metas() -> list[dict]:
    return sorted(_load_index().values(), key=_sort_key, reverse=True)


def _meta_age(meta: dict, now: float) -> float:
    created = float(meta.get("created_ts") or 0.0)
    if created:
        return max(0.0, now - created)
    mtime_ns = int(meta.get("mtime_ns") or 0)
    return max(0.0, now - mtime_ns / 1e9) if mtime_ns else 0.0


# ---------------------------------------------------------------------------
# State files
# ---------------------------------------------------------------------------

def save_state(state: dict) -> str:
    state_id = state["state_id"]
    d = dirs()
    path = d["states"] / f"{state_id}.json"
    tmp = path.with_name(path.name + ".tmp")
    payload = json.dumps(state, ensure_ascii=False)
    with _LOCK:
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)
        _MEMORY[state_id] = state
        while len(_MEMORY) > _MEMORY_LIMIT:
            _MEMORY.popitem(last=False)
        index = _load_index()
        index[state_id] = _state_meta(state, path)
        _persist_index(index)
        if len(index) > AUTO_PRUNE_AT:
            try:
                prune(_extra_protected={state_id})
            except Exception:
                pass
    return state_id


def load_state(state_id: str) -> dict | None:
    with _LOCK:
        if state_id in _MEMORY:
            _MEMORY.move_to_end(state_id)
            return _MEMORY[state_id]
    path = dirs()["states"] / f"{state_id}.json"
    if not path.exists():
        return None
    state = _read_state_file(path)
    if state is None:
        return None
    with _LOCK:
        _MEMORY[state_id] = state
        while len(_MEMORY) > _MEMORY_LIMIT:
            _MEMORY.popitem(last=False)
    return state


def latest_state() -> dict | None:
    states = list_states()
    return load_state(states[0]["state_id"]) if states else None


def latest_state_with_elements() -> dict | None:
    """Newest state that actually carries element data (post-click snapshots do not)."""
    for meta in _sorted_metas():
        if not meta.get("has_elements"):
            continue
        state = load_state(meta["state_id"])
        if state and state.get("elements"):
            return state
    return None


def states_for_window(hwnd: int, with_elements: bool = False) -> list[dict]:
    """Cached states attributed to ``hwnd``, newest first."""
    target = int(hwnd)
    result: list[dict] = []
    for meta in _sorted_metas():
        if with_elements and not meta.get("has_elements"):
            continue
        ids = meta.get("window_ids") or []
        if target not in ids:
            continue
        state = load_state(meta["state_id"])
        if not state or (with_elements and not state.get("elements")):
            continue
        if target in state_window_ids(state):
            result.append(state)
    return result


def list_states() -> list[dict]:
    now = time.time()
    result = []
    for meta in _sorted_metas():
        result.append(
            {
                "state_id": meta["state_id"],
                "image": meta.get("image"),
                "created_at": meta.get("created_at"),
                "age_seconds": round(_meta_age(meta, now), 1),
                "elements": int(meta.get("elements") or 0),
            }
        )
    return result


def query(text: str | None = None, regex: str | None = None, source: str | None = None,
          state_id: str | None = None, since: float | None = None, limit: int = 50) -> list[dict]:
    pattern = re.compile(regex, re.IGNORECASE) if regex else None
    needle = text.lower() if text else None
    now = time.time()
    results: list[dict] = []
    for meta in _sorted_metas():
        if state_id and meta["state_id"] != state_id:
            continue
        if since is not None and _meta_age(meta, now) > since:
            continue
        state = load_state(meta["state_id"])
        if not state:
            continue
        for element in state.get("elements", []):
            if source and element.get("source") != source:
                continue
            label = str(element.get("text") or element.get("name") or "")
            if needle and needle not in label.lower():
                continue
            if pattern and not pattern.search(label):
                continue
            results.append(
                {
                    "state_id": state["state_id"],
                    "image": state.get("image"),
                    "age_seconds": round(now - float(state.get("created_at", now)), 1),
                    "element": element,
                }
            )
            if len(results) >= limit:
                return results
    return results


def _is_within(path: Path, base: Path) -> bool:
    try:
        path.resolve().relative_to(base.resolve())
        return True
    except (ValueError, OSError):
        return False


def _delete_state_files(meta: dict) -> None:
    d = dirs()
    state_id = meta["state_id"]
    try:
        (d["states"] / f"{state_id}.json").unlink(missing_ok=True)
    except OSError:
        pass
    image = meta.get("image")
    if image:
        try:
            target = Path(image)
            if target.is_file() and _is_within(target, d["root"]):
                target.unlink()
        except OSError:
            pass
    # annotations, diff crops and node snapshots share the state id prefix
    try:
        for derivative in d["shots"].glob(f"{state_id}*"):
            try:
                if derivative.is_file():
                    derivative.unlink()
            except OSError:
                pass
    except OSError:
        pass
    with _LOCK:
        _MEMORY.pop(state_id, None)


def prune(max_states: int = MAX_STATES, max_age_seconds: int = MAX_AGE_SECONDS,
          _extra_protected: set[str] | None = None) -> dict:
    d = dirs()
    try:
        import tree  # lazy: tree imports cache at module level
        protected = set(tree.protect_state_ids())
    except Exception:
        # Never delete snapshots we cannot prove are unreferenced.
        return {"removed": 0, "kept": len(_sorted_metas()), "protected": 0,
                "root": str(d["root"]), "skipped": "tree_protection_unavailable"}
    protected |= set(_extra_protected or ())
    now = time.time()
    with _LOCK:
        index = _load_index()
        kept = 0
        removed = 0
        for meta in _sorted_metas():
            state_id = meta["state_id"]
            if state_id in protected:
                kept += 1
                continue
            too_old = _meta_age(meta, now) > max_age_seconds
            too_many = kept >= max_states
            if too_old or too_many:
                _delete_state_files(meta)
                index.pop(state_id, None)
                removed += 1
            else:
                kept += 1
        if removed:
            _persist_index(index)
    return {"removed": removed, "kept": kept, "protected": len(protected), "root": str(d["root"])}


def clear_all() -> dict:
    d = dirs()
    for key in ("shots", "states", "debug"):
        shutil.rmtree(d[key], ignore_errors=True)
        d[key].mkdir(parents=True, exist_ok=True)
    try:
        (d["root"] / INDEX_NAME).unlink(missing_ok=True)
    except OSError:
        pass
    global _INDEX, _INDEX_KEY, _ROOT_MEMO, _DIRS_MEMO
    with _LOCK:
        _MEMORY.clear()
        _INDEX = None
        _INDEX_KEY = None
        _ROOT_MEMO = None
        _DIRS_MEMO = None
    return {"cleared": str(d["root"])}


def audit(entry: dict) -> None:
    path = dirs()["root"] / "actions.log"
    entry = dict(entry)
    entry.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%S"))
    try:
        if path.exists() and path.stat().st_size >= LOG_MAX_BYTES:
            try:
                os.replace(path, path.with_name("actions.log.1"))
            except OSError:
                pass
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass

"""Action primitives: click, move, type, key, press/release, scroll, drag, wait."""
from __future__ import annotations

import re
import time

import numpy as np

import cache
import capture
import keys
import locate
import recognize
import sysres
import vision
import winenv

RISKY_WORDS = re.compile(r"提交|投递|购买|支付|删除|确认|卸载|发送|退出|回收站")
STALE_SECONDS = 8.0
CHANGE_THRESHOLD = 1.5
CHANGE_FRACTION = 0.008
LOCAL_MEAN = 0.4
LOCAL_FRACTION = 0.003


def _changed(metrics: dict) -> bool:
    return metrics["mean"] > CHANGE_THRESHOLD or metrics["fraction"] >= CHANGE_FRACTION


def _local_changed(metrics: dict | None) -> bool:
    return bool(metrics and (metrics["mean"] > LOCAL_MEAN or metrics["fraction"] >= LOCAL_FRACTION))


class ActionError(Exception):
    def __init__(self, code: str, hint: str = ""):
        super().__init__(code)
        self.code = code
        self.hint = hint


def parse_offset(text: str | None) -> list[int]:
    if not text:
        return [0, 0]
    values = [int(v) for v in re.findall(r"-?\d+", str(text))]
    if len(values) != 2:
        raise ActionError("bad_offset", "offset must be 'dx,dy'")
    return values


def parse_xy(text: str) -> list[int]:
    values = [int(v) for v in re.findall(r"-?\d+", str(text))]
    if len(values) != 2:
        raise ActionError("bad_xy", "coordinates must be 'x,y'")
    return values


def parse_selector(selector: str) -> tuple[str, str]:
    if ":" not in str(selector):
        raise ActionError("bad_selector", "use id:12, text:提交, template:name or xy:100,200")
    kind, value = str(selector).split(":", 1)
    kind = kind.strip().lower()
    if kind not in ("id", "text", "template", "xy"):
        raise ActionError("bad_selector", f"unknown selector kind: {kind}")
    return kind, value


def _age(state: dict) -> float:
    return time.time() - float(state.get("created_at", 0))


def _fresh_state(**see_kwargs) -> dict:
    # Internal re-shoots only need the image/元素, never the screen companion.
    see_kwargs.setdefault("include_screen", False)
    result = vision.see(**see_kwargs)
    state = cache.load_state(result["state_id"])
    if state is None:
        raise ActionError("see_failed", "could not cache the screenshot")
    return state


def _window_scope(window: str | None, hwnd: int | None) -> dict | None:
    """Resolve a title/hwnd into a scope descriptor; None when no scope was asked for."""
    if window is None and hwnd is None:
        return None
    match = sysres._match_window(window, hwnd)
    if match is None:
        label = f"hwnd {hwnd}" if hwnd else repr(window)
        raise ActionError("window_not_found", f"no visible window for {label}; use cu_window list")
    if match.get("minimized"):
        raise ActionError("window_minimized",
                          f"'{match['title']}' is minimized; restore it first "
                          "(cu_window action=focus or state=restore)")
    return {"hwnd": match["hwnd"], "title": match["title"], "class": match.get("class")}


def _select_state(state_id: str | None = None, need_elements: bool = True,
                  max_age: float = STALE_SECONDS, allow_stale: bool = False,
                  window: str | None = None, hwnd: int | None = None,
                  target_kind: str | None = None) -> tuple[dict, dict]:
    """Pick the state an action resolves against, optionally scoped to one window.

    Returns (state, meta); meta carries state_id / scope / state_refreshed for the
    reply so callers can confirm which screenshot was used.
    """
    scope = _window_scope(window, hwnd)
    if state_id:
        state = cache.load_state(state_id)
        if state is None:
            raise ActionError("unknown_state", f"state {state_id} is gone; call cu_see again")
        if scope and scope["hwnd"] not in cache.state_window_ids(state):
            raise ActionError("state_window_mismatch",
                              f"state {state_id} was not captured from '{scope['title']}'")
        if need_elements and not state.get("elements"):
            raise ActionError("state_has_no_elements", "that state came from fast/post capture; call cu_see")
        if need_elements and not allow_stale and _age(state) > max_age:
            raise ActionError("stale_state", f"{_age(state):.1f}s old; call cu_see again or pass force_stale=true")
        return state, {"state_id": state["state_id"], "scope": scope, "state_refreshed": False}

    if scope is None:
        if need_elements:
            state = cache.latest_state_with_elements()
            if state is None:
                state = _fresh_state()
            # Never silently re-shoot: ids belong to one screenshot, and remapping
            # them would make the caller click a different element than it saw.
            elif not allow_stale and _age(state) > max_age:
                raise ActionError("stale_state",
                                  f"latest state is {_age(state):.1f}s old and ids may have changed; "
                                  "call cu_see again, or pass force_stale=true to reuse those ids")
        else:
            state = cache.latest_state()
            if state is None or _age(state) > 2.0:
                state = _fresh_state(ocr=False, fast=True)
        return state, {"state_id": state["state_id"], "scope": None, "state_refreshed": False}

    if target_kind == "xy":
        state = cache.latest_state()
        if state is None or _age(state) > 2.0:
            state = _fresh_state(hwnd=scope["hwnd"], ocr=False, fast=True)
        return state, {"state_id": state["state_id"], "scope": scope, "state_refreshed": False}

    wants_elements = target_kind in ("id", "text")
    candidates = cache.states_for_window(scope["hwnd"], with_elements=wants_elements)
    newest = candidates[0] if candidates else None
    if newest is not None and (_age(newest) <= max_age or allow_stale):
        return newest, {"state_id": newest["state_id"], "scope": scope, "state_refreshed": False}
    if target_kind == "id":
        if newest is None:
            raise ActionError("no_state_for_window",
                              f"no cached state for '{scope['title']}'; call cu_see(window=...) first")
        raise ActionError("stale_state",
                          f"'{scope['title']}' state is {_age(newest):.1f}s old and ids may have changed; "
                          "call cu_see again, or pass force_stale=true")
    # text/template targets can safely be re-shot: they are resolved against content.
    state = _fresh_state(hwnd=scope["hwnd"], include_screen=False)
    if wants_elements and not state.get("elements"):
        raise ActionError("capture_failed", f"could not read elements from '{scope['title']}'")
    return state, {"state_id": state["state_id"], "scope": scope, "state_refreshed": True}


def _find_element(state: dict, element_id: int) -> dict:
    for element in state.get("elements", []):
        if int(element.get("id", -1)) == int(element_id):
            return element
    raise ActionError("unknown_id", f"id {element_id} is not in state {state.get('state_id')}")


def _on_screen(element: dict) -> bool:
    box = element.get("box") or []
    if len(box) != 4:
        return False
    return recognize._on_screen([int(v) for v in box])


def _find_text(state: dict, needle: str, nth: int = 1) -> dict:
    matched = [e for e in state.get("elements", []) if needle.lower() in str(e.get("text", "")).lower()]
    if not matched:
        raise ActionError("text_not_found", f"no element containing {needle!r}")
    # Offscreen UIA nodes often carry placeholder coordinates (-32000 etc.);
    # a real on-screen match should win, but keep them as a last resort.
    matched.sort(key=lambda e: 0 if _on_screen(e) else 1)
    if nth < 1 or nth > len(matched):
        raise ActionError("bad_nth", f"only {len(matched)} matches")
    return matched[nth - 1]


def resolve(state: dict, *, element_id=None, text=None, xy=None, template=None,
            nth: int = 1, threshold: float = 0.87, region=None) -> dict:
    if element_id is not None:
        element = _find_element(state, element_id)
        return {"point": list(element["center"]), "text": element.get("text", ""), "element": element,
                "source": "id", "hwnd": element.get("hwnd") or state.get("window")}
    if text is not None:
        element = _find_text(state, text, nth)
        return {"point": list(element["center"]), "text": element.get("text", ""), "element": element,
                "source": "text", "hwnd": element.get("hwnd") or state.get("window")}
    if xy is not None:
        point = parse_xy(xy) if isinstance(xy, str) else [int(xy[0]), int(xy[1])]
        return {"point": point, "text": "", "element": None, "source": "xy", "hwnd": state.get("window")}
    if template is not None:
        image = vision.image_of(state)
        offset = list(state.get("captured", {}).get("offset", [0, 0]))
        try:
            found = locate.locate(image, template, region=region, threshold=threshold, offset=offset)
        except FileNotFoundError as exc:
            raise ActionError("template_not_found", str(exc))
        except locate.TemplateNotFound as exc:
            raise ActionError("template_not_found", str(exc))
        match = found["matches"][min(max(nth - 1, 0), len(found["matches"]) - 1)]
        return {"point": list(match["global"]), "text": "", "element": None, "source": "template",
                "match": match, "hwnd": state.get("window")}
    raise ActionError("missing_target", "give id, text, xy or template")


def _risky_texts(state: dict, resolved: dict) -> list[str]:
    texts = []
    if resolved.get("text"):
        texts.append(resolved["text"])
    element = resolved.get("element")
    if element and element.get("text"):
        texts.append(element["text"])
    x, y = resolved["point"]
    texts.extend(recognize.texts_near(state.get("elements", []), x, y, radius=18))
    return [t for t in texts if t]


def _check_risky(state: dict, resolved: dict, allow_risky: bool) -> str | None:
    hits = [t for t in _risky_texts(state, resolved) if RISKY_WORDS.search(t)]
    if hits and not allow_risky:
        return hits[0]
    return None


def _resolved(base: list[int], offset: list[int], window_offset=(0, 0)) -> dict:
    return {
        "base": base,
        "offset": offset,
        "window_offset": list(window_offset),
        "final": [base[0] + offset[0] + window_offset[0], base[1] + offset[1] + window_offset[1]],
    }


def _before_capture() -> dict:
    """Fresh full-screen grab; the same rectangle is recaptured after the action."""
    img, meta = capture.grab_monitor("auto")
    return {"img": img, "meta": meta, "fp": capture.fingerprint(img),
            "windows": sysres.window_signature()}


def _new_popup_detail(before: dict) -> list[dict]:
    """Visible windows that appeared since the pre-action snapshot (menus, dialogs, panels)."""
    fresh = sysres.new_windows(before.get("windows"))
    # Cap the report: a burst of windows is not useful as a list.
    return fresh[:6]


def _local_metrics(before_img, after_img, point, meta: dict, radius: int = 200) -> dict | None:
    ox, oy = meta.get("offset", [0, 0])
    lx, ly = int(point[0] - ox), int(point[1] - oy)
    x1, y1 = max(0, lx - radius), max(0, ly - radius)
    x2 = min(before_img.width, lx + radius)
    y2 = min(before_img.height, ly + radius)
    if x2 <= x1 or y2 <= y1 or after_img.size != before_img.size:
        return None
    a = np.asarray(before_img.crop((x1, y1, x2, y2)).convert("L"), dtype=np.int16)
    b = np.asarray(after_img.crop((x1, y1, x2, y2)).convert("L"), dtype=np.int16)
    delta = np.abs(a - b)
    return {"mean": round(float(delta.mean()), 2), "fraction": round(float((delta > 8).mean()), 4)}


def _after_check(before: dict, point=None) -> dict:
    """Recapture and decide whether the action visibly changed anything.

    Global thumbnails miss small controls, so when a target point is known the
    comparison is repeated on a full-resolution crop around that point.
    """
    time.sleep(0.2)
    meta = before["meta"]
    offset, size = meta.get("offset", [0, 0]), meta.get("size", [0, 0])
    img, _ = capture.grab_rect([offset[0], offset[1], offset[0] + size[0], offset[1] + size[1]])
    metrics = capture.change_metrics(before["fp"], capture.fingerprint(img))
    local = _local_metrics(before["img"], img, point, meta) if point else None
    if not (_changed(metrics) or _local_changed(local)):
        # some UIs repaint slowly; one retry before declaring "no change"
        time.sleep(0.3)
        retry, _ = capture.grab_rect([offset[0], offset[1], offset[0] + size[0], offset[1] + size[1]])
        retry_metrics = capture.change_metrics(before["fp"], capture.fingerprint(retry))
        retry_local = _local_metrics(before["img"], retry, point, meta) if point else None
        if _changed(retry_metrics) or _local_changed(retry_local):
            img, metrics, local = retry, retry_metrics, retry_local
    changed = _changed(metrics) or _local_changed(local)
    source = "pixels" if changed else None
    # Reuse the image we already compared instead of grabbing the screen again.
    post = vision.snapshot("post", image=img, meta=meta)
    result = {"changed": changed, "change_score": metrics["mean"], "change_ratio": metrics["fraction"],
              "post_state_id": post["state_id"], "post_image": post["image"]}
    if local:
        result["change_local"] = local
    if not changed:
        # A menu or dialog drawn in its own window can stay invisible to the pixel
        # diff (slow first paint, or it lands outside the compared crop). Window
        # enumeration is far cheaper than another screenshot, so use it as a
        # second, independent signal.
        fresh = _new_popup_detail(before)
        if fresh:
            result.update({"changed": True, "change_source": "window",
                           "new_windows": fresh})
    if source:
        result["change_source"] = source
    return result


def _safe_verify_after_click(before: dict, point=None) -> dict:
    """The action already happened; a failed verification must not undo that result."""
    try:
        return _after_check(before, point)
    except Exception as exc:
        return {
            "changed": None,
            "verification_failed": True,
            "verify_error": f"{type(exc).__name__}: {exc}",
            "hint": "the action was performed but the post-action check failed; "
                    "confirm the result with cu_see",
        }


def _finish(tool: str, payload: dict, started: float | None = None) -> dict:
    keys.cleanup()
    held = keys.held_state()
    if held["held_keys"]:
        payload.update(held)
    auto = keys.take_auto_released()
    if auto:
        payload["auto_released"] = auto
    record = {
        "tool": tool,
        "ok": payload.get("ok"),
        "error": payload.get("error"),
        "target": payload.get("target"),
        "resolved": payload.get("resolved") or payload.get("at") or payload.get("to"),
        "state_id": payload.get("state_id"),
        "changed": payload.get("changed"),
    }
    if started is not None:
        record["elapsed_ms"] = int((time.time() - started) * 1000)
    cache.audit(record)
    return payload


def click(element_id=None, text=None, xy=None, template=None, offset: str | None = None,
          button: str = "left", count: int = 1, nth: int = 1, threshold: float = 0.87,
          region=None, modifiers: str | None = None, dry_run: bool = False,
          allow_risky: bool = False, state_id: str | None = None, force_stale: bool = False,
          window: str | None = None, hwnd: int | None = None) -> dict:
    started = time.time()
    kind = ("id" if element_id is not None else "text" if text is not None
            else "template" if template is not None else "xy")
    state, info = _select_state(state_id, need_elements=kind in ("id", "text"),
                                allow_stale=force_stale, window=window, hwnd=hwnd,
                                target_kind=kind)
    resolved = resolve(state, element_id=element_id, text=text, xy=xy, template=template,
                       nth=nth, threshold=threshold, region=region)
    shift = parse_offset(offset)
    final = _resolved(resolved["point"], shift)
    hit = _check_risky(state, resolved, allow_risky)
    payload = {"ok": True, "resolved": final, "target": {"source": resolved["source"],
               "text": resolved.get("text", ""), "button": button, "count": count}, **info}
    if hit:
        return _finish("click", {"ok": False, "error": "risky_requires_confirmation", "target_text": hit,
                                 "resolved": final, **info,
                                 "hint": "repeat with allow_risky=true after the user agrees"}, started)
    if dry_run:
        payload["dry_run"] = True
        return _finish("click", payload, started)

    before = _before_capture()
    winenv.move_to(*final["final"])
    time.sleep(0.03)
    pressed = []
    try:
        for mod in [m for m in (modifiers or "").split(",") if m.strip()]:
            winenv.key_down(mod.strip())
            pressed.append(mod.strip())
        winenv.click_at(final["final"][0], final["final"][1], button=button, count=count)
    finally:
        for mod in reversed(pressed):
            try:
                winenv.key_up(mod)
            except Exception:
                pass
    payload.update(_safe_verify_after_click(before, final["final"]))
    return _finish("click", payload, started)


def _glide(x: int, y: int, duration: float = 0.25, settle: float = 0.0) -> None:
    start = winenv.cursor_pos()
    steps = max(1, int(duration / 0.02))
    for index in range(1, steps + 1):
        xi = int(start[0] + (x - start[0]) * index / steps)
        yi = int(start[1] + (y - start[1]) * index / steps)
        winenv.move_to(xi, yi)
        time.sleep(max(0.0, duration / steps))
    if settle:
        time.sleep(max(0.0, settle))


def move(to: str, offset: str | None = None, duration: float = 0.25, settle: float = 0.15,
         state_id: str | None = None, window: str | None = None,
         hwnd: int | None = None) -> dict:
    started = time.time()
    kind, value = parse_selector(to)
    state, info = _select_state(state_id, need_elements=(kind in ("id", "text")),
                                window=window, hwnd=hwnd, target_kind=kind)
    resolved = resolve(state, element_id=value if kind == "id" else None,
                       text=value if kind == "text" else None,
                       xy=value if kind == "xy" else None,
                       template=value if kind == "template" else None)
    final = _resolved(resolved["point"], parse_offset(offset))
    _glide(final["final"][0], final["final"][1], duration=duration, settle=settle)
    hovered = recognize.texts_near(state.get("elements", []), final["final"][0], final["final"][1], radius=20)
    return _finish("move", {"ok": True, "resolved": final, "hover_texts": hovered[:8], **info}, started)


def _focus_scoped(scope: dict) -> dict | None:
    """Bring a scoped window (or its foreground popup) to the front.

    Returns an error payload when focusing failed, None on success. Keyboard input
    follows the foreground window, so every keyboard tool routes through here.
    """
    focus_hwnd = scope["hwnd"]
    cached = cache.states_for_window(scope["hwnd"])
    if cached and cached[0].get("popup"):
        focus_hwnd = cached[0]["popup"]["hwnd"]
    if winenv.foreground_hwnd() == focus_hwnd:
        return None
    focused = sysres.window_focus(hwnd=focus_hwnd)
    if not focused.get("ok"):
        return {"error": "focus_failed", "hint": f"could not focus '{scope['title']}'"}
    return None


def type_text(text: str, into_id: int | None = None, method: str = "auto", clear: bool = False,
              enter: bool = False, state_id: str | None = None, window: str | None = None,
              hwnd: int | None = None) -> dict:
    started = time.time()
    state = None
    resolved = None
    info: dict = {"state_id": None, "scope": None, "state_refreshed": False}
    if into_id is not None:
        state, info = _select_state(state_id, need_elements=True, window=window, hwnd=hwnd,
                                    target_kind="id")
        resolved = resolve(state, element_id=into_id)
        element = resolved.get("element") or {}
        label = str(element.get("text") or "").lower()
        if element.get("password") or "密码" in label or "password" in label:
            return _finish("type", {"ok": False, "error": "password_field",
                                    "hint": "typing into password fields is refused"}, started)
        winenv.click_at(*resolved["point"])
        time.sleep(0.08)
    elif window is not None or hwnd is not None:
        scope = _window_scope(window, hwnd)
        info = {"state_id": state_id, "scope": scope, "state_refreshed": False}
        failure = _focus_scoped(scope)
        if failure:
            return _finish("type", {"ok": False, **failure, **info}, started)

    if clear:
        winenv.combo("ctrl+a")
        time.sleep(0.03)

    use_clipboard = method == "clipboard" or (method == "auto" and len(text) > 200)
    if use_clipboard:
        sysres.clipboard_set(text)
        time.sleep(0.03)
        winenv.combo("ctrl+v")
    else:
        winenv.type_unicode(text)
    if enter:
        winenv.tap("enter")
    return _finish("type", {"ok": True, "length": len(text), "method": "clipboard" if use_clipboard else "sendinput",
                            "into": resolved["point"] if resolved else None, **info}, started)


def key_combo(combo: str, window: str | None = None, hwnd: int | None = None) -> dict:
    started = time.time()
    scope = _window_scope(window, hwnd)
    info = {"scope": scope}
    if scope:
        failure = _focus_scoped(scope)
        if failure:
            return _finish("key", {"ok": False, **failure, **info}, started)
    winenv.combo(combo)
    return _finish("key", {"ok": True, "combo": combo, **info}, started)


def press(key: str, timeout: float = 10.0) -> dict:
    started = time.time()
    state = keys.press(key, timeout)
    return _finish("press", {"ok": True, "key": key, "timeout": timeout, **state}, started)


def release(key: str | None = None) -> dict:
    started = time.time()
    state = keys.release(key)
    return _finish("release", {"ok": True, **state}, started)


def scroll(dy: int, dx: int = 0, at: str | None = None, to: str | None = None,
           state_id: str | None = None, window: str | None = None,
           hwnd: int | None = None) -> dict:
    started = time.time()
    point = None
    info = {"state_id": state_id, "scope": None, "state_refreshed": False}
    if to:
        kind, value = parse_selector(to)
        state, info = _select_state(state_id, need_elements=(kind in ("id", "text")),
                                    window=window, hwnd=hwnd, target_kind=kind)
        resolved = resolve(state, element_id=value if kind == "id" else None,
                           text=value if kind == "text" else None,
                           xy=value if kind == "xy" else None,
                           template=value if kind == "template" else None)
        point = resolved["point"]
    elif at:
        point = parse_xy(at)
    if window is not None or hwnd is not None:
        # Scroll follows the cursor; the scope is still validated so a typo in the
        # window name fails loudly instead of scrolling the wrong app.
        info["scope"] = _window_scope(window, hwnd)
    if point:
        winenv.scroll_at(point[0], point[1], int(dy), int(dx))
    else:
        x, y = winenv.cursor_pos()
        winenv.scroll_at(x, y, int(dy), int(dx))
    return _finish("scroll", {"ok": True, "dy": int(dy), "dx": int(dx),
                              "at": point or list(winenv.cursor_pos()), **info}, started)


def drag(from_selector: str, to_selector: str, from_offset: str | None = None,
         to_offset: str | None = None, button: str = "left", steps: int = 20,
         duration: float = 0.6, hold_before_release: float = 0.2,
         modifiers: str | None = None, dry_run: bool = False, allow_risky: bool = False,
         state_id: str | None = None, window: str | None = None,
         hwnd: int | None = None) -> dict:
    started = time.time()
    from_kind, from_value = parse_selector(from_selector)
    to_kind, to_value = parse_selector(to_selector)
    kinds = {from_kind, to_kind}
    target_kind = ("id" if "id" in kinds else "text" if "text" in kinds
                   else "template" if "template" in kinds else "xy")
    state, info = _select_state(state_id, need_elements=bool(kinds & {"id", "text"}),
                                window=window, hwnd=hwnd, target_kind=target_kind)
    start = resolve(state, element_id=from_value if from_kind == "id" else None,
                    text=from_value if from_kind == "text" else None,
                    xy=from_value if from_kind == "xy" else None,
                    template=from_value if from_kind == "template" else None)
    end = resolve(state, element_id=to_value if to_kind == "id" else None,
                  text=to_value if to_kind == "text" else None,
                  xy=to_value if to_kind == "xy" else None,
                  template=to_value if to_kind == "template" else None)
    from_final = _resolved(start["point"], parse_offset(from_offset))
    to_final = _resolved(end["point"], parse_offset(to_offset))
    hit = _check_risky(state, end, allow_risky)
    payload = {"ok": True, "from": from_final, "to": to_final, **info}
    if hit:
        return _finish("drag", {"ok": False, "error": "risky_requires_confirmation", "target_text": hit,
                                "from": from_final, "to": to_final, **info,
                                "hint": "repeat with allow_risky=true after the user agrees"}, started)
    if dry_run:
        payload["dry_run"] = True
        return _finish("drag", payload, started)

    before = _before_capture()
    pressed = []
    try:
        for mod in [m for m in (modifiers or "").split(",") if m.strip()]:
            winenv.key_down(mod.strip())
            pressed.append(mod.strip())
        winenv.move_to(*from_final["final"])
        time.sleep(0.08)
        winenv.mouse_down(button)
        time.sleep(0.12)
        count = max(2, int(steps))
        for index in range(1, count + 1):
            x = int(from_final["final"][0] + (to_final["final"][0] - from_final["final"][0]) * index / count)
            y = int(from_final["final"][1] + (to_final["final"][1] - from_final["final"][1]) * index / count)
            winenv.move_to(x, y)
            time.sleep(max(0.0, duration / count))
        time.sleep(max(0.0, hold_before_release))
        winenv.mouse_up(button)
    finally:
        try:
            winenv.mouse_up(button)
        except Exception:
            pass
        for mod in reversed(pressed):
            try:
                winenv.key_up(mod)
            except Exception:
                pass
    payload.update(_safe_verify_after_click(before, to_final["final"]))
    return _finish("drag", payload, started)


def hover(element_id=None, text=None, xy=None, template=None, offset: str | None = None,
          nth: int = 1, threshold: float = 0.87, region=None, window: str | None = None,
          hwnd: int | None = None, settle: float = 0.8, state_id: str | None = None,
          force_stale: bool = False) -> dict:
    """Move onto a target, then read whatever tooltip appears. Never clicks or types."""
    started = time.time()
    kind = ("id" if element_id is not None else "text" if text is not None
            else "template" if template is not None else "xy")
    state, info = _select_state(state_id, need_elements=kind in ("id", "text"),
                                allow_stale=force_stale, window=window, hwnd=hwnd,
                                target_kind=kind)
    resolved = resolve(state, element_id=element_id, text=text, xy=xy, template=template,
                       nth=nth, threshold=threshold, region=region)
    final = _resolved(resolved["point"], parse_offset(offset))
    x, y = final["final"]
    before_hwnds = {w["hwnd"] for w in sysres.windows_all()}
    _glide(x, y, duration=0.25)
    hovered = recognize.texts_near(state.get("elements", []), x, y, radius=20)
    element = resolved.get("element") or {}
    tooltip = recognize.read_tooltip(
        [x, y], before_hwnds, settle=settle,
        element_help=str(element.get("help") or ""),
        known_texts=[t for t in [resolved.get("text", ""), *hovered] if t],
    )
    payload = {"ok": True, "resolved": final, "hover_texts": hovered[:8], **info, **tooltip}
    payload["elapsed"] = round(time.time() - started, 2)
    return _finish("hover", payload, started)


_EDGE_TOKENS = ("left", "right", "top", "bottom")


def _resize_edges(anchor: str, edge: str | None, has_dx: bool,
                  has_dy: bool) -> tuple[str | None, str | None]:
    """Which window edges a drag should grab, and reject contradictory requests."""
    anchor_tokens = set(str(anchor or "top-left").strip().lower().split("-"))
    if edge:
        tokens = [t for t in str(edge).strip().lower().split("-") if t]
        if not tokens or any(t not in _EDGE_TOKENS for t in tokens):
            raise ActionError("bad_edge", "use left|right|top|bottom, e.g. bottom-right")
        ex = next((t for t in tokens if t in ("left", "right")), None)
        ey = next((t for t in tokens if t in ("top", "bottom")), None)
        # Dragging an edge the anchor pins cannot change anything.
        if (ex and ex in anchor_tokens) or (ey and ey in anchor_tokens):
            raise ActionError("anchor_edge_conflict",
                              f"anchor={anchor} pins that edge; drag the opposite one or omit edge")
    else:
        ex = "right" if "left" in anchor_tokens else "left" if "right" in anchor_tokens else "right"
        ey = "bottom" if "top" in anchor_tokens else "top" if "bottom" in anchor_tokens else "bottom"
    if not has_dx:
        ex = None
    if not has_dy:
        ey = None
    if ex is None and ey is None:
        raise ActionError("bad_size", "width/height unchanged; nothing to drag")
    return ex, ey


def _snap_edges(point: list[int], monitor: dict | None) -> list[str]:
    """Monitor edges the drag endpoint would land within Aero Snap range of."""
    if not monitor:
        return []
    x, y = int(point[0]), int(point[1])
    sx1, sy1, sx2, sy2 = monitor["bounds"]
    edges: list[str] = []
    if x <= sx1 + 16:
        edges.append("left")
    if x >= sx2 - 17:
        edges.append("right")
    if y <= sy1 + 16:
        edges.append("top")
    if y >= sy2 - 17:
        edges.append("bottom")
    return edges


def resize_drag(title: str | None = None, hwnd: int | None = None,
                width: int | None = None, height: int | None = None,
                anchor: str = "top-left", edge: str | None = None, steps: int = 12,
                duration: float = 0.35, restore: bool = True, dry_run: bool = False,
                allow_snap: bool = False) -> dict:
    """Resize by grabbing an edge/corner with the mouse (fallback for SetWindowPos)."""
    started = time.time()
    match = sysres._match_window(title, hwnd)
    if match is None:
        return {"ok": False, "error": "window_not_found"}
    target = match["hwnd"]
    if not dry_run and not restore and (winenv.is_zoomed(target) or winenv.is_minimized(target)):
        return {"ok": False, "error": "restore_failed",
                "hint": "window is maximized or minimized; pass restore=true"}
    restored = sysres._restore_for_resize(target, dry_run=dry_run)
    before = list(restored["rect"])
    plan = sysres.resize_plan(before, width, height, anchor)
    if not plan["ok"]:
        return plan
    requested = list(plan["rect"])
    dx, dy = requested[2] - before[2], requested[3] - before[3]
    ex, ey = _resize_edges(anchor, edge, dx != 0, dy != 0)

    # What the drag should produce: only the grabbed edges move.
    expected = list(before)
    if ex == "right":
        expected[2] = requested[2]
    elif ex == "left":
        expected[0] = requested[0]
    if ey == "bottom":
        expected[3] = requested[3]
    elif ey == "top":
        expected[1] = requested[1]

    border = winenv.resize_border_metrics(target)
    inset = {side: max(2, int(border.get(side, 8)) // 2) for side in _EDGE_TOKENS}
    bx1, by1, bx2, by2 = before
    gx = (bx2 - inset["right"]) if ex == "right" else (bx1 + inset["left"]) if ex == "left" else (bx1 + bx2) // 2
    gy = (by2 - inset["bottom"]) if ey == "bottom" else (by1 + inset["top"]) if ey == "top" else (by1 + by2) // 2
    mx = (requested[2] - bx2) if ex == "right" else (requested[0] - bx1) if ex == "left" else 0
    my = (requested[3] - by2) if ey == "bottom" else (requested[1] - by1) if ey == "top" else 0
    end = [gx + mx, gy + my]

    monitor = winenv.monitor_for_rect(before)
    snap_edges = _snap_edges(end, monitor)

    fixed_x = "left" if ex == "right" else "right" if ex == "left" else ""
    fixed_y = "top" if ey == "bottom" else "bottom" if ey == "top" else ""
    anchor_used = "-".join(t for t in (fixed_y, fixed_x) if t)
    base = {
        "action": "resize", "hwnd": target, "title": winenv.window_title(target),
        "before": before, "requested": requested, "expected": expected,
        "requested_size": plan["requested"], "anchor": plan["anchor"],
        "anchor_used": anchor_used or None, "edge": "-".join(t for t in (ex, ey) if t),
        "grab": [gx, gy], "end": end, "mode_used": "drag", "method": "drag",
        "restored_from": restored["from"], "clamped": plan["clamped"],
        "aero_snap_risk": bool(snap_edges), "snap_edges": snap_edges,
    }
    if anchor != (anchor_used or anchor):
        base["hint"] = "drag resizes only the grabbed edges; use mode=api for a centered resize"
    would_snap = bool(snap_edges) and not allow_snap
    if dry_run:
        return {"ok": True, "dry_run": True, "would_refuse": would_snap, **base}
    if would_snap:
        # Refuse before touching the mouse: Aero Snap would resize to a half screen.
        return _finish("resize", {
            "ok": False, "error": "aero_snap_risk", **base,
            "hint": "the drag would end within 16px of a monitor edge and may be snapped; "
                    "use mode=api, resize to a smaller target, or pass allow_snap=true",
        }, started)

    winenv.move_to(gx, gy)
    time.sleep(0.1)
    winenv.mouse_down("left")
    try:
        time.sleep(0.08)
        count = max(2, int(steps))
        for index in range(1, count + 1):
            winenv.move_to(gx + int((end[0] - gx) * index / count),
                           gy + int((end[1] - gy) * index / count))
            time.sleep(max(0.0, duration / count))
        time.sleep(0.15)
    finally:
        winenv.mouse_up("left")
    time.sleep(0.25)
    after = winenv.window_rect(target)
    verified = all(abs(after[i] - expected[i]) <= 12 for i in range(4))
    changed = after != before
    size_before = (before[2] - before[0], before[3] - before[1])
    size_after = (after[2] - after[0], after[3] - after[1])
    payload = {**base, "after": after, "verified": verified, "changed": changed,
               "clamped": base["clamped"] or (size_after != size_before and not verified)}
    if not changed:
        payload.update({"ok": False, "error": "resize_failed",
                        "hint": "the drag left the window untouched; the grab point may be "
                                "inside the client area or the window has a fixed size"})
    elif size_after == size_before:
        payload.update({"ok": False, "error": "moved_instead_of_resized",
                        "hint": "only the position changed; grab nearer the border"})
    else:
        payload["ok"] = True
    return _finish("resize", payload, started)


def _text_on_screen(needle: str) -> dict | None:
    """Look for text via UIA first (cheap), then OCR of the foreground window."""
    hwnd = winenv.foreground_hwnd()
    elements, _ = recognize.uia_elements(hwnd, timeout=1.5)
    for element in elements:
        if needle.lower() in str(element.get("text", "")).lower():
            return element
    rect = winenv.window_rect(hwnd)
    if rect[2] > rect[0] and rect[3] > rect[1]:
        img, _ = capture.grab_rect(rect)
        for element in recognize.ocr_elements(img, [rect[0], rect[1]]):
            if needle.lower() in str(element.get("text", "")).lower():
                return element
    return None


def wait(for_text: str | None = None, window: str | None = None, template: str | None = None,
         gone: str | None = None, change_from: str | None = None,
         timeout: float = 10.0, interval: float = 0.3) -> dict:
    started = time.time()
    deadline = started + max(0.1, timeout)
    baseline = None
    if change_from:
        state = cache.load_state(change_from)
        if state is None:
            return {"ok": False, "error": "unknown_state", "hint": change_from}
        baseline = capture.fingerprint(vision.image_of(state))
    sessions = 0

    def elapsed() -> float:
        return round(time.time() - started, 2)

    while time.time() < deadline:
        sessions += 1
        try:
            if window and sysres.windows_list(window):
                return {"ok": True, "matched": "window", "waited": elapsed()}
            if gone and not sysres.windows_list(gone):
                return {"ok": True, "matched": "gone", "waited": elapsed()}
            if for_text:
                element = _text_on_screen(for_text)
                if element:
                    return {"ok": True, "matched": "text", "element": element, "waited": elapsed()}
            if template:
                img, meta = capture.grab_monitor("auto")
                try:
                    found = locate.locate(img, template, offset=meta.get("offset"))
                    return {"ok": True, "matched": "template", "match": found["matches"][0], "waited": elapsed()}
                except locate.TemplateNotFound:
                    pass
            if baseline is not None:
                img, _ = capture.grab_monitor("auto")
                metrics = capture.change_metrics(baseline, capture.fingerprint(img))
                if _changed(metrics):
                    return {"ok": True, "matched": "change", "change_score": metrics["mean"],
                            "change_ratio": metrics["fraction"], "waited": elapsed()}
        except Exception:
            pass
        time.sleep(min(max(0.05, interval), max(0.0, deadline - time.time())))
    return {"ok": False, "error": "timeout", "sessions": sessions, "waited": elapsed()}

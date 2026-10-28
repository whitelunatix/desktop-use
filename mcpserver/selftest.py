"""Internal regression for the computer-use server. Not exposed as an MCP tool.

    python selftest.py            # read-only checks (dry-run clicks only)
    python selftest.py --full     # also opens a temp folder window, resizes it, runs the popup fixture
    python selftest.py --ui       # optional: open the test page in Edge and hover an icon
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent))

import actions
import cache
import capture
import crop
import diff
import doctor
import initmeta
import locate
import recognize
import sysres
import tree
import vision
import winenv

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((PASS if ok else FAIL, name, detail))
    print(f"[{PASS if ok else FAIL}] {name}" + (f"  {detail}" if detail else ""))


def skip(name: str, detail: str = "") -> None:
    results.append((SKIP, name, detail))
    print(f"[{SKIP}] {name}" + (f"  {detail}" if detail else ""))


def _rect_size(rect: list[int]) -> list[int]:
    return [rect[2] - rect[0], rect[3] - rect[1]]


def _rect_close(a: list[int], b: list[int], tol: int = 2) -> bool:
    return all(abs(int(a[i]) - int(b[i])) <= tol for i in range(4))


def _pick_other_window(exclude: set[int], screen: dict) -> dict | None:
    shell_classes = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}
    for item in sysres.windows_list():
        if item["hwnd"] in exclude or item["minimized"] or item.get("class") in shell_classes:
            continue
        rect = item["rect"]
        if (rect[2] - rect[0] >= 120 and rect[3] - rect[1] >= 80
                and rect[0] >= 0 and rect[1] >= 0
                and rect[2] <= screen["width"] and rect[3] <= screen["height"]):
            return item
    return None


def _synthetic_checks() -> None:
    """Deterministic cache/capture checks in an isolated temporary cache directory."""
    from PIL import Image

    tmp = tempfile.mkdtemp(prefix="cu-selftest-cache-")
    old_env = os.environ.get(cache.CACHE_ENV)
    os.environ[cache.CACHE_ENV] = tmp
    index_path = Path(tmp) / cache.INDEX_NAME
    states_dir = Path(tmp) / "states"

    def state(state_id: str, created: float, elements: list | None = None) -> dict:
        return {"state_id": state_id, "created_at": created, "image": None,
                "elements": elements or [], "captured": {}}

    def indexed_ids() -> set[str]:
        return {row["state_id"] for row in cache.list_states()}

    try:
        cache.clear_all()
        cache.save_state(state("s-0001", time.time() - 100))
        check("cache index is written", index_path.is_file(), str(index_path))
        rows = cache.list_states()
        check("cache list returns indexed metadata",
              [row["state_id"] for row in rows] == ["s-0001"] and rows[0]["elements"] == 0,
              str(rows))

        index_path.write_text("{not json", encoding="utf-8")
        check("corrupt index is rebuilt", "s-0001" in indexed_ids())
        try:
            json.loads(index_path.read_text(encoding="utf-8"))
            valid = True
        except Exception:
            valid = False
        check("rebuilt index is valid JSON", valid)

        index_path.unlink()
        check("missing index is rebuilt", "s-0001" in indexed_ids() and index_path.is_file())

        (states_dir / "s-0001.json").unlink()
        check("deleted state files leave the index", "s-0001" not in indexed_ids())

        cache.clear_all()
        ids = set()
        for _ in range(30):
            state_id = cache.new_state_id()
            ids.add(state_id)
            cache.save_state(state(state_id, time.time()))
        check("same-second state ids stay unique",
              len(ids) == 30 and len(list(states_dir.glob("*.json"))) == 30,
              f"{len(ids)} ids / {len(list(states_dir.glob('*.json')))} files")

        cache.clear_all()
        base = time.time()
        saved = []
        for i in range(cache.AUTO_PRUNE_AT + 1):
            state_id = f"p-{i:04d}"
            cache.save_state(state(state_id, base + i))
            saved.append(state_id)
        rows = cache.list_states()
        check("auto prune trims to MAX_STATES at the high-water mark",
              len(rows) == cache.MAX_STATES, f"{len(rows)} states")
        check("auto prune keeps the newest state", rows and rows[0]["state_id"] == saved[-1],
              rows[0]["state_id"] if rows else "none")

        cache.clear_all()
        for i in range(3):
            cache.save_state(state(f"t-{i}", base + i))
        keep_id = "t-0"
        original_protect = tree.protect_state_ids
        tree.protect_state_ids = lambda: {keep_id}
        try:
            pruned = cache.prune(max_states=1)
        finally:
            tree.protect_state_ids = original_protect
        remaining = indexed_ids()
        check("prune keeps tree-referenced states",
              keep_id in remaining and "t-1" not in remaining and len(remaining) == 2,
              f"removed={pruned['removed']} remaining={sorted(remaining)}")

        def _no_protection() -> set[str]:
            raise RuntimeError("tree store unavailable")

        tree.protect_state_ids = _no_protection
        try:
            skipped = cache.prune(max_states=0)
        finally:
            tree.protect_state_ids = original_protect
        check("prune skips when tree protection is unavailable",
              skipped.get("removed") == 0
              and skipped.get("skipped") == "tree_protection_unavailable", str(skipped))

        image = Image.new("RGB", (64, 48), (10, 20, 30))
        original_grab_monitor = capture.grab_monitor
        capture.grab_monitor = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no grab"))
        try:
            shot = vision.snapshot("post", image=image, meta={"offset": [0, 0], "size": [64, 48]})
        finally:
            capture.grab_monitor = original_grab_monitor
        check("snapshot reuses the supplied image",
              Path(shot["image"]).is_file() and shot["fingerprint"].shape == (24, 32),
              shot["image"])

        before = {"img": Image.new("RGB", (64, 48), (1, 2, 3)),
                  "meta": {"offset": [0, 0], "size": [64, 48]}}
        before["fp"] = capture.fingerprint(before["img"])
        original_grab_rect = capture.grab_rect
        capture.grab_rect = lambda rect: (_ for _ in ()).throw(RuntimeError("no capture"))
        try:
            failed = actions._safe_verify_after_click(before, [10, 10])
        finally:
            capture.grab_rect = original_grab_rect
        check("post-action verification failure is isolated",
              failed.get("changed") is None and failed.get("verification_failed") is True
              and "verify_error" in failed, str(failed.get("verify_error")))

        screen_meta = {"capture_mode": "screen"}
        wide = Image.new("RGB", (200, 150), (5, 6, 7))
        cropped = vision._reuse_screen_image(wide, screen_meta, [0, 0, 200, 150], [20, 20, 120, 120])
        partial = vision._reuse_screen_image(wide, screen_meta, [0, 0, 80, 80], [20, 20, 120, 120])
        printwindow = vision._reuse_screen_image(wide, {"capture_mode": "printwindow"},
                                                 [0, 0, 200, 150], [20, 20, 120, 120])
        check("screen image is reused only when the window covers the monitor",
              cropped is not None and cropped.size == (100, 100)
              and cropped.getpixel((0, 0)) == (5, 6, 7)
              and partial is None and printwindow is None,
              f"cropped={None if cropped is None else cropped.size}")

        calls = {"count": 0}
        original_screen = winenv.virtual_screen

        def counting_screen() -> dict:
            calls["count"] += 1
            return original_screen()

        winenv.virtual_screen = counting_screen
        try:
            merged = recognize.merge(
                [{"source": "uia", "type": "Button", "text": "on-screen",
                  "box": [0, 0, 10, 10], "center": [5, 5]}],
                [{"source": "ocr", "type": "text", "text": "off-screen",
                  "box": [3000, 3000, 3010, 3010], "center": [3005, 3005]}],
            )
        finally:
            winenv.virtual_screen = original_screen
        check("merge reads the virtual screen once",
              calls["count"] == 1 and merged[0]["text"] == "on-screen",
              f"{calls['count']} calls")

        cache.clear_all()
        original_bytes = cache.LOG_MAX_BYTES
        cache.LOG_MAX_BYTES = 64
        try:
            for i in range(6):
                cache.audit({"tool": "selftest", "n": i, "pad": "x" * 40})
        finally:
            cache.LOG_MAX_BYTES = original_bytes
        check("audit log rotates by size",
              (Path(tmp) / "actions.log").is_file()
              and (Path(tmp) / "actions.log.1").is_file(),
              str(sorted(p.name for p in Path(tmp).glob("actions.log*"))))

        error = None
        try:
            vision.see(window="__cu_missing_window__")
        except vision.CaptureError as exc:
            error = exc
        check("unknown window title raises window_not_found",
              error is not None and error.code == "window_not_found", str(error))

        error = None
        try:
            vision._grab_target(0xDEADBEEF, "screen")
        except vision.CaptureError as exc:
            error = exc
        check("vanished window raises window_gone",
              error is not None and error.code == "window_gone", str(error))

        fg = winenv.foreground_hwnd()
        if fg:
            original_minimized = winenv.is_minimized
            winenv.is_minimized = lambda _hwnd: True
            error = None
            try:
                vision._grab_target(fg, "screen")
            except vision.CaptureError as exc:
                error = exc
            finally:
                winenv.is_minimized = original_minimized
            check("minimized window raises window_minimized before capture",
                  error is not None and error.code == "window_minimized", str(error))
        else:
            skip("minimized window error", "no foreground window")

        try:
            import server
            payload = json.loads(server._guard(lambda: vision.see(window="__cu_missing_window__")))
            check("server maps capture errors",
                  payload.get("error") == "window_not_found", str(payload))
        except Exception as exc:
            skip("server maps capture errors", f"{type(exc).__name__}: {exc}")

        signature = sysres.window_signature()
        shaped = bool(signature) and all(
            set(info) == {"title", "class", "rect"}
            and info["rect"][2] > info["rect"][0] and info["rect"][3] > info["rect"][1]
            for info in signature.values())
        check("window signature lists visible window rectangles", shaped, f"{len(signature)} windows")

        extra = dict(signature)
        extra[-1] = {"title": "", "class": "#32768", "rect": [1, 2, 30, 40]}
        fresh = sysres.new_windows(signature, extra)
        check("new windows diff reports only fresh handles",
              len(fresh) == 1 and fresh[0]["hwnd"] == -1 and fresh[0]["class"] == "#32768"
              and sysres.new_windows(signature, dict(signature)) == []
              and sysres.new_windows(None) == [], str(fresh))

        error = None
        try:
            actions.key_combo("enter", window="__cu_missing_window__")
        except actions.ActionError as exc:
            error = exc
        check("key combo rejects an unknown window scope",
              error is not None and error.code == "window_not_found", str(error))

        origin, candidate = 0x1001, 0x1002
        patched = ("is_window_visible", "is_minimized", "window_owner", "window_title",
                   "is_popup_window", "class_name", "window_ex_style", "is_window_enabled",
                   "window_rect")
        originals = {name: getattr(winenv, name) for name in patched}
        try:
            winenv.is_window_visible = lambda _h: True
            winenv.is_minimized = lambda _h: False
            winenv.window_owner = lambda _h: origin
            winenv.window_title = lambda _h: ""
            winenv.class_name = lambda _h: "Chrome_WidgetWin_1"
            winenv.is_window_enabled = lambda _h: True
            winenv.window_rect = lambda _h: [0, 0, 400, 300]
            winenv.window_ex_style = lambda _h: winenv.WS_EX_TOOLWINDOW
            winenv.is_popup_window = lambda _h: True
            untitled_popup = sysres._owned_popup_candidate(candidate, origin)
            payload = sysres._popup_payload(candidate, "owned_enum", origin)
            winenv.is_popup_window = lambda _h: False
            untitled_tool = sysres._owned_popup_candidate(candidate, origin)
            winenv.window_ex_style = lambda _h: 0
            untitled_plain = sysres._owned_popup_candidate(candidate, origin)
            winenv.window_title = lambda _h: "captioned"
            titled_plain = sysres._owned_popup_candidate(candidate, origin)
            winenv.window_ex_style = lambda _h: winenv.WS_EX_TOOLWINDOW
            titled_tool = sysres._owned_popup_candidate(candidate, origin)
            winenv.window_owner = lambda _h: 0
            unowned = sysres._owned_popup_candidate(candidate, origin)
        finally:
            for name, value in originals.items():
                setattr(winenv, name, value)
        check("owned popup keeps untitled WS_POPUP windows",
              untitled_popup and payload.get("untitled") is True
              and not untitled_tool and not untitled_plain
              and titled_plain and not titled_tool and not unowned,
              f"untitled popup={untitled_popup} tool={untitled_tool} plain={untitled_plain} "
              f"titled plain={titled_plain} tool={titled_tool} unowned={unowned}")

        fg = winenv.foreground_hwnd()
        if fg:
            focused = sysres.window_focus(hwnd=fg)
            original_states = cache.states_for_window
            cache.states_for_window = lambda _h, **_k: []
            try:
                scoped = actions._focus_scoped({"hwnd": fg, "title": winenv.window_title(fg), "class": ""})
            finally:
                cache.states_for_window = original_states
            check("focusing the foreground window changes nothing",
                  focused.get("ok") is True and focused.get("already_foreground") is True
                  and scoped is None, f"focus={focused.get('error') or focused.get('already_foreground')}")
        else:
            skip("focus no-op on the foreground window", "no foreground window")

        encoding = (getattr(sys.stdout, "encoding", "") or "").lower().replace("-", "")
        check("selftest output is utf-8", encoding == "utf8",
              str(getattr(sys.stdout, "encoding", None)))
    finally:
        try:
            cache.clear_all()
        except Exception:
            pass
        if old_env is None:
            os.environ.pop(cache.CACHE_ENV, None)
        else:
            os.environ[cache.CACHE_ENV] = old_env
        shutil.rmtree(tmp, ignore_errors=True)


def main(full: bool = False, ui: bool = False) -> int:
    started = time.time()

    env = initmeta.init(force=True)
    check("init writes env.json", Path(env["env_path"]).is_file(), env["env_path"])
    check("monitors detected", len(env["monitors"]) >= 1, str([m["size"] for m in env["monitors"]]))

    report = doctor.report(calibrate=True)
    check("capture matches monitor", report["capture"].get("matches_monitor") is True, str(report["capture"].get("size")))
    check("pointer round-trip <= 2px", report["calibration"]["ok"], str(report["calibration"]["deltas"]))

    fast = vision.see(fast=True)
    check("fast see writes image", Path(fast["image"]).is_file(), fast["image"])
    check("fast see has no elements", fast["counts"]["returned"] == 0)

    seen = vision.see(max_elements=40)
    check("see returns elements", len(seen["elements"]) > 0, str(seen["counts"]))
    check("element centers match their boxes",
          all(e["center"] == [(e["box"][0] + e["box"][2]) // 2, (e["box"][1] + e["box"][3]) // 2]
              for e in seen["elements"]))

    screen = winenv.virtual_screen()
    state_obj = cache.load_state(seen["state_id"])
    state_image = vision.image_of(state_obj)
    cap_meta = state_obj.get("captured", {})
    offset = list(cap_meta.get("offset") or [0, 0])
    cap_size = list(cap_meta.get("size") or [state_image.width, state_image.height])
    # element boxes are desktop pixels; the image only covers its captured rect
    on_screen = [e for e in seen["elements"]
                 if e["box"][0] >= offset[0] and e["box"][1] >= offset[1]
                 and e["box"][2] <= offset[0] + cap_size[0]
                 and e["box"][3] <= offset[1] + cap_size[1]]
    if on_screen:
        small = [e for e in on_screen
                 if 100 <= (e["box"][2] - e["box"][0]) * (e["box"][3] - e["box"][1]) <= 20000]
        element = small[0] if small else on_screen[0]
        x1, y1, x2, y2 = element["box"]
        region, _used = crop.crop_image(
            state_image, [x1 - offset[0], y1 - offset[1], x2 - x1, y2 - y1], pad=2)
        probe = cache.dirs()["debug"] / "selftest-template.png"
        region.save(probe, "PNG")
        found = locate.locate(state_image, str(probe), offset=offset)
        hit = found["matches"][0]["global"]
        near = abs(hit[0] - element["center"][0]) <= 6 and abs(hit[1] - element["center"][1]) <= 6
        check("crop -> locate round-trip", near, str(hit))

        dry = actions.click(element_id=element["id"], dry_run=True)
        check("dry-run click resolves element center",
              dry["resolved"]["final"] == [element["center"][0], element["center"][1]], str(dry["resolved"]))

        offset_dry = actions.click(element_id=element["id"], offset="7,-3", dry_run=True)
        check("offset applied to resolved point",
              offset_dry["resolved"]["final"] == [element["center"][0] + 7, element["center"][1] - 3])

        hits = cache.query(text=str(element.get("text") or "")[:2])
        check("query finds cached elements", len(hits) > 0, f"{len(hits)} hits")

    # --- window-target capture: placement / screen companion / overlay ---
    from PIL import Image

    fg = winenv.foreground_hwnd()
    win_shot = vision.see(hwnd=fg, include_screen=True, max_elements=30)
    check("window see offset/size match captured",
          win_shot["offset"] == win_shot["captured"]["offset"] and win_shot["size"] == win_shot["captured"]["size"],
          f"offset={win_shot['offset']} size={win_shot['size']}")
    check("window see reports window_rect",
          isinstance(win_shot.get("window_rect"), list) and len(win_shot["window_rect"]) == 4,
          str(win_shot.get("window_rect")))
    check("windows list is opt-in", "windows" not in win_shot)
    with_windows = vision.see(hwnd=fg, include_windows=True, fast=True)
    check("include_windows=true returns a list",
          isinstance(with_windows.get("windows"), list) and len(with_windows["windows"]) > 0,
          f"{len(with_windows.get('windows') or [])} windows")
    plain = vision.see(hwnd=fg, include_screen=False, fast=True)
    plain_derivatives = list(cache.dirs()["shots"].glob(f"{plain['state_id']}-*"))
    check("include_screen=false writes no companion images",
          plain.get("screen_image") is None and not plain_derivatives, str(plain_derivatives))

    placement = win_shot.get("placement")
    check("placement computed", bool(placement) and bool(placement.get("screen_rect")), str(placement))
    if placement:
        screen_rect = placement["screen_rect"]
        screen_path = win_shot.get("screen_image")
        check("screen image exists", bool(screen_path) and Path(screen_path).is_file(), str(screen_path))
        if screen_path and Path(screen_path).is_file():
            with Image.open(screen_path) as shot_img:
                check("screen image matches screen_rect", list(shot_img.size) == _rect_size(screen_rect),
                      f"{shot_img.size} vs {_rect_size(screen_rect)}")
        win_rect = win_shot["window_rect"]
        check("image_offset_in_screen math",
              placement["image_offset_in_screen"] == [win_rect[0] - screen_rect[0], win_rect[1] - screen_rect[1]],
              str(placement["image_offset_in_screen"]))
        check("screen companion reports its source",
              placement.get("screen_source") in ("primary", "recapture"),
              str(placement.get("screen_source")))

        overlay_path = win_shot.get("overlay_image")
        if placement.get("off_screen"):
            check("overlay skipped for an off-screen window", overlay_path is None)
        else:
            check("overlay image produced", bool(overlay_path) and Path(overlay_path).is_file(), str(overlay_path))
            if overlay_path and Path(overlay_path).is_file():
                with Image.open(overlay_path) as overlay_img:
                    check("overlay is RGBA", overlay_img.mode == "RGBA", overlay_img.mode)
                    alpha = overlay_img.getchannel("A")
                    ix1, iy1 = max(win_rect[0], screen_rect[0]), max(win_rect[1], screen_rect[1])
                    ix2, iy2 = min(win_rect[2], screen_rect[2]), min(win_rect[3], screen_rect[3])
                    if ix2 > ix1 and iy2 > iy1:
                        inside = ((ix1 + ix2) // 2 - screen_rect[0], (iy1 + iy2) // 2 - screen_rect[1])
                        check("overlay window area is opaque", alpha.getpixel(inside) == 255, str(inside))
                        sw, sh = overlay_img.size
                        corners = [(0, 0), (sw - 1, 0), (0, sh - 1), (sw - 1, sh - 1)]
                        outside = next(
                            (p for p in corners
                             if not (ix1 <= p[0] + screen_rect[0] < ix2 and iy1 <= p[1] + screen_rect[1] < iy2)),
                            None,
                        )
                        if outside:
                            check("overlay outside the window is transparent", alpha.getpixel(outside) == 0,
                                  str(outside))
                        else:
                            skip("overlay outside the window is transparent", "window covers the whole monitor")
        composed, _composed_meta = vision._compose_overlay(
            Image.new("RGB", (80, 60), (255, 0, 0)), [20, 10, 100, 70], [0, 0, 100, 100])
        check("overlay composition clips at the screen edge",
              composed is not None and composed.getpixel((30, 20))[3] == 255
              and composed.getpixel((0, 0))[3] == 0)

    # --- window-scoped actions ---
    fg_match = sysres._match_window(hwnd=fg)
    fg_state = cache.load_state(win_shot["state_id"])
    fg_rect = fg_state.get("window_rect") or win_shot["window_rect"]

    def _inside_window(box: list) -> bool:
        # UIA can report rects that spill outside the window frame; only
        # elements really inside it can prove the scoped click stayed put.
        return (len(box) == 4 and fg_rect[0] <= box[0] and fg_rect[1] <= box[1]
                and box[2] <= fg_rect[2] and box[3] <= fg_rect[3])

    fg_element = next((e for e in fg_state.get("elements", [])
                       if e.get("text") and actions._on_screen(e)
                       and _inside_window(e.get("box") or [])), None)
    if fg_match and fg_element:
        scoped = actions.click(text=fg_element["text"], hwnd=fg, dry_run=True, force_stale=True)
        used = cache.load_state(scoped["state_id"])
        used_rect = used.get("window_rect") or win_shot["window_rect"]
        point = scoped["resolved"]["final"]
        check("scoped click selects the window's own state",
              scoped.get("scope", {}).get("hwnd") == fg and fg in cache.state_window_ids(used))
        check("scoped click lands inside the window",
              used_rect[0] <= point[0] <= used_rect[2] and used_rect[1] <= point[1] <= used_rect[3],
              f"{point} in {used_rect}")
    else:
        skip("scoped click", "foreground window has no readable text element")

    other = _pick_other_window({fg}, screen)
    if other:
        other_shot = vision.see(hwnd=other["hwnd"], include_screen=False, max_elements=20)
        other_state = cache.load_state(other_shot["state_id"])
        check("second window state is scoped to it",
              other["hwnd"] in cache.state_window_ids(other_state), other["title"])
        other_element = next((e for e in other_state.get("elements", [])
                              if e.get("text") and actions._on_screen(e)), None)
        if other_element:
            scoped_other = actions.click(text=other_element["text"], hwnd=other["hwnd"],
                                         dry_run=True, force_stale=True)
            check("scoped click stays in the second window",
                  scoped_other.get("scope", {}).get("hwnd") == other["hwnd"]
                  and other["hwnd"] in cache.state_window_ids(cache.load_state(scoped_other["state_id"])))
        else:
            skip("scoped click stays in the second window", "no text element in the second window")
    else:
        skip("second window scoping", "only one usable window on screen")

    # --- hover / tooltip ---
    if fg_element:
        box = fg_element["box"]
        hovered = actions.hover(text=fg_element["text"], hwnd=fg, state_id=win_shot["state_id"],
                                force_stale=True, settle=0.3)
        point = hovered["resolved"]["final"]
        check("hover resolves onto the element",
              box[0] - 8 <= point[0] <= box[2] + 8 and box[1] - 8 <= point[1] <= box[3] + 8, str(point))
        check("hover reports a tooltip source",
              hovered.get("tooltip_source") in ("uia_help", "window", "ocr", "none"),
              str(hovered.get("tooltip_source")))
    else:
        skip("hover", "no element to hover")

    press = actions.press("shift", timeout=0.6)
    held = press["held_keys"]
    time.sleep(1.0)
    released = actions.release()
    check("press registers key", held == ["shift"], str(held))
    check("watchdog auto-releases", released["held_keys"] == [], str(released.get("auto_released")))

    # --- diff engine (synthetic, deterministic) ---
    from PIL import Image, ImageDraw

    base = Image.new("RGB", (400, 300), (255, 255, 255))
    ImageDraw.Draw(base).rectangle([10, 10, 80, 60], fill=(0, 0, 0))
    changed_img = base.copy()
    ImageDraw.Draw(changed_img).rectangle([200, 150, 260, 200], fill=(255, 0, 0))

    same = diff.compare(base, base, after_offset=[100, 50])
    check("diff self-compare is empty", same["changed"] is False and same["regions"] == [])

    delta = diff.compare(changed_img, base, after_offset=[100, 50], before_offset=[100, 50])
    hit = delta["regions"][0] if delta["regions"] else None
    check("diff finds one region", delta["changed"] and len(delta["regions"]) == 1,
          str([r["box"] for r in delta["regions"]]))
    check("diff box contains the change",
          bool(hit) and hit["box"][0] <= 200 and hit["box"][1] <= 150
          and hit["box"][2] >= 260 and hit["box"][3] >= 200
          and (hit["box"][2] - hit["box"][0]) < 400,
          str(hit and hit["box"]))
    check("diff global adds capture offset",
          bool(hit) and hit["global"][0] == 100 + hit["box"][0] and hit["global"][1] == 50 + hit["box"][1])
    check("diff saves region crops",
          bool(hit) and Path(hit["image"]).is_file() and Path(hit["before_image"]).is_file())

    # --- trees ---
    tree_name = f"selftest-{int(time.time())}"
    created = tree.create(tree_name, title="root")
    root_id = created["root"]
    check("tree create", Path(created["path"]).is_file() and root_id in created["view"], created["path"])

    child = tree.add(tree_name, parent=root_id, title="tab A", target="text:选项卡 A", recapture=True)
    child_id = child["node"]["id"]
    grand = tree.add(tree_name, parent=child_id, title="panel", target="xy:100,200", recapture=False)
    grand_id = grand["node"]["id"]
    check("tree add children", grand_id in tree.load(tree_name)["nodes"], child["view"].splitlines()[-1])

    tree.update(tree_name, grand_id, title="panel renamed", notes="note", region="0,120,600,400")
    refreshed = tree.load(tree_name)["nodes"][grand_id]
    check("tree update", refreshed["title"] == "panel renamed" and refreshed["region"] == [0, 120, 600, 400])

    try:
        tree.remove(tree_name, child_id)
        check("tree refuses to drop a parent", False)
    except tree.TreeError as exc:
        check("tree refuses to drop a parent", exc.code == "node_has_children", exc.code)

    tree.move(tree_name, grand_id, root_id)
    check("tree move reparents", tree.load(tree_name)["nodes"][grand_id]["parent"] == root_id)
    tree.remove(tree_name, grand_id)

    matched = tree.match(tree_name)
    check("tree match finds current node", bool(matched["best"]) and matched["best"]["score"] >= 0.85,
          str(matched["top"][:2]))

    node_diff = tree.diff_node(tree_name, root_id)
    check("tree diff runs", "diff" in node_diff and node_diff["diff"]["size_match"] is True)

    protected = tree.protect_state_ids()
    check("tree protects its snapshots", tree.load(tree_name)["nodes"][root_id]["state_id"] in protected)
    tree.delete_tree(tree_name)
    check("tree delete", not Path(tree.path_of(tree_name)).exists())

    check("window list", len(sysres.windows_list()) > 0, f"{len(sysres.windows_list())} windows")
    check("foreground reported", winenv.foreground_hwnd() != 0, winenv.window_title(winenv.foreground_hwnd()))

    if full:
        folder = tempfile.mkdtemp(prefix="cu-selftest-")
        opened = sysres.folder_open(folder)
        time.sleep(1.2)
        found = [f for f in sysres.folder_list() if Path(f["path"]) == Path(folder)]
        check("folder open/list", opened["ok"] and bool(found), folder)
        if found:
            hwnd = found[0]["hwnd"]
            sysres.folder_focus(folder)
            time.sleep(0.4)
            # The shell remembers window sizes across runs; normalize first so the
            # final "restore" is a real change instead of a no-op.
            sysres.window_resize(hwnd=hwnd, width=1100, height=760, anchor="top-left")
            time.sleep(0.2)
            rect0 = winenv.window_rect(hwnd)
            dry = sysres.window_resize(hwnd=hwnd, width=900, height=600, dry_run=True)
            check("resize dry-run leaves the window alone",
                  dry.get("ok") and _rect_close(winenv.window_rect(hwnd), rect0), str(dry.get("requested")))
            api = sysres.window_resize(hwnd=hwnd, width=900, height=600, anchor="top-left")
            check("resize api hits the requested rect",
                  api.get("ok") and _rect_close(api["after"], api["requested"], 2), str(api.get("after")))
            drag = actions.resize_drag(hwnd=hwnd, width=980, height=660, anchor="top-left",
                                       edge="bottom-right", allow_snap=True)
            check("resize drag verified within 12px",
                  drag.get("ok") and drag.get("verified"), f"{drag.get('after')} vs {drag.get('expected')}")
            restored = sysres.window_resize(hwnd=hwnd, width=rect0[2] - rect0[0],
                                            height=rect0[3] - rect0[1], anchor="top-left")
            check("resize restores the original size",
                  restored.get("ok") and _rect_close(restored["after"], rect0, 2),
                  f"after={restored.get('after')} rect0={rect0} before={restored.get('before')}")

            monitor = winenv.monitor_for_rect(rect0)
            if monitor:
                wx1, wy1, wx2, _wy2 = monitor["work_area"]
                sysres.window_move(hwnd=hwnd, rect=[max(wx1, wx2 - 820), wy1 + 40, 820, 500])
                time.sleep(0.3)
                before_refuse = winenv.window_rect(hwnd)
                dry_snap = actions.resize_drag(hwnd=hwnd, width=900, height=500, anchor="top-left",
                                               edge="bottom-right", dry_run=True)
                refused = actions.resize_drag(hwnd=hwnd, width=900, height=500, anchor="top-left",
                                              edge="bottom-right")
                check("snap risk is reported before dragging",
                      dry_snap.get("aero_snap_risk") and dry_snap.get("would_refuse") is True,
                      str(dry_snap.get("snap_edges")))
                check("drag refuses Aero Snap endpoints",
                      refused.get("ok") is False and refused.get("error") == "aero_snap_risk"
                      and _rect_close(winenv.window_rect(hwnd), before_refuse, 2),
                      f"{refused.get('error')} rect={winenv.window_rect(hwnd)}")
                sysres.window_move(hwnd=hwnd, rect=[rect0[0], rect0[1],
                                                    rect0[2] - rect0[0], rect0[3] - rect0[1]])
                time.sleep(0.2)
            else:
                skip("snap refusal", "no monitor for the temp window")
        else:
            skip("resize", "temp folder window not found")
        closed = sysres.folder_close(path=folder)
        check("folder close", closed["ok"], str(len(closed.get("closed", []))))
        shutil.rmtree(folder, ignore_errors=True)

        fixture = Path(__file__).resolve().parent / "tests" / "popup_fixture.py"
        errlog = cache.dirs()["debug"] / "popup-fixture.err"
        with errlog.open("wb") as errf:
            proc = subprocess.Popen([sys.executable, str(fixture), "--dialog", "--passive",
                                     "--seconds", "30"],
                                    stdout=subprocess.DEVNULL, stderr=errf)
        try:
            main_hwnd = None
            deadline = time.time() + 15
            while time.time() < deadline and main_hwnd is None:
                main_hwnd = next((w["hwnd"] for w in sysres.windows_list()
                                  if "CU Popup Fixture Main" in w["title"]), None)
                if main_hwnd is None:
                    time.sleep(0.2)
            time.sleep(0.8)
            if main_hwnd is None:
                detail = errlog.read_text(encoding="utf-8", errors="replace").strip()[-300:]
                check("popup fixture starts", False,
                      f"main window did not appear (exit={proc.poll()}, stderr={detail!r})")
            else:
                dialog_hwnd = next((w["hwnd"] for w in sysres.windows_list()
                                    if "CU Popup Fixture Dialog" in w["title"]), None)
                popup = sysres.popup_for(main_hwnd)
                check("popup detection finds the owned dialog",
                      bool(popup) and popup.get("owner_hwnd") == main_hwnd
                      and popup.get("hwnd") == dialog_hwnd, str(popup))
                popup_shot = vision.see(hwnd=main_hwnd, include_screen=False, max_elements=10)
                check("cu_see captures the popup by default",
                      bool(popup_shot.get("popup"))
                      and popup_shot["popup"].get("owner_hwnd") == main_hwnd,
                      str((popup_shot.get("popup") or {}).get("title")))
                main_shot = vision.see(hwnd=main_hwnd, include_screen=False,
                                       include_popups=False, max_elements=10)
                check("include_popups=false captures the main window",
                      main_shot.get("popup") is None
                      and _rect_close(main_shot["window_rect"], winenv.window_rect(main_hwnd), 2),
                      str(main_shot.get("window_rect")))
                orig_popup_fn = winenv.get_last_active_popup
                orig_fg_fn = winenv.foreground_hwnd
                winenv.get_last_active_popup = lambda _hwnd: 0
                winenv.foreground_hwnd = lambda: main_hwnd
                try:
                    passive_popup = sysres.popup_for(main_hwnd)
                finally:
                    winenv.get_last_active_popup = orig_popup_fn
                    winenv.foreground_hwnd = orig_fg_fn
                check("owned enumeration finds a dialog that never took focus",
                      bool(passive_popup) and passive_popup.get("reason") == "owned_enum"
                      and passive_popup.get("hwnd") == dialog_hwnd, str(passive_popup))
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()

    if ui:
        _ui_checks()

    _synthetic_checks()

    elapsed = round(time.time() - started, 1)
    failures = [r for r in results if r[0] == FAIL]
    skipped = [r for r in results if r[0] == SKIP]
    print(f"\n{len(results) - len(failures) - len(skipped)}/{len(results) - len(skipped)} passed"
          f" in {elapsed}s" + (f", {len(skipped)} skipped" if skipped else ""))
    return 1 if failures else 0


def _ui_checks() -> None:
    """Optional browser checks (only with --ui): hover an icon button for a tooltip."""
    edge = shutil.which("msedge") or next(
        (p for p in (
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ) if Path(p).is_file()),
        None,
    )
    if not edge:
        skip("ui: browser hover", "Edge not found")
        return
    profile = tempfile.mkdtemp(prefix="cu-selftest-edge-")
    page = Path(__file__).resolve().parent / "tests" / "page.html"
    proc = subprocess.Popen([edge, f"--user-data-dir={profile}", "--new-window",
                             f"--app=file:///{page.as_posix()}"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    hwnd = None
    try:
        deadline = time.time() + 12
        while time.time() < deadline and hwnd is None:
            hwnd = next((w["hwnd"] for w in sysres.windows_list()
                         if "computer-use" in w["title"].lower()), None)
            if hwnd is None:
                time.sleep(0.3)
        if hwnd is None:
            skip("ui: browser hover", "Edge window not found")
            return
        time.sleep(1.0)
        shot = vision.see(hwnd=hwnd, include_screen=False, max_elements=60)
        target = next((e for e in shot["elements"] if "分享" in str(e.get("text"))), None)
        custom = target is not None
        if target is None:
            # Some browser builds expose no web content through UIA; fall back to a
            # native window button so the hover pipeline still gets exercised.
            target = next((e for e in shot["elements"]
                           if e.get("source") == "uia"
                           and str(e.get("text")) in ("最小化", "关闭")), None)
        if target is None:
            skip("ui: browser hover", "no hoverable element exposed")
            return
        hovered = actions.hover(text=target["text"], hwnd=hwnd, settle=1.2)
        check("ui: hovering yields a tooltip",
              bool(hovered.get("tooltip")),
              f"{hovered.get('tooltip')!r} via {hovered.get('tooltip_source')}")
        if custom:
            check("ui: custom tooltip text is read back",
                  "分享" in str(hovered.get("tooltip")),
                  f"{hovered.get('tooltip')!r}")
    finally:
        # Close the window first: it lets the browser exit its helper processes
        # cleanly, which a launcher-only terminate does not do.
        try:
            if hwnd is not None:
                sysres.window_close(hwnd=hwnd)
        except Exception:
            pass
        try:
            proc.wait(timeout=6)
        except Exception:
            proc.kill()
        for _ in range(6):
            shutil.rmtree(profile, ignore_errors=True)
            if not Path(profile).exists():
                break
            time.sleep(0.4)
        if Path(profile).exists():
            # Last resort: kill every browser process bound to this profile.
            ps = ("Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
                  f"Where-Object {{ $_.CommandLine -like '*{Path(profile).name}*' }} | "
                  "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
            try:
                subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
            except Exception:
                pass
            shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main(full="--full" in sys.argv, ui="--ui" in sys.argv))

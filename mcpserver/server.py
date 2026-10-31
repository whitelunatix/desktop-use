"""MCP server exposing generic computer-use primitives over stdio."""
from __future__ import annotations

import json
import sys
import threading
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp.server.fastmcp import FastMCP

import actions
import cache
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

mcp = FastMCP("computer-use")


def _json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _guard(fn):
    try:
        return _json(fn())
    except actions.ActionError as exc:
        return _json({"ok": False, "error": exc.code, "hint": exc.hint})
    except vision.CaptureError as exc:
        return _json({"ok": False, "error": exc.code, "hint": exc.hint})
    except locate.TemplateNotFound as exc:
        return _json({"ok": False, "error": "template_not_found", "hint": str(exc)})
    except diff.DiffError as exc:
        return _json({"ok": False, "error": exc.code, "hint": exc.hint})
    except tree.TreeError as exc:
        return _json({"ok": False, "error": exc.code, "hint": exc.hint})
    except Exception as exc:  # keep every failure machine-readable
        return _json({"ok": False, "error": "exception", "hint": f"{type(exc).__name__}: {exc}",
                      "trace": traceback.format_exc(limit=3).splitlines()[-3:]})


def _warm_ocr() -> None:
    time.sleep(1.0)
    try:
        recognize._engine()
    except Exception:
        pass


@mcp.tool()
def cu_init(force: bool = False) -> str:
    """初始化：采集显示器布局、DPI、活动窗口等元信息，写入 <cwd>/.computer-use/env.json。"""
    return _guard(lambda: initmeta.init(force=force))


@mcp.tool()
def cu_see(monitor: str = "auto", window: str | None = None, region: str | None = None,
           filter: str | None = None, ocr: bool = True, fast: bool = False,
           mode: str = "screen", templates: str | None = None, annotate: bool = False,
           max_elements: int = 120, redact: bool = True,
           compare_to: str | None = None, diff_keyword: str | None = None, max_regions: int = 8,
           diff_crops: bool = True, only_changed: bool = True, diff_threshold: int = 6,
           include_screen: bool = True, overlay: bool = True,
           include_popups: bool = True, include_windows: bool = False) -> str:
    """截屏并识别：返回图片路径 + 元素清单（id/文本/坐标，坐标是桌面物理像素）。

    monitor: auto|0|1|all；window: 窗口标题片段；region: "x,y,w,h"（桌面像素）；
    filter: 只看含该关键词的元素；fast: 只截图不做 UIA/OCR；
    mode: screen|window（window 用 PrintWindow，可抓被遮挡窗口）；
    templates: 逗号分隔的模板名，匹配结果也列进 elements；annotate: 另存标注图；
    max_elements: 最多返回多少条；redact: 密码框涂黑；
    compare_to: 差异对比引用 state:<id>|image:<路径>|node:<树>/<节点>，只回变化区域；
    diff_keyword: 只保留区域文本命中该词的差异块；max_regions: 最多几块；
    diff_crops: 保存区域裁剪图；only_changed: elements 只回落在变化区域内的元素。
    include_screen: window 目标默认额外截整块显示器，给 placement/overlay（截图含屏上其他内容，敏感场景传 false）；
    overlay: 生成 RGBA 叠加图（窗口区域不透明、其余透明）；include_popups: 目标窗口有归属弹窗时抓弹窗；
    include_windows: 是否附窗口列表（默认不附，需要时用 cu_window list）。
    """
    tpl_list = [t.strip() for t in templates.split(",") if t.strip()] if templates else None
    return _guard(lambda: vision.see(
        monitor=monitor, window=window, region=crop.parse_box(region) if region else None,
        keyword=filter, ocr=ocr, fast=fast, mode=mode, templates=tpl_list,
        annotate=annotate, max_elements=max_elements, redact=redact,
        compare_to=compare_to, diff_keyword=diff_keyword, max_regions=max_regions,
        diff_crops=diff_crops, only_changed=only_changed, diff_threshold=diff_threshold,
        include_screen=include_screen, overlay=overlay, include_popups=include_popups,
        include_windows=include_windows,
    ))


@mcp.tool()
def cu_diff(from_ref: str | None = None, to_ref: str | None = None, diff_keyword: str | None = None,
            max_regions: int = 8, diff_crops: bool = True, texts: bool = True,
            threshold: int = 6) -> str:
    """差异对比：只回变化区域，每块给出图片像素框、相对原图偏移、桌面坐标、区域内文本、裁剪图。

    from_ref/to_ref: current（实时截屏，默认）| state:<id> | image:<绝对路径> | node:<树>/<节点>。
    两张存量图互比时不会截屏；texts=false 跳过区域 OCR。
    """
    def run() -> dict:
        source_ref = (from_ref or "current").strip()
        target_ref = (to_ref or "current").strip()
        if source_ref == "current" and target_ref == "current":
            return {"ok": False, "error": "same_ref", "hint": "give at least one of state:/image:/node:"}
        source = diff.load_ref(source_ref)
        target = diff.load_ref(target_ref)
        result = diff.compare(
            target["image"], source["image"], after_offset=target["offset"],
            before_offset=source["offset"], threshold=threshold, max_regions=max_regions,
            save=diff_crops, keyword=diff_keyword, ocr_texts=texts,
        )
        result["compared_to"] = {
            "ref": source["ref"], "image": source.get("path"), "size": source["size"],
        }
        return {
            "ok": True,
            "from": {"ref": source["ref"], "image": source.get("path"), "size": source["size"],
                     "offset": source["offset"]},
            "to": {"ref": target["ref"], "image": target.get("path"), "size": target["size"],
                   "offset": target["offset"]},
            "diff": result,
        }
    return _guard(run)


@mcp.tool()
def cu_tree(action: str = "list", name: str | None = None, node: str | None = None,
            parent: str | None = None, title: str | None = None, target: str | None = None,
            region: str | None = None, state_id: str | None = None, recapture: bool = True,
            notes: str | None = None, tags: str | None = None, recursive: bool = False,
            refresh: bool = False, diff_keyword: str | None = None, max_regions: int = 8,
            allow_risky: bool = False, force: bool = False, app_title: str | None = None,
            clear_region: bool = False, threshold: float | None = None,
            hwnd: int | None = None) -> str:
    """界面状态树：把任意窗口/网页/对话框/屏幕区域的不同界面状态记成可编辑的树。

    action=create(name, app_title?, hwnd?, region?) 建树并截根节点（scope 绑窗口标题/hwnd/屏幕区域）；
    list 列树；show(name?, node?) 看树形结构；
    add(parent, title, target?, region?) 加子节点（target 用 text:/template:/xy:）；
    update(node, title/target/region/notes/tags/recapture) 改节点；remove(node, recursive)；
    move(node, parent)；delete(name) 删整棵树；
    match(name?) 判断当前屏幕在哪个节点；diff(node, refresh?) 对比节点快照；
    suggest(node) 只建议新区域不写盘；goto(node, allow_risky?) 从根逐级点过去。
    """
    def run() -> dict:
        if action == "list":
            return {"ok": True, "trees": tree.list_trees()}
        if action == "create":
            if not name:
                return {"ok": False, "error": "missing_name"}
            return tree.create(name, title=title, app_title=app_title, force=force, region=region,
                               hwnd=hwnd)
        if action == "show":
            return tree.show(name, node)
        if action == "add":
            if not parent or not title:
                return {"ok": False, "error": "missing_parent_or_title"}
            return tree.add(name, parent=parent, title=title, target=target, region=region,
                            state_id=state_id, recapture=recapture, notes=notes or "",
                            tags=[t.strip() for t in tags.split(",") if t.strip()] if tags else None)
        if action == "update":
            if not node:
                return {"ok": False, "error": "missing_node"}
            return tree.update(name, node, title=title, target=target, region=region,
                               clear_region=clear_region, notes=notes, tags=tags,
                               recapture=recapture, state_id=state_id)
        if action == "remove":
            if not node:
                return {"ok": False, "error": "missing_node"}
            return tree.remove(name, node, recursive=recursive)
        if action == "move":
            if not node or not parent:
                return {"ok": False, "error": "missing_node_or_parent"}
            return tree.move(name, node, parent)
        if action == "delete":
            if not name:
                return {"ok": False, "error": "missing_name"}
            return tree.delete_tree(name)
        if action == "match":
            return tree.match(name, threshold if threshold is not None else tree.MATCH_THRESHOLD)
        if action == "diff":
            if not node:
                return {"ok": False, "error": "missing_node"}
            return tree.diff_node(name, node, refresh=refresh, diff_keyword=diff_keyword,
                                  max_regions=max_regions)
        if action == "suggest":
            if not node:
                return {"ok": False, "error": "missing_node"}
            return tree.suggest(name, node, max_items=max_regions)
        if action == "goto":
            if not node:
                return {"ok": False, "error": "missing_node"}
            return tree.goto(name, node, allow_risky=allow_risky)
        return {"ok": False, "error": "bad_action",
                "hint": "create|list|show|add|update|remove|move|delete|match|diff|suggest|goto"}
    return _guard(run)


@mcp.tool()
def cu_query(action: str = "search", text: str | None = None, regex: str | None = None,
             source: str | None = None, state_id: str | None = None, since: float | None = None,
             limit: int = 50) -> str:
    """在历史截图缓存里检索：action=search|list|get|prune。

    search 按 OCR/UIA 文本找元素（一图多用）；list 列状态；get 取单个状态；prune 清理过期缓存。
    """
    def run() -> dict:
        if action == "list":
            return {"ok": True, "states": cache.list_states()}
        if action == "get":
            state = cache.load_state(state_id or "")
            if state is None:
                return {"ok": False, "error": "unknown_state", "hint": state_id}
            return {"ok": True, "state": state}
        if action == "prune":
            return {"ok": True, **cache.prune()}
        return {"ok": True, "results": cache.query(text=text, regex=regex, source=source,
                                                   state_id=state_id, since=since, limit=limit)}
    return _guard(run)


@mcp.tool()
def cu_locate(template: str, image: str | None = None, state_id: str | None = None,
              region: str | None = None, threshold: float = 0.87,
              scales: str = "0.75,0.9,1.0,1.1,1.25", max_results: int = 5) -> str:
    """模板定位：在原始图像里找裁剪模板，返回 position（图片像素）与 global（桌面像素）。

    image: 图片路径；state_id: 用某次截图的图；都不给则实时截屏。
    region: 限定搜索区域 "x,y,w,h"（图片坐标）；scales: 逗号分隔的多尺度。
    """
    def run() -> dict:
        from PIL import Image

        scale_values = tuple(float(v) for v in scales.split(",") if v.strip())
        if image:
            img = Image.open(image).convert("RGB")
            offset = [0, 0]
            image_ref = image
        elif state_id:
            state = cache.load_state(state_id)
            if state is None:
                return {"ok": False, "error": "unknown_state", "hint": state_id}
            img = vision.image_of(state)
            offset = list(state.get("captured", {}).get("offset", [0, 0]))
            image_ref = state.get("image")
        else:
            img, meta, _ = vision.capture_target()
            offset = list(meta.get("offset", [0, 0]))
            image_ref = "live"
        result = locate.locate(img, template, region=crop.parse_box(region) if region else None,
                               threshold=threshold, scales=scale_values,
                               max_results=max_results, offset=offset)
        return {"ok": True, "image": image_ref, "offset": offset, **result}
    return _guard(run)


@mcp.tool()
def cu_crop(action: str = "crop", state_id: str | None = None, image: str | None = None,
            box: str | None = None, center: str | None = None, size: str | None = None,
            name: str | None = None, pad: int = 4, upscale: int = 1, save: bool = True) -> str:
    """裁剪模板：action=crop 从截图裁一块存为模板；action=list 列出模板；action=delete 删除。

    box="x,y,w,h"，或 center="x,y" + size="w,h"；upscale 放大倍数便于匹配小图标。
    """
    def run() -> dict:
        from PIL import Image

        if action == "list":
            return {"ok": True, "templates": crop.list_templates()}
        if action == "delete":
            if not name:
                return {"ok": False, "error": "missing_name"}
            return {"ok": True, **crop.delete_template(name)}
        if box:
            crop_box = crop.parse_box(box)
        elif center and size:
            cx, cy = crop.parse_point(center)
            w, h = crop.parse_point(size)
            crop_box = [cx - w // 2, cy - h // 2, w, h]
        else:
            return {"ok": False, "error": "missing_box", "hint": "box or center+size"}
        if image:
            img = Image.open(image).convert("RGB")
        elif state_id:
            state = cache.load_state(state_id)
            if state is None:
                return {"ok": False, "error": "unknown_state", "hint": state_id}
            img = vision.image_of(state)
        else:
            img, _ = vision.capture_target()[0:2]
        region, used = crop.crop_image(img, crop_box, pad=pad, upscale=upscale)
        result = {"ok": True, "box": used, "size": list(region.size)}
        if save:
            path = crop.save_template(region, name or f"tpl-{int(time.time())}")
            result["path"] = str(path)
            result["name"] = path.stem
        return result
    return _guard(run)


@mcp.tool()
def cu_click(id: int | None = None, text: str | None = None, x: int | None = None, y: int | None = None,
             template: str | None = None, offset: str | None = None, button: str = "left",
             count: int = 1, nth: int = 1, threshold: float = 0.87, region: str | None = None,
             modifiers: str | None = None, dry_run: bool = False, allow_risky: bool = False,
             state_id: str | None = None, force_stale: bool = False,
             window: str | None = None, hwnd: int | None = None) -> str:
    """点击：目标四选一 —— id / text / x+y（桌面物理像素）/ template。

    offset="dx,dy" 在命中点上再平移；button=left|right|middle；count=1|2；
    window/hwnd: 只在该窗口的截图里解析目标（text/template 过期会现场重截，id 需先 cu_see）；
    提交/支付/删除类目标必须带 allow_risky=true 才会执行；dry_run 只回报坐标。
    """
    xy = f"{x},{y}" if x is not None and y is not None else None
    return _guard(lambda: actions.click(
        element_id=id, text=text, xy=xy, template=template, offset=offset, button=button,
        count=count, nth=nth, threshold=threshold, region=crop.parse_box(region) if region else None,
        modifiers=modifiers, dry_run=dry_run, allow_risky=allow_risky, state_id=state_id,
        force_stale=force_stale, window=window, hwnd=hwnd,
    ))


@mcp.tool()
def cu_double_click(id: int | None = None, text: str | None = None, x: int | None = None,
                    y: int | None = None, template: str | None = None, offset: str | None = None,
                    button: str = "left", nth: int = 1, threshold: float = 0.87,
                    region: str | None = None, modifiers: str | None = None,
                    dry_run: bool = False, allow_risky: bool = False,
                    state_id: str | None = None, force_stale: bool = False,
                    window: str | None = None, hwnd: int | None = None) -> str:
    """双击：等价 cu_click(count=2)，目标四选一 —— id / text / x+y（桌面物理像素）/ template。

    编辑器里的占位文字、Word/Excel 单元格、文件名这类“双击才进入编辑”的目标用这个；
    两次按下之间的间隔由 winenv.click_at 控制在同一位置、短间隔内，能触发 WM_LBUTTONDBLCLK。
    其余参数含义同 cu_click。
    """
    xy = f"{x},{y}" if x is not None and y is not None else None
    return _guard(lambda: actions.click(
        element_id=id, text=text, xy=xy, template=template, offset=offset, button=button,
        count=2, nth=nth, threshold=threshold, region=crop.parse_box(region) if region else None,
        modifiers=modifiers, dry_run=dry_run, allow_risky=allow_risky, state_id=state_id,
        force_stale=force_stale, window=window, hwnd=hwnd,
    ))


@mcp.tool()
def cu_hover(id: int | None = None, text: str | None = None, x: int | None = None, y: int | None = None,
             template: str | None = None, offset: str | None = None, nth: int = 1,
             threshold: float = 0.87, region: str | None = None, window: str | None = None,
             hwnd: int | None = None, settle: float = 0.8, state_id: str | None = None,
             force_stale: bool = False) -> str:
    """悬停读提示：移到目标上停留，读 tooltip，不点击不按键。

    目标同 cu_click（id/text/x+y/template）；settle 是等待提示出现的秒数（默认 0.8）；
    读取优先级：UIA HelpText > 提示窗口文本/子窗口文本 > 光标附近 OCR；
    返回 tooltip / tooltip_source(uia_help|window|ocr|none) / tooltip_candidates，适合定位纯图标按钮。
    """
    xy = f"{x},{y}" if x is not None and y is not None else None
    return _guard(lambda: actions.hover(
        element_id=id, text=text, xy=xy, template=template, offset=offset, nth=nth,
        threshold=threshold, region=crop.parse_box(region) if region else None,
        window=window, hwnd=hwnd, settle=settle, state_id=state_id, force_stale=force_stale,
    ))


@mcp.tool()
def cu_move(to: str, offset: str | None = None, duration: float = 0.25, settle: float = 0.15,
            state_id: str | None = None, window: str | None = None,
            hwnd: int | None = None) -> str:
    """只移动鼠标不点击：to 用目标选择器 id:12 / text:菜单 / template:图标 / xy:100,200。

    用于悬停展开菜单或触发提示；settle 是到达后的停留秒数；window/hwnd 限定解析窗口。
    """
    return _guard(lambda: actions.move(to=to, offset=offset, duration=duration, settle=settle,
                                       state_id=state_id, window=window, hwnd=hwnd))


@mcp.tool()
def cu_type(text: str, into_id: int | None = None, method: str = "auto", clear: bool = False,
            enter: bool = False, state_id: str | None = None,
            window: str | None = None, hwnd: int | None = None) -> str:
    """输入文本：默认 Unicode SendInput（不碰剪贴板），长文本可 method=clipboard。

    into_id: 先点这个元素再输入；clear: 先全选；enter: 输入后回车。密码框拒绝输入。
    window/hwnd: 不给 into_id 时先把该窗口（有弹窗则弹窗）提到前台再输入。
    """
    return _guard(lambda: actions.type_text(text=text, into_id=into_id, method=method, clear=clear,
                                            enter=enter, state_id=state_id, window=window, hwnd=hwnd))


@mcp.tool()
def cu_key(combo: str, window: str | None = None, hwnd: int | None = None) -> str:
    """按键组合：如 ctrl+s、ctrl+shift+t、enter、esc、tab、alt+f4。

    window/hwnd: 先把该窗口（有弹窗则弹窗）提到前台再按键。键盘输入跟的是前台窗口，
    不指定时按键会落到当时的前台窗口上。
    """
    return _guard(lambda: actions.key_combo(combo, window=window, hwnd=hwnd))


@mcp.tool()
def cu_press(key: str, timeout: float = 10.0) -> str:
    """按住一个键不放（ctrl/alt/shift/win/任意键）。到达 timeout 秒会自动松开。"""
    return _guard(lambda: actions.press(key=key, timeout=timeout))


@mcp.tool()
def cu_release(key: str | None = None) -> str:
    """松开按键；省略 key 则松开全部按住的键。"""
    return _guard(lambda: actions.release(key=key))


@mcp.tool()
def cu_scroll(dy: int, dx: int = 0, at: str | None = None, to: str | None = None,
              state_id: str | None = None, window: str | None = None,
              hwnd: int | None = None) -> str:
    """滚动鼠标滚轮：dy 为竖直方向（负=向上），dx 为水平；at="x,y" 或 to=目标选择器。

    window/hwnd 只做校验与解析作用域：滚动始终发生在光标处。
    """
    return _guard(lambda: actions.scroll(dy=dy, dx=dx, at=at, to=to, state_id=state_id,
                                         window=window, hwnd=hwnd))


@mcp.tool()
def cu_drag(from_item: str, to_item: str, from_offset: str | None = None, to_offset: str | None = None,
            button: str = "left", steps: int = 20, duration: float = 0.6,
            hold_before_release: float = 0.2, modifiers: str | None = None,
            dry_run: bool = False, allow_risky: bool = False, state_id: str | None = None,
            window: str | None = None, hwnd: int | None = None) -> str:
    """拖放：from_item → to_item，两端都用目标选择器（id: / text: / template: / xy:）。

    分 steps 步移动，落点停留 hold_before_release 再松手；文件拖放从资源管理器的文件项拖到目标。
    window/hwnd 限定解析窗口。
    """
    return _guard(lambda: actions.drag(
        from_selector=from_item, to_selector=to_item, from_offset=from_offset, to_offset=to_offset,
        button=button, steps=steps, duration=duration, hold_before_release=hold_before_release,
        modifiers=modifiers, dry_run=dry_run, allow_risky=allow_risky, state_id=state_id,
        window=window, hwnd=hwnd,
    ))


@mcp.tool()
def cu_wait(for_text: str | None = None, window: str | None = None, template: str | None = None,
            gone: str | None = None, change_from: str | None = None,
            timeout: float = 10.0, interval: float = 0.3) -> str:
    """等待条件成立：出现文本 / 出现窗口 / 模板命中 / 窗口消失 / 画面相对某状态发生变化。"""
    return _guard(lambda: actions.wait(for_text=for_text, window=window, template=template,
                                       gone=gone, change_from=change_from,
                                       timeout=timeout, interval=interval))


@mcp.tool()
def cu_window(action: str = "list", hwnd: int | None = None, title: str | None = None,
              rect: str | None = None, state: str | None = None,
              width: int | None = None, height: int | None = None, size: str | None = None,
              anchor: str = "top-left", edge: str | None = None, mode: str = "auto",
              steps: int = 12, duration: float = 0.35, restore: bool = True,
              dry_run: bool = False, allow_snap: bool = False) -> str:
    """窗口操作：action=list|focus|close|move|resize；rect="x,y,w,h"；state=restore|minimize|maximize。

    resize: width/height 或 size="w,h"；anchor=top-left|top|top-right|left|center|right|
    bottom-left|bottom|bottom-right（不动的一侧/角）；mode=api|drag|auto（默认先 API，没变化再拖边）；
    edge 指定拖哪条边（bottom-right 等，仅 drag）；restore: 最大化/最小化先还原（默认 true）；
    allow_snap: 允许拖边落点贴近显示器边缘（默认拒绝，避免 Aero Snap 把窗口吸成半屏）；
    dry_run 只算几何不落盘（会给出 would_refuse）。返回 before/after/requested/verified/clamped。
    """
    def run() -> dict:
        if action == "list":
            return {"ok": True, "windows": sysres.windows_list(title)}
        if action == "focus":
            return sysres.window_focus(title=title, hwnd=hwnd)
        if action == "close":
            return sysres.window_close(title=title, hwnd=hwnd)
        if action == "move":
            box = crop.parse_box(rect) if rect else None
            return sysres.window_move(title=title, hwnd=hwnd, rect=box, state=state)
        if action == "resize":
            if mode not in ("auto", "api", "drag"):
                return {"ok": False, "error": "bad_mode", "hint": "auto|api|drag"}
            w, h = width, height
            if size:
                try:
                    w, h = crop.parse_point(size)
                except Exception as exc:
                    return {"ok": False, "error": "bad_size", "hint": str(exc)}
            if w is None and h is None:
                return {"ok": False, "error": "bad_size", "hint": "give width/height or size=\"w,h\""}
            if mode == "drag":
                return actions.resize_drag(title=title, hwnd=hwnd, width=w, height=h,
                                           anchor=anchor, edge=edge, steps=steps,
                                           duration=duration, restore=restore, dry_run=dry_run,
                                           allow_snap=allow_snap)
            result = sysres.window_resize(title=title, hwnd=hwnd, width=w, height=h,
                                          anchor=anchor, dry_run=dry_run, restore=restore)
            if mode == "auto" and not result.get("ok") and result.get("error") == "resize_failed":
                dragged = actions.resize_drag(title=title, hwnd=hwnd, width=w, height=h,
                                              anchor=anchor, edge=edge, steps=steps,
                                              duration=duration, restore=restore, dry_run=dry_run,
                                              allow_snap=allow_snap)
                dragged["api_attempt"] = {k: result.get(k) for k in
                                          ("before", "after", "method", "verified", "clamped")}
                return dragged
            return result
        return {"ok": False, "error": "bad_action", "hint": "list|focus|close|move|resize"}
    return _guard(run)


@mcp.tool()
def cu_folder(action: str = "open", path: str | None = None, select: str | None = None,
              all: bool = False, allow_risky: bool = False) -> str:
    """资源管理器：action=open（一条命令打开文件夹，可 select 文件）/list/focus/close。

    close + all=true 会关掉所有资源管理器窗口，属于风险动作，需要 allow_risky=true。
    """
    def run() -> dict:
        if action == "open":
            if not path:
                return {"ok": False, "error": "missing_path"}
            return sysres.folder_open(path, select=select)
        if action == "list":
            return {"ok": True, "folders": sysres.folder_list()}
        if action == "focus":
            if not path:
                return {"ok": False, "error": "missing_path"}
            return sysres.folder_focus(path)
        if action == "close":
            if all and not allow_risky:
                return {"ok": False, "error": "risky_requires_confirmation", "target_text": "关闭全部资源管理器窗口",
                        "hint": "repeat with allow_risky=true after the user agrees"}
            return sysres.folder_close(path=path, close_all=all)
        return {"ok": False, "error": "bad_action", "hint": "open|list|focus|close"}
    return _guard(run)


@mcp.tool()
def cu_clipboard(action: str = "get", text: str | None = None) -> str:
    """剪贴板：action=get 读取，action=set 写入给定文本。"""
    def run() -> dict:
        if action == "set":
            if text is None:
                return {"ok": False, "error": "missing_text"}
            return sysres.clipboard_set(text)
        return {"ok": True, "text": sysres.clipboard_get()}
    return _guard(run)


@mcp.tool()
def cu_doctor(calibrate: bool = False, check_ocr: bool = False) -> str:
    """环境自检：DPI、显示器布局、截图尺寸、UIA/OCR 是否可用、缓存目录可写、坐标往返误差。"""
    return _guard(lambda: doctor.report(calibrate=calibrate, check_ocr=check_ocr))


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    cache.dirs()
    threading.Thread(target=_warm_ocr, name="cu-ocr-warmup", daemon=True).start()
    mcp.run()


if __name__ == "__main__":
    main()

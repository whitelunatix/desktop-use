# 常用流程

所有调用都是 MCP 工具；返回都是 JSON（`ok` 为假时看 `error` 和 `hint`）。
坐标一律是桌面物理像素，服务端已处理显示器偏移、DPI 和窗口位置。

## 0. 第一次用

```
cu_init()            # 采集显示器、DPI、活动窗口，写 <cwd>/.computer-use/env.json
cu_doctor(calibrate=True)   # 截图尺寸、UIA/OCR、坐标往返误差
```

## 1. 看屏幕

```
cu_see()                                  # 当前显示器
cu_see(monitor="all")                     # 所有显示器拼成一张
cu_see(window="<窗口标题>")                 # 指定窗口（PrintWindow 模式可抓被遮挡窗口）
cu_see(window="<窗口标题>", mode="window")   # 强制 PrintWindow
cu_see(region="100,200,800,600", annotate=True)
```

返回重点：

- `state_id`：这张截图的编号，后续点击/差异都用它；
- `image`：PNG 路径，可以直接自己看图确认；
- `elements`：`{id, source(uia/ocr/tpl), type, text, box, center, ...}`；屏幕外的隐藏节点排在最后；
- `counts`：UIA / OCR / 模板各识别到多少；
- `captured.offset / size`：这张图在桌面上的原点与像素尺寸。缩放或整窗截图时 `size` 可能不是 1:1（例如比显示器尺寸略大或略小），需要换算显示比例就从这两个值推，不要假定图片与桌面像素一一对应；工具坐标始终是桌面物理像素。

`fast=true` 只截图不识别；`filter="保存"` 只看含关键词的元素；`max_elements` 只截断返回列表，不影响后续按 id 或文本点击。

## 2. 点网页按钮

```
cu_see(window="<窗口标题>")                      # 先拿到 state
cu_click(text="提交")                            # 会返回 risky_requires_confirmation
cu_click(text="提交", allow_risky=True)          # 用户同意后再执行
```

返回里的 `resolved.base / offset / final` 是换算链，`changed` 表示画面确实变了，`change_local` 是落点附近的像素差。
像素没变时服务端还会比对动作前后新出现的窗口：`changed:true` + `change_source:"window"` + `new_windows` 表示点击其实唤出了菜单/对话框，只是首帧绘制晚于校验窗口（`change_source:"pixels"` 则是常规像素路径）。这两种情况都先按结果继续（`cu_see` 复核或直接用 `new_windows` 里的句柄），不要原样重复点击。
若 `changed:null` 且 `verification_failed:true`，说明动作已经执行、只是动作后的截图校验失败；用 `cu_see` 复核结果，不要据此重复点击。

其他目标写法：

```
cu_click(id=17)                        # 同一张截图内的元素编号
cu_click(x=640, y=420)                 # 物理像素
cu_click(template="btn-submit")        # 模板匹配
cu_click(text="保存", offset="0,-8")    # 命中点上再平移
cu_click(text="文件", modifiers="ctrl") # 组合键点击
cu_click(text="提交审核", dry_run=True)  # 只算坐标不点
```

## 3. 截图裁剪 + 模板点击

```
cu_see()
cu_crop(state_id="20261028-...", box="180,540,120,44", name="btn-submit")
cu_click(template="btn-submit")
cu_locate(template="btn-submit")              # 只定位，返回 position/global/score/scale
```

`box` 是截图内的像素框；`center="x,y"+size="w,h"` 也可以。小图标建议 `upscale=2` 再存模板。
模板匹配是多尺度的（0.75/0.9/1.0/1.1/1.25），所以 125% 缩放下裁的图也能命中。

## 4. 系统文件对话框

```
cu_key("ctrl+o")                       # 应用打开文件对话框
cu_see(window="<窗口标题>")              # 对话框是独立顶层窗口：默认会被识别为 popup
cu_type("D:\\<目录>\\<文件名>")           # 文件名框已是焦点时直接输入
cu_key("enter")
```

保存对话框同理（`ctrl+s`）。路径里有中文时用 `cu_type` 的默认 Unicode 输入，不要走剪贴板。
需要选文件夹时，可以在对话框里输入完整路径后回车。

`cu_see(window="<窗口标题>")` 会返回 `popup:{hwnd,title,class,owner_hwnd,...}` 和 `scope_hwnd`，
截图与元素坐标都来自弹窗；`include_popups=false` 时才抓主窗口本身。

## 5. 拖放（含把文件拖进网页）

```
cu_drag(from_item="text:<源元素>", to_item="text:<目标区域>")
cu_drag(from_item="text:<文件名>", to_item="xy:900,600", hold_before_release=0.4)
```

两端都支持 `id:` / `text:` / `template:` / `xy:`。从文件管理器拖文件时，先用 `cu_see(window="<文件管理器窗口>")` 拿到文件名元素。

## 6. 长按与组合键

```
cu_press("ctrl", timeout=5)   # 最多按 5 秒，超时自动松开
cu_click(text="<重复文本>", nth=2)
cu_release()                  # 松开所有键；进程退出也会自动松开
```

`cu_key("ctrl+s")` 是一次性组合键；`cu_press` 是按住不放。

## 7. 窗口与文件夹

```
cu_window("list")
cu_window("focus", title="<窗口标题>")
cu_window("move", title="<窗口标题>", rect="100,100,900,700")
cu_window("close", hwnd=12345)

cu_folder(path="D:\\<目录>")                    # 一条命令打开
cu_folder(path="D:\\<目录>", select="<文件名>")   # 打开并选中文件
cu_folder("list")                               # 当前所有文件管理器窗口
cu_folder("close", path="D:\\<目录>")
```

## 8. 等待

```
cu_wait(for_text="导出完成", timeout=30)
cu_wait(window="另存为", timeout=10)
cu_wait(gone="正在加载", timeout=10)
cu_wait(template="btn-done", timeout=10)
cu_wait(change_from="20261028-...", timeout=10)   # 相对某张截图画面变了
```

## 9. 复用历史截图

```
cu_query(text="导出")                     # 所有历史截图里含「导出」的元素
cu_query(regex="第\\s*\\d+\\s*页")
cu_query(action="list")                   # 最近的截图状态
cu_query(action="get", state_id="20261028-...")
cu_query(action="prune")                  # 手动清理（树节点引用的会被保留）
```

缓存目录里有一份 `states/index.json` 元数据索引：`cu_query(action="list")` 与按窗口检索只读索引，
不再逐张解析 state。state 数量超过 `MAX_STATES + 10` 时会自动回收最旧的、未被树引用的截图，无需手动 prune。

## 10. 差异与状态树

见 `references/diff-and-tree.md`。最短路径：

```
cu_see()                                       # 记下 state A
# …用户或你做了某个操作…
cu_see(compare_to="state:<A>")                 # 只回变化区域 + 每块裁剪图
cu_tree("create", name="my-app", app_title="窗口标题")
cu_tree("goto", node="n2", allow_risky=False)
```

## 11. 窗口作用域（多窗口时不点错）

```
cu_see(window="<窗口标题>")                                # state 记录 scope_hwnd
cu_click(text="<按钮文本>", window="<窗口标题>", dry_run=True)  # 只在该窗口的截图里找
cu_click(text="<按钮文本>", window="<窗口标题>")                # 过期会自动重截，state_refreshed=true
cu_click(id=12, window="<窗口标题>")                        # id 不自动重截：先 cu_see 或 force_stale=true
cu_type("<文本>", window="<窗口标题>")                      # 不给 into_id 时先聚焦该窗口再输入
cu_key("ctrl+s", window="<窗口标题>")                       # 键盘跟前台窗口：先聚焦（有弹窗则弹窗）再按键
cu_scroll(-600, to="text:<列表项>", window="<窗口标题>")
cu_drag(from_item="text:<源>", to_item="text:<目标>", window="<窗口标题>")
```

返回里的 `scope:{hwnd,title}`、`state_id`、`state_refreshed` 说明实际用了哪张截图。
目标窗口已在最前时不会重复激活，避免打断正在进行的就地编辑；有归属弹窗时激活的是弹窗。
错误：`window_not_found`、`window_minimized`（先 `cu_window("focus")` 还原）、
`no_state_for_window`（id 目标且没有该窗口的截图）、`state_window_mismatch`（显式 state 属于别的窗口）。

## 12. 整窗 + 屏幕叠加（算相对位置）

```
cu_see(window="<窗口标题>")               # 默认 include_screen=true, overlay=true
cu_see(window="<窗口标题>", mode="window", include_screen=True)  # 被遮挡时用 PrintWindow 内容做叠加
cu_see(window="<窗口标题>", include_screen=False)                # 隐私/省磁盘：只截窗口
```

返回重点：

- `window_rect` / `window_origin`：窗口在桌面上的绝对矩形；
- `screen_image`：包含该窗口的显示器整图（`<id>-screen.png`）；
- `overlay_image`：RGBA 图，画布=显示器，窗口区域不透明、其余 alpha=0（`<id>-overlay.png`）；
- `placement`：`window_rect / screen_rect / image_offset_in_screen / window_size / fully_visible /
  clipped / off_screen / monitor / occluded_ratio`；
- `placement.image_offset_in_screen` 就是「窗口图像素」换算到「屏幕图像素」的增量；
  元素 `box/center` 仍然是桌面绝对坐标，不需要再用它换算；
- `placement.screen_source`：`primary` 表示窗口完全覆盖该显示器、屏幕图直接复用整窗截图；
  `recapture` 表示另外抓了一次显示器。
- 屏幕图与窗口图不是同一瞬间拍的：叠加图只作几何参考；`occluded_ratio` 仅在 PrintWindow 且窗口完全在屏内时给，仅供参考。

## 13. 悬停读 tooltip（纯图标按钮）

```
cu_hover(text="<按钮文本>", window="<窗口标题>")   # 按元素悬停
cu_hover(x=640, y=420, window="<窗口标题>")        # 按坐标悬停
cu_hover(template="<模板名>", settle=1.0)          # 图标模板
```

返回 `tooltip`、`tooltip_source`（`uia_help` / `window` / `ocr` / `none`）、`tooltip_candidates`、
`tooltip_rect`、`tooltip_hwnd/class`、`hover_texts`。等待时间约为 `settle + 1.5s`，
提示一出现就提前返回；读不到时返回 `tooltip_source:"none"`，不报错。
它只移动鼠标，不点击、不按键，也不触发风险词。

## 14. 调整窗口大小

```
cu_window("resize", title="<窗口标题>", size="1200,800", dry_run=True)   # 先看几何
cu_window("resize", title="<窗口标题>", size="1200,800")                  # 默认 auto：先 API，没变化再拖边
cu_window("resize", title="<窗口标题>", width=1200, height=800, anchor="bottom-right")
cu_window("resize", title="<窗口标题>", size="1200,800", mode="drag", edge="bottom-right")
cu_window("resize", title="<窗口标题>", size="1200,800", mode="drag", allow_snap=True)  # 明确接受吸边
```

- `anchor`：不动的一侧/角（`top-left|top|top-right|left|center|right|bottom-left|bottom|bottom-right`）；
- `mode`：`api`（`SetWindowPos`，不激活窗口）｜`drag`（鼠标拖边）｜`auto`（默认）；
- `restore=true`：最大化/最小化先还原再量矩形；`dry_run` 无副作用；
- 返回 `before/after/requested/verified/clamped`，`mode_used` 说明走了哪条路；
- 最小尺寸夹到 160×120；
- 拖边落点距显示器边缘不到 16px 时默认**拒绝执行**（`aero_snap_risk`，附 `snap_edges`），鼠标不会被移动；
  `dry_run` 只回报 `would_refuse`；确认要触发吸边时传 `allow_snap=true`，或改用 `mode="api"`；
- 只有位置变了 → `moved_instead_of_resized`；完全没变 → `resize_failed`（试 `mode="drag"`）。

## 参数速查

| 工具 | 关键参数 |
|---|---|
| `cu_see` | monitor, window, region, filter, ocr, fast, mode, templates, annotate, max_elements, redact, compare_to, diff_keyword, max_regions, diff_crops, only_changed, diff_threshold, include_screen, overlay, include_popups, include_windows |
| `cu_click` | id / text / x+y / template, offset, button, count, nth, threshold, region, modifiers, dry_run, allow_risky, state_id, force_stale, window, hwnd |
| `cu_move` | to(选择器), offset, duration, settle, state_id, window, hwnd |
| `cu_hover` | id / text / x+y / template, offset, nth, threshold, region, window, hwnd, settle, state_id, force_stale |
| `cu_type` | text, into_id, method(auto/clipboard), clear, enter, state_id, window, hwnd |
| `cu_key` | combo, window, hwnd |
| `cu_press` | key, timeout |
| `cu_scroll` | dy, dx, at, to, state_id, window, hwnd |
| `cu_drag` | from_item, to_item, from_offset, to_offset, steps, duration, hold_before_release, modifiers, dry_run, allow_risky, window, hwnd |
| `cu_wait` | for_text / window / template / gone / change_from, timeout, interval |
| `cu_diff` | from_ref, to_ref, diff_keyword, max_regions, diff_crops, texts, threshold |
| `cu_window` | action + list/focus/close/move/resize; resize: width, height, size, anchor, edge, mode(api/drag/auto), steps, duration, restore, dry_run, allow_snap; rect, state, title, hwnd |
| `cu_folder` | action(open/list/focus/close), path, select, all, allow_risky |
| `cu_clipboard` | action(get/set), text |
| `cu_tree` | action + name/node/parent/title/target/region/state_id/recapture/notes/tags/recursive/refresh/diff_keyword/max_regions/allow_risky/force/app_title/hwnd/clear_region/threshold |

## 错误码

| 错误 | 含义 / 处理 |
|---|---|
| `risky_requires_confirmation` | 目标含风险词，先征得用户同意再加 `allow_risky=true` |
| `stale_state` | 截图超过 8 秒，元素编号可能已失效；重新 `cu_see` |
| `unknown_state` / `state_has_no_elements` | state 不存在，或来自 fast/post 快照；重新 `cu_see` |
| `unknown_id` / `text_not_found` / `bad_nth` | 换目标写法或重新截图 |
| `template_not_found` | 模板名/路径不对，或图里确实没有 |
| `window_not_found` | 用 `cu_window("list")` 看准确标题，或直接传 `hwnd`；不会静默退化成全屏截图 |
| `window_minimized` | 窗口最小化；先 `cu_window("focus")` 或 `state="restore"` |
| `window_gone` | 窗口在识别后、抓取前消失；重新 `cu_window("list")` 再试 |
| `no_state_for_window` / `state_window_mismatch` | 该窗口还没有截图 / 显式 state 属于别的窗口；先 `cu_see(window=...)` |
| `capture_failed` | 窗口没有可见面积或 PrintWindow 失败；改 `mode="window"` 或先用 `cu_window("focus")` |
| `bad_size` / `bad_anchor` / `bad_edge` / `anchor_edge_conflict` / `bad_mode` | resize 参数问题；`anchor` 固定了某条边时不要再去拖那条边 |
| `resize_failed` / `moved_instead_of_resized` | 窗口拒绝缩放 / 落点在客户区；试 `mode="drag"` 或换 `edge`，看 `snap_edges` |
| `aero_snap_risk` | 拖边落点贴近显示器边缘，默认拒绝；缩小目标、改 `mode="api"`，或确认后传 `allow_snap=true` |
| `restore_failed` | 窗口最大化/最小化但 `restore=false`；改为 `restore=true` |
| `missing_target` / `bad_selector` / `bad_xy` / `bad_offset` | 参数格式问题 |
| `bad_ref` / `ref_image_missing` | 差异引用的 state/图片/node 不存在 |
| `same_ref` | `cu_diff` 两端都是 `current`，没有意义 |
| `tree_not_found` / `tree_exists` / `node_not_found` | 树/节点不存在或已存在 |
| `node_has_children` / `cycle` / `cannot_remove_root` | 先递归删除、别把节点挂到自己的后代下、用 delete 删整树 |
| `step_failed` | `goto` 某一步没落到预期节点；按提示 `update` 该节点的 target 或快照 |
| `bad_region` / `tree_full` / `bad_action` | 参数问题、节点超 200、动作名写错 |

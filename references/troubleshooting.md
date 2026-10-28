# 排错

先跑 `cu_doctor(calibrate=True, check_ocr=True)`，大部分环境问题它会直接指出。

## 截图相关

**截出来是黑的 / 只有桌面**
- 目标窗口被遮挡或最小化：用 `cu_see(window="标题", mode="window")`（PrintWindow），最小化窗口抓不到，先 `cu_window("focus")` 恢复。
- 播放器、部分游戏用硬件覆盖层，PrintWindow 也抓不到；把窗口切到前台再截。

**`window_not_found` / `window_minimized` / `window_gone`**
- `cu_see(window=...)` 找不到可见窗口会直接报错，不会退化成全屏截图；标题用 `cu_window("list")` 核对。
- 最小化窗口在抓取前就报 `window_minimized`，不会产生 -32000 坐标的占位图；先 `cu_window("focus")` 还原。
- 识别后、抓取前窗口被关掉会报 `window_gone`，重新 `cu_window("list")` 再试。

**截图尺寸和显示器不一致**
- 混合 DPI：`cu_doctor` 会报 `dpi_awareness`。本服务要求进程 DPI 感知，若宿主已设置过会返回 `E_ACCESSDENIED` 但状态仍可用。
- 多显示器时 `monitor="auto"` 取**前台窗口中心**所在屏（不是光标所在屏）；要整块桌面用 `monitor="all"`。
- `cu_see(window=...)` 还会额外抓该窗口所在显示器（`-screen.png`）并合成透明叠加图（`-overlay.png`），
  屏上其他窗口也会进图；只要窗口本身时传 `include_screen=false`。

**抓到的不是主窗口，而是一个小对话框**
- 这是弹窗识别：目标窗口有归属弹窗（owner / `GetLastActivePopup` / 同进程 `#32770`）时默认抓弹窗，
  返回里的 `captured.popup` 和 `scope_hwnd` 会写明；弹窗是独立顶层窗口，`owner_hwnd` 指向原窗口。
- 想抓主窗口本身：`include_popups=false`。
- 弹窗在识别后、抓取前消失：自动回退抓原窗口，`captured.popup` 为空并标记 `popup_fallback`，截图仍然成功。
- 标准菜单类（`#32768`）、tooltip、输入法候选不算弹窗；无标题但带 WS_POPUP 样式的归属面板（自定义菜单、工具面板）会算，返回里 `untitled:true`。

**坐标点偏了**
- 用 `cu_click(dry_run=True)` 看 `resolved.final`，再和截图里的位置对比。
- 所有坐标都是物理像素；不要自己乘 DPI。窗口有边框时用 `cu_see(window=...)` 的 `captured.offset` 解释。
- 图片尺寸不一定和桌面 1:1（整窗截图、缩放时 `captured.size` 会和显示器不同）。要换算显示比例就从 `captured.offset/size` 推，别把图片像素直接当桌面坐标。

## 识别相关

**网页内容读不到**
- 浏览器页面通常不暴露 UIA，只能靠 OCR。文字按钮用 `text:`；图标按钮先 `cu_crop` 存模板，再用 `template:`。
- OCR 会把相邻按钮并成一条（如「确定 取消」）。用 `nth` 配合更长的文本，或改用模板 / 坐标。

**文本命中却在屏幕外**
- UIA 会返回屏幕外的控件，坐标可能是占位值（例如 -32000）。`text:` 解析优先选中心落在虚拟桌面内的命中，
  只有在全都不可见时才回退；`nth` 也因此先数屏幕内的匹配。
- 仍不对就说明目标确实没显示：先滚动/展开/切到该选项卡，再重新截图。

**UIA 调用卡住**
- 服务端默认在子进程里跑 UIA，2.5 秒超时；超时会自动退回 OCR。若你手动 `CU_UIA_INPROC=1` 强制同进程，遇到卡死请去掉它。

**模板匹配不上**
- 检查 `cu_locate(template=..., threshold=0.8)` 是否返回空；
- 缩放变化：多尺度默认 0.75~1.25，超出就重新裁；
- 模板背景透明或太大时，裁小一点、`pad=0`、`upscale=2`；
- 中文路径没问题（内部用 PIL 读图），但路径写错会报 `template_not_found`。

## 动作相关

**`stale_state`**
- 截图超过 8 秒，元素编号可能对应不上屏幕了。重新 `cu_see` 再点。
- 带 `window`/`hwnd` 时，text/template 目标会自动重截（返回 `state_refreshed:true`）；
  只有 id 目标不会自动重截，因为它属于一张固定的截图。
- 确实想复用过期的 id 才加 `force_stale=true`，风险自负。

**`window_not_found` / `window_minimized` / `no_state_for_window`**
- 标题片段没匹配到：用 `cu_window("list")` 看准确标题，或直接传 `hwnd`。
- 最小化窗口没有可点坐标：先 `cu_window("focus")`（或 `state="restore"`）。
- `no_state_for_window` 是 id 目标且该窗口还没有截图；先 `cu_see(window=...)`。
- `state_window_mismatch` 是显式 `state_id` 属于另一个窗口；去掉 `state_id` 让工具自己选。

**点了没反应 / `changed:false`**
- 先看 `change_source` 与 `new_windows`：`change_source:"window"` 表示像素没变、但动作后出现了新窗口（菜单、下拉、对话框常晚于校验窗口才绘制）。按 `new_windows` 的句柄继续，不要原样再点一次。
- 目标可能不是可点区域：用 `cu_see` 看它的 `box`，或 `cu_click(dry_run=True)` 核对坐标。1px 的偏差足以点空，必要时按 `offset` 微调。
- 有些控件需要先悬停：`cu_move(text=...)` 再点。
- 慢界面：点完用 `cu_wait(for_text=...)` 等结果。

**风险拦截**
- 报 `risky_requires_confirmation` 说明目标文本里含提交/删除/支付等词。先向用户确认，再带 `allow_risky=true`。

**密码框打不进字**
- 设计如此。把窗口留给用户输入。

**长按没松开**
- `cu_press` 自带 timeout 看门狗，进程退出也会 `atexit` 释放；仍担心时调用 `cu_release()`。

## 悬停与提示

**`tooltip_source:"none"`**
- 该控件可能本来就没有提示，或提示出现得比 `settle + 1.5s` 还晚；调大 `settle` 再试。
- 提示窗口只在鼠标停下后才弹出：目标先 `cu_hover(text=...)`，不要用 `cu_move` 后立即读。
- 纯图标按钮先用 `cu_see` 确认 `resolved.final` 真的落在图标上，偏一点就可能悬停在空白处。

**读到的却是按钮自己的文字**
- `hover_texts` 是光标附近的元素文本，本来就包含按钮标签；tooltip 只看 `tooltip` 字段。
- OCR 兜底会丢弃包含光标的框，但如果提示正好压在按钮上，可能读不全；优先看 UIA `HelpText` 或提示窗口文本。

**提示文字只读到一半**
- `tooltip_candidates` / `tooltip_texts` 保留多个候选，`tooltip_detail` 说明来源（`UIA HelpText` /
  `child-window text` / `uia-name` / `ocr of the tooltip window` / `ocr near cursor`）。
- 有的提示窗口没有标题也没有子窗口文本，只能 OCR；小提示框会先放大 3 倍再识别。

## 缩放相关

**`resize_failed`**
- 窗口可能固定尺寸或拒绝 `SetWindowPos`；试 `mode="drag"`，或换一条边。
- 目标矩形小于 160×120 会被夹住，看返回的 `clamped` 和 `after`。

**`moved_instead_of_resized`**
- 拖边落点落进了客户区。改用 `mode="api"`，或在 `drag` 下换 `edge` / 调整 `anchor`。

**`aero_snap_risk` / `snap_edges`**
- 拖边落点离屏幕边缘不到 16px，继续拖会被 Aero Snap 吸走并改变尺寸，所以默认**拒绝执行**，鼠标不会移动。
- `dry_run` 只给 `would_refuse:true` 和 `snap_edges`，方便先判断。
- 处理：把目标尺寸调小一点、改用 `mode="api"`，或确认就是要吸边时传 `allow_snap=true`。

**`restore_failed`**
- 窗口处于最大化/最小化且传了 `restore=false`。去掉该参数或改成 `true`；`restore` 后会先还原再量矩形。

**`bad_anchor` / `bad_edge` / `anchor_edge_conflict` / `bad_size`**
- `anchor` 固定了某条边时不能再去拖那条边（例如 `anchor="left"` 配 `edge="right"` 才一致）。
- `size` 写成 `"宽,高"`；`edge` 只在 `mode="drag"` 时生效。
- `dry_run=true` 时最大化窗口的矩形来自 `rcNormalPosition`，与实际还原后的值可能差几个像素。

## 差异相关

**什么都没变却报 changed**
- 动画、光标闪烁、视频：调高 `diff_threshold`（10~16），或给 `cu_see` 加 `diff_keyword` 只保留关心的块。
- `others` 里有块被丢弃但 `changed:false`：说明变化太小，低于 400px² / 120 像素门槛。

**区域比预期大**
- 分块 32px + 3×3 膨胀 + 12px 外扩的结果；用 `global` 去点，别用 `box` 当精确元素框。

**`size_match:false`**
- 对比的两张图分辨率不同（例如窗口截图 vs 全屏）。按左上角对齐比交集，`size_delta` 给出差值。

## 树相关

**`goto` 报 `step_failed`**
- 现场已经停在失败那一步。看返回的 `image`，人工点一次，再 `cu_tree("update", node=..., recapture=true)` 刷新快照；如果入口变了，改 `target`。
- `text:` 目标在 OCR 合并后读不到时，换 `template:`。

**`match` 分数都很低**
- 目标窗口不在前台或有遮挡：`cu_window("focus")` 后重试。
- 界面改动大：`update(recapture=true)` 刷新快照。
- 两个状态太像：给节点设 `region`（只比内容区）。

**`tree_not_found` 且不传 name**
- 同时有多棵树且都无法绑定当前窗口时，必须显式写 `name`。

**`step_failed: no target`**
- 节点只是记录了一个状态，还没告诉工具「怎么到这个状态」。给它 `update(target=...)`。

**prune 之后节点图没了**
- 正常情况不会：`cu_query(prune)` 会保护树引用的 state。若手动删了 `states/` 或 `shots/`，用 `update(recapture=true)` 重建快照。

## 缓存与权限

- 缓存根目录是 `<当前工作目录>\.computer-use`；不可写时退回 `%LOCALAPPDATA%\computer-use`（`cu_doctor` 会显示实际路径）。
- 想换位置：设置环境变量 `COMPUTER_USE_CACHE_DIR`。
- 目录结构：`shots/` 截图、`states/` 元数据、`templates/` 模板、`trees/` 状态树、`debug/` 定位调试图。
- 根目录的 `index.json` 是 state 的元数据索引；删除或损坏会自动从 `states/` 重建，不用手修。
- state 超过 60 条时自动回收最旧的、未被状态树引用的截图；树引用的快照不会被删。
- `actions.log` 超过 2MB 会轮转成 `actions.log.1`（只保留一份备份），审计记录不会无限增长。

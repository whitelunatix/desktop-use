# computer-use MCP server

把桌面当成可操作的界面：截屏 → 识别 → 点击/输入/按键，外加窗口、文件夹、差异对比和界面状态树。
对外只暴露 MCP 工具，AI 不执行这里的 Python。

## 启动与注册

```json
// ~/.claude.json -> mcpServers
"desktop-use": {
  "type": "stdio",
  "command": "<python 解释器路径>",
  "args": ["<仓库目录>/mcpserver/server.py"]
}
```

注意：Claude Code 保留 `computer-use` 这个 MCP 服务名，用它注册会被标为 reserved 且不加载；
服务名用 `desktop-use`（工具名随之变成 `mcp__desktop-use__cu_*`）。

依赖：`pip install -r requirements.txt`（mcp、mss、Pillow、numpy、opencv-python、rapidocr_onnxruntime、uiautomation、pywin32）。

启动时会预热 OCR 模型（后台线程），第一次识别因此不会卡 14 秒。

## 坐标契约

- 工具收到和返回的坐标**永远是桌面物理像素**；
- 显示器偏移、窗口位置、区域裁剪的换算全部在服务端完成；
- `cu_see` 返回 `captured.offset`，`cu_click` 返回 `resolved.{base,offset,final}`，方便核对；
- 多显示器虚拟桌面原点为 0,0，第二块屏可能从 (2560,139) 开始。

## 模块

| 文件 | 职责 |
|---|---|
| `server.py` | FastMCP，21 个工具，UTF-8，JSON 输出，异常映射成 `{ok:false,error,hint}` |
| `capture.py` | mss 全屏/单屏/区域截图、PrintWindow、缩略指纹、`change_metrics` |
| `recognize.py` | UIA（子进程 + 超时，含 HelpText）、RapidOCR、合并去重、文本检索、tooltip 读取 |
| `locate.py` | 多尺度模板匹配（灰度 + 带 alpha 掩码）、标注、调试图 |
| `crop.py` | 裁剪模板、模板库增删查 |
| `vision.py` | 组合层：一次 `see` = 截图（+ 弹窗选择 + 屏幕/叠加图）+ 识别 + 缓存 |
| `diff.py` | 分块差异、连通域、区域裁剪、引用解析（current/state/image/node） |
| `tree.py` | 界面状态树：原子写、CRUD、ASCII、指纹匹配、diff/suggest/goto（按树 hwnd 取景） |
| `actions.py` | 点击/移动/输入/按键/长按/滚动/拖拽/悬停/拖边缩放、窗口作用域、风险词拦截、变更检测 |
| `keys.py` | 按住键 + 看门狗超时 + 退出释放 |
| `sysres.py` | 窗口列表/聚焦/关闭/移动/缩放、弹窗识别、文件管理器枚举与开关、剪贴板 |
| `winenv.py` | DPI 感知、SendInput、光标、窗口几何/所有者/边框、子窗口文本 |
| `initmeta.py` | 采集显示器/DPI/活动窗口写 `env.json` |
| `doctor.py` | 自检与坐标往返校准 |
| `cache.py` | 项目级缓存 `<cwd>/.computer-use`，索引 + 状态存取、按窗口检索、自动/手动 prune（保护树引用）、审计轮转 |
| `selftest.py` | 内部回归（不是 MCP 工具） |

## 工具

`cu_init` `cu_see` `cu_diff` `cu_tree` `cu_query` `cu_locate` `cu_crop`
`cu_click` `cu_move` `cu_type` `cu_key` `cu_press` `cu_release` `cu_scroll` `cu_drag`
`cu_hover` `cu_wait` `cu_window` `cu_folder` `cu_clipboard` `cu_doctor`

参数与错误码见 `../references/recipes.md`；差异与树的细节见 `../references/diff-and-tree.md`。

## 缓存布局

```
<cwd>/.computer-use/
├── env.json            显示器/DPI/窗口元信息
├── index.json          state 元数据索引（原子写；损坏/缺失自动重建）
├── actions.log         JSONL 审计：tool/ok/error/target/resolved/state_id/changed/elapsed_ms
├── actions.log.1       上一份审计日志（超过 2MB 时轮转）
├── shots/              截图（含 -screen/-overlay）、区域裁剪、标注图
├── states/<id>.json    截图元数据 + 全量元素（id 只在这个 state 内有效）
├── templates/          裁剪模板
├── trees/<name>.json   界面状态树（原子写：.tmp + os.replace）
└── debug/              定位失败时的调试图
```

`COMPUTER_USE_CACHE_DIR` 可覆盖根目录；不可写时退到 `%LOCALAPPDATA%\computer-use`。

## 关键行为

- **state 时效 8 秒**：过期后按 id/文本点击会报 `stale_state`，绝不静默重截导致 id 漂移；
- **风险词**：提交/投递/购买/支付/删除/确认/卸载/发送/退出/回收站，默认拦截；
- **密码框**：`cu_type` 拒绝写入，截图里会涂黑；
- **变更检测**：动作前后做整屏指纹 + 落点 ±200px 全分辨率对比，0.2s 后重拍一次；像素没变时再比对前后可见窗口集合（菜单/下拉/对话框常晚于半秒才绘制），任一路命中都算 `changed`，`change_source` 标出 `pixels` 或 `window`，`new_windows` 列出新出现的窗口；
- **差异**：32px 分块 + 3×3 膨胀 + 连通域 + 合并/门槛过滤，默认最多 8 块；
- **树**：节点存选择器不存 id；`goto` 每步校验，失败即停并给出修复提示；prune 保护被引用的快照。
- **窗口截图**：`cu_see(window=...)` 默认额外抓该窗口所在显示器的整图（`<id>-screen.png`）并合成 RGBA 叠加图（`<id>-overlay.png`，窗口区域不透明、其余透明），返回 `placement` 给相对位置；`include_screen=false` 可关。两张图不是同一瞬间，叠加图只作几何参考。
- **弹窗**：目标窗口有归属弹窗（owner / GetLastActivePopup / 同进程 `#32770` / 归属枚举）时默认抓弹窗，`captured.popup` 记录弹窗句柄与 owner；无标题但带 WS_POPUP 样式的菜单、工具面板同样会被识别，`untitled:true` 标明；`include_popups=false` 可关。
- **窗口作用域**：`cu_click/move/type/scroll/drag/hover` 可传 `window` 或 `hwnd`，只在该窗口的截图里解析；text/template 过期会自动重截（不生成屏幕图），id 仍要求先 `cu_see`。`cu_key` 也接受该作用域：按键前把目标窗口（有弹窗则弹窗）提到前台，已在最前时不重复激活，避免打断就地编辑。
- **悬停提示**：`cu_hover` 依次读 UIA HelpText、提示窗口标题/子窗口文本/UIA Name、tooltip 窗口或光标附近 OCR，用来定位纯图标按钮。
- **缓存索引与回收**：`states/index.json` 只存元数据（不存元素）；state 超过 `MAX_STATES + 10` 时自动回收最旧的、未被树引用的截图；索引损坏/缺失会从 `states/` 重建。
- **截图复用**：点击后的校验复用已抓到的画面，不再额外抓屏；窗口完全覆盖显示器时屏幕图直接从整窗截图裁剪（`placement.screen_source="primary"`）。
- **捕获错误**：标题/句柄不存在报 `window_not_found`，最小化报 `window_minimized`，抓取前消失报 `window_gone`；不会静默抓全屏，也不会产生占位坐标的 1×1 图。
- **拖边保护**：拖边落点距显示器边缘 <16px 时默认拒绝（`aero_snap_risk`），`allow_snap=true` 才放行；`dry_run` 给 `would_refuse`。
- **校验失败隔离**：动作已执行但后置校验失败时返回 `changed:null` + `verification_failed:true`，动作与审计记录都保留。
- **审计轮转**：`actions.log` 超过 2MB 轮转为 `actions.log.1`，只保留一份备份。

## 测试

```bash
cd <任意项目目录>            # 缓存会落在这个目录的 .computer-use
python <skill>/mcpserver/selftest.py          # 只读检查（只有 dry-run 点击）
python <skill>/mcpserver/selftest.py --full   # 额外：临时文件夹窗口的 resize/拖边/吸边拒绝、弹窗夹具（含未聚焦对话框）
python <skill>/mcpserver/selftest.py --ui     # 可选：用 Edge 打开测试页验证悬停（找不到 Edge 则 SKIP）
```

- `mcpserver/tests/page.html`：提交按钮（含风险词）、两个选项卡、拖拽卡片、文件投放区、
  密码框，以及一个带 `title`/`aria-label` 的图标按钮（悬停出自定义提示），用于 hover 验证。
- `mcpserver/tests/popup_fixture.py`：Tkinter 主窗口 + `transient` 对话框，给弹窗识别提供
  可重复的本地夹具（对话框 owner 就是主窗口）。

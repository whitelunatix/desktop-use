# desktop-use

Windows 桌面自动化 MCP 服务器 + Claude Code skill。把桌面当成可操作的界面：截图 → 识别（UIA / OCR / 模板）→ 点击、输入、按键、拖拽，并把结果缓存下来做差异对比与路径记忆。

An MCP server that lets an agent see and operate a Windows desktop: screenshots, UIA / OCR / template recognition, clicks, typing, keyboard, drag, window management, diffing and a reusable UI state tree. 21 tools, JSON in / JSON out, no DOM required.

## 特性

- **看屏幕**：全屏 / 单显示器 / 指定窗口 / 区域截图，返回元素列表（UIA 控件、OCR 文本、模板命中），坐标统一为桌面物理像素。
- **点击与输入**：按 id、文本、坐标或模板命中；支持组合键点击、长按、滚动、拖拽、悬停；密码框拒绝输入。
- **窗口**：列表、聚焦、关闭、移动、缩放（API 或拖边），可识别归属弹窗并优先操作弹窗。
- **差异与状态树**：两张截图之间做分块差异；把「从主界面怎么点到某个功能」存成可复用的状态树。
- **等待**：等文本、等窗口、等画面变化、等模板出现，全部带超时。
- **安全边界**：提交 / 支付 / 删除等风险动作默认拦截；拖边贴屏幕边缘默认拒绝（Aero Snap）；动作前后的变更检测不会把「点了没反应」误判成成功。

## 环境要求

- Windows 10 / 11（依赖 Win32 API、UIA、DPI 感知）
- Python 3.10+
- 依赖：

```bash
pip install -r mcpserver/requirements.txt
```

## 安装

1. 克隆仓库：

```bash
git clone https://github.com/whitelunatix/desktop-use.git
cd desktop-use
```

2. 在 `~/.claude.json` 的 `mcpServers` 里注册（Windows 路径）：

```json
"desktop-use": {
  "type": "stdio",
  "command": "<python 解释器路径>",
  "args": ["<仓库目录>/mcpserver/server.py"]
}
```

> Claude Code 保留 `computer-use` 这个 MCP 服务名，用它注册会被标为 reserved 且不加载；服务名用 `desktop-use`，工具名随之变成 `mcp__desktop-use__cu_*`。

3. 重启 Claude Code，首次调用 `cu_init` 采集显示器 / DPI / 活动窗口信息。

作为 Claude Code skill 使用时，把整个仓库目录放到 skills 目录（例如 `<user>/.claude/skills/computer-use/`），`SKILL.md` 会告诉模型什么时候该用哪个工具；`references/` 里有常用流程、差异与状态树、排错手册。

## 快速上手

```python
cu_see()                                # 截当前显示器，返回 state_id + 元素
cu_click(text="保存")                    # 按文本点击（同一 state 内）
cu_click(id=17)                         # 按元素编号点击
cu_type("hello", clear=True)
cu_key("ctrl+s")
cu_wait(for_text="完成", timeout=30)

cu_see(window="<窗口标题>")              # 只截某个窗口，并识别它的归属弹窗
cu_window("focus", title="<窗口标题>")
cu_window("resize", title="<窗口标题>", size="1200,800", mode="drag", edge="bottom-right")
```

22 个工具：`cu_init` `cu_see` `cu_diff` `cu_tree` `cu_query` `cu_locate` `cu_crop`
`cu_click` `cu_double_click` `cu_move` `cu_type` `cu_key` `cu_press` `cu_release` `cu_scroll` `cu_drag`
`cu_hover` `cu_wait` `cu_window` `cu_folder` `cu_clipboard` `cu_doctor`。

参数、错误码与更多流程见 [`references/recipes.md`](references/recipes.md)。

## 仓库结构

```
.
├── SKILL.md                 # Claude Code skill 说明（何时用、安全底线）
├── references/
│   ├── recipes.md           # 常用流程 + 参数速查 + 错误码
│   ├── diff-and-tree.md     # 差异对比与界面状态树
│   └── troubleshooting.md   # 排错手册
└── mcpserver/
    ├── server.py            # FastMCP，22 个工具，异常统一映射为 {ok:false,error,hint}
    ├── vision.py            # 截图 + 弹窗选择 + 识别 + 缓存
    ├── capture.py           # mss / PrintWindow 抓图、指纹、变化度量
    ├── recognize.py         # UIA 子进程 + RapidOCR + 合并去重
    ├── locate.py            # 多尺度模板匹配
    ├── actions.py           # 点击 / 输入 / 按键 / 拖拽 / 缩放、变更检测
    ├── sysres.py            # 窗口、弹窗、文件夹、剪贴板
    ├── winenv.py            # DPI、SendInput、窗口几何
    ├── cache.py             # 项目级缓存：索引、自动回收、审计日志
    ├── tree.py              # 界面状态树
    ├── diff.py              # 分块差异与连通域
    ├── selftest.py          # 内部回归
    └── tests/               # popup fixture 与测试页
```

缓存默认写到当前工作目录的 `.computer-use/`（可用 `COMPUTER_USE_CACHE_DIR` 覆盖），包含截图、state 元数据、模板与审计日志。

## 测试

```bash
cd <任意项目目录>                 # 缓存会落在这个目录的 .computer-use
python <仓库>/mcpserver/selftest.py          # 只读检查（dry-run 点击）
python <仓库>/mcpserver/selftest.py --full   # 额外：窗口缩放/拖边、弹窗夹具、贴边拒绝
python <仓库>/mcpserver/selftest.py --ui     # 可选：用 Edge 打开测试页验证悬停
```

`--full` 会打开临时文件夹窗口与一个 Tkinter 对话框夹具，需要桌面会话可交互。

## 已知边界

- 仅支持 Windows；依赖 UIA 的应用与自绘界面识别效果不同，自绘界面建议配合模板匹配。
- 最小化窗口无法截图（会返回 `window_minimized`），先还原再操作。
- 硬件覆盖层（部分播放器、游戏）抓不到内容。
- 一个缓存目录建议单实例使用；跨进程只做「写前重载 + 原子替换」的尽力而为。

## License

[MIT](LICENSE)

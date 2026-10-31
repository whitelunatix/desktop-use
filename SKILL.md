---
name: computer-use
description: 像人一样看屏幕并在屏幕上操作时使用：截图识别、点击没有 DOM 的界面、系统对话框、桌面应用、窗口与文件夹，以及同一界面不同状态之间的差异对比和路径记忆。所有能力都通过配套 MCP 工具提供。
---

# Computer Use

## 什么时候用

- 需要「看」屏幕：要一张截图，以及图上可点的元素（文本、坐标、模板）。
- 需要点击没有 DOM 的界面：网页画布、Electron/桌面应用、安装器、系统对话框。
- 需要操作系统对话框：打开/保存文件、选择文件夹、确认框。
- 需要开关、移动、聚焦窗口，打开/关闭文件夹。
- 同一个界面有很多状态（选项卡、弹窗、面板）：只想看「变了哪一块」，或者想记住「从主界面怎么点到某个功能」。

不用于：

- 能用 DOM / 接口完成的网页操作 —— 那是 js-reverse / playwright 的活，两套系统各自使用。
- 能用命令行完成的文件、窗口、进程操作。

## 安全底线

1. **风险动作必须显式放行**：提交、投递、购买、支付、删除、确认、卸载、发送、退出、清空回收站，默认会被拦截；只有用户同意后才能带 `allow_risky=true` 重试。
2. **密码框不输入**：工具会拒绝向密码框打字；需要用户自己输入时，把窗口留给用户，并可以先用 `cu_window(focus)` 帮他切到前台。
3. **坐标永远是桌面物理像素**：不要自己加减缩放或偏移；工具自动换算，用返回的 `global` / `resolved.final` 核对落点。截图尺寸不一定和桌面 1:1，需要显示比例时用 `captured.offset/size` 推，别按图片像素反推点击点。
4. **窗口截图会附带整块显示器**：`cu_see(window=...)` 默认还会截所在显示器并生成透明叠加图，屏幕上其他窗口也会进图；涉及隐私时传 `include_screen=false`。
5. **窗口目标必须真实存在**：标题不匹配会报 `window_not_found`（不会退化成全屏截图）；最小化窗口先还原，抓取前消失会报 `window_gone`。
6. **拖边缩放默认拒绝吸边**：拖边落点距显示器边缘 <16px 时返回 `aero_snap_risk` 且不移动鼠标；确实要触发 Aero Snap 才传 `allow_snap=true`，否则改用 `mode="api"`。
7. **`changed:false` 不等于没生效**：看 `change_source` 与 `new_windows`。像素没变但出现了新窗口，说明菜单/对话框已经弹出（首帧可能晚于校验），按新窗口继续，不要原样重复点击；`verification_failed:true` 表示动作已执行、只是校验失败，同样不要重试。

## 工具

全部走 MCP，不要在临时脚本里重写这些能力。

| 用途 | 工具 |
|---|---|
| 看屏幕 / 识别元素 | `cu_see` |
| 差异对比 | `cu_see(compare_to=...)`、`cu_diff` |
| 界面状态树 | `cu_tree` |
| 找模板、查历史截图 | `cu_crop`、`cu_locate`、`cu_query` |
| 点、移、输、按键 | `cu_click`、`cu_double_click`、`cu_move`、`cu_type`、`cu_key`、`cu_press`、`cu_release`、`cu_scroll`、`cu_drag` |
| 悬停读 tooltip（纯图标） | `cu_hover` |
| 等待条件 | `cu_wait` |
| 窗口 / 文件夹 / 剪贴板 | `cu_window`（list/focus/close/move/**resize**）、`cu_folder`、`cu_clipboard` |
| 环境与初始化 | `cu_init`、`cu_doctor` |

## 进一步阅读

- 常用流程与示例：`references/recipes.md`
- 差异更新与界面状态树：`references/diff-and-tree.md`
- 出错时：`references/troubleshooting.md`

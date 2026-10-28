# 差异更新与界面状态树

这两个能力解决同一件事：**不要每次都重看整屏**。

- 差异更新：拿「当前截图」和「上一张图」比，只回变化的那几块（含每块的裁剪图、相对原图的偏移、桌面坐标、区域内文字）。
- 状态树：把同一个界面（窗口、网页、对话框，甚至某个屏幕区域）的多个状态按树组织：节点是状态，边是「点哪个东西能到这个状态」。下次先说「我现在在哪」「怎么去某个功能」。

两者都是可选的增强；`cu_see` 不带参数时行为不变，并且永远同时返回整图路径，避免只看局部误判。

---

## 一、差异更新

### 1.1 引用写法

`compare_to`（`cu_see`）和 `from_ref`/`to_ref`（`cu_diff`）共用：

| 写法 | 含义 |
|---|---|
| `current` | 实时截屏（`cu_diff` 的默认值） |
| `state:<id>` | 某次 `cu_see` 或快照；裸 `20261028-...` 也认 |
| `image:<绝对路径>` | 任意 PNG（中文路径也可以） |
| `node:<树名>/<节点>` | 树节点的快照；节点可写 id 或标题关键字 |

### 1.2 用法

```
cu_see()                       # 记下 state_id，记作 A
# …点击、等待、用户操作…
cu_see(compare_to="state:A")
```

返回里多了 `diff`：

```json
"diff": {
  "compared_to": {"ref": "state:20261028-015203-1e8d", "image": "...png", "size": [2560, 1440]},
  "changed": true,
  "size_match": true,
  "changed_area": 0.0032,
  "regions": [{
    "index": 1,
    "box": [0, 500, 332, 652],
    "offset": [0, 500],
    "global": [0, 500, 332, 652],
    "relative": [0.0, 0.347],
    "score": 4.81,
    "ratio": 0.04,
    "pixels": 4231,
    "texts": ["提交审核", "已提交"],
    "image": "...\\shots\\20261028-022127-5b5a-diff-1.png",
    "before_image": "...\\shots\\20261028-022127-5b5a-diff-1-b.png"
  }],
  "annotated_image": "...\\shots\\20261028-022127-5b5a-diff.png",
  "others": {"count": 0, "area": 0.0},
  "filtered": false
}
```

- `box`：当前图里的像素框；
- `offset`：**这块相对原图左上角的偏移**（就是 box 的原点）；
- `global`：**桌面物理像素**（图片偏移已经加好），可以直接给 `cu_click(x,y)`；
- `relative`：归一化位置（0~1），换分辨率后仍可用；
- `texts`：落在区域内的元素文字（没有元素时对区域裁剪图补 OCR）；
- `image` / `before_image`：变化后 / 变化前的区域内容图；
- `annotated_image`：整图 + 红框标注。

只想看某类变化：

```
cu_see(compare_to="state:A", diff_keyword="已提交")
```

`filtered:true` 且 `regions:[]` 表示「变化了，但没有命中关键字」。

默认 `only_changed=true`：返回的 `elements` 只保留落在变化区域内的，省 token。想拿到整屏元素就传 `only_changed=false`。

### 1.3 两张存量图互比

```
cu_diff(from_ref="image:D:\\old.png", to_ref="image:D:\\new.png")
cu_diff(from_ref="state:A", to_ref="state:B")
cu_diff(from_ref="node:word/插入选项卡", to_ref="current")
```

`cu_diff` 不会截屏（除非某端是 `current`），返回 `{from, to, diff}`。
两端都是 `current` 会报 `same_ref`；同一张图自比 → `changed:false`。

### 1.4 算法与可调参数

1. 两图转灰度，逐像素绝对差；
2. 按 32px 分块取均值 → 32×24 左右的粗网格；
3. 网格值 > `diff_threshold`（默认 6）视为变化，3×3 膨胀把相邻块连起来；
4. 连通块还原成全分辨率框，外扩 12px；
5. 间距 < 24px 的框合并；框面积 < 400px² 或差异像素 < 120 的丢弃（闪烁光标会被滤掉）；
6. 每块给 `score`（区域内平均差）和 `ratio`（差异像素占比）；
7. 按 score 排序，最多 `max_regions`（默认 8）块，其余汇总到 `others`。

尺寸不一致时不会报错：`size_match:false`，按左上角对齐比交集，并给 `size_delta`。

**注意**：膨胀和 32px 分块会让框比实际变化大一圈；这是故意的（保证框住目标）。视频、进度条、闪烁光标会被判为变化，属预期。

---

## 二、界面状态树

### 2.1 概念

```
n1 主界面
├─ n2 插入选项卡     ← text:插入      （点「插入」从主界面到这）
│  └─ n4 页边距弹窗  ← text:页边距
└─ n3 页面布局       ← template:tab-layout
```

- **节点** = 一个状态：一张快照 + 一个 128×72 指纹 + 可选关注区域 `region`；
- **边** = 从父状态到子状态的动作，存在子节点的 `target` 上；
- **树** = 一组节点，存在 `<cwd>\.computer-use\trees\<名字>.json`。

树不局限于「应用」：可以是一整个网页、一个系统对话框、某个窗口，或者用 `region` 限定的一块屏幕区域。

### 2.2 建树：人工走一遍，顺手记下来

```
cu_tree("create", name="word", app_title="swputhesis", title="主界面")
# 得到 n1，并截下当前屏幕

# 你（或 AI）先点开「插入」选项卡，再让工具把这个状态挂上去：
cu_see()                                       # 先确认真的切过去了
cu_tree("add", name="word", parent="n1", title="插入选项卡", target="text:插入")

# 子节点默认现场截图（recapture=true）；也可以用已有截图：
cu_tree("add", name="word", parent="n2", title="页边距",
        target="text:页边距", state_id="20261028-...")
```

`target` 三种写法（不存元素 id，id 只在一张截图内有效）：

| 写法 | 例子 | 适用 |
|---|---|---|
| `text:...` | `text:插入` | 文本稳定、OCR/UIA 能读到 |
| `template:...` | `template:tab-insert` | 图标按钮、文本被合并/读不准 |
| `xy:x,y` | `xy:640,120` | 固定布局 |
| `key:...` | `key:ctrl+o` | 用快捷键进入的状态（打开/保存对话框等） |

`key:` 只发按键，不做点击；`alt+f4`、`ctrl+w` 视为风险动作，需要 `allow_risky=true`。

`region="x,y,w,h"`（桌面像素）限定这个状态只关心哪块区域。**选项卡内容只差几个字时，强烈建议给节点设 region**（比如内容区），否则全屏指纹会把两个状态算得几乎一样。

### 2.3 查看与编辑

```
cu_tree("show", name="word")                  # ASCII 树 + 全部节点 JSON
cu_tree("show", name="word", node="n2")       # 只看子树
cu_tree("update", name="word", node="n2", title="插入", notes="工具栏+内容区", tags="tab")
cu_tree("update", name="word", node="n2", region="0,120,2560,1320")
cu_tree("update", name="word", node="n2", recapture=True)   # 重新截图并更新指纹
cu_tree("move", name="word", node="n4", parent="n3")
cu_tree("remove", name="word", node="n4")
cu_tree("remove", name="word", node="n2", recursive=True)
cu_tree("delete", name="word")                # 删整棵树
```

`update(recapture=true)` 或 `diff(refresh=true)` 会用当前屏幕**覆盖**节点快照 —— 这是「把它改成现在这个样子」，不是「拿现在和它比」。只想比不想改，就不要带 refresh。

### 2.4 认路：我现在在哪

```
cu_tree("match", name="word")
```

把当前屏幕和每个节点快照都缩到 128×72 灰度比，节点有 region 时只比那个区域，返回前 3 名和分数（1.0 最像）。最高分低于 0.88 时会带 `no_confident_match` 提示。

树绑定了 `hwnd` 时会优先聚焦那个窗口；标题只是兜底。创建树时自动记录当时前台窗口的 hwnd、类名和标题。

如果同时开了两个标题很像的窗口（比如两个 Edge 窗口），`app_title` 可能匹配错。先 `cu_window("list")` 拿到 `hwnd`，再
`cu_tree("create", name=..., hwnd=123456)` 就能精确绑定。

### 2.5 导航：怎么去某个节点

```
cu_tree("goto", name="word", node="n4")
```

从目标回溯到根得到路径，逐级执行：

1. 聚焦树绑定的窗口；
2. 取该节点的 `target`，先做风险词检查（提交/删除/支付…），没带 `allow_risky=true` 就停；
3. 对文本目标重新截屏识别，再点击（不会用过期状态）；
4. 等 0.35s，截屏和子节点指纹比；
5. 分数低于 0.88 → 立刻停，返回 `step_failed` + 现场截图，提示你 `update` 那个节点的 target 或快照。

最多 10 级。半路失败不会继续乱点。

### 2.6 发现新状态：suggest

```
cu_tree("suggest", name="word", node="n2")
```

拿当前屏幕和节点快照做差异，把通过面积门槛的区域变成候选节点：标题取区域内第一段可用文字，`region` 给桌面坐标，`suggested_target` 给 `text:...`。

**只建议、不写盘、不点击**（`persisted:false`）。你确认后自己 `add`。这是为了防止它自己乱点乱建树。

### 2.7 与缓存的关系

- 节点快照就是普通 state 图；`cu_query(prune)` 会**跳过**所有树节点引用的 state，其余照旧清理；
- 删树之后，它独有快照才会进入下次 prune 的候选；
- 树文件在项目目录 `<cwd>\.computer-use\trees`，换项目互不干扰；建议把这个目录加进 `.gitignore`。

### 2.8 什么时候不该用树

- 界面每次都不一样（聊天流、实时列表）：用差异更新就够；
- 需要精确 DOM 操作：用 js-reverse；
- 只是想点一次按钮：直接 `cu_see` + `cu_click`，不要为一次操作建树。

---

## 三、组合用法

### 3.1 选项卡式界面（Word / 设置页）

```
# 只比内容区，避免工具栏噪声
cu_tree("create", name="settings", app_title="设置", region="0,120,1600,900")
cu_tree("add", ..., parent="n1", title="网络", target="text:网络", region="0,120,1600,900")
cu_see(compare_to="node:settings/网络")     # 只回内容区变化
```

### 3.2 点完按钮确认结果

```
cu_see()                                     # before
cu_click(text="提交审核", allow_risky=True)   # 返回 changed/post_state_id
cu_see(compare_to="state:<before>", diff_keyword="已提交")
```

### 3.3 记住文件对话框的位置

```
cu_tree("create", name="open-dialog", title="打开对话框")
cu_tree("add", ..., parent="n1", title="文件名框", target="xy:1200,1115")
```

下次 `cu_tree("match", name="open-dialog")` 先判断「是不是又弹出了同一个对话框」，再 `goto`。

## 四、已知限制

- 区域框会被膨胀算法放大一圈，别拿 `box` 当精确元素框用；
- OCR 可能把相邻按钮并成一条文本，这时用模板或 `xy` 作为节点 target；
- 树按屏幕像素记；窗口移动、缩放、换分辨率后，`xy:` 目标会失效，`text:`/`template:` 更稳；
- `match` 比较的是整屏；如果有别的窗口挡住目标窗口，先 `cu_window("focus")`；
- 视频、动画、进度条会产生持续假差异，`diff_threshold` 可调高到 10~16。

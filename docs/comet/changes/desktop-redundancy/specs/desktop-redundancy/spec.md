# desktop-redundancy：桌面操作多条路可走

依据：brief.md 的 Outcome，以及 2026-10-13 在用户电脑上的实测（msinfo32、WinForms 测试窗体、Windows OCR、权限级别）。

### R1 `ui` 改用原生 UI Automation

- **客户端**：进程内的 IUIAutomation（优先 CUIAutomation8），经 comtypes 调用。
  - 调用都在专用的工作线程（多线程单元）里进行；
  - 能设置时，连接超时设为 5 秒、事务超时设为 10 秒。
- **读取**（inspect，以及每个动作前的读取）：
  - 从窗口元素开始，一次取回 control view 的整棵子树和需要的属性（缓存请求）。
  - 同一进程里可见的菜单窗口（类名 `#32768`），以及目标窗口拥有、没有标题栏的弹出窗口（工具提示除外），如果不在这棵子树里，也读进来。打开的菜单、右键菜单因此能读到。
  - 保留哪些元素，与 0.4.9 相同：
    - 屏幕外又没有名字的跳过；
    - 有名字、automation_id 或窗口句柄的保留；
    - 类型可操作的也保留：Button、SplitButton、Edit、CheckBox、RadioButton、ComboBox、ListItem、DataItem、MenuItem、TabItem、TreeItem、Hyperlink、Slider、Document；
    - 最多 600 个。
  - 每个元素的字段与 0.4.9 相同：index、type、name、automation_id、class、hwnd、enabled、focused、rect、patterns。
    - `patterns` 列出可用的 Invoke、Toggle、SelectionItem、ExpandCollapse、Value、ScrollItem；
    - 只在对应操作可用时给出：`value`（密码框为 `(hidden)`）、`toggle`（On / Off / Indeterminate）、`selected`、`expanded`（Collapsed / Expanded / PartiallyExpanded / LeafNode）；
    - `rect` 为屏幕物理像素，空矩形为 null。
  - inspect 的结果多一个 `backend`：`native` 或 `powershell`。
- **操作**：在同一次调用里，直接作用于读到的元素。
  - invoke：用 Invoke；没有时依次用 Toggle、SelectionItem 的 Select、ExpandCollapse 的 Expand（与 0.4.9 相同）；
  - toggle、select、expand、collapse、set_value：用对应的操作；focus 用 SetFocus；
  - 经典按钮、复选框、单选按钮（有自己的窗口句柄）做 invoke、toggle、select 时，先用投递的窗口消息（BM_CLICK）。
    - 原因：UI Automation 的 Invoke 要等按钮的处理程序运行完。按钮打开模态对话框时，这一等就是对话框开着的全部时间。
  - 原有后备不变：
    - 经典控件用窗口消息；
    - click，或控件不支持 invoke、toggle、select 时，用真实鼠标点击控件中心；
  - click 之前，如果控件在屏幕外并且支持 ScrollItem，先滚动到可见，再取位置；
  - 动作后的回读和 verified 规则不变：
    - 原生读到的控件，回读时直接重读同一个元素。经典菜单这类经旧版辅助功能接口提供的控件，它们的 runtime id 每次读取都会变；
    - PowerShell 后备仍按 runtime id 找回控件。
  - UI Automation 报出的 COM 错误转成写明原因的普通错误：控件已经不在、被禁用、没有可点击的位置、拒绝操作、拒绝访问。UI Automation 自己的超时（UIA_E_TIMEOUT）按“没有响应”报告。这些错误码与别的 COM 错误同号，Windows 自带的说明文字会误导人。
  - 没有自己窗口句柄的控件做 invoke、toggle、select、expand 时，最多等 5 秒。超过时不报错，`done` 写明“已发送，程序还没处理完（可能在等它打开的对话框）”。原因：UI Automation 的 Invoke 要等控件的处理程序返回，程序打开模态对话框时会一直等下去。
  - 动作的结果里也带 `backend`。
- **时限**：一次读取或操作超过 20 秒（目标程序没有响应）时：
  - 报错，写明程序没有及时响应 UI Automation（可能忙、卡住，或在等它打开的对话框），动作可能已经生效；
  - 放弃那个工作线程；之后的调用换新线程。
- **后备**：原生客户端建不起来（缺组件、COM 出错）时，改用 0.4.9 的 PowerShell 脚本，`backend: "powershell"`，结果里附上原因。
- `ui(action='wait')` 使用同一套读取：
  - 只有窗口不在时，控件才算“不在”；
  - 程序没有响应时报错，不当作控件已消失；
  - 读取时界面正在变化而读不出来，这一次不作判断，继续等；等到超时时结果里带 `last_error`；
  - 程序超过 UI Automation 自己的时限（约 10 秒）没有响应时报错，不继续等；
  - 管理员窗口与其他动作一样拒绝。
- `winhand doctor` 报告 `ui_automation`：`native`；或 `powershell: <原因>`。

Scenario: (A1) 经典菜单、列表
- 设置：Windows PowerShell 5.1 的 WinForms 窗体，带经典菜单（MainMenu：“文件(&F)” → “关于(&A)”，点击后窗体标题改变），以及详细信息视图的 ListView（三行）。
- 结果：
  - inspect 列出 MenuItem “文件(F)”（带 ExpandCollapse），以及三个 ListItem（带 SelectionItem）；
  - expand “文件(F)” 后，“关于(A)” 出现在 inspect 里，invoke 它后窗体标题改变；
  - select 列表行 “内存”，回读 `selected=true`，`verified=true`。

Scenario: (A1) 经典树和菜单栏（真实的 Win32 程序）
- 设置：msinfo32（Windows 自带；没有时跳过）。
- 结果：
  - inspect 至少有 4 个 MenuItem 和 1 个名字非空的 TreeItem；用户电脑上列表加载完后总数至少 100 个；
  - 展开菜单栏上的一个菜单，`verified=true`；select 一个树节点，读回 `selected=true`。

Scenario: (A2) 0.4.9 的测试照常
- 设置：0.4.9 的 WinForms 测试窗体（文本框、按钮、复选框、密码框）。
- 结果：
  - set_value、toggle、invoke 都 `verified=true`；
  - 按钮被按下；
  - 密码不出现在任何结果里。

Scenario: (A3) 退回 PowerShell
- 设置：模拟原生客户端建不起来。
- 结果：inspect 和 invoke 照常完成，`backend` 为 `powershell`，并附原因。

Scenario: (A4) 程序无响应
- 设置：模拟原生调用超过时限。
- 结果：
  - `ui` 返回写明“没有响应”的错误；
  - 下一次调用用新的线程，照常完成。

### R2 新工具 `ocr`：认屏幕上的字

- **参数**：
  - `action`：read（默认）| find | click | double_click | right_click | move | wait；
  - `text`：要找的文字，read 以外必填；
  - `window`、`monitor`、`region`：看哪里，与 screenshot 相同；都不给时看整个桌面；
  - `state`：wait 时用，exists（默认）| gone；
  - `index`：第几个匹配（从上到下、从左到右），默认 0；
  - `language`：语言标签，如 `en-US`、`zh-Hans-CN`、`ja`；不给时自动选择；
  - `timeout_s`：wait 时用，最多 40 秒，默认 20。
- **识别**：
  - 用 Windows 自带的 Windows.Media.Ocr，经 Windows PowerShell 5.1 调用；
  - 取图方式与 screenshot 相同：被遮住的窗口也能读，R6 的修正同样适用；用原始分辨率；
  - 图片长边不超过 4,000 像素时，另做一份放大 2 倍的图；
  - 语言：给了 `language` 时只用它。没给时用 Windows 用户语言里第一个可用的 OCR 语言；如果装有 en-US 且与它不同，再加上 en-US；
  - 一次 PowerShell 调用里，每种语言、每种尺寸各识别一遍。
- **整理文字**：Windows OCR 把中日文拆成一字一词。
  - 相邻两个词只要有一个是中日韩文字，只有两者之间的间隙至少有字高的 0.3 倍时才加空格；
  - 挨着全角标点不加空格：全角标点的墨迹很窄，按间隙判断会多出空格；
  - 其余词之间加空格。
- **位置**：
  - 每行、每个匹配都有 `rect` = [left, top, width, height]，单位为屏幕物理像素；匹配还有 `center` = [x, y]；
  - 可以直接用于 `input(..., coordinates='screen')`。
- **匹配**：
  - 先把文字规范化：NFKC（全角转半角）、不区分大小写、去掉所有空白；
  - `text` 规范化后，是某一行规范化后的一部分，就算匹配；
  - 匹配的 `rect` 是覆盖这段文字的那些词的外框；
  - 选哪一遍的结果：
    - read 用首选的那一遍：首选语言，有放大图时用放大图；
    - 其余动作取第一遍有匹配的。`text` 只含 ASCII 时 en-US 在前，否则首选语言在前；同一语言放大的图在前；
  - 没有匹配时：
    - 结果给出最相近的至多 3 行（`similar`，带相似度）；
    - 不点击，也不移动鼠标；点击类动作的结果另有 `error`，写明什么都没点。
- **结果**：
  - read：`lines`，每行有 text 和 rect，最多 400 行；还有 `count`、`language`、`area`（看的范围，屏幕像素）；
  - find：`matches`，每个有 text（整行）、match（匹配到的文字）、rect、center；还有 `count`、`language`；
  - click、double_click、right_click、move：
    - 先把窗口提到前台（给了 window 时）；
    - 再对第 `index` 个匹配的中心执行鼠标动作，走与 `input` 相同的路径（R4 的检查同样适用）；
    - 结果为 find 的内容加 `done`；
  - wait：约每秒识别一次，直到出现（或消失）。
    - 返回 `matched`、`state`、`waited_s`；出现时还有那个匹配；
    - 超时返回 `matched=false` 和说明。
- **出错**：
  - 没有 OCR 语言时，说明怎样安装：设置 → 时间和语言 → 语言和区域 → 添加语言；或以管理员身份运行 `Add-WindowsCapability -Online -Name "Language.OCR~~~en-US~0.0.1.0"`；
  - 指定的语言不可用时，列出可用的语言；
  - 不是 Windows 时：“desktop tools are only available on Windows”。

Scenario: (A5) 读字
- 设置：测试窗体里有标签 “状态：已完成 Done”。
- 结果：
  - read 中有一行，去掉空格后包含 “状态：已完成”；
  - 中文字之间没有空格，“Done” 前面的空格保留；
  - rect 落在窗体的屏幕范围内。

Scenario: (A6) 按字点击
- 设置：0.4.9 的测试窗体，按钮文字为 “Press me”。
- 结果：
  - `ocr(action='click', text='press  ME', window=...)` 按下按钮，窗体标题改变；
  - 匹配的 center 落在 UI Automation 报告的按钮范围内。

Scenario: (A7) 等字与出错
- 设置：按下 Start 后，标签在 3 秒后改为 “Done”。
- 结果：
  - `wait` 在超时前返回 `matched=true`；
  - 等一个不会出现的字，1 秒超时后返回 `matched=false`；
  - 找不到时给出 `similar`；
  - 模拟没有 OCR 语言时，错误里有安装方法。

### R3 `screenshot` 等画面变化

- **新参数**：
  - `wait`：不给（默认）| `change` | `stable`；
  - `timeout_s`：最多 40 秒，默认 20；
  - `stable_ms`：200 到 10,000，默认 1,000。
- **比较方法**：
  - 按原始分辨率比较灰度，找出灰度差超过 16 的像素和它们的外框；尺寸不同算全部变了；
  - 与参照图比较，不与上一帧比较，所以慢的变化（慢速进度条、渐变）会累积起来；
  - **小变化**：外框不超过 32 × 48 像素（按屏幕缩放放大，300% 时为 96 × 144），例如文字光标、块状光标、闪烁的图标：
    - 对 change，要持续 2 秒才算变了，比最慢的光标闪烁（每相 1.2 秒）还长；
    - 对 stable，画面回到最近几帧之一（同样按灰度差超过 16 判断，细微抖动不影响）时，算光标闪回去，不算变化；
  - 已知限制：在 `stable_ms` 内累积不到 16 个灰度级的极慢渐变，会被当作稳定；
  - 其余任何变化都算变了。
- **change**：
  - 参照图是同一目标上一次的截图。目标可以是同一个窗口句柄、显示器、区域或整个桌面；上一次的截图来自任何一次 `screenshot` 调用，10 分钟内有效；
  - 没有上一次时，和本次调用的第一张比较。
- **stable**：参照图是最近一次变化后的那一帧；连续 `stable_ms` 没有变化。
- 约每 250 毫秒取一次图。
- **结果**：
  - 结束时的截图（与普通截图相同）；
  - 加 `wait: {condition, matched, waited_s}`；
  - 超时时 `matched=false`，并说明。

Scenario: (A8) 等画面
- 设置：按下 Start 后，测试窗体在 3 秒后改变标签。先等按钮的按下动画结束（stable）再截参照图。
- 结果：
  - 先截一次图，再 `screenshot(window=..., wait='change')`，在超时前返回 `matched=true`；
  - `wait='stable'` 返回 `matched=true`；
  - 焦点在文本框里、光标闪烁时，`wait='stable', stable_ms=1000` 在 5 秒内返回 `matched=true`；
  - 窗体不再变化时，`wait='change', timeout_s=1` 返回 `matched=false` 和截图。

### R4 锁屏、安全桌面、会话断开、管理员窗口：说清原因

- **桌面状态**：
  - `disconnected`：会话已断开（远程桌面关掉了但没有注销）；
  - `locked`：会话已锁定；
  - `secure`：没有锁定，但当前输入桌面不是用户的桌面（UAC 提示、Ctrl+Alt+Del 界面）；
  - `ok`：其余情况。读不到状态时也按 `ok` 处理，不因检测失败挡住操作。
- **状态不是 ok 时**：
  - `input`、`window(action='focus')`、`ui` 的 click（真实鼠标），以及 ocr 的点击类动作：直接报错，不发送任何输入；
  - `ui` 的其他读取和操作照常尝试：UI Automation 不经过屏幕；
  - `screenshot` 和 `ocr` 取图：桌面、显示器、区域直接报错；窗口先让窗口自己画，画出来不是全黑就照常返回，并附一句说明，否则报错；
  - 错误写明状态、原因，以及需要人在电脑前做什么；命令、文件、会话照常可用。
- **管理员窗口**：窗口所属进程的完整性级别高于 winhand 时，算管理员窗口。读不到级别时不算。
  - `window(action='list')` 给这类窗口加上 `admin: true`；
  - `ui` 作用在这类窗口上时，直接报错；
  - `input` 的鼠标动作：如果坐标处（拖动时起点或终点）的顶层窗口是这类窗口，报错；
  - `input` 的 keys，以及没有坐标的 type：如果前台窗口是这类窗口，报错。按键总是发给前台窗口，与 x、y 无关；带坐标的 type 先点击那里，所以看坐标处的窗口；
  - `ocr` 的点击类动作：与 `input` 相同；
  - 错误写明程序名、窗口标题，并说明：
    - Windows 不让普通程序向管理员程序发送点击、按键和 UI Automation 命令，发了也会被悄悄丢掉；
    - 办法：请电脑前的人操作这一步，或者不以管理员身份重新打开那个程序；
  - 截图和读字照常。
- `sys_info(checks=true)` 在这些情况下加一项：
  - 桌面状态不是 ok；
  - 有可见的管理员窗口，最多列 10 个；
  - winhand 自己以管理员身份运行。

Scenario: (A9) 锁屏等情况
- 设置：模拟三种状态。
- 结果：
  - `input` 报错，并且没有调用发送输入的函数；
  - 整个桌面的 `screenshot` 和 `ocr` 报错，错误里写明状态；
  - `window(action='focus')` 报错。

Scenario: (A10) 管理员窗口
- 设置：模拟某个窗口的进程级别更高。
- 结果：
  - 窗口列表里它有 `admin: true`；
  - 对它的 `ui`、`input` 点击、`ocr` 点击，报错，错误里有程序名；
  - 截图照常；
  - `sys_info(checks=true)` 里有一项列出它。
- 真机：用户电脑上以管理员身份运行的程序（例如 PixPin），只要有可见窗口，在列表里就是 `admin: true`。

### R5 指南与文档

- instructions（不超过 4,096 字符）的桌面一段说明这几点：
  - 先用程序自己的命令行或接口；
  - 然后用 `ui`（菜单也能操作），其次快捷键；
  - 没有控件的界面用 `ocr` 找字和点击；
  - 用 `screenshot(wait=...)` 代替固定等待。
- `help()` 的完整指南写明 `ocr` 的动作，以及锁屏和管理员窗口的含义。
- README 的工具一览加上 `ocr` 和 `screenshot` 的 wait。

### R6 不支持高 DPI 的程序，窗口截图对齐屏幕

- 有些程序不是“每个显示器单独感知 DPI”的。在有缩放的显示器上，它们的窗口内容按逻辑尺寸画出，再由 Windows 放大显示。
- 对这类窗口截图时，窗口自己画出的内容（PrintWindow）只占左上角的逻辑尺寸，其余是黑的。
- 修正：按窗口所在显示器的 DPI 与程序使用的 DPI 之比，把内容放大到窗口在屏幕上的尺寸。截图因此与屏幕看到的一致，坐标照常换算到屏幕。
- 只在左上角以外确实全黑时才放大。程序自己已经画满整个窗口时（例如 GDI 缩放的程序）照原样使用。
- 程序的 DPI 感知方式读不到时，照旧处理。

Scenario: (A12) 150% 缩放下的老程序窗口
- 设置：用户电脑（150% 缩放）上，Windows PowerShell 5.1 的 WinForms 窗体（不感知 DPI）。
- 结果：
  - 窗口截图没有黑边；
  - 按截图像素点击按钮，能按下它；
  - `ocr` 找到的按钮文字中心落在 UI Automation 报告的按钮范围内。
- 自动测试：模拟程序的 DPI 和显示器的 DPI，检查缩放后的尺寸和内容位置。

### R7 兼容

- `ui`、`screenshot`、`input` 的原有参数和返回字段不变，只增不减。
- 新依赖 comtypes 只在 Windows 上安装。打包时预先生成 UI Automation 的接口代码，并一起打包。
- 打包冒烟测试（Windows）检查 `doctor` 报告 `ui_automation: native`。

Scenario: (A11) 整体兼容
- 结果：
  - instructions 不超过 4,096 字符；
  - 原有测试全部通过，ruff 通过；
  - 打包冒烟测试通过；
  - Linux 上桌面工具（包括 `ocr`）照旧报 “only available on Windows”。

# Outcome

让 winhand 操作 Windows 桌面时**有多条路可走**：

> 程序自己的命令行/接口 → 按名字操作控件（ui）→ 快捷键 → 认屏幕上的字（OCR）→ 坐标点击加截图核对

一条路不通时，下一条接上。用户原话：“我想要的是一个 windows 都能通用处理的场景，一些更复杂的场景也能通用，相当于性能冗余了”。

0.4.9 的实测缺口（2026-10-13，在用户电脑上测得）：

- **按名字操作在经典 Win32 程序里几乎看不见东西**：`ui` 用的是 .NET 的旧版托管 UI Automation 客户端。在“系统信息”（msinfo32）里，它只看到 9 个没有名字的 Pane；原生 UI Automation 看到 163 个元素，其中 65 个可操作，包括菜单项“文件(F)…”、树节点和列表行。KiCad 的菜单栏看不到也是这个原因。
- **没有 OCR**：程序自己画的界面（画布、游戏、远程桌面、部分 Electron）只能猜坐标。
- **没有“等画面变化/稳定”**：没有控件的程序只能固定等待。
- **管理员窗口、锁屏不能操作，也不说原因**：输入被 Windows 悄悄丢掉（调用方以为成功了），锁屏时截图是黑的。
- **不支持高 DPI 的老程序，窗口截图对不上**（探查时发现）：用户电脑 150% 缩放，WinForms 窗体的截图只有左上 2/3 有内容，其余是黑的，坐标对不上屏幕。

用户选定的范围：1 原生 UI Automation、2 OCR 找字、4 管理员窗口/锁屏说清原因、3 等画面变化。

# Scope

**原生 UI Automation（R1）**
- `ui` 的读取、操作和等待改用 Windows 原生 UI Automation（COM 接口 IUIAutomation），在 winhand 进程内调用，不再每次启动 PowerShell。
- 打开的菜单、右键菜单里的菜单项也能读到、能点。
- 原生客户端起不来时，退回 0.4.9 的 PowerShell 脚本。
- 目标程序无响应时，在限定时间内报错，不挂住。

**OCR 找字（R2）**
- 新工具 `ocr`：读出窗口、显示器或区域里的文字及其屏幕位置；按文字查找、点击；等文字出现或消失。
- 使用 Windows 自带的 OCR（Windows.Media.Ocr），不装别的东西。

**等画面变化（R3）**
- `screenshot(wait='change' | 'stable')`：等到画面变了，或停止变化，再返回截图。

**说清原因（R4）**
- 锁屏、UAC 提示（安全桌面）、远程桌面已断开时，`screenshot`、`input`、`ui`、`ocr`、`window(focus)` 直接说明原因和需要人做什么，不返回黑图，也不发送输入。
- 以管理员身份运行的窗口：
  - 窗口列表标出 `admin: true`；
  - `ui`、`input`、`ocr` 的点击作用在这类窗口上时，拒绝执行，说明原因和办法；
  - 截图和读字照常。
- `sys_info(checks=true)` 列出桌面状态和管理员窗口。

**老程序的窗口截图对齐屏幕（R6）**
- 不支持高 DPI 的程序在有缩放的显示器上，窗口截图按缩放比例放大到屏幕上的尺寸。

**指南与文档（R5）**
- instructions 和 `help()` 写明上面的多路做法和新工具，instructions 不超过 4,096 字符。
- 更新 README。

## Source coverage

| 来源条目 | 读取状态 | Spec 位置 | 验收 ID | 覆盖状态 |
|---|---|---|---|---|
| 用户：Windows 通用、复杂场景也能用、性能冗余 | complete | R1–R7 | A1–A12 | covered |
| 评估缺口 1：经典 Win32 的控件看不见 | complete | R1 | A1–A4 | covered |
| 评估缺口 2：没有 OCR | complete | R2 | A5–A7 | covered |
| 评估缺口 3：没有等画面 | complete | R3 | A8 | covered |
| 评估缺口 4：管理员窗口、锁屏 | complete | R4 | A9、A10 | covered |
| 评估缺口 5：守卫不管桌面动作 | complete | — | — | non-goal（用户本次未选） |
| 探查发现：老程序窗口截图缩小、有黑边 | complete | R6 | A12 | covered |
| 用户问：Playwright MCP 有没有必要 | complete | — | — | non-goal：只改配置，验收后另行添加，见 D7 |

# Non-goals

- 浏览器自动化（Playwright MCP 等）：用 winhand 已有的本机 MCP 网关接入，只需改配置，不在本 change 写代码。
- 操作管理员窗口的提权助手：本次只说清原因。
- 守卫规则覆盖桌面动作（点击、输入）。
- 第三方 OCR 引擎（Tesseract、PaddleOCR）、按图片模板找位置、UI Automation 事件订阅。
- 锁屏时自动解锁：做不到，也不应该做。
- KiCad 等具体程序的专用适配：由各自的技能负责。

# Acceptance examples

- **A1 经典 Win32 覆盖**
  - 测试窗体包含经典菜单（MainMenu）、树（TreeView）和列表（ListView）：
    - inspect 列出菜单项、树节点和列表行，并带上可用的操作；
    - 展开菜单后，能 invoke 其中的菜单项（窗体标题随之改变）；
    - select 树节点和列表行之后，读回 `selected=true`。
  - 用户电脑上的 msinfo32：inspect 至少有 100 个元素，包括菜单项“文件(F)”和树节点“系统摘要”。
- **A2 兼容**：0.4.9 的 WinForms 测试照常通过：输入文字、勾选、按按钮、密码框不显示内容；元素字段不变。
- **A3 后备**：原生客户端起不来时（测试中模拟），`ui` 用 PowerShell 脚本完成同样的读取和操作，结果写明 `backend: "powershell"`。
- **A4 不挂住**：原生调用超过时限时，`ui` 返回错误，说明程序没有响应；之后的调用照常可用。
- **A5 OCR 读字**：
  - 返回每行文字和屏幕像素位置；
  - 中文字之间没有多余的空格，英文单词之间保留空格。
- **A6 OCR 点击**：
  - `ocr(action='click', text='Press me', window=...)` 按下测试窗体的按钮；
  - 查找时不区分大小写、全角和半角，忽略空格。
- **A7 OCR 等待与出错**：
  - `action='wait'` 等文字出现，`state='gone'` 等文字消失；
  - 超时返回 `matched=false` 并说明；
  - 没有可用的 OCR 语言时，错误里写明怎么安装。
- **A8 等画面**：
  - `wait='change'`：窗体在 1.5 秒后改变时，在超时前返回改变后的截图；
  - `wait='stable'`：画面停止变化后返回；
  - 文本框里光标闪烁不算变化；
  - 超时返回当时的截图，并写明 `matched=false`。
- **A9 锁屏等情况**（单元测试模拟）：锁屏、安全桌面、会话断开时，`screenshot`、`input`、`ui`、`ocr`、`window(focus)` 返回写明原因的错误，`input` 不发送任何输入。
- **A10 管理员窗口**（单元测试模拟权限级别；真机上看已在运行的管理员程序）：
  - 窗口列表里标出 `admin: true`；
  - 对它的 `ui`、`input`、`ocr` 点击，拒绝并写明程序名和办法；
  - 截图照常；
  - `sys_info(checks=true)` 列出桌面状态和管理员窗口。
- **A12 老程序窗口截图**：用户电脑 150% 缩放下，WinForms 窗体的截图没有黑边；按截图像素点击能按中按钮；`ocr` 找到的按钮文字中心落在 UI Automation 报告的按钮范围内。
- **A11 整体兼容**：
  - instructions 不超过 4,096 字符；
  - 原有测试全部通过，ruff 通过；
  - 打包后的 winhand.exe 在 Windows 上能启动原生 UI Automation（`doctor` 报告，打包冒烟测试检查）；
  - Linux 上桌面工具照旧说明“只在 Windows 上可用”。

# Constraints and invariants

- `ui` 的参数、返回字段和 verified 规则不变；只多一个 `backend` 字段。
- 不改变原有的经典控件补充（窗口消息）和鼠标点击后备。
- OCR 和等画面只读屏幕；点击走与 `input` 相同的路径（同样的锁屏、管理员窗口检查）。
- 识别出的文字照常经过脱敏（结果中间件）。
- 新增的依赖只在 Windows 上安装。

# Decisions

- **D1 原生 UI Automation 用 comtypes 调用**：
  - comtypes 是 Python 访问 COM 的常用包（纯 Python，只在 Windows 上安装）；
  - 接口代码从系统的 UIAutomationCore.dll 生成，打包后的程序在内存里生成；
  - 不手写虚表调用：写错一个位置就会让 winhand 整个崩溃；
  - 不用 pywinauto 或 uiautomation 包：更重，还带全局状态；
  - 保留 PowerShell 脚本作为后备（R1）。
- **D2 OCR 用系统自带的 Windows.Media.Ocr，经 Windows PowerShell 5.1 调用**：
  - 不需要安装；用户电脑已有中文、英文、日文的识别；
  - 实测识别 2392×1369 的窗口截图约 0.3 秒，加上启动 PowerShell 共约 1 秒；
  - 不用 pywinrt（要多 5 个二进制包），也不用 Tesseract（要安装程序和中文数据）。
- **D3 新增 `ocr` 工具**：读字、查找、点击、等待与 `ui` 的用法平行；等画面变化放在 `screenshot` 上，因为它的结果就是一张截图。
- **D4 管理员窗口拒绝而不是照发**：Windows（UIPI）会悄悄丢掉发往更高权限窗口的输入，`SendInput` 也不报错，照发会让调用方以为成功了。
- **D5 锁屏等情况返回错误**：不返回黑图，也不报“成功”。
- **D6 版本 0.5.0**：新增工具，`ui` 换了底层。
- **D7 范围与授权**：
  - 用户 2026-10-13 选定 1+2+4 核心和 3 等画面变化。
  - 关于 Playwright MCP，结论是它只管网页，网页上比桌面方法都稳，但不能代替桌面能力；接入只需改配置，验收后再加，不在本 change。
  - 其余技术决定由 Builder 按推荐取值，记录在此。用户此前说过“交给你了”。

# Open questions

- （无阻断项）

# Verification expectations

- Linux：`ruff`、`pytest`，桌面逻辑用模拟。
- Windows CI（`agent.yml` 的 windows 任务有桌面）：
  - 新的 WinForms 测试窗体（经典菜单、树、列表）覆盖 A1；
  - OCR 和等画面在测试窗体上实测（A5–A8）；
  - 打包冒烟测试（`binary.yml`）检查原生 UI Automation（A11）。
- 真机：
  - 在用户电脑上用本分支的源码跑一个临时 winhand（不影响正在运行的那个），实测 msinfo32、OCR、等画面和管理员窗口标记；
  - 发布 0.5.0 后再从云端会话试一遍。
- 独立 Verifier 逐项给出 passed / failed / blocked。

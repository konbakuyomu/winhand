# Agent Note: Windows 操控照着 CUA 的做法自己写，不接入 CUA 驱动

Status: implemented

## Problem

主人 2026-11-24 说 Windows 操控是 winhand 最大的短板，点了三处：

- 没有明确的信号和机制，表示 AI 正在控制电脑；
- AI 不能和主人同时用电脑，比如主人在看网页，AI 只在打了标记的几个标签页里操作；
- 不能只控制某个程序或窗口，也没有全局控制和退出全局控制的办法。

在主人电脑上（Windows 11 家庭版、150% 缩放、winhand 0.13.0）量到的现状：

- **所有桌面动作都先等主人停手。** 主人最近 5 秒有输入就等，最多等 10 秒，然后回 `OwnerBusy`，见 [desktop-gives-way-to-owner](../feature/2026-11-01-desktop-gives-way-to-owner.md)。主人在用电脑时，AI 什么也做不了。
- **`input` 每次都把目标拉到前台**（`desktop._force_foreground`）。
- **就算只用 UI Automation 的控件操作，也会抢焦点。** 11-24 的配对实测：主人在记事本里打字时，winhand 的 `uia.act` 对字符映射表写值、对计算器点按钮，12 轮里每轮抢 2 次，共 24 次。主人打的数字进了 AI 的窗口。第一次抢之前，系统里多了一次注入键 VK 0xB9，winhand 的代码只调了 `Invoke()` 和 `SetValue()`。主人没打字时，48 个动作里抢了 15 次。

要的是：AI 在后台操作主人允许的窗口，主人同时在用电脑，主人的输入不被抢走，并且主人看得见 AI 在做什么、随时能停。家庭版没有子会话，也不做隐藏桌面，只能在同一个桌面上共用。

## Decision

**选 B：在 winhand 里（Python）照着 CUA 驱动的做法自己写，不把 CUA 驱动装进来。** 讨论记录在 parley 房间 t30：第一、二轮结论 m267，第三轮 m275，第四、五轮 m303。实现由 Epic 004（`codestable/epics/004-o-Windows操控与人机共用/`）承接。

### 后台控件动作不抢焦点：两招守卫（Epic 004 第 1 张票）

两招都来自 CUA 驱动的源码（trycua/cua，MIT，`libs/cua-driver/rust/crates/platform-windows/src`），照着写，没有复制代码。落点是 `desktop.BackgroundGuard` 和 `desktop.background()`。

- **不可激活**：动作期间给目标的顶层窗口（`GetAncestor(GA_ROOT)`）加 `WS_EX_NOACTIVATE`，做完只清这一位，不写回整个旧值。来自 CUA `input/inject.rs` 的 `NoActivateGuard`。
  - 窗口在别的虚拟桌面上时不加：改样式会让它的任务栏按钮跑到主人正在看的桌面。判断用 `IVirtualDesktopManager`，判断完 COM 保持初始化，不调 `CoUninitialize`：先关 COM 再释放对象，在 CI 上造成过访问违例。
  - 样式加不上（以管理员身份运行的窗口会被 UIPI 拦下）时照常做，现有规则本来就拒绝这类窗口。
- **临时禁用**：XAML、UWP、WinUI 和 Chromium、Electron 宿主，在处理 Invoke、Toggle、Select、Expand 时会自己调 `SetForegroundWindow`，`WS_EX_NOACTIVATE` 挡不住。所以调用期间对顶层窗口 `EnableWindow(FALSE)`，调完按原状态恢复。按窗口类名认：`ApplicationFrameWindow`、`Windows.UI.Core.CoreWindow`、`WinUIDesktopWin32WindowClass`、`Chrome_WidgetWin_*`。来自 CUA `uia/fg_bypass.rs` 的 `run_with_uwp_bypass`。
- **超时**：UIA 调用超时后，工作线程可能还在程序的处理函数里。`uia._guarded()` 等那次调用真正返回再撤守卫，最多 `GUARD_CAP_S` = 30 秒。
- **覆盖范围**：
  - `tools_desktop._ui_act` 的五处后台动作：投递点击、UIA 模式调用、PowerShell 退路、写编辑框、滚动到可见；
  - `window` 的最小化、还原、最大化、移动：目标不在前台时，也不再把它切到前台。
- **WPF 挡不住**：它的控件在 Invoke 和 SetValue 里同步调 `UIElement.Focus()`。CUA 自己的注释说只能靠带 UIAccess 的辅助进程，那要签名并装进系统目录。winhand 在结果里带 `focus_note`，这类窗口继续让路。
- **崩溃恢复**：winhand 在守卫期间退出时，目标窗口可能停在禁用或不可激活状态，关掉那个程序再打开即可。守卫只覆盖一次调用，平时几毫秒，超时时最多 30 秒，正好碰上的概率低。启动时自动还原留作 Epic 004 的后续切片。

### 后台控件动作不再等主人停手（第 2 张票）

- `presence.acting(what, background=True)`：后台动作只查急停，不等主人。嵌套在里面的前台动作，如果这一串还没等过主人，就在那一层等一次。退回前台真点时的 `window` 切前台、`input` 点击，`steps` 里的 `keys`、`type`，都走这条。
- `desktop.background_ok(w)` 决定算不算后台：前台窗口属于别的程序、目标不是 WPF 时才算。前台就是目标的程序时，主人在用它，照旧让路。
- `ui` 的单个动作和 `steps` 把 `background_ok(w)` 交给 `presence`。`input`、`window`、`ocr` 的点击不变，照旧让路。`yield_to_owner` 的名字不变，现在管的是前台输入。
- 这部分取代了 [desktop-gives-way-to-owner](../feature/2026-11-01-desktop-gives-way-to-owner.md) 里「所有桌面动作先等人停手」的范围，急停和边框不变。

### Epic 004 接着做的

- 止血（第 3 张票）：`new_windows` 只报同一程序的窗口；按窗口取图和 OCR 不退回屏幕像素；`steps` 切前台失败就停。
- 第三轮定的控制会话：范围分窗口、程序实例、标签页、全局四档；信号会话期间一直亮；暂停和停止；后台做不到时回 `BACKGROUND_UNAVAILABLE`，经主人批准再借用前台。
- 第一、二轮定的工具契约：`view_id`、短表、diff、默认不回图，以及效果回执（`confirmed`、`no_change`、「已发送待核验」）。
- 浏览器标签组扩展：MV3，`tabGroups` 加 `chrome.debugger`，经 Native Messaging 连 winhand。
- 守卫的崩溃恢复：启动时还原上次没撤掉的。

### 明确不做

- 不装 CUA 驱动，不把它的 62 个工具给模型。
- 不连主人整个浏览器的远程调试。
- 不在后台发 Ctrl、Alt 组合键：事先拒绝，或请主人批准借用前台，或走 UIA 快捷键。

## Alternatives considered

- **A：借 CUA 驱动当底层。** winhand 留着工具、批准和保护，桌面动作交给 cua-driver 的守护进程。
  - 最强的理由：它在这台电脑上整条链路都跑通了。Win32 和 UWP 程序的后台点击、写值，没有一次注入键鼠；被挡住的窗口能截到它自己；独立的 Chrome 也能后台操作；而且是 MIT 许可，Windows 上的脏活它都踩过。
  - 否掉的原因：
    - **主人最要的它不管**：信号、范围、暂停和停止、标签组、密码管理器保护、程序授权，这些都得在 winhand 里写。
    - **实测有毛病**：
      - 后台 Ctrl+A 把字母 a 输进了编辑框（选区 (0,0)→(1,1)，截图里是「awinhand 第 8 轮」），只回 `unverifiable`；
      - 偶尔抢焦点：主人没打字时 48 个动作里 2 次，22:19:42 的实测里还有一次；
      - `launch_app` 启动 UWP 时回的是所有商店程序共用的 ApplicationFrameHost 进程号，按这个号 `kill_app` 会关掉全部商店程序。
    - **维护重**：
      - 体积约 82 MB，守护进程闲着也占 85 MB；
      - 0.61.0 到 0.61.3 是同一天发的预发布版；工具提前两天通知就删；
      - 遥测默认开（PostHog），还自带 `update --apply`。
    - **核心的两招约 30 行**，用 Python 写出来效果不比它差：主人没打字时，它 48 个动作里抢 2 次，我们抢 0 次。
- **C：混合。** 只借独立浏览器或个别能力，或者先借、以后再换。
  - 最强的理由：先拿到稳定的底层，早点交付。
  - 否掉的原因：
    - 独立浏览器不是主人要的。主人要的是他自己浏览器里的标签页，那要扩展。
    - 先借后换，中间写的进程通信、状态转换都要扔掉。
    - m297、m300 约定了转向条件：winhand 自己做不到，并且 4 人日内补不上，才改成 C。配对实测没触发这个条件。
- **只改让路规则，不加守卫。** 第四轮 codex-c 最初的判断（m293）。
  - 最强的理由：`ui` 本来就用 UIA 的模式和 `BM_CLICK`，看起来只差「不等主人」这条规矩。
  - 否掉的原因：配对实测里，主人打字时 UIA 调用照样每轮抢 2 次焦点，字串进了 AI 的窗口。只放开等待，等于让 AI 抢主人的输入。
- **挂 ChatGPT（Codex）的 Computer Use**（m267）。
  - 最强的理由：现成的产品，局部控制、全局控制、退出控制都有。
  - 否掉的原因：
    - Windows 上它占前台；空闲 15 秒后元素编号作废；
    - 截图绕过密码管理器涂黑，也不受主人在场和急停管；
    - 依赖预览版的私有运行时，条款没核实。

## Consequences

- **收益**：
  - 主人在用电脑时，AI 的后台控件动作照常完成，不抢主人的输入。2026-11-25 在主人电脑上，主人在记事本打字，AI 经完整的 `ui` 工具做了 84 个动作：「这人正在用电脑」的拒绝 0 次，前台变化 0，注入 0，结果全对。改之前同样的动作，每轮抢 2 次焦点，主人一动键盘就等满 10 秒后放弃。
  - 没有新的外部依赖：不装 82 MB 的驱动和守护进程，没有遥测和自动更新。
- **代价与已知上限**：
  - **WPF 挡不住**，只能继续让路。要挡住，得有签名的 UIAccess 辅助进程。
  - **同一程序照旧让路**：前台窗口属于目标的程序时照旧等。所有商店程序共用一个宿主进程，所以主人在用任何商店程序时，AI 操作商店程序也会等。
  - **每次控件动作多几次系统调用**：加样式、撤样式、判断虚拟桌面。实测写值的中位数是 39–69 毫秒。
  - **崩溃时窗口可能停在禁用状态**，要主人关掉那个程序再打开。见「两招守卫」。
  - **没在主人电脑上实测的**：Chromium、Electron、Office 窗口，只有单测和 CI 的 WinForms 场景覆盖。
- **何时重访**：碰上 WPF 程序必须后台操作；或者主人报告守卫期间退出后窗口变灰。

## Verification

- 单测：
  - `agent/tests/test_background_guard.py`（14 项）：类名分类；只撤自己加的；原来禁用的仍禁用；超时时等调用结束再撤。
  - `agent/tests/test_background_presence.py`（10 项）：后台动作不等，前台动作和嵌套的前台步骤照旧等；急停照样拒；`background_ok` 的几种情况；`ui` 工具把 `background` 交给 `presence`。
- Windows 场景测试：`test_background_controls_keep_notepad_foreground`，在 CI 的 windows-latest 上通过（run 45433699647）。
- 主人电脑实测，脚本在 `~/.winhand/scratch/cua-trial/`：
  - `accept001.py`，11-25 00:06：`_ui_act` 的真实路径，90 个动作前台变化 0；
  - `accept002.py`，11-25 00:48：完整的 `ui` 工具，84 个动作，拒绝 0、前台变化 0。
- 选型时的配对实测和守卫原型：同一目录下的 `paired.py`、`paired-*-log.txt`、`monitor-*.json`。实测汇总在 `~/.winhand/scratch/winctl-research/brief-4-cua-trial.md`。

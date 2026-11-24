# Agent Note: Windows 操控照着 CUA 的做法自己写，不接入 CUA 驱动

Status: proposed

## Problem

主人 2026-11-24 说 Windows 操控是 winhand 最大的短板，点了三处：

- 没有明确的信号和机制，表示 AI 正在控制电脑；
- AI 不能和主人同时用电脑，比如主人在看网页，AI 只在打了标记的几个标签页里操作；
- 不能只控制某个程序或窗口，也没有全局控制和退出全局控制的办法。

在主人电脑上（Windows 11 家庭版、150% 缩放、winhand 0.13.0）量到的现状：

- **所有桌面动作都先等主人停手。** `presence.before()`：主人最近 5 秒有输入就等，最多等 10 秒，然后回 `OwnerBusy`。`input`、`ui` 的动作、`window` 都套着它，见 [desktop-gives-way-to-owner](../../implemented/feature/2026-11-01-desktop-gives-way-to-owner.md)。所以主人在用电脑时，AI 什么也做不了。
- **`input` 每次都把目标拉到前台**（`desktop._force_foreground`：`AttachThreadInput`、按一下 Alt、`SetForegroundWindow`）。
- **就算只用 UI Automation 的控件操作，也会抢焦点。** 11-24 的配对实测：主人在记事本里打字时，winhand 的 `uia.act` 对字符映射表写值、对计算器点按钮，12 轮里每轮抢 2 次，共 24 次。主人打的数字进了 AI 的窗口，主人自己数了约 11 次「窗口闪到前面」。第一次抢之前，系统里多了一次注入键 VK 0xB9，winhand 的代码只调了 `Invoke()` 和 `SetValue()`，没有发键。主人没打字时，48 个动作里抢了 15 次。

所以要解决的是：AI 在后台操作主人允许的窗口，主人同时在用电脑，主人的输入不被抢走，并且主人看得见 AI 在做什么、随时能停。家庭版没有子会话，也不做隐藏桌面，只能在同一个桌面上共用。

## Proposal

**选 B：在 winhand 里（Python）照着 CUA 驱动的做法自己写，不把 CUA 驱动装进来。** 讨论记录在 parley 房间 t30：第一、二轮结论 m267，第三轮 m275，第四、五轮 m303。实现由 Epic 004（`codestable/epics/004-o-Windows操控与人机共用/`）承接。

### 第一步：两招防抢焦点守卫（1–2 人日）

2026-11-24 已实现，在主人电脑上验收中。落点是 `desktop.BackgroundGuard`、`desktop.background()`、`uia._guarded()`，以及 `tools_desktop._ui_act` 里的五处后台动作调用。细节和出入见 Epic 004 第 1 张票的执行记录。

两招都来自 CUA 驱动的源码（trycua/cua，MIT，`libs/cua-driver/rust/crates/platform-windows/src`）。实现时照着写，不复制代码：

- **不可激活守卫**（CUA `input/inject.rs` 的 `NoActivateGuard`）。每个后台动作期间，给目标的顶层窗口（`GetAncestor(GA_ROOT)`）加 `WS_EX_NOACTIVATE`，做完只去掉这一位，不恢复整个样式值，以免盖掉程序中途自己改的样式。
  - 窗口在别的虚拟桌面上时不加：改样式会让它的任务栏按钮跑到主人正在看的桌面。
  - 样式没加上（以管理员身份运行的窗口会被 UIPI 拦下）时，照现有规则拒绝操作。
- **禁用窗口守卫**（CUA `uia/fg_bypass.rs` 的 `run_with_uwp_bypass`）。XAML、UWP、WinUI 和 Chromium、Electron 的宿主，在处理 Invoke、Toggle、Select、Expand 时会自己调 `SetForegroundWindow`。`WS_EX_NOACTIVATE` 挡不住这一下，所以调用期间对顶层窗口 `EnableWindow(FALSE)`，调完恢复原来的启用状态。被禁用的顶层窗口当不了前台，模式调用照样送达。
  - 只对这几类宿主做，按窗口类名认：`ApplicationFrameWindow`、`Windows.UI.Core.CoreWindow`、`WinUIDesktopWin32WindowClass`、`Chrome_WidgetWin_*`。经典 Win32 程序不做。
- **任何结束路径都要还原**：正常返回、被拒、异常、超时、急停。
  - UIA 调用超时后，工作线程可能还在程序的处理函数里（`uia.call` 的 `NoAnswer`）。守卫要等那个调用返回再撤，同时设上限，不能让主人的窗口一直是灰的。
- **WPF 程序挡不住**：它的控件在 Invoke 和 SetValue 里同步调 `UIElement.Focus()`。CUA 自己的注释说只能靠带 UIAccess 的辅助进程，那要签名并装进系统目录。第一版遇到 WPF 时，先说明，再请主人批准借用前台。
- **原型证据**：11-24 用 Python ctypes 写了约 30 行（`class guard`）。主人没打字时 48 个动作抢 0 次，打字时 72 个动作抢 0 次，注入键 0，每轮结果都对；跑完窗口都是启用的，也没留下 `WS_EX_NOACTIVATE`。同一轮里不加守卫的对照组照旧每轮抢 2 次。

### 之后的顺序

两招守卫通过验收以后，再动下面这些：

1. **后台动作不再等主人停手。** 只限经守卫执行、没碰前台的动作。`input` 的按键和坐标点击照旧等。
2. **第三轮定的控制会话**：范围分窗口、程序实例、标签页、全局四档；信号会话期间一直亮；暂停和停止；后台做不到时回 `BACKGROUND_UNAVAILABLE`，经主人批准再借用前台。
3. **第一、二轮定的工具契约**：`view_id`、短表、diff、默认不回图，以及效果回执（`confirmed`、`no_change`、「已发送待核验」）。
4. **浏览器标签组扩展**：MV3，`tabGroups` 加 `chrome.debugger`，经 Native Messaging 连 winhand。

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

## Acceptance criteria

- **主人输入不受影响**（在主人电脑上测）：
  - 做法：主人在记事本里打一串已知数字，至少 40 秒；同时 AI 在后台对 Win32 和 UWP 窗口写值、点按钮，包括一个灰掉的按钮。
  - 要求：
    - 记事本里的内容逐字一致；AI 的窗口里没有串进来的字符；
    - 前台变化 0 次（每 5 毫秒查一次）；
    - 注入的键鼠 0 次（低级钩子统计，带正对照）。
- **窗口状态还原**：正常、被拒、异常、超时、急停之后，目标窗口的启用状态和扩展样式都和动作前一样。
- **组合键和 WPF**：发送之前就拒绝，或者请主人批准借用前台。原文里不能多出字符，也不能只回「无法确认」。
- **测试**：只在 Windows 上跑的场景测试，覆盖 Win32、UWP、Chromium 窗口；宿主类名判断写成纯逻辑单测。

## Risks

- **超时**：撤守卫太早，会重新抢焦点；禁用太久，主人用不了那个窗口。按「等调用返回，有上限」实现，并单测超时这条路。
- **winhand 中途崩溃**：窗口可能停在禁用状态。启动时检查上次没还原的窗口，或者至少在说明里写清怎样恢复。实现时定一种。
- **程序在禁用期间可能不处理输入。** 计算器、字符映射表测过没问题；Chromium、Electron、Office 还没测，验收时补。
- **回退**：两招守卫和「后台动作不等主人」分开提交。守卫出问题时撤掉它那次提交；「不等主人」要在守卫验收以后才打开。

## 证据位置

- 讨论：parley 房间 t30，m267、m275、m303（房间已结束）。
- 实测脚本和日志在主人电脑的 `~/.winhand/scratch/cua-trial/`：
  - `paired.py`：配对实测和守卫原型；
  - `paired-*-log.txt`、`monitor-*.json`：日志和监视记录；
  - `live.ps1`、`repro.ps1`：CUA 和主人并行打字的实测。
- 实测汇总：主人电脑的 `~/.winhand/scratch/winctl-research/brief-4-cua-trial.md`。

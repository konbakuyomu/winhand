# Outcome

修复 10-15、10-16 使用中发现的问题，发布 0.6.2：

1. **`session_screen` 读不出 Codex 的界面。**
   - 现象：会话 `codex-tui-test-1`（Codex 0.162 的 TUI，120×30）里，先发了一句中文插话，之后所有要看屏幕的调用都失败，报 “string index out of range”。失败的调用包括 `session_screen`，以及带 `include_screen` 的 `session_send`。会话结束前一直如此，只能强制结束会话。
   - 原因：
     - pyte 把宽字符（中文、emoji）存成一格字符，后面跟一个空的占位格。
     - Codex 重画界面时，新字符可能只压住旧中文字符的一半。真正的终端会把这个旧字符整个清掉；pyte 不清，留下一个前面没有宽字符的占位格。ConPTY 按终端的规则认为那一格已经空了，不会再补发空格，所以这个占位格一直留着。
     - pyte 自带的 `Screen.display` 读到这个占位格时执行 `char[0]`，于是抛出 IndexError。之后每次读屏幕都失败。
   - 复现：用户电脑上这次会话的原始日志（161,581 字节，sha256 `5f588ec1…`）按 PTY 的方式分段喂给 `VirtualScreen(120, 30)`，读到第 107,008 个字符后 `snapshot()` 抛出同样的 IndexError。
     - 出问题的是第 15 行：这一行原来是输入框里的“› 这是 TUI 插话测试…”，Codex 把它改写成“  等它结束。”时，“。”压住了“插”的左半，留下了“插”的右半占位格。
2. **桌面端闪退，电脑随之离线。**
   - 现象：10-16 00:52:52 桌面端（0.6.0）突然退出。后台随之停止（日志“app closed the pipe; shutting down”），中转从 00:52:55 起显示电脑离线，直到用户 01:00 手动重开。
   - 不是第一次：Windows 事件日志里 40 天内有 6 次，签名完全相同，分别是 09-27、10-05、10-10 两次、10-14、10-16，涉及 0.3.4、0.3.5、0.6.0 三个版本：
     - Microsoft.UI.Xaml.dll 里异常 0xc000027b，偏移 0x3a9dbd；
     - 内含的错误码是 0x80070057（E_INVALIDARG）。
   - 崩溃链条：用微软公开符号解析崩溃转储里界面线程的栈，结果是：
     - XAML 内部定时器 → `CXcpDispatcher::OnReentrancyProtectedWindowMessage` → `ErrorHelper::ReportUnhandledError` → `ProcessUnhandledError` → `FailFastWithStowedExceptions`。
     - 也就是说，XAML 在一次内部处理（布局、事件）里收到一个没人接的错误。应用没有订阅 `Application.UnhandledException`，于是被强制结束。
   - 触发点：6 次里有 5 次，崩溃时“活动”页正有工具调用（多为 `run`）刚开始或正在进行。这时列表会插入新行，或者每秒更新这一行的耗时。
     - 09-27、10-05、10-10 03:31、10-16 都在某个调用开始的那一秒。
     - 10-14 时，一个 `run` 已经进行了 22 秒，它是被这次崩溃连带中断的。
     - 10-10 04:12 那次，前 90 秒没有活动。
     - 转储里没有保存原始错误的调用栈，所以具体是哪个调用抛出的，无法从转储确认。
     - 已排除：分栏宽度的计算（用户电脑上没有保存过宽度）；10-16 崩溃前那几条活动记录的内容（没有控制字符、代理对等特殊字符）。
3. **一次性文件链接被云端沙箱的代理拦下（403）时，说明不够。**
   - 云端会话的网络设置里，只有一条 Path prefixes 为 `/hook/` 的 Network secret 放行中转域名，于是 `/t/…` 被代理拒绝（`X-Proxy-Error: … allows the host but not path "/t/…"`）。
   - 那个会话不知道原因，改为分段写文件绕过。README 里云环境的设置名称也已经过时。

# Scope

- **`VirtualScreen` 不再调用 `Screen.display`**，改为自己按终端的规则逐格渲染：
  - 前面没有宽字符的占位格（左半被盖掉）显示为空格；
  - 右半被盖掉的宽字符显示为空格；
  - 完整的宽字符照常显示。pyte 会把紧跟在宽字符后的组合符号（例如 macOS 文件名里分开存的浊点：か 后面跟 U+3099）放进占位格，渲染时把它合回这个字。
  - `snapshot()` 和 `cursor_line()` 都改用这个渲染。`session_screen`、`session_send` / `session_wait` 的 `include_screen`、提示符检测都经过这里。
  - 宽度用 pyte 自己排格子时用的 `pyte.screens.wcwidth`，保证判断一致。
- **桌面端不再因为界面错误退出**（新文件 `CrashGuard.cs`，改 `App.xaml.cs`）：
  - 订阅 `Application.UnhandledException`：把错误写进 `logs\desktop-app.log`，内容包括时间、种类、版本、HRESULT、异常类型、消息和调用栈。然后标记为已处理，每次运行在窗口里提示一次。
    - 提示和后端的提示共用窗口右上角的一个位置。后端启动成功时只清掉它自己的“后端启动失败”，不清这条；后端启动失败的提示会盖过这条。
    - 主窗口还没建好时（启动失败）不标记，让它照常结束。否则一个没有窗口的进程会占住单实例，再次启动也打不开。
  - 另外记录 `AppDomain` 的未处理异常和未观察的 Task 异常。日志每次运行最多 20 条，超过 1 MB 轮换一次。
  - `RegisterApplicationRestart("--background")`：万一还有接不住的崩溃，Windows 在后台把桌面端重新启动，后台和中转连接随之恢复。
    - Windows 只对运行满 60 秒的进程这样做，所以启动后马上又崩的不会被反复重启。
    - 每次运行一分钟以上才崩的，Windows 每次都会重启它，并各留一份错误报告。
  - 自检开关 `WINHAND_DESKTOP_SELFTEST=ui-error`：窗口加载后从 XAML 事件里抛一个异常，用来在真机上检查上面这条。平时不起作用。
- **“活动”列表的每一行改为自己的 `ListViewItem`**，与“会话”“MCP 服务”两个列表相同。以前每一行是普通元素，由列表套进它自己创建、回收的容器。这一条是降低触发概率的加固，不是已确认的根因。
- **一次性链接的提示和文档**：
  - `fs_upload_url` / `fs_download_url` 的 note 写明：HTTP 403 来自沙箱自己的网络策略，应请对方放行域名，而不是绕路。
  - README 按现在的界面写：Network access 选 Custom，在 Allowed domains 加上域名，勾上 Also include default list of common package managers。
  - README 和 `winhand bridge init` 的说明写明：Network secret 的 Path prefixes 要留空，只填 `/hook/` 会挡住 `/t/`。
- 版本 0.6.2。

# Non-goals

- 不改 pyte 本身，不在写入时模拟终端清除半个字符的行为：只在读出时纠正，覆盖所有写入路径（直接写、插入、删除、擦除）。光标列号仍按格子计。
- 不把“活动”页改成数据绑定加模板（更大的重写）；也不改“桌面端退出时后台跟着退出”的设计。
- **中转频繁断线不改代码**，只记录分析：
  - 自 9-26 起每天 20–50 次 “no close frame received or sent”，2–3 秒内自动重连。
  - WinHand 连中转的连接走 mihomo 的 TUN（`198.18.0.1 → 104.21.24.59:443`）。10-15 23:07 和 10-16 00:52:33 两次 mihomo 重启，00:52:32 正好断一次。
  - 断线时，正在进行的调用会收到“check history() before resending”的错误；新调用最多等 20 秒重连。

# Acceptance examples

- **C1**：`VirtualScreen` 对四种情况显示得与终端一致：
  - 宽字符压住另一个宽字符的左半：`文 z`；
  - 窄字符压住宽字符的左半：`a z`；
  - 窄字符压住宽字符的右半：` b`；
  - 没被压住的宽字符：`中文ok`。
  - 宽字符后跟组合符号：`か` + U+3099 + `x` 显示 `がx`；这个字的左半再被盖掉时显示 `a z`。
  - 新测试 `test_screen_shows_half_overwritten_wide_characters_like_a_terminal`：修复前前三种失败，修复后都通过。
- **C2**：真实日志回放：按 1、7、512、4096 个字符一段喂入，每段后读屏幕，修复后 0 次失败（修复前在第 107,008 个字符后失败）。最终画面与 Codex 界面一致。
- **C3**：0.6.2 装好后，在用户电脑上用会话启动 Codex TUI，发中文插话；整个过程中 `session_screen` 都能读出画面。
- **D1**：从 XAML 事件里抛出的异常不再结束桌面端。
  - 在用户电脑上，用 `WINHAND_DESKTOP_SELFTEST=ui-error` 启动 0.6.2 的测试实例（独立的实例名和 `WINHAND_HOME`）：15 秒后进程仍在，窗口可用。
  - `desktop-app.log` 记下 `ArgumentException`、0x80070057 和出错的代码行。
- **D2**：桌面端向 Windows 登记了崩溃后重启：`GetApplicationRestartSettings` 读回命令行 `--background`，标志 12（不因补丁、不因系统更新重启）。
- **D3**：0.6.2 装好后日常使用：桌面端不再闪退；如果 `desktop-app.log` 出现 `ui` 记录，说明接住了一次，按记录的调用栈继续修。
- **E1**：`fs_upload_url` / `fs_download_url` 的 note 提到 403 来自沙箱的网络策略；README 和 `winhand bridge init` 的说明与现在的云环境设置一致。
- **F1**：原有测试全部通过，ruff 通过；CI（agent、两个平台的打包冒烟、桌面端编译和打包）通过。

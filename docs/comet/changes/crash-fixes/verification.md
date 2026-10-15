# 验收记录：crash-fixes（0.6.2）

## 第 1 轮（dc3f8b3、a7e997e）

**正式检查**
- 云端（Linux）：`ruff check`、`ruff format --check` 通过；`pytest`：654 passed，51 skipped。
- CI（dc3f8b3，手动触发）：`binary.yml`（两个平台的打包冒烟）、`agent.yml`（ubuntu、windows）、`desktop.yml`（编译、打包、安装包）都通过。a7e997e 只加了 C# 自检开关，在用户电脑上编译通过；cae7714 只有文档。
- 用户电脑（Windows 11）：`dotnet publish` 桌面端 Release x64，dc3f8b3 与 a7e997e 都编译通过，产物版本 0.6.2。

**C1、C2（session_screen）**
- 新测试 `test_screen_shows_half_overwritten_wide_characters_like_a_terminal`：修复前 3 个失败、1 个通过（没被压住的那种），修复后 4 个都通过。
- 真实日志回放（`codex-tui-test-1.log`，161,581 字节，sha256 `5f588ec1…`）：
  - 修复前：第 107,008 个字符后 `snapshot()` 抛 IndexError，错误位置是 pyte `screens.py` 的 `wcwidth(char[0])`；
  - 修复后：按 1、7、512、4096 个字符一段喂入，每段后都读屏幕和光标行，0 次失败。最后一屏是 Codex 的界面，中文对齐正常，包括“TUI插话已收到，sh 文件数 = 160。”。

**D1、D2（桌面端）**
- 在用户电脑上启动 a7e997e 编译的测试实例：
  - 设置：`WINHAND_DESKTOP_INSTANCE=selftest`、`WINHAND_HOME=%TEMP%\winhand-062-home`、`WINHAND_DESKTOP_SELFTEST=ui-error`；
  - 15 秒后进程仍在，窗口可正常显示和操作。这个测试实例没有带后端，所以窗口提示“找不到后端程序”，属于预期。
  - `desktop-app.log` 的第一条记录：`ui v0.6.2.0 0x80070057 System.ArgumentException`，调用栈指向 `MainWindow.xaml.cs:line 52`（自检开关），经由 `RoutedEventHandler.Do_Abi_Invoke` 从 XAML 进入。这与 6 次闪退里的错误码相同。
  - `GetApplicationRestartSettings` 读回：hr=0，命令行 `--background`，标志 12。
  - 检查完只停止了这个测试进程，用户正在用的桌面端（pid 76040）和后台（pid 38212）没有动。
- 崩溃分析的依据：
  - Windows 事件日志：40 天内 6 次 APPCRASH，签名都相同（Microsoft.UI.Xaml.dll 0xc000027b，偏移 0x3a9dbd，P7=80070057）。
  - 崩溃转储 `WinhandDesktop.exe.77700.dmp`：
    - stowed exception：SE02，hr=0x80070057，嵌套类型 'XAML'；原始调用栈和嵌套信息都没有存进转储。
    - 界面线程的栈用微软公开符号解析后是：`FailFastWithStowedExceptions` ← `ErrorHelper::ProcessUnhandledError` ← `FinalUnhandledErrorDetectedRegistration::OnFinalUnhandledErrorDetected` ← `ErrorHelper::ReportUnhandledError` ← （XAML 冷代码段）← `CXcpDispatcher::OnReentrancyProtectedWindowMessage` ← `CXcpDispatcher::MessageTimerCallback` ← XAML 内部 DispatcherQueueTimer。
    - 托管栈只有 `Program.Main` → `Application.Start`。

**待真机验收（装上 0.6.2 后）**：C3、D3。

**风险**
- 闪退的具体触发调用没有确认。现在的修复保证同类错误不再结束程序，并把调用栈记下来。“活动”列表改为 ListViewItem 只是降低概率的加固。
- 如果 XAML 把某个错误标成不可恢复，`Handled` 也拦不住。这时由 Windows 的崩溃后重启兜底：进程要运行满 60 秒，重启后回到托盘，后台和中转连接随之恢复。

**独立 Verifier 结论（第 1 轮）**

| 验收项 | 结论 |
|---|---|
| C1 | 通过。新测试 4 个都过；换回 cc96e9a 的 `screen.py` 后 3 个失败（两个 IndexError，一个右半被盖掉的情况显示成“中”），没被压住的那个通过 |
| C2 | 通过。日志 sha256 一致。按 1、7、512、4096 个字符一段回放，每段后读屏幕和光标行，0 次失败。旧代码在 pyte `screens.py:241` 抛 IndexError：按 512 一段是第 107,008 个字符后，逐字符是第 106,951 个。旧代码没出错的 16,297 次读取里，新旧输出完全相同 |
| C3 | 待真机 |
| D1 | 通过（代码和记录的证据）。写日志不会再抛错；启动失败时 `_window` 为空，照常退出；提示放进队列执行，不在出错的回调里改布局；`row.Root` 的所有用法都改成了 ListViewItem |
| D2 | 通过。标志 4\|8 只关掉了补丁、系统更新两种重启，崩溃和无响应后的重启保留；命令行与 `Program.Main` 读的 `--background` 一致 |
| D3 | 待真机 |
| E1 | 通过，但 `winhand bridge init` 的第二种方式漏改（见下表 4） |
| F1 | 本地检查通过（ruff、pytest 654 passed，51 skipped）；CI 由 Builder 记录（见上） |

Verifier 未能验证的：
- 标记已处理能否拦住真实崩溃那条路。转储里是 XAML 定时器上来的原生 E_INVALIDARG，自检只覆盖了从 Loaded 事件抛出的托管异常。
- 0xc000027b 这种强制退出之后，Windows 是否真的重启、是否先弹错误报告。

| # | 问题 | 级别 | 处理（第 2 轮提交） |
|---|---|---|---|
| 1 | pyte 把紧跟宽字符的组合符号放进占位格，新渲染把它当成“右半被盖掉”，例如 `か` + U+3099 显示成“ ゙”，旧代码至少显示“か” | 低 | 占位格里是零宽字符时，把它合回前一个字（NFC），显示“が”；以零宽字符开头的格子当作孤立的占位格，显示空格。新增两个测试用例 |
| 2 | 界面错误的提示与后端提示共用一个位置：启动阶段接住的错误，提示会被后端启动成功时的 `HideNotice()` 清掉 | 低 | 后端启动成功时只清掉它自己的“后端启动失败”；后端启动失败的提示仍然优先 |
| 3 | “不会循环重启”说得过满：每次运行一分钟以上才崩的，Windows 每次都会重启 | 低 | brief 和代码注释改为准确的说法 |
| 4 | `winhand bridge init` 第二种方式没写“选 Custom、勾上 Also include default list…” | 低 | 已补上 |

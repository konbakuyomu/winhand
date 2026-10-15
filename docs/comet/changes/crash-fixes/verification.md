# 验收记录：crash-fixes（0.6.2）

## 第 1 轮（dc3f8b3、a7e997e）

**正式检查**
- 云端（Linux）：`ruff check`、`ruff format --check` 通过；`pytest`：654 passed，51 skipped。
- CI（dc3f8b3）：`binary.yml`（两个平台的打包冒烟）通过；`agent.yml`、`desktop.yml` 见下方补记。
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

# winhand 桌面端（Windows 托盘 App）

托盘 App 是 winhand 的日常形态：登录后在通知区域运行，自动连接中转，并实时显示连接状态、Claude 的每次工具调用和打开的会话。它取代手动开终端运行 `winhand connect`。

- **无需 Python/.NET**：App 是自包含的 WinUI 3 程序；后端是 PyInstaller 打包的 `backend\winhand.exe`。打包时会在去掉所有 Python 的环境里做冒烟测试。
- **页面**：概览（连接状态、今日调用、最近活动）、活动（时间线，可筛选、搜索，看参数和结果）、会话（查看或停止会话、打开日志）、设置（中转地址与令牌、开机自启、本机数据目录）。
- **托盘**：已连接时显示彩色图标，否则显示灰色图标，提示文字写明状态。左键打开窗口，右键可以重新连接、断开或退出。关闭窗口只会隐藏到托盘。
- **开机自启**：写入当前用户的 `HKCU\...\Run`，带 `--background` 参数启动，不弹窗口。
- **与终端版的关系**：两者共用 `~/.winhand/connect.lock`，同一台机器只会有一个 agent 连中转。审计日志在 `~/.winhand/activity/`。
- **协议**：见 [PROTOCOL.md](PROTOCOL.md)。

## 构建

```powershell
./desktop/scripts/Build-Windows.ps1 -Zip -Installer
```

产物：`desktop/out/app/`（可直接运行）、便携 zip，以及 `desktop/out/releases/winhand-win-Setup.exe`（Velopack 安装器，装到 `%LocalAppData%\winhand` 并创建快捷方式）。CI 的 `desktop.yml` 生成相同的产物。

开发时可以让 App 直接使用源码后端：

```powershell
$env:WINHAND_BACKEND_PATH = (Get-Command uv).Source
$env:WINHAND_BACKEND_ARGS = 'run --project D:\path\to\winhand\agent winhand'
```

图标和看板娘由 `uv run desktop/scripts/build_icons.py` 从 `assets/branding/source.webp` 生成。

# 调研：本机 MCP 服务启动失败的根因（uv 项目的虚拟环境）

在用户电脑（MSI，uv 0.13.0，mise 管理的 Python）上实测，2026-10-13。

## 事故

- freecad-mcp、pyocd-debug、usb-camera-mcp 都用 `uv --directory <项目> run [--locked] <入口>` 启动，测试按钮报 `MCPError: Connection closed`。服务的最后输出是 `No Python at '"C:\Users\dxt98\AppData\Local\mise\installs\python\3.12.14\python.exe'`（引号是 venv 启动器自己加的）。
- 三个项目的 `.venv\pyvenv.cfg` 都是 `home = …\mise\installs\python\3.12.14`，而这个目录已不存在：
  - mise 2026-10-05 安装了 3.12.15；
  - 2026-10-08 把 `3.12` 指向 3.12.15，并删除了 3.12.14。
- eshop-mcp、fast-context 用 npx 启动，不受影响。

## 复现（`%TEMP%\winhand-venv-probe`）

1. 把 uv 自带的 Python 3.11 复制到临时目录，用它建一个项目的 `.venv`，`uv run python` 正常。
2. 把那份 Python 改名移走，模拟被删除：
   - `uv run python …` 失败：`No Python at '"…\py311\python.exe'`，退出码 103；
   - `uv sync` 显示 `Checked`，退出码 0，什么也没修。

**结论**：基础 Python 被删掉后，uv 不会发现项目的虚拟环境已经坏了，`uv run` 每次都失败，`uv sync` 也不修。[推断] uv 按启动器路径缓存了解释器信息，而 Windows 上 venv 的 `python.exe` 是一个不变的启动器副本。

## 修复方法（实测有效）

- 在项目目录里先 `uv venv --clear`，再 `uv sync`：
  - 重建后 `uv run` 正常；
  - 新环境按项目的 `.python-version` / `requires-python` 选 Python。
- 用 uv 自己安装的 Python 时，`pyvenv.cfg` 的 `home` 是 `…\uv\python\cpython-3.12-windows-x86_64-none`。这是 uv 维护的目录联接，指向当前的 3.12.x，小版本升级后仍然有效。
- 用户的三个项目已用 `uv sync --locked --managed-python --python 3.12` 手动修好，见 `docs/comet/archive/2026-10-13-local-skills-tools/verification.md`。

## 判断方法

- 虚拟环境的 `pyvenv.cfg` 里 `home` 指向基础 Python 所在的目录。
- 那个目录里没有 Python 可执行文件（Windows 上是 `python.exe`，其他系统是 `python3` 或 `python`），这个虚拟环境就坏了。
- 这个判断不依赖 uv 的缓存。

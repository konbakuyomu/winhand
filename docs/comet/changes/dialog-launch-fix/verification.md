# 验收记录：dialog-launch-fix（0.6.1）

## 第 1 轮（76f9f5d）

**正式检查**
- `ruff check`、`ruff format --check` 通过；`pytest`：Linux 650 passed，51 skipped。
- CI 都通过：`agent.yml`（ubuntu、windows-latest）、`binary.yml`（两个平台的打包冒烟）、`desktop.yml`（编译、打包、安装包）。
- 用户电脑（Windows 11）：
  - 新测试 `test_a_dialog_opens_while_another_thread_reads_stdin` 在 0.6.0 的代码上失败：等 20 秒后报“nobody answered”，与真机现象一致；在 76f9f5d 上通过。
  - `tests/test_dialog_launch.py` 加 `tests/test_approvals.py`：47 passed，3 skipped（跳过的需要建符号链接的特权）。

**独立 Verifier 结论**

| 验收项 | 结论 |
|---|---|
| B1 | 通过。四种弹窗（密码、选文件、确认、授权）都经过 `_run_dialog`，都不再继承标准输入；新测试在 Windows CI 和用户电脑上都是修复前失败、修复后通过 |
| B2 | 要等装上 0.6.1 的真机验收 |
| B3 | 编译通过；屏幕上的效果要等真机。0.6.0 显示 0.6.1 的更新说明时用的还是旧的显示方式，新的显示从下一次更新开始可见 |
| B4 | 通过 |

Verifier 查了其余所有启动子进程的地方：都已明确指定标准输入，或者本次已改为空输入。只有 ConPTY 会话（pywinpty）在 Linux 上没法确认，留到真机：在桌面端下开一个 python 会话试一下。

| # | 问题 | 级别 | 处理（第 2 轮提交） |
|---|---|---|---|
| 1 | 去掉 `__` 会把 `__init__.py`、`__version__` 这类名字弄坏 | 低 | 不再去掉 `__` |
| 2 | 单独一行的 `---`、`***` 分隔线会显示成正文 | 低 | 跳过 |
| 3 | 每行是单独可选的文字，不能一次选中全部说明 | 低 | 保留：说明不长，逐行复制够用 |
| 4 | `ReleaseNotes.Parse` 没有 C# 单元测试 | 低 | 保留：仓库目前没有 C# 测试项目；编译由 `desktop.yml` 检查，效果在真机验收里看 |

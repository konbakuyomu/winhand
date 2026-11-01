---
kind: issue
title: "任务命令里的 exit 照常记下退出码"
type: ff
status: closed
created: 2026-11-01
---

# 任务命令里的 exit 照常记下退出码

原来 `job_start` 的命令写在包装脚本里一起运行，命令里一写 `exit N`，包装脚本也跟着结束，来不及写 `exit.json`：任务显示为 vanished，也没有退出码。这是 11-01 实测时碰到的；调用日志里 5 天出现了 7 次，1174 个任务目录里有 10 个没有 `exit.json`。

- 改动：`agent/src/winhand/jobs.py` — 命令单独写成 `command.ps1`，包装脚本用 `& '<路径>'` 调它。命令里的 `exit N` 只结束命令本身，包装脚本照常记下 N。
- 验证：
  - `agent/tests/test_review_round4.py::test_a_job_command_runs_as_its_own_script`：命令写进了 `command.ps1`，`job.ps1` 里没有命令原文。
  - `agent/tests/test_windows_scenarios.py::test_exit_in_a_job_command_still_records_its_code`（只在 Windows 上跑）：命令以 `exit 4` 结尾，任务是 finished、退出码 4、输出完整。
  - 0.10.1 装上后在用户电脑上实测：`Write-Output 'before exit'; exit 4` 的任务是 finished、退出码 4、输出完整。
- codestable：无影响（README 写明了）。

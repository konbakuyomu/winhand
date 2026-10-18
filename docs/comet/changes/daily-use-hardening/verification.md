# 验收记录：daily-use-hardening（0.7.0）

## 第 1 轮（云端，2026-10-18）

**改动**：见 brief 的 Scope，提交 fcf5238、c0b7aa5、7e2c95b、98c8dc6、6187ee4、93ef5c1、0085a23、2bcb0b5（结果预算）、84f0a32（`ui` 鼠标点击前的检查）、5626ac6（状态页），以及本记录所在的提交（文档和版本号 0.7.0）。

**检查**
- agent（Linux）：`uv run pytest -q` 723 passed、54 skipped（Windows 专属的测试跳过）；`ruff check`、`ruff format --check` 通过。
- relay：`npm run typecheck` 通过；`npm test` 38 passed（新增 7 个续传测试）。
- `wrangler dev` 上的 `relay/test/e2e.py`：61 项 ALL PASSED。其中：
  - `a call in flight survives the connection dropping`：调用进行到一半时切断 agent 与中转之间的连接，agent 重连后客户端收到完整回复（'前半\n后半\n'）；
  - 加上本机端口口令和 Host/Origin 校验之后，经隧道的工具调用、一次性文件链接、本机 MCP 服务、会话开场 hook 都照常。

**逐项结论**

| 验收项 | 结论 | 依据 |
|---|---|---|
| A1 | 通过 | e2e 如上；`test_resume.py` 6 项：续传、中转放弃时取消、无人续传时 60 秒后结束、回复过大时随连接结束、旧中转（没有 welcome）照旧报错 |
| A2 | 通过 | `test_the_local_port_answers_only_the_tunnel`：不带口令 403、口令不对 403、Host 不对 421；`test_a_tunnel_request_passes_with_the_secret`；e2e 全部经隧道的调用通过 |
| A3 | 通过 | `test_device_token_and_passphrase_are_masked`、`test_known_token_formats_are_masked`、`test_known_secrets_can_be_turned_off`、`test_a_binary_file_holding_a_masked_value_is_not_sent` |
| A4 | 通过 | `test_daily_use.py` 39 项，按之前会话里失败过的真实调用写成 |
| A5 | 通过 | `test_a_chinese_file_is_read_in_parts_a_client_takes`、`test_a_listing_of_long_chinese_names_fits_one_result`、`test_search_results_fit_one_result`、`test_session_output_pages_by_what_it_costs`、`test_a_running_commands_chinese_output_is_tailed_by_what_it_costs`、`test_a_chinese_skill_is_cut_at_what_one_result_holds` |
| A6 | 通过 | `test_ocr_click_does_not_guess_between_places_showing_the_same_text`、`test_ui_does_not_guess_between_controls_of_the_same_name` |
| A7 | 见下 | Linux 上全部通过；Windows 上的结果见“Windows CI” |

**没做的**
- 没在用户电脑上跑测试：Windows 场景测试会动真实的鼠标、剪贴板和凭据管理器，用户可能正在用电脑。改在 GitHub Actions 的 windows-latest 上跑。
- 没部署中转、没发布 0.7.0：要用户确认。

**风险**
- 单次结果从 6 万字符降到 4 万字节：英文为主的长文件，一次读到的行数少了三分之一左右，要多翻一页。
- `history()` 的 `this_session` 只在客户端带 `Mcp-Session-Id` 时才有；用新协议（没有会话）的客户端只看得到 `client`。
- 同名目标不再默认点第一个：以前靠“默认第一个”的调用，现在要多给一个 `index`。

## Windows CI

（待补：workflow_dispatch 在本分支上的 agent.yml 结果）

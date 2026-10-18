# 验收记录：harmonia-secrets

## 第 1 轮（云端，2026-10-19）

**检查**
- `agent/tests/test_harmonia.py` 10 项，用假的 `harmonia` 命令行（按 Harmonia 724411f 的输出格式）。
- 全量：`uv run pytest -q` 739 passed、54 skipped；模拟没装 ripgrep 时 734 passed、59 skipped；`ruff check`、`ruff format --check` 通过。

**逐项结论**

| 验收项 | 结论 | 依据 |
|---|---|---|
| H1 | 通过 | `test_every_value_harmonia_gives_this_machine_is_masked`：Test、Prod 两个环境的 `SHARED` 都显示为 `«redacted:harmonia:SHARED»`；每个环境导出一次；短于 6 个字符的值不遮挡 |
| H2 | 通过 | `test_a_key_added_on_the_phone_is_masked_in_the_next_result`：内容没变的同步不重读；新加的 `GITHUB_TOKEN` 在下一个结果里被遮挡 |
| H3 | 通过 | `test_without_a_connected_harmonia_nothing_is_read`、`test_masking_harmonia_can_be_turned_off` |
| H4 | 通过 | `test_harmonias_files_are_off_limits`、`test_a_search_never_returns_harmonias_device_keys` |
| H5 | 通过 | `test_secret_prompt_stores_a_new_key_in_harmonia` |
| H6 | 通过 | 全量测试如上 |

**没做的**
- 没在真实的 Harmonia 上跑：用户的电脑上还没装。装好、接入之后，在电脑上核对一遍：`context()` 列出环境；`harmonia exec` 运行一个打印变量的命令，结果里是 `«redacted:harmonia:…»`；`fs_read ~/.harmonia/keys.json` 被拒绝。

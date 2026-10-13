# 验收记录：local-mcp-reliability

## 第 1 轮（c64f5d2）

**正式检查**
- `ruff check`、`ruff format --check` 通过；`pytest` 531 passed，40 skipped（Linux）。
- CI：`agent.yml`（run 38326400818）ubuntu 通过；**windows 有 4 个错误**，都出在新测试的 fixture 里（下表 1）。

**独立 Verifier 结论**：

| 验收项 | 结论 |
|---|---|
| A1、A2、A5、A8 | 通过 |
| A3、A4、A7 | Linux 通过，Windows 未验证（下表 1） |
| A6 | **未通过**：自动修复开启时，给出的修法不对（下表 2） |

核对方法：
- 对工具段做了 900 次随机压测，没有超出预算；
- 全部放得下时，输出与 0.4.8 相同；
- 用真实的 uv 试了各种项目形状。

| # | 问题 | 级别 | 处理（第 2 轮） |
|---|---|---|---|
| 1 | 测试用 Windows 路径作 `re.sub` 的替换串，在 Windows 上报 `bad escape \U`，四个真 uv 测试都没跑 | 阻断 | 替换串改为函数；在 POSIX 上还把环境里的 python 链接改成指向不存在的位置，才是真的坏掉 |
| 2 | 自动修复开启时，`No Python at` 的说明写“下次启动前重建”，但 winhand 检查的环境是好的（服务用了别的环境，例如 workspace 的根、子目录、`--active`），说法不对，也没给命令 | 应修 | 分三种情况说明，环境完好时说明服务用了别的环境，并给出两条命令 |
| 3 | 测试日志是追加写入的，测试按钮会把以前的 `No Python at` 当成这次失败的原因 | 应修 | 只看这次测试新写入的内容 |
| 4 | POSIX 上基础目录只有 `python3.11` 时，好的环境被判为坏掉并重建，而且之后每次都重建 | 应修 | POSIX 先看环境里的 `bin/python` 能否指到文件；基础目录里 `python3*` 也算；Windows 上任何 `python*.exe` 都算 |
| 5 | 工具段的后备步骤随服务数的三次方变慢（100 个服务 × 40 个工具要 31 秒，超过中转的 15 秒） | 应修 | 改为累加各块长度，只在最后生成一次文字 |
| 6 | A2 场景写“前面的服务每个工具都有说明”，与 R1 的步骤（先保证都列出名字）不符 | 应修（规格） | 场景按 R1 的步骤改写，并补 3 个服务和 100 个服务的场景 |
| 7 | 参数解析：全局的 `--managed-python` 丢失；`--config-file`、`--offline` 等没带到重建；不少带值的 `uv run` 选项没列出；`-s`、`--active`、`UV_NO_PROJECT`、`UV_PROJECT`、`UV_WORKING_DIR` 没考虑；`-p3.12` 丢失 | 小 | 都按 uv 的写法处理，见规格 R2 |
| 8 | 目录里有 `.venv` 但没有 `pyproject.toml` 时，重建后 `uv sync` 失败 | 小 | 向上找 `pyproject.toml`，找不到就不检查；也处理 workspace 成员（环境在 workspace 的根） |
| 9 | Windows 上超时后 `communicate()` 可能被子进程拖住；超时的部分输出丢失；运行不了 uv 时没记日志；修复中关闭 agent 要等线程 | 小 | 超时时结束整个进程树并带上已有输出；每一步都记日志；修复改在独立的守护线程里 |
| 10 | 桌面端的请求 30 秒超时，修复更久时测试对话框拿不到结果 | 小 | 保留：修复照常完成，再点一次测试即可（实测重建 4–16 秒） |
| 11 | 修复期间服务被替换，修完仍会启动一个没人管的进程 | 小 | 服务被关闭后，修完不再启动 |
| 12 | `desktop/PROTOCOL.md` 没提 `repair_env` | 外观 | 已补 |

## 第 2 轮（7798737）

**正式检查**
- `ruff check`、`ruff format --check` 通过；`pytest` 541 passed，40 skipped（Linux）。
- CI：`agent.yml`（run 38328748836）ubuntu 541 / 40、windows 558 / 23，全部通过；四个真 uv 测试在 windows-latest（uv 0.13.0）上通过。
- 用户电脑（uv 0.13.0）：
  - 在临时克隆里运行 `tests/test_local_mcp_reliability.py`：28 passed，1 skipped（只在 POSIX 上跑的那个）；
  - 用新代码只读检查用户的 5 个服务：两个 npx 服务不检查；三个 uv 服务都找到了正确的项目目录，`--locked` 也认了出来，而且都判为完好，不会误重建。

**独立 Verifier 结论**：

- A1–A8 全部通过；
- 第 1 轮的 12 条：10 条修好，1 条部分修好（下表 2），1 条按原处理接受；
- 新发现 9 条，都是小问题。

核对方法：
- 用真实的 uv 逐项实验；
- 600 次随机压测，与上一版对比；
- 对 34 种参数写法做了逐项检查。

| # | 问题 | 级别 | 处理（第 3 轮） |
|---|---|---|---|
| 1 | 列出过、但没有工具的服务，带说明的块比只列名字的块还短，后备步骤因此过早跳过服务 | 小 | 没有工具的服务两种写法都写“(no tools)” |
| 2 | `repair_env = false` 而 winhand 检查的环境完好或不存在时，说明仍说那个环境坏了，给出的命令会重建好的环境 | 小 | 不论是否开启自动修复，环境完好或不存在时都说明服务用了别的环境 |
| 3 | `pyproject.toml` 里 `tool` 或 `tool.uv` 不是表时抛 AttributeError，服务停在“starting”，`sys_info(checks=true)` 整个失败 | 小 | 逐层判断是不是表；`prepare`、`checks`、`explain` 遇到意外的配置时照旧启动，不出错 |
| 4 | POSIX 上超时后的输出重复了一遍 | 小 | 第二次读取的输出已包含全部内容，只用它 |
| 5 | 重建在项目目录里运行，而 uv 按服务启动时的目录解析 `--config-file` 等相对路径，重建失败 | 小 | 在服务启动时的目录里运行，并带上服务的 `--directory`、`--project` |
| 6 | `-p=3.13` 被读成 `=3.13`；全局的 `--isolated` 没有排除；`--python-preference`、`--preview-features` 的值被当成命令 | 小 | 都已处理；`--python-preference` 也带到重建 |
| 7 | `--with-requirements` 其实用项目的环境，却被排除 | 小 | 不再排除 |
| 8 | 基础目录列不出内容（没有权限、网络共享离线）时被当成“不存在”，好的环境会被重建 | 小 | 只有目录不存在才算没有 |
| 9 | 链接过来的目录、Windows 上大小写不同的 workspace 成员，找到的环境与 uv 不同（只会检查一个不存在的环境，不会误删） | 小 | 找项目时按真实路径；Windows 上成员匹配不区分大小写 |

## 第 3 轮（1c19e52）

**正式检查**
- `ruff check`、`ruff format --check` 通过；`pytest` 550 passed，40 skipped（Linux）。
- PR #11 的 6 项检查全部通过：
  - agent.yml：ubuntu 550 / 40；windows 566 / 24，uv 0.13.0；
  - binary.yml、desktop.yml、relay.yml。
- 用户电脑（uv 0.13.0）：
  - `tests/test_local_mcp_reliability.py` 和 `tests/test_local_mcp.py`：75 passed，2 skipped（只在 POSIX 上跑的两个）；
  - 用新代码只读检查用户的 5 个服务：判断都正确，没有误判。

**独立 Verifier 结论**：

- A1–A8 全部通过，没有阻断问题；
- 第 2 轮的 9 条：7 条修好，2 条部分修好。其中第 9 条的修法（解析链接、Windows 上不区分大小写）在某些布局下反而与 uv 不一致；
- 新发现 6 条小问题、1 条外观问题。

核对方法：
- 用真实的 uv 0.11.32 跑了 S1–S11 共 13 个场景；
- 对照了 uv 0.11.32 / 0.13.0 的源码（项目和 workspace 的查找、glob 匹配）以及 CPython 的错误码映射；
- 2,000 次随机压测，并与 0.4.8 对比。

| # | 问题 | 级别 | 处理（第 4 轮） |
|---|---|---|---|
| 1 | 对整个路径解析链接，与 uv 不一致：uv 不解析 `--project` 里的链接，Windows 上也不解析当前目录（回归） | 小 | 与 uv 相同：起点目录只在 POSIX 上按真实路径，`--project` 照写下的接上；Windows 上不解析链接 |
| 2 | Windows 上 workspace 成员不区分大小写，而 uv 区分（回归） | 小 | 去掉大小写折叠 |
| 3 | Windows 把离线的网络共享报告为“找不到”（错误 53、67、15），仍被当成已删除 | 小 | 这三个错误码视为“看不到”，判为完好 |
| 4 | 项目外层有一个不是 workspace 的项目时，uv 就此停止查找，winhand 继续往上找 | 小 | 与 uv 相同：遇到有 `[project]` 而没有 workspace 的 `pyproject.toml` 就停 |
| 5 | `pyvenv.cfg` 的 `home` 里有 NUL 字符时抛 ValueError，服务停在“starting” | 小 | 读不了、列不出时一律判为完好 |
| 6 | 测试会读开发者自己设置的 `UV_*` 环境变量 | 小（测试） | 测试开始前清掉这些变量 |
| 7 | 外观：规格没写“(no tools)”；说明里括号套括号；服务没有 `cwd` 时，手动修复的说明写出 winhand 自己的当前目录 | 外观 | 规格补上；改写说明；没有 `cwd` 时不写目录（修复本身仍在服务启动时的目录里运行） |

这些修正都很小，没有再请 Verifier 复查。每一条都加了测试，复现 Verifier 给出的场景：
- 第 1 条：链接过来的 `--project` 与 `--directory`；
- 第 2 条：大小写不同的成员；
- 第 3、5 条：错误码 53、NUL 字符；
- 第 4 条：外层有独立项目。

以上用本地全部测试和 CI 确认。

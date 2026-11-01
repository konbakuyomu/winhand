# Agent Note: 桌面操作给主人让路，截图与卡住提醒

Status: implemented

## Problem

2026-11-01 的使用复盘（`docs/reviews/2026-11-01-usage-review.md` 第四节 8、第五节）里，桌面工具调用不多，出错却多：

- `ui` 13/95、`ocr` 11/97、`screenshot` 12/219、`window` 8/82；
- 截最小化窗口失败 11 次：只能先还原再截，而还原要授权；
- 没给窗口的 `screenshot(wait="stable")` 四次都等到超时（20–30 秒）：别处在放视频；
- 一次 `git commit` 卡在 1Password 的签名窗口上，没人去点。

另外两件事来自同类项目，不是复盘里数出来的故障：

- **AI 和人同时动鼠标键盘。** Windows-MCP（CursorTouch）在这时把控制交还给人。它的做法：人一动真鼠标、或按急停键就收回控制，空闲 10 秒再交还；AI 操作时屏幕边缘发光。WinHand 没有这一层，人在电脑前时，AI 的点击会落在人正在做的事中间。
- **按编号操作控件、一次做几步。** 这来自 FlaUI-MCP 的 `ref` 和 `windows_batch`，以及 Anthropic computer use 的动作序列。

## Decision

- **判断“人在用电脑”。** 用 `GetLastInputInfo` 读最近一次输入的时刻（`presence.owner_input_ago`）：
  - WinHand 自己发的输入，在 `desktop._send` 和切前台用的 Alt 键之后由 `presence.note_sent()` 记下；500 毫秒内看到的输入算自己的。
  - 人在 WinHand 弹窗里的点击和输入（`secret_prompt._run_dialog` 外面包着 `presence.dialog()`）也不算。
  - 其余输入在 `owner_idle_s`（默认 5 秒）以内，就算人在用。
- **桌面动作先等人停手。** `input`、`ui` 的动作、`ocr` 的点击、`window` 的操作都包在 `presence.acting()` 里：
  - 人在用时，每 0.25 秒看一次，最多等 `WAIT_S`（10 秒），还在用就报 `OwnerBusy`，什么也不做；
  - 嵌套的调用（`ui` 点击里的 `input`）只在最外层等一次。
- **急停。** 后台线程用 `RegisterHotKey` 注册 Ctrl+Alt+Shift+Backspace：
  - WinHand 一启动就注册（`build_server` 里调 `presence.start()`）；
  - 按下后 `STOP_S`（60 秒）内，桌面动作一律报 `OwnerStopped`；
  - 按下的时间写进 `~/.winhand/desktop-stopped`，同一台电脑上的其他 WinHand 进程也读它。热键只能由一个进程持有，没抢到的进程在每次桌面动作时再试；
  - `ui(steps=[...])` 从下一步起停下。
- **边框。** 同一个线程里，每个显示器四条细窗口：
  - 窗口样式：置顶、工具窗口、分层、对鼠标透明、不抢焦点；
  - 窗口过程直接用 `DefWindowProcW` 的地址，靠类画刷上色，没有 Python 回调进 Windows 的窗口机制；
  - 每条都设 `SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)`，设不上就整个不显示边框；
  - 最后一个动作之后 2 秒隐藏。
  - `desktop.windows()` 本来就跳过工具窗口，边框不会出现在窗口列表里。
- **截最小化的窗口。** 截图时先 `SW_SHOWNOACTIVATE` 显示，截完用保存的 `WINDOWPLACEMENT` 放回去（`showCmd = SW_SHOWMINNOACTIVE`，保留“还原时最大化”）。不弹授权：这是读，焦点不动，状态原样放回。截图说明里写明这件事。
- **画面稳定。** 没给目标时，`wait="stable"` 只比较最前面那个窗口的区域；超时时用 `kept_changing` 报一直在变的区域和窗口。
- **命令被别的程序的窗口卡住。**
  - `run` 和 `job_start` 开始时记下顶层窗口句柄（`jobwatch.remember_windows`，只枚举句柄，不取详情）。
  - 命令超过 5 秒没输出时，`prompts_since` 找出之后新出现、像在要人回应的窗口：对话框类、有所有者的窗口，或已知的登录、签名、口令程序。
  - 这些窗口在 `prompts` 里列出，并用 `FlashWindowEx` 让它的任务栏按钮闪到人切过去，每个只闪一次；安全桌面（UAC）另写一句。
  - 长等待遇到它们提前返回（`matched: prompt / secure_screen`）。
- **`ui` 的 ref。**
  - 每次 inspect 把整份控件列表按顺序编号 `e1…`，存在内存里（每个窗口一份，最多 8 个窗口）。
  - 按 ref 操作时：有活的 UI Automation 元素就直接重读它；走 PowerShell 读法时，按运行时 id、窗口句柄或 automation id 在新读数里找回同一个。找不到就报“窗口变了，重新 inspect”。

## Alternatives considered

- **用全局低级钩子（`WH_MOUSE_LL` / `WH_KEYBOARD_LL`）区分真输入和注入的输入。** 最强的理由：`LLMHF_INJECTED` 标记是准的，不用靠时间窗猜。否掉的原因：系统里每一次鼠标移动都要跑一遍这个 Python 回调；回调慢了，Windows 会让所有人的输入一起卡（`LowLevelHooksTimeout`）；回调里出一次错就能拖垮 WinHand。按时间窗判断的误差只在 WinHand 刚发完输入的半秒之内。
- **边框交给桌面端（C#）画。** 最强的理由：界面本来就归桌面端管，样式好调。否掉的原因：WinHand 也会在没有桌面端时运行（stdio、HTTP）；WinUI 3 做一个点击穿透的置顶覆盖层，要另外的互操作代码，这里既编不了也测不了。以后桌面端要接手，`presence` 的显示和隐藏是唯一要换的地方。
- **最小化的窗口继续拒绝截图，让 AI 先还原。** 最强的理由：没有人同意，任何窗口的状态都不变。否掉的原因：复盘里失败 11 次，还原又要授权；这里不抢焦点、截完原样放回，对人的打扰只是窗口闪现一下。
- **卡住提醒只看任务自己的进程树（第 2 轮的做法）。** 最强的理由：不会把无关程序的对话框误报成“命令在等它”。否掉的原因：签名、登录窗口多半属于别的进程（1Password、`CredentialUIBroker.exe`）。所以加了两个条件：命令已经安静 5 秒，窗口是在命令开始之后才出现的；并在说明里写“可能”。
- **提醒人用桌面端托盘气泡。** 最强的理由：文字说得清。否掉的原因：要改 C#，而且没有桌面端时就没人提醒。让卡住命令的那个窗口自己闪，直接指向要人回应的东西。
- **急停键按两次恢复。** 最强的理由：人不用等 1 分钟。否掉的原因：手忙脚乱时按了两次，会在人以为已经停下时又放开。固定 60 秒，`presence.resume()` 留给桌面端以后接一个“继续”按钮。

## Consequences

- **收益**：
  - 人在电脑前时，AI 的点击和按键不会插进人的操作里，人有一个随手可按的停止键；
  - 最小化的窗口能直接截图和读字；
  - 视频之类的动画不再拖垮“等画面稳定”；
  - 命令卡在签名、登录窗口上时，AI 和人都能知道；
  - 一次调用能做完填表、勾选、点按钮几步。
- **代价与已知上限**：
  - 人一边看一边动鼠标时，AI 每个桌面动作会多等几秒；嫌慢可以把 `owner_idle_s` 调小，或者关掉 `yield_to_owner`。
  - `GetLastInputInfo` 分不清输入来自谁：别的远程控制程序发来的输入，也算人在用。
  - 急停后 1 分钟内，对话里没有办法提前恢复。
  - 判断“像在要人回应的窗口”靠窗口类、所有者和一份程序名单，名单外的密码管理器认不出来，无关程序的对话框也可能被报出来。
  - 截最小化窗口时，窗口会在屏幕上出现约半秒。
  - 急停键只有一个进程能持有。其他进程靠停止文件跟着停；持有的进程退出后，要等别的进程下一次桌面动作才会重新注册。
  - 这些都在 Linux 上靠模拟测试，真正的窗口行为要靠 CI 的 Windows 任务和在电脑上实测。

## Verification

- `agent/tests/test_review_round4.py`：
  - 过滤、ref、steps、新窗口，以及窗口在读的过程中变了；
  - OCR 忽略标点；修饰键和新的 `input` 动作；窗口内坐标；截图存文件；
  - 人在用时等待和放弃、自己的输入和弹窗不算、可以关掉、急停；
  - 命令被别的程序的窗口卡住、长等待提前返回。
- `agent/tests/test_windows_scenarios.py`（只在 Windows 上跑）：
  - 一次调用完成填写、勾选、点击；
  - 按 ref 填写；
  - 截最小化窗口后它仍是最小化；
  - 按窗口内像素点击，以及光标位置；
  - 边框出现、不在窗口列表里，也不在截图里。

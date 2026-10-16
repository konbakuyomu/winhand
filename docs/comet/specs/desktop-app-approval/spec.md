# desktop-app-approval：桌面操作按程序授权

依据：brief.md。D1–D4 是用户 2026-10-13 的选择；D5–D8 由用户在确认摘要中同意；D9、D10 是 Build 中补的实现决定（见 brief）。

### R1 哪些调用要检查，按什么顺序

- **要检查的操作**：
  - `input`：click、double_click、right_click、drag、scroll、hscroll、mouse_down、mouse_up、keys、type。move 不检查；
  - `ui`：inspect 和 wait 以外的动作；
  - `ocr`：click、double_click、right_click。move 和读取类动作不检查（指定窗口的 move 也不检查，也不为它把窗口切到前台）；
  - `window`：focus、minimize、maximize、restore、close、move。
- **不检查**：`screenshot`、`ocr` 的读取类动作、`ui` 的 inspect 和 wait、`window` 的 list 和 wait、`clipboard`。
- **顺序**：在发出任何输入、执行任何动作之前，依次判断：
  1. 桌面状态，同 0.5.0；
  2. WinHand 自己的窗口（R6），拒绝；
  3. 管理员窗口，同 0.5.0；
  4. 终端（R5）；
  5. 授权（R3、R4）。
- 授权关闭（R7）时，第 4、5 步跳过，第 1–3 步照常。
- 被拒绝时什么都不发送，工具返回 `{"error": ...}`。
- 参数错误（缺 keys 或 text、未知按键名或动作、`ui` 找不到控件、控件不可用）先报出来，不为它弹窗；`ui` 在找到要操作的控件之后、动手之前才检查授权，弹窗里写的是那个控件的名字。

Scenario: (A5) 只读不问
- 设置：一个没授权的程序。
- 结果：
  - `screenshot`、`ocr` read、`ui` inspect 不弹窗；
  - `input` click、`ui` invoke、`ocr` click、`window` close 先走授权。

### R2 识别程序

- 程序 = 一个顶层窗口所属进程的 exe 文件名，小写，例如 `notepad.exe`。
- 用哪个窗口：
  - 鼠标类动作：坐标处的顶层窗口；拖动看起点和终点；
  - keys，以及不带坐标的 type：前台窗口；带坐标的 type：坐标处的窗口；
  - 组合键里有 win 键时：Windows 外壳，即任务栏窗口所属的 explorer.exe；
  - `ui`、`window`：指定的窗口；
  - `ocr` 点击：指定的窗口，然后看点击坐标处的窗口。
- 一次工具调用可能涉及两个程序（拖动的起点和终点、Win 组合键的外壳和前台窗口、`ui` click 退回鼠标点击）：这些检查共用一次最多约 40 秒的等待，整个调用仍在客户端的时限内回答。
- 坐标不在任何屏幕上时（包括两块屏幕之间的空隙）直接拒绝（Windows 会把它挪到屏幕边上，落到那里的窗口上）；读不到鼠标位置、或按键时没有前台窗口，也拒绝。认不出窗口属于哪个程序（例如窗口正在关闭）时拒绝，并说明原因。
- 按键序列（如 `alt+tab ctrl+w`）按开始时的前台窗口判断；序列中途切到的窗口不再单独检查（已知限制）。
- UWP 应用：窗口属于 ApplicationFrameHost.exe 时，改用它里面第一个属于别的进程的子窗口所在的进程。
- 显示名：exe 的“文件说明”（版本信息），没有时用 exe 名。只用于弹窗和设置页。

Scenario: (A6) 识别
- 结果：
  - 记事本的窗口是 `notepad.exe`；
  - `keys='win+r'` 判为 `explorer.exe`；
  - ApplicationFrameHost 下的计算器判为里面的应用进程。

### R3 授权弹窗和等待

- 程序没有授权、也不在拒绝冷却期时，在电脑上弹窗：
  - **标题**：“WinHand：AI 想操作一个程序”。
  - **正文**：程序的显示名和 exe、窗口标题，以及这次的动作，例如“点击 (812, 440)”“输入文字”“按键 ctrl+s”“控件操作：invoke「保存」”“关闭窗口”。来自程序的文字合成一行再截短（显示名 80、标题 120、动作 160 个字符），弹窗不会被撑出屏幕。
  - **提醒**（R4 的提醒类程序才有）：一行红字。
  - **按钮**：拒绝、允许到 WinHand 重启、一直允许。
    - 默认焦点在“拒绝”；回车和空格作用于有焦点的按钮；
    - Esc 和关闭窗口都算拒绝。
  - 窗口置顶，2 分钟没人回答就自己关闭。
- **等待**：调用最多等 40 秒。
  - 期间有回答，按回答处理；
  - 没有回答时返回错误，写明正在等电脑前的人允许、弹窗还开着、等人回答后再调用一次，或者在对话里请人处理。弹窗继续开着，之后的回答照样记下。
  - 弹窗还没出现在屏幕上时（0.6.2 起），错误写明弹窗正在打开、什么都没发送，请几秒后再调用。
- **同一程序同时只有一个弹窗**：后来的调用等同一个弹窗的回答，等待时间同样最多 40 秒。
- **回答的处理**：
  - 允许到 WinHand 重启：记在本进程，执行动作；
  - 一直允许：写入授权文件（R4），执行动作；
  - 拒绝：记下拒绝时间。10 分钟内对这个程序的操作直接拒绝，不弹窗，错误写明人刚拒绝过、请在对话里问人；
  - 2 分钟没人回答，或弹不出窗：这次拒绝，不记住。
- 弹不出窗时，错误写明原因，例如没有桌面会话。
- 弹窗进程起来了，但屏幕上看不到它的窗口（0.6.2 起，等 20 秒）：结束这个进程，按弹不出窗处理。错误写明弹窗没有出现、没有问到人、这不是拒绝，并给出在设置页或用 `winhand apps allow <exe>` 放行的办法。
- 需要弹窗、但屏幕锁着、在 UAC 安全桌面上，或远程桌面已断开（0.6.2 起）时，不弹窗，直接返回锁屏之类的错误（同 0.5.0 的桌面状态错误），因为没人看得到弹窗。已授权的程序不受这一条影响。
- 判断状态和打开弹窗在同一把锁里完成，刚到的回答不会让同一程序再弹一个窗。

Scenario: (A1) 允许到重启
- 设置：模拟弹窗回答“允许到 WinHand 重启”。
- 结果：
  - 第一次点击弹窗一次，点击执行；
  - 第二次点击不弹窗；
  - 清空本进程的记录（相当于重启）后再点击，又会弹窗。

Scenario: (A3) 拒绝与冷却
- 设置：模拟回答“拒绝”。
- 结果：
  - 不发送输入；
  - 10 分钟内再点击，直接拒绝，不弹窗；
  - 把时间拨过 10 分钟后，又会弹窗。

Scenario: (A4) 没人回答
- 设置：模拟弹窗 60 秒后才回答“允许到 WinHand 重启”（测试里缩短时间）。
- 结果：
  - 第一次调用在等待上限时返回“正在等”；
  - 回答之后的调用直接执行；
  - 两个调用同时到达时只弹一个窗。
- 设置：模拟 2 分钟无人回答。
- 结果：拒绝，不记住，下一次调用重新弹窗。

### R4 授权的保存、状态和提醒类

- **授权文件** `~/.winhand/desktop-apps.toml`：
  ```toml
  # 程序授权：……由 WinHand 的授权弹窗、桌面端设置页（程序授权）或 `winhand apps` 写入；AI 的文件工具改不了它。
  ask = true

  [always]
  "notepad.exe" = "2026-10-13T14:30:00"
  ```
  - `ask` 是总开关（R7），没写时视为 true；
  - `[always]` 的键是 exe 名（小写），值是加入的时间；
  - 写入是原子的；文件按修改时间缓存，改了立即生效；
  - 文件坏了时视为开启、没有“一直允许”，弹窗照常，设置页和 `winhand apps` 显示错误；
  - 文件坏了时不改写它（免得冲掉人写的内容）：设置页和命令行的添加、开关报错，请人先修正；弹窗里选“一直允许”时按“允许到 WinHand 重启”处理。
- **状态**：
  - `always`：一直允许；
  - `session`：允许到重启；
  - `denied`：拒绝冷却中；
  - `terminal`：终端，未放开；
  - `ask`：要先问人。
- **提醒类**（只加提醒，照样可以一直允许）：
  - 浏览器：msedge、chrome、firefox、brave、opera、vivaldi、360se、360chrome、qqbrowser、sogouexplorer、iexplore、arc；
  - 密码管理器：1password、keepass、keepassxc、bitwarden、dashlane、enpass、roboform、nordpass、lastpass；
  - 聊天软件：weixin、wechat、wxwork、qq、tim、dingtalk、feishu、lark、telegram、slack、teams、ms-teams、discord、whatsapp、signal；
  - 系统工具：systemsettings、control、mmc、regedit、taskmgr、explorer；
  - 命令行程序：cmd、powershell、pwsh、bash、sh、wsl（它们的图形窗口不算终端，见 R5；提醒允许后它的终端窗口也能被操作）。

Scenario: (A2) 一直允许
- 结果：
  - 文件里出现 `"notepad.exe"`；
  - 换一个新进程（清空内存记录）也不弹窗；
  - 窗口列表里它的 `approval` 为 `always`。

Scenario: (A11) 提醒
- 结果：给 msedge.exe、keepass.exe、weixin.exe、explorer.exe 的弹窗请求里有提醒文字；给 notepad.exe 的没有。

### R5 终端

- 终端是：
  - 所有控制台窗口（窗口类 `ConsoleWindowClass` 或 `PseudoConsoleWindow`），不管它显示的是哪个程序（cmd、PowerShell、python……），按程序算就是这个窗口所属的进程；
  - 终端程序的窗口：windowsterminal、wt、openconsole、conhost、powershell_ise、mintty、wslhost、putty、kitty、alacritty、wezterm-gui、tabby、conemu、conemu64、hyper、mobaxterm、xshell、windterm、termius。
- cmd、powershell 等命令行程序自己的图形窗口（例如 PowerShell 脚本弹出的窗体）不算终端，照常走授权（D10）。
- 不在“一直允许”里时：
  - 不弹窗，直接拒绝；
  - 错误写明：往终端里打字会绕过守卫规则，请用 `run` 或会话工具（session_start 等），守卫规则在那里照样生效；需要时请人把它加入“一直允许”。
- 在“一直允许”里时照常放行。设置页和 `winhand apps allow` 都能添加终端。
- 只有“一直允许”能放开终端：因为某个程序的图形窗口回答了“允许到 WinHand 重启”，它的控制台窗口仍算终端（例如允许了一个 python 写的图形程序，python 的交互窗口仍被拒绝）。

Scenario: (A7) 终端
- 结果：
  - 对 `cmd.exe` 的控制台窗口、`windowsterminal.exe` 窗口的 click、keys 直接拒绝，不调用弹窗；
  - 加入“一直允许”后放行；
  - `powershell.exe` 的图形窗口（不是控制台）照常走授权，弹窗带命令行程序的提醒。

### R6 WinHand 自己的窗口；防止 AI 自己批准

- **WinHand 自己的窗口**：
  - 范围是这些进程的窗口：
    - exe 为 winhand.exe 或 winhanddesktop.exe；
    - 本进程；
    - 源码运行时的对话框：python 类进程（名字以 python 开头，或 py.exe、pyw.exe、uv.exe）且命令行里有 `winhand` 和 `secret-dialog`。
  - R1 里的所有操作一律拒绝，即使授权关闭；
  - 错误写明：这是 WinHand 自己的窗口（授权弹窗、它请人确认的对话框或桌面端），只能由电脑前的人操作；
  - 因为按键按前台窗口判断，弹窗在前台时按键也被拒绝。
- **授权文件不受文件工具改写**：
  - 守卫中间件内置一条：写入、修改、删除、移动类动作的路径就是授权文件时，拒绝，与用户规则无关；
  - 涉及的工具：`fs_write`、`fs_edit`、`fs_apply_patch`、`fs_op`、`fs_write_bytes`、`fs_upload_url`；
  - 路径的各种写法都算：`\\?\` 前缀、`::$DATA` 流后缀、8.3 短名、符号链接和联接点（同守卫规则的 `variants`）；授权文件的路径逐段解析，途经的每个链接（文件夹或文件，符号链接或联接点，嵌套的也算）所在的位置和最终的文件都受保护（最多跟 40 个链接）；
  - 包括整个文件夹：删除、移动它所在的文件夹，或把一个文件夹复制到它所在的文件夹上（会覆盖其中的文件），同样拒绝；在那里新建文件夹（mkdir）不受影响；
  - 错误写明：只有电脑前的人能改授权，可以通过弹窗、设置页或 `winhand apps`。
- **总开关也在授权文件里**（D9），所以文件工具同样改不了它。
- `winhand apps allow <exe>` 先弹确认框（“确定让 AI 一直可以操作 X 吗？”）；人选“是”才写入。`winhand apps off` 同样先弹确认框。授权文件有错时先报错，不弹确认框。
- 已知限制：AI 能用 `run` 跑脚本绕过（直接改文件、模拟按键）。这一层防的是误操作，不是安全边界（brief 的 Non-goals）。

Scenario: (A8) 自己的窗口
- 结果：
  - 对属于 winhand.exe、winhanddesktop.exe，以及命令行含 `secret-dialog` 的进程的窗口，click、keys、`ui` invoke、`window` close 都被拒绝；
  - 授权关闭时也一样。

Scenario: (A9) 防自己批准
- 结果：
  - `fs_write` 授权文件（包括把 `ask` 改成 false），返回拒绝，文件不变；
  - `winhand apps allow notepad.exe` 在确认框选“否”时不写入，选“是”时写入；
  - `winhand apps revoke notepad.exe` 直接移除。

### R7 管理：配置、命令行、桌面端

- **总开关**：授权文件里的 `ask = true | false`（R4，D9）。
  - 没写时视为 true；改了立即生效；
  - 关闭时 WinHand 自己的窗口和管理员窗口仍然不能操作。
- **命令行**：
  - `winhand apps`：列出开关状态、一直允许的程序、终端名单，以及授权文件路径；
  - `winhand apps allow <exe>`：见 R6；
  - `winhand apps revoke <exe>`：从“一直允许”中移除（授权文件有错时，只撤销本进程的“允许到重启”，并报错请人先修正文件）；
  - `winhand apps on`：开启，直接生效；`winhand apps off`：先弹确认框，选“是”才关闭。
- **桌面端协议**（desktop-backend）：
  - 新方法：
    - `apps_get`：返回 `{enabled, always:[{exe,since}], session:[{exe,since}], denied:[{exe,left_s}], terminals[], running:[{exe,title}], file, error?}`；
    - `apps_set_enabled {enabled}`：写授权文件的 `ask`（设置页由人操作，不再弹确认）；文件坏了时错误码 `approvals_file`；
    - `apps_allow {exe}`：设置页由人操作，不再弹确认；结果另带 `added`（规范化后的 exe 名）和 `caution?`；exe 为空时错误码 `invalid_exe`，文件坏了时 `approvals_file`；
    - `apps_revoke {exe}`：同时撤销“一直允许”和“允许到重启”；
  - 新事件 `apps`：授权变化时（回答弹窗、增删）发出，`data` 同 `apps_get`；
  - 快照里加 `apps`；
  - PROTOCOL.md 写明。
- **桌面端设置页**新增“程序授权”一节：
  - 开关“AI 操作程序前先问我”；
  - “一直允许”“允许到 WinHand 重启”两个列表，每行有“撤销”；
  - 一个可输入的下拉框（列出正在运行的程序）和“允许”按钮，用来加入“一直允许”；
  - 说明文字：只管点击、输入、控件操作，截图和读字不受限；终端请让 AI 用命令工具。

Scenario: (A10) 管理
- 结果：
  - `ask = false` 时不弹窗（行为同 0.5.0），WinHand 自己的窗口仍被拒绝；
  - 协议的四个方法：allow 后 always 有它，revoke 后两处都没有，set_enabled 写进授权文件；
  - `winhand apps off` 在确认框选“否”时不关闭；
  - 桌面端（WinUI）能编译。

### R8 AI 看得到的

- `window(action='list')` 的每个窗口加 `approval`，取值为：
  - `always`、`session`、`denied`、`terminal`、`ask`（同 R4）；
  - `winhand`：WinHand 自己的窗口。
  - 授权关闭时，只有 WinHand 自己的窗口带 `approval`。
- instructions 的桌面一段加一句：
  - 第一次点击、输入或操作某个程序时，电脑上会弹窗请人允许；
  - 窗口列表里的 approval 显示状态；
  - 终端请用 `run` 或会话。
- `help()` 的完整指南说明三种回答、等待和冷却。

Scenario: (A12) 兼容
- 结果：
  - instructions 不超过 4,096 字符；
  - 原有测试全部通过（Windows 桌面测试在授权关闭或预先允许的前提下运行）；
  - ruff 通过；
  - 打包冒烟通过；
  - Linux 上桌面工具照旧报只在 Windows 上可用。

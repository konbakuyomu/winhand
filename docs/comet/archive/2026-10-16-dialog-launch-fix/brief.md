# Outcome

修复 0.6.0 真机验收中发现的两个问题，发布 0.6.1：

1. **从桌面端后台触发的弹窗不出现。**
   - 现象：AI 第一次操作画图时，授权弹窗进程启动了，但一直没有窗口，2 分钟后自动结束；同一个 `winhand.exe secret-dialog` 单独启动时窗口正常。
   - 原因：桌面端后台有一个线程一直在读自己的标准输入（等桌面端的请求）。弹窗子进程继承了这个输入句柄，Python 启动时检查标准输入，要等那个读操作结束，于是卡死：只有 1 个线程，CPU 用时为 0，Tcl/Tk 都没加载。
   - 在用户电脑上用 Python 3.13 复现：父进程读标准输入时，继承了这个输入的子进程 8 秒没有反应；改用空输入（DEVNULL）后立刻完成。
   - 影响：授权弹窗、守卫确认框、`session_prompt_user` / `secret_prompt` 的密码框、`fs_pick` 选文件框，在桌面端后台和 `winhand stdio` 下都弹不出来；`winhand connect` 不受影响。授权默认开启，所以 0.6.0 桌面端下 AI 的桌面操作都会卡在“等人允许”。
2. **桌面端更新卡片把更新说明的 Markdown 原样显示**，例如“## winhand 0.6.0”“- feat: …”。

# Scope

- 弹窗子进程不再继承本进程的标准输入（`stdin=DEVNULL`）。
- 其余用不到输入的短命子进程也统一不继承：ripgrep、PowerShell（OCR、UI Automation 脚本、提权启动器）、icacls、git。
- 更新卡片按简单的 Markdown 显示：
  - 标题加粗；
  - 列表项显示成圆点；
  - 与卡片标题重复的“## winhand x.y.z”不再显示；
  - 链接只留文字，去掉加粗和代码标记。
- 版本 0.6.1。

# Non-goals

- 完整的 Markdown 渲染（表格、嵌套列表、图片）。
- 0.6.0 验收里记下的其他小问题另行处理：
  - 管理员窗口的 `approval` 也显示 `ask`；
  - 商店应用（如新版画图）的弹窗只显示 exe 名，没有中文名。

# Acceptance examples

- **B1**：在一个有线程正读着自己标准输入的进程里，`secret_prompt._run_dialog` 启动的子进程能立刻回答。
  - 测试 `test_a_dialog_opens_while_another_thread_reads_stdin` 在用户电脑上：0.6.0 代码失败（等 20 秒，“nobody answered”），修复后通过。
- **B2**：0.6.1 装好后，AI 第一次操作某个程序时，用户屏幕上出现授权弹窗。完成 desktop-app-approval 剩下的真机验收。
- **B3**：更新卡片显示的是渲染后的标题和圆点列表，没有 `##`、`- ` 这类符号；桌面端能编译。
- **B4**：原有测试全部通过，ruff 通过，打包冒烟通过。

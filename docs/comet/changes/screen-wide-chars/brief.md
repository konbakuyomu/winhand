# Outcome

修复云端会话用 WinHand 操作 Codex 交互界面时发现的问题，随 0.6.2 发布：

- **现象**：会话 `codex-tui-test-1`（Codex 0.162 的 TUI，120×30）里，先发了一句中文插话，之后所有要看屏幕的调用都失败，报 “string index out of range”。失败的调用包括 `session_screen`，以及带 `include_screen` 的 `session_send`。会话结束前一直如此，只能强制结束会话。
- **原因**：
  - pyte 把宽字符（中文、emoji）存成一格字符，后面跟一个空的占位格。
  - Codex 重画界面时，新字符可能只压住旧中文字符的一半。真正的终端会把这个旧字符整个清掉；pyte 不清，留下一个前面没有宽字符的占位格。ConPTY 按终端的规则认为那一格已经空了，不会再补发空格，所以这个占位格一直留着。
  - pyte 自带的 `Screen.display` 读到这个占位格时执行 `char[0]`，于是抛出 IndexError。之后每次读屏幕都失败。
- **复现**：用户电脑上这次会话的原始日志（161,581 字节，sha256 `5f588ec1…`）按 PTY 的方式分段喂给 `VirtualScreen(120, 30)`，读到第 107,008 个字符后 `snapshot()` 抛出同样的 IndexError。
  - 出问题的是第 15 行：这一行原来是输入框里的“› 这是 TUI 插话测试…”，Codex 把它改写成“  等它结束。”时，“。”压住了“插”的左半，留下了“插”的右半占位格。

# Scope

- `VirtualScreen` 不再调用 `Screen.display`，改为自己按终端的规则逐格渲染：
  - 前面没有宽字符的占位格（左半被盖掉）显示为空格；
  - 右半被盖掉的宽字符显示为空格；
  - 完整的宽字符照常显示。
- `snapshot()` 和 `cursor_line()` 都改用这个渲染。`session_screen`、`session_send` / `session_wait` 的 `include_screen`、提示符检测都经过这里。
- 宽度用 pyte 自己排格子时用的 `pyte.screens.wcwidth`，保证判断一致。

# Non-goals

- 不改 pyte 本身，不在写入时模拟终端清除半个字符的行为：只在读出时纠正，覆盖所有写入路径（直接写、插入、删除、擦除）。
- 光标列号仍按格子计（与以前相同）。
- 中转断线的分析不在本 change 里，结论另外记录。

# Acceptance examples

- **C1**：`VirtualScreen` 对四种情况显示得与终端一致：
  - 宽字符压住另一个宽字符的左半：`文 z`；
  - 窄字符压住宽字符的左半：`a z`；
  - 窄字符压住宽字符的右半：` b`；
  - 没被压住的宽字符：`中文ok`。
  - 新测试 `test_screen_shows_half_overwritten_wide_characters_like_a_terminal`：修复前前三种失败，修复后都通过。
- **C2**：真实日志回放：按 1、7、512、4096 个字符一段喂入，每段后读屏幕，修复后 0 次失败（修复前在第 107,008 个字符后失败）。最终画面与 Codex 界面一致。
- **C3**：0.6.2 装好后，在用户电脑上用会话启动 Codex TUI，发中文插话；整个过程中 `session_screen` 都能读出画面。
- **C4**：原有测试全部通过，ruff 通过，CI（含两个平台的打包冒烟）通过。

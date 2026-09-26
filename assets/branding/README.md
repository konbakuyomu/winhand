# winhand 形象与图标

- `source.webp`：用户提供的原图（2026-09-26），保持原样。它的“透明”是画在像素里的灰白棋盘格，没有 alpha 通道。
- `mascot.png`：去掉棋盘格后的透明立绘，放在应用侧栏左下角（做法同 smart-search 的 `MascotFooter`）。
- `winhand.png`：把立绘放在长春花蓝圆角底板上的 1024×1024 图标，用于任务栏、窗口、托盘和安装器。

重新生成全部图标（输出到本目录和 `desktop/windows/Assets/`，其中 `winhand.ico` 含 16–256 共 9 种尺寸）：

```sh
uv run desktop/scripts/build_icons.py
```

底板颜色和圆角参数在脚本顶部（`TILE_TOP` / `TILE_BOTTOM`）。

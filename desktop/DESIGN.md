# winhand 桌面端 UI 改版设计系统（DESIGN.md）

> 适用范围：winhand Windows 桌面端（WinUI 3 / Windows App SDK 2.5.1 / .NET 8）  
> 目标基准视口：主人日常窗口 `1280×1528 px @ 150% DPI`（约 **`853×1018` 逻辑像素**），兼顾全屏宽窗（≥ 1180 逻辑宽）与窄窗（≥ 680 逻辑宽）。  
> 设计模式：**Operate（高频操控与治理工作台）**

---

## 1. 视觉方向决策（按 `new-work.md` + `operate.md`）

### 1.1 产品机制与真实使用场景
- **核心机制（一句话）**：winhand 是云端 AI 与本机 Windows 桌面之间的「零入站端口反向代理闸门」——让使用者透明观察每一笔 AI 调用、锁住密钥不泄密、并按程序粒度审批桌面控制权。
- **物理场景与光线**：2560×1600 @ 150% 缩放的 Windows 11 工作机，常与 VS Code、Windows Terminal、KiCad、浏览器并排半屏使用，每天高频处理 160+ 次工具调用、15+ 条密钥与 10+ 项程序授权。日间浅色为主，夜间自动跟随系统深色模式。

### 1.2 原生 Fluent 2 还是强品牌定制？—— 明确结论
**选定方向：「85% Windows 11 原生 Fluent 2 骨架 + 15% 长春花蓝（Periwinkle）品牌微签名」**。

- **为什么拒绝“重品牌定制皮肤 / 暗黑极客仪表盘”？**  
  在 `Operate` 模式下，信任感来自**可预期的原生秩序**（如 Windows Security、PowerToys、Dev Home）。winhand 掌管 API 密钥与桌面操作权限，重绘非标滚动条、霓虹边或强装饰卡片只会让它显得像不可靠的第三方套壳。
- **为什么拒绝“100% 素颜默认 WinUI”？**  
  现状就是纯白页 + 默认黑条 Win32 标题栏 + 吃系统深炭灰强调色（`before-03.png`），不仅视觉平庸、沉闷，且缺乏品牌归属感。
- **15% 品牌签名精准落在 3 处**：
  1. **长春花蓝（Periwinkle Blue）专属强调色**：直接萃取自 `desktop/scripts/build_icons.py` 的应用图标底板（`TILE_TOP (128,152,232)` / `TILE_BOTTOM (104,128,216)`），经 WCAG AA 对比度校准，替代系统默认的炭灰/蓝强调色。
  2. **Mica 材质 + 一体化融合标题栏**：彻底移除突兀的黑色原生 Win32 标题条，让侧栏透出温和的 Windows 11 Mica 基底，内容区呈现柔和分层的现代桌面质感。
  3. **克制的看板娘底部锚点**：从 168px 截断巨贴纸收敛为 `88×88` 完整清晰立绘，并与全应用唯一的底部连接状态锚点自然融合。

---

## 2. 颜色系统（Restrained 策略 · 完整深浅色模式）

颜色策略遵循 `operate.md` 的 **Restrained（中性层 + 单一品牌强调色 + 语义状态色）**。严禁在业务代码里写死 `#FFFFFF` / `#000000`，所有画刷必须通过 `App.xaml` 的 `ThemeDictionaries`（`Light` / `Dark` / `HighContrast`）提供。

### 2.1 品牌强调色（Periwinkle Accent）

| Token 名 | Light Mode | Dark Mode | 对比度与用途 |
|---|---|---|---|
| `WinhandAccentBrush` | `#4F63D2` | `#8EA0F8` | 主按钮填充、选中导航指示条、活跃高亮、链接。Light 配白字 `5.2:1`，Dark 配 `#0F1426` 深字 `8.3:1` |
| `WinhandAccentHoverBrush` | `#4254BC` | `#A3B2FA` | 主按钮悬停态 |
| `WinhandAccentPressedBrush` | `#3747A3` | `#788CEB` | 主按钮按下态 |
| `WinhandAccentTextBrush` | `#3F51B5` | `#A8B7FF` | 浅底/透明底上的文字链接与高亮标签（≥ 5.5:1） |
| `WinhandAccentSoftBgBrush` | `#EEF1FC` | `#248EA0F8` *(14%)* | `进行中` 徽章底色、选中微底、活跃提示浅底 |
| `WinhandAccentForegroundOnFill` | `#FFFFFF` | `#0F1426` | 强调色主按钮上的文字与图标色 |

> WinUI 3 实现提示：在 `App.xaml` `ThemeDictionaries` 中同步覆写 `SystemAccentColor` / `AccentFillColorDefaultBrush` / `AccentFillColorSecondaryBrush` / `AccentFillColorTertiaryBrush` / `AccentTextFillColorPrimaryBrush` / `AccentTextFillColorSecondaryBrush` / `TextOnAccentFillColorPrimaryBrush`，即可让 `AccentButtonStyle`、`ToggleSwitch`、`SelectorBar` 指示条、`NavigationView` 选中竖条、`TextBox` 聚焦底边自动统一为长春花蓝，零自定义模板负担。

### 2.2 中性表面层（Neutral Surface Hierarchy）

| 层级 | Token 名 | Light Mode | Dark Mode | 用途 |
|---|---|---|---|---|
| 窗口基底 | `MicaBackdrop` (降级底色) | `#F3F4F8` | `#1C1D22` | 标题栏、侧边导航栏基底（微带一丝冷灰蓝调，呼应长春花蓝） |
| 主内容画布 | `WinhandCanvasBrush` | `#F9FAFC` *(或 `LayerFillColorDefaultBrush`)* | `#24252B` | 右侧圆角内容主画布（左上角 `8px` 圆角承接侧栏与标题栏） |
| 卡片表面 | `CardBackgroundFillColorDefaultBrush` | `#FFFFFF` | `#2D2F36` *(~7% 白叠加)* | `SettingsCard`、状态卡、列表容器卡 |
| 卡片悬停 | `CardBackgroundFillColorSecondaryBrush` | `#F5F6FA` | `#34363F` | 可点击行（如最近活动、可展开设置行）悬停底色 |
| 代码/内嵌凹槽 | `WinhandCodeBgBrush` | `#F1F3F7` | `#1B1C21` | `CodeBlock`、路径预览、显示态密文容器 |
| 卡片边框 | `CardStrokeColorDefaultBrush` | `#0F000000` *(~6%)* | `#18FFFFFF` *(~9%)* | 1px 清晰边界（**唯一海拔声明，不加外阴影**） |
| 细分隔线 | `DividerStrokeColorDefaultBrush` | `#0D000000` *(~5%)* | `#14FFFFFF` *(~8%)* | 同卡片内的行分隔线、分栏 1px 竖线 |
| 主文字 | `TextFillColorPrimaryBrush` | `#1A1D24` | `#F4F5F8` | 标题、正文、键名 |
| 次文字 | `TextFillColorSecondaryBrush` | `#5C6370` | `#A9B0BE` | 说明文案、时间、副标题（Light 对比度 `5.9:1`） |
| 弱文字 | `TextFillColorTertiaryBrush` | `#7E8594` | `#7D8494` | 占位符、空列表提示、掩码圆点 |

### 2.3 语义状态色（仅表达系统真实状态，禁止装饰滥用）

| 语义 | Light 前景 | Light 浅底 | Dark 前景 | Dark 浅底 | 典型状态 |
|---|---|---|---|---|---|
| **Success** | `#107C41` | `#E7F6EC` | `#5AD082` | `#225AD082` | 已连接、完成、运行中(MCP)、可用、已保存令牌 |
| **Active** | `#3F51B5` | `#EEF1FC` | `#A8B7FF` | `#248EA0F8` | 进行中、连接中、启动中、下载更新中 |
| **Warning** | `#9D5D00` | `#FFF4CE` | `#FCD14D` | `#22FCD14D` | 已断开等待重试、无响应、30s 防截屏显示倒计时、关闭审批警告 |
| **Critical** | `#C42B1C` | `#FDE7E9` | `#FF8C96` | `#24FF8C96` | 失败、令牌被拒绝、已被接管、冲突、删除/撤销危险悬停 |
| **Neutral** | `#5C6370` | `#EFF1F5` | `#A9B0BE` | `#18FFFFFF` | 待命、已暂停、未配置、已退出 |

---

## 3. 排版与字体系统（Typography）

- **中英文正文字体**：`Segoe UI Variable`（英文/数字）回退 `Microsoft YaHei UI`（中文）。这是 Windows 11 原生组合，在 150% DPI 下渲染最锐利。
- **等宽数据字体**：`Cascadia Mono, Consolas`。**严格限定用途**：时间戳、耗时、工具名/命令行、路径、`wss://` / `https://` URL、JSON 参数与结果、环境变量、密钥名与明文、exe 程序名。绝不用等宽字体给普通中文标题“装极客”。
- **等宽数字（Tabular Numerals）**：所有数字计数（今日调用数、倒计时秒数、耗时、时间戳、字符数）必须启用 `Typography.NumeralAlignment="Tabular"`，避免每秒跳动时横向抖动。

### 固定字号阶梯（Operate 紧比例，不使用流式字号）

| 样式名 (`App.xaml`) | 字号 / 行高 | 字重 | 字体族 | 用途 |
|---|---|---|---|---|
| `PageTitleStyle` | `22px` / `28px` | SemiBold (600) | UI Sans | 每页顶部唯一主标题（如「概览」「密钥」「程序授权」） |
| `MetricValueStyle` | `24px` / `30px` | SemiBold (600) | UI Sans (Tabular) | 仅限概览状态卡底部的 3 项核心遥测数字 |
| `SectionTitleStyle` | `15px` / `20px` | SemiBold (600) | UI Sans | 分组标题（如「最近活动」「一直允许」「软件更新」） |
| `RowTitleStyle` | `14px` / `20px` | SemiBold (600) | UI Sans | 设置行标题、MCP 服务名、卡片主标题 |
| `BodyTextStyle` | `14px` / `20px` | Regular (400) | UI Sans | 正文、输入框文字、按钮文字 |
| `CaptionTextStyle` | `12px` / `16px` | Regular (400) | UI Sans | 副标题说明、辅助元信息、加入时间、字符数 |
| `MonoTitleStyle` | `13.5px` / `20px` | SemiBold (600) | Cascadia Mono, Consolas | 密钥名称、exe 文件名、工具调用名 |
| `MonoDataStyle` | `12.5px` / `18px` | Regular (400) | Cascadia Mono, Consolas | 路径、URL、时间戳、耗时、代码块、密钥明文 |

---

## 4. 间距、圆角、卡片、分隔与图标

### 4.1 间距节奏（4px 基准）
- **页面边距**：水平 `24px`，顶部 `20px`，底部 `28px`；单列页最大宽度 `MaxWidth="880"`，左对齐。
- **区块节奏（上疏下密原则）**：
  - 页首行到首个区块：`16px`
  - 相邻 `Section` 之间：`24px`
  - `Section` 标题到其下方卡片：`8px`（清晰表明归属关系）
  - 同一组独立设置卡（如 `SettingsCard` 堆叠）之间：`4px`
  - 单张卡片内边距：`16px 20px`（高密度列表行内边距 `12px 16px`）
  - 行内元素横向间距：图标与文字 `12px`，按钮组 `8px`，文本列与右侧控件 `16px`。

### 4.2 圆角体系（Fluent 2 克制圆角）
- 主内容画布左上角：`8px`
- 卡片 / `SettingsCard` / `InfoBar`：`8px`
- 按钮 / 输入框 / 下拉框 / `ListViewItem` / 代码块：`6px`（或 WinUI 默认 `4px` ControlCornerRadius）
- 状态微标（Status Badge）：`4px` 圆角标签（高度 `22px`，内边距 `0 8px`，12px SemiBold；告别臃肿的大圆胶囊）
- 看板娘无外框无裁剪圆遮罩，保持原生透明 PNG 轮廓。

### 4.3 卡片与海拔规则
- **单层声明**：页面内静态卡片使用 `CardBackgroundFillColorDefaultBrush` + `1px` `CardStrokeColorDefaultBrush`，**绝不叠加大阴影**。
- **浮层例外**：真正脱离文档流悬浮的覆盖层（右下角悬浮通知 `OverlayNotice`、`MenuFlyout`、`TeachingTip`）使用 WinUI 3 原生浮层阴影（`ThemeShadow` / 轻投影）以与底图区分，这不属于“卡片加阴影”。
- **禁止嵌套卡片**：卡片内部如需分组，使用 `1px` `DividerStrokeColorDefaultBrush` 细线或 `WinhandCodeBgBrush` 轻凹槽（无外框），绝不出现“圆角卡片里再套圆角卡片”。
- **紧凑型列表卡片**：密钥列表、程序授权列表、本机数据路径列表使用**单张大容器卡片 + 内部 1px 细分隔线**，避免 15 条密钥变成 15 张独立悬浮块造成视觉碎片化。

### 4.4 图标规范
- 全应用唯一图标库：**`Segoe Fluent Icons`**（Win10 自动回退 `Segoe MDL2 Assets`），默认 `16px`（工具栏微按钮 `14px`）。
- 推荐 Glyph 映射表：
  - 概览 `&#xE80F;` · 活动 `&#xE81C;` · 会话 `&#xE756;` · MCP 服务 `&#xE71B;`（`&#xE968;` 在 Win11 里像立式话筒，验收时换掉了）
  - 密钥 `&#xE8D7;` · 程序授权 `&#xEA18;` · 中转 `&#xE753;` · 设置 `&#xE713;`
  - 显示 `&#xE7B3;` · 隐藏 `&#xED1A;` · 复制 `&#xE8C8;` · 更多 `&#xE712;` · 删除 `&#xE74D;` · 编辑 `&#xE70F;`
  - 搜索 `&#xE721;` · 添加 `&#xE710;` · 刷新/重连 `&#xE72C;` · 帮助 `&#xE897;` · 打开外部/目录 `&#xE8A7;` / `&#xE8DA;`

---

## 5. 看板娘（Mascot）规范

- **结论：保留！** 它是 winhand 最有辨识度的个人情感符号，且原图透明抠图干净、蓝窗配色与长春花蓝品牌色高度和谐。
- **位置与尺寸**：
  - 放在左侧 `NavigationView.PaneFooter` 最底部（位于底栏「设置」项**下方**）。
  - **尺寸从 `168×168` 缩小为 `88×88` 逻辑像素**，完整展示猫耳、小手与蓝色小窗，**取消 `TranslateTransform Y="16"` 截断**，给足底部 `4px` 呼吸留白。
  - **状态融合**：移除原先孤零零飘在侧栏右下角的 8px 绿点！在看板娘右侧紧邻排布两行紧凑信息：
    - 第一行：`8px 状态圆点` + 状态词（`已连接` / `重连中` / `已暂停` / `异常`），12px SemiBold，颜色随语义色变化；
    - 第二行：版本号或在线时长（`v0.13.0 · 7m`），11.5px 次要灰字，悬停 `ToolTip` 展示完整中转地址与详情；
    - 整块区域可点击（`Button` 透明底微悬停态），点击直接跳回「概览」。
  - **窄窗与手动折叠态**：标题栏最左侧保留 `36×32` 侧栏折叠按钮（快捷键 `Ctrl+B`）；当用户手动折叠或窗宽 `< 680px` 自动收起为 `48px` Compact 模式时，隐藏看板娘图片与文字，仅保留底部居中的 `8px` 状态圆点（带 `ToolTip`）。
- **活动标题字体判定**：英文/下划线/路径标识类标题（如 `job_status`、`fs_edit`）使用 `MonoTitleStyle`；含中文的自然语言标题（如「经一次性链接上传了…」）使用 `RowTitleStyle`（UI Sans），不得强行等宽化。

---

## 6. 给执行者（GPT）的「不许做」铁律清单

执行者实现 XAML / C# 时必须逐条自检，违反任意一条视为不通过：

1. **不许堆“4 宫格 KPI 营销卡片”**：概览页只用 1 张结构化状态卡承接连接与 3 项紧凑指标，不许放带大彩色圆底图标和 36px+ 夸张字号的营销数据块。
2. **不许用 Emoji 或 Unicode 符号当 UI 图标**：禁止出现 🔑、🛡️、🟢、⚙️、✓ 等字符代替图标，一律使用 `Segoe Fluent Icons` 的 `FontIcon`。
3. **不许卡片套卡片（Nested Cards）**：任何 `Border` 卡片内禁止再嵌套带圆角和边框的子卡片；内部分层一律用 `1px` 分隔线或无边框浅凹槽。
4. **不许“1px 边框 + 外扩散阴影”同时出现**：桌面卡片只用 `1px CardStrokeColorDefaultBrush` 表达层级，禁止手写 `ThemeShadow` 或 `DropShadow` 制造脏污灰边。
5. **不许还原“按钮墙”**：密钥行与程序授权行最多只保留 **2 个透明底（Ghost）高频按钮 + 1 个 `⋯` `MenuFlyout` 溢出菜单**，禁止每行横排 3~4 个带边框矩形按钮；高危的「删除」必须放进溢出菜单并标警示红字。
6. **不许在卡片或列表行左侧加彩色粗竖条（`border-left: 3px/4px`）**：除 `NavigationView` 原生选中指示器外，严禁用左侧彩条作为状态装饰。
7. **不许硬编码浅色/纯白/纯黑十六进制色值**：禁止在 XAML 或 C# 里直接写 `Colors.White`、`#FFFFFF`、`#F5F5F5`、`#000000`；必须全部绑定 `ThemeResource` / `ThemeDictionaries`，确保切换深色模式零破损。
8. **不许在标题上方加英文全大写 Eyebrow/Kicker 或序号**：禁止出现 `SECURITY` / `01 OVERVIEW` / `ACTIVE RELAY` 这类典型 AI 味装饰小标。
9. **不许用渐层文字、毛玻璃滥用或装饰发光**：只在 `Window` 根部挂一个 `MicaBackdrop`，不许在卡片、标题、圆点上加 `LinearGradientBrush` 或荧光阴影。
10. **不许忽略 `853×1018` 逻辑像素默认窗口尺寸**：禁止把分栏阈值写死到 > 600px 导致窄屏上下硬切；禁止隐藏分栏分隔线；禁止让工具栏 `SelectorBar` 的「失败」标签被搜索框挤出视口。
11. **不许把高频轻反馈做成阻塞弹窗，也不许让提示条盖住页首操作**：复制成功、保存中转、撤销单条授权、刷新完成一律走**右下角悬浮非阻塞 `InfoBar`** 或按钮瞬态反馈，绝不弹 `ContentDialog` 打断用户；`ContentDialog` 仅保留给破坏性确认（删除密钥/删除 MCP/停止会话）和添加/编辑表单。
12. **不许为了装饰给无差异的列表行加相同的占位小图标**：例如程序授权页协议不提供 exe 真图标时，直接留白对齐文本，严禁每行重复画一个一模一样的通用窗口小方框。
13. **不许为了 UI 改版弱化任何安全或协议不变量**：
    - 显示密钥前必须先调用 `SetWindowDisplayAffinity(_hwnd, WDA_EXCLUDEFROMCAPTURE)`；30 秒后、切换侧栏页面、隐藏窗口或后端断开时必须立即调用 `HideSecret()` 并通知后端 `secrets_hide`；
    - 复制密钥必须保留 `IsAllowedInHistory = false, IsRoamable = false`；
    - 设备令牌保存后绝不回显明文；
    - 后端 stdin/stdout JSON 协议字段与方法名零改动。

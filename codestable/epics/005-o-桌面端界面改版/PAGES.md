> 这一份是 Epic 005 的逐页规范和验收要点，由 Gemini（parley t36）写、Claude 审定。HTML 样稿和渲染图不在仓库里，在主人电脑的 `~/.winhand/scratch/ui-review/gemini/`（mockups/、render/）。视觉规范见仓库的 `desktop/DESIGN.md`。

# winhand 桌面端信息架构与逐页版式规范（PAGES.md）

> 对应设计系统：[DESIGN.md](../../../desktop/DESIGN.md)  
> 基准窗口：`853×1018` 逻辑像素（对应 2560×1600 @ 150% DPI 下的 `1280×1528` 物理像素）；侧栏展开 `212px` 时内容区宽 **`641px`**，手动折叠至 `48px` 时内容区宽 **`805px`**。

---

## 1. 新信息架构与侧边栏分组

### 1.1 原设置页 7 块拆分方案
将原先堆叠 4 屏的「设置」按**使用频率 × 领域心智模型**拆分为 4 个独立落点：

| 原设置块 | 新归宿 | 所在侧栏分组 | 理由 |
|---|---|---|---|
| **密钥**（15+ 项） | 独立页 `密钥` (`secrets`) | 安全与连接 | 高频日常查询/复制/新增，且具备独立防截屏安全态 |
| **程序授权**（总开关 + 10+ 程序） | 独立页 `程序授权` (`apps`) | 安全与连接 | 高频查看与撤销，属于桌面安全策略而非偏好设置 |
| **中转**（地址 + 设备令牌） | 独立页 `中转` (`relay`) | 安全与连接 | 核心连接凭证，未配置/令牌被拒时从概览与 MCP 一键直达 |
| **启动 + 更新 + 本机数据 + 关于** | 精简页 `设置` (`settings`) | 页脚 `FooterMenuItems` | 均为低频本机应用偏好，4 块合起来约 780px，一屏全览 |

### 1.2 「会话」页处理方案（动态条件显隐）
- **常态（`_sessions.Count == 0`）**：侧边栏 `NavSessions.Visibility = Visibility.Collapsed`，不占顶级席位，根治 `before-09.png` 长期空白死页问题。
- **活跃态（`_sessions.Count > 0`）**：自动设为 `Visibility.Visible`，显示在「活动」正下方，并绑定 `InfoBadge` 展示进行中数量（若有会话处于 `needs_user` / `blocked`，`InfoBadge` 切为警示琥珀色）。
- **协议与功能 100% 保留**：`MainWindow.Sessions.cs` 的实时 1s 刷新、打开日志、停止会话、移出列表能力丝毫不减；同时概览页的「活动会话」指标在 > 0 时可点击直达。

### 1.3 侧边栏折叠策略（两样都要）
- **默认状态**：853px 窗宽下侧栏保持展开（`212px`），活动页默认用底部详情抽屉，降低学习成本。
- **手动折叠能力**：在 40px 一体化标题栏最左侧放置 `36×32` 折叠图标按钮（Glyph `&#xE700;` GlobalNavButton，支持快捷键 `Ctrl+B`），点击切换 `RootNavigation.IsPaneOpen`（`212px ↔ 48px`）。
  - 853px 窗口手动折叠侧栏后，内容区立即扩宽至 **`805px`（≥ 760px 断点）**，活动页自动无缝切换为左右双栏；
  - 折叠状态**不跨重启持久化**，避免下次启动用户找不到带文字的菜单；
  - 折叠态隐藏看板娘图片和两行文字，仅在底端居中保留 `8px` 状态圆点（保留悬停 `ToolTip` 与点击跳回概览）。

### 1.4 侧边栏完整结构（静态 7 项，每组 ≤ 3 项）

```text
[一体化标题栏 40px: [E700 折叠按钮] [16px winhand 图标] "winhand" ... 最小化/最大化/关闭]
──────────────────────────────────────────────────────────────────────────────
  MenuItems:
    [E80F] 概览                 (tag: overview)
    [E81C] 活动                 (tag: activity)
    [E756] 会话  [InfoBadge]    (tag: sessions, 默认 Collapsed，有会话时 Visible)
    [E71B] MCP 服务             (tag: mcp)
    ─────────── (NavigationViewItemSeparator)
    安全与连接  (NavigationViewItemHeader)
    [E8D7] 密钥                 (tag: secrets)
    [EA18] 程序授权             (tag: apps)
    [E753] 中转                 (tag: relay)

  FooterMenuItems:
    [E713] 设置  [更新微点]      (tag: settings, _updater.Available||Ready 时显示蓝色点状 InfoBadge)

  PaneFooter:
    [88×88 完整看板娘]  +  [● 已连接 / v0.13.0 · 7m]  (可点击跳回概览；Compact 折叠态仅留 8px 状态圆点)
```

---

## 2. 依赖决策（已拍板：零新增 NuGet 依赖）

- **结论：不引入 `CommunityToolkit.WinUI.Controls.SettingsControls`**。
  - 项目使用 Windows App SDK 2.5.1 + `.NET 8` 自包含发布，Toolkit 8.x 按 1.x 构建存在兼容与发布体积风险；
  - 所需共享样式集中在 `App.xaml`（完整定义见第 5 节）与 `MainWindow.Layout.cs` 辅助构造器中，足以保证全应用零走样。

---

## 3. 全局外壳与悬浮提示条（`MainWindow.xaml`）

### 核心变更
1. 启用 `MicaBackdrop` 与 `ExtendsContentIntoTitleBar = true`，彻底消除深黑 Win32 标题条；将折叠按钮（`PaneToggleButton`）放在 40px 标题栏最左侧（不占用 `NavigationView` 内部顶部空间），并通过 `SetTitleBar(TitleBarDragRegion)` 仅把图标右侧空白区设为拖拽区，防止折叠按钮与系统三键吞事件。
2. **删除原先 52px 顶部全局白条**（移除重复标题、重复 `已连接` 胶囊和语义不明的 `✓` 按钮）。
3. **非阻塞悬浮提示条 `OverlayNotice` 改置内容区右下角**：
   - 定位：`HorizontalAlignment="Right"`, `VerticalAlignment="Bottom"`, `Margin="24,0,24,20"`, `MaxWidth="440"`。
   - **绝不遮挡页首主操作按钮**（「添加密钥」「授权文件」「编辑」等），亦不与左下角看板娘冲突。
   - 自动收起时长：
     - 普通成功/信息提示：**4 秒**自动关闭；
     - **带「恢复」/动作按钮的撤销提示**（如撤销一直允许程序）：**8 秒**自动关闭，且鼠标悬停（`PointerEntered`）暂停计时、移出（`PointerExited`）恢复计时；
     - 警告/错误提示：常驻直至用户手动点关闭。

### 关键 XAML 骨架

```xml
<Window x:Class="Winhand.Desktop.MainWindow"
        xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml">
  <Window.SystemBackdrop>
    <MicaBackdrop Kind="Base" />
  </Window.SystemBackdrop>

  <Grid>
    <Grid.RowDefinitions>
      <RowDefinition Height="40" />
      <RowDefinition Height="*" />
    </Grid.RowDefinitions>

    <!-- 一体化标题栏：左侧折叠按钮 + 品牌区 + 可拖拽空白区 -->
    <Grid Grid.Row="0" Padding="6,0,140,0">
      <Grid.ColumnDefinitions>
        <ColumnDefinition Width="Auto" />
        <ColumnDefinition Width="*" />
      </Grid.ColumnDefinitions>
      <Button x:Name="PaneToggleButton" Grid.Column="0"
              Style="{StaticResource IconGhostButtonStyle}"
              Width="36" Height="32" Click="OnPaneToggleClick"
              ToolTipService.ToolTip="折叠或展开导航栏 (Ctrl+B)"
              AutomationProperties.Name="折叠或展开导航栏">
        <FontIcon Glyph="&#xE700;" FontSize="14" />
      </Button>
      <Grid x:Name="TitleBarDragRegion" Grid.Column="1" Background="Transparent" Padding="8,0,0,0">
        <StackPanel Orientation="Horizontal" Spacing="9" VerticalAlignment="Center">
          <Image Source="ms-appx:///Assets/winhand.png" Width="16" Height="16" />
          <TextBlock Text="winhand" FontSize="12" FontWeight="SemiBold"
                     VerticalAlignment="Center" Foreground="{ThemeResource TextFillColorPrimaryBrush}" />
        </StackPanel>
      </Grid>
    </Grid>

    <NavigationView x:Name="RootNavigation" Grid.Row="1"
                    PaneDisplayMode="Left"
                    IsPaneOpen="True"
                    OpenPaneLength="212"
                    CompactPaneLength="48"
                    CompactModeThresholdWidth="680"
                    ExpandedModeThresholdWidth="680"
                    IsBackButtonVisible="Collapsed"
                    IsSettingsVisible="False"
                    IsPaneToggleButtonVisible="False"
                    IsTitleBarAutoPaddingEnabled="False"
                    AlwaysShowHeader="False"
                    PaneOpening="OnPaneStateChanged"
                    PaneClosing="OnPaneStateChanged"
                    SelectionChanged="OnNavigationSelectionChanged">
      <NavigationView.MenuItems>
        <NavigationViewItem Content="概览" Tag="overview" IsSelected="True">
          <NavigationViewItem.Icon><FontIcon Glyph="&#xE80F;" /></NavigationViewItem.Icon>
        </NavigationViewItem>
        <NavigationViewItem Content="活动" Tag="activity">
          <NavigationViewItem.Icon><FontIcon Glyph="&#xE81C;" /></NavigationViewItem.Icon>
        </NavigationViewItem>
        <NavigationViewItem x:Name="NavSessions" Content="会话" Tag="sessions" Visibility="Collapsed">
          <NavigationViewItem.Icon><FontIcon Glyph="&#xE756;" /></NavigationViewItem.Icon>
          <NavigationViewItem.InfoBadge>
            <InfoBadge x:Name="SessionsBadge" Value="0" />
          </NavigationViewItem.InfoBadge>
        </NavigationViewItem>
        <NavigationViewItem Content="MCP 服务" Tag="mcp">
          <NavigationViewItem.Icon><FontIcon Glyph="&#xE71B;" /></NavigationViewItem.Icon>
        </NavigationViewItem>
        <NavigationViewItemSeparator />
        <NavigationViewItemHeader Content="安全与连接" />
        <NavigationViewItem Content="密钥" Tag="secrets">
          <NavigationViewItem.Icon><FontIcon Glyph="&#xE8D7;" /></NavigationViewItem.Icon>
        </NavigationViewItem>
        <NavigationViewItem Content="程序授权" Tag="apps">
          <NavigationViewItem.Icon><FontIcon Glyph="&#xEA18;" /></NavigationViewItem.Icon>
        </NavigationViewItem>
        <NavigationViewItem Content="中转" Tag="relay">
          <NavigationViewItem.Icon><FontIcon Glyph="&#xE753;" /></NavigationViewItem.Icon>
        </NavigationViewItem>
      </NavigationView.MenuItems>

      <NavigationView.FooterMenuItems>
        <NavigationViewItem x:Name="NavSettings" Content="设置" Tag="settings">
          <NavigationViewItem.Icon><FontIcon Glyph="&#xE713;" /></NavigationViewItem.Icon>
          <NavigationViewItem.InfoBadge>
            <InfoBadge x:Name="UpdateBadge" Visibility="Collapsed" />
          </NavigationViewItem.InfoBadge>
        </NavigationViewItem>
      </NavigationView.FooterMenuItems>

      <NavigationView.PaneFooter>
        <Button x:Name="MascotStatusButton" Click="OnMascotStatusClick"
                Style="{StaticResource GhostButtonStyle}"
                Margin="6,4,6,10" Padding="6,4,8,4" Height="Auto"
                HorizontalAlignment="Stretch" HorizontalContentAlignment="Stretch">
          <ToolTipService.ToolTip><ToolTip x:Name="ConnectionToolTip" Placement="Top" /></ToolTipService.ToolTip>
          <Grid x:Name="MascotExpandedGrid" ColumnSpacing="8">
            <Grid.ColumnDefinitions>
              <ColumnDefinition Width="Auto" />
              <ColumnDefinition Width="*" />
            </Grid.ColumnDefinitions>
            <Image Source="ms-appx:///Assets/mascot.png" Width="88" Height="88" Stretch="Uniform"
                   AutomationProperties.AccessibilityView="Raw" />
            <StackPanel Grid.Column="1" VerticalAlignment="Bottom" Margin="0,0,0,8" Spacing="3">
              <StackPanel Orientation="Horizontal" Spacing="6">
                <Ellipse x:Name="ConnectionDot" Width="8" Height="8" VerticalAlignment="Center" />
                <TextBlock x:Name="ConnectionLabel" FontSize="12" FontWeight="SemiBold" Text="启动中" />
              </StackPanel>
              <TextBlock x:Name="ConnectionMeta" FontSize="11.5"
                         Foreground="{ThemeResource TextFillColorSecondaryBrush}"
                         TextTrimming="CharacterEllipsis" />
            </StackPanel>
          </Grid>
          <!-- 折叠态切换为单独的 8px 居中圆点（OnPaneStateChanged 控制显隐） -->
        </Button>
      </NavigationView.PaneFooter>

      <!-- 右侧圆角内容画布 -->
      <Grid Background="{ThemeResource WinhandCanvasBrush}"
            BorderBrush="{ThemeResource CardStrokeColorDefaultBrush}"
            BorderThickness="1,1,0,0" CornerRadius="8,0,0,0">
        <ContentControl x:Name="PageHost" HorizontalContentAlignment="Stretch" VerticalContentAlignment="Stretch" />
        <!-- 右下角非阻塞悬浮反馈条（带轻投影，不遮挡页首操作） -->
        <InfoBar x:Name="OverlayNotice" IsOpen="False" IsClosable="True"
                 HorizontalAlignment="Right" VerticalAlignment="Bottom"
                 Margin="24,0,24,20" MaxWidth="440"
                 PointerEntered="OnNoticePointerEntered" PointerExited="OnNoticePointerExited">
          <InfoBar.Shadow><ThemeShadow /></InfoBar.Shadow>
        </InfoBar>
      </Grid>
    </NavigationView>
  </Grid>
</Window>
```

---

## 4. 逐页版式规范

### 4.1 概览（`overview` · 见样稿 `mockups/overview.html`）
- **容器**：`ScrollViewer` + `StackPanel(MaxWidth=880, Spacing=20, Margin=24,20,24,28)`。
- **区块 1 — 页首行**：左 `概览`（`PageTitleStyle`），右侧留空（无冗余状态胶囊）。
- **区块 2 — 连接与今日遥测整合卡（1 张 `SurfaceCard`，非嵌套卡片）**：
  - 上半区（Padding `18,16`）：
    - 左：`10px` 语义圆点 + 状态大字（`中转已连接`，16px SemiBold）+ 副行单次显示主机与时长（`relay.example.com · 已在线 7 分钟`，完整 `wss://` 放 `ToolTip`）。
    - 右：已在线时 `断开`（`GhostButtonStyle`）+ `重新连接`（默认次按钮）；离线/断开/冲突态将 `重新连接` 升为 `AccentButtonStyle`；未配置态显示 `去配置中转`（`AccentButtonStyle`）。
  - 细分隔线（`1px DividerStrokeColorDefaultBrush`）。
  - 下半区（`Grid` 3 列等分 `1* : 1* : 1*`，列间 `1px` 竖线）：
    1. **今日工具调用**：`165`（`MetricValueStyle` 24px Tabular）+ 副注（`全部成功` 或红字 `含 N 次失败`），整格可点击跳转 `activity`。
    2. **MCP 服务**：`6` + 副注（如 `2 运行中 · 4 待命` / `已配置 · 待命`），整格可点击跳转 `mcp`。
    3. **活动会话**：`0`（0 时用 `TextFillColorTertiaryBrush` 弱化，`无活跃会话`；> 0 时正常色并可点击跳转 `sessions`）。
- **区块 3 — 动态状态提示条（按需 `InfoBar`，平时 `Collapsed`）**：
  - `unconfigured` → `Informational` + 按钮「去配置中转」(`ShowPage("relay")`)；
  - `refused` / `replaced` / `conflict` / `!backendRunning` → `Error` + 一键操作（「更新令牌」/「重新连接」）；
  - `_updater.Ready` / `_updater.Available` → `Informational` + 按钮「查看更新」(`ShowPage("settings")`)。
- **区块 4 — 最近活动**：
  - 区块头：左 `最近活动`（`SectionTitleStyle`），右 `GhostButton` 样式链接 `查看全部 ›`（点击跳转 `activity`）。
  - 内容卡（1 张 `SurfaceCard`，Padding `4`）：最多 8 条**可点击行**（点击跳转 `activity` 并自动选中该条展开详情）。
  - **去掉卡片下方任何“点击可查看详情”提示文字**，改由每行最右列悬停出现的右箭头表达可点击性！
  - 行列布局（`Grid` 5 列，Padding `12,10`，ColumnSpacing `12`）：
    - Col 0 (`60px`): `HH:mm:ss`（`MonoDataStyle`，弱色）
    - Col 1 (`1*`): 上行 `entry.Title`（遵循下文 **4.2 活动标题字体规则**），下行 `argsLine`（`MonoDataStyle` 12px 单行省略）
    - Col 2 (`Auto`): 耗时（`43 ms` / `3.2 s`，`MonoDataStyle` 右对齐）
    - Col 3 (`56px`): 状态微标（`StatusBadge*Style`，右对齐）
    - Col 4 (`14px`): 导航箭头 `FontIcon(Glyph="&#xE76C;", FontSize=11)`，常态 `Opacity="0"`，行 `PointerOver` 或键盘聚焦时平滑显为 `Opacity="1"`。
  - **空状态**：卡片内居中轻图标（`&#xE81C;`）+ “还没有活动。Claude 调用工具后会实时出现在这里。”

---

### 4.2 活动（`activity` · 见样稿 `mockups/activity.html`）
- **活动标题字体判定规则（全应用通用：概览、活动列表、活动详情）**：
  ```csharp
  // 工具名/英文标识符（仅含 ASCII 字母、数字、下划线、点、连字符、斜杠、空格、·）用等宽；
  // 含中文或其他自然语言句子的标题（如“经一次性链接上传了…”“已连接到中转”）用界面字体。
  private static bool IsIdentifierTitle(string title) =>
      title.Length > 0 && title.All(c => c <= 0x7F || c == '·');

  private static string ActivityTitleStyleKey(string title) =>
      IsIdentifierTitle(title) ? "MonoTitleStyle" : "RowTitleStyle";
  ```
- **顶部双行工具栏**（Padding `24,18,24,10`，底部 `1px` 分隔线）：
  - 第 1 行（`Grid`）：左 `活动`（`PageTitleStyle`）；右 `AutoSuggestBox(PlaceholderText="搜索工具名、参数或结果", QueryIcon="Find", MinWidth=220, MaxWidth=280)`（`Ctrl+F` 聚焦）+ `GhostButton` `[E8DA] 审计日志`。
  - 第 2 行（`Grid`, MarginTop `8`）：左 `SelectorBar`（`全部` · `工具调用` · `连接` · `失败`，**独占整行左侧，永不被搜索框挤掉**）；右计数 `共 165 条`（筛选中显示 `匹配 N / 165 条` + `重置` 轻链接）。
- **主工作区（响应断点 = 内容区宽度 `760px`）**：
  - **宽屏模式（内容区 `≥ 760px`，如全屏窗或 853px 窗手动折叠侧栏后的 `805px`）**：左右可拖拽分栏（左 `ListView` 占 `1*`，Min `380px`；中间 `8px Thumb` 含 `1px` 竖线；右详情面板 `360px` 起，未选中时显示居中轻空态“选择一条记录查看参数和结果”）。
  - **标准/窄屏模式（内容区 `< 760px`，如 853px 窗展开侧栏的 `641px`）**：
    - **未选中时**：`ListView` 占满 **100% 高度**（无空白下半屏）；
    - **选中任意行时**：底部展开 **可拖拽高度的详情抽屉（Bottom Inspector，默认占 46% 高度）**，顶边自带清晰 `1px` 分隔线 + 居中微握把拖拽 `Thumb`（`Cursor=SizeNorthSouth`）+ 抽屉顶栏（左：工具名 + 状态微标 + 一行元数据；右：`复制 JSON` 轻按钮 + `×` 关闭按钮，支持 `Esc` 关闭并取消选中）。
- **详情检查器正文**：
  - **结果区**：标题 `结果` + 右侧 `[E8C8] 复制` 透明微按钮 + `CodeBlock`（`WinhandCodeBgBrush`，等宽，可选中）。
  - **参数区**：标题 `参数` + 副注“长内容已截断；env、令牌等已脱敏” + 格式化 JSON `CodeBlock`。
- **空状态 / 筛选无匹配**：居中空态；筛选无结果时附 `清除筛选` 按钮。

---

### 4.3 会话（`sessions` · 条件显隐页）
- 仅在 `_sessions.Count > 0` 时显示于侧栏「活动」下方，挂 `InfoBadge` 指示活跃数（`needs_user`/`blocked` 时呈琥珀警示色）。
- 布局复用 MCP 服务同一套响应分栏规则（阈值 `568px`：左列表 `200px` + 右详情 ≥ `380px`；极窄窗下钻）。
- 保留原有 1s 原地文本刷新（不重建 DOM、不丢滚动位置）与全部动作：`打开日志`、`停止…`（`ContentDialog` 确认）、`移出列表`。

---

### 4.4 MCP 服务（`mcp` · 见样稿 `mockups/mcp.html`）
- **响应分栏（彻底消灭列表腰斩）**：
  - 左右双栏阈值降为 **`568px`**（左列表 `200px` [Min `180`, Max `260`] + `8px` 拖拽区含 `1px` 竖线 + 右详情 Min `380px`）。
  - 在主人日常 `853px` 窗口（内容区 `641px`）下，**直接呈现左右双栏**；仅在 `< 568px` 极窄窗切为**单层下钻**（点服务进详情，顶栏现 `← 返回服务列表`），永不上下硬切列表。
- **左栏（服务目录，200px）**：
  - 顶区（Padding `14,18,14,10`）：页标题 `MCP 服务`（16px SemiBold）+ 紧凑双按钮行（`+ 添加` `AccentButtonStyle` + `导入…` 默认次按钮）。
  - `ListView`：每行 2 行精简展示——`8px` 状态圆点 + 服务名（`MonoTitleStyle` 13.5px）+ 单行简短状态词（`运行中` / `待命` / `出错` / `未开启`，去掉每行重复的“第一次被调用时启动”尾巴）。
- **右栏（服务详情，`ScrollViewer` Padding `22,18,24,28`）**：
  - **详情头（`Grid` 2 列）**：左为服务名（20px SemiBold 等宽）+ 垂直居中的状态微标 + 来源副标（`从 Codex 导入`）；右为 `编辑`（默认次按钮）+ `⋯` `IconGhostButton`（`MenuFlyout` 含红色危险项 `删除服务…`，撤销原页面底部的整张删除卡）。
  - **远程访问卡**（单卡 2 行 + `1px` 内分隔）：
    - 行 1：`对外提供` + 简短副标 + `ToggleSwitch`
    - 行 2：`远程地址` + 等宽 URL（单行省略带 Tooltip）+ 右侧 `复制` 按钮（未配置中转时显示 `去配置中转` 按钮直达 `relay`）。
  - **运行状态卡**（仅开启时显示）：
    - 若有 `status.error`，卡顶内嵌红色 `InfoBar` + `stderr_tail` 代码块；
    - 紧凑事实表（标签列 `68px`）：状态、服务程序、协议版本、进程、工具调用、远程会话；
    - 底部操作行：`测试`、`重启`（非 http 且在 `running`/`error`/`unresponsive` 时可见）、`打开日志`（有 `log_file` 时可见）。
  - **启动方式卡**：
    - `stdio` 时合并呈现完整启动命令 `CodeBlock`（`command + args`），下方紧接工作目录、脱敏环境变量、启动超时与闲置停止；`http` 时展示地址与请求头。
- **空状态**（0 个服务）：整页居中引导 + `从其他客户端导入…`（主按钮）+ `手动添加`（次按钮）。

---

### 4.5 密钥（`secrets` · 见样稿 `mockups/secrets.html`）
- **容器**：`ScrollViewer` + `StackPanel(MaxWidth=880, Spacing=14, Margin=24,20,24,28)`。
- **页首行**：左 `密钥`（`PageTitleStyle`）+ 计数微标（`15`）+ `?` 透明帮助按钮（`TeachingTip` 说明隔离机制与 `secrets.env`）；右 `+ 添加密钥`（`AccentButtonStyle`，置顶立即可达）。
- **动态安全横幅（平时 `Collapsed`）**：
  - 显示密文期间显现琥珀色 `InfoBar`（`防截屏保护已开启 · 正在显示 {name}，{s} 秒后自动隐藏，期间无法捕获此窗口` + `立即隐藏` 按钮 + 底边 `2px` 倒计时进度条）；
  - `secrets.error != null` 时显现红色 `InfoBar` + `打开文件位置` 按钮。
- **搜索与定位条**：左 `AutoSuggestBox(PlaceholderText="搜索密钥名称… (Ctrl+F)", MaxWidth=320)`；右幽灵小按钮 `[E8DA] secrets.env`。
- **密钥列表单卡**：单张 `SurfaceCard` + 内部 `1px` 分隔线。每行：
  - 左：密钥名（`MonoTitleStyle`）+ 下行掩码（`•••••••••••• · 53 个字符`）；显示态切换为带琥珀细框的等宽明文盒（`IsTextSelectionEnabled="True"`）+ 倒计时微标 `26s`。
  - 右：**2 透明高频按钮 + 1 溢出菜单**：
    1. `显示 / 隐藏`（`GhostButtonStyle`，显示态切 `WarningTintedButtonStyle`）；
    2. `复制`（`GhostButtonStyle`，点击成功原地瞬态 1.5s 显示绿底 `已复制`，仍保留 `IsAllowedInHistory=false, IsRoamable=false`）；
    3. `⋯`（`IconGhostButtonStyle` + `MenuFlyout`：`修改名称或值…` / 分隔线 / 红字 `删除密钥…`）。
- **空状态 / 搜索无匹配**：单卡居中空态 + `添加密钥` 或 `清除搜索`。

---

### 4.6 程序授权（`apps` · 见样稿 `mockups/apps.html`）
- **容器**：`ScrollViewer` + `StackPanel(MaxWidth=880, Spacing=16, Margin=24,20,24,28)`。
- **页首行**：左 `程序授权`（`PageTitleStyle`）+ `?` 规则说明 `TeachingTip` 按钮；右幽灵按钮 `[E8DA] desktop-apps.toml`。
- **文件损坏横幅**（按需）：`apps.error != null` 时显示红色 `InfoBar` + `打开文件`。
- **置顶总开关卡**（`SettingsRowCardStyle`）：左 36×36 柔和盾牌微底图标 + `AI 操作程序前先问我` + 动态副标（关闭时切琥珀警告文案）；右 `ToggleSwitch`。
- **置顶快速加入卡**：标题 `提前加入一直允许` + 横向 `Grid(1* | Auto)`：左可编辑 `ComboBox`（展示 `running[{exe, title}]` 的 exe 与窗口标题），右 `允许`（`AccentButtonStyle`）。
- **一直允许列表**：
  - 区块头：`一直允许` + 计数微标（`9`）+ 右侧简短说明 `保存在授权文件里，重启后依然有效`。
  - 单张 `SurfaceCard` + 内部 `1px` 分隔线。**去掉每行左侧无意义的通用窗口小图标**，每行干净 2 列：
    - Col 0 (`1*`)：`exe`（`MonoTitleStyle`）+ `加入于 YYYY-MM-DD HH:mm:ss`（`CaptionTextStyle`）
    - Col 1 (`Auto`)：`撤销`（`DangerGhostButtonStyle`）
  - **撤销后「恢复」交互规范**：
    - 点击「一直允许」行的 `撤销` 立即调用 `apps_revoke`（不弹确认框），同时在右下角 `OverlayNotice` 弹出可恢复提示：
      - **Severity**：`Informational`
      - **Title**：`已撤销 {exe} 的授权`
      - **Message**：`AI 下次操作该程序前会重新弹窗询问。`
      - **ActionButton**：`恢复`（点击调用 `apps_allow(exe)` 恢复一直允许，刷新列表并将提示切换为 3 秒 `Success`：`已恢复 {exe} 的一直允许`）
      - **停留时长**：**8 秒**（鼠标悬停暂停倒计时，移出继续）。
    - 点击「本次运行临时允许」行的 `撤销` 时，不提供「恢复」按钮（避免误将其写入永久一直允许），仅弹 4 秒 `Success`：`已撤销 {exe} 的临时授权`。
- **允许到 winhand 重启**：有数据时同行样式；无数据时收敛为单行紧凑静音卡（`本次运行暂无临时授权的程序`）。
- **刚被拒绝（冷却中）**：仅当 `denied.Count > 0` 时动态显现，标示 `exe` 与剩余冷却时间（`left_s`）。
- **底注**：单段弱色规则脚注（截图/OCR 不受限；终端默认禁止 AI 直接打字）。

---

### 4.7 中转（`relay`）
- **页首行**：`中转` + 右侧状态微标（`StatusBadge*Style`）。
- **异常横幅**（按需）：`refused`（401）等状态置顶红色 `InfoBar`。
- **配置表单卡**（`SurfaceCard`，Padding `20`，`Spacing=16`）：
  - `中转地址`：`TextBox`（等宽字体，实时校验 `ws://`/`wss://`）。
  - `设备令牌`：标签行左 `设备令牌`、右绿标 `● 已保存`（`has_token == true` 时）；下方 `PasswordBox`（`PlaceholderText="已保存；留空保持不变"`，**绝不回显**）。
  - 操作行：`保存并连接`（`AccentButtonStyle`）+ 副注“仅主动外连 Cloudflare 中转，不开放任何入站端口”。
- **连接事实卡**：当前中转主机、在线时长、`config.toml` 路径（配 `打开` 按钮）与终端互斥锁提示。

---

### 4.8 设置（`settings`，页脚常驻，4 块纯本机偏好，~1 屏全览）
使用 `SettingsRowCardStyle` 紧凑堆叠（组内 `4px`，组间 `20px`）：
1. **启动与托盘**：`登录 Windows 后自动启动` 开关行（`Autostart.IsStale` 时下方显现黄色 `InfoBar` 提示修复）+ 1 行托盘退出说明。
2. **软件更新**：当前版本与检查状态 + 动态主按钮（`检查更新` / `下载更新` / `重启并完成更新`，下载中切换 `ProgressBar` + `取消`）+ 有更新时内嵌可滚 `ReleaseNotes` 凹槽 + 下行 `自动检查更新` 开关与 `所有版本与更新记录` 外链。
3. **本机数据**：单张卡片内 3 行（`配置文件`、`日志`、`活动记录`），等宽路径 + 右侧 `GhostButton` `[E8DA] 打开`。
4. **关于**：紧凑单卡：桌面端/后端版本号 + 后端 exe 路径（可选中）+ 右侧 `GitHub 项目主页` 外链按钮。

---

## 5. 零依赖完整共享样式（`App.xaml` 落地方案）

以下样式全部基于 WinUI 3 原生 `ThemeResource` 与 VisualStateManager，**零外部 NuGet 依赖**，完整覆盖 **Normal / PointerOver / Pressed / Disabled / Keyboard Focus** 五态，并兼容浅色、深色与高对比度主题。

### 5.1 主题资源字典（`ThemeDictionaries` 核心品牌与状态画刷）

```xml
<ResourceDictionary.ThemeDictionaries>
  <ResourceDictionary x:Key="Light">
    <!-- 覆写系统 Accent 贯通 AccentButton / ToggleSwitch / SelectorBar / NavIndicator -->
    <Color x:Key="SystemAccentColor">#4F63D2</Color>
    <SolidColorBrush x:Key="AccentFillColorDefaultBrush" Color="#4F63D2" />
    <SolidColorBrush x:Key="AccentFillColorSecondaryBrush" Color="#4254BC" />
    <SolidColorBrush x:Key="AccentFillColorTertiaryBrush" Color="#3747A3" />
    <SolidColorBrush x:Key="AccentTextFillColorPrimaryBrush" Color="#3F51B5" />
    <SolidColorBrush x:Key="TextOnAccentFillColorPrimaryBrush" Color="#FFFFFF" />

    <SolidColorBrush x:Key="WinhandCanvasBrush" Color="#F9FAFC" />
    <SolidColorBrush x:Key="WinhandCodeBgBrush" Color="#F1F3F7" />
    <SolidColorBrush x:Key="WinhandAccentSoftBgBrush" Color="#EEF1FC" />

    <!-- WarningTinted 专属画刷（30s 防截屏显示态） -->
    <SolidColorBrush x:Key="WinhandWarningTintBgBrush" Color="#FFF4CE" />
    <SolidColorBrush x:Key="WinhandWarningTintBgHoverBrush" Color="#FFEAA8" />
    <SolidColorBrush x:Key="WinhandWarningTintBgPressedBrush" Color="#FFE082" />
    <SolidColorBrush x:Key="WinhandWarningTintBorderBrush" Color="#48C77800" />
    <SolidColorBrush x:Key="WinhandWarningTintFgBrush" Color="#8A4F00" />

    <!-- 状态微标四语气 + 中性 -->
    <SolidColorBrush x:Key="BadgeNeutralBgBrush" Color="#EFF1F5" />
    <SolidColorBrush x:Key="BadgeNeutralFgBrush" Color="#5C6370" />
    <SolidColorBrush x:Key="BadgeSuccessBgBrush" Color="#E7F6EC" />
    <SolidColorBrush x:Key="BadgeSuccessFgBrush" Color="#107C41" />
    <SolidColorBrush x:Key="BadgeActiveBgBrush" Color="#EEF1FC" />
    <SolidColorBrush x:Key="BadgeActiveFgBrush" Color="#3F51B5" />
    <SolidColorBrush x:Key="BadgeWarningBgBrush" Color="#FFF4CE" />
    <SolidColorBrush x:Key="BadgeWarningFgBrush" Color="#9D5D00" />
    <SolidColorBrush x:Key="BadgeCriticalBgBrush" Color="#FDE7E9" />
    <SolidColorBrush x:Key="BadgeCriticalFgBrush" Color="#C42B1C" />
  </ResourceDictionary>

  <ResourceDictionary x:Key="Dark">
    <Color x:Key="SystemAccentColor">#8EA0F8</Color>
    <SolidColorBrush x:Key="AccentFillColorDefaultBrush" Color="#8EA0F8" />
    <SolidColorBrush x:Key="AccentFillColorSecondaryBrush" Color="#A3B2FA" />
    <SolidColorBrush x:Key="AccentFillColorTertiaryBrush" Color="#788CEB" />
    <SolidColorBrush x:Key="AccentTextFillColorPrimaryBrush" Color="#A8B7FF" />
    <SolidColorBrush x:Key="TextOnAccentFillColorPrimaryBrush" Color="#0F1426" />

    <SolidColorBrush x:Key="WinhandCanvasBrush" Color="#23252C" />
    <SolidColorBrush x:Key="WinhandCodeBgBrush" Color="#1B1D23" />
    <SolidColorBrush x:Key="WinhandAccentSoftBgBrush" Color="#248EA0F8" />

    <SolidColorBrush x:Key="WinhandWarningTintBgBrush" Color="#24F5B82E" />
    <SolidColorBrush x:Key="WinhandWarningTintBgHoverBrush" Color="#34F5B82E" />
    <SolidColorBrush x:Key="WinhandWarningTintBgPressedBrush" Color="#1CF5B82E" />
    <SolidColorBrush x:Key="WinhandWarningTintBorderBrush" Color="#4DF5B82E" />
    <SolidColorBrush x:Key="WinhandWarningTintFgBrush" Color="#FCD14D" />

    <SolidColorBrush x:Key="BadgeNeutralBgBrush" Color="#18FFFFFF" />
    <SolidColorBrush x:Key="BadgeNeutralFgBrush" Color="#A9B0BE" />
    <SolidColorBrush x:Key="BadgeSuccessBgBrush" Color="#225AD082" />
    <SolidColorBrush x:Key="BadgeSuccessFgBrush" Color="#5AD082" />
    <SolidColorBrush x:Key="BadgeActiveBgBrush" Color="#248EA0F8" />
    <SolidColorBrush x:Key="BadgeActiveFgBrush" Color="#A8B7FF" />
    <SolidColorBrush x:Key="BadgeWarningBgBrush" Color="#22FCD14D" />
    <SolidColorBrush x:Key="BadgeWarningFgBrush" Color="#FCD14D" />
    <SolidColorBrush x:Key="BadgeCriticalBgBrush" Color="#24FF8C96" />
    <SolidColorBrush x:Key="BadgeCriticalFgBrush" Color="#FF8C96" />
  </ResourceDictionary>
</ResourceDictionary.ThemeDictionaries>
```

### 5.2 `SettingsRowCardStyle`（静态行卡与可交互行卡）

```xml
<!-- 静态独立设置行卡（替代 CommunityToolkit SettingsCard） -->
<Style x:Key="SettingsRowCardStyle" TargetType="ContentControl">
  <Setter Property="Padding" Value="18,14" />
  <Setter Property="MinHeight" Value="64" />
  <Setter Property="HorizontalAlignment" Value="Stretch" />
  <Setter Property="HorizontalContentAlignment" Value="Stretch" />
  <Setter Property="VerticalContentAlignment" Value="Center" />
  <Setter Property="IsTabStop" Value="False" />
  <Setter Property="Template">
    <Setter.Value>
      <ControlTemplate TargetType="ContentControl">
        <Border Background="{ThemeResource CardBackgroundFillColorDefaultBrush}"
                BorderBrush="{ThemeResource CardStrokeColorDefaultBrush}"
                BorderThickness="1" CornerRadius="8" Padding="{TemplateBinding Padding}">
          <ContentPresenter HorizontalContentAlignment="{TemplateBinding HorizontalContentAlignment}"
                            VerticalContentAlignment="{TemplateBinding VerticalContentAlignment}" />
        </Border>
      </ControlTemplate>
    </Setter.Value>
  </Setter>
</Style>

<!-- 可整行点击的设置/活动行按钮（完整悬停、按下、禁用、键盘焦点态） -->
<Style x:Key="InteractiveRowButtonStyle" TargetType="Button">
  <Setter Property="Background" Value="Transparent" />
  <Setter Property="BorderBrush" Value="Transparent" />
  <Setter Property="BorderThickness" Value="0" />
  <Setter Property="CornerRadius" Value="6" />
  <Setter Property="Padding" Value="12,10" />
  <Setter Property="HorizontalAlignment" Value="Stretch" />
  <Setter Property="HorizontalContentAlignment" Value="Stretch" />
  <Setter Property="UseSystemFocusVisuals" Value="True" />
  <Setter Property="FocusVisualMargin" Value="-2" />
  <Setter Property="Template">
    <Setter.Value>
      <ControlTemplate TargetType="Button">
        <Border x:Name="RootBorder" Background="{TemplateBinding Background}"
                BorderBrush="{TemplateBinding BorderBrush}" BorderThickness="{TemplateBinding BorderThickness}"
                CornerRadius="{TemplateBinding CornerRadius}" Padding="{TemplateBinding Padding}">
          <VisualStateManager.VisualStateGroups>
            <VisualStateGroup x:Name="CommonStates">
              <VisualState x:Name="Normal" />
              <VisualState x:Name="PointerOver">
                <Storyboard>
                  <ObjectAnimationUsingKeyFrames Storyboard.TargetName="RootBorder" Storyboard.TargetProperty="Background">
                    <DiscreteObjectKeyFrame KeyTime="0" Value="{ThemeResource SubtleFillColorSecondaryBrush}" />
                  </ObjectAnimationUsingKeyFrames>
                </Storyboard>
              </VisualState>
              <VisualState x:Name="Pressed">
                <Storyboard>
                  <ObjectAnimationUsingKeyFrames Storyboard.TargetName="RootBorder" Storyboard.TargetProperty="Background">
                    <DiscreteObjectKeyFrame KeyTime="0" Value="{ThemeResource SubtleFillColorTertiaryBrush}" />
                  </ObjectAnimationUsingKeyFrames>
                </Storyboard>
              </VisualState>
              <VisualState x:Name="Disabled">
                <Storyboard>
                  <DoubleAnimation Storyboard.TargetName="RootBorder" Storyboard.TargetProperty="Opacity" To="0.45" Duration="0" />
                </Storyboard>
              </VisualState>
            </VisualStateGroup>
          </VisualStateManager.VisualStateGroups>
          <ContentPresenter HorizontalContentAlignment="{TemplateBinding HorizontalContentAlignment}"
                            VerticalContentAlignment="{TemplateBinding VerticalContentAlignment}" />
        </Border>
      </ControlTemplate>
    </Setter.Value>
  </Setter>
</Style>
```

### 5.3 四类核心按钮样式（均继承原生 FocusVisual 与四交互态）

```xml
<!-- 1. GhostButton：透明轻操作（显示、复制、断开、审计日志、打开文件） -->
<Style x:Key="GhostButtonStyle" TargetType="Button" BasedOn="{StaticResource DefaultButtonStyle}">
  <Setter Property="MinHeight" Value="30" />
  <Setter Property="Padding" Value="10,4" />
  <Setter Property="CornerRadius" Value="6" />
  <Setter Property="BorderThickness" Value="0" />
  <Setter Property="UseSystemFocusVisuals" Value="True" />
  <Setter Property="Background" Value="Transparent" />
  <Setter Property="Foreground" Value="{ThemeResource TextFillColorSecondaryBrush}" />
  <Setter Property="Resources">
    <ResourceDictionary>
      <SolidColorBrush x:Key="ButtonBackground" Color="Transparent" />
      <StaticResource x:Key="ButtonBackgroundPointerOver" ResourceKey="SubtleFillColorSecondaryBrush" />
      <StaticResource x:Key="ButtonBackgroundPressed" ResourceKey="SubtleFillColorTertiaryBrush" />
      <SolidColorBrush x:Key="ButtonBackgroundDisabled" Color="Transparent" />
      <StaticResource x:Key="ButtonForeground" ResourceKey="TextFillColorSecondaryBrush" />
      <StaticResource x:Key="ButtonForegroundPointerOver" ResourceKey="TextFillColorPrimaryBrush" />
      <StaticResource x:Key="ButtonForegroundPressed" ResourceKey="TextFillColorSecondaryBrush" />
      <StaticResource x:Key="ButtonForegroundDisabled" ResourceKey="TextFillColorDisabledBrush" />
      <SolidColorBrush x:Key="ButtonBorderBrush" Color="Transparent" />
      <SolidColorBrush x:Key="ButtonBorderBrushPointerOver" Color="Transparent" />
      <SolidColorBrush x:Key="ButtonBorderBrushPressed" Color="Transparent" />
      <SolidColorBrush x:Key="ButtonBorderBrushDisabled" Color="Transparent" />
    </ResourceDictionary>
  </Setter>
</Style>

<!-- 2. DangerGhostButton：常态克制、悬停警示红（程序授权「撤销」） -->
<Style x:Key="DangerGhostButtonStyle" TargetType="Button" BasedOn="{StaticResource GhostButtonStyle}">
  <Setter Property="Resources">
    <ResourceDictionary>
      <SolidColorBrush x:Key="ButtonBackground" Color="Transparent" />
      <StaticResource x:Key="ButtonBackgroundPointerOver" ResourceKey="BadgeCriticalBgBrush" />
      <StaticResource x:Key="ButtonBackgroundPressed" ResourceKey="BadgeCriticalBgBrush" />
      <SolidColorBrush x:Key="ButtonBackgroundDisabled" Color="Transparent" />
      <StaticResource x:Key="ButtonForeground" ResourceKey="TextFillColorSecondaryBrush" />
      <StaticResource x:Key="ButtonForegroundPointerOver" ResourceKey="BadgeCriticalFgBrush" />
      <StaticResource x:Key="ButtonForegroundPressed" ResourceKey="BadgeCriticalFgBrush" />
      <StaticResource x:Key="ButtonForegroundDisabled" ResourceKey="TextFillColorDisabledBrush" />
      <SolidColorBrush x:Key="ButtonBorderBrush" Color="Transparent" />
      <SolidColorBrush x:Key="ButtonBorderBrushPointerOver" Color="Transparent" />
      <SolidColorBrush x:Key="ButtonBorderBrushPressed" Color="Transparent" />
      <SolidColorBrush x:Key="ButtonBorderBrushDisabled" Color="Transparent" />
    </ResourceDictionary>
  </Setter>
</Style>

<!-- 3. IconGhostButton：纯图标透明按钮（折叠侧栏、溢出 ⋯、帮助 ?、关闭抽屉 ×） -->
<Style x:Key="IconGhostButtonStyle" TargetType="Button" BasedOn="{StaticResource GhostButtonStyle}">
  <Setter Property="Width" Value="30" />
  <Setter Property="Height" Value="30" />
  <Setter Property="MinWidth" Value="30" />
  <Setter Property="MinHeight" Value="30" />
  <Setter Property="Padding" Value="0" />
  <Setter Property="HorizontalContentAlignment" Value="Center" />
  <Setter Property="VerticalContentAlignment" Value="Center" />
</Style>

<!-- 4. WarningTintedButton：琥珀色高可见警示轻按钮（密钥显示态「隐藏」） -->
<Style x:Key="WarningTintedButtonStyle" TargetType="Button" BasedOn="{StaticResource DefaultButtonStyle}">
  <Setter Property="MinHeight" Value="30" />
  <Setter Property="Padding" Value="10,4" />
  <Setter Property="CornerRadius" Value="6" />
  <Setter Property="FontWeight" Value="SemiBold" />
  <Setter Property="UseSystemFocusVisuals" Value="True" />
  <Setter Property="Resources">
    <ResourceDictionary>
      <StaticResource x:Key="ButtonBackground" ResourceKey="WinhandWarningTintBgBrush" />
      <StaticResource x:Key="ButtonBackgroundPointerOver" ResourceKey="WinhandWarningTintBgHoverBrush" />
      <StaticResource x:Key="ButtonBackgroundPressed" ResourceKey="WinhandWarningTintBgPressedBrush" />
      <StaticResource x:Key="ButtonBackgroundDisabled" ResourceKey="ControlFillColorDisabledBrush" />
      <StaticResource x:Key="ButtonForeground" ResourceKey="WinhandWarningTintFgBrush" />
      <StaticResource x:Key="ButtonForegroundPointerOver" ResourceKey="WinhandWarningTintFgBrush" />
      <StaticResource x:Key="ButtonForegroundPressed" ResourceKey="WinhandWarningTintFgBrush" />
      <StaticResource x:Key="ButtonForegroundDisabled" ResourceKey="TextFillColorDisabledBrush" />
      <StaticResource x:Key="ButtonBorderBrush" ResourceKey="WinhandWarningTintBorderBrush" />
      <StaticResource x:Key="ButtonBorderBrushPointerOver" ResourceKey="WinhandWarningTintBorderBrush" />
      <StaticResource x:Key="ButtonBorderBrushPressed" ResourceKey="WinhandWarningTintBorderBrush" />
      <SolidColorBrush x:Key="ButtonBorderBrushDisabled" Color="Transparent" />
    </ResourceDictionary>
  </Setter>
</Style>
```

### 5.4 `StatusBadge` 四种语义语气 + 中性态

```xml
<Style x:Key="StatusBadgeNeutralStyle" TargetType="ContentControl">
  <Setter Property="Height" Value="22" />
  <Setter Property="Padding" Value="8,0" />
  <Setter Property="FontSize" Value="12" />
  <Setter Property="FontWeight" Value="SemiBold" />
  <Setter Property="IsTabStop" Value="False" />
  <Setter Property="HorizontalAlignment" Value="Left" />
  <Setter Property="VerticalAlignment" Value="Center" />
  <Setter Property="Background" Value="{ThemeResource BadgeNeutralBgBrush}" />
  <Setter Property="Foreground" Value="{ThemeResource BadgeNeutralFgBrush}" />
  <Setter Property="Template">
    <Setter.Value>
      <ControlTemplate TargetType="ContentControl">
        <Border Background="{TemplateBinding Background}" CornerRadius="4" Padding="{TemplateBinding Padding}">
          <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center"
                            Foreground="{TemplateBinding Foreground}" />
        </Border>
      </ControlTemplate>
    </Setter.Value>
  </Setter>
</Style>

<Style x:Key="StatusBadgeSuccessStyle" TargetType="ContentControl" BasedOn="{StaticResource StatusBadgeNeutralStyle}">
  <Setter Property="Background" Value="{ThemeResource BadgeSuccessBgBrush}" />
  <Setter Property="Foreground" Value="{ThemeResource BadgeSuccessFgBrush}" />
</Style>
<Style x:Key="StatusBadgeActiveStyle" TargetType="ContentControl" BasedOn="{StaticResource StatusBadgeNeutralStyle}">
  <Setter Property="Background" Value="{ThemeResource BadgeActiveBgBrush}" />
  <Setter Property="Foreground" Value="{ThemeResource BadgeActiveFgBrush}" />
</Style>
<Style x:Key="StatusBadgeWarningStyle" TargetType="ContentControl" BasedOn="{StaticResource StatusBadgeNeutralStyle}">
  <Setter Property="Background" Value="{ThemeResource BadgeWarningBgBrush}" />
  <Setter Property="Foreground" Value="{ThemeResource BadgeWarningFgBrush}" />
</Style>
<Style x:Key="StatusBadgeCriticalStyle" TargetType="ContentControl" BasedOn="{StaticResource StatusBadgeNeutralStyle}">
  <Setter Property="Background" Value="{ThemeResource BadgeCriticalBgBrush}" />
  <Setter Property="Foreground" Value="{ThemeResource BadgeCriticalFgBrush}" />
</Style>
```

---

## 6. 逐页截图验收要点（供 `WINHAND_DESKTOP_PAGE` 对照自检）

以下条目全部面向 `1280×1528 @ 150% DPI`（`853×1018` 逻辑像素）截图肉眼验收，每条均可直接判「通过 / 不通过」：

### 6.1 全局外壳与导航栏（每张截图均须满足）
1. 窗口顶部为融合的一体化 40px 浅色/深色标题栏，**不存在黑色原生 Win32 标题条**；最左侧有折叠图标按钮、16px `winhand` 图标与标题。
2. 左侧导航与右侧内容区存在清晰的表面分层（侧栏透 Mica 轻冷灰，内容区左上角 `8px` 圆角承接）。
3. 顶部**不再存在** 52px 全局二级白条、不再有重复的右上角 `已连接` 胶囊，也不再有孤立的 `✓` 方形按钮。
4. 侧栏分组为顶部 3 项（`概览` / `活动` / `MCP 服务`，仅在有会话时多出带计数徽章的 `会话`）、分隔线 +「安全与连接」3 项（`密钥` / `程序授权` / `中转`）、底栏 `设置`。
5. 左下角看板娘尺寸为 `88×88` 且**底部蓝窗完整无裁切**，右侧紧邻两行状态锚点（圆点+状态词、版本与在线时长），不再有孤零零飘在角落的小绿点。
6. 选中态指示条、主按钮与开启态开关均为长春花蓝（Light `#4F63D2` / Dark `#8EA0F8`），而非系统炭灰或默认深蓝；深色模式下无刺眼纯白区块或不可读暗字。

### 6.2 概览（`WINHAND_DESKTOP_PAGE=overview` · 对照 `mockups/overview.html`）
1. 全屏仅存在 **2 处**状态表达（侧栏左下角状态锚点 + 主连接卡），不再出现 3 处重复的 `已连接` 胶囊。
2. 连接卡内不再把中转域名与 `wss://...` URL 重复展示两行；右侧「断开」为无边框轻按钮。
3. 今日调用数、MCP 服务数、活动会话数位于连接卡下半区的 **3 列紧凑指标条**内（24px 等宽数字），**没有** 4 宫格彩色大图标 KPI 卡片。
4. 「最近活动」区块头右侧有带右箭头的蓝色 `查看全部 ›` 链接，卡片下方**没有**“点击任意一条…”解释文字。
5. 活动行每行包含等宽时间、标题（工具名等宽、中文自然语言用界面字体）、等宽单行参数预览、等宽耗时与 `4px` 圆角状态徽标，无行高错乱或文字重叠。

### 6.3 活动（`WINHAND_DESKTOP_PAGE=activity` · 对照 `mockups/activity.html`）
1. 顶部工具栏清晰分两行：第 1 行标题 + 搜索框 + 透明 `审计日志` 按钮；第 2 行 `SelectorBar` 的 **4 个页签（全部 / 工具调用 / 连接 / 失败）全部完整可见**，无任何截断。
2. 未选中记录时，列表占满下方 100% 高度，**不存在 60% 大片空白下半屏**，也**不再出现**“在左边选择一条记录”文案。
3. 选中一条记录后（853px 展开侧栏），底部展开详情抽屉且顶边有**清晰 `1px` 水平分隔线与居中拖拽握把**，抽屉顶栏包含标题、状态微标、`复制 JSON` 与 `×` 关闭按钮。
4. 详情内的结果与参数使用无外框圆角凹槽 `CodeBlock`（`WinhandCodeBgBrush`）和等宽字体，绝不出现“卡片套卡片”。
5. 手动折叠侧栏后（内容区 `805px`），活动页自动切换为左右双栏，中间有清晰 `1px` 竖线分隔。
6. 若存在活跃会话，侧栏「活动」下方显示「会话」并带数量徽章；无会话时侧栏不显示「会话」。

### 6.4 MCP 服务（`WINHAND_DESKTOP_PAGE=mcp` · 对照 `mockups/mcp.html`）
1. 853px 窗口下直接呈 **左右双栏**（左目录约 `200px` + `1px` 竖向分隔线 + 右详情），**绝不退化成上下堆叠，也不得把列表最后一行切半腰斩**。
2. 左栏顶部为 `MCP 服务` 标题 + `+ 添加`（主按钮）与 `导入…`（次按钮）；列表每行仅显示圆点、服务名与简短状态词，不再每行重复“第一次被调用时启动”。
3. 右栏详情头右侧有 `编辑` 与 `⋯` 溢出按钮，**页面底部不再有单独的“删除这个服务”整行卡片**。
4. 远程地址行 URL 单行省略不换行撑爆卡片，右侧 `复制` 按钮垂直居中对齐。
5. 运行状态卡事实表标签列紧凑（约 `68px`），无巨大空洞；操作按钮横排对齐于事实表下方；启动方式命令在 `CodeBlock` 中清晰可读。

### 6.5 密钥（`WINHAND_DESKTOP_PAGE=secrets` · 对照 `mockups/secrets.html`）
1. 页首行左侧为 `密钥` + 数量徽章 + `?` 帮助图标，右侧为置顶的长春花蓝 `+ 添加密钥` 主按钮（不再沉在列表底部）；无 3 行大段常驻说明占屏。
2. 页首下方有搜索框（带放大镜图标）与右侧 `secrets.env` 定位按钮。
3. 列表在单张圆角卡片内以 `1px` 细线分隔，每行右侧仅有 **`显示`、`复制` 两个无边框透明按钮 + `⋯` 溢出图标按钮**，彻底消灭 4 个带边框矩形按钮墙；「删除」只出现在 `⋯` 菜单内且标红。
4. 点击「显示」后，顶部出现琥珀色防截屏保护 `InfoBar`（含倒计时秒数、`立即隐藏` 单行不折行按钮及底边 `2px` 进度条），该行明文呈现在带琥珀细框的等宽盒内且按钮切为琥珀色「隐藏」。
5. 触发复制或操作提示时，右下角浮动 `InfoBar` 出现且**不遮挡**右上角 `+ 添加密钥` 按钮。

### 6.6 程序授权（`WINHAND_DESKTOP_PAGE=apps` · 对照 `mockups/apps.html`）
1. 页首行左侧为 `程序授权` + `?` 帮助图标，右侧为 `desktop-apps.toml` 定位按钮；无大段常驻段落压顶。
2. `AI 操作程序前先问我` 总开关卡位于首屏最上方，左侧带柔和蓝底盾牌图标、右侧长春花蓝 `ToggleSwitch`。
3. `提前加入一直允许` 输入条（可编辑 `ComboBox` + 长春花蓝 `允许` 按钮）位于「一直允许」列表**上方**，不再沉底。
4. 「一直允许」单张卡片内每行**没有重复的通用窗口小图标**，仅左侧等宽 exe 名 + 加入时间，右侧透明轻文字 `撤销` 按钮（无硬边框方块墙）。
5. 「允许到 winhand 重启」为空时收敛为单行紧凑静音卡，不浪费大块垂直留白；规则脚注以弱化小字静置于页面最底部。
6. 撤销「一直允许」条目后，右下角出现带 `恢复` 按钮的 `InfoBar`（停留 8 秒），不遮挡顶部开关与添加条。

### 6.7 中转（`WINHAND_DESKTOP_PAGE=relay`）与设置（`WINHAND_DESKTOP_PAGE=settings`）
1. **中转页**：单张配置表单卡内含等宽 `中转地址`、`设备令牌`（已保存时标绿 `● 已保存`、输入框仅显示占位提示、绝不回显明文）与 `保存并连接` 主按钮；下方配紧凑连接事实卡。
2. **设置页**：仅包含「启动与托盘」「软件更新」「本机数据」「关于」4 块纯客户端偏好，在 `853×1018` 窗口下约 1 屏即可全览，绝不再出现密钥、程序授权或中转表单。
3. 设置页「本机数据」3 行路径使用等宽字体单行展示，右侧为无边框透明 `打开` 按钮，无硬框重复；有新版本可用时侧栏底端「设置」项亮起蓝色点状 `InfoBadge`。
4. 全部页面中均无卡片套卡片、无左侧粗彩条、无 Emoji 图标、无英文全大写装饰 Eyebrow。
